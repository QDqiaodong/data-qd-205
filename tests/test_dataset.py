import copy
import json
import os
import tempfile
import unittest

from micro_objects import (BLIND, NEGATIVE, NO_COMPLETE_OBJECT,
                           NO_VISIBLE_BACKGROUND, POSITIVE,
                           VISIBLE_FOREGROUND, CompiledDataset,
                           DatasetCompileError, apply_calibration,
                           compile_dataset, parse_calibration)
from micro_objects.io import load_calibration, save_calibration


def board9x3(intensity_row=None):
    """The acceptance board: width 9, height 3, foreground at y=1 x=1,2,6,7."""
    row = intensity_row or [10, 1, 1, 90, 20, 5, 1, 1, 200]
    return {
        'board_id': 'b0',
        'data': {
            'mask': ['000000000', '011000110', '000000000'],
            'intensity': [[0] * 9, row, [0] * 9],
            'pixel_size': ['1', '1'],
            'connectivity': 4,
            'selection': {'min_pixels': 2, 'exclude_border': False},
        },
    }


def req(request_id, board_id, rect, kind):
    return {'request_id': request_id, 'board_id': board_id,
            'rect': list(rect), 'kind': kind}


class AcceptanceChainTests(unittest.TestCase):
    def _requests(self, blind_rect):
        return [
            req('p1', 'b0', (0, 1, 4, 2), POSITIVE),
            req('p2', 'b0', (1, 1, 5, 2), POSITIVE),
            req('p3', 'b0', (5, 1, 9, 2), POSITIVE),
            req('hold', 'b0', blind_rect, BLIND),
        ]

    def test_blind_starts_at_x8(self):
        compiled = compile_dataset([board9x3()],
                                   self._requests((8, 1, 9, 2)))
        self.assertIsInstance(compiled, CompiledDataset)
        self.assertEqual(compiled.not_emitted, {})
        self.assertEqual(sorted(compiled.emitted_ids()),
                         ['hold', 'p1', 'p2', 'p3'])
        cal = compiled.calibration
        self.assertEqual((cal['background_cells'], cal['baseline']),
                         (4, 10))

        s1 = compiled.get_sample('p1')
        self.assertEqual(s1['intensity'], [[0, -9, -9, 80]])
        self.assertEqual(s1['supervision'], [[0, 1, 1, 0]])
        self.assertEqual(s1['objects'],
                         [{'label': 1, 'board_id': 'b0',
                           'first_pixel': [1, 1]}])

        s2 = compiled.get_sample('p2')
        self.assertEqual(s2['intensity'], [[-9, -9, 80, 10]])
        self.assertEqual(s2['supervision'], [[1, 1, 0, 0]])

        s3 = compiled.get_sample('p3')
        # The reserved x=8 cell leaks neither raw intensity nor the mask.
        self.assertEqual(s3['intensity'], [[-5, -9, -9, None]])
        self.assertEqual(s3['supervision'], [[0, 1, 1, -1]])
        self.assertEqual(s3['objects'],
                         [{'label': 1, 'board_id': 'b0',
                           'first_pixel': [6, 1]}])

        blind = compiled.get_sample('hold')
        self.assertEqual(blind['intensity'], [[190]])
        # x=8 is whole-board background; the blind window is not masked by the
        # reservation boundary, so its supervision is ordinary background 0.
        self.assertEqual(blind['supervision'], [[0]])

    def test_blind_window_over_foreground_keeps_raw_pixels(self):
        compiled = compile_dataset(
            [board9x3()],
            [req('p1', 'b0', (0, 1, 4, 2), POSITIVE),
             req('hold', 'b0', (7, 1, 8, 2), BLIND)])
        # p3 would cover a cut object; only p1 emits, its x=0/x=3 background
        # gives baseline 10 over two cells.
        self.assertEqual(compiled.calibration['baseline'], 10)
        blind = compiled.get_sample('hold')
        self.assertEqual(blind['intensity'], [[-9]])
        # Foreground of an object cut by the blind window is ignore.
        self.assertEqual(blind['supervision'], [[-1]])

    def test_blind_moves_to_x7(self):
        compiled = compile_dataset([board9x3()],
                                   self._requests((7, 1, 9, 2)))
        # The third crop loses its complete object and is withheld.
        self.assertEqual(compiled.not_emitted,
                         {'p3': NO_COMPLETE_OBJECT})
        self.assertNotIn('p3', compiled.emitted_ids())
        with self.assertRaises(DatasetCompileError):
            compiled.get_sample('p3')
        cal = compiled.calibration
        # x=5 belonged to the withheld crop and exits the fit.
        self.assertEqual((cal['background_cells'], cal['baseline']),
                         (3, 20))

        # p1/p2 are recalibrated with the new baseline.
        self.assertEqual(compiled.get_sample('p1')['intensity'],
                         [[-10, -19, -19, 70]])

        blind = compiled.get_sample('hold')
        # The blind window is not masked by the reservation boundary.
        self.assertEqual(blind['intensity'], [[-19, 180]])
        self.assertEqual(blind['supervision'], [[-1, 0]])

    def test_calibration_record_roundtrip_and_application(self):
        compiled = compile_dataset([board9x3()],
                                   self._requests((7, 1, 9, 2)))
        record = compiled.calibration
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'cal.json')
            save_calibration(path, record)
            loaded = load_calibration(path)
            self.assertEqual(parse_calibration(loaded), record)
        result = apply_calibration(loaded, [[0, None, 20, 65535]])
        self.assertEqual(result, [[-20, None, 0, 65515]])

    def test_reserved_cell_intensity_change_is_invisible_to_training(self):
        boards_a = [board9x3()]
        requests = self._requests((8, 1, 9, 2))
        compiled_a = compile_dataset(copy.deepcopy(boards_a),
                                     copy.deepcopy(requests))
        before = {rid: compiled_a.get_sample(rid)
                  for rid in ('p1', 'p2', 'p3')}
        cal_before = compiled_a.calibration

        boards_b = board9x3([10, 1, 1, 90, 20, 5, 1, 1, 999])
        compiled_b = compile_dataset([boards_b],
                                     self._requests((8, 1, 9, 2)))
        self.assertEqual(compiled_b.calibration, cal_before)
        for rid in ('p1', 'p2', 'p3'):
            self.assertEqual(compiled_b.get_sample(rid), before[rid])
        # The blind crop still reads the changed raw value: 999 - 10.
        self.assertEqual(compiled_b.get_sample('hold')['intensity'],
                         [[989]])


class SampleSemanticsTests(unittest.TestCase):
    def _board(self, min_pixels):
        # Two 2-pixel components on y=1: x1-2 and x4-5, gap at x3.
        return {
            'board_id': 'b',
            'data': {
                'mask': ['000000', '011011', '000000'],
                'intensity': [[0] * 6, [0, 2, 2, 0, 2, 2], [0] * 6],
                'pixel_size': ['1', '1'],
                'connectivity': 4,
                'selection': {'min_pixels': min_pixels,
                              'exclude_border': False},
            },
        }

    def test_positive_with_complete_and_cut_object(self):
        compiled = compile_dataset(
            [self._board(2)],
            [req('pos', 'b', (0, 1, 5, 2), POSITIVE),
             req('hold', 'b', (5, 0, 6, 1), BLIND)])
        sample = compiled.get_sample('pos')
        # First object whole inside the crop -> label 1; x3 is a background
        # gap; the second object (x4-5) is cut at x4, so its visible
        # foreground is ignore, not a new component.
        self.assertEqual(sample['supervision'], [[0, 1, 1, 0, -1]])
        self.assertEqual(sample['objects'],
                         [{'label': 1, 'board_id': 'b',
                           'first_pixel': [1, 1]}])
        self.assertEqual(compiled.not_emitted, {})

    def test_rejected_foreground_cannot_be_a_negative(self):
        compiled = compile_dataset(
            [self._board(3)],
            [req('neg', 'b', (0, 1, 4, 2), NEGATIVE),
             req('bg', 'b', (0, 0, 1, 1), NEGATIVE),
             req('hold', 'b', (5, 0, 6, 1), BLIND)])
        # Both objects fail the whole-board selection, but their raw
        # foreground is still visible foreground for the negative rule.
        self.assertEqual(compiled.not_emitted,
                         {'neg': VISIBLE_FOREGROUND})
        # The separate background negative still emits and feeds the fit.
        self.assertIn('bg', compiled.emitted_ids())

    def test_legal_negative_is_all_background(self):
        compiled = compile_dataset(
            [self._board(2)],
            [req('neg', 'b', (0, 0, 5, 1), NEGATIVE),
             req('hold', 'b', (5, 0, 6, 1), BLIND)])
        sample = compiled.get_sample('neg')
        self.assertEqual(sample['supervision'], [[0] * 5])
        self.assertEqual(sample['objects'], [])
        self.assertTrue(
            all(cell is not None for row in sample['intensity']
                for cell in row))

    def test_negative_reports_foreground_before_missing_background(self):
        # Rect fully covering the x1-2 object: visible foreground, no visible
        # background at all -> foreground reason wins.  A separate background
        # crop keeps the batch's calibration feasible.
        compiled = compile_dataset(
            [self._board(2)],
            [req('neg', 'b', (1, 1, 3, 2), NEGATIVE),
             req('bg', 'b', (3, 0, 4, 1), NEGATIVE),
             req('hold', 'b', (0, 0, 1, 1), BLIND)])
        self.assertEqual(compiled.not_emitted,
                         {'neg': VISIBLE_FOREGROUND})

        compiled2 = compile_dataset(
            [self._board(2)],
            [req('neg2', 'b', (1, 1, 3, 2), NEGATIVE),
             req('bg2', 'b', (3, 0, 4, 1), NEGATIVE),
             req('hold2', 'b', (0, 0, 2, 1), BLIND)])
        # Reserved cells count as neither background nor foreground; the only
        # visible cells in neg2 are foreground, so foreground still wins.
        self.assertEqual(compiled2.not_emitted,
                         {'neg2': VISIBLE_FOREGROUND})

    def test_negative_reserved_only_reports_missing_background(self):
        # The negative rect lies wholly inside the blind union: no visible
        # foreground and no visible background, so the background reason fires.
        compiled = compile_dataset(
            [self._board(2)],
            [req('neg', 'b', (0, 0, 2, 1), NEGATIVE),
             req('bg', 'b', (3, 0, 4, 1), NEGATIVE),
             req('hold', 'b', (0, 0, 2, 1), BLIND)])
        self.assertEqual(compiled.not_emitted,
                         {'neg': NO_VISIBLE_BACKGROUND})

    def test_raster_first_pixel_numbering_and_identity(self):
        compiled = compile_dataset(
            [self._board(2)],
            [req('pos', 'b', (0, 1, 6, 2), POSITIVE),
             req('hold', 'b', (0, 0, 1, 1), BLIND)])
        sample = compiled.get_sample('pos')
        self.assertEqual(sample['supervision'], [[0, 1, 1, 0, 2, 2]])
        self.assertEqual(sample['objects'], [
            {'label': 1, 'board_id': 'b', 'first_pixel': [1, 1]},
            {'label': 2, 'board_id': 'b', 'first_pixel': [4, 1]},
        ])

    def test_same_coordinates_on_different_boards_count_separately(self):
        def make(board_id, value):
            return {
                'board_id': board_id,
                'data': {
                    'mask': ['000', '010', '000'],
                    'intensity': [[0, 0, 0], [value, 1, 0], [0, 0, 0]],
                    'pixel_size': ['1', '1'], 'connectivity': 4,
                    'selection': {'min_pixels': 1,
                                  'exclude_border': False},
                },
            }
        compiled = compile_dataset(
            [make('a', 5), make('b', 7)],
            [req('pa', 'a', (0, 1, 2, 2), POSITIVE),
             req('pb', 'b', (0, 1, 2, 2), POSITIVE),
             req('ha', 'a', (2, 1, 3, 2), BLIND),
             req('hb', 'b', (2, 1, 3, 2), BLIND)])
        # The same (0,1) background coordinate on two boards is counted twice.
        self.assertEqual(compiled.calibration['background_cells'], 2)
        self.assertEqual(compiled.calibration['baseline'], 5)

    def test_batch_without_training_background_is_wholesale_rejected(self):
        # The positive rect is exactly the object's bbox: it emits but
        # contains no background cell anywhere in the batch.
        board = self._board(2)
        with self.assertRaises(DatasetCompileError):
            compile_dataset(
                [board],
                [req('pos', 'b', (1, 1, 3, 2), POSITIVE),
                 req('hold', 'b', (0, 0, 1, 1), BLIND)])
        # Same request mix as a negative: nothing emits either, still rejected.
        with self.assertRaises(DatasetCompileError):
            compile_dataset(
                [self._board(2)],
                [req('neg', 'b', (1, 1, 3, 2), NEGATIVE),
                 req('hold', 'b', (0, 0, 1, 1), BLIND)])

    def test_blind_crop_reads_objects_but_cut_foreground_is_ignore(self):
        compiled = compile_dataset(
            [self._board(2)],
            [req('pos', 'b', (0, 1, 6, 2), POSITIVE),
             req('hold', 'b', (2, 1, 5, 2), BLIND),
             req('bg', 'b', (0, 0, 1, 1), NEGATIVE)])
        blind = compiled.get_sample('hold')
        # No reservation masking inside the blind window; the cut object at
        # x=2 and x=4 is foreground that is not whole in the crop -> -1.
        self.assertEqual(blind['supervision'], [[-1, 0, -1]])
        self.assertTrue(
            all(cell is not None for row in blind['intensity']
                for cell in row))


class CalibrationApplicationTests(unittest.TestCase):
    def _record(self):
        return {'format': 'micro-objects-calibration/1',
                'baseline': 20, 'background_cells': 3}

    def test_nulls_negatives_and_no_clipping(self):
        out = apply_calibration(self._record(), [[0, None, 20, 65535]])
        self.assertEqual(out, [[-20, None, 0, 65515]])

    def test_bad_matrices_and_records(self):
        for matrix in ([], [[]], [[1], [1, 2]], [[True]], [[65536]],
                       [[-1]], [[1.0]], 'x'):
            with self.subTest(matrix=matrix):
                with self.assertRaises(DatasetCompileError):
                    apply_calibration(self._record(), matrix)
        for record in (None, {}, {'format': 'x', 'baseline': 1,
                                  'background_cells': 1},
                       {'format': 'micro-objects-calibration/1',
                        'baseline': -1, 'background_cells': 1},
                       {'format': 'micro-objects-calibration/1',
                        'baseline': 1, 'background_cells': 0},
                       {'format': 'micro-objects-calibration/1',
                        'baseline': True, 'background_cells': 1}):
            with self.subTest(record=record):
                with self.assertRaises(DatasetCompileError):
                    parse_calibration(record)


class ValidationTests(unittest.TestCase):
    def _boards(self):
        return [board9x3()]

    def _requests(self):
        return [req('p1', 'b0', (0, 1, 4, 2), POSITIVE),
                req('hold', 'b0', (8, 1, 9, 2), BLIND)]

    def test_bad_inputs_rejected(self):
        good_boards = self._boards()
        good_requests = self._requests()
        bad_cases = []

        bad_cases.append(([], good_requests))
        bad_cases.append((good_boards * 9, good_requests))
        bad_cases.append((good_boards, []))
        bad_cases.append((good_boards, good_requests * 513))

        bad_id_board = board9x3()
        bad_id_board['board_id'] = 'bad id!'
        bad_cases.append(([bad_id_board], self._requests()))

        dup_board = board9x3()
        dup_board['board_id'] = 'b0'
        bad_cases.append(([board9x3(), dup_board],
                          [req('p1', 'b0', (0, 1, 4, 2), POSITIVE),
                           req('hold', 'b0', (8, 1, 9, 2), BLIND)]))

        unknown = req('p', 'nope', (0, 1, 1, 2), POSITIVE)
        bad_cases.append((good_boards, [unknown, good_requests[1]]))

        dup_id = [req('p1', 'b0', (0, 1, 4, 2), POSITIVE),
                  req('p1', 'b0', (1, 1, 4, 2), POSITIVE),
                  good_requests[1]]
        bad_cases.append((good_boards, dup_id))

        for rect in ((0, 1, 0, 2), (0, 1, 10, 2), (-1, 1, 2, 2),
                     (0, 1, 4, 4), (0, 1, 4, 1)):
            bad_cases.append((good_boards,
                              [req('p1', 'b0', rect, POSITIVE),
                               good_requests[1]]))

        bad_kind = req('p1', 'b0', (0, 1, 4, 2), 'train')
        bad_cases.append((good_boards, [bad_kind, good_requests[1]]))

        extra_field = req('p1', 'b0', (0, 1, 4, 2), POSITIVE)
        extra_field['extra'] = 1
        bad_cases.append((good_boards, [extra_field, good_requests[1]]))

        bool_rect = req('p1', 'b0', (True, 1, 4, 2), POSITIVE)
        bad_cases.append((good_boards, [bool_rect, good_requests[1]]))

        bad_board = board9x3()
        bad_board['data']['mask'] = ['000', '011']
        bad_cases.append(([bad_board], self._requests()))

        bad_board2 = board9x3()
        bad_board2['data']['intensity'][1][0] = True
        bad_cases.append(([bad_board2], self._requests()))

        missing_blind = [req('p1', 'b0', (0, 1, 4, 2), POSITIVE)]
        bad_cases.append((good_boards, missing_blind))
        missing_training = [req('hold', 'b0', (8, 1, 9, 2), BLIND)]
        bad_cases.append((good_boards, missing_training))

        for boards, requests in bad_cases:
            with self.subTest(requests=json.dumps(requests, default=str)[:60]):
                with self.assertRaises((ValueError, DatasetCompileError)):
                    compile_dataset(copy.deepcopy(boards),
                                    copy.deepcopy(requests))

    def test_same_rect_different_request_ids_is_legal(self):
        compiled = compile_dataset(
            self._boards(),
            [req('p1', 'b0', (0, 1, 4, 2), POSITIVE),
             req('p2', 'b0', (0, 1, 4, 2), POSITIVE),
             req('hold', 'b0', (8, 0, 9, 1), BLIND)])
        first = compiled.get_sample('p1')
        second = compiled.get_sample('p2')
        first.pop('request_id')
        second.pop('request_id')
        self.assertEqual(first, second)
        self.assertIsNot(compiled.get_sample('p1'),
                         compiled.get_sample('p2'))


class ImmunityAndMemoryTests(unittest.TestCase):
    def test_caller_mutations_do_not_change_repeated_reads(self):
        boards = [board9x3()]
        requests = [req('p1', 'b0', (0, 1, 4, 2), POSITIVE),
                    req('hold', 'b0', (8, 1, 9, 2), BLIND)]
        compiled = compile_dataset(copy.deepcopy(boards),
                                   copy.deepcopy(requests))
        first = compiled.get_sample('p1')
        first['intensity'][0][0] = 123456
        first['supervision'][0][0] = 99
        first['objects'][0]['label'] = 77

        # Mutate the original board input and request objects.
        boards[0]['data']['mask'] = ['111111111'] * 3
        boards[0]['data']['intensity'][1][1] = 65535
        requests[0]['rect'] = [0, 0, 1, 1]

        second = compiled.get_sample('p1')
        self.assertEqual(second['intensity'], [[0, -9, -9, 80]])
        self.assertEqual(second['supervision'], [[0, 1, 1, 0]])
        self.assertEqual(second['objects'],
                         [{'label': 1, 'board_id': 'b0',
                           'first_pixel': [1, 1]}])
        # Repeated reads are independent and order-insensitive.
        third = compiled.get_sample('hold')
        fourth = compiled.get_sample('p1')
        self.assertEqual(fourth, second)
        self.assertIsNot(fourth, second)
        self.assertEqual(third['intensity'], [[190]])

    def test_input_order_does_not_change_results(self):
        boards = [board9x3()]
        requests = [
            req('p3', 'b0', (5, 1, 9, 2), POSITIVE),
            req('hold', 'b0', (8, 1, 9, 2), BLIND),
            req('p1', 'b0', (0, 1, 4, 2), POSITIVE),
            req('p2', 'b0', (1, 1, 5, 2), POSITIVE),
        ]
        a = compile_dataset(copy.deepcopy(boards), copy.deepcopy(requests))
        b = compile_dataset(copy.deepcopy(boards),
                            list(reversed(copy.deepcopy(requests))))
        self.assertEqual(a.calibration, b.calibration)
        self.assertEqual(a.not_emitted, b.not_emitted)
        for rid in a.request_ids():
            self.assertEqual(a.get_sample(rid), b.get_sample(rid))

    def test_many_large_overlapping_requests_keep_no_crop_grids(self):
        # One 256x256 board with a single small object; 256 full-board
        # overlapping positive requests plus a small blind window.
        mask = ['0' * 256 for _ in range(256)]
        mask[1] = '0' + '11' + '0' * 253
        intensity = [[0] * 256 for _ in range(256)]
        board = {
            'board_id': 'big',
            'data': {
                'mask': mask, 'intensity': intensity,
                'pixel_size': ['1', '1'], 'connectivity': 4,
                'selection': {'min_pixels': 2, 'exclude_border': False},
            },
        }
        requests = [req(f'p{i}', 'big', (0, 0, 256, 256), POSITIVE)
                    for i in range(256)]
        requests.append(req('hold', 'big', (250, 0, 256, 1), BLIND))
        compiled = compile_dataset([board], requests)

        stats = compiled.storage_stats()
        self.assertEqual(stats['boards'], 1)
        self.assertEqual(stats['board_pixels'], 256 * 256)
        self.assertEqual(stats['requests'], 257)
        # No crop raster is retained: ~17 million overlapping output cells are
        # never materialized; a sample only costs one crop-sized temp grid.
        self.assertEqual(stats['retained_crop_grids'], 0)
        self.assertEqual(stats['retained_crop_cells'], 0)

        # Consumers only fetch a few samples; each is rendered on demand.
        sample = compiled.get_sample('p0')
        self.assertEqual(len(sample['intensity']), 256)
        self.assertEqual(len(sample['intensity'][0]), 256)
        self.assertEqual(sample['supervision'][1][1], 1)
        self.assertEqual(sample['supervision'][1][3], 0)
        again = compiled.get_sample('p255')
        self.assertEqual(again['supervision'], sample['supervision'])
        # Reserved training cells in the blind corner stay hidden.
        self.assertEqual(sample['intensity'][0][250], None)
        self.assertEqual(sample['supervision'][0][250], -1)
        blind = compiled.get_sample('hold')
        self.assertEqual(len(blind['intensity'][0]), 6)
        self.assertEqual(compiled.calibration['background_cells'],
                         256 * 256 - 6 - 2)


if __name__ == '__main__':
    unittest.main()
