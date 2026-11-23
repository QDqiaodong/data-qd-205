"""End-to-end offline chain on the 8x3 synthetic canvas from the contract.

Run from the repository root with an isolated interpreter:

    .venv/bin/python -I examples/run_pipeline.py

The script only writes under run/pipeline/ and never opens a network
connection.  Re-running is refused while that directory already exists.
"""
import json
import os

from micro_objects import (ReviewStore, build_delta, build_records,
                           compile_snapshot, record_digest)
from micro_objects.io import load_delta, save_delta

WIDTH, HEIGHT = 8, 3
OUT = 'run/pipeline'


def low_tile():
    mask = ['00000000', '01110110', '00000000']
    intensity = [[0] * WIDTH for _ in range(HEIGHT)]
    for x in (1, 2, 3, 5, 6):
        intensity[1][x] = 10
    return {'tile_id': 'low', 'x': 0, 'y': 0, 'priority': 0,
            'mask': mask, 'intensity': intensity}


def snapshot(version, high=None):
    tiles = [low_tile()]
    if high is not None:
        char, value = high
        tiles.append({'tile_id': 'high', 'x': 2, 'y': 1, 'priority': 10,
                      'mask': [char], 'intensity': [[value]]})
    return {
        'dataset_id': 'demo-dataset', 'version_id': version,
        'width': WIDTH, 'height': HEIGHT,
        'pixel_size': ['1', '1'], 'connectivity': 4,
        'selection': {'min_pixels': 1, 'exclude_border': True},
        'tiles': tiles,
    }


def main():
    if os.path.exists(OUT):
        raise SystemExit(f'{OUT} already exists; remove it before re-running')
    inbox = os.path.join(OUT, 'inbox')
    os.makedirs(inbox)

    # Upstream: compile snapshots and publish self-contained increments.
    s0, s1, s2 = snapshot('R0'), snapshot('R1', ('1', 10)), \
        snapshot('R2', ('0', 10))
    deltas = (build_delta(None, s0), build_delta(s0, s1),
              build_delta(s1, s2))
    for name, payload in zip(('r0.json', 'r1.json', 'r2.json'), deltas):
        save_delta(os.path.join(inbox, name), payload)

    # Downstream: rebuild state from delta files alone (no tiles available).
    store = ReviewStore(os.path.join(OUT, 'store.json'), 'demo-dataset')
    r0_receipt = store.import_delta(load_delta(os.path.join(inbox, 'r0.json')))
    print('r0.json', json.dumps(
        {k: r0_receipt[k] for k in ('inserted', 'updated', 'deleted',
                                    'revoked_review_ids')}, sort_keys=True))

    # Downstream reviews are recorded independently against R0 records.
    r0_records = build_records(compile_snapshot(s0))
    store.register_review('review-1', [1, 1],
                          record_digest(r0_records[(1, 1)]))
    store.register_review('review-2', [5, 1],
                          record_digest(r0_records[(5, 1)]))

    for name in ('r1.json', 'r2.json'):
        receipt = store.import_delta(load_delta(os.path.join(inbox, name)))
        print(name, json.dumps(
            {k: receipt[k] for k in ('inserted', 'updated', 'deleted',
                                     'revoked_review_ids')},
            sort_keys=True))

    # Replay R1 after R2 has already been imported: original receipt only,
    # the head does not move and no review is revoked twice.
    replay = store.import_delta(load_delta(os.path.join(inbox, 'r1.json')))
    print('replay r1:', replay['to_version_id'],
          'revoked:', replay['revoked_review_ids'],
          'head:', store.current_version())


if __name__ == '__main__':
    main()
