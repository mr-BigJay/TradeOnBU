"""Gross 3R target ceiling is only for newly created AI auto-paper orders."""

import math

import pytest

from app.domain.paper_execution_policy import (
    MAX_NEW_AUTO_PAPER_GROSS_RR,
    cap_new_auto_paper_tp,
)


@pytest.mark.parametrize(
    "side, entry, stop_loss, proposed, expected",
    [
        ("LONG", 100, 95, 110, 110),      # 2R unchanged
        ("LONG", 100, 95, 115, 115),      # 3R unchanged
        ("LONG", 100, 95, 115.5, 115),    # 3.1R capped
        ("LONG", 100, 95, 117, 115),      # 3.4R capped
        ("LONG", 100, 95, 125, 115),      # 5R capped
        ("SHORT", 100, 105, 90, 90),      # 2R unchanged
        ("SHORT", 100, 105, 85, 85),      # 3R unchanged
        ("SHORT", 100, 105, 84.5, 85),    # 3.1R capped
        ("SHORT", 100, 105, 83, 85),      # 3.4R capped
        ("SHORT", 100, 105, 75, 85),      # 5R capped
    ],
)
def test_gross_rr_tp_ceiling(side, entry, stop_loss, proposed, expected):
    assert MAX_NEW_AUTO_PAPER_GROSS_RR == 3.0
    assert cap_new_auto_paper_tp(side, entry, stop_loss, proposed) == pytest.approx(expected)


@pytest.mark.parametrize(
    "side, entry, sl, tp",
    [
        ("LONG", 100, 105, 115), ("SHORT", 100, 95, 85),
        ("LONG", 100, 95, 90), ("SHORT", 100, 105, 110),
        ("FLAT", 100, 95, 110), ("LONG", 100, 100, 110),
        ("LONG", math.nan, 95, 110), ("SHORT", 100, 105, math.inf),
    ],
)
def test_invalid_levels_are_not_silently_rewritten(side, entry, sl, tp):
    with pytest.raises(ValueError):
        cap_new_auto_paper_tp(side, entry, sl, tp)
