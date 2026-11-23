"""Deterministic object records, content digests and offline deltas.

Encoding contract
-----------------
Every digest is computed from a canonical JSON document:

* ``json.dumps(payload, ensure_ascii=False, sort_keys=True,
  separators=(',', ':'), allow_nan=False)`` encoded as UTF-8;
* SHA-256 of those bytes, rendered as 64 lowercase hex characters.

Canonical JSON never contains the ephemeral numeric component labels:
object identity is the raster-first foreground pixel ``[x, y]``.  Equal
pixel sizes, tile input order and numeric labels therefore cannot change
any digest.
"""
import copy
import hashlib
import json

from .tiles import compile_snapshot

FORMAT_ID = 'micro-objects-delta/1'


class DeltaError(ValueError):
    """Raised when an increment document fails structural validation."""


def canonical_bytes(payload):
    """Return the canonical UTF-8 JSON bytes used for every digest."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(payload):
    """SHA-256 hex digest of the canonical encoding of *payload*."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


# The empty delivery list is still an (empty) canonical JSON list.
EMPTY_DIGEST = digest([])


def build_records(compiled):
    """Build the delivery records for one compiled snapshot.

    Only objects passing the original selection are delivered.  The record
    key is the first foreground pixel of the object in whole-image raster
    order; the record itself carries no temporary numeric label.
    """
    result = compiled['result']
    sources = compiled['sources']
    intensity = compiled['intensity']
    records = {}
    for obj in result['objects']:
        if not obj['accepted']:
            continue
        pixels = [list(p) for p in obj['pixels']]
        key = [pixels[0][0], pixels[0][1]]
        measure = {name: copy.deepcopy(obj[name]) for name in (
            'pixel_count', 'bbox', 'area', 'edge_perimeter', 'centroid',
            'intensity_sum', 'mean_intensity', 'holes', 'touches_border')}
        pixel_sources = [
            {'coord': [x, y],
             'intensity': intensity[y][x],
             'source': sources[y][x]}
            for x, y in pixels
        ]
        record = {
            'key': key,
            'pixel_size': list(compiled['pixel_size']),
            'connectivity': compiled['connectivity'],
            'pixels': pixels,
            'geometry_topology_intensity': measure,
            'pixel_sources': pixel_sources,
        }
        records[tuple(key)] = record
    return records


def record_digest(record):
    """Digest of one delivery record (the record contains no labels)."""
    return digest(record)


def manifest_payload(records):
    """Canonical manifest payload: keys sorted by raster order."""
    ordered = sorted(records.values(),
                     key=lambda item: (item['key'][1], item['key'][0]))
    return [{'key': rec['key'], 'record_digest': record_digest(rec)}
            for rec in ordered]


def manifest_digest(records):
    """Digest of the complete delivery set of one version."""
    return digest(manifest_payload(records))


def _change_set(records):
    return [{'key': rec['key'], 'record_digest': record_digest(rec),
             'record': rec}
            for rec in sorted(records,
                              key=lambda item: (item['key'][1],
                                                item['key'][0]))]


def build_delta(previous, current):
    """Build a self-contained increment between two snapshots.

    *previous* is ``None`` for the first publication.  Each snapshot may be
    a raw snapshot (compiled here) or an already compiled canvas.
    """
    new = compile_snapshot(current) if 'result' not in current else current
    if previous is None:
        old_records = {}
        prev_version = None
    else:
        old = compile_snapshot(previous) if 'result' not in previous else previous
        if old['dataset_id'] != new['dataset_id']:
            raise ValueError('deltas only compare snapshots of one dataset')
        prev_version = old['version_id']
        old_records = build_records(old)
    new_records = build_records(new)

    upserts, deletes = [], []
    for key in sorted(set(old_records) | set(new_records),
                      key=lambda k: (k[1], k[0])):
        if key in new_records and key not in old_records:
            upserts.append(new_records[key])
        elif key in old_records and key not in new_records:
            deletes.append(list(key))
        elif record_digest(old_records[key]) != record_digest(new_records[key]):
            upserts.append(new_records[key])
        # Same key and identical content: nothing is delivered.

    payload = {
        'format': FORMAT_ID,
        'dataset_id': new['dataset_id'],
        'from_version_id': prev_version,
        'to_version_id': new['version_id'],
        'from_manifest_digest': manifest_digest(old_records),
        'to_manifest_digest': manifest_digest(new_records),
        'upserts': _change_set(upserts),
        'deletes': deletes,
    }
    payload['delta_digest'] = digest(
        {k: v for k, v in payload.items() if k != 'delta_digest'})
    return payload


def _require(value, predicate, message):
    if not predicate(value):
        raise DeltaError(message)


def _validate_key(value):
    _require(value, lambda v: isinstance(v, list) and len(v) == 2
             and type(v[0]) is int and type(v[1]) is int and v[0] >= 0
             and v[1] >= 0, 'object key must be two non-negative integers')
    return value[0], value[1]


def _validate_record(record):
    _require(record, lambda v: isinstance(v, dict), 'record must be an object')
    key = tuple(_validate_key(record['key']))
    _require(record['pixel_size'],
             lambda v: isinstance(v, list) and len(v) == 2
             and all(isinstance(s, str) for s in v),
             'record pixel_size must be two decimal strings')
    _require(record['connectivity'],
             lambda v: type(v) is int and v in (4, 8),
             'record connectivity must be 4 or 8')
    pixels = record['pixels']
    _require(pixels, lambda v: isinstance(v, list) and v
             and all(isinstance(p, list) and len(p) == 2
                     and type(p[0]) is int and type(p[1]) is int
                     for p in v),
             'record pixels must be a non-empty [x, y] list')
    ordered = sorted((tuple(p) for p in pixels), key=lambda p: (p[1], p[0]))
    if list(ordered[0]) != list(key):
        raise DeltaError('record key must be the raster-first foreground pixel')
    if len(set(ordered)) != len(ordered):
        raise DeltaError('record pixels contain duplicates')
    sources = record['pixel_sources']
    _require(sources, lambda v: isinstance(v, list)
             and len(v) == len(ordered),
             'pixel_sources must align with pixels one to one')
    for pixel, entry in zip(ordered, sorted(
            sources, key=lambda e: (e['coord'][1], e['coord'][0]))):
        _require(entry, lambda v: isinstance(v, dict)
                 and set(v) == {'coord', 'intensity', 'source'},
                 'pixel source entry requires coord, intensity and source')
        if tuple(entry['coord']) != pixel:
            raise DeltaError('pixel source coordinates must match pixels')
        _strict_int(entry['intensity'], 0, 65535)
        src = entry['source']
        _require(src, lambda v: isinstance(v, dict) and set(v) ==
                 {'tile_id', 'tile_coord'}, 'pixel source requires a tile id')
        _require(src['tile_id'], lambda v: isinstance(v, str)
                 and 1 <= len(v) <= 64, 'invalid source tile id')
        _require(src['tile_coord'], lambda v: isinstance(v, list) and len(v) == 2
                 and type(v[0]) is int and type(v[1]) is int
                 and v[0] >= 0 and v[1] >= 0, 'invalid tile coordinate')
    metric_names = {'pixel_count', 'bbox', 'area', 'edge_perimeter',
                    'centroid', 'intensity_sum', 'mean_intensity', 'holes',
                    'touches_border'}
    _require(record['geometry_topology_intensity'],
             lambda v: isinstance(v, dict) and set(v) == metric_names,
             'record metric block has an unexpected shape')
    return key, record


def _strict_int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise DeltaError(f'integer {low}..{high} required')


_ID_CHARS = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-')


def _validate_id(value, name):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 \
            or any(ch not in _ID_CHARS for ch in value):
        raise DeltaError(f'invalid {name}')
    return value


def parse_delta(payload):
    """Validate a loaded increment and return its normalized view.

    Structural, identity, change-set and embedded digest consistency are all
    checked here so :class:`micro_objects.store.ReviewStore` never has to
    trust bytes from the offline transfer.
    """
    _require(payload, lambda v: isinstance(v, dict), 'delta must be an object')
    expected_fields = {'format', 'dataset_id', 'from_version_id',
                       'to_version_id', 'from_manifest_digest',
                       'to_manifest_digest', 'upserts', 'deletes',
                       'delta_digest'}
    if set(payload) != expected_fields:
        raise DeltaError(f'delta requires fields {sorted(expected_fields)}')
    if payload['format'] != FORMAT_ID:
        raise DeltaError('unknown delta format')
    dataset_id = _validate_id(payload['dataset_id'], 'dataset_id')
    to_version = _validate_id(payload['to_version_id'], 'to_version_id')
    if payload['from_version_id'] is not None:
        _validate_id(payload['from_version_id'], 'from_version_id')
    for name in ('from_manifest_digest', 'to_manifest_digest', 'delta_digest'):
        value = payload[name]
        _require(value, lambda v: isinstance(v, str)
                 and len(v) == 64
                 and all(ch in '0123456789abcdef' for ch in v),
                 f'{name} must be a sha256 hex string')
    if digest({k: v for k, v in payload.items() if k != 'delta_digest'}) \
            != payload['delta_digest']:
        raise DeltaError('delta digest does not match its content')

    upserts, upsert_keys = [], set()
    for entry in payload['upserts']:
        _require(entry, lambda v: isinstance(v, dict)
                 and set(v) == {'key', 'record_digest', 'record'},
                 'upsert requires key, record_digest and record')
        key, record = _validate_record(entry['record'])
        if list(key) != entry['key']:
            raise DeltaError('upsert key does not match its record')
        if record_digest(record) != entry['record_digest']:
            raise DeltaError('record digest does not match its record')
        if key in upsert_keys:
            raise DeltaError('duplicate or conflicting delta entry')
        upsert_keys.add(key)
        upserts.append(record)

    deletes = []
    for key in payload['deletes']:
        parsed = _validate_key(key)
        if parsed in upsert_keys or parsed in deletes:
            raise DeltaError('duplicate or conflicting delta entry')
        deletes.append(parsed)

    if payload['from_version_id'] is None:
        # First publication replaces the empty delivery set, so the carried
        # upserts are the complete target manifest and can be fully checked
        # without any upstream state.  Incremental manifests are verified by
        # the store after applying the changes to its local head.
        if deletes:
            raise DeltaError('the first publication cannot delete keys')
        if digest(manifest_payload({tuple(r['key']): r for r in upserts})) \
                != payload['to_manifest_digest']:
            raise DeltaError('to-manifest digest does not match the records')
        if payload['from_manifest_digest'] != EMPTY_DIGEST:
            raise DeltaError('first publication must start from the empty list')
    return {
        'format': FORMAT_ID,
        'dataset_id': dataset_id,
        'from_version_id': payload['from_version_id'],
        'to_version_id': to_version,
        'from_manifest_digest': payload['from_manifest_digest'],
        'to_manifest_digest': payload['to_manifest_digest'],
        'upserts': upserts,
        'deletes': deletes,
        'delta_digest': payload['delta_digest'],
    }
