from types import SimpleNamespace

from app.services.horizontal_zone_memory import HOUR, advance, snapshot


def test_break_requires_later_retest_and_survives_catalog_removal():
    start = 100 * HOUR
    level = dict(role="support", timeframe="4h", time_ms=1, kind="support", low=100, high=102)
    zones = snapshot({}, [level], [], start)
    bars = [SimpleNamespace(time_ms=start, high=103, low=98, close=99),
            SimpleNamespace(time_ms=start + HOUR, high=101, low=97, close=98)]
    previous = {"analyst_summary": {"horizontal_zones": zones}}
    broken = snapshot(previous, [], bars, start + HOUR)
    key = next(iter(broken))
    assert broken[key]["state"] == "broken"
    retested = advance(broken, bars, start + 2 * HOUR)
    assert retested[key]["state"] == "retested"
    assert advance(retested, bars, start + 2 * HOUR) == retested
