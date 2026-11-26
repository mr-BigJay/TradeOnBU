from types import SimpleNamespace
from app.services.hourly_playbook import watch_plans


def test_revisions_keep_identity_and_reject_unknown_anchor():
    plan = dict(strategy="B",direction="LONG",anchor_ms=1000,zone_low=80,zone_high=82,
                expect_fa="بازپس‌گیری حمایت",stop_reference=79,target=100)
    series = {"1h":[SimpleNamespace(time_ms=1000)]}
    first = watch_plans([plan],series)
    assert list(first) == ["B:LONG:1000"]
    assert watch_plans([{**plan,"zone_low":81}],series).keys() == first.keys()
    assert not watch_plans([{**plan,"anchor_ms":2000}],series)
    assert not watch_plans([{**plan,"target":float("nan")}],series)
