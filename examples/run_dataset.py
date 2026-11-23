"""Fixed offline dataset-compilation example; run from the repository root.

    .venv/bin/python -I examples/run_dataset.py

The script reproduces the acceptance chain on a 9x3 synthetic board and
writes only under run/dataset/.  Re-running is refused while that directory
already exists.  It never opens a network connection and uses no models.
"""
import json
import os

from micro_objects import (POSITIVE, NEGATIVE, BLIND, compile_dataset,
                           apply_calibration)
from micro_objects.io import load_calibration, save_calibration

OUT = 'run/dataset'


def board(intensity_row):
    return {
        'board_id': 'b0',
        'data': {
            'mask': ['000000000', '011000110', '000000000'],
            'intensity': [[0] * 9, intensity_row, [0] * 9],
            'pixel_size': ['1', '1'],
            'connectivity': 4,
            'selection': {'min_pixels': 2, 'exclude_border': False},
        },
    }


def requests(blind_rect):
    return [
        {'request_id': 'p1', 'board_id': 'b0',
         'rect': [0, 1, 4, 2], 'kind': POSITIVE},
        {'request_id': 'p2', 'board_id': 'b0',
         'rect': [1, 1, 5, 2], 'kind': POSITIVE},
        {'request_id': 'p3', 'board_id': 'b0',
         'rect': [5, 1, 9, 2], 'kind': POSITIVE},
        {'request_id': 'hold', 'board_id': 'b0',
         'rect': list(blind_rect), 'kind': BLIND},
    ]


def main():
    if os.path.exists(OUT):
        raise SystemExit(f'{OUT} already exists; remove it before re-running')
    os.makedirs(OUT)
    row = [10, 1, 1, 90, 20, 5, 1, 1, 200]

    # First batch: blind window [8,9).
    first = compile_dataset([board(row)], requests((8, 1, 9, 2)))
    print('blind [8,9):', json.dumps(first.calibration, sort_keys=True),
          'not_emitted =', first.not_emitted)
    print('  p3 intensity :', first.get_sample('p3')['intensity'])
    print('  hold sample  :', first.get_sample('hold')['intensity'])

    # Second batch: blind window moved left to [7,9).
    second = compile_dataset([board(row)], requests((7, 1, 9, 2)))
    print('blind [7,9):', json.dumps(second.calibration, sort_keys=True),
          'not_emitted =', second.not_emitted)
    print('  hold sample  :', second.get_sample('hold')['intensity'],
          second.get_sample('hold')['supervision'])

    # The calibration record is an independent, portable document.
    cal_path = os.path.join(OUT, 'calibration.json')
    save_calibration(cal_path, second.calibration)
    record = load_calibration(cal_path)
    print('saved record  :', json.dumps(record, sort_keys=True))

    # A downstream with only a new intensity matrix: no mask, no refit.
    new_matrix = [[0, None, 20, 65535]]
    print('new matrix    :', new_matrix)
    print('calibrated    :', apply_calibration(record, new_matrix))

    # A legal negative request on a background-only strip.
    neg_batch = compile_dataset(
        [board(row)],
        [{'request_id': 'neg', 'board_id': 'b0',
          'rect': [0, 0, 9, 1], 'kind': NEGATIVE},
         {'request_id': 'hold2', 'board_id': 'b0',
          'rect': [7, 1, 9, 2], 'kind': BLIND}])
    print('negative emits:', neg_batch.emitted_ids(),
          'supervision row:',
          neg_batch.get_sample('neg')['supervision'])
    print('storage stats :', json.dumps(second.storage_stats(),
                                        sort_keys=True))


if __name__ == '__main__':
    main()
