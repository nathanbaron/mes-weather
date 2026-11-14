#!/usr/bin/env python3
"""Check every data source the briefing uses, and report model-run timing.

  python check_sources.py            reachability of every URL in config/services.yaml
  python check_sources.py --runs     plus: latest run and availability lag per model,
                                     and which cycle is in hand at the 05:00 build

Run once after setup, and whenever a source starts failing in the brief's
"Not retrieved this run" line. Append --log FILE to keep a history; a week of
--runs output is what settles whether 05:00 reliably catches each cycle.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
UA = {"User-Agent": "MTO-MES-marine-weather-briefing/1.0"}


def probe(url, params=None):
    try:
        r = requests.get(url, params=params, headers=UA, timeout=20)
        return r.status_code, len(r.content), r.headers.get("content-type", "")
    except Exception as e:  # noqa: BLE001
        return None, 0, str(e)[:80]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", action="store_true")
    ap.add_argument("--log")
    a = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config" / "services.yaml").read_text())
    src = cfg["sources"]
    _ = ZoneInfo(cfg["timezone"])  # validates the configured zone
    now = dt.datetime.now(dt.timezone.utc)
    report = {"checked_utc": now.isoformat(timespec="minutes"), "urls": [], "runs": {}}

    s0 = cfg["services"][0]
    checks = []
    for layer in ("HRDPS.CONTINENTAL_WSPD", "RDPS_10km_WindSpeed_10m", "GDPS_15km_WindSpeed_10m"):
        checks.append((f"EC model {layer.split('_')[0].split('.')[0]}", src["geomet_wms"],
                       {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetCapabilities", "LAYER": layer}))
    checks.append(("NOAA NOMADS GFS", src["nomads_gfs"].split("/cgi-bin")[0] + "/pub/data/nccf/com/gfs/prod/", None))
    checks.append(("MET Norway", src["metno"], {"lat": s0["lat"], "lon": s0["lon"]}))
    checks.append(("EC lake temperature", src["geomet_wms"], {"SERVICE": "WMS", "VERSION": "1.3.0",
                   "REQUEST": "GetCapabilities", "LAYER": src["water_temp_layer"]}))
    for base in src["ec_alerts_cap"]:
        checks.append(("EC CAP alerts", base.format(yyyymmdd=now.strftime("%Y%m%d")), None))
    for code in sorted({s["ec_city"] for s in cfg["services"] if s.get("ec_city")}):
        checks.append((f"EC city {code}", src["ec_city_rss"].format(code=code), None))
    for a_ in src["ec_marine_areas"]:
        checks.append((f"EC marine {a_['name']}", a_["url"], None))
    checks.append(("EC radar WMS", src["radar_wms"], {"SERVICE": "WMS", "REQUEST": "GetCapabilities",
                                                      "LAYER": src["radar_layers"]["rain"]}))
    checks.append(("NOAA radar WMS", src["radar_us_wms"], {"SERVICE": "WMS", "REQUEST": "GetCapabilities"}))
    for k, u in src["synoptic_charts"].items():
        checks.append((f"Synoptic {k}", u, None))
    for k, u in src["ice"].items():
        checks.append((f"Ice {k}", u, None))

    print(f"Source check {now:%Y-%m-%d %H:%MZ}\n")
    for name, url, params in checks:
        code, size, ctype = probe(url, params)
        ok = code == 200 and size > 0
        report["urls"].append({"name": name, "url": url, "status": code, "bytes": size, "ok": ok})
        print(f"  {'OK  ' if ok else 'FAIL'} {name:32s} {code or '---'} {size:>9,d} B  {url}")

    if a.runs:
        sys.path.insert(0, str(ROOT / "scripts"))
        from mwb import sources
        print("\nLatest model runs available now\n")
        sess = requests.Session()
        sess.headers.update(UA)
        for key, m in sources.EC_MODELS.items():
            try:
                times, ref = sources._ec_caps(src["geomet_wms"], m["ref_layer"], sess)
                age = (now - ref).total_seconds() / 3600
                report["runs"][key] = {"init": ref.isoformat(), "age_h": round(age, 1)}
                print(f"  {m['name']:20s} run {ref:%d %b %H}Z   ({age:4.1f} h ago)")
            except Exception as e:  # noqa: BLE001
                print(f"  {m['name']:20s} unavailable: {str(e)[:60]}")

        class _L:
            def ok(self, *a): pass
            def fail(self, *a): print("   ", a[0], a[2])
        cyc = sources._gfs_cycle(src["nomads_gfs"], now, sess, _L())
        if cyc:
            report["runs"]["gfs"] = {"init": cyc.isoformat(), "age_h": round((now - cyc).total_seconds() / 3600, 1)}
            print(f"  {'GFS 0.25°':20s} run {cyc:%d %b %H}Z   ({(now - cyc).total_seconds() / 3600:4.1f} h ago, complete to f120)")
        try:
            j = sess.get(src["metno"], params={"lat": s0["lat"], "lon": s0["lon"]}, timeout=30).json()
            print(f"  {'ECMWF (MET Norway)':20s} updated {j['properties']['meta']['updated_at']}")
        except Exception as e:  # noqa: BLE001
            print(f"  MET Norway unavailable: {str(e)[:60]}")
        print("\n  Run with --log daily at 05:00 for a week to see which cycle each model reliably has by then.")

    if a.log:
        with open(a.log, "a") as f:
            f.write(json.dumps(report) + "\n")
    failed = [u for u in report["urls"] if not u["ok"]]
    print(f"\n{len(report['urls']) - len(failed)}/{len(report['urls'])} sources reachable.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
