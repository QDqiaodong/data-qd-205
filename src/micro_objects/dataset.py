"""Batch compilation of synthetic texture boards into training/blind samples.

One compile run takes 1..8 uniquely numbered boards, each fed through the
unchanged :func:`micro_objects.service.process` pipeline, and up to 1024
crop requests.  Whole-board connectivity and selection are computed once per
board; crops never re-run connected components or edge filtering.

Blind rectangles are reserved up front: their union is hidden from every
training crop (null intensity, ignore supervision), while a blind crop may
read its whole request window.  One calibration record is fitted per batch
from the background cells actually delivered in emitted training crops, and
the same record calibrates every emitted sample and any later, mask-less
intensity matrix through :func:`apply_calibration`.

Only the Python standard library is used.
"""
import copy
import re

from .model import parse
from .service import process

CALIBRATION_FORMAT = 'micro-objects-calibration/1'
POSITIVE = 'positive'
NEGATIVE = 'negative'
BLIND = 'blind'
_KINDS = frozenset((POSITIVE, NEGATIVE, BLIND))

# Reasons reported for training requests that are not emitted.
NO_COMPLETE_OBJECT = 'no_complete_object'
VISIBLE_FOREGROUND = 'visible_foreground'
NO_VISIBLE_BACKGROUND = 'no_visible_background'

_ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_BOARD_FIELDS = frozenset(('board_id', 'data'))
_REQUEST_FIELDS = frozenset(('request_id', 'board_id', 'rect', 'kind'))
_CALIBRATION_FIELDS = frozenset(
    ('format', 'baseline', 'background_cells'))


class DatasetCompileError(ValueError):
    """A batch fails validation/fitting, or a sample is not available."""


def _strict_int(value, name):
    # bool is a subclass of int; the contract rejects booleans as numbers.
    if type(value) is not int:
        raise DatasetCompileError(f'{name} must be an integer')
    return value


def _valid_id(value, name):
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise DatasetCompileError(
            f'{name} must be 1..64 ascii letters, digits, _ or -')
    return value


class _Board:
    __slots__ = ('board_id', 'width', 'height', 'intensity', 'labels',
                 'order', 'components', 'reserved')

    def __init__(self, board_id, width, height, intensity, labels,
                 components, order):
        self.board_id = board_id
        self.width = width
        self.height = height
        # Frozen planes built from fresh parsed/processed structures, so the
        # caller mutating the original input dictionaries cannot reach them.
        self.intensity = intensity              # tuple[tuple[int]]
        self.labels = labels                    # tuple[tuple[int]], 0 = bg
        self.components = components            # label -> component dict
        self.order = order                      # labels in raster order
        self.reserved = bytearray(width * height)


def _build_board(entry):
    """Validate one board entry and run the original pipeline exactly once."""
    if not isinstance(entry, dict) or set(entry) != _BOARD_FIELDS:
        raise DatasetCompileError(
            f'board requires fields {sorted(_BOARD_FIELDS)}')
    board_id = _valid_id(entry['board_id'], 'board_id')
    data = entry['data']
    if not isinstance(data, dict):
        raise DatasetCompileError('board data must be a process() input object')
    try:
        # parse() validates the raw process input and builds fresh immutable
        # intensity tuples; process() performs connectivity and selection.
        mask, intensity, _size, _conn, _minimum, _exclude = parse(data)
        result = process(data)
    except ValueError as exc:
        raise DatasetCompileError(f'board {board_id!r}: {exc}') from exc
    height = len(mask)
    width = len(mask[0])
    labels = tuple(tuple(row) for row in result['labels'])
    components = {}
    for obj in result['objects']:
        label = obj['label']
        points = tuple((x, y) for x, y in obj['pixels'])
        pixels = frozenset(points)
        xs = [x for x, _y in points]
        ys = [y for _x, y in points]
        components[label] = {
            'first': points[0],               # raster-first foreground pixel
            'pixels': pixels,
            'bbox': (min(xs), min(ys), max(xs) + 1, max(ys) + 1),
            'accepted': bool(obj['accepted']),
        }
    return _Board(board_id, width, height, intensity, labels,
                  components, tuple(range(1, len(components) + 1)))


def _validate_request(raw, boards):
    if not isinstance(raw, dict) or set(raw) != _REQUEST_FIELDS:
        raise DatasetCompileError(
            f'request requires fields {sorted(_REQUEST_FIELDS)}')
    request_id = _valid_id(raw['request_id'], 'request_id')
    board_id = raw['board_id']
    if not isinstance(board_id, str) or board_id not in boards:
        raise DatasetCompileError(
            f'request {request_id!r} references an unknown board')
    kind = raw['kind']
    if kind not in _KINDS:
        raise DatasetCompileError(
            f'request {request_id!r} kind must be one of {sorted(_KINDS)}')
    rect = raw['rect']
    if not isinstance(rect, list) or len(rect) != 4:
        raise DatasetCompileError(
            f'request {request_id!r} rect must be [x0, y0, x1, y1]')
    x0, y0, x1, y1 = (_strict_int(value, 'rect coordinate') for value in rect)
    board = boards[board_id]
    if not (0 <= x0 < x1 <= board.width and 0 <= y0 < y1 <= board.height):
        raise DatasetCompileError(
            f'request {request_id!r} rect must be non-empty and fully inside '
            f'board {board_id!r}')
    return request_id, board_id, (x0, y0, x1, y1), kind


def _scan_visible(board, rect, use_reserved):
    """Return (has_visible_foreground, has_visible_background)."""
    x0, y0, x1, y1 = rect
    width = board.width
    has_fg = False
    has_bg = False
    for y in range(y0, y1):
        row = board.labels[y]
        base = y * width
        for x in range(x0, x1):
            if use_reserved and board.reserved[base + x]:
                continue
            if row[x]:
                has_fg = True
            else:
                has_bg = True
    return has_fg, has_bg


def _complete_labels(board, rect, use_reserved):
    """Accepted whole-board objects whose every pixel is visible in the crop."""
    x0, y0, x1, y1 = rect
    width = board.width
    complete = []
    for label in board.order:
        comp = board.components[label]
        if not comp['accepted']:
            continue
        bx0, by0, bx1, by1 = comp['bbox']
        if not (x0 <= bx0 and bx1 <= x1 and y0 <= by0 and by1 <= y1):
            continue
        if use_reserved:
            hidden = any(board.reserved[y * width + x]
                         for x, y in comp['pixels'])
            if hidden:
                continue
        complete.append(label)
    return complete


class CompiledDataset:
    """Read-side handle for one successfully compiled batch.

    Instances are created only by :func:`compile_dataset`, after the whole
    batch has been validated, emission decisions and the calibration fit have
    all succeeded.  Retained data grows with the total board pixels and the
    number of requests, never with the sum of all crop areas: samples are
    rendered on demand into one crop-sized temporary grid each.
    """

    def __init__(self, boards, requests, not_emitted, calibration):
        self._boards = boards                       # board_id -> _Board
        self._requests = requests                   # id -> (board_id, rect, kind)
        self._not_emitted = not_emitted             # id -> reason
        self._calibration = calibration

    # -- introspection -----------------------------------------------------

    def request_ids(self):
        """All request ids, sorted; emission state is available separately."""
        return sorted(self._requests)

    def emitted_ids(self):
        """Sorted ids of requests whose samples can be read."""
        return sorted(rid for rid in self._requests
                      if rid not in self._not_emitted)

    @property
    def not_emitted(self):
        """Copy of the ``request_id -> reason`` map for withheld training."""
        return dict(self._not_emitted)

    @property
    def calibration(self):
        """Deep copy of the independent calibration record."""
        return copy.deepcopy(self._calibration)

    def storage_stats(self):
        """Size of the retained compiler state, in input-sized units.

        ``retained_crop_grids`` is always zero: crop rasters exist only while
        :meth:`get_sample` is running.  The state grows with board pixels and
        request count, not with the (potentially huge) sum of crop areas.
        """
        return {
            'boards': len(self._boards),
            'board_pixels': sum(b.width * b.height for b in
                                self._boards.values()),
            'requests': len(self._requests),
            'reserved_cells': sum(sum(b.reserved) for b in
                                  self._boards.values()),
            'retained_crop_grids': 0,
            'retained_crop_cells': 0,
        }

    # -- sample reading ----------------------------------------------------

    def get_sample(self, request_id):
        """Render one emitted sample as fresh, independent lists.

        Raises :class:`KeyError` for an unknown id and
        :class:`DatasetCompileError` for a known request that was not emitted.
        """
        try:
            board_id, rect, kind = self._requests[request_id]
        except KeyError:
            raise KeyError(request_id) from None
        reason = self._not_emitted.get(request_id)
        if reason is not None:
            raise DatasetCompileError(
                f'request {request_id!r} was not emitted: {reason}')
        board = self._boards[board_id]
        blind = kind == BLIND
        use_reserved = not blind
        complete = _complete_labels(board, rect, use_reserved)
        crop_number = {label: index + 1
                       for index, label in enumerate(complete)}
        baseline = self._calibration['baseline']
        x0, y0, x1, y1 = rect
        width = board.width
        intensity_rows = []
        supervision_rows = []
        for y in range(y0, y1):
            label_row = board.labels[y]
            value_row = board.intensity[y]
            base = y * width
            intensity_cells = []
            supervision_cells = []
            for x in range(x0, x1):
                if use_reserved and board.reserved[base + x]:
                    # Reserved training cells leak neither raw intensity nor
                    # the original mask; blind crops never take this branch.
                    intensity_cells.append(None)
                    supervision_cells.append(-1)
                    continue
                raw = value_row[x]
                intensity_cells.append(raw - baseline)
                label = label_row[x]
                if label == 0:
                    supervision_cells.append(0)
                else:
                    number = crop_number.get(label)
                    supervision_cells.append(
                        number if number is not None else -1)
            intensity_rows.append(intensity_cells)
            supervision_rows.append(supervision_cells)
        objects = [{
            'label': crop_number[label],
            'board_id': board_id,
            'first_pixel': list(board.components[label]['first']),
        } for label in complete]
        return {
            'request_id': request_id,
            'board_id': board_id,
            'kind': kind,
            'rect': list(rect),
            'intensity': intensity_rows,
            'supervision': supervision_rows,
            'objects': objects,
        }


def compile_dataset(boards, requests):
    """Validate and compile one batch; return a :class:`CompiledDataset`.

    ``boards`` is 1..8 dicts ``{"board_id", "data"}`` with unique ids and
    raw :func:`micro_objects.process` inputs.  ``requests`` is 1..1024 dicts
    ``{"request_id", "board_id", "rect", "kind"}``; ids are unique across the
    whole batch, rects are half-open ``[x0, y0, x1, y1]`` whole-board pixel
    rectangles and kinds are :data:`POSITIVE`, :data:`NEGATIVE` or
    :data:`BLIND`.  At least one blind and one training request are required.

    Validation, emission decisions and the calibration fit all finish before
    any readable result is returned.  A batch with no delivered training
    background fails wholesale, including its blind samples.  Bad fields,
    duplicate ids, unknown boards, out-of-bounds rects and bad board data
    raise :class:`DatasetCompileError` (a :class:`ValueError`).
    """
    if not isinstance(boards, list) or not 1 <= len(boards) <= 8:
        raise DatasetCompileError('a batch carries 1..8 boards')
    compiled_boards = {}
    for entry in boards:
        board = _build_board(entry)
        if board.board_id in compiled_boards:
            raise DatasetCompileError(
                f'duplicate board id {board.board_id!r}')
        compiled_boards[board.board_id] = board

    if not isinstance(requests, list) or not 1 <= len(requests) <= 1024:
        raise DatasetCompileError('a batch carries 1..1024 requests')
    normalized = {}
    has_blind = False
    has_training = False
    for raw in requests:
        request_id, board_id, rect, kind = _validate_request(
            raw, compiled_boards)
        if request_id in normalized:
            raise DatasetCompileError(
                f'duplicate request id {request_id!r}')
        normalized[request_id] = (board_id, rect, kind)
        if kind == BLIND:
            has_blind = True
        else:
            has_training = True
    if not has_blind:
        raise DatasetCompileError('at least one blind request is required')
    if not has_training:
        raise DatasetCompileError('at least one training request is required')

    # The blind union is reserved independently of whether any other crop is
    # ever emitted; overlapping blind rects simply merge.
    for request_id, (board_id, rect, kind) in normalized.items():
        if kind != BLIND:
            continue
        board = compiled_boards[board_id]
        x0, y0, x1, y1 = rect
        for y in range(y0, y1):
            start = y * board.width + x0
            board.reserved[start:start + (x1 - x0)] = b'\x01' * (x1 - x0)

    # Emission decisions.  Blind crops always emit, even when fully ignored.
    not_emitted = {}
    for request_id in sorted(normalized):
        board_id, rect, kind = normalized[request_id]
        board = compiled_boards[board_id]
        if kind == BLIND:
            continue
        has_fg, has_bg = _scan_visible(board, rect, True)
        if kind == NEGATIVE:
            # Foreground is reported first when both problems co-occur.
            if has_fg:
                not_emitted[request_id] = VISIBLE_FOREGROUND
            elif not has_bg:
                not_emitted[request_id] = NO_VISIBLE_BACKGROUND
        elif not _complete_labels(board, rect, True):
            not_emitted[request_id] = NO_COMPLETE_OBJECT

    # One calibration record per batch: background cells of emitted training
    # crops only, deduplicated by (board, whole-board coordinate).
    values = []
    for board in compiled_boards.values():
        seen = bytearray(board.width * board.height)
        width = board.width
        for request_id, (board_id, rect, kind) in normalized.items():
            if board_id != board.board_id or request_id in not_emitted:
                continue
            x0, y0, x1, y1 = rect
            for y in range(y0, y1):
                label_row = board.labels[y]
                value_row = board.intensity[y]
                base = y * width
                for x in range(x0, x1):
                    offset = base + x
                    if board.reserved[offset] or label_row[x] or seen[offset]:
                        continue
                    seen[offset] = 1
                    values.append(value_row[x])
    if not values:
        raise DatasetCompileError(
            'no training background delivered; the whole batch is rejected')
    values.sort()
    baseline = values[(len(values) - 1) // 2]      # lower median
    calibration = {
        'format': CALIBRATION_FORMAT,
        'baseline': baseline,
        'background_cells': len(values),
    }
    return CompiledDataset(compiled_boards, normalized, not_emitted,
                           calibration)


def parse_calibration(record):
    """Validate a calibration record read back from storage.

    Returns a normalized fresh dict.  A malformed record raises
    :class:`DatasetCompileError`.
    """
    if not isinstance(record, dict) or set(record) != _CALIBRATION_FIELDS:
        raise DatasetCompileError(
            f'calibration requires fields {sorted(_CALIBRATION_FIELDS)}')
    if record['format'] != CALIBRATION_FORMAT:
        raise DatasetCompileError('unknown calibration format')
    baseline = _strict_int(record['baseline'], 'baseline')
    if not 0 <= baseline <= 65535:
        raise DatasetCompileError('baseline must be an integer in 0..65535')
    count = _strict_int(record['background_cells'], 'background_cells')
    if count < 1:
        raise DatasetCompileError('background_cells must be positive')
    return {'format': CALIBRATION_FORMAT,
            'baseline': baseline,
            'background_cells': count}


def apply_calibration(record, matrix):
    """Apply a read-back calibration record to a new intensity matrix.

    ``matrix`` is a non-empty rectangular list of rows whose cells are
    integers in the original 0..65535 range or ``None``.  No mask is needed
    and no refit happens: each integer becomes ``value - baseline`` (negative
    results are kept, nothing is clipped), ``None`` stays ``None``.
    """
    parsed = parse_calibration(record)
    baseline = parsed['baseline']
    if not isinstance(matrix, list) or not matrix:
        raise DatasetCompileError('intensity matrix must be a non-empty list')
    width = None
    output = []
    for row in matrix:
        if not isinstance(row, list) or not row:
            raise DatasetCompileError(
                'intensity matrix rows must be non-empty lists')
        if width is None:
            width = len(row)
        elif len(row) != width:
            raise DatasetCompileError('intensity matrix must be rectangular')
        calibrated = []
        for cell in row:
            if cell is None:
                calibrated.append(None)
                continue
            if type(cell) is not int or not 0 <= cell <= 65535:
                raise DatasetCompileError(
                    'intensity cells must be integers in 0..65535 or null')
            calibrated.append(cell - baseline)
        output.append(calibrated)
    return output
