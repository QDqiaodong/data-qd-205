import copy
import json
import os
import tempfile
import unittest

from micro_objects import (CORRESPONDENCE_FORMAT, CorrespondenceError,
                           GRADE_CONFLICT, NO_MATCH, POSSIBLY_UNMATCHED,
                           deliver_correspondence, parse_grade_delivery,
                           parse_group_delivery)
from micro_objects.io import (load_grade_delivery, load_group_delivery,
                              save_grade_delivery, save_group_delivery)

WIDTH, HEIGHT = 5, 7


def board(mask, *, connectivity=4, selection=None, pixel_size=None):
    return {
        'mask': mask,
        'intensity': [[1 if char == '1' else 0 for char in row] for row in mask],
        'pixel_size': pixel_size or ['1', '1'],
        'connectivity': connectivity,
        'selection': selection
        or {'min_pixels': 1, 'exclude_border': False},
    }


def vertical_line_board():
    """Reference board: vertical lines x=1 and x=3 over y=1..5 (5x7)."""
    mask = ['00000'] * HEIGHT
    for y in range(1, 6):
        mask[y] = '01010'
    return board(mask)


def horizontal_line_board(rows=(1, 3)):
    """Retest board: horizontal lines at the given y rows over x=1..3."""
    mask = ['00000'] * HEIGHT
    for y in rows:
        mask[y] = '01110'
    return board(mask)


def reference_table(grade_a='A', grade_b='A', group_a='g1', group_b='g1'):
    return [
        {'key': [1, 1], 'grade': grade_a, 'group': group_a},
        {'key': [3, 1], 'grade': grade_b, 'group': group_b},
    ]


def retest_table(readings=((1, 1, 4), (1, 3, 9))):
    return [{'key': [x, y], 'reading': value} for x, y, value in readings]


class AcceptanceChainTests(unittest.TestCase):
    def test_base_chain_two_swaps_both_valid(self):
        result = deliver_correspondence(
            vertical_line_board(), horizontal_line_board(),
            reference_table(), retest_table())
        self.assertEqual(result['format'], CORRESPONDENCE_FORMAT)
        self.assertEqual(
            result['grade_delivery']['records'],
            [{'key': [1, 1], 'grade': 'A'},
             {'key': [1, 3], 'grade': 'A'}])
        self.assertEqual(result['group_delivery']['records'],
                         [{'group': 'g1', 'total': 13}])
        evidence = {tuple(e['key']): e
                    for e in result['diagnostics']['object_evidence']}
        for key in ((1, 1), (1, 3)):
            entry = evidence[key]
            self.assertFalse(entry['can_be_unmatched'])
            self.assertEqual(entry['candidate_reference_keys'],
                             [[1, 1], [3, 1]])
            self.assertIsNone(entry['reason'])
            self.assertTrue(entry['delivered'])
        optimal = result['diagnostics']['optimal_matching']
        self.assertEqual((optimal['pair_count'],
                          optimal['overlap_pixels'],
                          optimal['candidate_edge_count'],
                          optimal['allowed_edge_count']),
                         (2, 2, 4, 4))
        # Diagnostics stay separate: consumable tables carry no evidence.
        self.assertEqual(set(result['grade_delivery']), {'format', 'records'})
        self.assertEqual(set(result['group_delivery']), {'format', 'records'})

    def test_distinct_groups_keep_grades_but_withhold_group_totals(self):
        result = deliver_correspondence(
            vertical_line_board(), horizontal_line_board(),
            reference_table(group_b='g2'), retest_table())
        self.assertEqual(len(result['grade_delivery']['records']), 2)
        self.assertEqual(result['group_delivery']['records'], [])
        bounds = {b['group']: b for b in
                  result['diagnostics']['group_bounds']}
        for group in ('g1', 'g2'):
            self.assertEqual((bounds[group]['min_total'],
                              bounds[group]['max_total']), (4, 9))

    def test_distinct_grades_withhold_grades_but_keep_group_total(self):
        result = deliver_correspondence(
            vertical_line_board(), horizontal_line_board(),
            reference_table(grade_b='B'), retest_table())
        self.assertEqual(result['grade_delivery']['records'], [])
        reasons = {tuple(e['key']): e['reason']
                   for e in result['diagnostics']['object_evidence']}
        self.assertEqual(reasons, {(1, 1): GRADE_CONFLICT,
                                   (1, 3): GRADE_CONFLICT})
        # Withholding reasons never carry a guessed grade, and the consumable
        # table carries neither candidate objects nor evidence.
        for entry in result['diagnostics']['object_evidence']:
            self.assertNotIn('grade', entry)
            self.assertEqual(set(entry),
                             {'key', 'candidate_reference_keys',
                              'can_be_unmatched', 'delivered', 'reason'})
        self.assertEqual(result['group_delivery']['records'],
                         [{'group': 'g1', 'total': 13}])

    def test_third_line_makes_every_retest_object_possibly_unmatched(self):
        result = deliver_correspondence(
            vertical_line_board(),
            horizontal_line_board(rows=(1, 3, 5)),
            reference_table(),
            retest_table(readings=((1, 1, 4), (1, 3, 9), (1, 5, 20))))
        self.assertEqual(result['grade_delivery']['records'], [])
        for entry in result['diagnostics']['object_evidence']:
            self.assertTrue(entry['can_be_unmatched'])
            self.assertEqual(entry['reason'], POSSIBLY_UNMATCHED)
            self.assertEqual(len(entry['candidate_reference_keys']), 2)
        self.assertEqual(result['group_delivery']['records'], [])
        bounds = result['diagnostics']['group_bounds'][0]
        self.assertEqual((bounds['min_total'], bounds['max_total']),
                         (13, 29))
        # Neither 0 nor the full 33 is an attainable group total.
        self.assertNotIn(0, (bounds['min_total'], bounds['max_total']))
        self.assertNotEqual(bounds['max_total'], 33)

    def test_zero_reading_is_a_real_value(self):
        result = deliver_correspondence(
            vertical_line_board(), horizontal_line_board(),
            reference_table(),
            retest_table(readings=((1, 1, 0), (1, 3, 0))))
        self.assertEqual(result['group_delivery']['records'],
                         [{'group': 'g1', 'total': 0}])


class GreedyAndExcludedEdgeTests(unittest.TestCase):
    def test_locally_greedy_overlap_misses_a_pair(self):
        # r1 is the vertical line x=1 over y=1..4; r2 is the singleton (3,2).
        # t1 touches r1 in two pixels and r2 in one; t2 touches r1 only.
        # Taking the heaviest edge r1-t1 strands both t2 and r2 (one pair);
        # the maximum-cardinality optimum uses r2-t1 + r1-t2 (two pairs).
        reference = board([
            '00000', '01000', '01010', '01000',
            '01000', '00000', '00000'])
        retest = board([
            '00000', '01000', '01110', '00000',
            '01000', '00000', '00000'])
        ref_rows = [
            {'key': [1, 1], 'grade': 'A', 'group': 'g'},
            {'key': [3, 2], 'grade': 'A', 'group': 'g'},
        ]
        test_rows = [
            {'key': [1, 1], 'reading': 5},
            {'key': [1, 4], 'reading': 7},
        ]
        result = deliver_correspondence(reference, retest, ref_rows, test_rows)
        optimal = result['diagnostics']['optimal_matching']
        self.assertEqual(optimal['pair_count'], 2)
        self.assertEqual(optimal['overlap_pixels'], 2)
        evidence = {tuple(e['key']): e
                    for e in result['diagnostics']['object_evidence']}
        # The heavy r1-t1 edge only occurs in a one-pair scheme: it is excluded
        # from the evidence even though the spatial overlap really exists.
        self.assertEqual(evidence[(1, 1)]['candidate_reference_keys'],
                         [[3, 2]])
        self.assertEqual(evidence[(1, 4)]['candidate_reference_keys'],
                         [[1, 1]])
        self.assertFalse(any(e['can_be_unmatched']
                             for e in result['diagnostics']['object_evidence']))
        self.assertEqual(result['group_delivery']['records'],
                         [{'group': 'g', 'total': 12}])
        grades = {tuple(r['key']): r['grade']
                  for r in result['grade_delivery']['records']}
        self.assertEqual(grades, {(1, 1): 'A', (1, 4): 'A'})

    def test_equal_pair_count_different_overlap_excludes_losing_candidates(self):
        # Both cardinality-two schemes exist: {r1-t1, r2-t2} overlaps 2+2=4,
        # {r2-t1, r1-t2} overlaps 1+2=3.  Only the heavier scheme is optimal,
        # so the losing edges never reach evidence, grades or group totals.
        reference = board([
            '00000', '01010', '01010', '01010',
            '01010', '01010', '00000'])
        retest = board([
            '00000', '01000', '01110', '00000',
            '01010', '01110', '00000'])
        ref_rows = [
            {'key': [1, 1], 'grade': 'A', 'group': 'g1'},
            {'key': [3, 1], 'grade': 'B', 'group': 'g2'},
        ]
        test_rows = [
            {'key': [1, 1], 'reading': 4},
            {'key': [1, 4], 'reading': 9},
        ]
        result = deliver_correspondence(reference, retest, ref_rows, test_rows)
        optimal = result['diagnostics']['optimal_matching']
        self.assertEqual((optimal['pair_count'], optimal['overlap_pixels']),
                         (2, 4))
        evidence = {tuple(e['key']): e
                    for e in result['diagnostics']['object_evidence']}
        self.assertEqual(evidence[(1, 1)]['candidate_reference_keys'],
                         [[1, 1]])
        self.assertEqual(evidence[(1, 4)]['candidate_reference_keys'],
                         [[3, 1]])
        # Grades are certain precisely because the losing candidates cannot
        # enter any optimal scheme.
        grades = {tuple(r['key']): r['grade']
                  for r in result['grade_delivery']['records']}
        self.assertEqual(grades, {(1, 1): 'A', (1, 4): 'B'})
        totals = {r['group']: r['total']
                  for r in result['group_delivery']['records']}
        self.assertEqual(totals, {'g1': 4, 'g2': 9})

    def test_spatial_overlap_without_any_optimal_scheme_is_no_match(self):
        # r1 is a four-pixel line.  t1 touches only its top pixel; a separate
        # t2 touches its two bottom pixels.  Both schemes use one pair, but
        # r1-t2 overlaps two pixels and beats r1-t1's one, so t1's spatial
        # contact is not viable in any optimal scheme.
        reference = board(['000', '100', '100', '100', '100'])
        retest = board(['000', '110', '000', '100', '110'])
        ref_rows = [{'key': [0, 1], 'grade': 'A', 'group': 'g'}]
        test_rows = [
            {'key': [0, 1], 'reading': 6},
            {'key': [0, 3], 'reading': 8},
        ]
        result = deliver_correspondence(reference, retest, ref_rows, test_rows)
        optimal = result['diagnostics']['optimal_matching']
        self.assertEqual(optimal['pair_count'], 1)
        self.assertEqual(optimal['overlap_pixels'], 2)
        by_key = {tuple(e['key']): e
                  for e in result['diagnostics']['object_evidence']}
        self.assertEqual(by_key[(0, 1)]['candidate_reference_keys'], [])
        self.assertEqual(by_key[(0, 1)]['reason'], NO_MATCH)
        self.assertEqual(by_key[(0, 3)]['candidate_reference_keys'],
                         [[0, 1]])
        self.assertEqual(result['group_delivery']['records'],
                         [{'group': 'g', 'total': 8}])


class SelectionAndZeroGroupTests(unittest.TestCase):
    def test_reference_to_filtered_out_object_rejects_batch(self):
        # A single-pixel object fails min_pixels=2 on the reference side.
        reference = board(['100', '000'],
                          selection={'min_pixels': 2,
                                     'exclude_border': False})
        retest = board(['100', '000'])
        with self.assertRaises(CorrespondenceError):
            deliver_correspondence(
                reference, retest,
                [{'key': [0, 0], 'grade': 'A', 'group': 'g'}],
                [{'key': [0, 0], 'reading': 1}])

    def test_filtered_objects_do_not_participate(self):
        # Reference has a rejected two-pixel object (min_pixels=3) and a
        # rejected singleton; neither may participate or appear in tables.
        reference = board(
            ['110', '001'],
            selection={'min_pixels': 3, 'exclude_border': False})
        retest = board(['100', '001'])
        with self.assertRaises(CorrespondenceError):
            # The table may not register the filtered-out object.
            deliver_correspondence(
                reference, retest,
                [{'key': [0, 0], 'grade': 'A', 'group': 'g'}],
                [{'key': [0, 0], 'reading': 5},
                 {'key': [2, 1], 'reading': 9}])

    def test_accepted_object_pairs_while_neighboring_singleton_is_filtered(self):
        # With min_pixels=1 both reference objects survive; the retest
        # singleton only touches its own matching reference pixel.
        reference = board(['110', '001'])
        retest = board(['100', '001'])
        result = deliver_correspondence(
            reference, retest,
            [{'key': [0, 0], 'grade': 'A', 'group': 'g'},
             {'key': [2, 1], 'grade': 'A', 'group': 'g'}],
            [{'key': [0, 0], 'reading': 5},
             {'key': [2, 1], 'reading': 9}])
        by_key = {tuple(e['key']): e
                  for e in result['diagnostics']['object_evidence']}
        self.assertEqual(by_key[(0, 0)]['candidate_reference_keys'],
                         [[0, 0]])
        self.assertEqual(by_key[(2, 1)]['candidate_reference_keys'],
                         [[2, 1]])
        self.assertEqual(result['group_delivery']['records'],
                         [{'group': 'g', 'total': 14}])

    def test_group_with_no_overlap_delivers_zero(self):
        # r2 in group g2 shares no foreground cell with any retest object.
        reference = board(['101', '100', '000'])
        retest = board(['100', '100', '000'])
        ref_rows = [
            {'key': [0, 0], 'grade': 'A', 'group': 'g1'},
            {'key': [2, 0], 'grade': 'A', 'group': 'g2'},
        ]
        test_rows = [{'key': [0, 0], 'reading': 11}]
        result = deliver_correspondence(reference, retest, ref_rows, test_rows)
        totals = {r['group']: r['total']
                  for r in result['group_delivery']['records']}
        self.assertEqual(totals, {'g1': 11, 'g2': 0})
        bounds = {b['group']: b
                  for b in result['diagnostics']['group_bounds']}
        self.assertEqual((bounds['g2']['min_total'],
                          bounds['g2']['max_total']), (0, 0))

    def test_empty_sides_are_valid_and_zero(self):
        empty = board(['000', '000'])
        result = deliver_correspondence(empty, empty, [], [])
        self.assertEqual(result['grade_delivery']['records'], [])
        self.assertEqual(result['group_delivery']['records'], [])
        self.assertEqual(result['diagnostics']['optimal_matching'],
                         {'pair_count': 0, 'overlap_pixels': 0,
                          'candidate_edge_count': 0, 'allowed_edge_count': 0})

    def test_no_mutual_overlap_gives_certain_zero_groups(self):
        # Disjoint foreground sets: no candidate edge anywhere, so every
        # registered group is certainly empty even though both sides have
        # selected objects.
        # Two isolated reference pixels and one disjoint retest object.
        reference = board(['10100'])
        retest = board(['00011'])
        ref_rows = [
            {'key': [0, 0], 'grade': 'A', 'group': 'g1'},
            {'key': [2, 0], 'grade': 'A', 'group': 'g2'},
        ]
        test_rows = [
            {'key': [3, 0], 'reading': 7},
        ]
        result = deliver_correspondence(
            reference, retest, ref_rows, test_rows)
        self.assertEqual(result['diagnostics']['optimal_matching'],
                         {'pair_count': 0, 'overlap_pixels': 0,
                          'candidate_edge_count': 0, 'allowed_edge_count': 0})
        self.assertEqual(result['group_delivery']['records'],
                         [{'group': 'g1', 'total': 0},
                          {'group': 'g2', 'total': 0}])
        entry = result['diagnostics']['object_evidence'][0]
        self.assertEqual(entry['reason'], NO_MATCH)
        self.assertTrue(entry['can_be_unmatched'])

    def test_eight_connectivity_is_respected(self):
        data = ['10', '01']
        four = board(data, connectivity=4)
        eight = board(data, connectivity=8)
        # Under 8-connectivity the diagonal pixels form one object each.
        result = deliver_correspondence(
            eight, eight,
            [{'key': [0, 0], 'grade': 'A', 'group': 'g'}],
            [{'key': [0, 0], 'reading': 3}])
        self.assertEqual(result['grade_delivery']['records'],
                         [{'key': [0, 0], 'grade': 'A'}])
        # Under 4-connectivity the same pixels are two objects per side.
        result4 = deliver_correspondence(
            four, four,
            [{'key': [0, 0], 'grade': 'A', 'group': 'g'},
             {'key': [1, 1], 'grade': 'A', 'group': 'g'}],
            [{'key': [0, 0], 'reading': 3},
             {'key': [1, 1], 'reading': 4}])
        self.assertEqual(len(result4['grade_delivery']['records']), 2)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.reference = vertical_line_board()
        self.retest = horizontal_line_board()
        self.good_ref = reference_table()
        self.good_test = retest_table()

    def assert_rejected(self, reference=None, retest=None,
                        reference_rows=None, retest_rows=None):
        with self.assertRaises(CorrespondenceError):
            deliver_correspondence(
                self.reference if reference is None else reference,
                self.retest if retest is None else retest,
                self.good_ref if reference_rows is None else reference_rows,
                self.good_test if retest_rows is None else retest_rows)

    def test_shape_and_pixel_size_mismatch(self):
        other = board(['0000'] * HEIGHT)
        self.assert_rejected(retest=other)
        equivalent = board(self.retest['mask'], pixel_size=['1.0', '1.00'])
        # Numerically equal spellings are accepted.
        deliver_correspondence(self.reference, equivalent,
                               self.good_ref, self.good_test)
        unequal = board(self.retest['mask'], pixel_size=['2', '1'])
        self.assert_rejected(retest=unequal)

    def test_bad_reference_rows(self):
        bad_field = [dict(row, extra=1) for row in self.good_ref]
        self.assert_rejected(reference_rows=bad_field)
        duplicate = self.good_ref + [self.good_ref[0]]
        self.assert_rejected(reference_rows=duplicate)
        missing = self.good_ref[:1]
        self.assert_rejected(reference_rows=missing)
        unknown = self.good_ref + [
            {'key': [0, 0], 'grade': 'A', 'group': 'g1'}]
        self.assert_rejected(reference_rows=unknown)
        bad_key = [{'key': [1], 'grade': 'A', 'group': 'g1'},
                   self.good_ref[1]]
        self.assert_rejected(reference_rows=bad_key)
        bool_key = [{'key': [True, 1], 'grade': 'A', 'group': 'g1'},
                    self.good_ref[1]]
        self.assert_rejected(reference_rows=bool_key)
        bad_name = [dict(self.good_ref[0], grade='A/B'), self.good_ref[1]]
        self.assert_rejected(reference_rows=bad_name)
        long_name = 'A' * 33
        bad_grade = [dict(self.good_ref[0], grade=long_name),
                     self.good_ref[1]]
        self.assert_rejected(reference_rows=bad_grade)
        non_string_group = [dict(self.good_ref[0], group=7),
                            self.good_ref[1]]
        self.assert_rejected(reference_rows=non_string_group)

    def test_more_than_eight_groups_rejected(self):
        # Nine isolated reference objects, each registered in its own group.
        mask = ['0' * 9] * 2
        mask[0] = '1' * 9
        reference = board(mask)
        retest = copy.deepcopy(reference)
        ref_rows = [{'key': [x, 0], 'grade': 'A', 'group': f'g{x}'}
                    for x in range(9)]
        test_rows = [{'key': [x, 0], 'reading': 1} for x in range(9)]
        with self.assertRaises(CorrespondenceError):
            deliver_correspondence(reference, retest, ref_rows, test_rows)

    def test_bad_retest_rows(self):
        boolean = [dict(self.good_test[0], reading=True),
                   self.good_test[1]]
        self.assert_rejected(retest_rows=boolean)
        too_high = [dict(self.good_test[0], reading=65536),
                    self.good_test[1]]
        self.assert_rejected(retest_rows=too_high)
        negative = [dict(self.good_test[0], reading=-1),
                    self.good_test[1]]
        self.assert_rejected(retest_rows=negative)
        float_reading = [dict(self.good_test[0], reading=4.0),
                         self.good_test[1]]
        self.assert_rejected(retest_rows=float_reading)
        duplicate = self.good_test + [self.good_test[0]]
        self.assert_rejected(retest_rows=duplicate)
        missing = self.good_test[:1]
        self.assert_rejected(retest_rows=missing)
        bad_field = [dict(row, extra=1) for row in self.good_test]
        self.assert_rejected(retest_rows=bad_field)

    def test_more_than_128_objects_rejected(self):
        mask = ['1' * 129]
        reference = board(mask)
        retest = copy.deepcopy(reference)
        rows = [{'key': [x, 0], 'grade': 'A', 'group': 'g'}
                for x in range(129)]
        readings = [{'key': [x, 0], 'reading': 1} for x in range(129)]
        with self.assertRaises(CorrespondenceError):
            deliver_correspondence(reference, retest, rows, readings)

    def test_underlying_process_error_rejects_batch(self):
        broken = board(['10', '001'])
        with self.assertRaises(CorrespondenceError):
            deliver_correspondence(broken, self.retest,
                                   self.good_ref, self.good_test)


    def test_unequal_sides_use_dummy_vertices_correctly(self):
        # Three reference objects, two retest objects on a 2x5 board; the
        # compact model must handle padding rows/columns (n = max(nr, nt))
        # without letting dummy vertices enter real evidence.
        reference = board(['10101', '00000'])
        retest = board(['10100', '00101'])
        ref_rows = [
            {'key': [0, 0], 'grade': 'A', 'group': 'g1'},
            {'key': [2, 0], 'grade': 'A', 'group': 'g1'},
            {'key': [4, 0], 'grade': 'A', 'group': 'g2'},
        ]
        test_rows = [
            {'key': [0, 0], 'reading': 4},
            {'key': [2, 0], 'reading': 9},
            {'key': [4, 1], 'reading': 5},
        ]
        result = deliver_correspondence(
            reference, retest, ref_rows, test_rows)
        evidence = {tuple(e['key']): e
                    for e in result['diagnostics']['object_evidence']}
        self.assertEqual(evidence[(0, 0)]['candidate_reference_keys'],
                         [[0, 0]])
        self.assertEqual(evidence[(2, 0)]['candidate_reference_keys'],
                         [[2, 0]])
        self.assertEqual(evidence[(4, 1)]['candidate_reference_keys'], [])
        self.assertTrue(evidence[(4, 1)]['can_be_unmatched'])
        totals = {r['group']: r['total']
                  for r in result['group_delivery']['records']}
        self.assertEqual(totals, {'g1': 13, 'g2': 0})


class DeterminismAndRoundTripTests(unittest.TestCase):
    def test_input_reordering_does_not_change_delivery(self):
        first = deliver_correspondence(
            vertical_line_board(), horizontal_line_board(),
            reference_table(group_b='g2'), retest_table())
        shuffled_ref = list(reversed(reference_table(group_b='g2')))
        shuffled_test = list(reversed(retest_table()))
        second = deliver_correspondence(
            vertical_line_board(), horizontal_line_board(),
            shuffled_ref, shuffled_test)
        self.assertEqual(first['grade_delivery'], second['grade_delivery'])
        self.assertEqual(first['group_delivery'], second['group_delivery'])
        self.assertEqual(first['diagnostics'], second['diagnostics'])

    def test_equivalent_pixel_size_spelling_is_invisible(self):
        ref = vertical_line_board()
        test_equiv = board(horizontal_line_board()['mask'],
                           pixel_size=['1.0', '1.00'])
        result = deliver_correspondence(ref, test_equiv,
                                        reference_table(), retest_table())
        self.assertEqual(result['group_delivery']['records'],
                         [{'group': 'g1', 'total': 13}])

    def test_tables_round_trip_without_masks_or_evidence(self):
        result = deliver_correspondence(
            vertical_line_board(),
            horizontal_line_board(rows=(1, 3, 5)),
            reference_table(group_b='g2'),
            retest_table(readings=((1, 1, 4), (1, 3, 9), (1, 5, 20))))
        with tempfile.TemporaryDirectory() as tmp:
            grade_path = os.path.join(tmp, 'grades.json')
            group_path = os.path.join(tmp, 'groups.json')
            save_grade_delivery(grade_path, result['grade_delivery'])
            save_group_delivery(group_path, result['group_delivery'])
            grades = load_grade_delivery(grade_path)
            groups = load_group_delivery(group_path)
        # A consumer holding only the two files gets normalized, sorted data.
        self.assertEqual(grades, parse_grade_delivery(
            json.loads(json.dumps(result['grade_delivery']))))
        self.assertEqual(groups, parse_group_delivery(
            json.loads(json.dumps(result['group_delivery']))))
        # Records are raster sorted by (y, x); groups sorted by name.
        keys = [tuple(r['key']) for r in grades['records']]
        self.assertEqual(keys, sorted(keys, key=lambda k: (k[1], k[0])))
        group_names = [r['group'] for r in groups['records']]
        self.assertEqual(group_names, sorted(group_names))

    def test_saved_files_are_canonical_and_reject_bad_documents(self):
        result = deliver_correspondence(
            vertical_line_board(), horizontal_line_board(),
            reference_table(), retest_table())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'g.json')
            save_grade_delivery(path, result['grade_delivery'])
            raw = open(path, encoding='utf-8').read()
            # Canonical, deterministic bytes.
            self.assertEqual(raw, json.dumps(
                result['grade_delivery'], sort_keys=True,
                separators=(',', ':'), ensure_ascii=False) + '\n')
        for bad in ({'format': 'nope', 'records': []},
                    {'format': result['grade_delivery']['format'],
                     'records': [{'key': [0, 0]}]},
                    []):
            with self.assertRaises(CorrespondenceError):
                parse_grade_delivery(bad)
        for bad in ({'format': 'nope', 'records': []},
                    {'format': result['group_delivery']['format'],
                     'records': [{'group': 'g1', 'total': -1}]},
                    []):
            with self.assertRaises(CorrespondenceError):
                parse_group_delivery(bad)

    def test_two_tables_are_delivered_independently(self):
        # Distinct grades withhold grade rows, but the group table still ships.
        result = deliver_correspondence(
            vertical_line_board(), horizontal_line_board(),
            reference_table(grade_b='B'), retest_table())
        self.assertEqual(result['grade_delivery']['records'], [])
        self.assertEqual(result['group_delivery']['records'],
                         [{'group': 'g1', 'total': 13}])
        # Conversely, an uncertain group withholds the group row while grades
        # still ship.
        other = deliver_correspondence(
            vertical_line_board(), horizontal_line_board(),
            reference_table(group_b='g2'), retest_table())
        self.assertEqual(len(other['grade_delivery']['records']), 2)
        self.assertEqual(other['group_delivery']['records'], [])


class DenseGridTests(unittest.TestCase):
    def test_256_grid_with_128_lines_per_side(self):
        size = 256
        ref_mask = ['0' * size for _ in range(size)]
        test_mask = ['0' * size for _ in range(size)]
        for x in range(1, size, 2):
            ref_mask = [row[:x] + '1' + row[x + 1:] for row in ref_mask]
        for y in range(1, size, 2):
            test_mask[y] = '1' * size
        reference = board(ref_mask)
        retest = board(test_mask)
        ref_rows = [{'key': [x, 0], 'grade': 'A', 'group': 'g'}
                    for x in range(1, size, 2)]
        test_rows = [{'key': [0, y], 'reading': 1}
                     for y in range(1, size, 2)]
        result = deliver_correspondence(
            reference, retest, ref_rows, test_rows)
        # No family of optimal schemes is retained; every retest line can
        # reach every reference line, yet none can be left unmatched.
        self.assertEqual(len(result['grade_delivery']['records']), 128)
        self.assertTrue(all(
            not e['can_be_unmatched']
            and len(e['candidate_reference_keys']) == 128
            for e in result['diagnostics']['object_evidence']))
        self.assertEqual(result['group_delivery']['records'],
                         [{'group': 'g', 'total': 128}])
        self.assertEqual(result['diagnostics']['optimal_matching'],
                         {'pair_count': 128, 'overlap_pixels': 128,
                          'candidate_edge_count': 128 * 128,
                          'allowed_edge_count': 128 * 128})
        # The delivery itself never stores candidate masks or full schemes.
        self.assertEqual(
            set(result['grade_delivery']['records'][0]), {'key', 'grade'})


if __name__ == '__main__':
    unittest.main()
