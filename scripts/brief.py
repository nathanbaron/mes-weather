#!/usr/bin/env python3
"""Marine Weather Briefing — build and render.

  python brief.py build  --out DIR [--sample]   fetch, analyse, chart; writes data.json + narrative.json (draft)
  python brief.py render --out DIR              narrative.json + data.json -> briefing.html + briefing.pdf
  python brief.py all    --out DIR [--sample]   both, with the auto-drafted narrative

Claude normally runs `build`, rewrites narrative.json from data.json, the charts and
the EC text forecasts, then runs `render`. `all` exists so the brief still goes out
if no one is there to write it.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import pathlib
import subprocess
import sys
from zoneinfo import ZoneInfo

import numpy as np
import yaml

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from mwb import analyze, charts, fetch, language, sources  # noqa: E402
from mwb.analyze import LEVEL_NAME  # noqa: E402

CHROME_CANDIDATES = ["/opt/pw-browsers/chromium-1243/chrome-linux64/chrome", "chromium", "chromium-browser",
                     "google-chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]


def load_cfg(path=None):
    return yaml.safe_load((pathlib.Path(path) if path else ROOT / "config" / "services.yaml").read_text())


def _jsonable(o):
    if isinstance(o, np.ndarray):
        return [None if (isinstance(x, float) and np.isnan(x)) else x for x in o.tolist()]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else round(float(o), 2)
    raise TypeError(type(o))


def in_ice_season(cfg, d):
    s, e = cfg["ice_season"]["start"], cfg["ice_season"]["end"]
    md = d.strftime("%m-%d")
    return md >= s or md <= e if s > e else s <= md <= e


# =========================================================================== build

def build(out, sample=False, cfg_path=None):
    cfg = load_cfg(cfg_path)
    tz = ZoneInfo(cfg["timezone"])
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(tz)
    out = pathlib.Path(out)
    (out / "img").mkdir(parents=True, exist_ok=True)
    log = fetch.SourceLog()

    if sample:
        from mwb import sample as smp
        start = now_local.replace(minute=0, second=0, microsecond=0, tzinfo=None)
        runs = smp.sample_runs(cfg, now_utc)
    else:
        all_models, runs = sources.fetch_all(cfg, log, now_utc, tz)

    data = {"generated_local": now_local.isoformat(timespec="minutes"), "sample": sample,
            "timezone": cfg["timezone"], "model_runs": runs, "services": {}, "alerts": [], "ec_city": {},
            "images": {}, "ice_season": in_ice_season(cfg, now_local.date())}

    water = {} if sample else fetch.fetch_water_temps(cfg, log, now_local.date())
    days = None
    precip_soon = False
    any_spray = False
    for s in cfg["services"]:
        models = smp.sample_models(cfg, s, start) if sample else all_models.get(s["id"], {})
        if not models:
            data["services"][s["id"]] = {"name": s["name"], "error": "no model data"}
            continue
        cons = analyze.build_consensus(models)
        wt = ({"value_c": cfg["water_temp_fallback_c"][now_local.month], "basis": "monthly estimate (sample run)"}
              if sample else water[s["id"]])
        asmt = analyze.assess_service(cfg, s, cons, wt["value_c"])
        events = analyze.extract_events(cons, asmt, tz)
        daily = analyze.daily_status(cons, asmt)
        days = days or sorted(daily)[: cfg["forecast_days"]]
        png = charts.meteogram(s, cons, asmt, events, tz)
        (out / "img" / f"meteogram_{s['id']}.png").write_bytes(png)
        p24 = np.nansum(np.nan_to_num(cons["precip"][:36]))
        precip_soon |= p24 >= 1.0
        any_spray |= any(c != "none" for c in asmt["spray_class"])
        data["services"][s["id"]] = {
            "name": s["name"], "short": s["short"], "vessels": s["vessels"], "rules": s["rules"],
            "water_temp": wt, "models_used": list(models),
            "events": events, "daily": daily, "peak_wind": analyze.peak_wind_by_day(cons),
            "temp_range": analyze.temp_range_by_day(cons),
            "spray_hours": int(sum(c != "none" for c in asmt["spray_class"])),
            "precip_36h_mm": round(float(p24), 1),
            "snow_5d_cm": round(float(np.nansum(np.nan_to_num(cons["snow"]))), 1),
            "img": f"img/meteogram_{s['id']}.png",
        }
    data["days"] = days or []
    data["precip_expected"] = bool(precip_soon)
    data["spray_expected"] = bool(any_spray)

    if not sample:
        data["alerts"] = fetch.fetch_ec_alerts(cfg, log, now_utc)
        data["ec_marine"] = fetch.fetch_ec_marine(cfg, log)
        for code in sorted({s["ec_city"] for s in cfg["services"] if s.get("ec_city")}):
            data["ec_city"][code] = fetch.fetch_ec_city(cfg, code, log)
        for key, url in cfg["sources"]["synoptic_charts"].items():
            img = fetch.fetch_image(url, log, f"Synoptic chart ({key})")
            if img:
                ext = url.rsplit(".", 1)[-1]
                (out / "img" / f"synoptic_{key}.{ext}").write_bytes(img)
                data["images"][f"synoptic_{key}"] = f"img/synoptic_{key}.{ext}"
        alert_precip = any(w in (a["event"] or "").lower() for a in data["alerts"]
                           for w in ("rain", "snow", "thunder", "freezing", "squall"))
        if precip_soon or alert_precip:
            kind = "snow" if any(min(v["temp_range"].get(data["days"][0], {"max": 5}).values()) < 1
                                 for v in data["services"].values() if "temp_range" in v) else "rain"
            for ext_name in ("local", "approach"):
                frames = fetch.fetch_radar_frames(cfg, cfg["radar_extents"][ext_name], log, now_utc, kind=kind)
                png = charts.radar_panel(frames, cfg["radar_extents"][ext_name], cfg["services"], tz,
                                         f"Radar — {'local area' if ext_name == 'local' else 'approaching weather'}"
                                         f" ({kind}), last {len(frames)} frames, oldest left",
                                         local=(ext_name == "local"))
                if png:
                    (out / "img" / f"radar_{ext_name}.png").write_bytes(png)
                    data["images"][f"radar_{ext_name}"] = f"img/radar_{ext_name}.png"
        if data["ice_season"]:
            ice = fetch.fetch_ice(cfg, log)
            if ice and ice.get("png"):
                ext = ice["url"].rsplit(".", 1)[-1][:4]
                (out / "img" / f"ice.{ext}").write_bytes(ice["png"])
                data["images"]["ice"] = f"img/ice.{ext}"
                data["ice_url"] = ice["url"]

    data["source_log"] = log.entries
    data["stale_models"] = _stale(runs, now_utc)
    (out / "data.json").write_text(json.dumps(data, indent=1, default=_jsonable))
    (out / "narrative.json").write_text(json.dumps(draft_narrative(cfg, data), indent=1))
    return data


def _stale(runs, now_utc, max_age_h=18):
    out = []
    for r in runs.values():
        if not r.get("init") and r.get("available"):
            continue
        if not r.get("init"):
            out.append(f"{r['name']}: run time unknown")
            continue
        age = (now_utc - dt.datetime.fromisoformat(r["init"])).total_seconds() / 3600
        if age > max_age_h:
            out.append(f"{r['name']}: latest run is {age:.0f} h old")
    return out


# =========================================================================== draft narrative

def _t(iso):
    return dt.datetime.fromisoformat(iso).strftime("%a %H:%M")


def draft_narrative(cfg, data):
    """Deterministic, descriptive draft. Claude replaces it; it must still be accurate on its own."""
    svc_text, flagged = {}, []
    for sid, s in data["services"].items():
        if "error" in s:
            svc_text[sid] = "No model data was available for this service on this run."
            continue
        ev = [e for e in s["events"] if e["level"] >= 1]
        if not ev:
            pk = max(s["peak_wind"].values(), key=lambda p: p["wind"]) if s["peak_wind"] else None
            svc_text[sid] = ("Forecast data remain below MES reference thresholds through the period."
                             + (f" Strongest consensus wind is {pk['dir']} {pk['wind']:.0f} kt"
                                + (f", gusting {pk['gust']:.0f} kt." if pk["gust"] is not None else ".")
                                if pk else ""))
            continue
        parts = []
        for e in ev[:4]:
            span = (f"yellow from {_t(e['start'])}, red from {_t(e['first_peak'])}, easing after {_t(e['end'])}"
                    if e["level"] == 2 and e["first_peak"] != e["start"] else
                    f"{e['level_name']} at {_t(e['start'])}" if e["start"] == e["end"] else
                    f"{e['level_name']} from {_t(e['start'])} to {_t(e['end'])}")
            if e["kind"] == "wind":
                p = e["peak"]
                gtxt = f" gusting {p['gust']:.0f} kt" if p["gust"] is not None else ""
                parts.append(f"{e['label']}: {span}, peaking {p['dir']} {p['wind']:.0f} kt{gtxt}"
                             f" near {_t(e['peak_time'])} (model spread {p['spread']:.0f} kt).")
            else:
                parts.append(f"{e['label']}: {span} ({e['why']}).")
        svc_text[sid] = " ".join(parts)
        worst = max(e["level"] for e in ev)
        flagged.append((worst, s["short"], next(e for e in ev if e["level"] == worst)))
    missing = [s["name"] for s in data["services"].values() if "error" in s]
    if len(missing) == len(data["services"]):
        return {"headline": "INCOMPLETE — no model data was retrieved on this run.",
                "summary": "The briefing generator could not retrieve forecast model data for any service. "
                           "No status has been assessed. Refer to Environment Canada forecasts directly.",
                "synopsis": "", "services": svc_text, "ec_crosscheck": "", "ice": "", "radar": "",
                "author": "Auto-drafted by the briefing generator"}
    flagged.sort(key=lambda x: (-x[0], x[2]["start"]))
    if flagged:
        lead = "; ".join(f"{name} {LEVEL_NAME[w]} from {_t(e['first_peak'])}" for w, name, e in flagged[:5])
        summary = f"Forecast data reach or approach MES reference thresholds on this run: {lead}."
    else:
        summary = "Forecast data remain below MES reference thresholds for all services through the five-day period."
    if data["alerts"]:
        summary += " Environment Canada has " + ", ".join(sorted({a["event"] for a in data["alerts"]})) + " in effect."
    if missing:
        summary += f" No model data for: {', '.join(missing)}."
    return {
        "headline": (f"{sum(1 for f in flagged if f[0] == 2)} of {len(data['services'])} services reach red on this run."
                     if any(f[0] == 2 for f in flagged) else
                     "Yellow conditions only; no service reaches red." if flagged else
                     "All services green through the period."),
        "summary": summary,
        "synopsis": "[Synoptic summary to be written from the analysis and forecast charts.]",
        "services": svc_text,
        "ec_crosscheck": "[EC cross-check to be written from the EC public and marine forecasts.]",
        "ice": "[Ice summary from the latest CIS chart.]" if data["ice_season"] else "",
        "radar": "",
        "author": "Auto-drafted by the briefing generator",
    }


# =========================================================================== render

def render(out, cfg_path=None):
    import jinja2
    cfg = load_cfg(cfg_path)
    out = pathlib.Path(out)
    data = json.loads((out / "data.json").read_text())
    nar = json.loads((out / "narrative.json").read_text())
    flags = language.check(nar)
    (out / "language_flags.txt").write_text("\n".join(flags))
    for f in flags:
        print("LANGUAGE:", f)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT / "templates"), autoescape=True)

    def img(rel):
        p = out / rel
        if not rel or not p.exists():
            return None
        mime = {"png": "image/png", "gif": "image/gif", "jpg": "image/jpeg", "jpeg": "image/jpeg"}[p.suffix[1:].lower()]
        return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"

    merged = {}
    for sid, s in data["services"].items():
        for e in s.get("events", []):
            if e["level"] < 1:
                continue
            k = (e["label"] if e["kind"] == "wind" else e["kind"], e["level"], e["first_peak"][:10])
            if k in merged:
                m = merged[k]
                if s["short"] not in m["service"]:
                    m["service"] += ", " + s["short"]
                m["first_peak"] = min(m["first_peak"], e["first_peak"])
                m["end"] = max(m["end"], e["end"])
            else:
                merged[k] = dict(e, service=s["short"])
    reds = [e for e in merged.values() if e["level"] == 2]
    yellows = sorted((e for e in merged.values() if e["level"] == 1), key=lambda e: e["first_peak"])
    key_times = sorted(reds + yellows[: max(0, 10 - len(reds))], key=lambda e: (e["first_peak"], -e["level"]))

    ctx = dict(
        cfg=cfg, d=data, n=nar, img=img,
        ec_alerts_ok=any(e["ok"] for e in data["source_log"] if e["source"].startswith("EC CAP")) or data["sample"],
        key_times=key_times, level_name=LEVEL_NAME,
        colours={-1: "#c9ced3", 0: "#2e7d32", 1: "#e0a800", 2: "#c62828"},
        fmt_day=lambda s: dt.date.fromisoformat(s).strftime("%a %d %b"),
        fmt_t=lambda s: dt.datetime.fromisoformat(s).strftime("%a %H:%M") if s else "—",
        fmt_dt=lambda s: (dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc).strftime("%d %b %H:%MZ")
                          if s else "unknown"),
        generated=dt.datetime.fromisoformat(data["generated_local"]).astimezone(ZoneInfo(data["timezone"])),
    )
    html = env.get_template("briefing.html.j2").render(**ctx)
    (out / "email.html").write_text(env.get_template("email.html.j2").render(**ctx))
    worst = {}
    for e in key_times:
        worst.setdefault(e["level"], []).append(f"{e['service']} {dt.datetime.fromisoformat(e['first_peak']):%a}")
    tag = ("Red: " + "; ".join(dict.fromkeys(worst[2]))) if 2 in worst else (
        ("Yellow: " + "; ".join(dict.fromkeys(worst[1]))) if 1 in worst else "All green")
    (out / "email_subject.txt").write_text(
        f"Marine Weather Briefing — {ctx['generated']:%a %d %b %Y} — {tag}" + (" [SAMPLE]" if data["sample"] else ""))
    (out / "briefing.html").write_text(html)
    pdf = _pdf(out / "briefing.html", out / "briefing.pdf")
    return out / "briefing.html", pdf


def _pdf(src, dst):
    for exe in CHROME_CANDIDATES:
        try:
            subprocess.run([exe, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={dst}", src.resolve().as_uri()],
                           check=True, capture_output=True, timeout=120)
            return dst
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return None


# =========================================================================== cli

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "render", "all"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", action="store_true", help="synthetic data, for testing only")
    ap.add_argument("--config")
    a = ap.parse_args()
    if a.cmd in ("build", "all"):
        d = build(a.out, a.sample, a.config)
        bad = [e for e in d["source_log"] if not e["ok"]]
        print(f"built: {len(d['services'])} services, {len(d['alerts'])} EC alerts, "
              f"{len(bad)} source failures, stale: {d['stale_models'] or 'none'}")
    if a.cmd in ("render", "all"):
        html, pdf = render(a.out, a.config)
        print(f"rendered: {html} {pdf or '(PDF not produced: no Chromium found)'}")


if __name__ == "__main__":
    main()
