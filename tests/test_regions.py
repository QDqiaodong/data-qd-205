import copy
import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from micro_objects import deliver_regions, process, verify_region_delivery
from micro_objects.io import load_region_delivery, save_region_delivery


def board_5x3():
    return {
        'mask': ['00000', '01110', '00000'],
        'intensity': [[0, 0, 0, 0, 0],
                      [0, 10, 20, 30, 0],
                      [0, 0, 0, 0, 0]],
        'pixel_size': ['1', '1'],
        'connectivity': 4,
        'selection': {'min_pixels': 3, 'exclude_border': False},
    }


def cross_grid(middle='A'):
    return [
        [None, None, None, None, None],
        [None, 'A', middle, 'B', None],
        [None, None, None, None, None],
    ]


def quiet_specimen(specimen_id='s2'):
    board = {
        'mask': ['00000', '01000', '00000'],
        'intensity': [[0] * 5, [0, 7, 0, 0, 0], [0] * 5],
        'pixel_size': ['1', '1'],
        'connectivity': 4,
        'selection': {'min_pixels': 3, 'exclude_border': False},
    }
    grid = [[None] * 5, [None, 'A', None, None, None], [None] * 5]
    return {'specimen_id': specimen_id, 'data': board,
            'region_grid': grid, 'observed_regions': ['A']}


def valid_batch():
    return (
        ['A', 'B'],
        [
            {'specimen_id': 's1', 'data': board_5x3(),
             'region_grid': cross_grid(),
             'observed_regions': ['A', 'B']},
            quiet_specimen(),
        ],
    )


def region_record(delivery, specimen_id, region_id):
    return next(r for r in delivery['specimen_region_records']
                if r['specimen_id'] == specimen_id
                and r['region_id'] == region_id)


class RegionAttributionTests(unittest.TestCase):
    def test_cross_region_object_keeps_whole_board_identity(self):
        catalog, specimens = valid_batch()
        delivery = deliver_regions(catalog, specimens)
        specimen = delivery.specimen('s1')
        obj = specimen['objects'][0]

        # The region overlay must not change the process() result.
        self.assertEqual(specimen['process_result'], process(board_5x3()))
        self.assertEqual(len(specimen['objects']), 1)
        self.assertEqual(obj['object_key'], ['s1', 1, 1])
        self.assertEqual(obj['first_x'], 1)
        self.assertEqual(obj['first_y'], 1)
        self.assertEqual(obj['pixels'][0], [1, 1])
        self.assertFalse(obj['touches_border'])

        by_region = {c['region_id']: c
                     for c in obj['region_contributions']}
        self.assertEqual(sorted(by_region), ['A', 'B'])
        a, b = by_region['A'], by_region['B']

        self.assertEqual((a['pixel_count'], a['intensity_sum']), (2, 30))
        self.assertEqual((b['pixel_count'], b['intensity_sum']), (1, 30))
        self.assertEqual(a['area'], '2')
        self.assertEqual(b['area'], '1')
        self.assertEqual(a['ratio'], '2/3')
        self.assertEqual(b['ratio'], '1/3')
        self.assertEqual(Fraction(a['ratio']), Fraction(2, 3))
        self.assertEqual(Fraction(b['ratio']), Fraction(1, 3))
        self.assertEqual(
            Fraction(a['area']) + Fraction(b['area']),
            Fraction(obj['area']))
        self.assertEqual(obj['unassigned']['pixel_count'], 0)
        self.assertTrue(verify_region_delivery(delivery))
        json.dumps(delivery, allow_nan=False)

    def test_atomic_region_delivery_save_and_load(self):
        delivery = deliver_regions(*valid_batch())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'regions.json'
            save_region_delivery(path, delivery)
            loaded = load_region_delivery(path)
        self.assertTrue(verify_region_delivery(loaded))
        self.assertEqual(
            loaded['region_summaries'][0]['pixel_count'], 2)
        self.assertEqual(
            loaded['objects'][0]['region_contributions'][0]['ratio'], '2/3')

    def test_null_middle_pixel_conserves_all_measures(self):
        catalog, specimens = valid_batch()
        specimens[0]['region_grid'][1][2] = None
        delivery = deliver_regions(catalog, specimens)
        obj = delivery.specimen('s1')['objects'][0]
        by_region = {c['region_id']: c
                     for c in obj['region_contributions']}

        self.assertEqual(obj['pixel_count'], 3)
        self.assertEqual(obj['intensity_sum'], 60)
        self.assertEqual(by_region['A']['pixel_count'], 1)
        self.assertEqual(by_region['A']['intensity_sum'], 10)
        self.assertEqual(by_region['A']['ratio'], '1/3')
        self.assertEqual(by_region['B']['pixel_count'], 1)
        self.assertEqual(by_region['B']['intensity_sum'], 30)
        unassigned = obj['unassigned']
        self.assertEqual(unassigned['pixel_count'], 1)
        self.assertEqual(unassigned['intensity_sum'], 20)
        self.assertEqual(unassigned['area'], '1')
        self.assertEqual(unassigned['ratio'], '1/3')

        count = (sum(c['pixel_count'] for c in obj['region_contributions'])
                  + unassigned['pixel_count'])
        intensity = (sum(c['intensity_sum']
                         for c in obj['region_contributions'])
                     + unassigned['intensity_sum'])
        area = sum((Fraction(c['area'])
                    for c in obj['region_contributions']), Fraction(0))
        area += Fraction(unassigned['area'])
        self.assertEqual(count, obj['pixel_count'])
        self.assertEqual(intensity, obj['intensity_sum'])
        self.assertEqual(area, Fraction(obj['area']))
        self.assertEqual(delivery.specimen('s1')['null_pixel_count'], 1)
        self.assertEqual(delivery.specimen('s1')['null_intensity_sum'], 20)

    def test_observed_zero_and_missing_numeric_nulls(self):
        delivery = deliver_regions(*valid_batch())

        a2 = region_record(delivery, 's2', 'A')
        b2 = region_record(delivery, 's2', 'B')
        self.assertTrue(a2['observed'])
        self.assertEqual(a2['status'], 'observed')
        self.assertEqual((a2['pixel_count'], a2['area'],
                          a2['intensity_sum'], a2['object_count']),
                         (0, '0', 0, 0))
        self.assertFalse(b2['observed'])
        self.assertEqual(b2['status'], 'missing')
        self.assertIsNone(b2['pixel_count'])
        self.assertIsNone(b2['area'])
        self.assertIsNone(b2['intensity_sum'])
        self.assertIsNone(b2['object_count'])

        summaries = {r['region_id']: r for r in delivery['region_summaries']}
        self.assertTrue(summaries['A']['complete'])
        self.assertEqual(summaries['A']['observed_specimen_ids'], ['s1', 's2'])
        self.assertEqual(summaries['A']['pixel_count'], 2)
        self.assertEqual(summaries['A']['intensity_sum'], 30)
        self.assertEqual(summaries['A']['object_count'], 1)

        self.assertFalse(summaries['B']['complete'])
        self.assertEqual(summaries['B']['observed_specimen_ids'], ['s1'])
        self.assertEqual(summaries['B']['missing_specimen_ids'], ['s2'])
        self.assertEqual(summaries['B']['observed_specimen_count'], 1)
        self.assertEqual(summaries['B']['pixel_count'], 1)
        self.assertEqual(summaries['B']['intensity_sum'], 30)
        self.assertEqual(summaries['B']['object_count'], 1)

        # Adding a real observed B cell makes the region complete while the
        # newly observed specimen-region can still be an exact zero.
        catalog, specimens = valid_batch()
        specimens[1]['observed_regions'].append('B')
        specimens[1]['region_grid'][0][4] = 'B'
        completed = deliver_regions(catalog, specimens)
        b_summary = completed.region_summary('B')
        self.assertTrue(b_summary['complete'])
        self.assertEqual(b_summary['missing_specimen_ids'], [])
        self.assertEqual(b_summary['pixel_count'], 1)
        self.assertEqual(region_record(completed, 's2', 'B')['pixel_count'], 0)

    def test_region_containing_only_rejected_object_is_zero(self):
        board = {
            'mask': ['010', '000', '000'],
            'intensity': [[9, 3, 0], [0, 0, 0], [0, 0, 0]],
            'pixel_size': ['1', '1'],
            'connectivity': 4,
            'selection': {'min_pixels': 2, 'exclude_border': False},
        }
        specimen = {'specimen_id': 'rejected', 'data': board,
                    'region_grid': [['A', 'A', None],
                                    [None, None, None],
                                    [None, None, None]],
                    'observed_regions': ['A']}
        delivery = deliver_regions(['A'], [specimen])
        result = delivery.specimen('rejected')['process_result']
        self.assertEqual(result['summary']['accepted'], 0)
        self.assertEqual(result['objects'][0]['rejection_reasons'],
                         ['below_min_pixels'])
        self.assertEqual(delivery.specimen('rejected')['objects'], [])
        record = region_record(delivery, 'rejected', 'A')
        self.assertEqual((record['pixel_count'], record['intensity_sum'],
                          record['object_count']), (0, 0, 0))

    def test_region_boundary_does_not_change_border_status(self):
        board = {
            'mask': ['111', '000', '000'],
            'intensity': [[1, 2, 3], [0, 0, 0], [0, 0, 0]],
            'pixel_size': ['1', '1'],
            'connectivity': 4,
            'selection': {'min_pixels': 3, 'exclude_border': False},
        }
        specimen = {'specimen_id': 'edge', 'data': board,
                    'region_grid': [['A', 'A', 'B'],
                                    [None, None, None],
                                    [None, None, None]],
                    'observed_regions': ['A', 'B']}
        delivery = deliver_regions(['A', 'B'], [specimen])
        obj = delivery.specimen('edge')['objects'][0]
        self.assertTrue(obj['touches_border'])
        counts = {c['region_id']: c['pixel_count']
                  for c in obj['region_contributions']}
        self.assertEqual(counts, {'A': 2, 'B': 1})

        board['selection']['exclude_border'] = True
        delivery = deliver_regions(['A', 'B'], [specimen])
        obj = delivery.specimen('edge')['process_result']['objects'][0]
        self.assertIn('touches_border', obj['rejection_reasons'])
        self.assertEqual(delivery.specimen('edge')['objects'], [])
        for rid in ('A', 'B'):
            self.assertEqual(
                region_record(delivery, 'edge', rid)['pixel_count'], 0)

    def test_eight_connectivity_diagonal_object_can_span_regions(self):
        board = {
            'mask': ['10', '01'],
            'intensity': [[5, 0], [0, 7]],
            'pixel_size': ['1', '1'],
            'connectivity': 8,
            'selection': {'min_pixels': 1, 'exclude_border': False},
        }
        specimen = {
            'specimen_id': 'diag', 'data': board,
            'region_grid': [['A', None], [None, 'B']],
            'observed_regions': ['A', 'B'],
        }
        delivery = deliver_regions(['A', 'B'], [specimen])
        obj = delivery.specimen('diag')['objects'][0]
        self.assertEqual(delivery.specimen('diag')['process_result']
                         ['summary']['objects'], 1)
        self.assertEqual(obj['object_key'], ['diag', 0, 0])
        self.assertEqual(obj['pixel_count'], 2)
        by_region = {c['region_id']: c for c in obj['region_contributions']}
        self.assertEqual(by_region['A']['intensity_sum'], 5)
        self.assertEqual(by_region['B']['intensity_sum'], 7)
        self.assertEqual(by_region['A']['ratio'], '1/2')
        self.assertEqual(by_region['B']['ratio'], '1/2')

    def test_object_entirely_in_null_cells_is_not_dropped(self):
        specimen = {
            'specimen_id': 'null-object', 'data': board_5x3(),
            'region_grid': [[None] * 5 for _ in range(3)],
            'observed_regions': [],
        }
        delivery = deliver_regions(['A', 'B'], [specimen])
        obj = delivery.specimen('null-object')['objects'][0]
        self.assertEqual(obj['region_contributions'], [])
        self.assertEqual(obj['unassigned']['pixel_count'], 3)
        self.assertEqual(obj['unassigned']['intensity_sum'], 60)
        self.assertEqual(obj['unassigned']['area'], '3')
        self.assertEqual(obj['unassigned']['ratio'], '1')
        null = delivery.specimen('null-object')['null_allocation']
        self.assertEqual((null['pixel_count'], null['intensity_sum']), (3, 60))
        for rid in ('A', 'B'):
            record = region_record(delivery, 'null-object', rid)
            self.assertFalse(record['observed'])
            self.assertIsNone(record['pixel_count'])
        self.assertTrue(delivery.region_summary('A')['complete'] is False)
        self.assertEqual(delivery.region_summary('A')['observed_specimen_ids'], [])

    def test_physical_area_uses_exact_arithmetic(self):
        board = board_5x3()
        board['pixel_size'] = ['2', '3']
        specimen = {'specimen_id': 'area', 'data': board,
                    'region_grid': cross_grid(),
                    'observed_regions': ['A', 'B']}
        delivery = deliver_regions(['A', 'B'], [specimen])
        obj = delivery.specimen('area')['objects'][0]
        areas = {c['region_id']: Fraction(c['area'])
                 for c in obj['region_contributions']}
        self.assertEqual(areas, {'A': Fraction(12), 'B': Fraction(6)})
        self.assertEqual(Fraction(obj['area']), Fraction(18))

        board['pixel_size'] = ['0.1', '0.2']
        delivery = deliver_regions(['A', 'B'], [specimen])
        obj = delivery.specimen('area')['objects'][0]
        areas = {c['region_id']: Fraction(c['area'])
                 for c in obj['region_contributions']}
        self.assertEqual(areas['A'], Fraction(1, 25))
        self.assertEqual(areas['B'], Fraction(1, 50))
        self.assertEqual(Fraction(obj['area']), Fraction(3, 50))

    def test_input_order_does_not_change_delivery(self):
        catalog, specimens = valid_batch()
        first = deliver_regions(catalog, specimens)

        reordered_specimens = [copy.deepcopy(specimens[1]),
                               copy.deepcopy(specimens[0])]
        reordered_specimens[0]['observed_regions'].reverse()
        reordered_specimens[1]['observed_regions'].reverse()
        second = deliver_regions(list(reversed(catalog)), reordered_specimens)
        self.assertEqual(first, second)
        self.assertEqual(second['region_catalog'], ['A', 'B'])
        self.assertEqual([s['specimen_id'] for s in second['specimens']],
                         ['s1', 's2'])
        self.assertEqual([r['region_id'] for r in second['region_summaries']],
                         ['A', 'B'])

    def test_deterministic_small_board_conservation(self):
        import random
        rng = random.Random(205)
        specimens = []
        for index in range(6):
            width = rng.randint(1, 8)
            height = rng.randint(1, 8)
            mask = [''.join(rng.choice('01') for _ in range(width))
                    for _ in range(height)]
            intensity = [[rng.randrange(0, 65536) for _ in range(width)]
                         for _ in range(height)]
            grid = [[rng.choice([None, 'A', 'B']) for _ in range(width)]
                    for _ in range(height)]
            grid[0][0] = 'A'
            if width > 1:
                grid[0][1] = 'B'
            else:
                grid[min(1, height - 1)][0] = 'B'
            specimens.append({
                'specimen_id': f'r{index}',
                'data': {
                    'mask': mask,
                    'intensity': intensity,
                    'pixel_size': [str(rng.randint(1, 4)),
                                   str(rng.randint(1, 4))],
                    'connectivity': rng.choice([4, 8]),
                    'selection': {
                        'min_pixels': rng.randint(1, 4),
                        'exclude_border': rng.choice([True, False]),
                    },
                },
                'region_grid': grid,
                'observed_regions': ['A', 'B'],
            })
        delivery = deliver_regions(['B', 'A'], specimens)
        self.assertTrue(delivery.verify_conservation())
        self.assertEqual(len(delivery['specimens']), 6)


class RegionValidationTests(unittest.TestCase):
    def assert_rejects(self, catalog, specimens):
        with self.assertRaises(ValueError):
            deliver_regions(catalog, specimens)

    def test_duplicate_and_unknown_identities(self):
        catalog, specimens = valid_batch()
        duplicate_specimens = [specimens[0], copy.deepcopy(specimens[0])]
        self.assert_rejects(catalog, duplicate_specimens)
        self.assert_rejects(['A', 'A'], specimens)
        self.assert_rejects(['A', 'A:B'], specimens)

        catalog, specimens = valid_batch()
        specimens[0]['observed_regions'] = ['A', 'C']
        self.assert_rejects(catalog, specimens)

    def test_grid_declaration_consistency(self):
        catalog, specimens = valid_batch()
        specimens[0]['region_grid'][1][3] = 'C'
        self.assert_rejects(catalog, specimens)

        catalog, specimens = valid_batch()
        specimens[0]['region_grid'][1][3] = 'A'
        specimens[0]['observed_regions'] = ['B']
        self.assert_rejects(catalog, specimens)

        catalog, specimens = valid_batch()
        specimens[0]['observed_regions'] = ['A', 'B', 'C']
        self.assert_rejects(catalog, specimens)

        catalog, specimens = valid_batch()
        specimens[0]['observed_regions'] = ['A', 'B']
        specimens[0]['region_grid'][1][3] = None
        self.assert_rejects(catalog, specimens)

    def test_dimension_mismatch(self):
        catalog, specimens = valid_batch()
        specimens[0]['region_grid'].pop()
        self.assert_rejects(catalog, specimens)

        catalog, specimens = valid_batch()
        specimens[0]['region_grid'][0].pop()
        self.assert_rejects(catalog, specimens)

    def test_bad_board_and_illegal_fields_raise(self):
        catalog, specimens = valid_batch()
        specimens[0]['data']['intensity'][1][0] = True
        self.assert_rejects(catalog, specimens)

        catalog, specimens = valid_batch()
        specimens[0]['data']['selection']['min_pixels'] = True
        self.assert_rejects(catalog, specimens)

        catalog, specimens = valid_batch()
        specimens[0]['unexpected'] = True
        self.assert_rejects(catalog, specimens)

        with self.assertRaises(ValueError):
            deliver_regions({'region_catalog': ['A'], 'specimens': [],
                              'extra': 1})

    def test_batch_limits(self):
        board = board_5x3()
        specimen = {'specimen_id': 's', 'data': board,
                    'region_grid': cross_grid(),
                    'observed_regions': ['A', 'B']}
        self.assert_rejects([], [specimen])
        self.assert_rejects([f'r{i}' for i in range(65)], [specimen])
        self.assert_rejects(['A', 'B'], [])
        specimens = []
        for i in range(33):
            item = copy.deepcopy(specimen)
            item['specimen_id'] = f's{i}'
            specimens.append(item)
        self.assert_rejects(['A', 'B'], specimens)

    def test_batch_dict_and_flat_board_forms(self):
        catalog, specimens = valid_batch()
        flat = dict(specimens[0])
        flat.update(flat.pop('data'))
        batch = {'region_catalog': catalog, 'specimens': [flat]}
        delivery = deliver_regions(batch)
        self.assertEqual(delivery.specimen('s1')['object_keys'],
                         [['s1', 1, 1]])

        delivery2 = deliver_regions(catalog, [specimens[0]])
        self.assertEqual(delivery, delivery2)

        reversed_args = deliver_regions([specimens[0]], catalog)
        self.assertEqual(reversed_args, delivery2)

        duplicated_observed = copy.deepcopy(specimens[0])
        duplicated_observed['observed_regions'] = ['A', 'B', 'A']
        delivery3 = deliver_regions(catalog, [duplicated_observed])
        self.assertEqual(delivery3, delivery2)

        weird_id = copy.deepcopy(specimens[0])
        weird_id['specimen_id'] = 'specimen: 01'
        delivery4 = deliver_regions(catalog, [weird_id])
        self.assertEqual(delivery4.specimen('specimen: 01')
                         ['object_keys'][0][0], 'specimen: 01')


if __name__ == '__main__':
    unittest.main()
