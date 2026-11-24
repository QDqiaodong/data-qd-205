"""Compatibility namespace for callers that prefer the module name ``survey``.

The implementation and field documentation live in :mod:`micro_objects.sampling`.
"""
from .sampling import (SURVEY_DESIGN_FORMAT, SURVEY_ESTIMATE_FORMAT,
                       SURVEY_RECEIPT_FORMAT, TASK_PACKAGE_FORMAT,
                       ReceiptConflictError, ReceiptError,
                       SamplingDesign, SamplingEstimate, SamplingTaskPackage,
                       SurveyDesign, SurveyDesignError, SurveyError,
                       SurveyEstimate, TaskPackage, TaskPackageError,
                       analyze_receipt, build_design, build_receipt,
                       build_survey_design, build_task_package,
                       compile_survey_design, create_design,
                       create_survey_design, create_task_package,
                       deliver_survey_estimate, design_sampling,
                       design_survey, estimate, estimate_from_receipt,
                       estimate_survey, evaluate_receipt,
                       generate_task_package, inspect_survey_design,
                       issue_package, issue_task_package, load_package,
                       load_survey_estimate, load_task_package, make_package,
                       parse_design, parse_task_package, read_package,
                       read_task_package, save_survey_estimate,
                       save_task_package, validate_receipt)

__all__ = [
    "design_survey", "inspect_survey_design", "parse_design", "evaluate_receipt",
    "issue_task_package", "save_task_package", "load_task_package",
    "parse_task_package", "validate_receipt", "build_receipt",
    "save_survey_estimate", "load_survey_estimate", "SurveyDesign",
    "TaskPackage", "SurveyEstimate", "SurveyError", "SurveyDesignError",
    "TaskPackageError", "ReceiptError", "ReceiptConflictError",
]
