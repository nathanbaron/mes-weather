"""Meteograms and radar panels (matplotlib, PNG)."""

from __future__ import annotations

import datetime as dt
import io
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from .analyze import compass, wind_text  # noqa: E402

GREEN, YELLOW, RED, GREY = "#2e7d32", "#e0a800", "#c62828", "#9e9e9e"
LEVEL_COLOUR = {-1: "#d0d0d0", 0: GREEN, 1: YELLOW, 2: RED}
INK, MUTED, GRID = "#1b1f24", "#5f6b76", "#e3e7eb"
BLUE, PURPLE, ICE = "#1f6fb2", "#7b4fb3", "#3aa7c9"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 8.5, "axes.edgecolor": "#b8c0c8",
    "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.spines.top": False, "axes.spines.right": False,
})

ORIENTATION_POINTS = {
    "Kingston": (44.231, -76.486), "Belleville": (44.163, -77.383), "Picton": (44.006, -77.140),
    "Gananoque": (44.330, -76.162), "Watertown NY": (43.975, -75.911), "Oswego NY": (43.455, -76.511),
    "Rochester NY": (43.157, -77.616), "Toronto": (43.653, -79.383), "Ottawa": (45.421, -75.697),
    "Syracuse NY": (43.048, -76.147), "Buffalo NY": (42.886, -78.878), "Montréal": (45.502, -73.567),
}


def _png(fig, dpi=150):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _sun_times(date, lat, lon, tz):
    """Approximate local sunrise/sunset (NOAA simplified), good to a few minutes."""
    n = date.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lr = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(lr) * math.cos(decl)) - math.tan(lr) * math.tan(decl))
    ha = math.degrees(math.acos(max(-1, min(1, cos_ha))))
    noon_utc = 720 - 4 * lon - eqt
    base = dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc)
    rise = (base + dt.timedelta(minutes=noon_utc - 4 * ha)).astimezone(tz)
    sset = (base + dt.timedelta(minutes=noon_utc + 4 * ha)).astimezone(tz)
    return rise, sset


def _decorate_time(ax, times, lat, lon, tz, label_days=False):
    t0, t1 = times[0], times[-1]
    d = t0.date()
    while d <= t1.date():
        rise, sset = _sun_times(d, lat, lon, tz)
        day_start = dt.datetime(d.year, d.month, d.day, tzinfo=tz)
        ax.axvspan(max(day_start, t0), min(rise, t1), color="#eef1f5", zorder=0, lw=0)
        ax.axvspan(max(sset, t0), min(day_start + dt.timedelta(days=1), t1), color="#eef1f5", zorder=0, lw=0)
        if day_start > t0:
            ax.axvline(day_start, color="#9aa5b1", lw=0.8, zorder=1)
        d += dt.timedelta(days=1)
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.set_xlim(t0, t1)


def meteogram(service, cons, assessment, events, tz, max_notes=5):
    times = [dt.datetime.fromisoformat(t).replace(tzinfo=tz) for t in cons["time"]]
    lat, lon = service["lat"], service["lon"]
    has_rules = bool(service["rules"])

    fig, axes = plt.subplots(5, 1, figsize=(11, 8.0), sharex=True,
                             gridspec_kw={"height_ratios": [0.30, 0.34, 2.5, 1.35, 1.15], "hspace": 0.10})
    a_status, a_dir, a_wind, a_temp, a_pcp = axes

    # ---- status strip
    lvl = assessment["overall"]
    width = 1 / 24
    for i, t in enumerate(times):
        a_status.add_patch(plt.Rectangle((mdates.date2num(t), 0), width, 1, color=LEVEL_COLOUR[int(lvl[i])], lw=0))
    a_status.set_ylim(0, 1)
    a_status.set_yticks([])
    a_status.spines["left"].set_visible(False)
    a_status.set_title(f"{service['name']} — {', '.join(service['vessels'])}", loc="left",
                       fontsize=11, color=INK, fontweight="bold", pad=18)
    a_status.text(0, 1.25, "Forecast vs MES reference thresholds (for awareness)" if has_rules else
                  "No MES wind reference thresholds defined — hazards only", transform=a_status.transAxes,
                  fontsize=8, color=MUTED)
    for ax in axes:
        _decorate_time(ax, times, lat, lon, tz)

    # ---- wind
    w, g = cons["wind"], cons["gust"]
    a_wind.fill_between(times, cons["wind_min"], cons["wind_max"], color="#c9d6e3", alpha=0.7, lw=0,
                        label="Model range (sustained)", zorder=2)
    a_wind.plot(times, w, color=INK, lw=1.8, label="Sustained (consensus)", zorder=4)
    a_wind.plot(times, g, color=INK, lw=1.0, ls=(0, (3, 2)), label="Gusts (consensus)", zorder=3)
    wind_lvl = np.max(np.vstack([c["level"] for k, c in assessment["cats"].items() if k.startswith("wind:")]),
                      axis=0) if has_rules else np.zeros(len(times), dtype=int)
    for L in (1, 2):
        m = wind_lvl == L
        a_wind.scatter(np.array(times)[m], w[m], s=14, color=LEVEL_COLOUR[L], zorder=5, edgecolor="white", lw=0.4)
    ymax = max(np.nanmax(cons["gust_max"]) if np.isfinite(np.nanmax(cons["gust_max"])) else 10,
               max([r["threshold_kt"] for r in service["rules"]], default=0)) + 8
    by_t = {}
    for r in service["rules"]:
        sec = f"{r['sector']['from']:03d}–{r['sector']['to']:03d}°" + (f" {r['vessel']}" if r.get("vessel") else "")
        by_t.setdefault(r["threshold_kt"], []).append(sec)
    for T, secs in by_t.items():
        a_wind.axhline(T, color=RED, lw=0.9, alpha=0.75, zorder=1)
        a_wind.text(times[-1], T + 0.6, f"{T} kt from " + " / ".join(secs), ha="right", va="bottom",
                    fontsize=7, color=RED, bbox=dict(fc="white", ec="none", pad=0.5, alpha=0.8))
    a_wind.set_ylim(0, ymax)
    a_wind.set_ylabel("Wind (kt)")
    a_wind.legend(loc="upper left", fontsize=7, frameon=False, ncol=3)

    # Direction row: arrows every 3 h (pointing where the wind blows to), compass label every 6 h
    a_dir.set_ylim(0, 1)
    a_dir.set_yticks([])
    a_dir.spines["left"].set_visible(False)
    a_dir.grid(False)
    a_dir.set_ylabel("Dir", rotation=0, ha="right", va="center")
    for i in range(0, len(times), 3):
        d = cons["dir"][i]
        if np.isnan(d):
            continue
        rad = math.radians(d)
        u, v = -math.sin(rad), -math.cos(rad)
        x = mdates.date2num(times[i])
        a_dir.annotate("", xy=(x + u * 0.05, 0.66 + v * 0.26), xytext=(x - u * 0.05, 0.66 - v * 0.26),
                       arrowprops=dict(arrowstyle="-|>", color=INK, lw=0.8, mutation_scale=7))
        if i % 6 == 0:
            a_dir.text(times[i], 0.05, compass(d), ha="center", fontsize=6.3, color=MUTED)

    # ---- annotations: the moments the overall status steps up
    notes = transitions(cons, assessment, max_notes)
    for k, (i, level, text) in enumerate(notes):
        t = times[i]
        colour = LEVEL_COLOUR[level]
        for ax in (a_wind, a_temp, a_pcp):
            ax.axvline(t, color=colour, lw=1.1, ls=":", zorder=6)
        a_wind.annotate(f"{t:%a %H:%M}\n{text}", xy=(t, 0.90 - 0.16 * (k % 3)), xycoords=("data", "axes fraction"),
                        xytext=(3, 0), textcoords="offset points", fontsize=7, color=INK, va="top",
                        bbox=dict(boxstyle="round,pad=0.25", fc="white", ec=colour, lw=1.1), zorder=7)

    # ---- temperature
    a_temp.plot(times, cons["temp"], color="#d35400", lw=1.5, label="Air temperature")
    feel = np.where(cons["temp"] <= 10, cons["wind_chill"], np.where(cons["temp"] >= 20, cons["humidex"], np.nan))
    if np.any(np.isfinite(feel)):
        a_temp.plot(times, feel, color="#d35400", lw=1.0, ls=(0, (2, 2)), label="Wind chill / humidex")
    a_temp.axhline(0, color=BLUE, lw=0.8, alpha=0.6)
    spray = np.array([c != "none" for c in assessment["spray_class"]])
    if spray.any():
        a_temp.scatter(np.array(times)[spray], np.array(cons["temp"])[spray], marker="D", s=16, color=ICE,
                       zorder=5, label="Freezing spray potential")
    a_temp.set_ylabel("°C")
    a_temp.legend(loc="best", fontsize=7, frameon=False, ncol=3)

    # ---- precipitation + pressure
    p = np.nan_to_num(cons["precip"])
    snow = np.nan_to_num(cons["snow"]) > 0
    fz = assessment["cats"]["fzra"]["level"] > 0
    colours = [PURPLE if fz[i] else ICE if snow[i] else BLUE for i in range(len(p))]
    a_pcp.bar(times, p, width=width * 0.9, color=colours, align="edge")
    a_pcp.set_ylabel("Precip (mm/h)")
    a_pcp.set_ylim(0, max(2, float(np.nanmax(cons["precip_max"])) * 1.15 if np.isfinite(np.nanmax(cons["precip_max"])) else 2))
    a2 = a_pcp.twinx()
    a2.plot(times, cons["pres"], color=MUTED, lw=1.0)
    a2.set_ylabel("MSLP (mb)", color=MUTED)
    a2.spines["right"].set_visible(True)
    a2.tick_params(colors=MUTED)
    a_pcp.legend(handles=[Patch(color=BLUE, label="Rain"), Patch(color=ICE, label="Snow"),
                          Patch(color=PURPLE, label="Freezing"), plt.Line2D([], [], color=MUTED, label="Pressure")],
                 loc="upper left", fontsize=7, frameon=False, ncol=4)

    a_pcp.xaxis.set_major_locator(mdates.DayLocator(tz=tz))
    a_pcp.xaxis.set_major_formatter(mdates.DateFormatter("%a %d %b", tz=tz))
    a_pcp.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18], tz=tz))
    a_pcp.xaxis.set_minor_formatter(mdates.DateFormatter("%H", tz=tz))
    a_pcp.tick_params(axis="x", which="major", pad=12, labelsize=8.5, colors=INK)
    a_pcp.tick_params(axis="x", which="minor", labelsize=6.5)
    fig.text(0.125, 0.015, f"Consensus = median of {len(cons['models'])} models. Shaded = night. "
             "Dotted lines mark when forecast data first reach a reference threshold or hazard.", fontsize=7, color=MUTED)
    return _png(fig)


def transitions(cons, assessment, max_notes=6, min_gap_h=4):
    """Hours where the overall status steps up, with the reason that drove it."""
    lvl = assessment["overall"]
    cands = []
    for i in range(len(lvl)):
        prev = lvl[i - 1] if i else 0
        if lvl[i] >= 1 and lvl[i] > prev:
            # Reason: a wind rule at this level first, otherwise any hazard at this level
            best = None
            for k, c in assessment["cats"].items():
                if c["level"][i] == lvl[i] and (best is None or (k.startswith("wind:") and not best[0].startswith("wind:"))):
                    best = (k, c)
            k, c = best
            if k.startswith("wind:"):
                reason = (wind_text(compass(cons['dir'][i]), cons['wind'][i], cons['gust'][i])
                          + f" vs {c['rule']['threshold_kt']}" + (f" ({_abbr(c['rule']['vessel'])})" if c["rule"].get("vessel") else ""))
            else:
                reason = c["label"]
            cands.append((i, int(lvl[i]), f"{LEVEL_WORD[int(lvl[i])]} · {reason}"))
    keep = []
    for c in sorted(cands, key=lambda c: (-c[1], c[0])):
        if len(keep) >= max_notes:
            break
        if all(abs(c[0] - k[0]) >= min_gap_h for k in keep):
            keep.append(c)
    return sorted(keep)


LEVEL_WORD = {1: "Yellow", 2: "Red"}


def _abbr(vessel):
    return {"WOLFE ISLANDER III": "WI3", "WOLFE ISLANDER IV": "WI4", "AMHERST ISLANDER II": "AI2",
            "FRONTENAC HOWE ISLANDER": "FHI", "QUINTE LOYALIST": "QL", "FRONTENAC II": "F2"}.get(vessel, vessel)


def _short(e):
    p = e.get("peak", {})
    if e["kind"] == "wind":
        return wind_text(p["dir"], p["wind"], p["gust"])
    if e["kind"] == "spray":
        return f"Spray: {p.get('class', '')}"
    return e["label"]


def status_strip_png(services_daily, days, labels):
    """Small coloured grid used in the summary when HTML colour cells aren't available (e.g. email)."""
    fig, ax = plt.subplots(figsize=(1.2 + 1.1 * len(days), 0.35 * len(labels) + 0.6))
    for r, sid in enumerate(services_daily):
        for c, d in enumerate(days):
            lv = services_daily[sid].get(d, {}).get("overall", -1)
            ax.add_patch(plt.Rectangle((c, -r - 1), 0.96, 0.92, color=LEVEL_COLOUR[lv]))
    ax.set_xlim(0, len(days))
    ax.set_ylim(-len(labels), 0)
    ax.set_yticks([-r - 0.55 for r in range(len(labels))], labels)
    ax.set_xticks([c + 0.48 for c in range(len(days))], days)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    return _png(fig)


# --------------------------------------------------------------------------- radar

def radar_panel(frames, extent, services, tz, title, local=False):
    """Radar frames side by side (oldest -> newest) over coastlines when cartopy is available."""
    if not frames:
        return None
    from PIL import Image
    lon0, lat0, lon1, lat1 = extent
    n = len(frames)
    aspect = (lat1 - lat0) / ((lon1 - lon0) * math.cos(math.radians((lat0 + lat1) / 2)))
    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature
        proj = ccrs.PlateCarree()
    except Exception:  # noqa: BLE001
        ccrs = None
    panel_w = 4.4
    fig = plt.figure(figsize=(panel_w * n, panel_w * aspect + 0.7))
    for k, f in enumerate(frames):
        if ccrs:
            ax = fig.add_subplot(1, n, k + 1, projection=proj)
            ax.set_extent([lon0, lon1, lat0, lat1], crs=proj)
            ax.set_aspect(1 / math.cos(math.radians((lat0 + lat1) / 2)))
            try:
                ax.add_feature(cfeature.LAND.with_scale("10m"), facecolor="#f3f1ec", zorder=0)
                ax.add_feature(cfeature.OCEAN.with_scale("10m"), facecolor="#e3edf5", zorder=0)
                ax.add_feature(cfeature.LAKES.with_scale("10m"), facecolor="#e3edf5", edgecolor="#8aa3b8", lw=0.5, zorder=1)
                ax.add_feature(cfeature.BORDERS.with_scale("10m"), edgecolor="#8a8a8a", lw=0.6, zorder=1)
                ax.add_feature(cfeature.STATES.with_scale("10m"), edgecolor="#bdbdbd", lw=0.4, zorder=1)
            except Exception:  # noqa: BLE001
                pass
            kw = {"transform": proj}
        else:
            ax = fig.add_subplot(1, n, k + 1)
            ax.set_facecolor("#f4f6f8")
            ax.set_xlim(lon0, lon1)
            ax.set_ylim(lat0, lat1)
            ax.set_aspect(1 / math.cos(math.radians((lat0 + lat1) / 2)))
            ax.set_xticks([])
            ax.set_yticks([])
            kw = {}
        if f.get("us_png"):
            ax.imshow(Image.open(io.BytesIO(f["us_png"])), extent=[lon0, lon1, lat0, lat1], origin="upper",
                      alpha=0.6, zorder=3, **kw)
        ax.imshow(Image.open(io.BytesIO(f["png"])), extent=[lon0, lon1, lat0, lat1], origin="upper", zorder=4, **kw)
        if local:
            for s in services:
                ax.plot(s["lon"], s["lat"], "s", ms=4.5, color=INK, mec="white", mew=0.6, zorder=6, **kw)
                ax.text(s["lon"] + 0.02, s["lat"] + 0.025, s["short"], fontsize=6.5, color=INK, zorder=6, **kw)
        else:
            for name, (la, lo) in ORIENTATION_POINTS.items():
                if lon0 < lo < lon1 and lat0 < la < lat1 and name != "Kingston":
                    ax.plot(lo, la, "o", ms=2, color=MUTED, zorder=5, **kw)
                    ax.text(lo + 0.1, la + 0.08, name, fontsize=6, color=MUTED, zorder=5, **kw)
            la_c = sum(s["lat"] for s in services) / len(services)
            lo_c = sum(s["lon"] for s in services) / len(services)
            ax.plot(lo_c, la_c, "*", ms=9, color=RED, mec="white", mew=0.6, zorder=6, **kw)
            ax.text(lo_c + 0.15, la_c - 0.35, "Ferry services", fontsize=6.5, color=RED, zorder=6, **kw)
        t = dt.datetime.fromisoformat(f["time"]).astimezone(tz)
        ax.set_title(f"{t:%a %H:%M}", fontsize=8.5, color=INK)
    fig.suptitle(title, x=0.01, ha="left", fontsize=10, color=INK, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    return _png(fig, dpi=130)
