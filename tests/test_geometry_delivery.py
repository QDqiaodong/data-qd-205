import copy
import unittest

from micro_objects import (ReviewStore, build_delta, build_records,
                           compile_snapshot, manifest_digest, parse_delta,
                           record_digest)


def canvas_snapshot(version, tiles, *, width=7, height=5,
                    connectivity=4, selection=None):
    return {
        'dataset_id': 'geo', 'version_id': version,
        'width': width, 'height': height,
        'pixel_size': ['2', '1'], 'connectivity': connectivity,
        'selection': selection or {'min_pixels': 2,
                                   'exclude_border': False},
        'tiles': tiles,
    }


def fill_intensity(mask, value=1):
    return [[value if char == '1' else 0 for char in row] for row in mask]


class CrossTileGeometryTests(unittest.TestCase):
    def test_ring_spanning_four_tiles_keeps_one_hole(self):
        # A 3x3 ring at canvas (1..3, 1..3), center (2,2) unobserved, split
        # over four 2x2 tiles.  A higher-priority tile later replaces one
        # ring pixel with the same foreground value: provenance changes while
        # the stitched geometry and hole count stay identical.
        ring_tiles = [
            {'tile_id': 'q-tl', 'x': 1, 'y': 1, 'priority': 1,
             'mask': ['11', '10'], 'intensity': [[1, 1], [1, 0]]},
            {'tile_id': 'q-tr', 'x': 3, 'y': 1, 'priority': 1,
             'mask': ['1', '1'], 'intensity': [[1], [1]]},
            {'tile_id': 'q-bl', 'x': 1, 'y': 3, 'priority': 1,
             'mask': ['11'], 'intensity': [[1, 1]]},
            {'tile_id': 'q-br', 'x': 3, 'y': 3, 'priority': 1,
             'mask': ['1'], 'intensity': [[1]]},
        ]
        snap_a = canvas_snapshot('A', copy.deepcopy(ring_tiles))
        compiled = compile_snapshot(snap_a)
        self.assertEqual(
            compiled['mask'],
            ['0000000', '0111000', '0101000', '0111000', '0000000'])
        records = build_records(compiled)
        self.assertEqual(len(records), 1)
        record = next(iter(records.values()))
        self.assertEqual(record['key'], [1, 1])
        self.assertEqual(record['geometry_topology_intensity']['holes'], 1)
        self.assertEqual(record['geometry_topology_intensity']['pixel_count'],
                         8)
        # Every foreground pixel is sourced; the ring center is an explicit,
        # sourced background zero rather than an unobserved gap.
        self.assertTrue(all(entry['source'] is not None
                            for entry in record['pixel_sources']))
        self.assertEqual(compiled['sources'][2][2],
                         {'tile_id': 'q-tl', 'tile_coord': [1, 1]})
        self.assertEqual(compiled['mask'][2][2], '0')

        snap_b = copy.deepcopy(snap_a)
        snap_b['version_id'] = 'B'
        snap_b['tiles'].append({
            'tile_id': 'high', 'x': 1, 'y': 1, 'priority': 9,
            'mask': ['1'], 'intensity': [[1]]})
        compiled_b = compile_snapshot(snap_b)
        self.assertEqual(compiled_b['mask'], compiled['mask'])
        self.assertEqual(compiled_b['sources'][1][1]['tile_id'], 'high')
        delta = build_delta(snap_a, snap_b)
        self.assertEqual([u['key'] for u in delta['upserts']], [[1, 1]])
        changed = delta['upserts'][0]['record']
        self.assertEqual(changed['geometry_topology_intensity']['holes'], 1)
        parse_delta(copy.deepcopy(delta))

    def test_diagonal_object_stitched_across_tiles_uses_8_connectivity(self):
        # Two separate single-pixel tiles touching only diagonally.
        snap = canvas_snapshot('A', [
            {'tile_id': 'one', 'x': 2, 'y': 2, 'priority': 0,
             'mask': ['1'], 'intensity': [[5]]},
            {'tile_id': 'two', 'x': 3, 'y': 3, 'priority': 0,
             'mask': ['1'], 'intensity': [[7]]},
        ], connectivity=8, selection={'min_pixels': 1,
                                      'exclude_border': False})
        compiled = compile_snapshot(snap)
        records = build_records(compiled)
        self.assertEqual(len(records), 1)
        record = next(iter(records.values()))
        self.assertEqual(record['key'], [2, 2])
        self.assertEqual(record['geometry_topology_intensity']
                         ['mean_intensity'], '6')
        four = compile_snapshot({**snap, 'connectivity': 4})
        self.assertEqual(len(build_records(four)), 2)

    def test_selection_change_removes_object_from_delivery_set(self):
        snap_a = canvas_snapshot('A', [
            {'tile_id': 't', 'x': 1, 'y': 1, 'priority': 0,
             'mask': ['11', '11'],
             'intensity': [[1, 2], [3, 4]]},
        ], selection={'min_pixels': 4, 'exclude_border': False})
        snap_b = copy.deepcopy(snap_a)
        snap_b['version_id'] = 'B'
        snap_b['selection'] = {'min_pixels': 5, 'exclude_border': False}
        delta = build_delta(snap_a, snap_b)
        # The object still exists on the canvas but leaves the delivery set.
        self.assertEqual(delta['upserts'], [])
        self.assertEqual(delta['deletes'], [[1, 1]])
        parse_delta(copy.deepcopy(delta))

        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(os.path.join(tmp, 's.json'), 'geo')
            store.import_delta(build_delta(None, snap_a))
            record = next(iter(
                build_records(compile_snapshot(snap_a)).values()))
            store.register_review('rv', record['key'],
                                  record_digest(record))
            receipt = store.import_delta(delta)
            self.assertEqual(receipt['deleted'], [[1, 1]])
            self.assertEqual(receipt['revoked_review_ids'], ['rv'])
            self.assertEqual(store.list_object_keys(), [])
            self.assertFalse(store.get_review('rv')['active'])
            self.assertEqual(
                store.get_review('rv')['revoked_at_version'], 'B')

    def test_manifest_digest_agrees_between_compiler_and_store(self):
        snap = canvas_snapshot('A', [
            {'tile_id': 't', 'x': 0, 'y': 0, 'priority': 0,
             'mask': ['1100000'], 'intensity': fill_intensity(['1100000'])},
        ])
        compiled = compile_snapshot(snap)
        self.assertEqual(manifest_digest(build_records(compiled)),
                         build_delta(None, snap)['to_manifest_digest'])


if __name__ == '__main__':
    unittest.main()
