"""Canonical outcome buckets for opportunity miss audit reports."""

from __future__ import annotations

# Report row classification (machine-readable)
OUTCOME_ORDER_CREATED = "valid_opportunity_order_created"
OUTCOME_CONFIRMATION_INCOMPLETE = "confirmation_incomplete"
OUTCOME_INVALIDATED = "invalidated"
OUTCOME_NET_RR_BELOW_MIN = "net_rr_below_min"
OUTCOME_MARKET_DATA_GAP_STALE = "market_data_gap_or_stale"
OUTCOME_ENGINE_DELAY = "engine_delay"
OUTCOME_ORDER_REJECTED = "order_rejected"
OUTCOME_LIMIT_NOT_FILLED = "limit_not_filled"
OUTCOME_POTENTIAL_MISSED = "potential_missed_valid_opportunity"
OUTCOME_AMBIGUOUS_CHRONOLOGY = "ambiguous_chronology_unsafe_to_infer"
OUTCOME_INTENTIONAL_WAIT = "intentional_wait_or_monitoring"
OUTCOME_TERMINAL_OTHER = "terminal_other"

ALL_OUTCOMES = frozenset(
    {
        OUTCOME_ORDER_CREATED,
        OUTCOME_CONFIRMATION_INCOMPLETE,
        OUTCOME_INVALIDATED,
        OUTCOME_NET_RR_BELOW_MIN,
        OUTCOME_MARKET_DATA_GAP_STALE,
        OUTCOME_ENGINE_DELAY,
        OUTCOME_ORDER_REJECTED,
        OUTCOME_LIMIT_NOT_FILLED,
        OUTCOME_POTENTIAL_MISSED,
        OUTCOME_AMBIGUOUS_CHRONOLOGY,
        OUTCOME_INTENTIONAL_WAIT,
        OUTCOME_TERMINAL_OTHER,
    }
)
