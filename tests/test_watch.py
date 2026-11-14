"""Checks for the gust rule and the watcher's notice logic. Run: python -m pytest tests -q"""

import copy
import datetime as dt
import pathlib
import sys
from zoneinfo import ZoneInfo

import numpy as np
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import watch  # noqa: E402
from mwb import analyze, language, sources  # noqa: E402

CFG = yaml.safe_load((ROOT / "config" / "services.yaml").read_text())
TZ = ZoneInfo(CFG["timezone"])
NOW = dt.datetime(2026, 11, 14, 15, 0, tzinfo=dt.timezone.utc)
SVC = {s["id"]: s for s in CFG["services"]}


def model(n=72, wind=10.0, gust=14.0, direction=225.0, vis=20000.0, bump=None):
    """One flat model series; `bump` = (first hour, last hour, field, value)."""
    axis = sources.make_axis(NOW, TZ, n // 24)
    cols = {"wind_speed_10m": np.full(n, wind), "wind_gusts_10m": np.full(n, gust),
            "wind_direction_10m": np.full(n, direction), "visibility": np.full(n, vis)}
    for a, b, f, v in bump or []:
        cols[f][a:b + 1] = v
    return sources.finish(axis, TZ, cols), axis


def assess(sid, **kw):
    m, axis = model(**kw)
    cons = analyze.build_consensus({"A": m, "B": copy.deepcopy(m)})
    return cons, analyze.assess_service(CFG, SVC[sid], cons, 8.0), axis


def wi4(asmt):
    return next(c for c in asmt["cats"].values() if c["kind"] == "wind" and c["rule"].get("vessel") == "WOLFE ISLANDER IV")


def test_wi4_rule_is_measured_on_gusts():
    # Sustained 13 kt SW: gusts of 22 kt are yellow, gusts of 25 kt are red.
    _, asmt, _ = assess("wolfe", wind=13, gust=14, bump=[(10, 12, "wind_gusts_10m", 22), (30, 31, "wind_gusts_10m", 25)])
    lvl = wi4(asmt)["level"]
    assert list(lvl[9:14]) == [0, 1, 1, 1, 0] and list(lvl[29:33]) == [0, 2, 2, 0]
    assert "gusts 22 kt" in wi4(asmt)["why"][10] and "20 kt gust reference" in wi4(asmt)["why"][10]
    assert "at or above the 25 kt gust reference" in wi4(asmt)["why"][30]


def test_wi4_gusts_outside_sector_stay_green():
    _, asmt, _ = assess("wolfe", wind=13, gust=28, direction=45)
    assert wi4(asmt)["level"].max() == 0


def test_wi4_yellow_starts_at_twenty():
    _, asmt, _ = assess("wolfe", wind=10, gust=19)
    assert set(wi4(asmt)["level"]) == {0}
    _, asmt, _ = assess("wolfe", wind=10, gust=20)
    assert set(wi4(asmt)["level"]) == {1}
    _, asmt, _ = assess("wolfe", wind=10, gust=24.9)
    assert set(wi4(asmt)["level"]) == {1}


def test_sustained_rules_are_unchanged():
    # Glenora east quadrant, 20 kt sustained: gusts of 24 kt with 12 kt sustained are yellow, not red.
    _, asmt, _ = assess("glenora", wind=12, gust=24, direction=90)
    assert set(asmt["cats"]["wind:0"]["level"]) == {1}
    _, asmt, _ = assess("glenora", wind=21, gust=24, direction=90)
    assert set(asmt["cats"]["wind:0"]["level"]) == {2}


def test_visibility_red_below_half_a_mile():
    _, asmt, _ = assess("simcoe", bump=[(5, 6, "visibility", 0.4 * 1852)])
    assert list(asmt["cats"]["vis"]["level"][4:8]) == [0, 2, 2, 0]
    _, asmt, _ = assess("simcoe", bump=[(5, 6, "visibility", 0.5 * 1852)])
    assert asmt["cats"]["vis"]["level"].max() == 1


def state_with(bump=None, now=NOW, **kw):
    m, axis = model(bump=bump, **kw)
    st = {"models": {}}
    for key, name in (("hrdps", "HRDPS 2.5 km"), ("rdps", "RDPS 10 km")):
        cols = {f: np.array(m[f], dtype=float) for f in watch.WATCH_FIELDS}
        watch._store(st, key, name, watch._iso(now), axis, {sid: cols for sid in SVC}, now)
    return st


def run(st, now=NOW, changed=True):
    current, picture = watch.evaluate(CFG, st, now, TZ)
    return watch.diff_forecast(CFG, st, current, now, TZ, changed), picture


def test_forecast_notice_sent_once_then_withdrawn():
    st = state_with(wind=13, gust=14, bump=[(20, 24, "wind_gusts_10m", 27)])
    items, _ = run(st)
    assert [(i["type"], i["service"]) for i in items] == [("forecast", "Wolfe Island")]
    assert "WOLFE ISLANDER IV" in items[0]["label"] and "gusts 27 kt" in items[0]["text"]
    assert run(st)[0] == []                                # same forecast: silence

    calm = state_with(wind=13, gust=14)
    calm["announced"] = st["announced"]
    assert run(calm)[0] == []                              # gone on one model update: wait
    items, _ = run(calm)                                   # gone on a second: withdrawn
    assert [i["type"] for i in items] == ["withdrawn"] and calm["announced"] == {}


def test_forecast_period_that_shifts_is_not_announced_twice():
    st = state_with(wind=13, gust=14, bump=[(20, 24, "wind_gusts_10m", 27)])
    run(st)
    later = state_with(wind=13, gust=14, bump=[(23, 27, "wind_gusts_10m", 23)])
    later["announced"] = st["announced"]
    assert run(later)[0] == []


def test_same_models_do_not_withdraw():
    st = state_with(wind=13, gust=14, bump=[(20, 24, "wind_gusts_10m", 27)])
    run(st)
    calm = state_with(wind=13, gust=14)
    calm["announced"] = st["announced"]
    for _ in range(4):
        assert run(calm, changed=False)[0] == []


def test_visibility_notice_groups_services():
    st = state_with(bump=[(5, 6, "visibility", 600.0)])
    items, _ = run(st)
    assert len(items) == 5 and {i["label"] for i in items} == {"Visibility"}
    subject, text, page, flags = watch.compose(items, NOW, TZ)
    assert flags == [] and text.count("FORECAST —") == 1 and "Wolfe Island, Amherst Island" in text


def test_cache_follows_the_clock():
    st = state_with(wind=13, gust=14, bump=[(20, 24, "wind_gusts_10m", 27)])
    per, axis = watch.cached_models(CFG, st, NOW + dt.timedelta(hours=5), TZ)
    g = per["wolfe"]["HRDPS 2.5 km"]["wind_gusts_10m"]
    assert g[15:20] == [27.0] * 5 and g[14] == 14.0       # five hours on, the same period sits five hours sooner
    per, _ = watch.cached_models(CFG, st, NOW + dt.timedelta(hours=19), TZ)
    assert per["wolfe"] == {}                              # past model_max_age_h: left out


def reading(wind, gust, direction, minutes=0, services=("wolfe", "howe", "simcoe", "amherst")):
    return [{"station": "KINGSTON CLIMATE", "services": list(services), "time": watch._iso(NOW + dt.timedelta(minutes=minutes)),
             "wind_kt": wind, "dir": direction, "gust_kt": gust}]


def test_observed_gust_notice_and_repeat_rules():
    st = {}
    at = lambda m: NOW + dt.timedelta(minutes=m)  # noqa: E731
    assert watch.diff_obs(CFG, st, reading(14, 22, 230), at(0), TZ) == []                # yellow range: silent
    items = watch.diff_obs(CFG, st, reading(14, 26, 230), at(0), TZ)
    assert [i["label"] for i in items] == ["WOLFE ISLANDER IV — Kingston secondary ramp, SW quadrant"]
    assert "10-minute peak 26 kt" in items[0]["text"] and "25 kt gust reference" in items[0]["text"]
    assert watch.diff_obs(CFG, st, reading(14, 27, 230, 30), at(30), TZ) == []           # still on: silence
    assert len(watch.diff_obs(CFG, st, reading(16, 32, 230, 60), at(60), TZ)) == 1       # risen 5 kt: repeat
    assert len(watch.diff_obs(CFG, st, reading(16, 32, 230, 250), at(250), TZ)) == 1     # three hours on: repeat
    assert watch.diff_obs(CFG, st, reading(8, 12, 230, 280), at(280), TZ) == [] and st["obs_active"]
    assert watch.diff_obs(CFG, st, reading(8, 12, 230, 310), at(310), TZ) == [] and not st["obs_active"]


def test_observed_sustained_rules():
    # West 31 kt gusting 35: both Wolfe sustained rules and the WOLFE ISLANDER IV gust rule, Amherst dead west,
    # Howe hard west. Simcoe has no rules.
    items = watch.diff_obs(CFG, {}, reading(31, 35, 270), NOW, TZ)
    assert {i["service"] for i in items} == {"Wolfe Island", "Amherst Island", "Howe Island"} and len(items) == 5
    # East 31 kt: Amherst dead east (30) and Howe hard east (25) only.
    assert {i["service"] for i in watch.diff_obs(CFG, {}, reading(31, 35, 100), NOW, TZ)} == {"Amherst Island", "Howe Island"}
    assert watch.diff_obs(CFG, {}, reading(18, 19, 250), NOW, TZ) == []


def test_notice_wording_passes_the_language_check():
    st = state_with(wind=32, gust=40, direction=270, bump=[(5, 6, "visibility", 600.0)])
    items, _ = run(st)
    items += watch.diff_obs(CFG, {}, reading(31, 35, 270), NOW, TZ)
    subject, text, page, flags = watch.compose(items, NOW, TZ)
    assert flags == []
    assert language.check({"t": subject}) == []
    assert "Marine weather watch" in subject and "Master makes the final decision" in text


def test_marine_warning_reaches_the_alert_header():
    from mwb import fetch
    marine = [{"name": "Eastern Lake Ontario", "url": "u", "warnings": ["Strong wind warning in effect"]},
              {"name": "Kingston to Cornwall", "url": "u", "warnings": []}]
    got = fetch.marine_warning_alerts(marine, [])
    assert [(a["event"], a["areas"]) for a in got] == [("strong wind warning", ["Eastern Lake Ontario (EC marine forecast)"])]
    # Already carried by the CAP feed: not listed twice.
    assert fetch.marine_warning_alerts(marine, [{"event": "Strong Wind Warning"}]) == []
    assert fetch.marine_warning_alerts([], []) == [] and fetch.marine_warning_alerts(None, []) == []
