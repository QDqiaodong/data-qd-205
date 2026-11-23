import copy
import json
import unittest

from micro_objects import (build_delta, build_records, compile_snapshot,
                           digest, manifest_digest, parse_delta, parse_snapshot,
                           process, record_digest)
from micro_objects.delta import EMPTY_DIGEST

WIDTH, HEIGHT = 8, 3


def low_tile():
    """Full-canvas low-priority tile; foreground at y=1, x in 1,2,3,5,6."""
    mask = ['00000000', '01110110', '00000000']
    intensity = [[0] * WIDTH for _ in range(HEIGHT)]
    for x in (1, 2, 3, 5, 6):
        intensity[1][x] = 10
    return {'tile_id': 'low', 'x': 0, 'y': 0, 'priority': 0,
            'mask': mask, 'intensity': intensity}


def snapshot(version, high=None, selection=None):
    tiles = [low_tile()]
    if high is not None:
        char, value = high
        tiles.append({'tile_id': 'high', 'x': 2, 'y': 1, 'priority': 10,
                      'mask': [char], 'intensity': [[value]]})
    return {
        'dataset_id': 'ds-1', 'version_id': version,
        'width': WIDTH, 'height': HEIGHT,
        'pixel_size': ['1', '1'], 'connectivity': 4,
        'selection': selection or {'min_pixels': 1,
                                   'exclude_border': True},
        'tiles': tiles,
    }


class TileCompilationTests(unittest.TestCase):
    def test_process_contract_preserved_on_compiled_canvas(self):
        compiled = compile_snapshot(snapshot('R0'))
        data = {k: compiled[k] for k in
                ('mask', 'intensity', 'pixel_size', 'connectivity',
                 'selection')}
        direct = process(copy.deepcopy(data))
        self.assertEqual(compiled['result'], direct)
        self.assertEqual(compiled['mask'],
                         ['00000000', '01110110', '00000000'])

    def test_foreground_keys_and_sources_r0(self):
        compiled = compile_snapshot(snapshot('R0'))
        records = build_records(compiled)
        self.assertEqual(sorted(records), [(1, 1), (5, 1)])
        for x in (1, 2, 3, 5, 6):
            self.assertEqual(compiled['sources'][1][x],
                             {'tile_id': 'low', 'tile_coord': [x, 1]})
        # The low tile explicitly observes the whole canvas, including its
        # background: explicit winning background still carries provenance.
        self.assertEqual(compiled['sources'][0][0],
                         {'tile_id': 'low', 'tile_coord': [0, 0]})
        self.assertEqual(compiled['mask'][0][0], '0')

    def test_never_observed_background_has_null_source(self):
        snap = snapshot('partial')
        snap['tiles'] = [{
            'tile_id': 'corner', 'x': 0, 'y': 0, 'priority': 0,
            'mask': ['1'], 'intensity': [[7]]}]
        compiled = compile_snapshot(snap)
        self.assertEqual(compiled['sources'][0][0],
                         {'tile_id': 'corner', 'tile_coord': [0, 0]})
        self.assertIsNone(compiled['sources'][2][7])
        self.assertEqual(compiled['mask'][2][7], '0')

    def test_high_priority_replaces_source_without_pixels_or_intensity(self):
        r0 = compile_snapshot(snapshot('R0'))
        r1 = compile_snapshot(snapshot('R1', ('1', 10)))
        self.assertEqual(r0['mask'], r1['mask'])
        self.assertEqual(r0['intensity'], r1['intensity'])
        self.assertEqual(r1['sources'][1][2],
                         {'tile_id': 'high', 'tile_coord': [0, 0]})
        # Explicit winning background is sourced too.
        r2 = compile_snapshot(snapshot('R2', ('0', 10)))
        self.assertEqual(r2['mask'][1], '01010110')
        self.assertEqual(r2['sources'][1][2],
                         {'tile_id': 'high', 'tile_coord': [0, 0]})

    def test_question_mark_is_transparent(self):
        rq = compile_snapshot(snapshot('RQ', ('?', 10)))
        self.assertEqual(rq['mask'][1], '01110110')
        # The lower tile's bridge and provenance stay visible.
        self.assertEqual(rq['sources'][1][2],
                         {'tile_id': 'low', 'tile_coord': [2, 1]})
        self.assertEqual(rq['intensity'][1][2], 10)

    def test_equal_priority_uses_lexicographically_smallest_id(self):
        snap = snapshot('T')
        snap['tiles'][0]['priority'] = 5
        snap['tiles'].append({
            'tile_id': 'aaaa', 'x': 2, 'y': 1, 'priority': 5,
            'mask': ['1'], 'intensity': [[42]]})
        compiled = compile_snapshot(snap)
        self.assertEqual(compiled['sources'][1][2]['tile_id'], 'aaaa')
        snap['tiles'][1]['tile_id'] = 'zzzz'
        compiled = compile_snapshot(snap)
        # 'low' < 'zzzz', so the original tile keeps the pixel.
        self.assertEqual(compiled['sources'][1][2]['tile_id'], 'low')

    def test_winning_zero_covers_lower_foreground(self):
        snap = snapshot('Z', ('0', 0))
        compiled = compile_snapshot(snap)
        self.assertEqual(compiled['mask'][1], '01010110')
        self.assertEqual(compiled['intensity'][1][2], 0)

    def test_tile_input_order_does_not_change_digests(self):
        a = snapshot('A', ('1', 10))
        b = copy.deepcopy(a)
        b['tiles'] = list(reversed(b['tiles']))
        self.assertEqual(manifest_digest(build_records(compile_snapshot(a))),
                         manifest_digest(build_records(compile_snapshot(b))))

    def test_digests_ignore_ephemeral_numeric_labels(self):
        r1 = compile_snapshot(snapshot('R1', ('1', 10)))
        r2 = compile_snapshot(snapshot('R2', ('0', 10)))
        # Numeric labels differ ...
        self.assertNotEqual(r1['result']['labels'], r2['result']['labels'])
        # ... but the unchanged [5,1] object keeps an identical record.
        self.assertEqual(record_digest(build_records(r1)[(5, 1)]),
                         record_digest(build_records(r2)[(5, 1)]))

    def test_pixel_size_change_changes_record_digest(self):
        a = snapshot('A', ('1', 10))
        b = copy.deepcopy(a)
        b['pixel_size'] = ['2', '1']
        self.assertNotEqual(
            record_digest(build_records(compile_snapshot(a))[(1, 1)]),
            record_digest(build_records(compile_snapshot(b))[(1, 1)]))

    def test_bad_snapshots(self):
        bad_cases = []
        bad_cases.append({**snapshot('B'), 'width': 0})
        bad_cases.append({**snapshot('B'), 'height': 257})
        bad_cases.append({**snapshot('B'), 'dataset_id': 'bad id!'})
        bad_cases.append({**snapshot('B'), 'version_id': ''})
        outside = snapshot('B')
        outside['tiles'][0]['x'] = 1
        bad_cases.append(outside)
        ragged = snapshot('B')
        ragged['tiles'][0]['mask'] = ['00', '0']
        bad_cases.append(ragged)
        bad_char = snapshot('B')
        bad_char['tiles'][0]['mask'][1] = '0x110110'
        bad_cases.append(bad_char)
        bad_priority = snapshot('B', ('1', 10))
        bad_priority['tiles'][1]['priority'] = 65536
        bad_cases.append(bad_priority)
        bad_intensity = snapshot('B')
        bad_intensity['tiles'][0]['intensity'][1][1] = 65536
        bad_cases.append(bad_intensity)
        bool_intensity = snapshot('B')
        bool_intensity['tiles'][0]['intensity'][1][1] = True
        bad_cases.append(bool_intensity)
        dup = snapshot('B', ('1', 10))
        dup['tiles'][1]['tile_id'] = 'low'
        bad_cases.append(dup)
        too_many = snapshot('B')
        too_many['tiles'] = [
            dict(low_tile(), tile_id=f't{i}', priority=0) for i in range(33)]
        bad_cases.append(too_many)
        empty_mask = snapshot('B')
        empty_mask['tiles'][0]['mask'] = []
        bad_cases.append(empty_mask)
        shape_mismatch = snapshot('B')
        shape_mismatch['tiles'][0]['intensity'].append([0] * WIDTH)
        bad_cases.append(shape_mismatch)
        unknown_field = snapshot('B')
        unknown_field['extra'] = 1
        bad_cases.append(unknown_field)
        for case in bad_cases:
            with self.subTest(case=json.dumps(case, default=str)[:60]):
                with self.assertRaises(ValueError):
                    parse_snapshot(case)
                with self.assertRaises(ValueError):
                    compile_snapshot(case)

    def test_no_tiles_is_unobserved_canvas(self):
        snap = snapshot('empty')
        snap['tiles'] = []
        compiled = compile_snapshot(snap)
        self.assertTrue(all(cell is None for row in compiled['sources']
                            for cell in row))
        self.assertEqual(compiled['result']['summary']['foreground_pixels'], 0)
        self.assertEqual(build_records(compiled), {})
        delta = build_delta(None, snap)
        self.assertEqual(delta['upserts'], [])
        self.assertEqual(delta['deletes'], [])
        self.assertEqual(delta['from_manifest_digest'],
                         delta['to_manifest_digest'])
        parse_delta(copy.deepcopy(delta))

    def test_canvas_and_tile_limits(self):
        limit = {
            'dataset_id': 'd', 'version_id': 'v', 'width': 256,
            'height': 256, 'pixel_size': ['1', '1'], 'connectivity': 4,
            'selection': {'min_pixels': 1, 'exclude_border': False},
            'tiles': [
                {'tile_id': f't{i}', 'x': 0, 'y': 0, 'priority': 0,
                 'mask': ['1'], 'intensity': [[1]]} for i in range(32)],
        }
        compiled = compile_snapshot(limit)
        self.assertEqual(compiled['sources'][0][0]['tile_id'], 't0')
        over = copy.deepcopy(limit)
        over['tiles'].append(
            {'tile_id': 'extra', 'x': 1, 'y': 0, 'priority': 0,
             'mask': ['1'], 'intensity': [[1]]})
        with self.assertRaises(ValueError):
            compile_snapshot(over)
        on_edge = copy.deepcopy(limit)
        on_edge['tiles'] = [{
            'tile_id': 'edge', 'x': 255, 'y': 255, 'priority': 0,
            'mask': ['1'], 'intensity': [[65535]]}]
        self.assertEqual(compile_snapshot(on_edge)['intensity'][255][255],
                         65535)


class DeltaBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s0 = snapshot('R0')
        cls.s1 = snapshot('R1', ('1', 10))
        cls.s2 = snapshot('R2', ('0', 10))
        cls.sq = snapshot('RQ', ('?', 10))

    def test_first_publication_uses_empty_manifest(self):
        delta = build_delta(None, self.s0)
        self.assertIsNone(delta['from_version_id'])
        self.assertEqual(delta['from_manifest_digest'], EMPTY_DIGEST)
        self.assertEqual([u['key'] for u in delta['upserts']],
                         [[1, 1], [5, 1]])
        self.assertEqual(delta['deletes'], [])
        # Round trip through the strict parser.
        parsed = parse_delta(copy.deepcopy(delta))
        self.assertEqual(parsed['delta_digest'], delta['delta_digest'])

    def test_r1_only_replaces_first_object(self):
        delta = build_delta(self.s0, self.s1)
        self.assertEqual([u['key'] for u in delta['upserts']], [[1, 1]])
        self.assertEqual(delta['deletes'], [])
        changed = delta['upserts'][0]['record']
        self.assertEqual(
            changed['pixel_sources'][1],
            {'coord': [2, 1], 'intensity': 10,
             'source': {'tile_id': 'high', 'tile_coord': [0, 0]}})

    def test_r2_split_replace_and_insert_but_not_unchanged_key(self):
        delta = build_delta(self.s1, self.s2)
        self.assertEqual([u['key'] for u in delta['upserts']],
                         [[1, 1], [3, 1]])
        self.assertEqual(delta['deletes'], [])
        # [5,1] is absent from the delta despite its numeric label change.
        self.assertNotIn([5, 1], [u['key'] for u in delta['upserts']])

    def test_question_mark_restores_low_bridge(self):
        delta = build_delta(self.s2, self.sq)
        self.assertEqual([u['key'] for u in delta['upserts']], [[1, 1]])
        source = {tuple(entry['coord']): entry['source']
                  for entry in
                  delta['upserts'][0]['record']['pixel_sources']}
        self.assertEqual(source[(2, 1)],
                         {'tile_id': 'low', 'tile_coord': [2, 1]})
        # No explicit background remains at the bridge position.
        self.assertEqual(compile_snapshot(self.sq)['mask'][1],
                         '01110110')

    def test_restore_r0_does_not_revive_anything_and_is_content_driven(self):
        # R2 -> R0: [3,1] leaves the delivery set, [1,1] changes again.
        delta = build_delta(self.s2, self.s0)
        self.assertEqual([u['key'] for u in delta['upserts']], [[1, 1]])
        self.assertEqual(delta['deletes'], [[3, 1]])

    def test_unchanged_manifest_still_publishes_a_new_version(self):
        s3 = copy.deepcopy(self.s2)
        s3['version_id'] = 'R3'
        delta = build_delta(self.s2, s3)
        self.assertEqual(delta['upserts'], [])
        self.assertEqual(delta['deletes'], [])
        self.assertEqual(delta['to_version_id'], 'R3')
        self.assertEqual(delta['from_manifest_digest'],
                         delta['to_manifest_digest'])
        self.assertNotEqual(delta['from_version_id'],
                            delta['to_version_id'])
        parse_delta(copy.deepcopy(delta))

    def test_delta_digest_covers_identity_and_manifests(self):
        delta = build_delta(self.s0, self.s1)
        body = {k: v for k, v in delta.items() if k != 'delta_digest'}
        self.assertEqual(delta['delta_digest'], digest(body))
        tampered = copy.deepcopy(delta)
        tampered['to_version_id'] = 'RX'
        self.assertNotEqual(delta['delta_digest'],
                            digest({k: v for k, v in tampered.items()
                                    if k != 'delta_digest'}))

    def test_manifest_digest_is_order_insensitive_to_inputs(self):
        delta_a = build_delta(None, self.s1)
        reordered = copy.deepcopy(self.s1)
        reordered['tiles'] = list(reversed(reordered['tiles']))
        delta_b = build_delta(None, reordered)
        self.assertEqual(delta_a['to_manifest_digest'],
                         delta_b['to_manifest_digest'])
        self.assertEqual(delta_a['delta_digest'], delta_b['delta_digest'])

    def test_cross_dataset_comparison_rejected(self):
        other = copy.deepcopy(self.s1)
        other['dataset_id'] = 'other-ds'
        with self.assertRaises(ValueError):
            build_delta(self.s0, other)


if __name__ == '__main__':
    unittest.main()
