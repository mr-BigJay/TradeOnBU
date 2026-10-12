from app.models import SignalStatus

TERMINAL_STATUSES = frozenset(
    {
        SignalStatus.TP_HIT.value,
        SignalStatus.SL_HIT.value,
        SignalStatus.TIME_EXIT.value,
        SignalStatus.MISSED_ENTRY.value,
        SignalStatus.EXPIRED.value,
        SignalStatus.INVALIDATED.value,
        SignalStatus.REJECTED.value,
    }
)

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    SignalStatus.CREATED.value: {
        SignalStatus.WAITING_FOR_ENTRY.value,
        SignalStatus.REJECTED.value,
    },
    SignalStatus.WAITING_FOR_ENTRY.value: {
        SignalStatus.ACTIVE.value,
        SignalStatus.MISSED_ENTRY.value,
        SignalStatus.EXPIRED.value,
        SignalStatus.INVALIDATED.value,
        SignalStatus.REJECTED.value,
        SignalStatus.DATA_ERROR.value,
    },
    SignalStatus.ACTIVE.value: {
        SignalStatus.TP_HIT.value,
        SignalStatus.SL_HIT.value,
        SignalStatus.TIME_EXIT.value,
        SignalStatus.DATA_ERROR.value,
    },
    SignalStatus.DATA_ERROR.value: {
        SignalStatus.WAITING_FOR_ENTRY.value,
        SignalStatus.ACTIVE.value,
    },
}


def can_transition(from_status: str, to_status: str) -> bool:
    if from_status == to_status:
        return False
    if from_status in TERMINAL_STATUSES:
        return False
    return to_status in ALLOWED_TRANSITIONS.get(from_status, set())
