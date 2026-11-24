"""Offline probability sampling, field packages, receipts and HT totals.

The design space is the set of feasible *K*-window subsets.  Counting,
unranking and inclusion probabilities use bounded dynamic programs; the
implementation never materializes the list of feasible schemes.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from fractions import Fraction
from pathlib import Path

from .service import process

SURVEY_DESIGN_FORMAT = "micro_objects.survey_design"
TASK_PACKAGE_FORMAT = "micro_objects.survey_task_package"
SURVEY_RECEIPT_FORMAT = "micro_objects.survey_receipt"
SURVEY_ESTIMATE_FORMAT = "micro_objects.survey_estimate"
SCHEMA_VERSION = 1

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")


class SurveyError(ValueError):
    """Base class for all sampling-chain contract errors."""


class SurveyDesignError(SurveyError):
    """Board, candidate windows, K, B or rank do not form a usable design."""


class TaskPackageError(SurveyError):
    """A task package is malformed, inconsistent or no longer usable."""


class ReceiptError(SurveyError):
    """A receipt is missing, extra, malformed or does not match its package."""


class ReceiptConflictError(ReceiptError):
    """The same object was reported with different readings."""


def _json_pairs_hook(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SurveyError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise SurveyError(f"non-finite JSON value is not allowed: {value}")


def _read_json_file(path, error_cls=SurveyError, limit=8_000_000):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as exc:
        raise error_cls(f"cannot read JSON document {path}: {exc}") from exc
    if len(raw) > limit:
        raise error_cls("JSON document is too large")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_pairs_hook,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise error_cls(f"invalid JSON document: {exc}") from exc


def _atomic_write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=".survey-", suffix=".json", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _strict_int(value, low, high, what, error_cls=SurveyDesignError):
    # ``type(True) is bool``, so booleans are deliberately rejected here.
    if type(value) is not int or not low <= value <= high:
        raise error_cls(
            f"{what} must be an integer in [{low}, {high}]; "
            "booleans are not integers"
        )
    return value


def _exact_fields(obj, required, optional, what, error_cls):
    if not isinstance(obj, dict):
        raise error_cls(f"{what} must be a JSON object")
    allowed = set(required) | set(optional)
    missing = set(required) - obj.keys()
    unknown = set(obj.keys()) - allowed
    if missing:
        raise error_cls(f"{what} is missing fields: {sorted(missing)}")
    if unknown:
        raise error_cls(f"{what} has illegal fields: {sorted(unknown)}")


def _parse_object_key(value, what="object key", error_cls=SurveyDesignError):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise error_cls(f"{what} must be a two-item [x, y] array")
    x, y = value
    if type(x) is not int or type(y) is not int:
        raise error_cls(f"{what} coordinates must be integers; booleans are invalid")
    return (x, y)


def _parse_rect(value, width, height, what="rectangle", error_cls=SurveyDesignError):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise error_cls(f"{what} must be [x0, y0, x1, y1]")
    x0, y0, x1, y1 = value
    for name, coordinate, upper in (
        ("x0", x0, width),
        ("y0", y0, height),
        ("x1", x1, width),
        ("y1", y1, height),
    ):
        _strict_int(coordinate, 0, upper, f"{what}.{name}", error_cls)
    if x0 >= x1 or y0 >= y1:
        raise error_cls(f"{what} must be a non-empty half-open rectangle")
    return (x0, y0, x1, y1)


def _as_key(value, design=None, error_cls=SurveyDesignError):
    """Resolve a key/index/string to a canonical ``(x, y)`` key."""
    if type(value) is int and design is not None:
        if not 0 <= value < len(design.objects):
            raise error_cls(f"object index {value} is outside the population")
        return design.objects[value].key
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise error_cls(f"invalid object key {value!r}") from exc
            return _parse_object_key(loaded, error_cls=error_cls)
        parts = text.split(",")
        if len(parts) == 2:
            try:
                return (int(parts[0].strip()), int(parts[1].strip()))
            except ValueError as exc:
                raise error_cls(f"invalid object key {value!r}") from exc
    key = _parse_object_key(value, error_cls=error_cls)
    if design is not None and key not in design.key_index:
        raise error_cls(f"object {list(key)} is not in the surveyed population")
    return key


def _bbox_contains(rect, bbox):
    x0, y0, x1, y1 = rect
    bx0, by0, bx1, by1 = bbox
    return x0 <= bx0 and y0 <= by0 and x1 >= bx1 and y1 >= by1


class ObjectSpec:
    __slots__ = ("key", "bbox")

    def __init__(self, key, bbox):
        self.key = tuple(key)
        self.bbox = tuple(bbox)

    def to_document(self):
        return {"key": list(self.key), "bbox": list(self.bbox)}


class WindowSpec:
    __slots__ = ("window_id", "rect", "cost", "object_indices", "coverage_mask")

    def __init__(self, window_id, rect, cost, object_indices, object_count):
        self.window_id = window_id
        self.rect = tuple(rect)
        self.cost = cost
        self.object_indices = tuple(object_indices)
        mask = 0
        for index in self.object_indices:
            mask |= 1 << index
        self.coverage_mask = mask

    def to_document(self, objects):
        return {
            "id": self.window_id,
            "rect": list(self.rect),
            "cost": self.cost,
            "object_keys": [list(objects[i].key) for i in self.object_indices],
        }


class SurveyDesign(dict):
    """Canonical, board-free sampling design with exact probability queries."""

    def __init__(
        self,
        *,
        board_width,
        board_height,
        k,
        budget,
        objects,
        windows,
        error_cls=SurveyDesignError,
    ):
        self.board_width = board_width
        self.board_height = board_height
        self.k = k
        self.budget = budget
        self.objects = tuple(objects)
        self.windows = tuple(sorted(windows, key=lambda item: item.window_id))
        if len({w.window_id for w in self.windows}) != len(self.windows):
            raise error_cls("window IDs must be unique")
        self.key_index = {obj.key: i for i, obj in enumerate(self.objects)}
        self.window_index = {w.window_id: i for i, w in enumerate(self.windows)}
        self.window_by_id = {w.window_id: w for w in self.windows}

        costs = [w.cost for w in self.windows]
        self._suffix = _build_budget_suffix(costs, k, budget)
        self.scheme_count = self._suffix[0][k][budget]

        m = len(self.windows)
        all_windows = (1 << m) - 1
        self._missing_masks = []
        for i in range(len(self.objects)):
            bit = 1 << i
            mask = 0
            for wi, window in enumerate(self.windows):
                if not (window.coverage_mask & bit):
                    mask |= 1 << wi
            self._missing_masks.append(mask & all_windows)
        self._count_cache = {}
        self._unsupported_pairs = None
        dict.__init__(
            self,
            format=SURVEY_DESIGN_FORMAT,
            schema_version=SCHEMA_VERSION,
            board_width=board_width,
            board_height=board_height,
            k=k,
            budget=budget,
            scheme_count=self.scheme_count,
            N=self.scheme_count,
            object_keys=[list(obj.key) for obj in self.objects],
            window_ids=[w.window_id for w in self.windows],
        )

    @property
    def N(self):
        return self.scheme_count

    @property
    def n_schemes(self):
        return self.scheme_count

    @property
    def object_keys(self):
        return [obj.key for obj in self.objects]

    def p_i(self, obj):
        return self.inclusion_probability(obj)

    def p_ij(self, first, second=None):
        return self.joint_probability(first, second)

    # ----- combinatorial core -------------------------------------------
    def _count_allowed(self, allowed_mask):
        """Count feasible K-subsets using only windows in ``allowed_mask``."""
        cached = self._count_cache.get(allowed_mask)
        if cached is not None:
            return cached

        k = self.k
        budget = self.budget
        dp = [[0] * (budget + 1) for _ in range(k + 1)]
        dp[0][0] = 1
        available = 0
        mask = allowed_mask
        while mask:
            low_bit = mask & -mask
            wi = low_bit.bit_length() - 1
            mask -= low_bit
            cost = self.windows[wi].cost
            if cost > budget:
                continue
            upper_k = min(k, available + 1)
            for chosen in range(upper_k, 0, -1):
                row = dp[chosen]
                previous = dp[chosen - 1]
                for total_cost in range(budget, cost - 1, -1):
                    row[total_cost] += previous[total_cost - cost]
            available += 1

        result = sum(dp[k])
        self._count_cache[allowed_mask] = result
        return result

    def scheme_for_rank(self, rank):
        """Return the canonical tuple of window IDs at zero-based ``rank``."""
        if type(rank) is not int:
            raise SurveyDesignError("rank must be an integer; booleans are invalid")
        if not 0 <= rank < self.scheme_count:
            raise SurveyDesignError(
                f"rank {rank!r} is outside [0, {self.scheme_count})"
            )

        m = len(self.windows)
        chosen = []
        position = 0
        remaining = self.k
        spent = 0
        suffix = self._suffix

        while remaining:
            last_candidate = m - remaining
            progressed = False
            for wi in range(position, last_candidate + 1):
                limit = self.budget - spent - self.windows[wi].cost
                if limit < 0:
                    block = 0
                else:
                    block = suffix[wi + 1][remaining - 1][limit]
                if rank >= block:
                    rank -= block
                else:
                    chosen.append(wi)
                    spent += self.windows[wi].cost
                    position = wi + 1
                    remaining -= 1
                    progressed = True
                    break
            if not progressed:  # defensive; N and rank were checked
                raise SurveyDesignError("rank does not resolve to a feasible scheme")
        return tuple(self.windows[i].window_id for i in chosen)

    # ----- probability API ----------------------------------------------
    def _object_index(self, obj, error_cls=SurveyDesignError):
        if type(obj) is int:
            if not 0 <= obj < len(self.objects):
                raise error_cls(f"object index {obj} is outside the population")
            return obj
        key = _as_key(obj, self, error_cls)
        return self.key_index[key]

    def inclusion_count(self, obj):
        i = self._object_index(obj)
        missing = self._count_allowed(self._missing_masks[i])
        return self.scheme_count - missing

    def inclusion_probability(self, obj):
        """Exact ``p_i`` as a :class:`fractions.Fraction`."""
        return Fraction(self.inclusion_count(obj), self.scheme_count)

    def joint_count(self, first, second=None):
        if second is None and isinstance(first, (tuple, list)) and len(first) == 2:
            first, second = first
        i = self._object_index(first)
        j = self._object_index(second)
        if i == j:
            return self.inclusion_count(i)
        miss_i = self._count_allowed(self._missing_masks[i])
        miss_j = self._count_allowed(self._missing_masks[j])
        miss_both = self._count_allowed(
            self._missing_masks[i] & self._missing_masks[j]
        )
        return self.scheme_count - miss_i - miss_j + miss_both

    def joint_probability(self, first, second=None):
        """Exact ``p_ij``; ``p_ii = p_i``."""
        if second is None and isinstance(first, (tuple, list)) and len(first) == 2:
            first, second = first
        return Fraction(self.joint_count(first, second), self.scheme_count)

    def unsupported_pairs(self):
        """Population index pairs whose joint inclusion probability is zero."""
        if self._unsupported_pairs is None:
            bad = []
            n = len(self.objects)
            for i in range(n):
                for j in range(i + 1, n):
                    if self.joint_count(i, j) == 0:
                        bad.append((i, j))
            self._unsupported_pairs = tuple(bad)
        return self._unsupported_pairs

    @property
    def variance_supported(self):
        return not self.unsupported_pairs()

    # ----- serialization -------------------------------------------------
    def to_document(self):
        return {
            "format": SURVEY_DESIGN_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "board_width": self.board_width,
            "board_height": self.board_height,
            "k": self.k,
            "budget": self.budget,
            "scheme_count": self.scheme_count,
            "objects": [obj.to_document() for obj in self.objects],
            "windows": [w.to_document(self.objects) for w in self.windows],
        }

    def probability_document(self):
        """Optional exact report of all p_i and unordered p_ij values."""
        inclusion = [
            {
                "object_key": list(obj.key),
                "inclusion_count": self.inclusion_count(i),
                "probability": str(self.inclusion_probability(i)),
            }
            for i, obj in enumerate(self.objects)
        ]
        joint = []
        n = len(self.objects)
        for i in range(n):
            for j in range(i, n):
                joint.append(
                    {
                        "object_keys": [
                            list(self.objects[i].key),
                            list(self.objects[j].key),
                        ],
                        "joint_count": self.joint_count(i, j),
                        "probability": str(self.joint_probability(i, j)),
                    }
                )
        return {
            "format": "micro_objects.survey_probabilities",
            "schema_version": SCHEMA_VERSION,
            "scheme_count": self.scheme_count,
            "probability_encoding": "Fraction canonical string, e.g. 4/5 or 15",
            "inclusion": inclusion,
            "joint": joint,
        }


def _build_budget_suffix(costs, k_max, budget):
    """``table[pos][chosen][limit]`` = number of subsets from ``pos:``.

    The subset has exactly ``chosen`` windows and total cost at most
    ``limit``.  This direct <= budget recurrence makes rank block counts O(1).
    """
    m = len(costs)
    table = [[[0] * (budget + 1) for _ in range(k_max + 1)] for _ in range(m + 1)]
    for limit in range(budget + 1):
        table[m][0][limit] = 1

    for pos in range(m - 1, -1, -1):
        cost = costs[pos]
        current = table[pos]
        following = table[pos + 1]
        for limit in range(budget + 1):
            current[0][limit] = 1
            upper_k = min(k_max, m - pos)
            for chosen in range(1, upper_k + 1):
                value = following[chosen][limit]  # skip this window
                if cost <= limit:
                    value += following[chosen - 1][limit - cost]  # take it
                current[chosen][limit] = value
    return table


def _compiled_board(board):
    if isinstance(board, SurveyDesign):  # helpful misuse guard
        raise SurveyDesignError("a SurveyDesign is not board data")
    if isinstance(board, dict) and "mask" in board:
        try:
            return process(board)
        except SurveyError:
            raise
        except Exception as exc:
            raise SurveyDesignError(f"invalid process(data) board: {exc}") from exc
    if (
        isinstance(board, dict)
        and isinstance(board.get("labels"), list)
        and "objects" in board
    ):
        return board
    raise SurveyDesignError(
        "board must be process(data) input or the result of process(data)"
    )


_ALLOWED_WINDOW_KEYS = {
    "id",
    "window_id",
    "rect",
    "rectangle",
    "bounds",
    "cost",
    "work_cost",
    "work_minutes",
    "work_hours",
    "hours",
    "effort",
    "workload",
}
_ID_KEYS = ("id", "window_id")
_RECT_KEYS = ("rect", "rectangle", "bounds")
_COST_KEYS = (
    "cost",
    "work_cost",
    "work_minutes",
    "work_hours",
    "hours",
    "effort",
    "workload",
)


def _alias_value(item, names, what, error_cls):
    present = [name for name in names if name in item]
    if len(present) != 1:
        raise error_cls(f"{what} must define exactly one of {names}")
    return item[present[0]]


def _parse_window_input(item, width, height, index):
    what = f"window[{index}]"
    if isinstance(item, (list, tuple)) and len(item) == 3:
        window_id, rect_value, cost_value = item
    elif isinstance(item, dict):
        unknown = set(item) - _ALLOWED_WINDOW_KEYS
        if unknown:
            raise SurveyDesignError(f"{what} has illegal fields: {sorted(unknown)}")
        window_id = _alias_value(item, _ID_KEYS, what, SurveyDesignError)
        rect_value = _alias_value(item, _RECT_KEYS, what, SurveyDesignError)
        cost_value = _alias_value(item, _COST_KEYS, what, SurveyDesignError)
    else:
        raise SurveyDesignError(f"{what} must be an object or [id, rect, cost]")

    if not isinstance(window_id, str) or not _ID_RE.fullmatch(window_id):
        raise SurveyDesignError(
            f"{what}.id must be 1..32 ASCII letters, digits, '-' or '_'"
        )
    rect = _parse_rect(rect_value, width, height, f"{what}.rect")
    cost = _strict_int(cost_value, 1, 16, f"{what}.cost")
    return window_id, rect, cost


def design_survey(board, windows, k=None, budget=None, *, K=None, B=None):
    """Compile and validate the equal-probability feasible-scheme design.

    ``K``/``B`` are accepted as uppercase keyword aliases for ``k``/``budget``.

    Raises :class:`SurveyDesignError` when there is no feasible scheme or an
    accepted board object is absent from every feasible scheme.  On success
    the returned design exposes ``scheme_count`` and probability queries.
    """
    if K is not None:
        if k is not None:
            raise SurveyDesignError("pass K either positionally or as K, not both")
        k = K
    if B is not None:
        if budget is not None:
            raise SurveyDesignError("pass B either positionally or as B, not both")
        budget = B
    if k is None or budget is None:
        raise SurveyDesignError("both K and B are required")

    k = _strict_int(k, 1, 8, "K")
    budget = _strict_int(budget, 1, 64, "budget B")
    if not isinstance(windows, (list, tuple)):
        raise SurveyDesignError("windows must be a list of 1..32 window definitions")
    _strict_int(len(windows), 1, 32, "window count")

    compiled = _compiled_board(board)
    labels = compiled["labels"]
    height = len(labels)
    width = len(labels[0]) if height else 0

    parsed_windows = [
        _parse_window_input(item, width, height, i)
        for i, item in enumerate(windows)
    ]
    if len({wid for wid, _, _ in parsed_windows}) != len(parsed_windows):
        raise SurveyDesignError("window IDs must be unique")

    objects = []
    for obj in compiled.get("objects", []):
        if not obj.get("accepted"):
            continue
        pixels = obj.get("pixels")
        if not pixels:
            raise SurveyDesignError("accepted object has no first pixel")
        first = pixels[0]
        key = (int(first[0]), int(first[1]))
        objects.append(ObjectSpec(key, obj["bbox"]))
    objects.sort(key=lambda item: (item.key[1], item.key[0]))
    if len({o.key for o in objects}) != len(objects):
        raise SurveyDesignError("accepted object first-pixel keys must be unique")
    _strict_int(len(objects), 1, 64, "accepted object count")

    specs = []
    for window_id, rect, cost in parsed_windows:
        indices = [
            i for i, obj in enumerate(objects) if _bbox_contains(rect, obj.bbox)
        ]
        specs.append(WindowSpec(window_id, rect, cost, indices, len(objects)))

    design = SurveyDesign(
        board_width=width,
        board_height=height,
        k=k,
        budget=budget,
        objects=objects,
        windows=specs,
    )
    if design.scheme_count <= 0:
        raise SurveyDesignError("design has no feasible K-window scheme")
    for i, obj in enumerate(objects):
        if design.inclusion_count(i) <= 0:
            raise SurveyDesignError(
                f"object {list(obj.key)} is not measurable in any feasible scheme"
            )
    return design


# A descriptive alias for callers looking for an inspection entry point.
def inspect_survey_design(board, windows, k, budget):
    """Same validation/design entry as :func:`design_survey`."""
    return design_survey(board, windows, k, budget)


# ---------------------------------------------------------------------------
# Task packages
# ---------------------------------------------------------------------------


class TaskPackage(dict):
    """JSON-document mapping plus a board-free :class:`SurveyDesign`."""

    def __init__(self, document, design):
        super().__init__(document)
        self.design = design

    def save(self, path):
        save_task_package(path, self)
        return path


def issue_task_package(design, rank):
    if isinstance(design, dict) and not isinstance(design, SurveyDesign):
        design = parse_design(design)
    if not isinstance(design, SurveyDesign):
        raise SurveyDesignError("design must be a SurveyDesign instance or document")
    window_ids = design.scheme_for_rank(rank)
    tasks = []
    for window_id in window_ids:
        window = design.window_by_id[window_id]
        tasks.append(
            {
                "window_id": window_id,
                "rect": list(window.rect),
                "cost": window.cost,
                "objects": [
                    list(design.objects[i].key) for i in window.object_indices
                ],
            }
        )
    document = {
        "format": TASK_PACKAGE_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "rank": rank,
        "design": design.to_document(),
        "window_ids": list(window_ids),
        "tasks": tasks,
    }
    return TaskPackage(document, design)


def _parse_design_document(doc, error_cls):
    _exact_fields(
        doc,
        (
            "format",
            "schema_version",
            "board_width",
            "board_height",
            "k",
            "budget",
            "scheme_count",
            "objects",
            "windows",
        ),
        (),
        "design",
        error_cls,
    )
    if doc["format"] != SURVEY_DESIGN_FORMAT:
        raise error_cls("unsupported survey design format")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise error_cls("unsupported survey design schema version")

    width = _strict_int(doc["board_width"], 1, 256, "board_width", error_cls)
    height = _strict_int(doc["board_height"], 1, 256, "board_height", error_cls)
    k = _strict_int(doc["k"], 1, 8, "k", error_cls)
    budget = _strict_int(doc["budget"], 1, 64, "budget", error_cls)
    claimed_n = _strict_int(
        doc["scheme_count"], 0, 100_000_000, "scheme_count", error_cls
    )

    raw_objects = doc["objects"]
    if not isinstance(raw_objects, list):
        raise error_cls("design.objects must be a list")
    _strict_int(len(raw_objects), 1, 64, "design.objects count", error_cls)
    objects = []
    seen_keys = set()
    for pos, raw in enumerate(raw_objects):
        what = f"design.objects[{pos}]"
        _exact_fields(raw, ("key", "bbox"), (), what, error_cls)
        key = _parse_object_key(raw["key"], f"{what}.key", error_cls)
        x, y = key
        if not 0 <= x < width or not 0 <= y < height:
            raise error_cls(f"{what}.key is outside the board")
        if key in seen_keys:
            raise error_cls(f"duplicate object key {list(key)}")
        seen_keys.add(key)
        bbox = _parse_rect(raw["bbox"], width, height, f"{what}.bbox", error_cls)
        if not (bbox[0] <= x < bbox[2] and bbox[1] <= y < bbox[3]):
            raise error_cls(f"{what}.key is not inside its bbox")
        objects.append(ObjectSpec(key, bbox))
    objects.sort(key=lambda item: (item.key[1], item.key[0]))
    key_index = {obj.key: i for i, obj in enumerate(objects)}

    raw_windows = doc["windows"]
    if not isinstance(raw_windows, list):
        raise error_cls("design.windows must be a list")
    _strict_int(len(raw_windows), 1, 32, "design.windows count", error_cls)
    windows = []
    seen_ids = set()
    for pos, raw in enumerate(raw_windows):
        what = f"design.windows[{pos}]"
        _exact_fields(
            raw, ("id", "rect", "cost", "object_keys"), (), what, error_cls
        )
        window_id = raw["id"]
        if not isinstance(window_id, str) or not _ID_RE.fullmatch(window_id):
            raise error_cls(f"{what}.id is invalid")
        if window_id in seen_ids:
            raise error_cls(f"duplicate window id {window_id!r}")
        seen_ids.add(window_id)
        rect = _parse_rect(raw["rect"], width, height, f"{what}.rect", error_cls)
        cost = _strict_int(raw["cost"], 1, 16, f"{what}.cost", error_cls)

        supplied = raw["object_keys"]
        if not isinstance(supplied, list):
            raise error_cls(f"{what}.object_keys must be a list")
        supplied_indices = set()
        for key_value in supplied:
            key = _parse_object_key(
                key_value, f"{what}.object_keys", error_cls
            )
            if key not in key_index:
                raise error_cls(
                    f"{what} references unknown object {list(key)}"
                )
            idx = key_index[key]
            if idx in supplied_indices:
                raise error_cls(f"{what} repeats object {list(key)}")
            supplied_indices.add(idx)
        expected_indices = {
            i for i, obj in enumerate(objects) if _bbox_contains(rect, obj.bbox)
        }
        if supplied_indices != expected_indices:
            raise error_cls(
                f"{what}.object_keys does not match complete bbox containment"
            )
        windows.append(
            WindowSpec(window_id, rect, cost, sorted(expected_indices), len(objects))
        )

    design = SurveyDesign(
        board_width=width,
        board_height=height,
        k=k,
        budget=budget,
        objects=objects,
        windows=windows,
        error_cls=error_cls,
    )
    if design.scheme_count != claimed_n:
        raise error_cls(
            f"scheme_count {claimed_n} does not match recomputed "
            f"{design.scheme_count}"
        )
    if design.scheme_count <= 0:
        raise error_cls("design has no feasible K-window scheme")
    for i, obj in enumerate(objects):
        if design.inclusion_count(i) <= 0:
            raise error_cls(
                f"object {list(obj.key)} is not measurable in any feasible scheme"
            )
    return design


def parse_design(document):
    """Validate a board-free design document and return a ``SurveyDesign``."""
    if isinstance(document, SurveyDesign):
        return document
    if not isinstance(document, dict):
        raise SurveyDesignError("design document must be a JSON object")
    return _parse_design_document(document, SurveyDesignError)


def parse_task_package(document):
    """Strictly validate a task package and return a :class:`TaskPackage`."""
    if not isinstance(document, dict):
        raise TaskPackageError("task package must be a JSON object")
    _exact_fields(
        document,
        ("format", "schema_version", "rank", "design", "window_ids", "tasks"),
        (),
        "task package",
        TaskPackageError,
    )
    if document["format"] != TASK_PACKAGE_FORMAT:
        raise TaskPackageError("unsupported task package format")
    if type(document["schema_version"]) is not int or (
        document["schema_version"] != SCHEMA_VERSION
    ):
        raise TaskPackageError("unsupported task package schema version")
    rank = _strict_int(document["rank"], 0, 100_000_000, "rank", TaskPackageError)
    design = _parse_design_document(document["design"], TaskPackageError)
    if rank >= design.scheme_count:
        raise TaskPackageError(
            f"rank {rank} is outside [0, {design.scheme_count})"
        )

    expected_ids = design.scheme_for_rank(rank)
    raw_ids = document["window_ids"]
    if not isinstance(raw_ids, list) or raw_ids != list(expected_ids):
        raise TaskPackageError(
            "window_ids must be the ASCII-sorted scheme for design and rank"
        )

    raw_tasks = document["tasks"]
    if not isinstance(raw_tasks, list) or len(raw_tasks) != design.k:
        raise TaskPackageError("tasks must contain exactly K window instructions")

    seen = set()
    for pos, task in enumerate(raw_tasks):
        what = f"tasks[{pos}]"
        _exact_fields(
            task,
            ("window_id", "rect", "cost", "objects"),
            (),
            what,
            TaskPackageError,
        )
        window_id = task["window_id"]
        if not isinstance(window_id, str) or window_id not in design.window_by_id:
            raise TaskPackageError(f"{what} has unknown window_id {window_id!r}")
        if window_id in seen:
            raise TaskPackageError(f"duplicate task for window {window_id!r}")
        seen.add(window_id)
        window = design.window_by_id[window_id]
        task_rect = _parse_rect(
            task["rect"],
            design.board_width,
            design.board_height,
            f"{what}.rect",
            TaskPackageError,
        )
        if task_rect != window.rect:
            raise TaskPackageError(f"{what}.rect does not match the design")
        if type(task["cost"]) is not int or task["cost"] != window.cost:
            raise TaskPackageError(f"{what}.cost does not match the design")
        raw_objects = task["objects"]
        if not isinstance(raw_objects, list):
            raise TaskPackageError(f"{what}.objects must be a list")
        task_keys = []
        task_seen = set()
        for value in raw_objects:
            key = _parse_object_key(value, f"{what}.objects", TaskPackageError)
            if key not in design.key_index:
                raise TaskPackageError(f"{what} has unknown object {list(key)}")
            if key in task_seen:
                raise TaskPackageError(f"{what} repeats object {list(key)}")
            task_seen.add(key)
            task_keys.append(key)
        expected_keys = [design.objects[i].key for i in window.object_indices]
        if set(task_keys) != set(expected_keys):
            raise TaskPackageError(
                f"{what} does not list exactly the complete objects for "
                f"{window_id}"
            )

    if seen != set(expected_ids):
        raise TaskPackageError("tasks do not exactly match the issued scheme")

    # Return a canonical package so callers cannot retain ordering quirks.
    return issue_task_package(design, rank)


def save_task_package(path, package):
    """Validate and atomically save a task package as canonical JSON."""
    parsed = parse_task_package(package)
    _atomic_write_json(path, dict(parsed))
    return path


def load_task_package(path):
    """Read and strictly validate a task package without reading a board."""
    document = _read_json_file(path, TaskPackageError)
    return parse_task_package(document)


# ---------------------------------------------------------------------------
# Receipts and estimation
# ---------------------------------------------------------------------------


class SurveyEstimate(dict):
    """Estimate mapping whose numeric values are exact Fractions/ints."""

    def __init__(self, document, design, readings):
        super().__init__(document)
        self.design = design
        self.readings = dict(readings)

    def to_document(self):
        return _json_safe(dict(self))

    def save(self, path):
        save_survey_estimate(path, self)
        return path


def _json_safe(value):
    if isinstance(value, Fraction):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def save_survey_estimate(path, estimate):
    if not isinstance(estimate, SurveyEstimate):
        raise SurveyError("estimate must be a SurveyEstimate")
    _atomic_write_json(path, estimate.to_document())
    return path


def load_survey_estimate(path):
    """Load an exact estimate document; fraction strings become Fractions."""
    doc = _read_json_file(path, SurveyError)
    if not isinstance(doc, dict):
        raise SurveyError("estimate document must be an object")
    for field in ("total_estimate", "variance_estimate"):
        value = doc.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise SurveyError(f"{field} must be an exact fraction string")
            try:
                doc[field] = Fraction(value)
            except (ValueError, ZeroDivisionError) as exc:
                raise SurveyError(f"invalid exact fraction {value!r}") from exc
    return doc


def _coerce_package(package):
    if isinstance(package, str) and package.lstrip().startswith("{"):
        try:
            document = json.loads(
                package,
                object_pairs_hook=_json_pairs_hook,
                parse_constant=_reject_json_constant,
            )
        except ValueError as exc:
            raise TaskPackageError(f"invalid task package JSON: {exc}") from exc
        return parse_task_package(document)
    if isinstance(package, (str, os.PathLike)):
        return load_task_package(package)
    if isinstance(package, dict):
        # Re-parse even TaskPackage instances so a mutated mapping cannot
        # bypass structural validation.
        return parse_task_package(package)
    raise TaskPackageError("package must be a TaskPackage, dict or file path")


def _coerce_receipt_document(receipt):
    if isinstance(receipt, str) and receipt.lstrip()[:1] in ("{", "["):
        try:
            return json.loads(
                receipt,
                object_pairs_hook=_json_pairs_hook,
                parse_constant=_reject_json_constant,
            )
        except ValueError as exc:
            raise ReceiptError(f"invalid receipt JSON: {exc}") from exc
    if isinstance(receipt, (str, os.PathLike)):
        return _read_json_file(receipt, ReceiptError)
    if isinstance(receipt, (bytes, bytearray)):
        try:
            return json.loads(
                bytes(receipt).decode("utf-8"),
                object_pairs_hook=_json_pairs_hook,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, ValueError) as exc:
            raise ReceiptError(f"invalid receipt JSON: {exc}") from exc
    return receipt


_RECORD_ID_KEYS = ("window_id", "id")
_RECORD_ROWS_KEYS = ("readings", "rows", "objects", "measurements")
_RECORD_ALLOWED = set(_RECORD_ID_KEYS) | set(_RECORD_ROWS_KEYS)
_ROW_KEY_KEYS = ("object_key", "key", "object")
_ROW_VALUE_KEYS = ("reading", "value", "y", "measurement")
_ROW_ALLOWED = set(_ROW_KEY_KEYS) | set(_ROW_VALUE_KEYS)


def _parse_row(row, what, design):
    if isinstance(row, (list, tuple)) and len(row) == 2:
        key_value, reading_value = row
    elif isinstance(row, dict):
        unknown = set(row) - _ROW_ALLOWED
        if unknown:
            raise ReceiptError(f"{what} has illegal fields: {sorted(unknown)}")
        key_value = _alias_value(row, _ROW_KEY_KEYS, what, ReceiptError)
        reading_value = _alias_value(row, _ROW_VALUE_KEYS, what, ReceiptError)
    else:
        raise ReceiptError(f"{what} must be [object_key, reading] or an object")

    key = _parse_object_key(key_value, f"{what}.object_key", ReceiptError)
    if key not in design.key_index:
        raise ReceiptError(f"{what} reports unknown object {list(key)}")
    reading = _strict_int(
        reading_value, 0, 65535, f"{what}.reading", ReceiptError
    )
    return design.key_index[key], reading


def _receipt_records(raw_receipt):
    if isinstance(raw_receipt, list):
        return raw_receipt
    if not isinstance(raw_receipt, dict):
        raise ReceiptError("receipt must be an object or list of window records")
    _exact_fields(
        raw_receipt,
        ("records",),
        ("format", "schema_version"),
        "receipt",
        ReceiptError,
    )
    if "format" in raw_receipt and raw_receipt["format"] != SURVEY_RECEIPT_FORMAT:
        raise ReceiptError("unsupported receipt format")
    version = raw_receipt.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise ReceiptError("unsupported receipt schema version")
    records = raw_receipt["records"]
    if not isinstance(records, list):
        raise ReceiptError("receipt.records must be a list")
    return records


def validate_receipt(package, receipt):
    """Validate a receipt and return ``(readings_by_object, per_window)``."""
    pkg = _coerce_package(package)
    design = pkg.design
    raw_receipt = _coerce_receipt_document(receipt)
    records = _receipt_records(raw_receipt)
    selected_ids = tuple(pkg["window_ids"])
    selected_set = set(selected_ids)

    per_window = {}
    for pos, record in enumerate(records):
        what = f"receipt.records[{pos}]"
        if not isinstance(record, dict):
            raise ReceiptError(f"{what} must be an object")
        unknown = set(record) - _RECORD_ALLOWED
        if unknown:
            raise ReceiptError(f"{what} has illegal fields: {sorted(unknown)}")
        window_id = _alias_value(
            record, _RECORD_ID_KEYS, what, ReceiptError
        )
        if not isinstance(window_id, str) or window_id not in selected_set:
            raise ReceiptError(f"{what} is not one of the issued windows")
        if window_id in per_window:
            raise ReceiptError(f"duplicate receipt record for window {window_id!r}")
        rows_value = _alias_value(
            record, _RECORD_ROWS_KEYS, what, ReceiptError
        )
        if not isinstance(rows_value, list):
            raise ReceiptError(f"{what}.readings must be a list")

        window = design.window_by_id[window_id]
        required = set(window.object_indices)
        reported = {}
        for row_pos, row in enumerate(rows_value):
            idx, reading = _parse_row(
                row, f"{what}.readings[{row_pos}]", design
            )
            if idx not in required:
                raise ReceiptError(
                    f"{what}.readings[{row_pos}] reports object "
                    f"{list(design.objects[idx].key)} not complete in {window_id}"
                )
            if idx in reported:
                raise ReceiptError(
                    f"duplicate object row {list(design.objects[idx].key)} "
                    f"in {window_id}"
                )
            reported[idx] = reading

        missing = required - reported.keys()
        if missing:
            keys = [list(design.objects[i].key) for i in sorted(missing)]
            raise ReceiptError(f"receipt for window {window_id!r} misses {keys}")
        per_window[window_id] = reported

    missing_windows = selected_set - per_window.keys()
    if missing_windows:
        raise ReceiptError(
            f"receipt is missing records for windows {sorted(missing_windows)}"
        )
    if len(records) != design.k:
        # Unknown/duplicate checks normally make this unreachable.
        raise ReceiptError("receipt must contain exactly one record per window")

    readings = {}
    for window_id in selected_ids:
        for idx, reading in per_window[window_id].items():
            if idx in readings and readings[idx] != reading:
                key = list(design.objects[idx].key)
                raise ReceiptConflictError(
                    f"conflicting readings for object {key}: "
                    f"{readings[idx]} vs {reading}"
                )
            readings[idx] = reading
    return readings, per_window


def evaluate_receipt(package, receipt):
    """Validate the receipt and compute exact HT total and maybe variance."""
    pkg = _coerce_package(package)
    design = pkg.design
    readings, per_window = validate_receipt(pkg, receipt)
    observed = sorted(readings)

    total = Fraction(0, 1)
    for idx in observed:
        total += Fraction(readings[idx]) / design.inclusion_probability(idx)

    unsupported = design.unsupported_pairs()
    if unsupported:
        variance = None
        reason = "design_has_zero_joint_inclusion_probability_pair"
        bad_pairs = [
            [list(design.objects[i].key), list(design.objects[j].key)]
            for i, j in unsupported
        ]
    else:
        variance = Fraction(0, 1)
        for i in observed:
            yi_over_pi = Fraction(readings[i]) / design.inclusion_probability(i)
            for j in observed:
                pi = design.inclusion_probability(i)
                pj = design.inclusion_probability(j)
                pij = design.joint_probability(i, j)
                variance += (
                    (pij - pi * pj) / pij
                ) * yi_over_pi * (
                    Fraction(readings[j]) / pj
                )
        reason = None
        bad_pairs = []

    observations = []
    selected_ids = tuple(pkg["window_ids"])
    for idx in observed:
        windows = [
            wid
            for wid in selected_ids
            if idx in design.window_by_id[wid].object_indices
        ]
        observations.append(
            {
                "object_key": list(design.objects[idx].key),
                "reading": readings[idx],
                "reported_by_windows": windows,
                "inclusion_probability": str(design.inclusion_probability(idx)),
            }
        )

    document = {
        "format": SURVEY_ESTIMATE_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "rank": pkg["rank"],
        "scheme": list(selected_ids),
        "valid": True,
        "population_object_count": len(design.objects),
        "observed_object_count": len(observed),
        "observations": observations,
        "total_estimate": total,
        "variance_supported": not unsupported,
        "variance_estimate": variance,
        "variance_unavailable_reason": reason,
        "unsupported_object_pairs": bad_pairs,
    }
    return SurveyEstimate(document, design, readings)


def build_receipt(package, readings_by_window):
    """Build a canonical receipt from ``{window_id: [{object/value...}]}``.

    This is mainly for examples/tests; field systems may construct the same
    JSON themselves.  It still requires every mandated object and value.
    """
    pkg = _coerce_package(package)
    records = []
    if not isinstance(readings_by_window, dict):
        raise ReceiptError("readings_by_window must be a mapping")
    for window in pkg["tasks"]:
        wid = window["window_id"]
        if wid not in readings_by_window:
            raise ReceiptError(f"missing readings for {wid}")
        rows = readings_by_window[wid]
        if not isinstance(rows, list):
            raise ReceiptError(f"readings for {wid} must be a list")
        records.append({"window_id": wid, "readings": rows})
    extra = set(readings_by_window) - set(pkg["window_ids"])
    if extra:
        raise ReceiptError(f"readings supplied for unissued windows {sorted(extra)}")
    receipt = {"format": SURVEY_RECEIPT_FORMAT, "schema_version": 1, "records": records}
    validate_receipt(pkg, receipt)
    return receipt


# Friendly API aliases.
compile_survey_design = design_survey
build_survey_design = design_survey
create_survey_design = design_survey
build_design = design_survey
create_design = design_survey
design_sampling = design_survey
design_sampling_plan = design_survey
build_sampling_design = design_survey
SamplingDesign = SurveyDesign
SamplingPlan = SurveyDesign

build_task_package = issue_task_package
create_task_package = issue_task_package
issue_package = issue_task_package
generate_task_package = issue_task_package
make_package = issue_task_package
read_task_package = load_task_package
load_package = load_task_package
read_package = load_task_package
SamplingTaskPackage = TaskPackage

estimate_survey = evaluate_receipt
estimate_from_receipt = evaluate_receipt
analyze_receipt = evaluate_receipt
deliver_survey_estimate = evaluate_receipt
estimate = evaluate_receipt
calculate_estimate = evaluate_receipt
compute_estimate = evaluate_receipt
estimate_total = evaluate_receipt
SamplingEstimate = SurveyEstimate
