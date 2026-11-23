"""Tile snapshot validation and full-canvas compilation.

A snapshot describes one version of a dataset as up to 32 overlapping
synthetic pixel tiles.  Tiles are resolved into a single binary mask, an
intensity surface and a per-pixel provenance plane; the compiled canvas is
then fed through the unchanged :func:`micro_objects.service.process`
pipeline so that connectivity, topology and selection budgets are always
computed on the stitched image, never per tile.
"""
import re

from .checks import integer, number
from .service import process

_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_MASK_CHARS = frozenset('01?')
_TILE_FIELDS = frozenset(('tile_id', 'x', 'y', 'priority', 'mask', 'intensity'))
_SNAPSHOT_FIELDS = frozenset(
    ('dataset_id', 'version_id', 'width', 'height', 'pixel_size',
     'connectivity', 'selection', 'tiles'))
_SELECTION_FIELDS = frozenset(('min_pixels', 'exclude_border'))


def _ident(value, name):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f'{name} must be 1..64 ascii letters, digits, _ or -')
    return value


def _strict_int(value, name, low, high):
    # bool is a subclass of int; the contract rejects booleans as numbers.
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}..{high}')
    return value


def _pixel_size(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('pixel_size must be two decimal strings')
    parsed = []
    for item in value:
        parsed.append(number(item))
    if any(size <= 0 for size in parsed):
        raise ValueError('positive pixel sizes required')
    return list(value)


def _selection(value):
    if not isinstance(value, dict) or set(value) != _SELECTION_FIELDS:
        raise ValueError('selection requires min_pixels and exclude_border')
    integer(value['min_pixels'], 1, 65536)
    if type(value['exclude_border']) is not bool:
        raise ValueError('exclude_border must be boolean')
    return {'min_pixels': value['min_pixels'],
            'exclude_border': value['exclude_border']}


def _tile(value, width, height):
    if not isinstance(value, dict) or set(value) != _TILE_FIELDS:
        raise ValueError(f'tile requires fields {sorted(_TILE_FIELDS)}')
    tile_id = _ident(value['tile_id'], 'tile_id')
    x = _strict_int(value['x'], 'tile x', 0, 255)
    y = _strict_int(value['y'], 'tile y', 0, 255)
    priority = _strict_int(value['priority'], 'priority', 0, 65535)
    mask = value['mask']
    if (not isinstance(mask, list) or not mask or not isinstance(mask[0], str)
            or not mask[0] or len(mask[0]) > 256):
        raise ValueError('tile mask must be non-empty equal-width rows')
    tile_w = len(mask[0])
    if any(not isinstance(row, str) or len(row) != tile_w
           or not row or set(row) - _MASK_CHARS for row in mask):
        raise ValueError('tile mask rows must contain only 0, 1 and ?')
    tile_h = len(mask)
    if x + tile_w > width or y + tile_h > height:
        raise ValueError(f'tile {tile_id} extends outside the canvas')
    intensity = value['intensity']
    if not isinstance(intensity, list) or len(intensity) != tile_h:
        raise ValueError(f'tile {tile_id} intensity must match the mask shape')
    for row in intensity:
        if not isinstance(row, list) or len(row) != tile_w:
            raise ValueError(f'tile {tile_id} intensity must match the mask shape')
        for cell in row:
            _strict_int(cell, 'intensity', 0, 65535)
    return {'tile_id': tile_id, 'x': x, 'y': y, 'priority': priority,
            'mask': mask, 'intensity': intensity,
            'width': tile_w, 'height': tile_h}


def parse_snapshot(snapshot):
    """Validate a raw snapshot dictionary and return a normalized copy."""
    if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
        raise ValueError(f'snapshot requires fields {sorted(_SNAPSHOT_FIELDS)}')
    dataset_id = _ident(snapshot['dataset_id'], 'dataset_id')
    version_id = _ident(snapshot['version_id'], 'version_id')
    width = _strict_int(snapshot['width'], 'width', 1, 256)
    height = _strict_int(snapshot['height'], 'height', 1, 256)
    pixel_size = _pixel_size(snapshot['pixel_size'])
    connectivity = integer(snapshot['connectivity'], 4, 8)
    if connectivity not in (4, 8):
        raise ValueError('connectivity must be four or eight')
    selection = _selection(snapshot['selection'])
    raw_tiles = snapshot['tiles']
    if not isinstance(raw_tiles, list) or not 0 <= len(raw_tiles) <= 32:
        raise ValueError('a snapshot carries 0..32 tiles')
    tiles = [_tile(tile, width, height) for tile in raw_tiles]
    seen = set()
    for tile in tiles:
        if tile['tile_id'] in seen:
            raise ValueError(f"duplicate tile id {tile['tile_id']}")
        seen.add(tile['tile_id'])
    return {'dataset_id': dataset_id, 'version_id': version_id,
            'width': width, 'height': height, 'pixel_size': pixel_size,
            'connectivity': connectivity, 'selection': selection,
            'tiles': tiles}


def compile_snapshot(snapshot):
    """Compile a validated/raw snapshot into mask, intensity and sources.

    Returns the original-format :func:`micro_objects.process` result plus the
    compiled inputs and the provenance plane.  Unobserved canvas positions
    participate as plain background (mask ``0``) but carry a null source, so
    they can never be mistaken for explicitly sourced background.
    """
    snap = parse_snapshot(snapshot)
    width, height = snap['width'], snap['height']
    winner = [[None] * width for _ in range(height)]      # '0' | '1' | None
    win_priority = [[None] * width for _ in range(height)]
    win_id = [[None] * width for _ in range(height)]
    intensity = [[0] * width for _ in range(height)]
    sources = [[None] * width for _ in range(height)]

    # Resolution is independent of tile input order: highest priority wins,
    # ties go to the lexicographically smallest tile id; '?' never covers.
    for tile in snap['tiles']:
        tid, priority = tile['tile_id'], tile['priority']
        for ty, row in enumerate(tile['mask']):
            cy = tile['y'] + ty
            for tx, char in enumerate(row):
                if char == '?':
                    continue
                cx = tile['x'] + tx
                current = win_priority[cy][cx]
                if (current is None or priority > current
                        or (priority == current and tid < win_id[cy][cx])):
                    winner[cy][cx] = char
                    win_priority[cy][cx] = priority
                    win_id[cy][cx] = tid
                    intensity[cy][cx] = tile['intensity'][ty][tx]
                    sources[cy][cx] = {'tile_id': tid,
                                       'tile_coord': [tx, ty]}

    mask_rows = [''.join('1' if cell == '1' else '0' for cell in row)
                 for row in winner]
    result = process({
        'mask': mask_rows,
        'intensity': intensity,
        'pixel_size': snap['pixel_size'],
        'connectivity': snap['connectivity'],
        'selection': snap['selection'],
    })
    return {
        'dataset_id': snap['dataset_id'],
        'version_id': snap['version_id'],
        'width': width,
        'height': height,
        'pixel_size': snap['pixel_size'],
        'connectivity': snap['connectivity'],
        'selection': snap['selection'],
        'mask': mask_rows,
        'intensity': intensity,
        'sources': sources,
        'result': result,
    }
