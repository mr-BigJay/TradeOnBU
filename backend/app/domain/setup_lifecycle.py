from __future__ import annotations

"""Persistent ICT opportunity lifecycle states (restart-safe)."""

# Analysis-only (not stored on AiProposedSetup)
NO_TRADE = "NO_TRADE"

WAIT_LONG = "WAIT_LONG"
WAIT_SHORT = "WAIT_SHORT"
READY_LONG = "READY_LONG"
READY_SHORT = "READY_SHORT"
ICT_CONFIRMED_LONG = "ICT_CONFIRMED_LONG"
ICT_CONFIRMED_SHORT = "ICT_CONFIRMED_SHORT"
PENDING_ENTRY = "PENDING_ENTRY"
POSITION_OPEN = "POSITION_OPEN"
POSITION_CLOSED = "POSITION_CLOSED"
INVALIDATED = "INVALIDATED"
EXPIRED = "EXPIRED"

ACTIVE_MONITOR_STATES = frozenset(
    {
        WAIT_LONG,
        WAIT_SHORT,
        READY_LONG,
        READY_SHORT,
        ICT_CONFIRMED_LONG,
        ICT_CONFIRMED_SHORT,
        PENDING_ENTRY,
        POSITION_OPEN,
    }
)
TERMINAL_STATES = frozenset({INVALIDATED, EXPIRED, POSITION_CLOSED, NO_TRADE})

SETUP_MODE_IMMEDIATE = "immediate"
SETUP_MODE_CONDITIONAL = "conditional"
SETUP_MODE_FLAT = "flat"

# Machine-readable rejection / audit reason codes
REASON_NET_RR_BELOW_MIN = "net_rr_below_min"
REASON_ICT_CONFIRMED_BLOCKED_RR = "ict_confirmed_blocked_by_net_rr"
DISPLAY_ICT_CONFIRMED_BLOCKED_RR = "ICT_CONFIRMED — BLOCKED_BY_NET_RR"
REASON_MISSING_MARKET_DATA = "missing_market_data"
REASON_STALE_MARKET_DATA = "stale_market_data"
REASON_STRUCTURAL_INVALIDATION = "structural_invalidation"
REASON_SETUP_EXPIRED = "setup_expired"
REASON_AUTO_PAPER_DISABLED = "auto_paper_disabled"
REASON_WALLET_NOT_INITIALIZED = "wallet_not_initialized"
REASON_MISSING_CONFIRMATIONS = "missing_confirmations"
REASON_IMMEDIATE_MISSING_MONITOR = "immediate_missing_monitor_requirements"
REASON_UNAPPROVED_CONFIRMATION_SEQUENCE = "unapproved_confirmation_sequence"
REASON_DUPLICATE_ORDER = "duplicate_order"
REASON_MAX_POSITIONS = "max_simultaneous_positions"
REASON_SUPERSEDED = "superseded_by_newer_analysis"


def wait_state_for_direction(direction: str) -> str:
    d = direction.upper()
    return WAIT_LONG if d == "LONG" else WAIT_SHORT


def ready_state_for_direction(direction: str) -> str:
    d = direction.upper()
    return READY_LONG if d == "LONG" else READY_SHORT


def ict_confirmed_state_for_direction(direction: str) -> str:
    d = direction.upper()
    return ICT_CONFIRMED_LONG if d == "LONG" else ICT_CONFIRMED_SHORT


def direction_from_lifecycle(state: str) -> str | None:
    if state in (WAIT_LONG, READY_LONG, PENDING_ENTRY) and "LONG" in state:
        return "LONG"
    if state in (WAIT_SHORT, READY_SHORT):
        return "SHORT"
    if state.endswith("_LONG") or state == "POSITION_OPEN":
        return "LONG" if "LONG" in state else None
    if "SHORT" in state:
        return "SHORT"
    return None
