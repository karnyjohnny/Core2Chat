"""Status state machine (task §4.3): unambiguous, self-recovering states."""

import pytest

from services.app_status import (ALL_STATES, AUTH_REQUIRED, BUSY_STATES,
                                 CANCELLED, CONNECTING, ERROR, IDLE, LABELS,
                                 OFFLINE, PAUSED, RATE_LIMITED, SENDING,
                                 STICKY_STATES, STREAMING, SUCCESS,
                                 TRANSITIONS, TRANSIENT_STATES, AppStatus,
                                 status_from_connectivity,
                                 status_from_provider_error)


class FakeScheduler(object):
    """Deterministic replacement for QTimer.singleShot."""

    def __init__(self):
        self.scheduled = []

    def __call__(self, delay_ms, callback):
        self.scheduled.append((delay_ms, callback))

    def fire(self, index=-1):
        delay, callback = self.scheduled[index]
        callback()
        return delay

    def count(self):
        return len(self.scheduled)


# ------------------------------------------------------------------- coverage
def test_every_state_has_a_label_and_transitions():
    assert set(LABELS) == set(ALL_STATES)
    assert set(TRANSITIONS) == set(ALL_STATES)
    for state, targets in TRANSITIONS.items():
        assert state in ALL_STATES
        for target in targets:
            assert target in ALL_STATES, "%s -> %s" % (state, target)


def test_initial_state_is_idle_and_unambiguous():
    status = AppStatus()
    assert status.state == IDLE
    assert status.label() == "Gotowy"
    assert status.status_text() == "Gotowy"
    assert status.is_busy is False
    assert status.detail == ""


def test_busy_states_are_recognised():
    status = AppStatus()
    for state in (CONNECTING, SENDING, STREAMING):
        status.set(state)
        assert status.is_busy is True, state
    for state in (IDLE, SUCCESS, ERROR, OFFLINE):
        status.set(state, force=True)
        assert status.is_busy is False, state


# ---------------------------------------------------------------- transitions
def test_normal_lifecycle_transitions():
    status = AppStatus()
    assert status.set(CONNECTING)
    assert status.set(SENDING)
    assert status.set(STREAMING)
    assert status.set(SUCCESS)
    assert status.state == SUCCESS
    status.reset_to_idle()
    assert status.state == IDLE


def test_illegal_transition_is_rejected_and_logged():
    status = AppStatus()
    status.set(OFFLINE, "brak sieci")
    assert status.set(STREAMING) is False
    assert status.state == OFFLINE          # state not corrupted
    assert any(entry[1].startswith("illegal:") for entry in status.history())


def test_force_allows_recovery_from_any_state():
    status = AppStatus()
    status.set(OFFLINE)
    assert status.set(STREAMING, force=True) is True
    assert status.state == STREAMING


def test_unknown_state_raises():
    status = AppStatus()
    with pytest.raises(ValueError):
        status.set("exploded")


# ------------------------------------------------------------- transient states
def test_error_returns_to_idle_automatically():
    """The old UI stayed in 'Błąd' forever - that is the bug being fixed."""
    scheduler = FakeScheduler()
    status = AppStatus(scheduler=scheduler)
    status.set(ERROR, "Limit API został osiągnięty.")
    assert status.state == ERROR
    assert scheduler.count() == 1
    delay = scheduler.fire()
    assert delay == 6000
    assert status.state == IDLE
    # The detail must not survive as a stale "Gotowy — Błąd ..." string.
    assert status.status_text() == "Gotowy"


def test_success_and_cancel_are_also_transient():
    for state in (SUCCESS, ERROR, RATE_LIMITED, CANCELLED):
        scheduler = FakeScheduler()
        status = AppStatus(scheduler=scheduler)
        status.set(state, "szczegół")
        assert state in TRANSIENT_STATES
        scheduler.fire()
        assert status.state == IDLE, state
        assert status.detail == ""


def test_sticky_states_do_not_reset_on_their_own():
    for state in (OFFLINE, PAUSED, AUTH_REQUIRED):
        scheduler = FakeScheduler()
        status = AppStatus(scheduler=scheduler)
        status.set(state, "powód")
        assert status.is_sticky is True
        assert scheduler.count() == 0, state
        assert status.state == state
        status.clear_sticky()
        assert status.state == IDLE


def test_a_new_operation_supersedes_a_pending_reset():
    scheduler = FakeScheduler()
    status = AppStatus(scheduler=scheduler)
    status.set(ERROR, "pierwszy błąd")
    status.set(SENDING)                      # user retries immediately
    assert status.state == SENDING
    scheduler.fire()                         # stale timer fires
    assert status.state == SENDING, "stale timer must not reset a busy state"


def test_only_the_latest_transient_timer_wins():
    scheduler = FakeScheduler()
    status = AppStatus(scheduler=scheduler)
    status.set(SUCCESS, "ok")
    status.set(SENDING)
    status.set(ERROR, "nowy błąd")
    assert scheduler.count() == 2
    scheduler.fire(0)                        # the superseded SUCCESS timer
    assert status.state == ERROR
    scheduler.fire(1)                        # the current one
    assert status.state == IDLE


def test_transient_timeout_is_configurable():
    scheduler = FakeScheduler()
    status = AppStatus(scheduler=scheduler)
    status.set_transient_timeout(1500)
    status.set(SUCCESS)
    assert scheduler.fire() == 1500
    status.set_transient_timeout(1)          # clamped to a sane minimum
    status.set(SUCCESS)
    assert scheduler.fire() == 500


def test_without_a_scheduler_the_state_still_changes():
    status = AppStatus(scheduler=None)
    status.set(ERROR, "błąd")
    assert status.state == ERROR
    status.reset_to_idle()
    assert status.state == IDLE


# -------------------------------------------------------------------- detail
def test_status_text_combines_state_and_detail():
    status = AppStatus()
    status.set(STREAMING)
    assert status.status_text() == "Generowanie…"
    status.set(ERROR, "Limit API został osiągnięty.")
    assert status.status_text() == "Błąd — Limit API został osiągnięty."
    assert "wróci do" in status.tooltip() or "Gotowy" in status.tooltip()


def test_listeners_are_notified_and_isolated_from_errors():
    status = AppStatus()
    seen = []

    def bad_listener(state, detail):
        raise RuntimeError("listener failure")

    status.add_listener(bad_listener)
    status.add_listener(lambda state, detail: seen.append(state))
    status.set(STREAMING)
    assert seen == [STREAMING]
    status.remove_listener(bad_listener)
    status.set(SUCCESS)
    assert seen == [STREAMING, SUCCESS]


def test_history_is_bounded():
    status = AppStatus()
    for index in range(400):
        status.set(ERROR, "b%d" % index, force=True)
        status.set(IDLE, force=True)
    assert len(status.history(limit=1000)) <= 200


def test_counters_track_outcomes():
    status = AppStatus()
    status.set(ERROR, "pierwszy")
    status.set(ERROR, "drugi")        # repeated failure must still count
    status.set(ERROR, "trzeci", force=True)
    status.set(SUCCESS, force=True)
    assert status.error_count == 3
    assert status.success_count == 1


def test_cancel_is_reachable_from_idle():
    """A Stop press can land after the stream already ended."""
    status = AppStatus()
    assert status.set(CANCELLED, "przerwano") is True
    assert status.state == CANCELLED
    status.reset_to_idle()
    assert status.state == IDLE


# ------------------------------------------------------------------- mappings
@pytest.mark.parametrize("category,expected", [
    ("AUTHENTICATION", AUTH_REQUIRED),
    ("AUTHORIZATION", AUTH_REQUIRED),
    ("RATE_LIMIT", RATE_LIMITED),
    ("NETWORK", OFFLINE),
    ("TIMEOUT", OFFLINE),
    ("CANCELLED", CANCELLED),
    ("MODEL_UNAVAILABLE", ERROR),
    ("CONTEXT_TOO_LARGE", ERROR),
    ("INVALID_REQUEST", ERROR),
    ("SERVER_ERROR", ERROR),
    ("UNKNOWN", ERROR),
])
def test_provider_error_maps_to_a_state(category, expected):
    state, detail = status_from_provider_error(category, "komunikat")
    assert state == expected
    assert detail == "komunikat"
    assert state in ALL_STATES


@pytest.mark.parametrize("connectivity,expected", [
    ("online", IDLE), ("offline", OFFLINE), ("auth_failed", AUTH_REQUIRED),
    ("rate_limited", RATE_LIMITED), ("no_key", AUTH_REQUIRED), ("error", ERROR),
    ("cokolwiek-innego", ERROR),
])
def test_connectivity_maps_to_a_state(connectivity, expected):
    assert status_from_connectivity(connectivity) == expected


def test_scheduler_can_be_installed_later():
    status = AppStatus()
    scheduler = FakeScheduler()
    status.set(ERROR, "przed schedulerem")
    assert status.state == ERROR
    status.set_scheduler(scheduler)
    status.set(ERROR, "po", force=True)
    scheduler.fire()
    assert status.state == IDLE
