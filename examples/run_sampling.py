"""Synthetic offline sampling → task package → receipt → exact HT estimate.

Run from the repository root:

    python -I examples/run_sampling.py

The example writes only synthetic JSON under ``run/sampling/``.  After the
task package and receipt are written, estimation uses those two files only;
the original board mapping is deleted before the offline read.
"""
from __future__ import annotations

import json
import sys
import math
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from micro_objects import (  # noqa: E402
    ReceiptConflictError,
    SurveyDesignError,
    design_survey,
    evaluate_receipt,
    issue_task_package,
)
from micro_objects.sampling import (  # noqa: E402
    save_survey_estimate,
    save_task_package,
)


def make_board():
    # 8x3 unit-pixel board.  Foreground cells are A=(1,1), B=(3,1), C=(6,1).
    return {
        "mask": ["00000000", "01010010", "00000000"],
        "intensity": [[0] * 8 for _ in range(3)],
        "pixel_size": ["1", "1"],
        "connectivity": 4,
        "selection": {"min_pixels": 1, "exclude_border": False},
    }


def make_windows(f4_cost=1, f3_cost=2):
    return [
        {"id": "f1", "rect": [1, 1, 2, 2], "cost": 1},
        {"id": "f2", "rect": [1, 1, 4, 2], "cost": 2},
        {"id": "f3", "rect": [6, 1, 7, 2], "cost": f3_cost},
        {"id": "f4", "rect": [3, 1, 4, 2], "cost": f4_cost},
    ]


def correct_receipt():
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


def main():
    out = ROOT / "run" / "sampling"
    out.mkdir(parents=True, exist_ok=True)

    board = make_board()
    design = design_survey(board, make_windows(), k=2, budget=3)
    package = issue_task_package(design, rank=0)
    save_task_package(out / "task_package.json", package)

    receipt = correct_receipt()
    receipt_path = out / "receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    probability_summary = {
        "N": design.scheme_count,
        "p_A": str(design.inclusion_probability([1, 1])),
        "p_B": str(design.inclusion_probability([3, 1])),
        "p_C": str(design.inclusion_probability([6, 1])),
        "p_AB": str(design.joint_probability([1, 1], [3, 1])),
        "p_AC": str(design.joint_probability([1, 1], [6, 1])),
        "p_BC": str(design.joint_probability([3, 1], [6, 1])),
    }

    # Simulate moving/removing the board before the statistics machine reads.
    del board
    del design
    del package

    estimate = evaluate_receipt(out / "task_package.json", receipt_path)
    save_survey_estimate(out / "estimate.json", estimate)
    print("first design:", json.dumps(probability_summary, sort_keys=True))
    print(
        "first estimate:",
        "total =",
        str(estimate["total_estimate"]),
        "variance =",
        str(estimate["variance_estimate"]),
    )
    assert estimate["total_estimate"] == Fraction(15)
    assert estimate["variance_estimate"] == Fraction(55, 3)

    conflict = correct_receipt()
    conflict["records"][0]["readings"][0]["reading"] = 5
    try:
        evaluate_receipt(out / "task_package.json", conflict)
    except ReceiptConflictError as exc:
        print("conflict receipt rejected:", exc)
    else:
        raise AssertionError("conflict receipt must not produce an estimate")

    # Changing only the unselected f4 cost changes the design distribution.
    second = design_survey(make_board(), make_windows(f4_cost=2), 2, 3)
    second_package = issue_task_package(second, 0)
    second_estimate = evaluate_receipt(second_package, correct_receipt())
    print(
        "second design:",
        {
            "N": second.scheme_count,
            "p_A": str(second.inclusion_probability([1, 1])),
            "p_B": str(second.inclusion_probability([3, 1])),
            "p_C": str(second.inclusion_probability([6, 1])),
            "p_BC": str(second.joint_probability([3, 1], [6, 1])),
        },
    )
    print(
        "second estimate:",
        "total =",
        str(second_estimate["total_estimate"]),
        "variance =",
        second_estimate["variance_estimate"],
        "-",
        second_estimate["variance_unavailable_reason"],
    )
    assert second_estimate["total_estimate"] == Fraction(16)
    assert second_estimate["variance_estimate"] is None

    try:
        design_survey(make_board(), make_windows(f3_cost=3), 2, 3)
    except SurveyDesignError as exc:
        print("unobservable C design rejected:", exc)
    else:
        raise AssertionError("f3 cost 3 design must be rejected")

    # 32-window scale smoke check; no full scheme list is materialized.
    scale_board = {
        "mask": ["00000", "01010", "00000"],
        "intensity": [[0] * 5 for _ in range(3)],
        "pixel_size": ["1", "1"],
        "connectivity": 4,
        "selection": {"min_pixels": 1, "exclude_border": False},
    }
    scale_windows = [
        {"id": f"a{i:02d}", "rect": [1, 1, 2, 2], "cost": 1}
        for i in range(16)
    ] + [
        {"id": f"b{i:02d}", "rect": [3, 1, 4, 2], "cost": 1}
        for i in range(16)
    ]
    scale_design = design_survey(scale_board, scale_windows, 8, 8)
    expected_n = math.comb(32, 8)
    assert scale_design.scheme_count == expected_n
    assert scale_design.scheme_for_rank(0) == tuple(f"a{i:02d}" for i in range(8))
    assert scale_design.scheme_for_rank(expected_n - 1) == tuple(
        f"b{i:02d}" for i in range(8, 16)
    )
    print("scale N =", scale_design.scheme_count)
    print("wrote", out)


if __name__ == "__main__":
    main()
