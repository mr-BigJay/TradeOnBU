from copy import deepcopy
import pytest
from app.services.outlook_text_prices import reconcile_condition_prices


def fixture():
    condition = {
        "activation_fa": "بسته‌شدن روزانه بالای ۶۷٬۴۵۵٫۵ دلار.",
        "activation_level_ids": ["activation"],
        "invalidation_fa": "بسته‌شدن روزانه زیر ۶۴٬۴۲۷٫۶ دلار این مسیر را باطل می‌کند.",
        "invalidation_level_ids": ["invalidation"],
    }
    report = {"dominant": deepcopy(condition), "alternative": deepcopy(condition),
              "trend_assessment": {"reversal_watch_fa": condition["invalidation_fa"]},
              "previous_plan_review": {"assessment_fa": condition["invalidation_fa"]}}
    levels = [{"id": "activation", "price": 67455.5}, {"id": "invalidation", "price": 66427.6}]
    return report, levels


def test_reconciles_repeated_current_condition_but_preserves_prior_review_and_input():
    report, levels = fixture()
    prior = deepcopy(report["previous_plan_review"])
    reconcile_condition_prices(report, levels)
    assert "۶۶٬۴۲۷٫۶" in report["alternative"]["invalidation_fa"]
    assert "۶۶٬۴۲۷٫۶" in report["trend_assessment"]["reversal_watch_fa"]
    assert len(report["text_price_corrections"]) == 3
    assert report["previous_plan_review"] == prior
    assert "text_validation_warnings" not in report


@pytest.mark.parametrize("mode", ["multiple_refs", "multiple_numbers", "known_level", "conflicting_refs"])
def test_ambiguous_prices_remain_warned(mode):
    report, levels = fixture()
    for scenario in ("dominant", "alternative"):
        if mode == "multiple_refs":
            report[scenario]["invalidation_level_ids"].append("activation")
        if mode == "multiple_numbers":
            report[scenario]["invalidation_fa"] += " یا ۶۳٬۰۰۰ دلار."
    if mode == "known_level":
        levels.append({"id": "other", "price": 64427.6})
    if mode == "conflicting_refs":
        report["dominant"]["invalidation_level_ids"] = ["activation"]
    reconcile_condition_prices(report, levels)
    assert "۶۴٬۴۲۷٫۶" in report["alternative"]["invalidation_fa"]
    assert report["text_validation_warnings"]
    assert "text_price_corrections" not in report


@pytest.mark.parametrize("number", ["64427.6", "64,427.6", "۶۴٬۴۲۷٫۶", "٦٤٬٤٢٧٫٦"])
def test_supported_digit_formats(number):
    report, levels = fixture()
    report["alternative"]["invalidation_fa"] = f"روزانه زیر {number} دلار."
    reconcile_condition_prices(report, levels)
    assert report["text_price_corrections"]
    assert "text_validation_warnings" not in report


def test_ambiguous_occurrence_blocks_global_replacement():
    report, levels = fixture()
    report["dominant"]["invalidation_level_ids"].append("activation")
    reconcile_condition_prices(report, levels)
    assert "text_price_corrections" not in report
    assert len(report["text_validation_warnings"]) == 2
