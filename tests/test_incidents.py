from api_health_monitor.checker import CheckResult, Reason, Status
from api_health_monitor.config import Endpoint
from api_health_monitor.incidents import EventKind, IncidentTracker, State

EP = Endpoint(name="api", url="http://x/", failures_to_alert=3, successes_to_recover=2)

UP = (Status.UP, Reason.OK)
DOWN = (Status.DOWN, Reason.HTTP_STATUS)
SLOW = (Status.DEGRADED, Reason.SLOW)


def feed(seq, ep=EP):
    tr = IncidentTracker([ep])
    events = []
    for t, (status, reason) in enumerate(seq):
        events += tr.observe(CheckResult(ep.name, float(t), status, reason))
    return tr, events


def test_single_blip_does_not_alert():
    _, ev = feed([UP, DOWN, UP, DOWN, DOWN, UP])
    assert ev == []


def test_three_failures_alert_once_with_true_start():
    tr, ev = feed([UP, UP, DOWN, DOWN, DOWN, DOWN, DOWN])
    assert [e.kind for e in ev] == [EventKind.DOWN]
    assert ev[0].incident_started_at == 2.0   # first failing check, not detection time
    assert ev[0].at == 4.0
    assert tr.state("api") is State.DOWN


def test_recovery_needs_two_successes():
    _, ev = feed([DOWN, DOWN, DOWN, UP, DOWN, UP, UP])
    assert [e.kind for e in ev] == [EventKind.DOWN, EventKind.RECOVERED]
    assert ev[1].incident_started_at == 0.0
    assert ev[1].duration_s == 6.0


def test_degraded_cycle():
    _, ev = feed([UP, SLOW, SLOW, SLOW, UP, SLOW, UP, UP])
    assert [e.kind for e in ev] == [EventKind.DEGRADED, EventKind.DEGRADED_RESOLVED]
    assert ev[1].incident_started_at == 1.0


def test_slow_then_down_then_recovered():
    _, ev = feed([SLOW, SLOW, SLOW, DOWN, DOWN, DOWN, UP, UP])
    assert [e.kind for e in ev] == [EventKind.DEGRADED, EventKind.DOWN, EventKind.RECOVERED]


def test_realert_reminder():
    ep = Endpoint(name="api", url="http://x/", failures_to_alert=1, realert_every_s=3)
    _, ev = feed([DOWN] * 8, ep)
    assert [e.kind for e in ev] == [EventKind.DOWN, EventKind.STILL_DOWN, EventKind.STILL_DOWN]


def test_threshold_one_alerts_immediately():
    ep = Endpoint(name="api", url="http://x/", failures_to_alert=1, successes_to_recover=1)
    _, ev = feed([UP, DOWN, UP], ep)
    assert [e.kind for e in ev] == [EventKind.DOWN, EventKind.RECOVERED]


def test_first_contact_down_still_alerts():
    _, ev = feed([DOWN, DOWN, DOWN])
    assert [e.kind for e in ev] == [EventKind.DOWN]
