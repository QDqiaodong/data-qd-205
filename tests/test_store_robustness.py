import json
import os
import tempfile
import unittest

from micro_objects import (DatasetMismatch, DeltaRejected, ReviewRejected,
                           ReviewStore, StoreError, build_delta,
                           build_records, compile_snapshot, digest,
                           parse_delta, record_digest)
from micro_objects.io import load_delta, save_delta


def snapshot(version, selection=None):
    return {
        'dataset_id': 'ds-x', 'version_id': version, 'width': 4, 'height': 2,
        'pixel_size': ['1', '1'], 'connectivity': 4,
        'selection': selection or {'min_pixels': 1,
                                   'exclude_border': False},
        'tiles': [{
            'tile_id': 't', 'x': 0, 'y': 0, 'priority': 0,
            'mask': ['1101', '0000'],
            'intensity': [[1, 2, 0, 4], [0, 0, 0, 0]]}],
    }


class StoreRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'nested', 'state.json')
        self.store = ReviewStore(self.path, 'ds-x')
        self.s0 = snapshot('v0')
        self.s1 = snapshot('v1', {'min_pixels': 2,
                                  'exclude_border': False})
        self.d0 = build_delta(None, self.s0)
        self.d1 = build_delta(self.s0, self.s1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_delta_file_roundtrip_parses_identically(self):
        file_path = os.path.join(self.tmp.name, 'd0.json')
        save_delta(file_path, self.d0)
        loaded = load_delta(file_path)
        self.assertEqual(parse_delta(loaded), parse_delta(self.d0))
        # Canonical on-disk bytes are stable across re-serialization.
        with open(file_path, 'rb') as handle:
            first = handle.read()
        save_delta(os.path.join(self.tmp.name, 'd0b.json'), loaded)
        with open(os.path.join(self.tmp.name, 'd0b.json'), 'rb') as handle:
            second = handle.read()
        self.assertEqual(first.rstrip(b'\n'), second.rstrip(b'\n'))

    def test_dataset_mismatch_on_import_and_open(self):
        other = build_delta(None, {**self.s0, 'dataset_id': 'ds-y'})
        with self.assertRaises(DatasetMismatch):
            self.store.import_delta(other)
        with self.assertRaises(DatasetMismatch):
            ReviewStore(self.path, 'ds-y')

    def test_duplicate_review_id_and_double_registration(self):
        self.store.import_delta(self.d0)
        record = next(iter(build_records(
            compile_snapshot(self.s0)).values()))
        self.store.register_review('rv', record['key'],
                                   record_digest(record))
        with self.assertRaises(ReviewRejected):
            self.store.register_review(
                'rv', record['key'], record_digest(record))
        with self.assertRaises(ReviewRejected):
            self.store.register_review(
                'rv2', record['key'], record_digest(record))

    def test_malformed_documents(self):
        no_records = {**self.d0, 'to_manifest_digest': digest([])}
        no_records['delta_digest'] = digest(
            {k: v for k, v in no_records.items() if k != 'delta_digest'})
        for payload in (None, [], 'text', 42, {}, {'format': 'x'},
                        {**self.d0, 'delta_digest': '0' * 64}, no_records):
            with self.subTest(payload=str(payload)[:30]):
                with self.assertRaises(DeltaRejected):
                    self.store.import_delta(payload)

    def test_corrupt_state_file_rejected_without_overwrite(self):
        self.store.import_delta(self.d0)
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('{not json')
        with self.assertRaises(StoreError):
            ReviewStore(self.path, 'ds-x')
        with open(self.path, 'w', encoding='utf-8') as handle:
            json.dump({'format': 'unknown/9'}, handle)
        with self.assertRaises(StoreError):
            ReviewStore(self.path, 'ds-x')

    def test_selection_driven_delete_revokes_review(self):
        self.store.import_delta(self.d0)
        records = build_records(compile_snapshot(self.s0))
        # Review the singleton object that the new min_pixels threshold drops.
        singleton = records[(3, 0)]
        self.store.register_review('rv', singleton['key'],
                                   record_digest(singleton))
        receipt = self.store.import_delta(self.d1)
        self.assertEqual(receipt['deleted'], [[3, 0]])
        self.assertEqual(receipt['revoked_review_ids'], ['rv'])
        self.assertFalse(self.store.get_review('rv')['active'])
        self.assertEqual(self.store.get_review('rv')
                         ['revoked_at_version'], 'v1')
        # The surviving object's key is untouched by the delta.
        self.assertEqual(self.store.list_object_keys(), [(0, 0)])


if __name__ == '__main__':
    unittest.main()
