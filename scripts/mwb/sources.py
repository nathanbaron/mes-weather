"""Direct model sources: EC GeoMet (HRDPS, RDPS, GDPS), NOAA NOMADS (GFS), MET Norway (ECMWF-based).

Every source is mapped onto one hourly axis in local time so the consensus can
compare like with like. Output per service per model matches the shape the
analysis expects: {"time": [...], "wind_speed_10m": [...kt], ...}.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import io
import math
import os
import re
import tempfile
import time as _time

import numpy as np
import requests

UA = {"User-Agent": "MTO-MES-marine-weather-briefing/1.0 (Marine Engineering Services, Ontario MTO)"}
MS_TO_KT = 1.0 / 0.514444
UTC = dt.timezone.utc

FIELDS = ["wind_speed_10m", "wind_gusts_10m", "wind_direction_10m", "temperature_2m", "dew_point_2m",
          "pressure_msl", "precipitation", "snowfall", "freezing", "visibility", "cape"]


# --------------------------------------------------------------------------- axis helpers

def make_axis(now_utc, tz, days):
    start = now_utc.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    return [(start + dt.timedelta(hours=h)).astimezone(UTC) for h in range(24 * days)]


def _idx(axis):
    return {t: i for i, t in enumerate(axis)}


def place(axis, series, kind):
    """Map {valid_utc: value} onto the axis.

    kind 'inst'  — instantaneous; linear interpolation across gaps of up to 6 h
    kind 'dir'   — direction in degrees; interpolated through u/v
    kind 'total' — accumulation since run start; differenced, spread evenly over each step
    """
    n = len(axis)
    out = np.full(n, np.nan)
    if not series:
        return out
    times = sorted(series)
    pos = _idx(axis)
    if kind == "total":
        prev_t, prev_v = None, None
        for t in times:
            v = series[t]
            if v is None or (isinstance(v, float) and math.isnan(v)):
                prev_t = None
                continue
            if prev_t is not None:
                inc = max(0.0, v - prev_v)
                hours = int((t - prev_t).total_seconds() // 3600)
                if 0 < hours <= 6:
                    for k in range(hours):
                        tt = t - dt.timedelta(hours=k)
                        if tt in pos:
                            out[pos[tt]] = inc / hours
            prev_t, prev_v = t, v
        return out
    for a, b in zip(times, times[1:] + [None]):
        va = series[a]
        if va is None:
            continue
        if a in pos:
            out[pos[a]] = va
        if b is None or series[b] is None:
            continue
        gap = int((b - a).total_seconds() // 3600)
        if 1 < gap <= 6:
            vb = series[b]
            for k in range(1, gap):
                tt = a + dt.timedelta(hours=k)
                if tt not in pos:
                    continue
                f = k / gap
                if kind == "dir":
                    ua, va_ = math.sin(math.radians(va)), math.cos(math.radians(va))
                    ub, vb_ = math.sin(math.radians(vb)), math.cos(math.radians(vb))
                    out[pos[tt]] = (math.degrees(math.atan2(ua + f * (ub - ua), va_ + f * (vb_ - va_))) + 360) % 360
                else:
                    out[pos[tt]] = va + f * (vb - va)
    return out


def finish(axis, tz, cols):
    """Assemble the per-model dict, adding a weather code the analysis understands."""
    n = len(axis)
    d = {"time": [t.astimezone(tz).strftime("%Y-%m-%dT%H:%M") for t in axis]}
    for f in FIELDS:
        d[f] = cols.get(f, np.full(n, np.nan))
    p = np.nan_to_num(d["precipitation"])
    code = np.where(np.isnan(d["wind_speed_10m"]), np.nan, 3.0)
    code = np.where(p >= 0.2, 63, code)
    code = np.where(np.nan_to_num(d["snowfall"]) > 0.1, 73, code)
    code = np.where(np.nan_to_num(d["freezing"]) > 0.05, 66, code)
    d["weather_code"] = code
    return {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in d.items()}


def _nan_to_none(d):
    return {k: [None if (isinstance(x, float) and math.isnan(x)) else x for x in v] if isinstance(v, list) else v
            for k, v in d.items()}


# --------------------------------------------------------------------------- EC GeoMet (WCS)

EC_MODELS = {
    "hrdps": {
        "name": "HRDPS 2.5 km", "horizon_h": 48, "ref_layer": "HRDPS.CONTINENTAL_WSPD",
        "layers": {
            "wind_speed_10m": ("HRDPS.CONTINENTAL_WSPD", "inst", MS_TO_KT),
            "wind_gusts_10m": ("HRDPS.CONTINENTAL_WGE", "inst", MS_TO_KT),
            "wind_direction_10m": ("HRDPS.CONTINENTAL_WD", "dir", 1),
            "temperature_2m": ("HRDPS.CONTINENTAL_TT", "inst", 1),
            "dew_point_2m": ("HRDPS.CONTINENTAL_TD", "inst", 1),
            "pressure_msl": ("HRDPS.CONTINENTAL_PN-SLP", "inst", 0.01),
            "precipitation": ("HRDPS.CONTINENTAL_PR", "total", 1),
            "snowfall": ("HRDPS.CONTINENTAL_SN", "total", 1),      # mm water equiv -> cm at 10:1
            "freezing": ("HRDPS.CONTINENTAL_FR", "total", 1),
            "visibility": ("HRDPS-WEonG_2.5km_LiquidFogVisibility", "inst", 1),
        },
    },
    "rdps": {
        "name": "RDPS 10 km", "horizon_h": 84, "ref_layer": "RDPS_10km_WindSpeed_10m",
        "layers": {
            "wind_speed_10m": ("RDPS_10km_WindSpeed_10m", "inst", MS_TO_KT),
            "wind_gusts_10m": ("RDPS_10km_WindGust_10m", "inst", MS_TO_KT),
            "wind_direction_10m": ("RDPS_10km_WindDir_10m", "dir", 1),
            "temperature_2m": ("RDPS_10km_AirTemp_2m", "inst", 1),
            "dew_point_2m": ("RDPS_10km_DewPoint_2m", "inst", 1),
            "pressure_msl": ("RDPS_10km_Pressure_MSL", "inst", 0.01),
            "precipitation": ("RDPS_10km_Precip-Accum", "total", 1),
            "snowfall": ("RDPS_10km_Snow-Accum", "total", 1),
            "freezing": ("RDPS_10km_FreezingRain-Accum", "total", 1),
            "visibility": ("RDPS-WEonG_10km_LiquidFogVisibility", "inst", 1),
            "cape": ("RDPS_10km_CAPE", "inst", 1),
        },
    },
    "gdps": {
        "name": "GDPS 15 km", "horizon_h": 240, "ref_layer": "GDPS_15km_WindSpeed_10m",
        "layers": {
            "wind_speed_10m": ("GDPS_15km_WindSpeed_10m", "inst", MS_TO_KT),
            "wind_gusts_10m": ("GDPS_15km_WindGust_10m", "inst", MS_TO_KT),
            "wind_direction_10m": ("GDPS_15km_WindDir_10m", "dir", 1),
            "temperature_2m": ("GDPS_15km_AirTemp_2m", "inst", 1),
            "dew_point_2m": ("GDPS_15km_DewPoint_2m", "inst", 1),
            "pressure_msl": ("GDPS_15km_Pressure_MSL", "inst", 0.01),
            "precipitation": ("GDPS_15km_Precip-Accum", "total", 1),
            "snowfall": ("GDPS_15km_Snow-Accum", "total", 1),
            "freezing": ("GDPS_15km_FreezingRain-Accum", "total", 1),
            "cape": ("GDPS_15km_CAPE", "inst", 1),
        },
    },
}


def _ec_caps(wms, layer, session):
    r = session.get(wms, params={"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetCapabilities", "LAYER": layer},
                    timeout=60)
    r.raise_for_status()
    t = re.search(r'<Dimension name="time"[^>]*>([^<]*)<', r.text)
    rt = re.search(r'<Dimension name="reference_time"[^>]*default="([^"]*)"', r.text)
    if not t:
        raise RuntimeError(f"no time dimension for {layer}")
    times = []
    for part in t.group(1).split(","):
        if "/" in part:
            a, b, step = part.split("/")
            a, b = dt.datetime.fromisoformat(a.replace("Z", "+00:00")), dt.datetime.fromisoformat(b.replace("Z", "+00:00"))
            h = int(re.search(r"PT(\d+)H", step).group(1))
            while a <= b:
                times.append(a)
                a += dt.timedelta(hours=h)
        else:
            times.append(dt.datetime.fromisoformat(part.replace("Z", "+00:00")))
    ref = dt.datetime.fromisoformat(rt.group(1).replace("Z", "+00:00")) if rt else None
    return times, ref


def _read_tiff(content):
    from PIL import Image
    im = Image.open(io.BytesIO(content))
    a = np.array(im, dtype=float)
    sx, sy = im.tag_v2[33550][:2]
    x0, y0 = im.tag_v2[33922][3:5]
    return a, x0, y0, sx, sy


def _sample(grid, lat, lon):
    a, x0, y0, sx, sy = grid
    j = int((lon - x0) / sx)
    i = int((y0 - lat) / sy)
    i = min(max(i, 0), a.shape[0] - 1)
    j = min(max(j, 0), a.shape[1] - 1)
    v = a[i, j]
    return None if (math.isnan(v) or abs(v) > 1e8) else float(v)


def fetch_ec_model(key, wms, services, axis, log, workers=12, thin_after_h=48, fields=None):
    """`fields` limits the fetch to those series (the watcher needs only wind and visibility)."""
    m = EC_MODELS[key]
    layers = {f: v for f, v in m["layers"].items() if fields is None or f in fields}
    session = requests.Session()
    session.headers.update(UA)
    try:
        times, ref = _ec_caps(wms, m["ref_layer"], session)
    except Exception as e:  # noqa: BLE001
        log.fail(f"EC {m['name']} capabilities", wms, e)
        return {}, None
    lo = min(s["lon"] for s in services) - 0.3
    hi = max(s["lon"] for s in services) + 0.3
    la0 = min(s["lat"] for s in services) - 0.3
    la1 = max(s["lat"] for s in services) + 0.3
    want = [t for t in times if axis[0] - dt.timedelta(hours=6) <= t <= axis[-1]
            and (ref is None or (t - ref).total_seconds() / 3600 <= m["horizon_h"])]
    if ref is not None:  # beyond 48 h, every 3rd hour is plenty — interpolation fills between
        want = [t for t in want if (t - ref).total_seconds() / 3600 <= thin_after_h or t.hour % 3 == 0]

    def one(job):
        field, layer, t = job
        p = [("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"), ("COVERAGEID", layer),
             ("SUBSETTINGCRS", "EPSG:4326"), ("SUBSET", f"x({lo:.2f},{hi:.2f})"), ("SUBSET", f"y({la0:.2f},{la1:.2f})"),
             ("FORMAT", "image/tiff"), ("TIME", t.strftime("%Y-%m-%dT%H:%M:%SZ"))]
        if ref is not None and not layer.endswith("Visibility"):
            p.append(("DIM_REFERENCE_TIME", ref.strftime("%Y-%m-%dT%H:%M:%SZ")))
        for attempt in range(3):
            try:
                r = session.get(wms, params=p, timeout=60)
                if r.headers.get("content-type", "").startswith("image/tiff"):
                    return field, t, _read_tiff(r.content)
                if r.status_code < 500:
                    return field, t, None
            except requests.RequestException:
                pass
            _time.sleep(1.5 * (attempt + 1))
        return field, t, None

    jobs = [(f, layer, t) for f, (layer, _, _) in layers.items() for t in want]
    grids = {}
    with cf.ThreadPoolExecutor(workers) as ex:
        for field, t, g in ex.map(one, jobs):
            if g is not None:
                grids[(field, t)] = g
    got = len(grids)
    if got == 0:
        log.fail(f"EC {m['name']}", wms, "no coverages returned")
        return {}, ref
    log.ok(f"EC {m['name']}", wms, f"{got}/{len(jobs)} coverages, run {ref:%Y-%m-%d %HZ}" if ref else "")

    out = {}
    for s in services:
        cols = {}
        for field, (_, kind, scale) in layers.items():
            series = {}
            for t in want:
                g = grids.get((field, t))
                if g is not None:
                    v = _sample(g, s["lat"], s["lon"])
                    series[t] = None if v is None else v * scale
            if kind == "total" and ref is not None and series:
                series.setdefault(ref, 0.0)
            cols[field] = place(axis, series, kind)
        out[s["id"]] = cols
    return out, ref


# --------------------------------------------------------------------------- NOAA NOMADS (GFS)

def _gfs_cycle(nomads, now_utc, session, log):
    """Newest GFS cycle whose f120 file exists (i.e. the run is complete enough)."""
    base = nomads.split("/cgi-bin")[0]
    for back in range(0, 30, 6):
        c = (now_utc - dt.timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        c -= dt.timedelta(hours=c.hour % 6)
        url = f"{base}/pub/data/nccf/com/gfs/prod/gfs.{c:%Y%m%d}/{c:%H}/atmos/gfs.t{c:%H}z.pgrb2.0p25.f120.idx"
        try:
            r = session.head(url, timeout=20)
            if r.status_code == 200:
                return c
        except requests.RequestException:
            continue
    log.fail("GFS cycle discovery", base, "no complete cycle found")
    return None


def fetch_gfs(nomads, services, axis, now_utc, log, pace_s=0.55, hourly_until=48):
    try:
        import eccodes
    except ImportError as e:
        log.fail("GFS (NOMADS)", nomads, f"eccodes not installed: {e}")
        return {}, None
    session = requests.Session()
    session.headers.update(UA)
    cyc = _gfs_cycle(nomads, now_utc, session, log)
    if cyc is None:
        return {}, None
    lo = min(s["lon"] for s in services) - 0.5
    hi = max(s["lon"] for s in services) + 0.5
    la0 = min(s["lat"] for s in services) - 0.5
    la1 = max(s["lat"] for s in services) + 0.5
    end_h = int((axis[-1] - cyc).total_seconds() // 3600) + 1
    start_h = max(0, int((axis[0] - cyc).total_seconds() // 3600) - 6)
    hours = [h for h in range(start_h, min(end_h, 120) + 1) if h <= hourly_until or h % 3 == 0]
    raw = {s["id"]: {f: {} for f in ("u", "v", "gust", "t2", "d2", "msl", "tp", "csnow", "cfrzr", "cape", "vis")}
           for s in services}
    ok = 0
    for h in hours:
        q = {"dir": f"/gfs.{cyc:%Y%m%d}/{cyc:%H}/atmos", "file": f"gfs.t{cyc:%H}z.pgrb2.0p25.f{h:03d}",
             "var_UGRD": "on", "var_VGRD": "on", "var_GUST": "on", "var_TMP": "on", "var_DPT": "on",
             "var_PRMSL": "on", "var_APCP": "on", "var_CSNOW": "on", "var_CFRZR": "on", "var_CAPE": "on",
             "var_VIS": "on", "lev_10_m_above_ground": "on", "lev_2_m_above_ground": "on", "lev_surface": "on",
             "lev_mean_sea_level": "on", "subregion": "", "toplat": f"{la1:.2f}", "bottomlat": f"{la0:.2f}",
             "leftlon": f"{lo % 360:.2f}", "rightlon": f"{hi % 360:.2f}"}
        content = None
        for attempt in range(3):
            try:
                r = session.get(nomads, params=q, timeout=60)
                if r.status_code == 200 and len(r.content) > 200:
                    content = r.content
                    break
            except requests.RequestException:
                pass
            _time.sleep(2 * (attempt + 1))
        _time.sleep(pace_s)  # NOMADS blocks clients over ~120 requests/minute
        if content is None:
            continue
        ok += 1
        valid = cyc + dt.timedelta(hours=h)
        with tempfile.NamedTemporaryFile(suffix=".grb2") as tf:
            tf.write(content)
            tf.flush()
            with open(tf.name, "rb") as fh:
                while True:
                    g = eccodes.codes_grib_new_from_file(fh)
                    if g is None:
                        break
                    try:
                        sn = eccodes.codes_get(g, "shortName")
                        step = eccodes.codes_get(g, "stepRange")
                        key = {"10u": "u", "10v": "v", "gust": "gust", "2t": "t2", "2d": "d2", "prmsl": "msl",
                               "cape": "cape", "vis": "vis"}.get(sn)
                        if sn in ("tp", "csnow", "cfrzr"):
                            # keep only the 0-h accumulation / interval ending now
                            if sn == "tp" and not step.startswith("0-"):
                                key = None
                            elif sn != "tp" and "-" in str(step):
                                key = None
                            else:
                                key = sn
                        if key:
                            ni, nj = eccodes.codes_get(g, "Ni"), eccodes.codes_get(g, "Nj")
                            vals = eccodes.codes_get_values(g).reshape(nj, ni)
                            lat1 = eccodes.codes_get(g, "latitudeOfFirstGridPointInDegrees")
                            lon1 = eccodes.codes_get(g, "longitudeOfFirstGridPointInDegrees")
                            dlat = eccodes.codes_get(g, "jDirectionIncrementInDegrees")
                            dlon = eccodes.codes_get(g, "iDirectionIncrementInDegrees")
                            scans_neg = eccodes.codes_get(g, "jScansPositively") == 0
                            for s in services:
                                i = int(round(((lat1 - s["lat"]) if scans_neg else (s["lat"] - lat1)) / dlat))
                                j = int(round(((s["lon"] % 360) - lon1) / dlon))
                                i, j = min(max(i, 0), nj - 1), min(max(j, 0), ni - 1)
                                raw[s["id"]][key][valid] = float(vals[i, j])
                    finally:
                        eccodes.codes_release(g)
    if ok == 0:
        log.fail("GFS (NOMADS)", nomads, "no files returned")
        return {}, cyc
    log.ok("GFS (NOMADS)", nomads, f"{ok}/{len(hours)} steps, run {cyc:%Y-%m-%d %HZ}")
    out = {}
    for s in services:
        r = raw[s["id"]]
        spd = {t: math.hypot(r["u"][t], r["v"][t]) * MS_TO_KT for t in r["u"] if t in r["v"]}
        wdir = {t: (math.degrees(math.atan2(-r["u"][t], -r["v"][t])) + 360) % 360 for t in r["u"] if t in r["v"]}
        tp = dict(r["tp"])
        tp.setdefault(cyc, 0.0)
        precip = place(axis, tp, "total")
        snow_flag = place(axis, r["csnow"], "inst")
        fz_flag = place(axis, r["cfrzr"], "inst")
        out[s["id"]] = {
            "wind_speed_10m": place(axis, spd, "inst"),
            "wind_gusts_10m": place(axis, {t: v * MS_TO_KT for t, v in r["gust"].items()}, "inst"),
            "wind_direction_10m": place(axis, wdir, "dir"),
            "temperature_2m": place(axis, {t: v - 273.15 for t, v in r["t2"].items()}, "inst"),
            "dew_point_2m": place(axis, {t: v - 273.15 for t, v in r["d2"].items()}, "inst"),
            "pressure_msl": place(axis, {t: v / 100 for t, v in r["msl"].items()}, "inst"),
            "precipitation": precip,
            "snowfall": np.where(np.nan_to_num(snow_flag) >= 0.5, precip, 0.0),
            "freezing": np.where(np.nan_to_num(fz_flag) >= 0.5, precip, 0.0),
            "cape": place(axis, r["cape"], "inst"),
            "visibility": place(axis, r["vis"], "inst"),
        }
    return out, cyc


# --------------------------------------------------------------------------- MET Norway (ECMWF-based)

def fetch_metno(url, services, axis, log):
    session = requests.Session()
    session.headers.update(UA)
    contact = os.environ.get("MWB_CONTACT")
    if contact:
        session.headers["User-Agent"] += f" {contact}"
    out, updated = {}, None
    for s in services:
        try:
            r = session.get(url, params={"lat": round(s["lat"], 3), "lon": round(s["lon"], 3)}, timeout=30)
            r.raise_for_status()
            j = r.json()
        except Exception as e:  # noqa: BLE001
            log.fail(f"MET Norway ({s['short']})", url, e)
            continue
        updated = j["properties"]["meta"].get("updated_at")
        cols = {k: {} for k in ("spd", "dir", "t", "td", "p", "pr", "fog")}
        precip_series = {}
        for ts in j["properties"]["timeseries"]:
            t = dt.datetime.fromisoformat(ts["time"].replace("Z", "+00:00"))
            d = ts["data"]["instant"]["details"]
            cols["spd"][t] = d.get("wind_speed", float("nan")) * MS_TO_KT
            cols["dir"][t] = d.get("wind_from_direction")
            cols["t"][t] = d.get("air_temperature")
            cols["td"][t] = d.get("dew_point_temperature")
            cols["p"][t] = d.get("air_pressure_at_sea_level")
            for span, key in ((1, "next_1_hours"), (6, "next_6_hours")):
                blk = ts["data"].get(key)
                if blk and "precipitation_amount" in blk.get("details", {}):
                    amt = blk["details"]["precipitation_amount"]
                    for k in range(span):
                        precip_series.setdefault(t + dt.timedelta(hours=k), amt / span)
                    break
        pos = _idx(axis)
        precip = np.full(len(axis), np.nan)
        for t, v in precip_series.items():
            if t in pos:
                precip[pos[t]] = v
        temp = place(axis, cols["t"], "inst")
        out[s["id"]] = {
            "wind_speed_10m": place(axis, cols["spd"], "inst"),
            "wind_direction_10m": place(axis, cols["dir"], "dir"),
            "temperature_2m": temp,
            "dew_point_2m": place(axis, cols["td"], "inst"),
            "pressure_msl": place(axis, cols["p"], "inst"),
            "precipitation": precip,
            "snowfall": np.where(np.nan_to_num(temp, nan=5) <= 0.5, precip, 0.0),
        }
    if out:
        log.ok("MET Norway (ECMWF-based)", url, f"updated {updated}")
    return out, updated


# --------------------------------------------------------------------------- orchestration

def fetch_all(cfg, log, now_utc, tz):
    """Returns ({service_id: {model display name: series}}, {model name: run info})."""
    axis = make_axis(now_utc, tz, cfg["forecast_days"])
    services = cfg["services"]
    src = cfg["sources"]
    per_model, runs = {}, {}

    for key in ("hrdps", "rdps", "gdps"):
        if key not in src["models"]:
            continue
        data, ref = fetch_ec_model(key, src["geomet_wms"], services, axis, log)
        name = EC_MODELS[key]["name"]
        runs[key] = {"name": name, "init": ref.isoformat() if ref else None, "available": None,
                     "provider": "ECCC MSC GeoMet"}
        if data:
            per_model[name] = data

    if "gfs" in src["models"]:
        data, cyc = fetch_gfs(src["nomads_gfs"], services, axis, now_utc, log)
        runs["gfs"] = {"name": "GFS 0.25°", "init": cyc.isoformat() if cyc else None, "available": None,
                       "provider": "NOAA NOMADS"}
        if data:
            per_model["GFS 0.25°"] = data

    if "ecmwf_metno" in src["models"]:
        data, updated = fetch_metno(src["metno"], services, axis, log)
        runs["ecmwf_metno"] = {"name": "ECMWF (MET Norway)", "init": None, "available": updated,
                               "provider": "MET Norway locationforecast"}
        if data:
            per_model["ECMWF (MET Norway)"] = data

    out = {}
    for s in services:
        models = {}
        for name, data in per_model.items():
            if s["id"] in data:
                models[name] = _nan_to_none(finish(axis, tz, data[s["id"]]))
        out[s["id"]] = models
    return out, runs
