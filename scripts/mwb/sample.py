"""Synthetic model data for testing the pipeline offline. Never used in a real brief:
any output built from it is stamped SAMPLE DATA in the header and footer."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np


SAMPLE_MODELS = ["HRDPS 2.5 km", "RDPS 10 km", "GDPS 15 km", "GFS 0.25°", "ECMWF (MET Norway)"]


def sample_models(cfg, service, start_local):
    rng = np.random.default_rng(abs(hash(service["id"])) % 2**32)
    n = 24 * cfg["forecast_days"]
    times = [(start_local + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(n)]
    h = np.arange(n)
    # A low passes north on day 2: S winds back to SW then W and NW, peaking ~30 h out,
    # then a cold NW flow on days 4–5 with lake-effect snow.
    base_dir = np.interp(h, [0, 18, 30, 42, 70, 119], [160, 200, 240, 275, 300, 310])
    base_spd = 10 + 20 * np.exp(-((h - 32) / 9) ** 2) + 12 * np.exp(-((h - 80) / 14) ** 2)
    base_t = np.interp(h, [0, 30, 50, 80, 119], [9, 11, 3, -7, -4]) + 2.5 * np.sin((h % 24 - 9) / 24 * 2 * math.pi)
    bias = {"wolfe": 1.0, "amherst": 1.05, "glenora": 0.75, "howe": 0.85, "simcoe": 0.95}[service["id"]]
    models = {}
    for k, mid in enumerate(SAMPLE_MODELS):
        horizon = 48 if "HRDPS" in mid else 84 if "RDPS" in mid else n
        jitter = rng.normal(0, 1.6, n)
        spd = np.clip(bias * base_spd * (0.92 + 0.04 * k) + jitter, 0, None)
        wdir = (base_dir + rng.normal(0, 8, n) + (k - 2) * 4) % 360
        temp = base_t + rng.normal(0, 0.6, n) + (k - 2) * 0.3
        precip = np.clip(1.6 * np.exp(-((h - 28) / 5) ** 2) + 0.6 * np.exp(-((h - 84) / 10) ** 2)
                         + rng.normal(0, 0.05, n), 0, None)
        snow = np.where(temp < 0.5, precip * 0.9, 0)
        code = np.where(precip > 0.2, np.where(temp < 0.5, 73, 63), 3)
        code[(h > 40) & (h < 44) & (temp < 0.5) & (temp > -2)] = 66
        vis = np.where(precip > 0.5, 3500, 20000) + rng.normal(0, 500, n)
        cut = lambda a: [None if i >= horizon else round(float(x), 1) for i, x in enumerate(a)]  # noqa: E731
        models[mid] = {
            "time": times,
            "wind_speed_10m": cut(spd), "wind_gusts_10m": cut(spd * 1.35 + 2),
            "wind_direction_10m": cut(wdir), "temperature_2m": cut(temp),
            "apparent_temperature": cut(temp - 4), "dew_point_2m": cut(temp - 3),
            "relative_humidity_2m": cut(np.full(n, 80)), "precipitation": cut(precip),
            "rain": cut(np.where(temp >= 0.5, precip, 0)), "showers": cut(np.zeros(n)), "snowfall": cut(snow),
            "weather_code": cut(code), "pressure_msl": cut(1012 - 14 * np.exp(-((h - 30) / 12) ** 2) + 0.08 * h),
            "cloud_cover": cut(np.full(n, 70)), "visibility": cut(vis), "cape": cut(np.zeros(n)),
        }
    return models


def sample_runs(cfg, now_utc):
    out = {}
    init = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    for mid in SAMPLE_MODELS:
        out[mid] = {"name": mid, "init": init.isoformat(),
                    "available": (init + dt.timedelta(hours=4)).isoformat()}
    return out
