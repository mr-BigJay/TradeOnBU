from app.domain.state_machine import can_transition
from app.models import SignalStatus


def test_waiting_to_active():
    assert can_transition(SignalStatus.WAITING_FOR_ENTRY.value, SignalStatus.ACTIVE.value)


def test_terminal_blocked():
    assert not can_transition(SignalStatus.TP_HIT.value, SignalStatus.ACTIVE.value)


def test_no_tp_without_active_path():
    assert not can_transition(SignalStatus.CREATED.value, SignalStatus.TP_HIT.value)
