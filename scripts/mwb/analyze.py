"""Consensus, colour status, and event extraction.

Status levels: 0 green, 1 yellow, 2 red, -1 not assessed (no rule defined).
The status compares forecast data with MES reference thresholds to draw attention.
It is not an operating limit or an instruction; the Master decides.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np

LEVEL_NAME = {-1: "not assessed", 0: "green", 1: "yellow", 2: "red"}
SPRAY_RANK = {"none": 0, "light": 1, "moderate": 2, "heavy": 3, "extreme": 4}
FREEZING_CODES = {56, 57, 66, 67}
THUNDER_CODES = {95, 96, 99}
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg):
    if deg is None or np.isnan(deg):
        return "—"
    return COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def _arr(models, key):
    rows = []
    for m in models.values():
        v = m.get(key)
        rows.append([np.nan if x is None else float(x) for x in v] if v else None)
    n = max((len(r) for r in rows if r), default=0)
    rows = [r + [np.nan] * (n - len(r)) if r else [np.nan] * n for r in rows]
    return np.array(rows) if rows else np.empty((0, 0))


def _nanstat(a, how="median"):
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return {"median": np.nanmedian, "min": np.nanmin, "max": np.nanmax, "mean": np.nanmean,
                    "sum": np.nansum}[how](a, axis=0)


def circular_mean(dirs, weights=None):
    d = np.radians(dirs)
    w = np.ones_like(d) if weights is None else weights
    s = np.nansum(np.sin(d) * w, axis=0)
    c = np.nansum(np.cos(d) * w, axis=0)
    out = (np.degrees(np.arctan2(s, c)) + 360) % 360
    out[np.all(np.isnan(dirs), axis=0)] = np.nan
    return out


def in_sector(deg, sector):
    a, b = sector["from"] % 360, sector["to"] % 360
    deg = deg % 360
    return (a <= deg <= b) if a <= b else (deg >= a or deg <= b)


def sector_distance(deg, sector):
    """Degrees outside the sector (0 if inside)."""
    if in_sector(deg, sector):
        return 0.0
    a, b = sector["from"], sector["to"]
    d1 = min(abs(deg - a) % 360, 360 - abs(deg - a) % 360)
    d2 = min(abs(deg - b) % 360, 360 - abs(deg - b) % 360)
    return min(d1, d2)


def wind_text(d, w, g):
    """'SW 22 G31 kt', omitting the gust when no model supplies one."""
    gs = "" if g is None or (isinstance(g, float) and math.isnan(g)) else f" G{g:.0f}"
    return f"{d} {w:.0f}{gs} kt"


def wind_chill(t_c, v_kt):
    v = v_kt * 1.852
    if t_c > 10 or v < 5:
        return t_c
    return 13.12 + 0.6215 * t_c - 11.37 * v ** 0.16 + 0.3965 * t_c * v ** 0.16


def humidex(t_c, td_c):
    if t_c < 20:
        return t_c
    e = 6.11 * math.exp(5417.7530 * (1 / 273.16 - 1 / (273.15 + td_c)))
    return t_c + 0.5555 * (e - 10.0)


def spray_ppr(v_kt, t_air, t_water, t_freeze=0.0):
    """Overland (1990) icing predictor for fresh water (Tf = 0 °C)."""
    if t_air >= t_freeze:
        return 0.0
    va = v_kt * 0.5144
    return va * (t_freeze - t_air) / (1 + 0.3 * (max(t_water, t_freeze) - t_freeze))


def spray_class(ppr):
    if ppr <= 0:
        return "none"
    if ppr <= 22.4:
        return "light"
    if ppr <= 53.3:
        return "moderate"
    if ppr <= 83.0:
        return "heavy"
    return "extreme"


# --------------------------------------------------------------------------- core

def build_consensus(models, display=None):
    if not models:
        return None
    names = [(display or {}).get(m, m) for m in models]
    times = next(iter(models.values()))["time"]
    spd = _arr(models, "wind_speed_10m")
    gst = _arr(models, "wind_gusts_10m")
    wdir = _arr(models, "wind_direction_10m")
    weights = np.where(np.isnan(spd), 0, np.maximum(spd, 0.1))
    c = {
        "time": times,
        "models": names,
        "n_models": np.sum(~np.isnan(spd), axis=0).astype(int),
        "wind": _nanstat(spd), "wind_min": _nanstat(spd, "min"), "wind_max": _nanstat(spd, "max"),
        "gust": _nanstat(gst), "gust_max": _nanstat(gst, "max"),
        "dir": circular_mean(wdir, weights),
        "temp": _nanstat(_arr(models, "temperature_2m")),
        "dew": _nanstat(_arr(models, "dew_point_2m")),
        "pres": _nanstat(_arr(models, "pressure_msl")),
        "precip": _nanstat(_arr(models, "precipitation")),
        "precip_max": _nanstat(_arr(models, "precipitation"), "max"),
        "snow": _nanstat(_arr(models, "snowfall")),
        "vis_m": _nanstat(_arr(models, "visibility")),
        "cape": _nanstat(_arr(models, "cape")),
        "cloud": _nanstat(_arr(models, "cloud_cover")),
        "_spd": spd, "_gst": gst, "_dir": wdir, "_code": _arr(models, "weather_code"),
    }
    n = len(times)
    c["wind_chill"] = np.array([wind_chill(c["temp"][i], c["wind"][i]) if not np.isnan(c["temp"][i]) else np.nan
                                for i in range(n)])
    c["humidex"] = np.array([humidex(c["temp"][i], c["dew"][i])
                             if not (np.isnan(c["temp"][i]) or np.isnan(c["dew"][i])) else np.nan
                             for i in range(n)])
    codes = c["_code"]
    c["freezing_votes"] = np.sum(np.isin(codes, list(FREEZING_CODES)), axis=0)
    c["thunder_votes"] = np.sum(np.isin(codes, list(THUNDER_CODES)), axis=0)
    return c


def assess_service(cfg, service, cons, water_temp_c):
    """Hourly status per category, overall hourly status, and events."""
    sr, hz = cfg["status_rules"], cfg["hazards"]
    n = len(cons["time"])
    cats = {}

    # ---- wind rules
    # A rule is measured on sustained wind unless it says `measure: gust`, in which
    # case the gust is compared with the threshold (sustained wind stands in for an
    # hour where no model supplies a gust).
    for r_i, rule in enumerate(service["rules"]):
        lvl = np.zeros(n, dtype=int)
        why = [""] * n
        T = rule["threshold_kt"]
        on_gust = rule.get("measure", "sustained") == "gust"
        per_model = cons["_gst"] if on_gust else cons["_spd"]
        for i in range(n):
            w, g, d = cons["wind"][i], cons["gust"][i], cons["dir"][i]
            if np.isnan(w) or np.isnan(d):
                lvl[i] = -1
                continue
            ins = in_sector(d, rule["sector"])
            if on_gust:
                v = w if np.isnan(g) else g
                what, ref = ("sustained" if np.isnan(g) else "gusts"), f"{T} kt gust reference"
            else:
                v, what, ref = w, "sustained", f"{T} kt reference"
            if ins and v >= T:
                lvl[i], why[i] = 2, f"{what} {v:.0f} kt {compass(d)}, at or above the {ref}"
                continue
            if ins and v >= T - sr["yellow_margin_kt"]:
                lvl[i], why[i] = 1, f"{what} {v:.0f} kt {compass(d)}, within {sr['yellow_margin_kt']} kt of the {ref}"
                continue
            if ins and not on_gust and sr["gusts_to_yellow"] and not np.isnan(g) and g >= T:
                lvl[i], why[i] = 1, f"gusts {g:.0f} kt {compass(d)}, at or above the {T} kt reference"
                continue
            if sr["near_sector_deg"] and v >= T and sector_distance(d, rule["sector"]) <= sr["near_sector_deg"]:
                lvl[i], why[i] = 1, f"{what} {v:.0f} kt {compass(d)}, just outside the {rule['sector']['from']}–{rule['sector']['to']}° sector"
                continue
            if sr["model_outlier_to_yellow"]:
                for m_i in range(per_model.shape[0]):
                    ms, md = per_model[m_i, i], cons["_dir"][m_i, i]
                    if not np.isnan(ms) and not np.isnan(md) and ms >= T and in_sector(md, rule["sector"]):
                        lvl[i] = 1
                        why[i] = (f"{cons['models'][m_i]} shows {'gusts ' if on_gust else ''}{ms:.0f} kt {compass(md)}; "
                                  f"consensus {v:.0f} kt")
                        break
        cats[f"wind:{r_i}"] = {"label": rule["label"], "kind": "wind", "rule": rule, "level": lvl, "why": why}

    # ---- visibility
    vis_nm = cons["vis_m"] / 1852.0
    lvl = np.where(np.isnan(vis_nm), 0, np.where(vis_nm < hz["visibility_nm"]["red_below"], 2,
                   np.where(vis_nm < hz["visibility_nm"]["yellow_below"], 1, 0)))
    cats["vis"] = {"label": "Visibility", "kind": "vis", "level": lvl,
                   "why": [f"visibility {v:.1f} NM" if l else "" for v, l in zip(vis_nm, lvl)]}

    # ---- freezing spray
    ppr = np.array([spray_ppr(cons["wind"][i], cons["temp"][i], water_temp_c)
                    if not (np.isnan(cons["wind"][i]) or np.isnan(cons["temp"][i])) else 0.0 for i in range(n)])
    sc = [spray_class(p) for p in ppr]
    y_rank, r_rank = SPRAY_RANK[hz["freezing_spray"]["yellow_class"]], SPRAY_RANK[hz["freezing_spray"]["red_class"]]
    lvl = np.array([2 if SPRAY_RANK[c] >= r_rank else 1 if SPRAY_RANK[c] >= y_rank else 0 for c in sc])
    cats["spray"] = {"label": "Freezing spray", "kind": "spray", "level": lvl, "ppr": ppr, "class": sc,
                     "why": [f"{c} freezing spray potential" if l else "" for c, l in zip(sc, lvl)]}

    # ---- temperature extremes
    wc, hx = cons["wind_chill"], cons["humidex"]
    lvl = np.zeros(n, dtype=int)
    why = [""] * n
    for i in range(n):
        if not np.isnan(wc[i]) and wc[i] <= hz["wind_chill_c"]["red_at_or_below"]:
            lvl[i], why[i] = 2, f"wind chill {wc[i]:.0f}"
        elif not np.isnan(wc[i]) and wc[i] <= hz["wind_chill_c"]["yellow_at_or_below"]:
            lvl[i], why[i] = 1, f"wind chill {wc[i]:.0f}"
        elif not np.isnan(hx[i]) and hx[i] >= hz["humidex"]["red_at_or_above"]:
            lvl[i], why[i] = 2, f"humidex {hx[i]:.0f}"
        elif not np.isnan(hx[i]) and hx[i] >= hz["humidex"]["yellow_at_or_above"]:
            lvl[i], why[i] = 1, f"humidex {hx[i]:.0f}"
    cats["temp"] = {"label": "Temperature extremes", "kind": "temp", "level": lvl, "why": why}

    # ---- snowfall (rolling)
    snow = np.nan_to_num(cons["snow"])
    s12 = np.convolve(snow, np.ones(12), "full")[:n]
    s24 = np.convolve(snow, np.ones(24), "full")[:n]
    lvl = np.where(s24 >= hz["snowfall_cm"]["red_24h"], 2, np.where(s12 >= hz["snowfall_cm"]["yellow_12h"], 1, 0))
    cats["snow"] = {"label": "Snowfall", "kind": "snow", "level": lvl,
                    "why": [f"{a:.0f} cm in 24 h" if l == 2 else f"{b:.0f} cm in 12 h" if l else ""
                            for a, b, l in zip(s24, s12, lvl)]}

    # ---- freezing precipitation (need 2 models to agree, or the only model available)
    need = np.minimum(2, np.maximum(cons["n_models"], 1))
    fz = cons["freezing_votes"] >= need
    run = np.zeros(n, dtype=int)
    for i in range(n):
        run[i] = (run[i - 1] + 1) if fz[i] and i else int(fz[i])
    lvl = np.where(fz, 1, 0)
    for i in range(n):
        if run[i] >= hz["freezing_precip"]["red_hours"]:
            lvl[i - run[i] + 1:i + 1] = 2
    cats["fzra"] = {"label": "Freezing rain / drizzle", "kind": "fzra", "level": lvl,
                    "why": ["freezing precipitation in model output" if l else "" for l in lvl]}

    # ---- thunderstorms
    th = (cons["thunder_votes"] >= need) | ((np.nan_to_num(cons["cape"]) >= hz["thunderstorm"]["cape_jkg_yellow"])
                                           & (np.nan_to_num(cons["precip"]) >= 0.2))
    cats["thunder"] = {"label": "Thunderstorms", "kind": "thunder", "level": th.astype(int),
                       "why": ["thunderstorm potential" if t else "" for t in th]}

    wind_levels = [c["level"] for k, c in cats.items() if k.startswith("wind:")]
    hazard_levels = [c["level"] for k, c in cats.items() if not k.startswith("wind:")]
    overall = np.max(np.vstack(wind_levels + hazard_levels), axis=0)
    if not service["rules"]:
        # No vessel thresholds: wind not assessed, hazards still reported.
        overall = np.max(np.vstack(hazard_levels), axis=0)
    return {"cats": cats, "overall": overall, "ppr": ppr, "spray_class": sc}


def extract_events(cons, assessment, tz, gap_h=2):
    """Contiguous yellow/red periods per category, with peak and a plain description."""
    times = [dt.datetime.fromisoformat(t) for t in cons["time"]]
    events = []
    for key, cat in assessment["cats"].items():
        lvl = cat["level"]
        i = 0
        while i < len(lvl):
            if lvl[i] >= 1:
                j = i
                while True:  # bridge gaps of up to `gap_h` hours so one episode reads as one event
                    nxt = next((k for k in range(j + 1, min(j + 2 + gap_h, len(lvl))) if lvl[k] >= 1), None)
                    if nxt is None:
                        break
                    j = nxt
                seg = slice(i, j + 1)
                peak_level = int(lvl[seg].max())
                if cat["kind"] == "wind":
                    on_gust = cat["rule"].get("measure", "sustained") == "gust"
                    series = np.where(np.isnan(cons["gust"]), cons["wind"], cons["gust"]) if on_gust else cons["wind"]
                    k = i + int(np.nanargmax(np.nan_to_num(series[seg], nan=-1)))
                    peak = {"wind": float(cons["wind"][k]),
                            "gust": None if np.isnan(cons["gust"][k]) else float(cons["gust"][k]),
                            "dir": compass(cons["dir"][k]), "dir_deg": float(cons["dir"][k]),
                            "spread": float(cons["wind_max"][k] - cons["wind_min"][k])}
                elif cat["kind"] == "spray":
                    k = i + int(np.argmax(assessment["ppr"][seg]))
                    peak = {"class": assessment["spray_class"][k]}
                else:
                    k = i + int(np.argmax(lvl[seg]))
                    peak = {}
                # First hour that reaches the peak level — the "when it starts to matter" time
                first_peak = i + int(np.argmax(lvl[seg] == peak_level))
                events.append({
                    "category": key, "label": cat["label"], "kind": cat["kind"],
                    "level": peak_level, "level_name": LEVEL_NAME[peak_level],
                    "start": times[i].isoformat(), "end": times[j].isoformat(),
                    "first_peak": times[first_peak].isoformat(),
                    "peak_time": times[k].isoformat(), "peak": peak, "why": cat["why"][first_peak],
                    "hours": j - i + 1,
                })
                i = j + 1
            else:
                i += 1
    events.sort(key=lambda e: (e["start"], -e["level"]))
    return events


def daily_status(cons, assessment):
    """Worst level per local calendar day, per category and overall."""
    days = {}
    for i, t in enumerate(cons["time"]):
        d = t[:10]
        days.setdefault(d, {"overall": -1, "cats": {}})
        days[d]["overall"] = max(days[d]["overall"], int(assessment["overall"][i]))
        for k, c in assessment["cats"].items():
            days[d]["cats"][k] = max(days[d]["cats"].get(k, -1), int(c["level"][i]))
    return days


def peak_wind_by_day(cons):
    out = {}
    for i, t in enumerate(cons["time"]):
        d = t[:10]
        w = cons["wind"][i]
        if np.isnan(w):
            continue
        if d not in out or w > out[d]["wind"]:
            g = cons["gust"][i]
            out[d] = {"wind": float(w), "gust": None if np.isnan(g) else float(g), "dir": compass(cons["dir"][i]),
                      "time": t[11:16]}
    return out


def temp_range_by_day(cons):
    out = {}
    for i, t in enumerate(cons["time"]):
        d = t[:10]
        v = cons["temp"][i]
        if np.isnan(v):
            continue
        lo, hi = out.get(d, (v, v))
        out[d] = (min(lo, v), max(hi, v))
    return {d: {"min": round(a), "max": round(b)} for d, (a, b) in out.items()}
