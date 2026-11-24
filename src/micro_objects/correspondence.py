"""Pairing of two independently extracted object sets and certain delivery.

Two already grid-aligned synthetic texture boards are processed with the
unchanged :func:`micro_objects.service.process` pipeline.  Only objects that
pass each board's own selection participate; objects are identified by their
raster-first foreground pixel ``[x, y]``, never by a temporary numeric label.

A reference/retest object pair requires at least one shared foreground grid
cell; the edge weight is the exact number of shared pixels (never a bounding
box area).  Valid schemes are matchings that lexicographically maximize
``(pair count, total overlap pixels)``.  The module never enumerates optimal
matchings: one weighted assignment plus dual-variable sensitivity analysis
decides which edges and which "left unmatched" choices occur in at least one
optimal scheme, and group totals are bounded with a fixed number of further
assignment runs on the tight equality graph.

Only the Python standard library is used; there is no UI, network service or
model training.
"""
import re
from fractions import Fraction

from .service import process

CORRESPONDENCE_FORMAT = 'micro-objects-correspondence/1'
GRADE_DELIVERY_FORMAT = 'micro-objects-grade-delivery/1'
GROUP_DELIVERY_FORMAT = 'micro-objects-group-delivery/1'
DIAGNOSTICS_FORMAT = 'micro-objects-correspondence-diagnostics/1'

# Per-retest-object withholding reasons.
NO_MATCH = 'no_match'
POSSIBLY_UNMATCHED = 'possibly_unmatched'
GRADE_CONFLICT = 'grade_conflict'

_NAME_RE = re.compile(r'[A-Za-z0-9_-]{1,32}\Z')
_MAX_OBJECTS = 128
_MAX_GROUPS = 8
_MAX_READING = 65535
# Larger than any achievable cost: readings sum to at most 128 * 65535 and
# transformed matching weights stay below board_pixels * 129.
_INF = 10 ** 30


class CorrespondenceError(ValueError):
    """A batch fails board/table validation; no partial delivery is returned."""


# ---------------------------------------------------------------------------
# Weighted assignment (Hungarian algorithm, minimum cost, square matrix)
# ---------------------------------------------------------------------------

def hungarian(cost):
    """Minimum-cost perfect assignment on an *n x n* integer matrix.

    Returns ``(total, assignment, row_potential, column_potential)`` where
    ``assignment[j]`` is the row assigned to column *j* and the potentials
    satisfy ``cost[i][j] - row_potential[i] - column_potential[j] >= 0`` with
    equality on every assigned edge.  Forbidden entries may carry a very large
    cost; the caller must guarantee that at least one finite perfect matching
    exists.  Pure integer arithmetic keeps every equality exact.
    """
    n = len(cost)
    u = [0] * (n + 1)
    v = [0] * (n + 1)
    p = [0] * (n + 1)          # p[j] = row (1-based) currently on column j
    way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [_INF] * (n + 1)
        used = bytearray(n + 1)
        while True:
            used[j0] = 1
            i0 = p[j0]
            row = cost[i0 - 1]
            ui0 = u[i0]
            delta = _INF
            j1 = 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = row[j - 1] - ui0 - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    assignment = [p[j] - 1 for j in range(1, n + 1)]
    total = sum(cost[assignment[j]][j] for j in range(n))
    return total, assignment, u[1:], v[1:]


def _strong_components(adjacency):
    """Kosaraju SCC with explicit stacks; vertices are 0..m-1."""
    m = len(adjacency)
    seen = bytearray(m)
    order = []
    for start in range(m):
        if seen[start]:
            continue
        seen[start] = 1
        stack = [(start, 0)]
        while stack:
            node, index = stack[-1]
            if index < len(adjacency[node]):
                nxt = adjacency[node][index]
                stack[-1] = (node, index + 1)
                if not seen[nxt]:
                    seen[nxt] = 1
                    stack.append((nxt, 0))
            else:
                order.append(node)
                stack.pop()
    reverse = [[] for _ in range(m)]
    for node, neighbors in enumerate(adjacency):
        for nxt in neighbors:
            reverse[nxt].append(node)
    component = [-1] * m
    component_id = 0
    for start in reversed(order):
        if component[start] != -1:
            continue
        component[start] = component_id
        stack = [start]
        while stack:
            node = stack.pop()
            for nxt in reverse[node]:
                if component[nxt] == -1:
                    component[nxt] = component_id
                    stack.append(nxt)
        component_id += 1
    return component


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def _board_side(data, side):
    """Run the original whole-board pipeline once and index accepted objects."""
    if not isinstance(data, dict):
        raise CorrespondenceError(f'{side} board must be a process() input object')
    try:
        result = process(data)
        height = len(data['mask'])
        width = len(data['mask'][0])
        size = tuple(Fraction(text) for text in data['pixel_size'])
    except CorrespondenceError:
        raise
    except ValueError as exc:
        raise CorrespondenceError(f'{side} board rejected: {exc}') from exc
    accepted = []
    for obj in result['objects']:
        if not obj['accepted']:
            continue
        first = obj['pixels'][0]
        accepted.append({
            'key': (first[0], first[1]),
            'pixels': frozenset((x, y) for x, y in obj['pixels']),
        })
    if len(accepted) > _MAX_OBJECTS:
        raise CorrespondenceError(
            f'{side} board has {len(accepted)} selected objects; at most '
            f'{_MAX_OBJECTS} may participate')
    return {
        'width': width,
        'height': height,
        'pixel_size': size,
        'objects': accepted,
    }


def _strict_key(value, what):
    if (not isinstance(value, list) or len(value) != 2
            or type(value[0]) is not int or type(value[1]) is not int
            or value[0] < 0 or value[1] < 0):
        raise CorrespondenceError(f'{what} object key must be [x, y] integers')
    return value[0], value[1]


def _name(value, what):
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise CorrespondenceError(
            f'{what} must be 1..32 ascii letters, digits, underscore or hyphen')
    return value


def _validate_reference_table(rows, side):
    if not isinstance(rows, list):
        raise CorrespondenceError('reference table must be a list of rows')
    objects = side['objects']
    index_by_key = {obj['key']: i for i, obj in enumerate(objects)}
    grade_of = [None] * len(objects)
    group_of = [None] * len(objects)
    seen = set()
    groups = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'key', 'grade', 'group'}:
            raise CorrespondenceError(
                'reference rows require exactly key, grade and group')
        key = _strict_key(row['key'], 'reference')
        index = index_by_key.get(key)
        if index is None:
            raise CorrespondenceError(
                f'reference row {list(key)} does not identify a selected object')
        if key in seen:
            raise CorrespondenceError(f'duplicate reference row {list(key)}')
        seen.add(key)
        grade = _name(row['grade'], 'grade')
        group = _name(row['group'], 'group')
        grade_of[index] = grade
        group_of[index] = group
        groups.add(group)
    if len(groups) > _MAX_GROUPS:
        raise CorrespondenceError(
            f'at most {_MAX_GROUPS} distinct groups are registered')
    if seen != set(index_by_key):
        raise CorrespondenceError(
            'reference table must cover every selected object exactly once')
    return grade_of, group_of, sorted(groups)


def _validate_retest_table(rows, side):
    if not isinstance(rows, list):
        raise CorrespondenceError('retest table must be a list of rows')
    objects = side['objects']
    index_by_key = {obj['key']: i for i, obj in enumerate(objects)}
    reading_of = [None] * len(objects)
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'key', 'reading'}:
            raise CorrespondenceError('retest rows require exactly key and reading')
        key = _strict_key(row['key'], 'retest')
        index = index_by_key.get(key)
        if index is None:
            raise CorrespondenceError(
                f'retest row {list(key)} does not identify a selected object')
        if key in seen:
            raise CorrespondenceError(f'duplicate retest row {list(key)}')
        seen.add(key)
        reading = row['reading']
        # bool is a subclass of int; independent readings are never boolean.
        if type(reading) is not int or not 0 <= reading <= _MAX_READING:
            raise CorrespondenceError(
                'retest reading must be an integer in 0..65535')
        reading_of[index] = reading
    if seen != set(index_by_key):
        raise CorrespondenceError(
            'retest table must cover every selected object exactly once')
    return reading_of


# ---------------------------------------------------------------------------
# Matching evidence
# ---------------------------------------------------------------------------

def _candidate_edges(reference, retest):
    """Map ``(ref_index, retest_index) -> exact shared foreground pixel count."""
    ref_at_pixel = {}
    for i, obj in enumerate(reference['objects']):
        for pixel in obj['pixels']:
            ref_at_pixel[pixel] = i
    edges = {}
    for j, obj in enumerate(retest['objects']):
        counts = {}
        for pixel in obj['pixels']:
            i = ref_at_pixel.get(pixel)
            if i is not None:
                counts[i] = counts.get(i, 0) + 1
        for i, weight in counts.items():
            edges[(i, j)] = weight
    return edges


def _solve_matching(edges, nr, nt, board_pixels):
    """Solve the lexicographic objective and return the equality-graph model.

    Edge value ``C + overlap`` with ``C > board_pixels`` makes one extra pair
    outweigh every possible overlap difference, so a single assignment run
    optimizes pair count first and total overlap second.

    The general model is a square matrix of size ``nr + nt`` whose extra rows
    and columns are explicit unmatched ("dummy") vertices of value zero;
    non-candidate real/real cells carry a penalty larger than every finite
    matching value, so a finite perfect matching always exists while no
    forbidden cell can enter an optimum.  Keeping forbidden cells *out* of the
    finite set is essential: otherwise they would pollute the dual potentials
    and corrupt sensitivity analysis.  Dense batches, including the documented
    256x256 grid, use a compact ``max(nr, nt)`` matrix with no forbidden cells,
    which is exact whenever every real/real cell is a candidate.
    """
    constant = board_pixels + 1
    forbidden_cost = 10 ** 12                 # > every finite matching value
    n = max(nr, nt)
    # The compact matrix works whenever every real/real cell is a candidate
    # (possibly of zero overlap): missing cells would silently behave like
    # zero-valued unmatched slots there, so sparse graphs take the explicit
    # dummy model instead.  Dense batches therefore stay on an n x n matrix
    # even with unequal side counts, while the documented 256x256 grid never
    # pays for the nr+nt general model.
    compact = nr * nt == len(edges)
    if compact:
        values = [[0] * n for _ in range(n)]
        for (i, j), weight in edges.items():
            values[i][j] = constant + weight
        finite = None                              # every cell is finite
        # Padding rows (nr < nt) are the only unmatched slots; when nr >= nt
        # every real retest column is matchable, so none can be left single.
        dummy_rows = range(nr, n)
    else:
        n = nr + nt
        values = [[None] * n for _ in range(n)]
        finite = set()
        # Explicit dummy rows and columns make a finite perfect matching
        # always exist; both kinds are unmatched slots of value zero.
        for i in range(n):
            for j in range(n):
                real_pair = i < nr and j < nt
                if not real_pair:
                    values[i][j] = 0
                    finite.add((i, j))
        for (i, j), weight in edges.items():
            values[i][j] = constant + weight
            finite.add((i, j))
        dummy_rows = range(nr, n)
    cost = [[(-value if value is not None else forbidden_cost)
             for value in row] for row in values]
    _total, assignment, cu, cv = hungarian(cost)
    # Max-side potentials U, V satisfy U[i] + V[j] >= value on finite cells,
    # with equality on every assigned edge.
    row_potential = [-x for x in cu]
    col_potential = [-x for x in cv]
    pair_count = 0
    overlap = 0
    for j, i in enumerate(assignment):
        if i < nr and j < nt and (i, j) in edges:
            pair_count += 1
            overlap += edges[(i, j)]
    return {
        'n': n,
        'values': values,
        'finite': finite,
        'dummy_rows': tuple(dummy_rows),
        'assignment': assignment,
        'row_potential': row_potential,
        'col_potential': col_potential,
        'pair_count': pair_count,
        'overlap': overlap,
    }


def _is_finite(model, i, j):
    finite = model['finite']
    return finite is None or (i, j) in finite


def _allowed_edges(edges, model, nr, nt):
    """Edges and unmatched choices that occur in at least one optimal scheme.

    Tight finite edges form the equality graph; an edge belongs to some
    optimal assignment iff it is tight and its endpoints share an SCC.  A real
    retest object can be left unmatched iff a tight dummy row reaches it
    inside one SCC.  No optimal matching is ever enumerated.
    """
    n = model['n']
    values = model['values']
    row_potential = model['row_potential']
    col_potential = model['col_potential']
    adjacency = [[] for _ in range(2 * n)]
    for i in range(n):
        for j in range(n):
            value = values[i][j]
            if value is not None and _is_finite(model, i, j) \
                    and row_potential[i] + col_potential[j] == value:
                adjacency[i].append(n + j)
    for j, i in enumerate(model['assignment']):
        adjacency[n + j].append(i)
    component = _strong_components(adjacency)

    allowed = set()
    for (i, j) in edges:
        value = values[i][j]
        if row_potential[i] + col_potential[j] == value \
                and component[i] == component[n + j]:
            allowed.add((i, j))
    can_be_unmatched = [False] * nt
    for j in range(nt):
        for i in model['dummy_rows']:
            if row_potential[i] + col_potential[j] == 0 \
                    and component[i] == component[n + j]:
                can_be_unmatched[j] = True
                break
    return allowed, can_be_unmatched


def _group_bounds(model, nr, nt, group_of, reading_of, groups):
    """Min/max group totals over complete optimal schemes.

    Every perfect matching made of tight finite edges is optimal (its edge sum
    equals the dual constant), and every optimal matching uses tight edges
    only.  Group totals are therefore the extrema of two ordinary assignments
    on that equality graph: one minimizes, one maximizes.  Extrema of
    different groups are independent witnesses; they are not promised to
    co-occur.
    """
    n = model['n']
    values = model['values']
    row_potential = model['row_potential']
    col_potential = model['col_potential']
    tight = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            value = values[i][j]
            tight[i][j] = (value is not None and _is_finite(model, i, j)
                           and row_potential[i] + col_potential[j] == value)
    bounds = {}
    for group in groups:
        minimum = [[_INF] * n for _ in range(n)]
        maximum = [[_INF] * n for _ in range(n)]
        for i in range(n):
            min_row = minimum[i]
            max_row = maximum[i]
            for j in range(n):
                if not tight[i][j]:
                    continue
                if i < nr and j < nt and group_of[i] == group:
                    reading = reading_of[j]
                    min_row[j] = reading
                    max_row[j] = -reading          # minimize -total
                else:
                    min_row[j] = 0
                    max_row[j] = 0
        min_total, _p, _u, _v = hungarian(minimum)
        neg_max, _p, _u, _v = hungarian(maximum)
        bounds[group] = (min_total, -neg_max)
    return bounds


# ---------------------------------------------------------------------------
# Public entry point and delivery documents
# ---------------------------------------------------------------------------

def deliver_correspondence(reference, retest, reference_table, retest_table):
    """Validate one batch and build the two independent delivery documents.

    ``reference`` and ``retest`` are raw :func:`micro_objects.process` inputs
    of two grid-aligned boards.  ``reference_table`` is a list of
    ``{"key": [x, y], "grade": ..., "group": ...}`` rows and ``retest_table``
    a list of ``{"key": [x, y], "reading": ...}`` rows; each table must cover
    exactly the selected objects of its board.  Any invalid board, table or
    cross-board shape raises :class:`CorrespondenceError` and rejects the whole
    batch.

    The returned mapping contains three self-contained documents:
    ``grade_delivery`` and ``group_delivery`` are the consumable tables;
    ``diagnostics`` carries per-object evidence, withholding reasons and group
    ranges and must not be merged into a consumable table.
    """
    ref_side = _board_side(reference, 'reference')
    test_side = _board_side(retest, 'retest')
    if ref_side['width'] != test_side['width'] \
            or ref_side['height'] != test_side['height']:
        raise CorrespondenceError(
            'the two boards must have equal width and height')
    if ref_side['pixel_size'] != test_side['pixel_size']:
        raise CorrespondenceError(
            'the two boards must use numerically equal pixel sizes on both axes')

    grade_of, group_of, groups = _validate_reference_table(
        reference_table, ref_side)
    reading_of = _validate_retest_table(retest_table, test_side)

    nr = len(ref_side['objects'])
    nt = len(test_side['objects'])
    edges = _candidate_edges(ref_side, test_side)
    model = _solve_matching(edges, nr, nt,
                            ref_side['width'] * ref_side['height'])
    allowed, can_be_unmatched = _allowed_edges(edges, model, nr, nt)
    bounds = _group_bounds(model, nr, nt,
                           group_of, reading_of, groups)

    candidates = [[] for _ in range(nt)]
    for i, j in allowed:
        candidates[j].append(i)
    for j in range(nt):
        candidates[j].sort(
            key=lambda index: (ref_side['objects'][index]['key'][1],
                               ref_side['objects'][index]['key'][0]))

    grade_records = []
    evidence = []
    for j, obj in enumerate(test_side['objects']):
        key = list(obj['key'])
        candidate_keys = [list(ref_side['objects'][i]['key'])
                          for i in candidates[j]]
        if not candidates[j]:
            delivered = False
            reason = NO_MATCH
        elif can_be_unmatched[j]:
            delivered = False
            reason = POSSIBLY_UNMATCHED
        else:
            grades = {grade_of[i] for i in candidates[j]}
            if len(grades) == 1:
                delivered = True
                reason = None
                grade_records.append({'key': key,
                                      'grade': next(iter(grades))})
            else:
                delivered = False
                reason = GRADE_CONFLICT
        evidence.append({
            'key': key,
            'candidate_reference_keys': candidate_keys,
            'can_be_unmatched': can_be_unmatched[j],
            'delivered': delivered,
            'reason': reason,
        })

    group_records = []
    group_bounds = []
    for group in groups:
        minimum, maximum = bounds[group]
        group_bounds.append({'group': group,
                             'min_total': minimum,
                             'max_total': maximum})
        if minimum == maximum:
            group_records.append({'group': group, 'total': minimum})

    grade_doc = {
        'format': GRADE_DELIVERY_FORMAT,
        'records': grade_records,
    }
    group_doc = {
        'format': GROUP_DELIVERY_FORMAT,
        'records': group_records,
    }
    diagnostics = {
        'format': DIAGNOSTICS_FORMAT,
        'optimal_matching': {
            'pair_count': model['pair_count'],
            'overlap_pixels': model['overlap'],
            'candidate_edge_count': len(edges),
            'allowed_edge_count': len(allowed),
        },
        'object_evidence': evidence,
        'group_bounds': group_bounds,
    }
    return {
        'format': CORRESPONDENCE_FORMAT,
        'grade_delivery': grade_doc,
        'group_delivery': group_doc,
        'diagnostics': diagnostics,
    }


# ---------------------------------------------------------------------------
# Strict read-back for consumers that hold neither masks nor evidence
# ---------------------------------------------------------------------------

def _parse_key(value):
    if (not isinstance(value, list) or len(value) != 2
            or type(value[0]) is not int or type(value[1]) is not int
            or value[0] < 0 or value[1] < 0):
        raise CorrespondenceError('delivery key must be [x, y] integers')
    return [value[0], value[1]]


def parse_grade_delivery(document):
    """Validate a grade delivery document read from JSON; normalized copy."""
    if not isinstance(document, dict) \
            or set(document) != {'format', 'records'}:
        raise CorrespondenceError(
            'grade delivery requires exactly format and records')
    if document['format'] != GRADE_DELIVERY_FORMAT:
        raise CorrespondenceError('unknown grade delivery format')
    records = document['records']
    if not isinstance(records, list):
        raise CorrespondenceError('grade delivery records must be a list')
    normalized = []
    seen = set()
    for row in records:
        if not isinstance(row, dict) or set(row) != {'key', 'grade'}:
            raise CorrespondenceError('grade row requires exactly key and grade')
        key = tuple(_parse_key(row['key']))
        if key in seen:
            raise CorrespondenceError('duplicate grade delivery key')
        seen.add(key)
        normalized.append({'key': list(key),
                           'grade': _name(row['grade'], 'grade')})
    normalized.sort(key=lambda row: (row['key'][1], row['key'][0]))
    return {'format': GRADE_DELIVERY_FORMAT, 'records': normalized}


def parse_group_delivery(document):
    """Validate a group delivery document read from JSON; normalized copy."""
    if not isinstance(document, dict) \
            or set(document) != {'format', 'records'}:
        raise CorrespondenceError(
            'group delivery requires exactly format and records')
    if document['format'] != GROUP_DELIVERY_FORMAT:
        raise CorrespondenceError('unknown group delivery format')
    records = document['records']
    if not isinstance(records, list):
        raise CorrespondenceError('group delivery records must be a list')
    normalized = []
    seen = set()
    for row in records:
        if not isinstance(row, dict) or set(row) != {'group', 'total'}:
            raise CorrespondenceError(
                'group row requires exactly group and total')
        group = _name(row['group'], 'group')
        if group in seen:
            raise CorrespondenceError('duplicate group delivery row')
        seen.add(group)
        total = row['total']
        if type(total) is not int or total < 0:
            raise CorrespondenceError('group total must be a non-negative integer')
        normalized.append({'group': group, 'total': total})
    normalized.sort(key=lambda row: row['group'])
    return {'format': GROUP_DELIVERY_FORMAT, 'records': normalized}
