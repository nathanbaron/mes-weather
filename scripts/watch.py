#!/usr/bin/env python3
"""Marine weather watch — checks the forecast and station readings between briefings.

  python watch.py --state DIR [--dry-run] [--refetch]

No model writes anything here. Each run:
  1. reads the latest ECCC station winds and compares them with the reference thresholds;
  2. asks each forecast model whether it has a new run, and re-fetches only those that do
     (wind, gust, direction and visibility for the five routes);
  3. rebuilds the consensus with the same code the briefing uses and finds the hours at
     or above a reference threshold in the next `watch.horizon_h` hours;
  4. sends one short email when something is new, and nothing when nothing has changed.

State lives in DIR: state.json (model cache and what has already been sent),
latest.json (the current picture, for the briefing or anyone else to read) and
alerts.jsonl (every notice, one per line).

The watch informs; it never directs. The Master makes the final decision.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import html
import json
import os
import pathlib
import sys
from zoneinfo import ZoneInfo

import numpy as np
import requests
import yaml

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from mwb import analyze, fetch, language, sources  # noqa: E402
from mwb.analyze import compass, in_sector, wind_text  # noqa: E402

UTC = dt.timezone.utc
WATCH_FIELDS = ["wind_speed_10m", "wind_gusts_10m", "wind_direction_10m", "visibility"]
KMH_TO_KT = 1 / 1.852
FOOTER = ("For awareness. Reference thresholds are planning values, not operating limits, and a notice is not "
          "a decision. The Master makes the final decision on every crossing, using conditions observed on scene. "
          "Official warnings are those issued by Environment and Climate Change Canada.")


def _iso(t):
    return t.astimezone(UTC).isoformat(timespec="minutes")


def _parse(s):
    return dt.datetime.fromisoformat(s)


def _when(t, tz):
    return t.astimezone(tz).strftime("%a %d %b %H:%M")


# =========================================================================== forecast models

def _store(state, key, name, run, axis, data, now):
    state["models"][key] = {
        "name": name, "run": run, "fetched": _iso(now), "axis0": _iso(axis[0]),
        "series": {sid: {f: [None if np.isnan(x) else round(float(x), 1) for x in cols[f]]
                         for f in WATCH_FIELDS if f in cols} for sid, cols in data.items()},
    }


def refresh_models(cfg, state, now, tz, log, refetch=False):
    """Re-fetch only the models that have a new run. Returns the keys that changed."""
    w, src, services = cfg["watch"], cfg["sources"], cfg["services"]
    axis = sources.make_axis(now, tz, -(-w["horizon_h"] // 24))
    models = state.setdefault("models", {})
    changed = []
    session = requests.Session()
    session.headers.update(sources.UA)

    for key in ("hrdps", "rdps", "gdps"):
        if key not in src["models"]:
            continue
        m = sources.EC_MODELS[key]
        try:
            _, ref = sources._ec_caps(src["geomet_wms"], m["ref_layer"], session)
        except Exception as e:  # noqa: BLE001
            log.fail(f"EC {m['name']} run check", src["geomet_wms"], e)
            continue
        run = _iso(ref) if ref else None
        if refetch or run != models.get(key, {}).get("run"):
            data, _ = sources.fetch_ec_model(key, src["geomet_wms"], services, axis, log, fields=WATCH_FIELDS)
            if data:
                _store(state, key, m["name"], run, axis, data, now)
                changed.append(key)

    if "gfs" in src["models"]:
        cyc = sources._gfs_cycle(src["nomads_gfs"], now, session, log)
        run = _iso(cyc) if cyc else None
        if cyc and (refetch or run != models.get("gfs", {}).get("run")):
            data, _ = sources.fetch_gfs(src["nomads_gfs"], services, axis, now, log, hourly_until=0)
            if data:
                _store(state, "gfs", "GFS 0.25°", run, axis, data, now)
                changed.append("gfs")

    if "ecmwf_metno" in src["models"]:
        prev = models.get("ecmwf_metno", {})
        age_min = (now - _parse(prev["fetched"])).total_seconds() / 60 if prev else 1e9
        if refetch or age_min >= w["metno_refresh_min"]:
            data, updated = sources.fetch_metno(src["metno"], services, axis, log)
            if data:
                _store(state, "ecmwf_metno", "ECMWF (MET Norway)", updated, axis, data, now)
                if updated != prev.get("run"):
                    changed.append("ecmwf_metno")
    return changed


def cached_models(cfg, state, now, tz):
    """{service_id: {model name: series on the current hourly axis}}, leaving out stale runs."""
    w = cfg["watch"]
    axis = sources.make_axis(now, tz, -(-w["horizon_h"] // 24))
    n = len(axis)
    out = {s["id"]: {} for s in cfg["services"]}
    for key, m in state.get("models", {}).items():
        stamp = m["fetched"] if key == "ecmwf_metno" else (m["run"] or m["fetched"])
        if (now - _parse(stamp)).total_seconds() / 3600 > w["model_max_age_h"]:
            continue
        off = int(round((axis[0] - _parse(m["axis0"])).total_seconds() / 3600))
        for sid, fields in m["series"].items():
            cols = {}
            for f, vals in fields.items():
                a = np.full(n, np.nan)
                for i in range(n):
                    j = i + off
                    if 0 <= j < len(vals) and vals[j] is not None:
                        a[i] = vals[j]
                cols[f] = a
            if sid in out:
                out[sid][m["name"]] = sources.finish(axis, tz, cols)
    return out, axis


def flagged_runs(cons, assessment, categories, level, axis, gap_h=2):
    """Contiguous periods at or above `level` for the watched categories."""
    runs = {}
    for key, cat in assessment["cats"].items():
        if cat["kind"] not in categories:
            continue
        lvl = cat["level"]
        i = 0
        while i < len(lvl):
            if lvl[i] < level:
                i += 1
                continue
            j = i
            while True:  # bridge short gaps so one episode is one notice
                nxt = next((k for k in range(j + 1, min(j + 2 + gap_h, len(lvl))) if lvl[k] >= level), None)
                if nxt is None:
                    break
                j = nxt
            r = {"label": cat["label"], "kind": cat["kind"], "level": int(lvl[i:j + 1].max()),
                 "start": _iso(axis[i]), "end": _iso(axis[j]), "why": cat["why"][i]}
            if cat["kind"] == "wind":
                on_gust = cat["rule"].get("measure") == "gust"
                series = np.where(np.isnan(cons["gust"]), cons["wind"], cons["gust"]) if on_gust else cons["wind"]
                k = i + int(np.nanargmax(np.nan_to_num(series[i:j + 1], nan=-1)))
                g = cons["gust"][k]
                r["peak"] = wind_text(compass(cons["dir"][k]), cons["wind"][k], None if np.isnan(g) else g)
                r["peak_time"] = _iso(axis[k])
                r["spread_kt"] = round(float(cons["wind_max"][k] - cons["wind_min"][k]))
            runs.setdefault(key, []).append(r)
            i = j + 1
    return runs


def evaluate(cfg, state, now, tz):
    """Consensus and status for every service from the cached models."""
    w = cfg["watch"]
    per_service, axis = cached_models(cfg, state, now, tz)
    water = cfg["water_temp_fallback_c"][now.astimezone(tz).month]
    current, picture = {}, {}
    for s in cfg["services"]:
        models = per_service[s["id"]]
        if not models:
            picture[s["id"]] = {"name": s["name"], "error": "no model data"}
            continue
        cons = analyze.build_consensus(models)
        asmt = analyze.assess_service(cfg, s, cons, water)
        for key, runs in flagged_runs(cons, asmt, w["categories"], w["notify_level"], axis).items():
            current[f"{s['id']}|{key}"] = runs
        picture[s["id"]] = {
            "name": s["name"], "short": s["short"], "models_used": list(models),
            "events": [e for e in analyze.extract_events(cons, asmt, tz) if e["kind"] in w["categories"]],
            "daily": {d: {"wind": max([v for k, v in x["cats"].items() if k.startswith("wind:")], default=-1),
                          "vis": x["cats"].get("vis", -1)} for d, x in analyze.daily_status(cons, asmt).items()},
            "peak_wind": analyze.peak_wind_by_day(cons),
        }
    return current, picture


def _overlaps(a, b, pad_h=6):
    pad = dt.timedelta(hours=pad_h)
    return _parse(a["start"]) - pad <= _parse(b["end"]) and _parse(b["start"]) <= _parse(a["end"]) + pad


def diff_forecast(cfg, state, current, now, tz, models_changed):
    """Notices for forecast periods not yet announced, and for announced ones that have gone."""
    short = {s["id"]: s["short"] for s in cfg["services"]}
    ann = state.setdefault("announced", {})
    items = []
    for key, runs in current.items():
        prev = ann.setdefault(key, [])
        for r in runs:
            match = next((a for a in prev if _overlaps(a, r)), None)
            if match:
                match.update(start=r["start"], end=r["end"], missing=0, seen=True)
                continue
            prev.append({"start": r["start"], "end": r["end"], "label": r["label"], "sent": _iso(now),
                         "missing": 0, "seen": True})
            span = (f"at {_when(_parse(r['start']), tz)}" if r["start"] == r["end"] else
                    f"from {_when(_parse(r['start']), tz)} to about {_when(_parse(r['end']), tz)}")
            text = f"forecast {r['why']}, {span}"
            if r["kind"] == "wind":
                text += (f"; peak {r['peak']} near {_when(_parse(r['peak_time']), tz)}"
                         f" (model spread {r['spread_kt']} kt)")
            items.append({"type": "forecast", "service": short[key.split('|')[0]], "label": r["label"],
                          "text": text + "."})
    for key in list(ann):
        keep = []
        for a in ann[key]:
            if a.pop("seen", False):
                keep.append(a)
            elif _parse(a["end"]) < now:
                continue                                  # the period has passed
            else:
                a["missing"] += 1 if models_changed else 0
                if a["missing"] >= cfg["watch"]["clear_after_runs"]:
                    items.append({"type": "withdrawn", "service": short[key.split("|")[0]], "label": a["label"],
                                  "text": f"no longer at the reference threshold in the current forecast "
                                          f"(was {_when(_parse(a['start']), tz)} to {_when(_parse(a['end']), tz)})."})
                else:
                    keep.append(a)
        if keep:
            ann[key] = keep
        else:
            del ann[key]
    return items


# =========================================================================== station readings

def fetch_obs(cfg, now, log):
    o = cfg["watch"]["obs"]
    out = []
    for st in o["stations"]:
        try:
            r = requests.get(o["api"], headers=sources.UA, timeout=30, params={
                "f": "json", "msc_id-value": st["msc_id"], "limit": 1, "sortby": "-date_tm-value",
                "properties": "date_tm-value,avg_wnd_spd_10m_pst10mts,avg_wnd_dir_10m_pst10mts,max_wnd_spd_10m_pst10mts"})
            r.raise_for_status()
            p = r.json()["features"][0]["properties"]
            t = _parse(p["date_tm-value"].replace("Z", "+00:00"))
            spd, d, mx = p.get("avg_wnd_spd_10m_pst10mts"), p.get("avg_wnd_dir_10m_pst10mts"), p.get("max_wnd_spd_10m_pst10mts")
            age = (now - t).total_seconds() / 60
            if spd is None or d is None or age > o["max_age_min"]:
                log.fail(f"Station {st['name']}", o["api"], f"no usable wind (reading {age:.0f} min old)")
                continue
            out.append({"station": st["name"], "services": st["services"], "time": _iso(t),
                        "wind_kt": round(spd * KMH_TO_KT, 1), "dir": int(d),
                        "gust_kt": None if mx is None else round(mx * KMH_TO_KT, 1)})
            log.ok(f"Station {st['name']}", o["api"], f"{age:.0f} min old")
        except Exception as e:  # noqa: BLE001
            log.fail(f"Station {st['name']}", o["api"], e)
    return out


def diff_obs(cfg, state, readings, now, tz):
    o = cfg["watch"]["obs"]
    active = state.setdefault("obs_active", {})
    by_id = {s["id"]: s for s in cfg["services"]}
    items, hit = [], set()
    for rd in readings:
        for sid in rd["services"]:
            for r_i, rule in enumerate(by_id[sid]["rules"]):
                on_gust = rule.get("measure") == "gust"
                v = rd["gust_kt"] if on_gust and rd["gust_kt"] is not None else rd["wind_kt"]
                if not (in_sector(rd["dir"], rule["sector"]) and v >= rule["threshold_kt"]):
                    continue
                key = f"{sid}|wind:{r_i}"
                hit.add(key)
                a = active.get(key)
                due = (a is None
                       or (now - _parse(a["sent"])).total_seconds() / 3600 >= o["renotify_h"]
                       or v >= a["value"] + o["renotify_rise_kt"])
                if a:
                    a["below"] = 0
                if not due:
                    continue
                active[key] = {"since": a["since"] if a else rd["time"], "sent": _iso(now), "value": v, "below": 0}
                peak = "" if rd["gust_kt"] is None else f", 10-minute peak {rd['gust_kt']:.0f} kt"
                items.append({"type": "observed", "service": by_id[sid]["short"], "label": rule["label"],
                              "text": f"{rd['station']} reported {compass(rd['dir'])} {rd['wind_kt']:.0f} kt{peak} at "
                                      f"{_when(_parse(rd['time']), tz)}, at or above the {rule['threshold_kt']} kt "
                                      f"{'gust ' if on_gust else ''}reference"
                                      + (f" (continuing since {_when(_parse(a['since']), tz)})." if a else ".")})
    covered = {f"{sid}|" for rd in readings for sid in rd["services"]}
    for key in list(active):
        if key in hit or not any(key.startswith(c) for c in covered):
            continue                                      # still on, or no reading for it this time
        active[key]["below"] += 1
        if active[key]["below"] >= o["clear_after_checks"]:
            del active[key]
    return items


# =========================================================================== notice

def compose(items, now, tz):
    """Group identical notices across services; return subject, text, html."""
    grouped = {}
    for it in items:
        grouped.setdefault((it["type"], it["label"], it["text"]), []).append(it["service"])
    title = {"observed": "Observed", "forecast": "Forecast", "withdrawn": "Withdrawn"}
    order = {"observed": 0, "forecast": 1, "withdrawn": 2}
    lines = []
    for (typ, label, text), svcs in sorted(grouped.items(), key=lambda kv: order[kv[0][0]]):
        who = ", ".join(dict.fromkeys(svcs))
        head = who if label.startswith(who) else f"{who} — {label}"
        lines.append((title[typ], head, text))
    first = lines[0]
    subject = f"Marine weather watch — {first[0]}: {first[1]}" + (f" (+{len(lines) - 1} more)" if len(lines) > 1 else "")
    stamp = f"Issued {_when(now, tz)} {now.astimezone(tz).tzname()}"
    text = "\n\n".join(f"{t.upper()} — {h}: {x}" for t, h, x in lines)
    text = f"Marine weather watch\n{stamp}\n\n{text}\n\nStation readings are from shore stations and may differ from conditions on the route.\n\n{FOOTER}\n"
    body = "".join(f'<p style="margin:0 0 10px"><b>{html.escape(t)} — {html.escape(h)}</b><br>{html.escape(x)}</p>'
                   for t, h, x in lines)
    page = (f'<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#1b1f24;max-width:680px">'
            f'<div style="font-size:18px;font-weight:700;border-bottom:3px solid #0b3d5c;padding-bottom:6px">Marine weather watch</div>'
            f'<div style="font-size:12px;color:#5f6b76;margin:6px 0 12px">{html.escape(stamp)}</div>{body}'
            f'<p style="font-size:12px;color:#5f6b76">Station readings are from shore stations and may differ from conditions on the route.</p>'
            f'<p style="font-size:11px;color:#5f6b76;border-top:1px solid #d9dee3;padding-top:8px">{html.escape(FOOTER)}</p></div>')
    flags = language.check({"notice": " ".join(x for _, _, x in lines)})
    return subject, text, page, flags


def send(cfg, subject, text, page, now):
    key = os.environ.get("AGENTMAIL_API_KEY")
    if not key:
        raise RuntimeError("AGENTMAIL_API_KEY is not set")
    from agentmail import AgentMail
    a = cfg["watch"]["alerts"]
    r = AgentMail(api_key=key).inboxes.messages.send(
        a["from_inbox"], to=a["recipients"], subject=subject, html=page, text=text,
        idempotency_key=f"mww-{now:%Y%m%dT%H%M}")
    return r.message_id


# =========================================================================== main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--state", required=True, help="folder that holds state.json, latest.json and alerts.jsonl")
    ap.add_argument("--dry-run", action="store_true", help="print the notice; do not send it")
    ap.add_argument("--refetch", action="store_true", help="re-fetch every model whether or not it has a new run")
    ap.add_argument("--config")
    a = ap.parse_args()

    cfg = yaml.safe_load((pathlib.Path(a.config) if a.config else ROOT / "config" / "services.yaml").read_text())
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(UTC)
    sdir = pathlib.Path(a.state)
    sdir.mkdir(parents=True, exist_ok=True)
    sfile = sdir / "state.json"
    state = json.loads(sfile.read_text()) if sfile.exists() else {}
    log = fetch.SourceLog()

    readings = fetch_obs(cfg, now, log)
    changed = refresh_models(cfg, state, now, tz, log, refetch=a.refetch)
    before = copy.deepcopy({k: state.get(k, {}) for k in ("announced", "obs_active")})
    current, picture = evaluate(cfg, state, now, tz)
    items = diff_obs(cfg, state, readings, now, tz) + diff_forecast(cfg, state, current, now, tz, bool(changed))

    sent, error = None, None
    if items:
        subject, text, page, flags = compose(items, now, tz)
        if flags:
            error = "language check: " + "; ".join(flags)
        elif a.dry_run:
            print(f"--- would send to {cfg['watch']['alerts']['recipients']} ---\n{subject}\n\n{text}")
            sent = "dry-run"
        else:
            try:
                sent = send(cfg, subject, text, page, now)
            except Exception as e:  # noqa: BLE001
                error = f"send failed: {e}"
        if error:
            state.update(before)                          # nothing was delivered, so nothing counts as announced
        with (sdir / "alerts.jsonl").open("a") as fh:
            fh.write(json.dumps({"time": _iso(now), "sent": sent, "error": error, "subject": subject,
                                 "items": items}) + "\n")

    failures = [e for e in log.entries if not e["ok"]]
    (sdir / "latest.json").write_text(json.dumps({
        "checked": _iso(now), "checked_local": _when(now, tz),
        "model_runs": {k: {"name": m["name"], "run": m["run"], "fetched": m["fetched"]}
                       for k, m in state.get("models", {}).items()},
        "models_updated_this_check": changed, "station_readings": readings,
        "observed_at_or_above": state.get("obs_active", {}), "forecast_announced": state.get("announced", {}),
        "services": picture, "source_failures": failures}, indent=1, default=float))
    state["last_check"] = _iso(now)
    sfile.write_text(json.dumps(state, separators=(",", ":")))

    print(f"checked {_when(now, tz)}: {len(readings)} station readings, models updated: {changed or 'none'}, "
          f"{len(items)} notices ({sent or error or 'nothing to send'}), {len(failures)} source failures")
    for f in failures:
        print(f"  source failure: {f['source']}: {f['detail']}")

    no_data = not state.get("models") or all("error" in p for p in picture.values())
    if error or no_data:
        sys.exit(f"watch failed: {error or 'no forecast model data'}")
    beat = os.environ.get("MWB_HEARTBEAT_URL")
    if beat:
        try:
            requests.get(beat, timeout=10)
        except requests.RequestException as e:
            print(f"  heartbeat not delivered: {e}")


if __name__ == "__main__":
    main()
