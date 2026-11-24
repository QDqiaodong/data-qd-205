"""Fixed offline region-attribution example; run from the repository root.

    .venv/bin/python -I examples/run_regions.py

The first specimen is the acceptance board: a 3-pixel whole-board object
spans A and B while the middle pixel can alternatively be declared null.
The second specimen observes A but has no selected foreground there, and
has not collected B.  The script writes one atomic JSON delivery and never
uses a network service, UI, or real image importer.
"""
import os

from micro_objects import deliver_regions
from micro_objects.io import publish

OUT = 'run/regions'


def acceptance_board():
    return {
        'mask': ['00000', '01110', '00000'],
        'intensity': [
            [0, 0, 0, 0, 0],
            [0, 10, 20, 30, 0],
            [0, 0, 0, 0, 0],
        ],
        'pixel_size': ['1', '1'],
        'connectivity': 4,
        'selection': {'min_pixels': 3, 'exclude_border': False},
    }


def main():
    if os.path.exists(OUT):
        raise SystemExit(f'{OUT} already exists; remove it before re-running')
    os.makedirs(OUT)

    specimens = [
        {
            'specimen_id': 's1',
            'data': acceptance_board(),
            'region_grid': [
                [None, None, None, None, None],
                [None, 'A', 'A', 'B', None],
                [None, None, None, None, None],
            ],
            'observed_regions': ['A', 'B'],
        },
        {
            'specimen_id': 's2',
            'data': {
                'mask': ['00000', '01000', '00000'],
                'intensity': [[0] * 5, [0, 7, 0, 0, 0], [0] * 5],
                'pixel_size': ['1', '1'],
                'connectivity': 4,
                'selection': {'min_pixels': 3, 'exclude_border': False},
            },
            'region_grid': [
                [None, None, None, None, None],
                [None, 'A', None, None, None],
                [None, None, None, None, None],
            ],
            'observed_regions': ['A'],
        },
    ]

    # Deliberately reverse both business-irrelevant input orders.
    delivery = deliver_regions(['B', 'A'], list(reversed(specimens)))
    assert delivery.verify_conservation()

    obj = delivery.specimen('s1')['objects'][0]
    contributions = {c['region_id']: c for c in obj['region_contributions']}
    print('object key       :', obj['object_key'])
    print('A contribution   :',
          contributions['A']['pixel_count'],
          contributions['A']['intensity_sum'],
          contributions['A']['area'],
          contributions['A']['ratio'])
    print('B contribution   :',
          contributions['B']['pixel_count'],
          contributions['B']['intensity_sum'],
          contributions['B']['area'],
          contributions['B']['ratio'])
    print('region summaries :', [(r['region_id'], r['complete'],
                                 r['observed_specimen_ids'],
                                 r['missing_specimen_ids'])
                                for r in delivery['region_summaries']])

    output_path = os.path.join(OUT, 'region-delivery.json')
    publish(output_path, delivery)
    print('wrote            :', output_path)

    # A second, in-memory batch shows completion after B is collected.
    completed_specimens = [
        specimens[0],
        {
            **specimens[1],
            'observed_regions': ['A', 'B'],
            'region_grid': [
                [None, None, None, None, 'B'],
                [None, 'A', None, None, None],
                [None, None, None, None, None],
            ],
        },
    ]
    completed = deliver_regions(['A', 'B'], completed_specimens)
    print('B complete after observation patch:',
          completed.region_summary('B')['complete'])


if __name__ == '__main__':
    main()
