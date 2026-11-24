"""Offline archive flow for very long synthetic texture scan strips.

A board (width 1..256, height 1..32768) is delivered as a one-shot, top-to
bottom stream of binary mask/intensity strips.  Whole-board connected
components must be computed across strip seams without ever holding the
board, the full object table or a whole-pixel index in memory.

The implementation combines:

* a streaming connected-component labeler (:class:`_StreamLabeler`) whose
  union-find, recycled slot pool and per-component statistics occupy
  O(width) memory; hole counts use a bounded complementary-connectivity
  flood fill at component finalization, so no per-object bounding-box scan
  is ever expanded while rows stream;
* a single local SQLite database (Python standard library) as the portable,
  offline data pack.  Every original intensity sample is stored at most once
  per coordinate; object delivery and approved-strip recovery reference the
  same rows.

Only the Python standard library is used; there is no UI and no service.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from fractions import Fraction

from .checks import number

ARCHIVE_FORMAT = 'micro-objects-archive/1'
APPROVED = 'approved'
NOT_REQUESTED = 'not_requested'
INSUFFICIENT_BUDGET = 'insufficient_budget'

_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')


class ArchiveError(ValueError):
    """A board, strip, archive path or offline read request is invalid."""


def _strict_int(value, name, low, high):
    # bool is a subclass of int; the contract rejects booleans as numbers.
    if type(value) is not int or not low <= value <= high:
        raise ArchiveError(f'{name} must be an integer in {low}..{high}')
    return value


def _valid_id(value, name):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ArchiveError(
            f'{name} must be 1..64 ascii letters, digits, _ or -')
    return value


# ---------------------------------------------------------------------------
# Streaming connected-component labeler
# ---------------------------------------------------------------------------

class _Stats:
    """Per-root running statistics; one instance per live slot (O(W) total)."""

    __slots__ = ('count', 'sum_x', 'sum_y', 'intensity_sum', 'perimeter',
                 'min_x', 'min_y', 'max_x', 'max_y',
                 'first_x', 'first_y', 'row_xs', 'touches_border')

    def __init__(self, x, y, intensity, width, height, sx, sy):
        self.count = 1
        self.sum_x = x
        self.sum_y = y
        self.intensity_sum = intensity
        # Exposed edges are accumulated exclusively by the labeler's per-cell
        # checks, so the running total starts at zero.
        self.perimeter = 0
        self.min_x = self.max_x = x
        self.min_y = self.max_y = y
        # Raster-first foreground coordinate, independent of bbox extrema and
        # of the order in which temporary slots happen to be merged.
        self.first_x = x
        self.first_y = y
        # Foreground x positions on the row currently being processed; used to
        # add board-bottom lower edges when the component finalizes.
        self.row_xs = {x}
        self.touches_border = (
            x == 0 or y == 0 or x == width - 1 or y == height - 1)

    def merge(self, other):
        self.count += other.count
        self.sum_x += other.sum_x
        self.sum_y += other.sum_y
        self.intensity_sum += other.intensity_sum
        self.perimeter += other.perimeter
        if other.min_x < self.min_x:
            self.min_x = other.min_x
        if other.max_x > self.max_x:
            self.max_x = other.max_x
        if other.min_y < self.min_y:
            self.min_y = other.min_y
        if other.max_y > self.max_y:
            self.max_y = other.max_y
        if (other.first_y, other.first_x) < (self.first_y, self.first_x):
            self.first_y = other.first_y
            self.first_x = other.first_x
        self.row_xs |= other.row_xs
        self.touches_border = self.touches_border or other.touches_border


class _StreamLabeler:
    """Whole-board connected components over a one-shot row stream.

    Rows are ``'0'/'1'`` strings processed strictly top to bottom.
    Temporary slot numbers are recycled once a component can no longer touch
    future rows, so every parallel array stays within O(width) slots (only
    slots reachable from two adjacent rows stay allocated).  Pixel rows
    written by the builder carry the ``(slot, epoch)`` pair active for that
    row; an epoch bump makes a recycled slot distinguishable from every
    earlier use.

    Hole counts are produced when a component finalizes, by the same bounded
    complementary-connectivity flood fill as :func:`micro_objects.label.holes`,
    walking only the component's padded bounding box (linear in its cells);
    another component's cells always count as background for that fill.  No
    per-object bounding-box work is expanded while rows are still streaming.
    """

    def __init__(self, width, height, connectivity, sx, sy, sink):
        self.width = width
        self.height = height
        self.connectivity = connectivity
        self.sx = sx
        self.sy = sy
        self.sink = sink
        # Parallel arrays indexed by slot (slot 0 is the background sentinel).
        self.parent = [0]
        self.union_size = [0]
        self.epoch = [0]
        self.stats = [None]
        # A dead slot's component has finalized; its cells remain readable in
        # prev until the next row swap but it must never receive new pixels.
        self.dead = [True]
        self.head = [0]
        self.tail = [0]
        self.free_slots = []
        # Slots finalized while row y is processed can still be read through
        # prev during that row's window pass; they are parked here and only
        # recycled after the row swap, when prev points at the new row.
        self.pending_free = []
        self.active_roots = set()
        self.prev = [0] * width
        self.cur = [0] * width
        self._prev_mask = ['0'] * width
        self.y = 0
        self.component_seq = 0

    # -- union-find ---------------------------------------------------------

    def find(self, slot):
        parent = self.parent
        root = slot
        while parent[root] != root:
            root = parent[root]
        while parent[slot] != slot:
            nxt = parent[slot]
            parent[slot] = root
            slot = nxt
        return root

    def _allocate(self, x, y, intensity):
        if self.free_slots:
            slot = self.free_slots.pop()
            self.epoch[slot] += 1
        else:
            slot = len(self.parent)
            self.parent.append(slot)
            self.union_size.append(1)
            self.epoch.append(1)
            self.stats.append(None)
            self.dead.append(False)
            self.head.append(0)
            self.tail.append(0)
        self.parent[slot] = slot
        self.union_size[slot] = 1
        self.dead[slot] = False
        self.stats[slot] = _Stats(x, y, intensity, self.width, self.height,
                                  self.sx, self.sy)
        tag_id = self.sink.allocate_tag(slot, self.epoch[slot])
        self.head[slot] = self.tail[slot] = tag_id
        self.active_roots.add(slot)
        return slot

    def _union(self, first, second):
        r1 = self.find(first)
        r2 = self.find(second)
        if r1 == r2:
            return r1
        # Union by size; splice the second tag chain onto the first with one
        # indexed update so pixel ownership never moves per pixel.
        if self.union_size[r1] < self.union_size[r2]:
            r1, r2 = r2, r1
        self.parent[r2] = r1
        self.union_size[r1] += self.union_size[r2]
        self.stats[r1].merge(self.stats[r2])
        self.sink.splice_tags(self.tail[r1], self.head[r2])
        self.tail[r1] = self.tail[r2]
        self.active_roots.discard(r2)
        return r1

    def _active(self, slot):
        """True only for slots whose component may still receive pixels."""
        return slot != 0 and not self.dead[slot]

    # -- row processing -----------------------------------------------------

    def add_row(self, mask_row, intensity_row):
        width = self.width
        y = self.y
        cur = self.cur
        prev = self.prev
        for x, char in enumerate(mask_row):
            if char != '1':
                continue
            neighbor_slots = []
            if x > 0 and self._active(cur[x - 1]):
                neighbor_slots.append(cur[x - 1])
            if self._active(prev[x]):
                neighbor_slots.append(prev[x])
            if self.connectivity == 8:
                if x > 0 and self._active(prev[x - 1]):
                    neighbor_slots.append(prev[x - 1])
                if x + 1 < width and self._active(prev[x + 1]):
                    neighbor_slots.append(prev[x + 1])
            if not neighbor_slots:
                slot = self._allocate(x, y, intensity_row[x])
            else:
                slot = neighbor_slots[0]
                for other in neighbor_slots[1:]:
                    slot = self._union(slot, other)
                slot = self.find(slot)
                stats = self.stats[slot]
                stats.count += 1
                stats.sum_x += x
                stats.sum_y += y
                stats.intensity_sum += intensity_row[x]
                if x < stats.min_x:
                    stats.min_x = x
                if x > stats.max_x:
                    stats.max_x = x
                if y < stats.min_y:
                    stats.min_y = y
                if y > stats.max_y:
                    stats.max_y = y
                if (y, x) < (stats.first_y, stats.first_x):
                    stats.first_y = y
                    stats.first_x = x
                if (x == 0 or y == 0 or x == width - 1
                        or y == self.height - 1):
                    stats.touches_border = True
            cur[x] = slot
            stats = self.stats[slot]
            # Vertical (left/right) edges carry the vertical pixel size.  A
            # left neighbor belongs to the same component only when its active
            # slot resolves to this cell's root; a dead slot is another
            # component and stays background.
            left_same = x > 0 and self._active(cur[x - 1]) and \
                self.find(cur[x - 1]) == slot
            if not left_same:
                stats.perimeter += self.sy
            # cur[x+1] is unassigned in this left-to-right scan, so an active
            # foreground cell there is impossible; the edge is exposed unless
            # the cell is the right board boundary.
            if x + 1 == width or mask_row[x + 1] != '1':
                stats.perimeter += self.sy
            # Exposed upper edge carries the horizontal pixel size.
            if not self._active(prev[x]):
                stats.perimeter += self.sx
        # Refresh each surviving root's current-row x positions from cur.
        # Roots absent from this row retain their previous row_xs, which is
        # exactly their final row when they finalize below.
        current_row_xs = {}
        for x, slot in enumerate(cur):
            if self._active(slot):
                current_row_xs.setdefault(self.find(slot), set()).add(x)
        for root, xs in current_row_xs.items():
            self.stats[root].row_xs = xs
        # Lower edges of the previous row's foreground cells: exposed when the
        # cell directly below (same x in cur) is not the same component.
        if y > 0:
            for x, slot in enumerate(prev):
                if slot and self._active(slot) and self._prev_mask[x] == '1':
                    root = self.find(slot)
                    below = cur[x] if self._active(cur[x]) else 0
                    if not below or self.find(below) != root:
                        self.stats[root].perimeter += self.sx
        self._finalize_dead_roots()
        self.prev = cur
        self._prev_mask = list(mask_row)
        self.cur = [0] * width
        # Slots finalized this row are no longer reachable once prev moved.
        self.free_slots.extend(self.pending_free)
        self.pending_free = []
        self.y += 1

    def finish(self):
        """Flush every component still alive after the final row."""
        if self.y != self.height:
            raise ArchiveError(
                f'stream ended at row {self.y}; expected {self.height}')
        # Components reaching the board bottom still own an exposed lower edge
        # on every final-row foreground cell; add them before finalizing.
        for root in list(self.active_roots):
            if self.find(root) == root:
                self.stats[root].perimeter += \
                    self.sx * len(self.stats[root].row_xs)
                self._finalize_root(root)

    def _finalize_root(self, root):
        self.active_roots.discard(root)
        self.component_seq += 1
        stats = self.stats[root]
        owned_slots = [slot for slot in range(1, len(self.parent))
                       if self.find(slot) == root]
        # Holes use the exact bounded complementary-connectivity flood fill
        # from label.holes; the sink keeps membership and visited state in the
        # local database, so no component-sized Python set is retained.
        holes = self.sink.count_holes(
            self.head[root], stats.min_x, stats.min_y,
            stats.max_x + 1, stats.max_y + 1, self.connectivity)
        self.sink.finalize_component(
            self.component_seq, self.head[root], stats, holes,
            self.sx, self.sy)
        for slot in owned_slots:
            # Mark dead and detach the stats: finalized components no longer
            # accumulate topology or perimeter.  The slot stays reachable
            # through prev for this row's adjacency checks until the row swap,
            # then returns to the free pool for a fresh epoch.
            self.dead[slot] = True
            self.parent[slot] = slot
            self.union_size[slot] = 0
            self.stats[slot] = None
            self.head[slot] = self.tail[slot] = 0
            self.pending_free.append(slot)

    def _finalize_dead_roots(self):
        live_roots = {self.find(slot) for slot in self.cur if slot}
        for root in list(self.active_roots):
            if root not in live_roots:
                self._finalize_root(root)


# ---------------------------------------------------------------------------
# SQLite-backed tag sink
# ---------------------------------------------------------------------------

class _SqliteSink:
    """Tag chains and component rows live in SQLite.

    Only head/tail pointers stay in Python memory.  Pixel rows are written by
    the builder with the ``(slot, epoch)`` pair active for that row; when a
    component finalizes, its tag chain enumerates every ``(slot, epoch)`` it
    ever owned and one indexed UPDATE per pair assigns the component id.
    """

    def __init__(self, connection):
        self.conn = connection
        self._next_tag = 1

    def allocate_tag(self, slot, epoch):
        tag_id = self._next_tag
        self._next_tag += 1
        self.conn.execute(
            'INSERT INTO tags(id, slot, epoch, next_tag) VALUES (?,?,?,?)',
            (tag_id, slot, epoch, 0))
        return tag_id

    def splice_tags(self, tail_id, head_id):
        self.conn.execute('UPDATE tags SET next_tag=? WHERE id=?',
                          (head_id, tail_id))

    def count_holes(self, head_id, x0, y0, x1, y1, connectivity):
        """Complementary-connectivity hole count matching label.holes.

        Two bytearrays cover the padded bounding box: one marks the
        component's occupied cells, one visited background.  Their size is
        linear in the component's bbox area (never the whole board) and they
        are released when this method returns, so the steady-state builder
        memory stays bounded by the largest component finalized in one step.
        """
        conn = self.conn
        pairs = []
        tag_id = head_id
        while tag_id:
            slot, epoch, next_id = conn.execute(
                'SELECT slot, epoch, next_tag FROM tags WHERE id=?',
                (tag_id,)).fetchone()
            pairs.append((slot, epoch))
            tag_id = next_id
        width = (x1 - x0 + 1) + 2          # columns x0-1 .. x1
        height = (y1 - y0 + 1) + 2         # rows y0-1 .. y1
        ox, oy = x0 - 1, y0 - 1
        occupied = bytearray(width * height)

        for slot, epoch in pairs:
            for x, y in conn.execute(
                    'SELECT x, y FROM pixels WHERE slot=? AND epoch=?',
                    (slot, epoch)):
                occupied[(y - oy) * width + (x - ox)] = 1
        seen = bytearray(width * height)
        steps = ((1, 0), (-1, 0), (0, 1), (0, -1),
                 (1, 1), (1, -1), (-1, 1), (-1, -1)) if \
            connectivity == 4 else \
            ((1, 0), (-1, 0), (0, 1), (0, -1))
        regions = 0
        for yy in range(y0 - 1, y1 + 1):
            base = (yy - oy) * width - ox
            for xx in range(x0 - 1, x1 + 1):
                start = base + xx
                if occupied[start] or seen[start]:
                    continue
                regions += 1
                pending = [start]
                seen[start] = 1
                while pending:
                    i = pending.pop()
                    a, b = i % width, i // width
                    for dx, dy in steps:
                        px, py = a + dx, b + dy
                        if 0 <= px < width and 0 <= py < height:
                            j = py * width + px
                            if not occupied[j] and not seen[j]:
                                seen[j] = 1
                                pending.append(j)
        return regions - 1

    def finalize_component(self, component_id, head_id, stats, holes,
                           sx, sy):
        conn = self.conn
        tag_id = head_id
        assignments = []
        while tag_id:
            slot, epoch, next_id = conn.execute(
                'SELECT slot, epoch, next_tag FROM tags WHERE id=?',
                (tag_id,)).fetchone()
            assignments.append((component_id, slot, epoch))
            tag_id = next_id
        conn.executemany(
            'UPDATE pixels SET component=? WHERE slot=? AND epoch=?',
            assignments)
        count = stats.count
        # Physical centroid of pixel-center coordinates, matching measure.py.
        centroid_x = str(
            (Fraction(stats.sum_x) + Fraction(count, 2)) * sx / count)
        centroid_y = str(
            (Fraction(stats.sum_y) + Fraction(count, 2)) * sy / count)
        conn.execute(
            'INSERT INTO components(id, key_x, key_y, pixel_count, '
            'bbox_x0, bbox_y0, bbox_x1, bbox_y1, area, edge_perimeter, '
            'centroid_x, centroid_y, intensity_sum, mean_intensity, '
            'holes, touches_border, selected) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)',
            (component_id, stats.first_x, stats.first_y, count,
             stats.min_x, stats.min_y, stats.max_x + 1, stats.max_y + 1,
             str(Fraction(count) * sx * sy), str(stats.perimeter),
             centroid_x, centroid_y, stats.intensity_sum,
             str(Fraction(stats.intensity_sum, count)), holes,
             1 if stats.touches_border else 0))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _parse_header(header):
    if not isinstance(header, dict) or set(header) != {
            'width', 'height', 'pixel_size', 'connectivity', 'selection'}:
        raise ArchiveError("header requires width, height, pixel_size, "
                           "connectivity and selection")
    width = _strict_int(header['width'], 'width', 1, 256)
    height = _strict_int(header['height'], 'height', 1, 32768)
    pixel_size = header['pixel_size']
    if not isinstance(pixel_size, list) or len(pixel_size) != 2:
        raise ArchiveError('pixel_size must be two decimal strings')
    sizes = [number(item) for item in pixel_size]
    if any(size <= 0 for size in sizes):
        raise ArchiveError('positive pixel sizes required')
    connectivity = header['connectivity']
    if connectivity not in (4, 8):
        raise ArchiveError('connectivity must be four or eight')
    selection = header['selection']
    if not isinstance(selection, dict) or set(selection) != {
            'min_pixels', 'exclude_border'}:
        raise ArchiveError('selection requires min_pixels and exclude_border')
    min_pixels = _strict_int(selection['min_pixels'], 'min_pixels', 1,
                             width * height)
    if type(selection['exclude_border']) is not bool:
        raise ArchiveError('exclude_border must be boolean')
    return {'width': width, 'height': height,
            'pixel_size': [str(item) for item in pixel_size],
            'pixel_size_fraction': sizes, 'connectivity': connectivity,
            'min_pixels': min_pixels,
            'exclude_border': selection['exclude_border']}


def _validate_strip(item, width):
    if not isinstance(item, dict) or set(item) != {
            'band_id', 'start_row', 'offline', 'mask', 'intensity'}:
        raise ArchiveError('strip requires band_id, start_row, offline, '
                           'mask and intensity')
    band_id = _valid_id(item['band_id'], 'band_id')
    if type(item['offline']) is not bool:
        raise ArchiveError('offline must be boolean')
    if type(item['start_row']) is not int or item['start_row'] < 0:
        raise ArchiveError('start_row must be a non-negative integer')
    mask = item['mask']
    if not isinstance(mask, list) or not 1 <= len(mask) <= 64:
        raise ArchiveError('strip mask must contain 1..64 rows')
    for row in mask:
        if not isinstance(row, str) or len(row) != width \
                or any(ch not in '01' for ch in row):
            raise ArchiveError(
                'strip mask rows must be board-width 0/1 strings')
    intensity = item['intensity']
    if not isinstance(intensity, list) or len(intensity) != len(mask):
        raise ArchiveError('strip intensity must match the mask shape')
    for row in intensity:
        if not isinstance(row, list) or len(row) != width:
            raise ArchiveError('strip intensity must match the mask shape')
        for value in row:
            _strict_int(value, 'intensity', 0, 65535)
    return band_id, len(mask)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _create_database(path):
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=DELETE')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA temp_store=MEMORY')
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE strips(
            ord INTEGER PRIMARY KEY,
            band_id TEXT NOT NULL UNIQUE,
            start_row INTEGER NOT NULL,
            height INTEGER NOT NULL,
            requested INTEGER NOT NULL,
            decision TEXT NOT NULL,
            cost INTEGER
        );
        CREATE TABLE components(
            id INTEGER PRIMARY KEY,
            key_x INTEGER NOT NULL,
            key_y INTEGER NOT NULL,
            pixel_count INTEGER NOT NULL,
            bbox_x0 INTEGER NOT NULL,
            bbox_y0 INTEGER NOT NULL,
            bbox_x1 INTEGER NOT NULL,
            bbox_y1 INTEGER NOT NULL,
            area TEXT NOT NULL,
            edge_perimeter TEXT NOT NULL,
            centroid_x TEXT NOT NULL,
            centroid_y TEXT NOT NULL,
            intensity_sum INTEGER NOT NULL,
            mean_intensity TEXT NOT NULL,
            holes INTEGER NOT NULL,
            touches_border INTEGER NOT NULL,
            selected INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE pixels(
            y INTEGER NOT NULL,
            x INTEGER NOT NULL,
            slot INTEGER NOT NULL,
            epoch INTEGER NOT NULL,
            component INTEGER,
            mask INTEGER NOT NULL,
            intensity INTEGER NOT NULL,
            strip INTEGER NOT NULL,
            PRIMARY KEY(y, x)
        ) WITHOUT ROWID;
        CREATE TABLE tags(
            id INTEGER PRIMARY KEY,
            slot INTEGER NOT NULL,
            epoch INTEGER NOT NULL,
            next_tag INTEGER NOT NULL
        );
        CREATE INDEX idx_pixels_slot ON pixels(slot, epoch);
        """)
    return conn


def build_archive(target, header, strips, budget):
    """Build one offline archive pack from a single strip iterator.

    ``target`` must not already exist.  ``strips`` is consumed exactly once,
    in order, and must cover rows ``0..height-1`` without gaps, overlaps or
    extra rows; the iterator is never replayed.  On any validation error or
    interrupted iteration the temporary build area is removed and no
    consumable target is published.

    The retained Python memory is O(W*B + W + S) where W is the board width,
    B the largest delivered strip height and S the strip count; all
    board-scale state (pixels, tag chains, components) lives in the local
    SQLite build database and grows linearly with the board pixels.
    """
    target = os.fspath(target)
    if os.path.lexists(target):
        raise ArchiveError(f'target already exists: {target}')
    if not isinstance(header, dict):
        raise ArchiveError('header must be an object')
    parsed = _parse_header(header)
    width, height = parsed['width'], parsed['height']
    board_pixels = width * height
    budget = _strict_int(budget, 'budget', 0, board_pixels)

    target_path = os.path.abspath(target)
    parent = os.path.dirname(target_path) or os.curdir
    os.makedirs(parent, exist_ok=True)
    temp_dir = tempfile.mkdtemp(dir=parent, prefix='.archive-build-')
    db_path = os.path.join(temp_dir, 'archive.db')
    conn = None
    try:
        conn = _create_database(db_path)
        sink = _SqliteSink(conn)
        sx, sy = parsed['pixel_size_fraction']
        labeler = _StreamLabeler(width, height, parsed['connectivity'],
                                 sx, sy, sink)
        seen_ids = set()
        cursor = 0
        strip_ord = 0
        foreground_pixels = 0
        try:
            iterator = iter(strips)
        except TypeError as exc:
            raise ArchiveError('strips must be iterable') from exc
        try:
            for item in iterator:
                band_id, strip_height = _validate_strip(item, width)
                if band_id in seen_ids:
                    raise ArchiveError(f'duplicate band id {band_id!r}')
                start_row = item['start_row']
                if start_row != cursor:
                    raise ArchiveError(
                        f'strip {band_id!r} must start at row {cursor}, '
                        f'not {start_row}')
                if start_row + strip_height > height:
                    raise ArchiveError(
                        f'strip {band_id!r} extends past the board bottom')
                seen_ids.add(band_id)
                conn.execute(
                    'INSERT INTO strips(ord, band_id, start_row, height, '
                    'requested, decision, cost) VALUES (?,?,?,?,?,?,?)',
                    (strip_ord, band_id, start_row, strip_height,
                     1 if item['offline'] else 0, NOT_REQUESTED, None))
                for local_y, (mask_row, intensity_row) in enumerate(
                        zip(item['mask'], item['intensity'])):
                    y = start_row + local_y
                    labeler.add_row(mask_row, intensity_row)
                    rows = []
                    for x, char in enumerate(mask_row):
                        slot = labeler.prev[x]
                        rows.append((
                            y, x, slot, labeler.epoch[slot], None,
                            1 if char == '1' else 0,
                            intensity_row[x], strip_ord))
                        if char == '1':
                            foreground_pixels += 1
                    conn.executemany(
                        'INSERT INTO pixels(y, x, slot, epoch, component, '
                        'mask, intensity, strip) VALUES (?,?,?,?,?,?,?,?)',
                        rows)
                strip_ord += 1
                cursor += strip_height
        except ArchiveError:
            raise
        except Exception as exc:  # interrupted/broken caller iteration
            raise ArchiveError(f'strip stream failed: {exc}') from exc
        if cursor != height:
            raise ArchiveError(
                f'strips cover {cursor} of {height} rows; gaps are refused')
        if strip_ord == 0:
            raise ArchiveError('at least one strip is required')
        labeler.finish()

        selected_pixels, selected_count = _apply_selection(
            conn, parsed['min_pixels'], parsed['exclude_border'])
        used_budget = _apply_budget_and_cleanup(conn, budget)
        _publish_meta(conn, parsed, budget, used_budget, strip_ord,
                      board_pixels, foreground_pixels, selected_pixels,
                      selected_count)
        conn.commit()
        conn.execute('DROP INDEX idx_pixels_slot')
        conn.execute('DROP TABLE tags')
        conn.execute(
            'CREATE INDEX idx_pixels_component ON pixels(component, y, x)')
        conn.execute(
            'CREATE INDEX idx_pixels_strip ON pixels(strip, y, x)')
        conn.commit()
        conn.execute('VACUUM')
        conn.close()
        conn = None
        # Publish only after every row and component is durably complete.
        os.replace(db_path, target_path)
        # The database moved out; remove the now-empty build area so a
        # successful publish leaves no temporary directory behind.
        shutil.rmtree(temp_dir, ignore_errors=True)
        return target_path
    except BaseException:
        if conn is not None:
            conn.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _apply_selection(conn, min_pixels, exclude_border):
    selected_pixels = 0
    selected_count = 0
    for cid, count, touches in conn.execute(
            'SELECT id, pixel_count, touches_border FROM components'):
        if count >= min_pixels and not (exclude_border and touches):
            conn.execute('UPDATE components SET selected=1 WHERE id=?',
                         (cid,))
            selected_pixels += count
            selected_count += 1
    return selected_pixels, selected_count


def _apply_budget_and_cleanup(conn, budget):
    """Process offline requests in board order; remove unpublishable rows.

    A requested band's cost is every cell not already delivered as selected
    foreground (all background cells plus all non-selected foreground).  The
    selected foreground of every band stays in the pack regardless of the
    decision; every other cell of a non-approved band is deleted, never
    zero-filled on recovery.
    """
    selected_ids = tuple(row[0] for row in conn.execute(
        'SELECT id FROM components WHERE selected=1'))
    # Per-band costs aggregate in one scan, ordered by strip ordinal.
    if selected_ids:
        placeholders = ','.join('?' * len(selected_ids))
        cost_rows = conn.execute(
            f'SELECT strip, COUNT(*) FROM pixels WHERE '
            f'component IS NULL OR component NOT IN ({placeholders}) '
            f'GROUP BY strip', selected_ids).fetchall()
    else:
        cost_rows = conn.execute(
            'SELECT strip, COUNT(*) FROM pixels GROUP BY strip').fetchall()
    cost_by_strip = dict(cost_rows)

    remaining = budget
    used = 0
    decisions = []
    rejected_ordinals = []
    for ordinal, requested in conn.execute(
            'SELECT ord, requested FROM strips ORDER BY ord'):
        if not requested:
            decisions.append((ordinal, NOT_REQUESTED, None))
            continue
        cost = cost_by_strip.get(ordinal, 0)
        if cost <= remaining:
            remaining -= cost
            used += cost
            decisions.append((ordinal, APPROVED, cost))
        else:
            decisions.append((ordinal, INSUFFICIENT_BUDGET, cost))
            rejected_ordinals.append(ordinal)
    conn.executemany(
        'UPDATE strips SET decision=?, cost=? WHERE ord=?',
        [(decision, cost, ordinal)
         for ordinal, decision, cost in decisions])
    # One set-based delete for all rejected bands.
    if rejected_ordinals:
        if selected_ids:
            placeholders = ','.join('?' * len(selected_ids))
            strip_marks = ','.join('?' * len(rejected_ordinals))
            conn.execute(
                f'DELETE FROM pixels WHERE strip IN ({strip_marks}) AND '
                f'(component IS NULL OR component NOT IN ({placeholders}))',
                (*rejected_ordinals, *selected_ids))
        else:
            strip_marks = ','.join('?' * len(rejected_ordinals))
            conn.execute(
                f'DELETE FROM pixels WHERE strip IN ({strip_marks})',
                rejected_ordinals)
    return used


def _publish_meta(conn, parsed, budget, used_budget, strip_count,
                  board_pixels, foreground_pixels, selected_pixels,
                  selected_count):
    meta = {
        'format': ARCHIVE_FORMAT,
        'width': str(parsed['width']),
        'height': str(parsed['height']),
        'pixel_size_x': parsed['pixel_size'][0],
        'pixel_size_y': parsed['pixel_size'][1],
        'connectivity': str(parsed['connectivity']),
        'min_pixels': str(parsed['min_pixels']),
        'exclude_border': '1' if parsed['exclude_border'] else '0',
        'budget': str(budget),
        'used_budget': str(used_budget),
        'strip_count': str(strip_count),
        'board_pixels': str(board_pixels),
        'foreground_pixels': str(foreground_pixels),
        'selected_pixels': str(selected_pixels),
        'component_count': str(
            conn.execute('SELECT COUNT(*) FROM components').fetchone()[0]),
        'selected_count': str(selected_count),
    }
    conn.executemany('INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)',
                     sorted(meta.items()))


# ---------------------------------------------------------------------------
# Offline reader
# ---------------------------------------------------------------------------

def _key_tuple(key):
    if not isinstance(key, (list, tuple)) or len(key) != 2 \
            or type(key[0]) is not int or type(key[1]) is not int \
            or key[0] < 0 or key[1] < 0:
        raise ArchiveError('object key must be two non-negative integers')
    return key[0], key[1]


class ArchiveReader:
    """Read-only, offline access to a published archive pack.

    The reader only needs the single SQLite file; it never opens the original
    strips or any network resource and can be copied to an air-gapped machine
    with a standard Python interpreter.
    """

    def __init__(self, path):
        self.path = os.fspath(path)
        if not os.path.isfile(self.path):
            raise ArchiveError(f'archive not found: {self.path}')
        uri = 'file:' + os.path.abspath(self.path).replace('#', '%23') \
            + '?mode=ro'
        self.conn = sqlite3.connect(uri, uri=True)
        try:
            if self._meta('format') != ARCHIVE_FORMAT:
                raise ArchiveError('unknown archive format')
            self._width = int(self._meta('width'))
            self._height = int(self._meta('height'))
        except sqlite3.DatabaseError as exc:
            self.close()
            raise ArchiveError(f'not a readable archive: {exc}') from exc

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        self.conn.close()

    def _meta(self, key):
        row = self.conn.execute('SELECT value FROM meta WHERE key=?',
                                (key,)).fetchone()
        if row is None:
            raise ArchiveError(f'archive is missing meta {key!r}')
        return row[0]

    # -- header-level access ------------------------------------------------

    @property
    def width(self):
        return self._width

    @property
    def height(self):
        return self._height

    @property
    def pixel_size(self):
        return [self._meta('pixel_size_x'), self._meta('pixel_size_y')]

    @property
    def connectivity(self):
        return int(self._meta('connectivity'))

    @property
    def selection(self):
        return {'min_pixels': int(self._meta('min_pixels')),
                'exclude_border': self._meta('exclude_border') == '1'}

    @property
    def budget(self):
        return int(self._meta('budget'))

    def used_budget(self):
        return int(self._meta('used_budget'))

    # -- object delivery ----------------------------------------------------

    def list_object_keys(self):
        """Selected object keys ``[x, y]`` in whole-board raster order."""
        return [[x, y] for x, y in self.conn.execute(
            'SELECT key_x, key_y FROM components WHERE selected=1 '
            'ORDER BY key_y, key_x')]

    def has_object(self, key):
        x, y = _key_tuple(key)
        return self.conn.execute(
            'SELECT 1 FROM components WHERE key_x=? AND key_y=? AND selected=1',
            (x, y)).fetchone() is not None

    def get_object(self, key):
        """Exact whole-board metrics for one selected object."""
        x, y = _key_tuple(key)
        row = self.conn.execute(
            'SELECT key_x, key_y, pixel_count, bbox_x0, bbox_y0, bbox_x1, '
            'bbox_y1, area, edge_perimeter, centroid_x, centroid_y, '
            'intensity_sum, mean_intensity, holes, touches_border '
            'FROM components WHERE key_x=? AND key_y=? AND selected=1',
            (x, y)).fetchone()
        if row is None:
            raise KeyError([x, y])
        (kx, ky, count, x0, y0, x1, y1, area, perimeter, cx, cy,
         intensity_sum, mean, holes, touches) = row
        return {
            'key': [kx, ky],
            'pixel_count': count,
            'bbox': [x0, y0, x1, y1],
            'area': area,
            'edge_perimeter': perimeter,
            'centroid': [cx, cy],
            'intensity_sum': intensity_sum,
            'mean_intensity': mean,
            'holes': holes,
            'touches_border': bool(touches),
        }

    def iter_object_samples(self, key, batch_size=256):
        """Yield batches of the object's raster-order foreground samples.

        Each sample is ``{'coord': [x, y], 'intensity': value}``.  Reads use
        the component index; no other object and no full-board label plane is
        touched.  ``batch_size`` must be a positive integer.
        """
        x, y = _key_tuple(key)
        _strict_int(batch_size, 'batch_size', 1, 1 << 31)
        row = self.conn.execute(
            'SELECT id FROM components WHERE key_x=? AND key_y=? AND selected=1',
            (x, y)).fetchone()
        if row is None:
            raise KeyError([x, y])
        component_id = row[0]
        cursor = self.conn.execute(
            'SELECT x, y, intensity FROM pixels WHERE component=? '
            'ORDER BY y, x', (component_id,))
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                return
            yield [{'coord': [px, py], 'intensity': value}
                   for px, py, value in batch]

    def read_object_samples(self, key):
        """Convenience: all raster-order samples of one selected object."""
        samples = []
        for batch in self.iter_object_samples(key):
            samples.extend(batch)
        return samples

    # -- strip recovery -----------------------------------------------------

    def list_band_decisions(self):
        """Per-band decisions in board order."""
        result = []
        for band_id, start_row, strip_height, requested, decision, cost in \
                self.conn.execute(
                    'SELECT band_id, start_row, height, requested, decision, '
                    'cost FROM strips ORDER BY ord'):
            entry = {'band_id': band_id, 'start_row': start_row,
                     'height': strip_height,
                     'requested': bool(requested), 'decision': decision}
            if cost is not None:
                entry['cost'] = cost
            result.append(entry)
        return result

    def get_band_decision(self, band_id):
        _valid_id(band_id, 'band_id')
        row = self.conn.execute(
            'SELECT start_row, height, requested, decision, cost '
            'FROM strips WHERE band_id=?', (band_id,)).fetchone()
        if row is None:
            raise ArchiveError(f'unknown band id {band_id!r}')
        start_row, strip_height, requested, decision, cost = row
        entry = {'band_id': band_id, 'start_row': start_row,
                 'height': strip_height, 'requested': bool(requested),
                 'decision': decision}
        if cost is not None:
            entry['cost'] = cost
        return entry

    def recover_band(self, band_id):
        """Reconstruct an approved band's original mask and intensity.

        Only approved ids are recoverable; unknown or non-approved ids raise
        :class:`ArchiveError`.  Missing cells are never zero-filled: a gap in
        the pack rows is an error.
        """
        _valid_id(band_id, 'band_id')
        row = self.conn.execute(
            'SELECT start_row, height, decision FROM strips '
            'WHERE band_id=?', (band_id,)).fetchone()
        if row is None:
            raise ArchiveError(f'unknown band id {band_id!r}')
        start_row, strip_height, decision = row
        if decision != APPROVED:
            raise ArchiveError(
                f'band {band_id!r} was not approved for offline recovery')
        pixels = self.conn.execute(
            'SELECT y, x, mask, intensity FROM pixels '
            'WHERE strip=(SELECT ord FROM strips WHERE band_id=?) '
            'ORDER BY y, x', (band_id,)).fetchall()
        expected = strip_height * self._width
        if len(pixels) != expected:
            raise ArchiveError(
                f'band {band_id!r} archive is incomplete; refusing to '
                'zero-fill missing cells')
        mask_rows = []
        intensity_rows = []
        index = 0
        for local_y in range(strip_height):
            mask_chars = []
            intensity = []
            for x in range(self._width):
                py, px, mask, value = pixels[index]
                index += 1
                if py != start_row + local_y or px != x:
                    raise ArchiveError(
                        f'band {band_id!r} archive has a cell gap')
                mask_chars.append('1' if mask else '0')
                intensity.append(value)
            mask_rows.append(''.join(mask_chars))
            intensity_rows.append(intensity)
        return {'band_id': band_id, 'start_row': start_row,
                'mask': mask_rows, 'intensity': intensity_rows}
