"""Offline scan-strip archive: end-to-end synthetic example.

Builds a portable SQLite archive from a one-shot stream of strips, then
re-opens the pack without the source data and reads object metrics, streams
one object's samples in batches, inspects the per-band offline decisions and
recovers an approved band's original mask and intensities.

Run from the repository root with:

    PYTHONPATH=src python3 examples/run_archive.py
"""
import json
import os
import tempfile

from micro_objects import (APPROVED, ArchiveReader, INSUFFICIENT_BUDGET,
                           build_archive)

WIDTH, HEIGHT = 8, 6
BAND_HEIGHT = 2


def make_strips():
    """A small board with two objects, one of which encloses a hole."""
    board = [
        '11100011',
        '10100011',
        '00000000',
        '01111100',
        '01000100',
        '01111100',
    ]
    strips = []
    for ordinal, start in enumerate(range(0, HEIGHT, BAND_HEIGHT)):
        rows = board[start:start + BAND_HEIGHT]
        strips.append({
            'band_id': f'band-{ordinal}',
            'start_row': start,
            'offline': ordinal in (0, 2),
            'mask': rows,
            'intensity': [[start * WIDTH + y * WIDTH + x + 1
                           for x in range(WIDTH)]
                          for y in range(len(rows))],
        })
    return strips


def main():
    header = {
        'width': WIDTH,
        'height': HEIGHT,
        'pixel_size': ['1', '1'],
        'connectivity': 4,
        'selection': {'min_pixels': 1, 'exclude_border': False},
    }
    strips = make_strips()
    # A deliberately tight budget: band-0 is approved, band-2 then cannot fit.
    budget = 8

    with tempfile.TemporaryDirectory() as workdir:
        target = os.path.join(workdir, 'archive.db')
        build_archive(target, header, strips, budget)
        print(f'published portable pack: {target}')

        reader = ArchiveReader(target)
        print('\nheader:', reader.selection, 'connectivity',
              reader.connectivity, 'budget', reader.budget,
              'used', reader.used_budget())

        print('\nselected objects:')
        for key in reader.list_object_keys():
            metrics = reader.get_object(key)
            print(' ', key, json.dumps(metrics, sort_keys=True))

        ring_key = [1, 3]
        print(f'\nstreaming samples of {ring_key} in batches of 4:')
        for batch in reader.iter_object_samples(ring_key, batch_size=4):
            print(' ', batch)

        print('\nper-band offline decisions:')
        for decision in reader.list_band_decisions():
            print(' ', decision)

        approved = next(d['band_id'] for d in reader.list_band_decisions()
                        if d['decision'] == APPROVED)
        recovered = reader.recover_band(approved)
        print(f'\nrecovered approved band {approved!r}:')
        print(json.dumps(recovered, indent=2, sort_keys=True))

        refused = next(d['band_id'] for d in reader.list_band_decisions()
                       if d['decision'] == INSUFFICIENT_BUDGET)
        try:
            reader.recover_band(refused)
        except Exception as exc:
            print(f"\nrecovering non-approved band {refused!r} is refused: "
                  f"{type(exc).__name__}: {exc}")
        reader.close()


if __name__ == '__main__':
    main()
