import json
import math
import sys
import tempfile
import unittest
from fractions import Fraction
from itertools import combinations
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from micro_objects import (ReceiptConflictError, ReceiptError,
                           SurveyDesignError, design_survey,
                           evaluate_receipt, issue_task_package,
                           load_survey_estimate, load_task_package,
                           save_survey_estimate, save_task_package)
from micro_objects.sampling import parse_task_package


def acceptance_board():
    # Width 8, height 3. Foreground A=(1,1), B=(3,1), C=(6,1).
    return {
        "mask": ["00000000", "01010010", "00000000"],
        "intensity": [[0] * 8 for _ in range(3)],
        "pixel_size": ["1", "1"],
        "connectivity": 4,
        "selection": {"min_pixels": 1, "exclude_border": False},
    }


def acceptance_windows(f4_cost=1, f3_cost=2):
    return [
        {"id": "f1", "rect": [1, 1, 2, 2], "cost": 1},
        {"id": "f2", "rect": [1, 1, 4, 2], "cost": 2},
        {"id": "f3", "rect": [6, 1, 7, 2], "cost": f3_cost},
        {"id": "f4", "rect": [3, 1, 4, 2], "cost": f4_cost},
    ]


def good_receipt():
    return {
        "format": "micro_objects.survey_receipt",
        "schema_version": 1,
        "records": [
            {
                "window_id": "f1",
                "readings": [{"object_key": [1, 1], "reading": 4}],
            },
            {
                "window_id": "f2",
                "readings": [
                    {"object_key": [1, 1], "reading": 4},
                    {"object_key": [3, 1], "reading": 8},
                ],
            },
        ],
    }


class AcceptanceSamplingTests(unittest.TestCase):
    def test_first_accepted_design_and_offline_package(self):
        design = design_survey(acceptance_board(), acceptance_windows(), 2, 3)
        self.assertEqual(design.scheme_count, 5)
        self.assertEqual(design.scheme_for_rank(0), ("f1", "f2"))

        for key, expected in (
            ([1, 1], Fraction(4, 5)),
            ([3, 1], Fraction(4, 5)),
            ([6, 1], Fraction(2, 5)),
        ):
            self.assertEqual(design.inclusion_probability(key), expected)
        for a, b, expected in (
            ([1, 1], [3, 1], Fraction(3, 5)),
            ([1, 1], [6, 1], Fraction(1, 5)),
            ([3, 1], [6, 1], Fraction(1, 5)),
        ):
            self.assertEqual(design.joint_probability(a, b), expected)

        package = issue_task_package(design, 0)
        self.assertEqual(
            [(t["window_id"], t["objects"]) for t in package["tasks"]],
            [("f1", [[1, 1]]), ("f2", [[1, 1], [3, 1]])],
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_path = root / "task-package.json"
            receipt_path = root / "receipt.json"
            save_task_package(package_path, package)
            receipt_path.write_text(json.dumps(good_receipt()), encoding="utf-8")

            raw_package = json.loads(package_path.read_text(encoding="utf-8"))
            self.assertNotIn("mask", package_path.read_text(encoding="utf-8"))
            self.assertNotIn("intensity", package_path.read_text(encoding="utf-8"))

            # Stats side uses only the two offline documents.
            estimate = evaluate_receipt(package_path, receipt_path)
            self.assertEqual(estimate["total_estimate"], Fraction(15))
            self.assertEqual(estimate["variance_estimate"], Fraction(55, 3))
            self.assertTrue(estimate["variance_supported"])

            estimate_path = root / "estimate.json"
            save_survey_estimate(estimate_path, estimate)
            reloaded_estimate = load_survey_estimate(estimate_path)
            self.assertEqual(
                reloaded_estimate["total_estimate"], Fraction(15)
            )
            self.assertEqual(
                reloaded_estimate["variance_estimate"], Fraction(55, 3)
            )
            estimate_json = json.loads(estimate_path.read_text(encoding="utf-8"))
            self.assertEqual(estimate_json["total_estimate"], "15")
            self.assertEqual(estimate_json["variance_estimate"], "55/3")

            loaded = load_task_package(package_path)
            self.assertEqual(loaded.design.scheme_count, 5)
            self.assertEqual(raw_package["rank"], 0)

    def test_reading_conflict_rejects_every_estimate(self):
        design = design_survey(acceptance_board(), acceptance_windows(), 2, 3)
        package = issue_task_package(design, 0)
        receipt = good_receipt()
        receipt["records"][0]["readings"][0]["reading"] = 5
        with self.assertRaises(ReceiptConflictError):
            evaluate_receipt(package, receipt)

    def test_second_budget_case_has_zero_bc_pair(self):
        windows = acceptance_windows(f4_cost=2)
        design = design_survey(acceptance_board(), windows, 2, 3)
        self.assertEqual(design.scheme_count, 3)
        self.assertEqual(design.scheme_for_rank(0), ("f1", "f2"))
        self.assertEqual(design.inclusion_probability([1, 1]), Fraction(1))
        self.assertEqual(design.inclusion_probability([3, 1]), Fraction(2, 3))
        self.assertEqual(design.inclusion_probability([6, 1]), Fraction(1, 3))
        self.assertEqual(
            design.joint_probability([3, 1], [6, 1]), Fraction(0)
        )
        self.assertFalse(design.variance_supported)

        package = issue_task_package(design, 0)
        estimate = evaluate_receipt(package, good_receipt())
        self.assertEqual(estimate["total_estimate"], Fraction(16))
        self.assertIsNone(estimate["variance_estimate"])
        self.assertFalse(estimate["variance_supported"])
        self.assertEqual(
            estimate["variance_unavailable_reason"],
            "design_has_zero_joint_inclusion_probability_pair",
        )

    def test_window_covering_no_feasible_scheme_rejects_design(self):
        windows = acceptance_windows(f3_cost=3)
        with self.assertRaisesRegex(SurveyDesignError, r"\[6, 1\]"):
            design_survey(acceptance_board(), windows, 2, 3)


class GeometryAndReceiptTests(unittest.TestCase):
    def test_union_cover_without_single_complete_window_rejected(self):
        board = {
            "mask": ["110"],
            "intensity": [[0, 0, 0]],
            "pixel_size": ["1", "1"],
            "connectivity": 4,
            "selection": {"min_pixels": 1, "exclude_border": False},
        }
        windows = [
            {"id": "left", "rect": [0, 0, 1, 1], "cost": 1},
            {"id": "right", "rect": [1, 0, 2, 1], "cost": 1},
        ]
        with self.assertRaisesRegex(SurveyDesignError, r"\[0, 0\]"):
            design_survey(board, windows, 2, 2)

    def test_rejected_foreground_is_empty_window_and_zero_reading_valid(self):
        board = {
            "mask": ["1101"],
            "intensity": [[1, 2, 3, 4]],
            "pixel_size": ["1", "1"],
            "connectivity": 4,
            "selection": {"min_pixels": 2, "exclude_border": False},
        }
        windows = [
            {"id": "obj", "rect": [0, 0, 2, 1], "cost": 1},
            {"id": "noise", "rect": [3, 0, 4, 1], "cost": 1},
        ]
        design = design_survey(board, windows, 2, 2)
        package = issue_task_package(design, 0)
        task_objects = {t["window_id"]: t["objects"] for t in package["tasks"]}
        self.assertEqual(task_objects, {"obj": [[0, 0]], "noise": []})

        receipt = {
            "records": [
                {"window_id": "obj", "readings": [[[0, 0], 0]]},
                {"window_id": "noise", "readings": []},
            ]
        }
        estimate = evaluate_receipt(package, receipt)
        self.assertEqual(estimate["total_estimate"], Fraction(0))
        self.assertEqual(estimate["observed_object_count"], 1)

        missing_empty = {
            "records": [
                {"window_id": "obj", "readings": [[[0, 0], 0]]},
            ]
        }
        with self.assertRaisesRegex(ReceiptError, "missing records"):
            evaluate_receipt(package, missing_empty)

    def test_empty_observed_sample_is_empty_sum_when_design_supports_variance(self):
        board = {
            "mask": ["101"],
            "intensity": [[0, 0, 0]],
            "pixel_size": ["1", "1"],
            "connectivity": 4,
            "selection": {"min_pixels": 1, "exclude_border": False},
        }
        windows = [
            {"id": "e0", "rect": [1, 0, 2, 1], "cost": 1},
            {"id": "e1", "rect": [1, 0, 2, 1], "cost": 1},
            {"id": "wa", "rect": [0, 0, 1, 1], "cost": 1},
            {"id": "wb", "rect": [2, 0, 3, 1], "cost": 1},
        ]
        # The two e-windows are duplicate empty rectangles; e0/e1 is rank 0.
        design = design_survey(board, windows, 2, 2)
        self.assertEqual(design.scheme_for_rank(0), ("e0", "e1"))
        package = issue_task_package(design, 0)
        self.assertEqual(
            [task["objects"] for task in package["tasks"]], [[], []]
        )
        receipt = {
            "records": [
                {"window_id": "e0", "readings": []},
                {"window_id": "e1", "readings": []},
            ]
        }
        estimate = evaluate_receipt(package, receipt)
        self.assertEqual(estimate["observed_object_count"], 0)
        self.assertEqual(estimate["total_estimate"], Fraction(0))
        self.assertTrue(estimate["variance_supported"])
        self.assertEqual(estimate["variance_estimate"], Fraction(0))

    def test_no_feasible_scheme(self):
        board = acceptance_board()
        windows = [
            {"id": "f1", "rect": [1, 1, 2, 2], "cost": 1},
            {"id": "f4", "rect": [3, 1, 4, 2], "cost": 1},
        ]
        with self.assertRaisesRegex(SurveyDesignError, "no feasible"):
            design_survey(board, windows, 2, 1)

    def test_input_reorder_and_rank_bounds(self):
        windows = acceptance_windows()
        design = design_survey(acceptance_board(), windows, 2, 3)
        reordered = list(reversed(windows))
        reordered_design = design_survey(
            acceptance_board(), reordered, 2, 3
        )
        self.assertEqual(reordered_design.scheme_count, design.scheme_count)
        first = issue_task_package(design, 0)
        second = issue_task_package(reordered_design, 0)
        self.assertEqual(first["window_ids"], second["window_ids"])
        self.assertEqual(first["tasks"], second["tasks"])
        self.assertEqual(first["design"], second["design"])

        for bad_rank in (-1, 5, 1.0, True, "0"):
            with self.assertRaises(SurveyDesignError):
                issue_task_package(design, bad_rank)

    def test_receipt_defects_do_not_produce_estimates(self):
        design = design_survey(acceptance_board(), acceptance_windows(), 2, 3)
        package = issue_task_package(design, 0)

        def check(mutate, message=None):
            receipt = good_receipt()
            mutate(receipt)
            with self.assertRaises(ReceiptError) as context:
                evaluate_receipt(package, receipt)
            if message:
                self.assertRegex(str(context.exception), message)

        check(lambda r: r["records"].pop(1), "missing records")
        check(
            lambda r: r["records"].append(dict(r["records"][0])),
            "duplicate receipt record",
        )
        check(
            lambda r: r["records"].append(
                {
                    "window_id": "f3",
                    "readings": [{"object_key": [6, 1], "reading": 1}],
                }
            ),
            "not one of the issued windows",
        )
        check(
            lambda r: r["records"][0]["readings"][0].__setitem__(
                "object_key", [3, 1]
            ),
            "not complete in f1",
        )
        check(lambda r: r["records"][1]["readings"].pop(1), "misses")
        check(
            lambda r: r["records"][1]["readings"].append(
                {"object_key": [6, 1], "reading": 2}
            ),
            "not complete in f2",
        )
        check(
            lambda r: r["records"][1]["readings"].append(
                {"object_key": [3, 1], "reading": 9}
            ),
            "duplicate object row",
        )
        for bad_reading in (-1, 65536, "4", True, 4.0):
            check(
                lambda r, value=bad_reading: r["records"][0]["readings"][
                    0
                ].__setitem__("reading", value),
                "reading",
            )

    def test_receipt_and_row_order_do_not_matter(self):
        design = design_survey(acceptance_board(), acceptance_windows(), 2, 3)
        package = issue_task_package(design, 0)
        receipt = good_receipt()
        receipt["records"].reverse()
        receipt["records"][0]["readings"].reverse()  # f2 rows B,A
        estimate = evaluate_receipt(package, receipt)
        self.assertEqual(estimate["total_estimate"], Fraction(15))
        self.assertEqual(estimate["variance_estimate"], Fraction(55, 3))

    def test_duplicate_rectangle_windows_each_require_consistent_row(self):
        board = {
            "mask": ["10"],
            "intensity": [[7, 0]],
            "pixel_size": ["1", "1"],
            "connectivity": 4,
            "selection": {"min_pixels": 1, "exclude_border": False},
        }
        windows = [
            {"id": "z", "rect": [0, 0, 1, 1], "cost": 1},
            {"id": "a", "rect": [0, 0, 1, 1], "cost": 1},
        ]
        design = design_survey(board, windows, 2, 2)
        self.assertEqual(design.scheme_for_rank(0), ("a", "z"))
        package = issue_task_package(design, 0)
        receipt = {
            "records": [
                {"window_id": "z", "readings": [{"object_key": [0, 0], "reading": 9}]},
                {"window_id": "a", "readings": [{"object_key": [0, 0], "reading": 9}]},
            ]
        }
        estimate = evaluate_receipt(package, receipt)
        self.assertEqual(estimate["total_estimate"], Fraction(9))
        bad = json.loads(json.dumps(receipt))
        bad["records"][0]["readings"][0]["reading"] = 10
        with self.assertRaises(ReceiptConflictError):
            evaluate_receipt(package, bad)

    def test_boolean_integer_parameters_rejected(self):
        board = acceptance_board()
        windows = acceptance_windows()
        with self.assertRaises(SurveyDesignError):
            design_survey(board, windows, True, 3)
        with self.assertRaises(SurveyDesignError):
            design_survey(board, windows, 2, True)
        bad_cost = [dict(w, cost=True) if w["id"] == "f1" else w for w in windows]
        with self.assertRaises(SurveyDesignError):
            design_survey(board, bad_cost, 2, 3)

    def test_malformed_package_rejected(self):
        design = design_survey(acceptance_board(), acceptance_windows(), 2, 3)
        package = issue_task_package(design, 0)
        document = dict(package)
        document["rank"] = 1
        with self.assertRaises(Exception):
            parse_task_package(document)

        bad_design = json.loads(json.dumps(package))
        bad_design["design"]["scheme_count"] = 99
        with self.assertRaises(Exception):
            parse_task_package(bad_design)


class IndependentEnumerationTests(unittest.TestCase):
    def test_small_design_all_ranks_and_probabilities(self):
        board = {
            "mask": ["1010101"],
            "intensity": [[0] * 7],
            "pixel_size": ["1", "1"],
            "connectivity": 4,
            "selection": {"min_pixels": 1, "exclude_border": False},
        }
        windows = [
            {"id": "w0", "rect": [0, 0, 1, 1], "cost": 1},
            {"id": "w1", "rect": [0, 0, 3, 1], "cost": 2},
            {"id": "w2", "rect": [2, 0, 5, 1], "cost": 2},
            {"id": "w3", "rect": [4, 0, 7, 1], "cost": 1},
            {"id": "w4", "rect": [6, 0, 7, 1], "cost": 1},
            {"id": "w5", "rect": [0, 0, 7, 1], "cost": 3},
        ]
        k, budget = 3, 6
        design = design_survey(board, list(reversed(windows)), k, budget)

        feasible = []
        for combo in combinations(windows, k):
            if sum(w["cost"] for w in combo) <= budget:
                feasible.append(tuple(sorted(w["id"] for w in combo)))
        feasible.sort()
        self.assertEqual(design.scheme_count, len(feasible))
        for rank, expected_ids in enumerate(feasible):
            self.assertEqual(design.scheme_for_rank(rank), expected_ids)

        coverage = {
            "w0": {0},
            "w1": {0, 1},
            "w2": {1, 2},
            "w3": {2, 3},
            "w4": {3},
            "w5": {0, 1, 2, 3},
        }
        keys = [(0, 0), (2, 0), (4, 0), (6, 0)]
        n = len(feasible)
        for i, key_i in enumerate(keys):
            numerator = sum(
                any(i in coverage[wid] for wid in scheme) for scheme in feasible
            )
            self.assertEqual(
                design.inclusion_probability(key_i),
                Fraction(numerator, n),
            )
            for j, key_j in enumerate(keys):
                numerator_pair = sum(
                    any(i in coverage[wid] for wid in scheme)
                    and any(j in coverage[wid] for wid in scheme)
                    for scheme in feasible
                )
                self.assertEqual(
                    design.joint_probability(key_i, key_j),
                    Fraction(numerator_pair, n),
                )

        # Every rank must issue the independently enumerated tuple/objects.
        for rank, expected_ids in enumerate(feasible):
            package = issue_task_package(design, rank)
            self.assertEqual(tuple(package["window_ids"]), expected_ids)
            for task in package["tasks"]:
                wid = task["window_id"]
                expected_keys = [
                    list(keys[i]) for i in sorted(coverage[wid])
                ]
                self.assertEqual(task["objects"], expected_keys)


class ScaleChecks(unittest.TestCase):
    def test_32_windows_first_last_and_pair_probability(self):
        board = {
            "mask": ["00000", "01010", "00000"],
            "intensity": [[0] * 5 for _ in range(3)],
            "pixel_size": ["1", "1"],
            "connectivity": 4,
            "selection": {"min_pixels": 1, "exclude_border": False},
        }
        windows = [
            {"id": f"a{i:02d}", "rect": [1, 1, 2, 2], "cost": 1}
            for i in range(16)
        ] + [
            {"id": f"b{i:02d}", "rect": [3, 1, 4, 2], "cost": 1}
            for i in range(16)
        ]
        design = design_survey(board, windows, 8, 8)
        expected_n = math.comb(32, 8)
        self.assertEqual(design.scheme_count, expected_n)
        self.assertEqual(
            design.scheme_for_rank(0), tuple(f"a{i:02d}" for i in range(8))
        )
        self.assertEqual(
            design.scheme_for_rank(expected_n - 1),
            tuple(f"b{i:02d}" for i in range(8, 16)),
        )
        expected_pair = Fraction(
            expected_n - 2 * math.comb(16, 8), expected_n
        )
        self.assertEqual(
            design.joint_probability([1, 1], [3, 1]), expected_pair
        )


if __name__ == "__main__":
    unittest.main()
