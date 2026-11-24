import copy
import os
import random
import tempfile
import unittest

from micro_objects import (APPROVED, ArchiveError, ArchiveReader,
                           INSUFFICIENT_BUDGET, NOT_REQUESTED, build_archive,
                           process)


def header(width, height, connectivity=4, min_pixels=1, exclude_border=False,
           pixel_size=('1', '1')):
    return {
        'width': width, 'height': height,
        'pixel_size': list(pixel_size), 'connectivity': connectivity,
        'selection': {'min_pixels': min_pixels,
                      'exclude_border': exclude_border},
    }


def single_row_strips(mask_rows, offline, intensities=None):
    strips = []
    for y, row in enumerate(mask_rows):
        w = len(row)
        intensity = [intensities[y][x] if intensities else y * w + x + 1
                     for x in range(w)]
        strips.append({'band_id': chr(65 + y), 'start_row': y,
                       'offline': offline(y), 'mask': [row],
                       'intensity': [intensity]})
    return strips


class ArchiveContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = os.path.join(self.tmp.name, 'pack.db')

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, head, strips, budget):
        build_archive(self.target, head, strips, budget)
        return ArchiveReader(self.target)

    # -- The exact four-strip scenario from the acceptance brief -----------

    def test_spec_scenario_min_pixels_two(self):
        masks = ['0100', '0101', '0001', '0001']
        intensity = {
            'A': [2, 1, 3, 4], 'B': [5, 6, 7, 8],
            'C': [9, 10, 11, 12], 'D': [13, 14, 15, 16],
        }
        strips = []
        for y, (bid, row) in enumerate(zip('ABCD', masks)):
            strips.append({'band_id': bid, 'start_row': y,
                           'offline': bid in ('A', 'B'),
                           'mask': [row], 'intensity': [intensity[bid]]})
        r = self.build(header(4, 4, min_pixels=2), strips, 3)
        self.assertEqual(r.list_object_keys(), [[1, 0], [3, 1]])
        obj = r.get_object([1, 0])
        self.assertEqual(obj['pixel_count'], 2)
        self.assertEqual(obj['bbox'], [1, 0, 2, 2])
        decisions = {d['band_id']: d for d in r.list_band_decisions()}
        self.assertEqual(decisions['A']['decision'], APPROVED)
        self.assertEqual(decisions['A']['cost'], 3)
        self.assertEqual(decisions['B']['decision'], INSUFFICIENT_BUDGET)
        self.assertEqual(decisions['B']['cost'], 2)
        self.assertEqual(decisions['C']['decision'], NOT_REQUESTED)
        self.assertEqual(decisions['D']['decision'], NOT_REQUESTED)
        self.assertEqual(r.used_budget(), 3)
        # A is recoverable with its exact mask and original intensities.
        recovered = r.recover_band('A')
        self.assertEqual(recovered['mask'], ['0100'])
        self.assertEqual(recovered['intensity'], [intensity['A']])
        self.assertEqual(recovered['start_row'], 0)
        # B was not approved; its recovery is refused.
        with self.assertRaises(ArchiveError):
            r.recover_band('B')
        # Unrequested bands cannot be recovered.
        for bid in ('C', 'D'):
            with self.assertRaises(ArchiveError):
                r.recover_band(bid)
        # Unknown band id is rejected rather than silently fabricated.
        with self.assertRaises(ArchiveError):
            r.recover_band('ZZ')
        r.close()

    def test_spec_scenario_min_pixels_three(self):
        masks = ['0100', '0101', '0001', '0001']
        intensity = {
            'A': [2, 1, 3, 4], 'B': [5, 6, 7, 8],
            'C': [9, 10, 11, 12], 'D': [13, 14, 15, 16],
        }
        strips = []
        for y, (bid, row) in enumerate(zip('ABCD', masks)):
            strips.append({'band_id': bid, 'start_row': y,
                           'offline': bid in ('A', 'B'),
                           'mask': [row], 'intensity': [intensity[bid]]})
        r = self.build(header(4, 4, min_pixels=3), strips, 3)
        # [1,0] drops out of the index; [3,1] remains (three pixels).
        self.assertEqual(r.list_object_keys(), [[3, 1]])
        self.assertFalse(r.has_object([1, 0]))
        decisions = {d['band_id']: d for d in r.list_band_decisions()}
        # A now costs 4 (its foreground is no longer selected).
        self.assertEqual(decisions['A']['decision'], INSUFFICIENT_BUDGET)
        self.assertEqual(decisions['A']['cost'], 4)
        # B costs 3 and is approved with the remaining full budget.
        self.assertEqual(decisions['B']['decision'], APPROVED)
        self.assertEqual(decisions['B']['cost'], 3)
        self.assertEqual(r.used_budget(), 3)
        recovered = r.recover_band('B')
        # B's recovery includes the filtered-out foreground at (1,1) and all
        # background cells, with the original intensities.
        self.assertEqual(recovered['mask'], ['0101'])
        self.assertEqual(recovered['intensity'], [intensity['B']])
        # A cannot be recovered and its non-delivered cells are not retained.
        with self.assertRaises(ArchiveError):
            r.recover_band('A')
        # The selected [3,1] foreground from B is still readable as an object.
        samples = r.read_object_samples([3, 1])
        self.assertEqual([s['coord'] for s in samples],
                         [[3, 1], [3, 2], [3, 3]])
        r.close()

    def test_metrics_match_process_and_survive_repartition(self):
        rng = random.Random(2024)
        for trial in range(40):
            width = rng.randrange(1, 12)
            height = rng.randrange(1, 18)
            connectivity = rng.choice((4, 8))
            mask = [''.join('1' if rng.random() < 0.4 else '0'
                            for _ in range(width)) for _ in range(height)]
            intensity = [[rng.randrange(65536) for _ in range(width)]
                         for _ in range(height)]
            pixel_size = (str(rng.randrange(1, 5)), str(rng.randrange(1, 5)))
            min_pixels = rng.randrange(1, max(2, width * height + 1))
            exclude_border = rng.choice((True, False))
            head = header(width, height, connectivity, min_pixels,
                          exclude_border, pixel_size)
            data = {'mask': mask, 'intensity': intensity,
                    'pixel_size': list(pixel_size),
                    'connectivity': connectivity,
                    'selection': {'min_pixels': min_pixels,
                                  'exclude_border': exclude_border}}
            reference = process(data)
            expected = {tuple(o['pixels'][0]): o
                        for o in reference['objects'] if o['accepted']}
            # Two different strip partitions must produce identical objects.
            for partition, rows_per in enumerate((1, rng.choice((2, 3, 7)))):
                strips = []
                y, idx = 0, 0
                while y < height:
                    h = min(rows_per, height - y)
                    strips.append({
                        'band_id': f's{idx}', 'start_row': y,
                        'offline': False, 'mask': mask[y:y + h],
                        'intensity': intensity[y:y + h]})
                    y += h
                    idx += 1
                target = os.path.join(
                    self.tmp.name, f'p{trial}_{partition}_{rows_per}.db')
                build_archive(target, head, strips, width * height)
                reader = ArchiveReader(target)
                self.assertEqual(
                    sorted(map(tuple, reader.list_object_keys())),
                    sorted(expected))
                for key, obj in expected.items():
                    got = reader.get_object(list(key))
                    for field in ('pixel_count', 'bbox', 'area',
                                  'edge_perimeter', 'centroid',
                                  'intensity_sum', 'mean_intensity',
                                  'holes', 'touches_border'):
                        self.assertEqual(got[field], obj[field], field)
                    samples = reader.read_object_samples(list(key))
                    self.assertEqual(
                        [tuple(s['coord']) for s in samples],
                        [tuple(p) for p in obj['pixels']])
                    for sample, (px, py) in zip(samples, obj['pixels']):
                        self.assertEqual(sample['intensity'],
                                         intensity[py][px])
                reader.close()

    def test_diagonal_connectivity(self):
        # Diagonally touching cells: separate under 4-connectivity, one
        # component under 8-connectivity.
        mask = ['10', '01']
        intensity = [[1, 2], [3, 4]]
        strips = single_row_strips(mask, lambda y: False, intensity)
        for connectivity, count in ((4, 2), (8, 1)):
            target = os.path.join(self.tmp.name, f'c{connectivity}.db')
            build_archive(target, header(2, 2, connectivity, 1), strips, 4)
            reader = ArchiveReader(target)
            self.assertEqual(len(reader.list_object_keys()), count)
            reader.close()

    def test_ring_with_independent_foreground_hole(self):
        # A ring enclosing an independent foreground cell; the enclosed cell
        # is a distinct object and does not fill the ring's hole.
        mask = ['11111', '10001', '10101', '10001', '11111']
        intensity = [[y * 5 + x + 1 for x in range(5)] for y in range(5)]
        strips = single_row_strips(mask, lambda y: False, intensity)
        r = self.build(header(5, 5, connectivity=4, min_pixels=1),
                       strips, 25)
        by_key = {tuple(k): r.get_object(k) for k in r.list_object_keys()}
        ring = by_key[(0, 0)]
        self.assertEqual(ring['holes'], 1)
        self.assertEqual(ring['touches_border'], True)
        inner = by_key[(2, 2)]
        self.assertEqual(inner['pixel_count'], 1)
        self.assertEqual(inner['holes'], 0)
        r.close()

    # -- Budget edge cases --------------------------------------------------

    def test_zero_cost_band_when_budget_empty_is_still_approved(self):
        # A requested band made entirely of selected foreground costs zero and
        # is approved even with an exhausted (zero) remaining budget.
        masks = ['11', '11']
        strips = single_row_strips(masks, lambda y: True)
        r = self.build(header(2, 2, min_pixels=1), strips, 0)
        decisions = {d['band_id']: d['decision']
                     for d in r.list_band_decisions()}
        self.assertTrue(all(v == APPROVED for v in decisions.values()))
        self.assertEqual(r.used_budget(), 0)
        r.close()

    def test_approved_selected_foreground_survives_in_nonapproved_band(self):
        masks = ['0100', '0101', '0001', '0001']
        strips = single_row_strips(masks, lambda y: True)
        r = self.build(header(4, 4, min_pixels=2), strips, 0)
        # Nothing is approved (every band has background), but selected
        # foreground cells are retained and object reads still work.
        self.assertEqual(r.list_object_keys(), [[1, 0], [3, 1]])
        for d in r.list_band_decisions():
            if d['requested']:
                self.assertEqual(d['decision'], INSUFFICIENT_BUDGET)
        # Recovery refuses because no band was approved.
        with self.assertRaises(ArchiveError):
            r.recover_band('A')
        r.close()

    # -- Validation and publication guarantees ------------------------------

    def test_rejects_malformed_and_discontinuous_inputs(self):
        good = {'width': 2, 'height': 2,
                'pixel_size': ['1', '1'], 'connectivity': 4,
                'selection': {'min_pixels': 1, 'exclude_border': False}}
        base_strips = [
            {'band_id': 'a', 'start_row': 0, 'offline': False,
             'mask': ['10'], 'intensity': [[1, 2]]},
            {'band_id': 'b', 'start_row': 1, 'offline': False,
             'mask': ['01'], 'intensity': [[3, 4]]}]
        bad_cases = [
            # Gap in coverage.
            [dict(base_strips[0], start_row=0),
             dict(base_strips[1], start_row=2)],
            # Overlapping start.
            [dict(base_strips[0], start_row=0),
             dict(base_strips[1], start_row=0)],
            # Extra row beyond the board.
            base_strips + [
                {'band_id': 'c', 'start_row': 2, 'offline': False,
                 'mask': ['11'], 'intensity': [[5, 6]]}],
            # Duplicate band id.
            [dict(base_strips[0], band_id='x'),
             dict(base_strips[1], band_id='x')],
            # Wrong mask width.
            [dict(base_strips[0], mask=['100']), base_strips[1]],
            # Bad intensity value (boolean is rejected, not coerced).
            [dict(base_strips[0], intensity=[[True, 2]]), base_strips[1]],
            # Strip taller than the 64-row limit.
            [{'band_id': 'big', 'start_row': 0, 'offline': False,
              'mask': ['10'] * 65, 'intensity': [[1, 2]] * 65}],
        ]
        for case in bad_cases:
            with self.assertRaises(ArchiveError):
                build_archive(os.path.join(self.tmp.name, 'x.db'),
                              good, case, 4)
        # Bad header fields.
        with self.assertRaises(ArchiveError):
            build_archive(os.path.join(self.tmp.name, 'x.db'),
                          header(2, 2, connectivity=5), base_strips, 4)
        with self.assertRaises(ArchiveError):
            build_archive(os.path.join(self.tmp.name, 'x.db'),
                          header(2, 2, min_pixels=99), base_strips, 4)
        with self.assertRaises(ArchiveError):
            build_archive(os.path.join(self.tmp.name, 'x.db'),
                          {**good, 'pixel_size': ['0', '1']},
                          base_strips, 4)
        # Budget out of range.
        with self.assertRaises(ArchiveError):
            build_archive(os.path.join(self.tmp.name, 'x.db'),
                          good, base_strips, 5)
        with self.assertRaises(ArchiveError):
            build_archive(os.path.join(self.tmp.name, 'x.db'),
                          good, base_strips, -1)
        # Empty iterator leaves no published target.
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp.name, 'empty.db')))
        with self.assertRaises(ArchiveError):
            build_archive(os.path.join(self.tmp.name, 'empty.db'),
                          good, iter(()), 4)
        self.assertFalse(
            os.path.exists(os.path.join(self.tmp.name, 'empty.db')))

    def test_existing_target_is_never_replaced(self):
        with open(self.target, 'wb') as handle:
            handle.write(b'pre-existing')
        strips = single_row_strips(['10', '01'], lambda y: False)
        with self.assertRaises(ArchiveError):
            build_archive(self.target, header(2, 2), strips, 4)
        with open(self.target, 'rb') as handle:
            self.assertEqual(handle.read(), b'pre-existing')

    def test_input_iterator_consumed_exactly_once(self):
        strips = single_row_strips(['10', '01'], lambda y: False)
        consumed = []

        def gen():
            for item in strips:
                consumed.append(item['band_id'])
                yield item

        r = self.build(header(2, 2), gen(), 4)
        self.assertEqual(consumed, ['A', 'B'])
        r.close()

    def test_broken_iterator_publishes_nothing(self):
        def gen():
            yield {'band_id': 'a', 'start_row': 0, 'offline': False,
                   'mask': ['10'], 'intensity': [[1, 2]]}
            raise RuntimeError('boom mid-stream')

        target = os.path.join(self.tmp.name, 'broken.db')
        with self.assertRaises(ArchiveError):
            build_archive(target, header(2, 2), gen(), 4)
        self.assertFalse(os.path.exists(target))

    def test_temporary_build_area_is_always_removed(self):
        strips = single_row_strips(['10', '01'], lambda y: False)
        build_archive(os.path.join(self.tmp.name, 'ok.db'),
                      header(2, 2), strips, 4)

        def broken():
            yield {'band_id': 'b', 'start_row': 0, 'offline': False,
                   'mask': ['10'], 'intensity': [[1, 2]]}
            yield {'band_id': 'c', 'start_row': 9, 'offline': False,
                   'mask': ['10'], 'intensity': [[1, 2]]}

        with self.assertRaises(ArchiveError):
            build_archive(os.path.join(self.tmp.name, 'broken.db'),
                          header(2, 12), broken(), 24)
        leftovers = [name for name in os.listdir(self.tmp.name)
                     if name.startswith('.archive-build-')]
        self.assertEqual(leftovers, [])

    def test_reader_rejects_corrupt_pack(self):
        strips = single_row_strips(['10', '01'], lambda y: False)
        build_archive(self.target, header(2, 2), strips, 4)
        # Corrupt the file in place; the read-only connection must fail cleanly.
        with open(self.target, 'r+b') as handle:
            handle.seek(0)
            handle.write(b'not a database')
        with self.assertRaises(ArchiveError):
            ArchiveReader(self.target)

    def test_batched_reads_respect_batch_size(self):
        mask = ['11111'] * 4
        strips = single_row_strips(mask, lambda y: False)
        r = self.build(header(5, 4, min_pixels=1), strips, 0)
        key = r.list_object_keys()[0]
        batches = list(r.iter_object_samples(key, batch_size=7))
        self.assertTrue(all(len(b) <= 7 for b in batches))
        self.assertEqual(sum(len(b) for b in batches), 20)
        with self.assertRaises(ArchiveError):
            list(r.iter_object_samples(key, batch_size=0))
        r.close()

    def test_unknown_object_key_raises_keyerror(self):
        strips = single_row_strips(['10', '00'], lambda y: False)
        r = self.build(header(2, 2, min_pixels=1), strips, 0)
        with self.assertRaises(KeyError):
            r.get_object([1, 1])
        r.close()


if __name__ == '__main__':
    unittest.main()
