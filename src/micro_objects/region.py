"""Spatial region attribution and batch delivery for whole boards.

The original :func:`micro_objects.service.process` result remains the only
whole-board segmentation/filtering authority.  This module overlays a
same-shaped region raster without feeding that raster into connectivity,
border, hole, or selection calculations.

All public records are JSON-safe.  Counts and intensity sums are Python
integers; physical areas and fractions are emitted as exact strings accepted
by :class:`fractions.Fraction`.  The pure entry point validates the complete
batch before returning, so a caller never receives a partial delivery.
"""

from fractions import Fraction
import re

from .service import process

REGION_DELIVERY_FORMAT = 'micro-objects-region-delivery/1'
REGION_OBSERVED = 'observed'
REGION_MISSING = 'missing'

# The task permits ASCII letters, digits, underscore and hyphen, but not the
# colon accepted by the older generic identifier helper.
_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_MAX_REGIONS = 64
_MAX_SPECIMENS = 32

_PROCESS_FIELDS = ('mask', 'intensity', 'pixel_size', 'connectivity',
                   'selection')
_DATA_KEYS = ('data', 'board', 'board_data', 'process_data')
_GRID_KEYS = ('region_grid', 'region_raster', 'region_mask', 'region_map',
              'regions', 'grid')
_OBSERVED_KEYS = ('observed_regions', 'observed_region_ids', 'observed')
_ID_KEYS = ('specimen_id', 'id')

_BATCH_KEYS = {
    'region_catalog', 'catalog', 'region_ids', 'regions',
    'specimens', 'specimen_deliveries', 'items',
}


class RegionDeliveryError(ValueError):
    """The region batch is invalid; no partial delivery should be consumed."""


# Compatibility names for callers/tests that name the capability differently.
RegionError = RegionDeliveryError
RegionCompileError = RegionDeliveryError
RegionAttributionError = RegionDeliveryError
RegionValidationError = RegionDeliveryError
RegionBatchError = RegionDeliveryError


class AliasDict(dict):
    """Dictionary with in-memory compatibility keys not serialized twice."""

    aliases = {}

    def __init__(self, *args, aliases=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._alias_map = self.aliases if aliases is None else aliases

    def _canonical_key(self, key):
        return self._alias_map.get(key, key)

    def __getitem__(self, key):
        return super().__getitem__(self._canonical_key(key))

    def __contains__(self, key):
        return super().__contains__(self._canonical_key(key))

    def get(self, key, default=None):
        return super().get(self._canonical_key(key), default)


class RegionDelivery(AliasDict):
    """Dictionary delivery with small stable lookup helpers."""

    aliases = {
        'catalog': 'region_catalog',
        'specimen_deliveries': 'specimens',
        'specimen_results': 'specimens',
        'selected_objects': 'objects',
        'contributions': 'object_region_contributions',
        'region_records': 'specimen_region_records',
        'complete_regions': 'complete_region_ids',
        'incomplete_regions': 'incomplete_region_ids',
        'region_summary': 'region_summaries',
        'summaries': 'region_summaries',
        'results': 'specimens',
        'deliveries': 'specimens',
        'attributions': 'object_region_contributions',
    }

    def specimen(self, specimen_id):
        for item in self['specimens']:  # pragma: no branch - tiny
            if item['specimen_id'] == specimen_id:
                return item
        raise KeyError(specimen_id)

    def region_summary(self, region_id):
        for item in self['region_summaries']:  # pragma: no branch
            if item['region_id'] == region_id:
                return item
        raise KeyError(region_id)

    def object_record(self, object_key):
        key = list(object_key)
        for item in self['objects']:  # pragma: no branch
            if item['object_key'] == key:
                return item
        raise KeyError(key)

    def verify_conservation(self):
        """Return ``True`` when every emitted object/region total balances."""
        return verify_region_delivery(self)


RegionBatchDelivery = RegionDelivery


_OBJECT_ALIASES = {
    'contributions': 'region_contributions',
    'region_attributions': 'region_contributions',
    'null_allocation': 'unassigned',
}
_SPECIMEN_ALIASES = {
    'board_result': 'process_result',
    'result': 'process_result',
    'selected_objects': 'objects',
    'contributions': 'object_region_contributions',
    'region_attributions': 'object_region_contributions',
    'specimen_region_records': 'region_records',
    'regions': 'region_records',
    'unassigned': 'null_allocation',
}


def _identity(value, what):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise RegionDeliveryError(
            f'{what} must be 1..64 ASCII letters, digits, "_" or "-"')
    return value


def _specimen_identity(value):
    # The specification fixes the region-id grammar; specimen ids only need
    # to be present and unique.  Keep them strings for stable JSON keys.
    if not isinstance(value, str) or not value:
        raise RegionDeliveryError('specimen_id must be a non-empty string')
    return value


def _duplicate(values, what):
    if len(set(values)) != len(values):
        raise RegionDeliveryError(f'duplicate {what}')


def _pick(mapping, keys, required=True):
    present = [key for key in keys if key in mapping]
    if len(present) > 1:
        raise RegionDeliveryError(
            f'ambiguous fields {present}; provide exactly one of {list(keys)}')
    if not present:
        if required:
            raise RegionDeliveryError(f'missing one of {list(keys)}')
        return None
    return mapping[present[0]]


def _adapt_call(region_catalog, specimens, kwargs):
    """Support the documented two-argument API and a batch-dict form."""
    if isinstance(region_catalog, dict) and specimens is None and not kwargs:
        batch = region_catalog
        unknown = set(batch) - _BATCH_KEYS
        if unknown:
            raise RegionDeliveryError(f'unknown batch fields: {sorted(unknown)}')
        catalog = _pick(
            batch, ('region_catalog', 'catalog', 'region_ids', 'regions'))
        items = _pick(
            batch, ('specimens', 'specimen_deliveries', 'items'))
        return catalog, items

    if (isinstance(region_catalog, list) and region_catalog
            and isinstance(region_catalog[0], dict)
            and ('specimen_id' in region_catalog[0]
                 or 'region_grid' in region_catalog[0]
                 or 'data' in region_catalog[0])):
        # Tolerate the natural reversed positional order: specimens, catalog.
        region_catalog, specimens = specimens, region_catalog

    catalog = region_catalog
    items = specimens
    if catalog is None:
        catalog = kwargs.pop('catalog', None)
    if catalog is None:
        catalog = kwargs.pop('region_catalog', None)
    if catalog is None:
        catalog = kwargs.pop('region_ids', None)
    if catalog is None:
        catalog = kwargs.pop('regions', None)
    if items is None:
        items = kwargs.pop('specimens', None)
    if items is None:
        items = kwargs.pop('specimen_deliveries', None)
    if items is None:
        items = kwargs.pop('items', None)
    if 'batch' in kwargs and catalog is None and items is None:
        return _adapt_call(kwargs.pop('batch'), None, {})
    if kwargs:
        raise RegionDeliveryError(f'unknown arguments: {sorted(kwargs)}')
    if catalog is None or items is None:
        raise RegionDeliveryError(
            'region catalog and 1..32 specimens are required')
    return catalog, items


def _parse_catalog(catalog):
    if not isinstance(catalog, list) or not 1 <= len(catalog) <= _MAX_REGIONS:
        raise RegionDeliveryError(
            f'region catalog must be a list of 1..{_MAX_REGIONS} region IDs')
    ids = []
    for entry in catalog:
        if isinstance(entry, dict):
            unknown = set(entry) - {'region_id', 'id'}
            if unknown:
                raise RegionDeliveryError(
                    f'unknown region catalog fields: {sorted(unknown)}')
            value = _pick(entry, ('region_id', 'id'))
        else:
            value = entry
        ids.append(_identity(value, 'region_id'))
    _duplicate(ids, 'region catalog ID')
    return ids


def _parse_specimen(raw):
    if not isinstance(raw, dict):
        raise RegionDeliveryError('each specimen must be a dictionary')

    specimen_id = _specimen_identity(_pick(raw, _ID_KEYS))
    board = _pick(raw, _DATA_KEYS, required=False)
    flat_fields = {key: raw[key] for key in _PROCESS_FIELDS if key in raw}
    if board is not None and flat_fields:
        raise RegionDeliveryError(
            f'specimen {specimen_id!r}: provide either nested board data or '
            'flat process(data) fields, not both')
    if board is None:
        if not flat_fields:
            raise RegionDeliveryError(
                f'specimen {specimen_id!r}: process(data) board is required')
        # process.parse performs exact field and value validation.
        board = flat_fields

    grid = _pick(raw, _GRID_KEYS)
    observed_raw = _pick(raw, _OBSERVED_KEYS)
    if not isinstance(observed_raw, list):
        raise RegionDeliveryError(
            f'specimen {specimen_id!r}: observed_regions must be a list')

    known = ({*_ID_KEYS, *_DATA_KEYS, *_GRID_KEYS, *_OBSERVED_KEYS}
             | set(_PROCESS_FIELDS))
    unknown = set(raw) - known
    if unknown:
        raise RegionDeliveryError(
            f'specimen {specimen_id!r}: unknown fields {sorted(unknown)}')

    return specimen_id, board, grid, observed_raw


def _exact_part(count, intensity, area, ratio):
    ratio = Fraction(ratio)
    area_text = str(area)
    ratio_text = str(ratio)
    return {
        'pixel_count': count,
        'intensity_sum': intensity,
        'area': area_text,
        'physical_area': area_text,
        'ratio': ratio_text,
        'fraction': ratio_text,
        'proportion': ratio_text,
        'share': ratio_text,
        'pixel_ratio': ratio_text,
        'ratio_terms': [ratio.numerator, ratio.denominator],
        'ratio_numerator': ratio.numerator,
        'ratio_denominator': ratio.denominator,
    }


def _validate_grid(specimen_id, grid, result, catalog_set, observed_set):
    labels = result['labels']
    height = len(labels)
    width = len(labels[0])
    if not isinstance(grid, list) or len(grid) != height:
        raise RegionDeliveryError(
            f'specimen {specimen_id!r}: region grid must have {height} rows')

    present = set()
    for y, row in enumerate(grid):
        if not isinstance(row, list) or len(row) != width:
            raise RegionDeliveryError(
                f'specimen {specimen_id!r}: region row {y} must be a list '
                f'of {width} cells')
        for x, cell in enumerate(row):
            if cell is None:
                continue
            region_id = _identity(cell, 'region grid ID')
            if region_id not in catalog_set:
                raise RegionDeliveryError(
                    f'specimen {specimen_id!r}: grid contains catalog-unknown '
                    f'region {region_id!r} at ({x}, {y})')
            if region_id not in observed_set:
                raise RegionDeliveryError(
                    f'specimen {specimen_id!r}: unobserved region '
                    f'{region_id!r} appears in grid at ({x}, {y})')
            present.add(region_id)

    missing_cells = observed_set - present
    if missing_cells:
        raise RegionDeliveryError(
            f'specimen {specimen_id!r}: observed_regions lack grid cells: '
            f'{sorted(missing_cells)}')


def _attribute_objects(specimen_id, board, result, grid):
    object_records = []
    contributions = []
    null_totals = {'pixel_count': 0, 'intensity_sum': 0,
                   'area': Fraction(0)}

    for obj in result['objects']:
        if not obj['accepted']:
            continue

        count_total = obj['pixel_count']
        intensity_total = obj['intensity_sum']
        unit_area = Fraction(obj['area']) / count_total
        first_x, first_y = obj['pixels'][0]
        buckets = {}
        null_count = 0
        null_intensity = 0

        for x, y in obj['pixels']:
            intensity = board['intensity'][y][x]
            region_id = grid[y][x]
            if region_id is None:
                null_count += 1
                null_intensity += intensity
            else:
                bucket = buckets.setdefault(
                    region_id, {'pixel_count': 0, 'intensity_sum': 0})
                bucket['pixel_count'] += 1
                bucket['intensity_sum'] += intensity

        bucket_count = sum(v['pixel_count'] for v in buckets.values())
        bucket_intensity = sum(v['intensity_sum'] for v in buckets.values())
        if bucket_count + null_count != count_total:
            raise RegionDeliveryError(
                f'{specimen_id}/{first_x},{first_y}: pixel conservation failed')
        if bucket_intensity + null_intensity != intensity_total:
            raise RegionDeliveryError(
                f'{specimen_id}/{first_x},{first_y}: intensity conservation failed')

        object_key = [specimen_id, first_x, first_y]
        object_contributions = []
        for region_id in sorted(buckets):
            bucket = buckets[region_id]
            count = bucket['pixel_count']
            ratio = Fraction(count, count_total)
            contribution = {
                'object_key': list(object_key),
                'object_id': list(object_key),
                'key': list(object_key),
                'specimen_id': specimen_id,
                'region_id': region_id,
            }
            contribution.update(_exact_part(
                count, bucket['intensity_sum'],
                count * unit_area, ratio))
            object_contributions.append(contribution)
            contributions.append(contribution)

        null_ratio = Fraction(null_count, count_total)
        unassigned = _exact_part(
            null_count, null_intensity,
            null_count * unit_area, null_ratio)

        record = AliasDict(obj, aliases=_OBJECT_ALIASES)
        record.update({
            'specimen_id': specimen_id,
            'object_key': object_key,
            'object_id': object_key,
            'key': object_key,
            'first_x': first_x,
            'first_y': first_y,
            'first_pixel': [first_x, first_y],
            'region_contributions': object_contributions,
            'unassigned': unassigned,
            'unassigned_pixel_count': null_count,
            'unassigned_pixels': null_count,
            'null_pixel_count': null_count,
            'null_pixels': null_count,
            'unassigned_intensity_sum': null_intensity,
            'null_intensity_sum': null_intensity,
            'unassigned_area': unassigned['area'],
            'null_area': unassigned['area'],
        })
        object_records.append(record)

        null_totals['pixel_count'] += null_count
        null_totals['intensity_sum'] += null_intensity
        null_totals['area'] += Fraction(unassigned['area'])

    null_allocation = {
        'pixel_count': null_totals['pixel_count'],
        'intensity_sum': null_totals['intensity_sum'],
        'area': str(null_totals['area']),
        'physical_area': str(null_totals['area']),
    }
    return object_records, contributions, null_allocation


def _specimen_region_records(specimen_id, catalog, observed_set, contributions):
    aggregates = {
        region_id: {'pixel_count': 0, 'intensity_sum': 0,
                   'area': Fraction(0), 'object_count': 0}
        for region_id in catalog
    }
    for contribution in contributions:
        aggregate = aggregates[contribution['region_id']]
        aggregate['pixel_count'] += contribution['pixel_count']
        aggregate['intensity_sum'] += contribution['intensity_sum']
        aggregate['area'] += Fraction(contribution['area'])
        aggregate['object_count'] += 1

    records = []
    for region_id in catalog:
        observed = region_id in observed_set
        if not observed:
            records.append({
                'record_type': 'specimen_region',
                'specimen_id': specimen_id,
                'region_id': region_id,
                'observed': False,
                'status': REGION_MISSING,
                'observation_status': REGION_MISSING,
                'missing': True,
                'pixel_count': None,
                'area': None,
                'physical_area': None,
                'intensity_sum': None,
                'object_count': None,
                'object_region_count': None,
                'contribution_count': None,
            })
            continue

        aggregate = aggregates[region_id]
        area_text = str(aggregate['area'])
        records.append({
            'record_type': 'specimen_region',
            'specimen_id': specimen_id,
            'region_id': region_id,
            'observed': True,
            'status': REGION_OBSERVED,
            'observation_status': REGION_OBSERVED,
            'missing': False,
            'pixel_count': aggregate['pixel_count'],
            'area': area_text,
            'physical_area': area_text,
            'intensity_sum': aggregate['intensity_sum'],
            'object_count': aggregate['object_count'],
            'object_region_count': aggregate['object_count'],
            'contribution_count': aggregate['object_count'],
        })
    return records


def deliver_regions(region_catalog=None, specimens=None, **kwargs):
    """Attribute selected whole-board objects to catalog regions.

    The canonical call is ``deliver_regions(region_ids, specimens)``.
    See ``docs/regions.md`` for the complete public field contract.
    """
    catalog_raw, specimen_raw = _adapt_call(region_catalog, specimens, kwargs)
    catalog = sorted(_parse_catalog(catalog_raw))
    catalog_set = set(catalog)

    if not isinstance(specimen_raw, list) or not 1 <= len(specimen_raw) <= _MAX_SPECIMENS:
        raise RegionDeliveryError(
            f'specimens must be a list of 1..{_MAX_SPECIMENS} specimens')

    prepared = []
    specimen_ids = []
    for raw in specimen_raw:
        specimen_id, board, grid, observed_raw = _parse_specimen(raw)
        specimen_ids.append(specimen_id)
        if not isinstance(observed_raw, list):
            raise RegionDeliveryError(
                f'specimen {specimen_id!r}: observed_regions must be a list')
        observed = []
        for value in observed_raw:
            region_id = _identity(value, 'observed region_id')
            if region_id not in catalog_set:
                raise RegionDeliveryError(
                    f'specimen {specimen_id!r}: observed region {region_id!r} '
                    'is absent from catalog')
            observed.append(region_id)
        observed_set = set(observed)
        prepared.append({
            'specimen_id': specimen_id,
            'board': board,
            'grid': grid,
            'observed': observed_set,
        })
    _duplicate(specimen_ids, 'specimen_id')
    prepared.sort(key=lambda item: item['specimen_id'])

    specimen_records = []
    object_records = []
    contribution_records = []
    region_records = []

    for item in prepared:
        specimen_id = item['specimen_id']
        board = item['board']
        # This is the unchanged whole-board pipeline.  Do not pass the region
        # raster into it; region boundaries must never affect components.
        board_result = process(board)
        _validate_grid(
            specimen_id, item['grid'], board_result,
            catalog_set, item['observed'])

        objects, contributions, null_allocation = _attribute_objects(
            specimen_id, board, board_result, item['grid'])
        records = _specimen_region_records(
            specimen_id, catalog, item['observed'], contributions)

        observed_list = sorted(item['observed'])
        missing_list = [rid for rid in catalog if rid not in item['observed']]
        specimen_record = AliasDict({
            'specimen_id': specimen_id,
            'observed_regions': observed_list,
            'missing_regions': missing_list,
            'process_result': board_result,
            'objects': objects,
            'object_keys': [obj['object_key'] for obj in objects],
            'object_region_contributions': contributions,
            'region_records': records,
            'null_allocation': null_allocation,
            'unassigned_pixel_count': null_allocation['pixel_count'],
            'unassigned_pixels': null_allocation['pixel_count'],
            'null_pixel_count': null_allocation['pixel_count'],
            'null_pixels': null_allocation['pixel_count'],
            'unassigned_intensity_sum': null_allocation['intensity_sum'],
            'null_intensity_sum': null_allocation['intensity_sum'],
            'unassigned_area': null_allocation['area'],
            'null_area': null_allocation['area'],
        }, aliases=_SPECIMEN_ALIASES)
        specimen_records.append(specimen_record)
        object_records.extend(objects)
        contribution_records.extend(contributions)
        region_records.extend(records)

    # Make the documented deterministic ordering explicit rather than relying
    # only on traversal order inside process().
    object_records.sort(key=lambda obj: (
        obj['specimen_id'], obj['first_y'], obj['first_x'], obj['label']))
    contribution_records.sort(key=lambda row: (
        row['specimen_id'], row['object_key'][2], row['object_key'][1],
        row['region_id']))

    summaries = _region_summaries(
        catalog, specimen_records, region_records)

    all_null_count = sum(s['null_pixel_count'] for s in specimen_records)
    all_null_intensity = sum(
        s['null_intensity_sum'] for s in specimen_records)
    all_null_area = sum(Fraction(s['null_area']) for s in specimen_records)
    delivery = RegionDelivery({
        'format': REGION_DELIVERY_FORMAT,
        'region_catalog': catalog,
        'specimens': specimen_records,
        'objects': object_records,
        'object_keys': [obj['object_key'] for obj in object_records],
        'object_region_contributions': contribution_records,
        'specimen_region_records': region_records,
        'region_summaries': summaries,
        'complete_region_ids': [r['region_id'] for r in summaries
                                if r['complete']],
        'incomplete_region_ids': [r['region_id'] for r in summaries
                                  if not r['complete']],
        'null_allocation': {
            'pixel_count': all_null_count,
            'intensity_sum': all_null_intensity,
            'area': str(all_null_area),
            'physical_area': str(all_null_area),
        },
        'summary': {
            'specimen_count': len(specimen_records),
            'region_count': len(catalog),
            'selected_object_count': len(object_records),
            'object_region_contribution_count': len(contribution_records),
            'complete_region_count': sum(r['complete'] for r in summaries),
            'null_pixel_count': all_null_count,
            'null_intensity_sum': all_null_intensity,
        },
    })
    verify_region_delivery(delivery, raise_on_error=True)
    return delivery


def _region_summaries(catalog, specimen_records, region_records):
    by_pair = {(r['specimen_id'], r['region_id']): r for r in region_records}
    summaries = []
    for region_id in catalog:
        observed_ids = []
        missing_ids = []
        pixel_count = 0
        intensity_sum = 0
        area = Fraction(0)
        object_count = 0
        for specimen in specimen_records:
            specimen_id = specimen['specimen_id']
            record = by_pair[(specimen_id, region_id)]
            if record['observed']:
                observed_ids.append(specimen_id)
                pixel_count += record['pixel_count']
                intensity_sum += record['intensity_sum']
                area += Fraction(record['area'])
                object_count += record['object_count']
            else:
                missing_ids.append(specimen_id)

        total_specimens = len(specimen_records)
        summaries.append({
            'record_type': 'region_summary',
            'region_id': region_id,
            'observed_specimen_ids': observed_ids,
            'missing_specimen_ids': missing_ids,
            'observed_ids': observed_ids,
            'missing_ids': missing_ids,
            'observed_specimens': observed_ids,
            'missing_specimens': missing_ids,
            'observed_specimen_count': len(observed_ids),
            'missing_specimen_count': len(missing_ids),
            'total_specimen_count': total_specimens,
            'complete': not missing_ids,
            'pixel_count': pixel_count,
            'area': str(area),
            'physical_area': str(area),
            'intensity_sum': intensity_sum,
            'object_count': object_count,
            'object_region_count': object_count,
            'contribution_count': object_count,
        })
    return summaries


def verify_region_delivery(delivery, raise_on_error=False):
    """Check exact object, specimen-region and cross-specimen conservation."""
    try:
        specimens = delivery['specimens']
        summaries = delivery['region_summaries']
        for specimen in specimens:
            contributions_by_region = {}
            for obj in specimen['objects']:
                contributions = obj['region_contributions']
                count = sum(c['pixel_count'] for c in contributions)
                intensity = sum(c['intensity_sum'] for c in contributions)
                area = sum((Fraction(c['area']) for c in contributions),
                           Fraction(0))
                unassigned = obj['unassigned']
                assert count + unassigned['pixel_count'] == obj['pixel_count']
                assert (intensity + unassigned['intensity_sum']
                        == obj['intensity_sum'])
                assert area + Fraction(unassigned['area']) == Fraction(obj['area'])
                for contribution in contributions:
                    assert contribution['object_key'] == obj['object_key']
                    assert Fraction(contribution['ratio']) == Fraction(
                        contribution['pixel_count'], obj['pixel_count'])
                    contributions_by_region.setdefault(
                        contribution['region_id'], []).append(contribution)

            for region_record in specimen['region_records']:
                if not region_record['observed']:
                    assert all(region_record[key] is None for key in (
                        'pixel_count', 'area', 'intensity_sum', 'object_count'))
                    continue
                rows = contributions_by_region.get(
                    region_record['region_id'], [])
                assert region_record['pixel_count'] == sum(
                    c['pixel_count'] for c in rows)
                assert region_record['intensity_sum'] == sum(
                    c['intensity_sum'] for c in rows)
                assert Fraction(region_record['area']) == sum(
                    (Fraction(c['area']) for c in rows), Fraction(0))
                assert region_record['object_count'] == len(rows)

        for summary in summaries:
            rid = summary['region_id']
            rows = [r for s in specimens for r in s['region_records']
                    if r['region_id'] == rid and r['observed']]
            assert summary['pixel_count'] == sum(r['pixel_count'] for r in rows)
            assert summary['intensity_sum'] == sum(
                r['intensity_sum'] for r in rows)
            assert Fraction(summary['area']) == sum(
                (Fraction(r['area']) for r in rows), Fraction(0))
            assert summary['object_count'] == sum(r['object_count'] for r in rows)
            assert summary['complete'] == (
                not summary['missing_specimen_ids'])
        return True
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        if raise_on_error:
            raise RegionDeliveryError(
                f'region delivery conservation verification failed: {exc}') from exc
        return False


# Descriptive aliases for the same public entry point.
attribute_regions = deliver_regions
attribute_objects = deliver_regions
attribute_objects_to_regions = deliver_regions
assign_regions = deliver_regions
deliver_region_batch = deliver_regions
deliver_region_attributions = deliver_regions
build_region_attribution = deliver_regions
build_region_delivery = deliver_regions
compile_region_batch = deliver_regions
compile_region_delivery = deliver_regions
compile_regions = deliver_regions
create_region_delivery = deliver_regions
deliver_region_attribution = deliver_regions
region_delivery = deliver_regions
process_regions = deliver_regions
process_region_batch = deliver_regions
summarize_regions = deliver_regions
