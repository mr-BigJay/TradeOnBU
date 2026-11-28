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


def test_hourly_projection_moves_and_stops_after_convergence():
    from app.services.hourly_playbook import pattern_edges_at_close
    pattern = {"kind":"triangle", "timeframe":"4h",
               "upper":{"anchors":[[1000,110]], "slope":-.001},
               "lower":{"anchors":[[1000,90]], "slope":.001}}
    assert pattern_edges_at_close([pattern], 2000)[0]["lower"] == 91
    assert pattern_edges_at_close([pattern], 3000)[0]["upper"] == 108
    assert not pattern_edges_at_close([pattern], 12000)
    assert not pattern_edges_at_close([pattern], 999)
