"""Offline downstream review store.

The store is a single-writer, file-backed local library.  Importing an
increment never reads tiles and never contacts an upstream: only the
self-contained delta document and the local state are used.

All state lives in one JSON file.  A commit is a single atomic
``os.replace`` of a fsynced temp file, so an interrupted process either
leaves the previous state untouched or the fully committed new state;
retries never produce partial updates or double revocations.
"""
import copy
import json
import os
import tempfile

from .delta import (EMPTY_DIGEST, DeltaError, manifest_digest, parse_delta,
                    record_digest)

STATE_FORMAT = 'micro-objects-review-store/1'
_ID_CHARS = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-')


class StoreError(ValueError):
    """Base class for every store-level rejection."""


class DatasetMismatch(StoreError):
    """The increment belongs to another dataset."""


class StaleVersion(StoreError):
    """The increment's predecessor does not match the current head."""


class DeltaRejected(StoreError):
    """The increment is malformed, conflicting or redundant."""


class ReviewRejected(StoreError):
    """A review cannot be registered in the current state."""


def _valid_id(value, name):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 \
            or any(ch not in _ID_CHARS for ch in value):
        raise StoreError(f'invalid {name}')
    return value


def _key_tuple(key):
    if not isinstance(key, list) or len(key) != 2 \
            or type(key[0]) is not int or type(key[1]) is not int \
            or key[0] < 0 or key[1] < 0:
        raise StoreError('object key must be two non-negative integers')
    return key[0], key[1]


def _store_key(key):
    """JSON object keys are strings; keep one canonical textual form."""
    return json.dumps(list(key), separators=(',', ':'))


def _from_store_key(text):
    return tuple(json.loads(text))


class ReviewStore:
    """Single-writer offline review database at *path*."""

    def __init__(self, path, dataset_id):
        _valid_id(dataset_id, 'dataset_id')
        self.path = os.fspath(path)
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as handle:
                    state = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                raise StoreError(f'store file is not readable JSON: {exc}') \
                    from exc
            required = {'format', 'dataset_id', 'current_version',
                        'manifest_digest', 'objects', 'reviews', 'receipts'}
            if not isinstance(state, dict) or set(state) != required:
                raise StoreError('store file has an unexpected shape')
            if state.get('format') != STATE_FORMAT:
                raise StoreError('unknown store file format')
            if state['dataset_id'] != dataset_id:
                raise DatasetMismatch(
                    f"store is bound to {state['dataset_id']!r}, "
                    f'not {dataset_id!r}')
            self.state = state
        else:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            self.state = {
                'format': STATE_FORMAT,
                'dataset_id': dataset_id,
                'current_version': None,
                'manifest_digest': EMPTY_DIGEST,
                'objects': {},
                'reviews': {},
                'receipts': {},
            }
            self._commit()

    # -- persistence -------------------------------------------------------

    def _commit(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        tmp = tempfile.NamedTemporaryFile(
            dir=directory, prefix='.state-', suffix='.tmp',
            delete=False, mode='w', encoding='utf-8')
        try:
            json.dump(self.state, tmp, ensure_ascii=False, sort_keys=True,
                      indent=2, allow_nan=False)
            tmp.write('\n')
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self.path)
        except BaseException:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except FileNotFoundError:
                pass
            raise

    def _reload_committed(self, version_id, delta_digest_value):
        """Recover in-memory state after a commit-time interruption.

        Returns the persisted receipt when the replacement had actually
        landed (post-commit interruption), otherwise ``None``.  This keeps
        retries idempotent even without reopening the store.
        """
        try:
            with open(self.path, 'r', encoding='utf-8') as handle:
                disk = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        receipt = disk.get('receipts', {}).get(version_id)
        if isinstance(disk, dict) and isinstance(receipt, dict) \
                and receipt.get('delta_digest') == delta_digest_value:
            self.state = disk
            return copy.deepcopy(receipt)
        return None

    # -- read side ---------------------------------------------------------

    @property
    def dataset_id(self):
        return self.state['dataset_id']

    def current_version(self):
        """Return the head version id, or ``None`` before the first import."""
        return self.state['current_version']

    def current_manifest_digest(self):
        return self.state['manifest_digest']

    def list_object_keys(self):
        """All delivered object keys, sorted in whole-image raster order."""
        return sorted((_from_store_key(key)
                       for key in self.state['objects']),
                      key=lambda k: (k[1], k[0]))

    def get_object(self, key):
        record = self.state['objects'].get(_store_key(_key_tuple(key)))
        if record is None:
            raise KeyError(key)
        return copy.deepcopy(record)

    def has_object(self, key):
        return _store_key(_key_tuple(key)) in self.state['objects']

    def list_reviews(self, include_revoked=False):
        entries = [copy.deepcopy(e) for e in self.state['reviews'].values()
                   if include_revoked or e['active']]
        return sorted(entries, key=lambda e: e['review_id'])

    def get_review(self, review_id):
        entry = self.state['reviews'].get(review_id)
        if entry is None:
            raise KeyError(review_id)
        return copy.deepcopy(entry)

    def review_history(self, review_id):
        """Return the review entry including its registration-time record."""
        return self.get_review(review_id)

    def receipt(self, version_id):
        """Return the persisted receipt of a successful import, if any."""
        entry = self.state['receipts'].get(version_id)
        return copy.deepcopy(entry) if entry is not None else None

    # -- reviews -----------------------------------------------------------

    def register_review(self, review_id, key, record_digest_value):
        """Register a downstream review against a delivered object record."""
        _valid_id(review_id, 'review_id')
        if review_id in self.state['reviews']:
            raise ReviewRejected(f'review id {review_id!r} already exists')
        key = _key_tuple(key)
        record = self.state['objects'].get(_store_key(key))
        if record is None:
            raise ReviewRejected('object is not in the delivery set')
        if not isinstance(record_digest_value, str) \
                or record_digest(record) != record_digest_value:
            raise ReviewRejected('record digest does not match')
        for entry in self.state['reviews'].values():
            if entry['active'] and tuple(entry['key']) == key:
                raise ReviewRejected('object already has an active review')
        self.state['reviews'][review_id] = {
            'review_id': review_id,
            'key': list(key),
            'record_digest': record_digest_value,
            'version_id': self.state['current_version'],
            'active': True,
            'revoked_at_version': None,
            'registered_record': copy.deepcopy(record),
        }
        self._commit()
        return self.get_review(review_id)

    # -- import ------------------------------------------------------------

    def import_delta(self, payload):
        """Validate and apply *payload*; replay-safe and atomic.

        A re-delivery of an already imported target version always returns
        the original receipt (including originally revoked reviews), even
        when the head has since advanced; replay is resolved before any
        staleness check.  A different payload for the same target version is
        rejected and never overwrites the stored receipt.
        """
        try:
            delta = parse_delta(payload)
        except DeltaError as exc:
            raise DeltaRejected(str(exc)) from exc

        if delta['dataset_id'] != self.state['dataset_id']:
            raise DatasetMismatch(
                f"delta dataset {delta['dataset_id']!r} does not match store "
                f"{self.state['dataset_id']!r}")

        existing = self.state['receipts'].get(delta['to_version_id'])
        if existing is not None:
            if existing['delta_digest'] != delta['delta_digest']:
                raise DeltaRejected(
                    'different delta content for an already imported version')
            return copy.deepcopy(existing)

        # Not a replay: the chain must land exactly on the current head.
        if delta['from_version_id'] != self.state['current_version']:
            raise StaleVersion(
                f"predecessor {delta['from_version_id']!r} does not match "
                f"current head {self.state['current_version']!r}")
        if delta['from_manifest_digest'] != self.state['manifest_digest']:
            raise StaleVersion('predecessor manifest digest does not match')

        objects = self.state['objects']
        inserted, updated, deleted = [], [], []
        delete_keys = set()
        for record in delta['upserts']:
            key = tuple(record['key'])
            text = _store_key(key)
            if text not in objects:
                inserted.append(list(key))
            elif record_digest(objects[text]) == record_digest(record):
                raise DeltaRejected(
                    f'no-change replacement for key {list(key)} is not a delta')
            else:
                updated.append(list(key))
        for key_tuple in delta['deletes']:
            text = _store_key(key_tuple)
            if text not in objects:
                raise DeltaRejected(
                    f'cannot delete missing key {list(key_tuple)}')
            if key_tuple in delete_keys:
                raise DeltaRejected('duplicate or conflicting delta entry')
            delete_keys.add(key_tuple)
            deleted.append(list(key_tuple))
        upsert_keys = {tuple(r['key']) for r in delta['upserts']}
        if upsert_keys & delete_keys:
            raise DeltaRejected('duplicate or conflicting delta entry')

        # Apply on an in-memory copy first; only a clean run commits.
        new_objects = copy.deepcopy(objects)
        for record in delta['upserts']:
            new_objects[_store_key(tuple(record['key']))] = \
                copy.deepcopy(record)
        for key_tuple in delta['deletes']:
            del new_objects[_store_key(key_tuple)]
        if manifest_digest({_from_store_key(text): value
                            for text, value in new_objects.items()}) \
                != delta['to_manifest_digest']:
            raise DeltaRejected(
                'post-import manifest digest does not match the delta')

        revoked = []
        new_reviews = copy.deepcopy(self.state['reviews'])
        touched = upsert_keys | delete_keys
        for review_id, entry in new_reviews.items():
            if entry['active'] and tuple(entry['key']) in touched:
                entry['active'] = False
                entry['revoked_at_version'] = delta['to_version_id']
                revoked.append(review_id)

        receipt = {
            'dataset_id': delta['dataset_id'],
            'from_version_id': delta['from_version_id'],
            'to_version_id': delta['to_version_id'],
            'inserted': sorted(inserted, key=lambda k: (k[1], k[0])),
            'updated': sorted(updated, key=lambda k: (k[1], k[0])),
            'deleted': sorted(deleted, key=lambda k: (k[1], k[0])),
            'revoked_review_ids': sorted(revoked),
            'delta_digest': delta['delta_digest'],
        }
        new_state = copy.deepcopy(self.state)
        new_state['objects'] = new_objects
        new_state['reviews'] = new_reviews
        new_state['current_version'] = delta['to_version_id']
        new_state['manifest_digest'] = delta['to_manifest_digest']
        new_state['receipts'][delta['to_version_id']] = receipt
        previous = self.state
        self.state = new_state
        try:
            self._commit()
        except BaseException as exc:
            # The replacement may still have landed: adopt the durable truth
            # on disk so a same-process retry behaves like a replay, then
            # surface the interruption because no receipt was delivered.
            landed = self._reload_committed(delta['to_version_id'],
                                            delta['delta_digest'])
            if landed is not None:
                raise StoreError(
                    'commit landed before the interruption; retry to obtain '
                    'the stored receipt') from exc
            self.state = previous
            raise
        return copy.deepcopy(receipt)
