import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from micro_objects import (DeltaRejected, ReviewRejected, ReviewStore,
                           StaleVersion, StoreError, build_delta,
                           build_records, compile_snapshot, digest,
                           manifest_digest, record_digest)

WIDTH, HEIGHT = 8, 3


def low_tile():
    mask = ['00000000', '01110110', '00000000']
    intensity = [[0] * WIDTH for _ in range(HEIGHT)]
    for x in (1, 2, 3, 5, 6):
        intensity[1][x] = 10
    return {'tile_id': 'low', 'x': 0, 'y': 0, 'priority': 0,
            'mask': mask, 'intensity': intensity}


def snapshot(version, high=None, selection=None):
    tiles = [low_tile()]
    if high is not None:
        char, value = high
        tiles.append({'tile_id': 'high', 'x': 2, 'y': 1, 'priority': 10,
                      'mask': [char], 'intensity': [[value]]})
    return {
        'dataset_id': 'ds-1', 'version_id': version,
        'width': WIDTH, 'height': HEIGHT,
        'pixel_size': ['1', '1'], 'connectivity': 4,
        'selection': selection or {'min_pixels': 1, 'exclude_border': True},
        'tiles': tiles,
    }


class ReviewStoreChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'store', 'state.json')
        self.store = ReviewStore(self.path, 'ds-1')
        self.s0 = snapshot('R0')
        self.s1 = snapshot('R1', ('1', 10))
        self.s2 = snapshot('R2', ('0', 10))
        self.sq = snapshot('RQ', ('?', 10))
        self.d0 = build_delta(None, self.s0)
        self.d1 = build_delta(self.s0, self.s1)
        self.d2 = build_delta(self.s1, self.s2)
        self.dq = build_delta(self.s2, self.sq)

    def tearDown(self):
        self.tmp.cleanup()

    def _digest(self, snap, key):
        return record_digest(build_records(compile_snapshot(snap))[key])

    def test_full_review_lifecycle_and_revocation(self):
        receipt0 = self.store.import_delta(self.d0)
        self.assertEqual(receipt0['inserted'], [[1, 1], [5, 1]])
        self.assertEqual(self.store.current_version(), 'R0')
        self.store.register_review(
            'rev-a', [1, 1], self._digest(self.s0, (1, 1)))
        self.store.register_review(
            'rev-b', [5, 1], self._digest(self.s0, (5, 1)))

        receipt1 = self.store.import_delta(self.d1)
        self.assertEqual(receipt1['updated'], [[1, 1]])
        self.assertEqual(receipt1['inserted'], [])
        self.assertEqual(receipt1['revoked_review_ids'], ['rev-a'])
        history = self.store.review_history('rev-a')
        self.assertFalse(history['active'])
        self.assertEqual(history['revoked_at_version'], 'R1')
        # History keeps the registration-time provenance (old tile).
        old_sources = {tuple(e['coord']): e['source']
                       for e in history['registered_record']['pixel_sources']}
        self.assertEqual(old_sources[(2, 1)]['tile_id'], 'low')
        # Unchanged object keeps its review.
        self.assertTrue(self.store.get_review('rev-b')['active'])

        # Re-review [1,1] on R1.
        self.store.register_review(
            'rev-c', [1, 1], self._digest(self.s1, (1, 1)))
        with self.assertRaises(ReviewRejected):
            self.store.register_review(
                'rev-d', [1, 1], self._digest(self.s1, (1, 1)))

        receipt2 = self.store.import_delta(self.d2)
        self.assertEqual(receipt2['updated'], [[1, 1]])
        self.assertEqual(receipt2['inserted'], [[3, 1]])
        self.assertEqual(receipt2['revoked_review_ids'], ['rev-c'])
        self.assertTrue(self.store.get_review('rev-b')['active'])
        self.assertEqual(self.store.list_object_keys(),
                         [(1, 1), (3, 1), (5, 1)])
        # [5,1] numeric label changed but its stored record did not.
        self.assertEqual(self.store.get_object([5, 1])['pixel_sources'],
                         build_records(compile_snapshot(self.s0))[(5, 1)]
                         ['pixel_sources'])

    def test_review_rejected_for_missing_object_or_digest(self):
        self.store.import_delta(self.d0)
        with self.assertRaises(ReviewRejected):
            self.store.register_review('r1', [9, 9], '0' * 64)
        with self.assertRaises(ReviewRejected):
            self.store.register_review('r2', [1, 1], '0' * 64)
        with self.assertRaises(ReviewRejected):
            self.store.register_review(
                'r3', [1, 1], self._digest(self.s1, (1, 1)))
        with self.assertRaises(KeyError):
            self.store.get_object([9, 9])

    def test_replay_returns_original_receipt_even_after_head_advanced(self):
        self.store.import_delta(self.d0)
        self.store.register_review(
            'rev-a', [1, 1], self._digest(self.s0, (1, 1)))
        self.store.register_review(
            'rev-b', [5, 1], self._digest(self.s0, (5, 1)))
        receipt1 = self.store.import_delta(self.d1)
        self.store.register_review(
            'rev-c', [1, 1], self._digest(self.s1, (1, 1)))
        self.store.import_delta(self.d2)
        # A new review at R2 must not be touched by replaying R1.
        self.store.register_review(
            'rev-e', [3, 1], self._digest(self.s2, (3, 1)))

        replayed = self.store.import_delta(copy.deepcopy(self.d1))
        self.assertEqual(replayed, receipt1)
        self.assertEqual(replayed['revoked_review_ids'], ['rev-a'])
        self.assertEqual(self.store.current_version(), 'R2')
        self.assertTrue(self.store.get_review('rev-e')['active'])
        self.assertTrue(self.store.get_review('rev-b')['active'])

    def test_same_target_version_different_content_rejected(self):
        self.store.import_delta(self.d0)
        self.store.import_delta(self.d1)
        # A genuinely different payload that still targets version R1:
        # swap the carried record content, then re-sign every digest so the
        # envelope itself validates; only the target-version conflict fires.
        evil = copy.deepcopy(self.d1)
        record = evil['upserts'][0]['record']
        record['geometry_topology_intensity']['intensity_sum'] = 999
        evil['upserts'][0]['record_digest'] = record_digest(record)
        evil['to_manifest_digest'] = manifest_digest(
            {tuple(r['record']['key']): r['record']
             for r in evil['upserts']})
        evil['delta_digest'] = digest(
            {k: v for k, v in evil.items() if k != 'delta_digest'})
        with self.assertRaises(DeltaRejected):
            self.store.import_delta(evil)
        # Original receipt remains available and intact.
        self.assertEqual(self.store.receipt('R1')['to_version_id'], 'R1')

    def test_stale_predecessor_and_manifest_mismatch(self):
        # R2 first: predecessor R1 does not match the empty head.
        with self.assertRaises(StaleVersion):
            self.store.import_delta(self.d2)
        self.store.import_delta(self.d0)
        self.store.import_delta(self.d1)
        self.store.import_delta(self.d2)
        # An early never-imported increment whose predecessor is not the head.
        other = ReviewStore(os.path.join(self.tmp.name, 'other', 's.json'),
                            'ds-1')
        other.import_delta(self.d0)
        with self.assertRaises(StaleVersion):
            other.import_delta(self.d2)
        # Manifest digest mismatch on an otherwise chain-valid increment.
        tampered = copy.deepcopy(self.dq)
        tampered['from_manifest_digest'] = '0' * 64
        tampered['delta_digest'] = digest(
            {k: v for k, v in tampered.items() if k != 'delta_digest'})
        with self.assertRaises(StaleVersion):
            self.store.import_delta(tampered)
        # A different target version reusing R1's predecessor id is still
        # stale because the head has advanced.
        relabelled = copy.deepcopy(self.d1)
        relabelled['to_version_id'] = 'R1-prime'
        relabelled['delta_digest'] = digest(
            {k: v for k, v in relabelled.items() if k != 'delta_digest'})
        with self.assertRaises(StaleVersion):
            self.store.import_delta(relabelled)

    def test_malformed_deltas_leave_no_partial_update(self):
        self.store.import_delta(self.d0)
        snapshot_before = json.dumps(self.store.state, sort_keys=True)

        bad_digest = copy.deepcopy(self.d1)
        bad_digest['upserts'][0]['record_digest'] = '0' * 64
        with self.assertRaises(DeltaRejected):
            self.store.import_delta(bad_digest)

        delete_missing = copy.deepcopy(self.d1)
        delete_missing['deletes'] = [[7, 2]]
        with self.assertRaises(DeltaRejected):
            self.store.import_delta(delete_missing)

        no_change = copy.deepcopy(self.d1)
        no_change['to_version_id'] = 'R1b'
        record = build_records(compile_snapshot(self.s0))[(1, 1)]
        no_change['upserts'] = [{
            'key': [1, 1],
            'record_digest': record_digest(record),
            'record': record,
        }]
        no_change['to_manifest_digest'] = self.d1['from_manifest_digest']
        no_change['delta_digest'] = digest(
            {k: v for k, v in no_change.items() if k != 'delta_digest'})
        with self.assertRaises(DeltaRejected):
            self.store.import_delta(no_change)

        self.assertEqual(json.dumps(self.store.state, sort_keys=True),
                         snapshot_before)
        self.assertEqual(self.store.current_version(), 'R0')

    def test_no_change_version_advances_head_and_keeps_reviews(self):
        self.store.import_delta(self.d0)
        self.store.register_review(
            'rev-a', [1, 1], self._digest(self.s0, (1, 1)))
        s3 = copy.deepcopy(self.s0)
        s3['version_id'] = 'R3'
        d3 = build_delta(self.s0, s3)
        receipt = self.store.import_delta(d3)
        self.assertEqual(receipt['inserted'], [])
        self.assertEqual(receipt['updated'], [])
        self.assertEqual(receipt['deleted'], [])
        self.assertEqual(receipt['revoked_review_ids'], [])
        self.assertEqual(self.store.current_version(), 'R3')
        self.assertTrue(self.store.get_review('rev-a')['active'])
        # Version ids are not interchangeable: another empty increment that
        # claims R2 as its predecessor is stale once the head has advanced.
        s4 = copy.deepcopy(self.s0)
        s4['version_id'] = 'R4'
        d4 = build_delta(self.s1, s4)  # from R1, not from the current R3
        with self.assertRaises(StaleVersion):
            self.store.import_delta(d4)
        # Re-delivery of the exact R3 increment replays its stored receipt.
        self.assertEqual(self.store.import_delta(copy.deepcopy(d3)), receipt)

    def test_question_mark_then_restore_r0_never_revives_reviews(self):
        self.store.import_delta(self.d0)
        self.store.register_review(
            'rev-a', [1, 1], self._digest(self.s0, (1, 1)))
        self.store.import_delta(self.d1)
        self.store.register_review(
            'rev-c', [1, 1], self._digest(self.s1, (1, 1)))
        self.store.import_delta(self.d2)
        q_receipt = self.store.import_delta(self.dq)
        self.assertEqual(q_receipt['deleted'], [[3, 1]])
        # Restore mask and provenance exactly to R0 content under a new
        # version id (version ids are never reused with different content).
        s_restore = copy.deepcopy(self.s0)
        s_restore['version_id'] = 'R-restored'
        d_restore = build_delta(self.sq, s_restore)
        receipt = self.store.import_delta(d_restore)
        # [3,1] already left the set when the transparent '?' restored the
        # bridge; the '?' also restored [1,1] to its exact R0 record, so the
        # restore is a valid object-change-free version publication.
        self.assertEqual(receipt['deleted'], [])
        self.assertEqual(receipt['updated'], [])
        self.assertEqual(receipt['inserted'], [])
        self.assertEqual(self.store.list_object_keys(), [(1, 1), (5, 1)])
        self.assertEqual(
            self.store.get_object([1, 1])['pixel_sources'][1]['source']
            ['tile_id'], 'low')
        for review_id in ('rev-a', 'rev-c'):
            self.assertFalse(self.store.get_review(review_id)['active'])
        # Re-appearance requires a fresh review.
        with self.assertRaises(ReviewRejected):
            self.store.register_review(
                'rev-a', [1, 1], self._digest(self.s0, (1, 1)))
        self.store.register_review(
            'rev-f', [1, 1], self._digest(self.s0, (1, 1)))
        self.assertTrue(self.store.get_review('rev-f')['active'])

    def test_persistence_roundtrip_and_dataset_binding(self):
        self.store.import_delta(self.d0)
        self.store.register_review(
            'rev-a', [1, 1], self._digest(self.s0, (1, 1)))
        reopened = ReviewStore(self.path, 'ds-1')
        self.assertEqual(reopened.current_version(), 'R0')
        self.assertEqual(reopened.list_object_keys(), [(1, 1), (5, 1)])
        self.assertTrue(reopened.get_review('rev-a')['active'])
        with self.assertRaises(Exception):
            ReviewStore(self.path, 'other-dataset')

    def test_rebuild_from_saved_deltas_without_snapshots(self):
        # Simulate an offline directory that holds delta files only.
        transfer = os.path.join(self.tmp.name, 'inbox')
        os.makedirs(transfer)
        for name, payload in (('d0.json', self.d0), ('d1.json', self.d1),
                              ('d2.json', self.d2)):
            with open(os.path.join(transfer, name), 'w',
                      encoding='utf-8') as handle:
                json.dump(payload, handle, sort_keys=True)

        rebuilt_path = os.path.join(self.tmp.name, 'rebuilt', 'state.json')
        rebuilt = ReviewStore(rebuilt_path, 'ds-1')
        for name in ('d0.json', 'd1.json', 'd2.json'):
            with open(os.path.join(transfer, name),
                      encoding='utf-8') as handle:
                rebuilt.import_delta(json.load(handle))
        self.assertEqual(rebuilt.current_version(), 'R2')
        self.assertEqual(rebuilt.list_object_keys(),
                         [(1, 1), (3, 1), (5, 1)])
        self.assertEqual(rebuilt.current_manifest_digest(),
                         self.d2['to_manifest_digest'])

    def test_crash_before_commit_leaves_old_state_and_is_retryable(self):
        self.store.import_delta(self.d0)
        before_head = self.store.current_version()
        before_files = set(os.listdir(os.path.dirname(self.path)))

        with mock.patch('micro_objects.store.os.replace',
                        side_effect=RuntimeError('power loss pre-commit')):
            with self.assertRaises(RuntimeError):
                self.store.import_delta(self.d1)

        # Reopening sees only the old state.
        reopened = ReviewStore(self.path, 'ds-1')
        self.assertEqual(reopened.current_version(), before_head)
        self.assertEqual(reopened.receipt('R1'), None)
        leftovers = set(os.listdir(os.path.dirname(self.path))) - before_files
        self.assertEqual(leftovers, set())
        # Retry succeeds exactly once.
        receipt = reopened.import_delta(copy.deepcopy(self.d1))
        self.assertEqual(receipt['to_version_id'], 'R1')
        self.assertEqual(receipt['revoked_review_ids'], [])

    def test_crash_after_commit_before_receipt_is_idempotent(self):
        path = os.path.join(self.tmp.name, 'crash-after', 's.json')
        store = ReviewStore(path, 'ds-1')
        store.import_delta(self.d0)
        store.register_review(
            'rev-a', [1, 1], self._digest(self.s0, (1, 1)))
        real_replace = os.replace
        calls = {'count': 0}

        def flaky(src, dst):
            real_replace(src, dst)
            calls['count'] += 1
            raise RuntimeError('power loss post-commit')

        with mock.patch('micro_objects.store.os.replace', side_effect=flaky):
            with self.assertRaises(StoreError):
                store.import_delta(self.d1)

        # The commit landed: reopening must recognize the finished import.
        reopened = ReviewStore(path, 'ds-1')
        self.assertEqual(reopened.current_version(), 'R1')
        stored = reopened.receipt('R1')
        self.assertEqual(stored['revoked_review_ids'], ['rev-a'])
        # Retry on a fresh instance is a replay: no second revocation.
        retry_store = ReviewStore(path, 'ds-1')
        retry = retry_store.import_delta(copy.deepcopy(self.d1))
        self.assertEqual(retry, stored)
        self.assertEqual(calls['count'], 1)
        self.assertEqual(
            retry_store.get_review('rev-a')['revoked_at_version'], 'R1')

    def test_same_instance_post_commit_interruption_retries_as_replay(self):
        path = os.path.join(self.tmp.name, 'crash-same', 's.json')
        store = ReviewStore(path, 'ds-1')
        store.import_delta(self.d0)
        store.register_review(
            'rev-a', [1, 1], self._digest(self.s0, (1, 1)))
        real_replace = os.replace

        def flaky(src, dst):
            real_replace(src, dst)
            raise RuntimeError('power loss post-commit')

        with mock.patch('micro_objects.store.os.replace', side_effect=flaky):
            with self.assertRaises(StoreError):
                store.import_delta(self.d1)
        # No reopening: in-memory state already follows the durable commit
        # and the immediate retry returns the stored receipt without a
        # second revocation.
        receipt = store.import_delta(copy.deepcopy(self.d1))
        self.assertEqual(receipt['to_version_id'], 'R1')
        self.assertEqual(receipt['revoked_review_ids'], ['rev-a'])
        self.assertEqual(store.current_version(), 'R1')
        review = store.get_review('rev-a')
        self.assertFalse(review['active'])
        self.assertEqual(review['revoked_at_version'], 'R1')


if __name__ == '__main__':
    unittest.main()
