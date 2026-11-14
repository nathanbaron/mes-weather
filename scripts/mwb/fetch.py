"""Data acquisition. Every call fails soft and records itself in the source log,
so the brief can state plainly what it did and did not get."""

from __future__ import annotations

import datetime as dt
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

UA = {"User-Agent": "MTO-MES-marine-weather-briefing/1.0"}
TIMEOUT = 30


@dataclass
class SourceLog:
    entries: list = field(default_factory=list)

    def ok(self, name, url, detail=""):
        self.entries.append({"source": name, "url": url, "ok": True, "detail": detail})

    def fail(self, name, url, err):
        self.entries.append({"source": name, "url": url, "ok": False, "detail": str(err)[:200]})


def _get(url, log, name, params=None, binary=False):
    try:
        r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        log.ok(name, r.url)
        return r.content if binary else r
    except Exception as e:  # noqa: BLE001 — every failure is reported, never raised
        log.fail(name, url, e)
        return None


# --------------------------------------------------------------------------- lake temperature

def fetch_water_temps(cfg, log, today):
    """Lake surface temperature per service from the GDPS analysis (ECCC GeoMet WCS).

    Land cells come back as 0 K; each service takes its nearest water cell.
    Falls back to the monthly estimate in the config if the layer is unavailable.
    """
    import numpy as np
    from PIL import Image
    svcs = cfg["services"]
    lo, hi = min(s["lon"] for s in svcs) - 0.5, max(s["lon"] for s in svcs) + 0.5
    la0, la1 = min(s["lat"] for s in svcs) - 0.4, max(s["lat"] for s in svcs) + 0.4
    params = [("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
              ("COVERAGEID", cfg["sources"]["water_temp_layer"]), ("SUBSETTINGCRS", "EPSG:4326"),
              ("SUBSET", f"x({lo:.2f},{hi:.2f})"), ("SUBSET", f"y({la0:.2f},{la1:.2f})"), ("FORMAT", "image/tiff")]
    out = {}
    try:
        r = requests.get(cfg["sources"]["geomet_wms"], params=params, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content))
        a = np.array(im, dtype=float)
        sx, sy = im.tag_v2[33550][:2]
        x0, y0 = im.tag_v2[33922][3:5]
        water = np.argwhere(a > 250)
        if not len(water):
            raise ValueError("no water cells in extent")
        log.ok("EC GDPS lake temperature", r.url)
        for s in svcs:
            ci, cj = (y0 - s["lat"]) / sy, (s["lon"] - x0) / sx
            i, j = min(water, key=lambda w: (w[0] + 0.5 - ci) ** 2 + (w[1] + 0.5 - cj) ** 2)
            out[s["id"]] = {"value_c": round(a[i, j] - 273.15, 1), "basis": "ECCC GDPS lake surface analysis"}
    except Exception as e:  # noqa: BLE001
        log.fail("EC GDPS lake temperature", cfg["sources"]["geomet_wms"], e)
        fb = cfg["water_temp_fallback_c"][today.month]
        out = {s["id"]: {"value_c": fb, "basis": "monthly estimate (lake temperature unavailable)"} for s in svcs}
    return out


# --------------------------------------------------------------------------- EC alerts

CAP_NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}


def _point_in_poly(lat, lon, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        y1, x1 = poly[i]
        y2, x2 = poly[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            xi = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < xi:
                inside = not inside
    return inside


def fetch_ec_alerts(cfg, log, now_utc):
    """Active EC alerts (public and marine) whose CAP polygon covers any service point.

    Reads today's and yesterday's CWTO CAP directories, keeps the newest message
    per alert reference chain, and drops cancelled or expired ones.
    """
    points = [(s["id"], s["lat"], s["lon"]) for s in cfg["services"]]
    files = []
    for day in (now_utc - dt.timedelta(days=1), now_utc):
        ymd = day.strftime("%Y%m%d")
        for base in cfg["sources"]["ec_alerts_cap"]:
            root = base.format(yyyymmdd=ymd)
            r = _get(root, log, f"EC CAP index {ymd}")
            if r is None:
                continue
            hours = sorted(set(re.findall(r'href="(\d{2})/"', r.text)))
            for hh in hours:
                rr = _get(root + hh + "/", log, f"EC CAP {ymd}/{hh}")
                if rr is None:
                    continue
                files += [root + hh + "/" + f for f in sorted(set(re.findall(r'href="([^"]+\.cap)"', rr.text)))]
            break  # first base URL that answers wins

    messages = {}
    superseded = set()
    for url in files:
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception:  # noqa: BLE001
            continue
        ident = root.findtext("cap:identifier", namespaces=CAP_NS)
        msg_type = root.findtext("cap:msgType", namespaces=CAP_NS)
        refs = root.findtext("cap:references", namespaces=CAP_NS) or ""
        for ref in refs.split():
            parts = ref.split(",")
            if len(parts) >= 2:
                superseded.add(parts[1])
        for info in root.findall("cap:info", CAP_NS):
            if not (info.findtext("cap:language", namespaces=CAP_NS) or "").startswith("en"):
                continue
            expires = info.findtext("cap:expires", namespaces=CAP_NS)
            try:
                if expires and dt.datetime.fromisoformat(expires) < now_utc:
                    continue
            except ValueError:
                pass
            hit = set()
            areas = []
            for area in info.findall("cap:area", CAP_NS):
                areas.append(area.findtext("cap:areaDesc", namespaces=CAP_NS))
                for poly in area.findall("cap:polygon", CAP_NS):
                    pts = [tuple(map(float, p.split(","))) for p in poly.text.split()]
                    for sid, la, lo in points:
                        if _point_in_poly(la, lo, pts):
                            hit.add(sid)
            if not hit:
                continue
            messages[ident] = {
                "id": ident, "msg_type": msg_type,
                "event": info.findtext("cap:event", namespaces=CAP_NS),
                "headline": info.findtext("cap:headline", namespaces=CAP_NS),
                "severity": info.findtext("cap:severity", namespaces=CAP_NS),
                "urgency": info.findtext("cap:urgency", namespaces=CAP_NS),
                "onset": info.findtext("cap:onset", namespaces=CAP_NS),
                "expires": expires,
                "description": (info.findtext("cap:description", namespaces=CAP_NS) or "").strip(),
                "areas": sorted(set(a for a in areas if a)),
                "services": sorted(hit),
                "url": url,
            }
    active = [m for k, m in messages.items() if k not in superseded and m["msg_type"] != "Cancel"]
    # One line per event per area set: keep the latest
    dedup = {}
    for m in active:
        dedup[(m["event"], tuple(m["areas"]))] = m
    return sorted(dedup.values(), key=lambda m: (m["severity"] != "Severe", m["event"] or ""))


def fetch_ec_city(cfg, code, log):
    """EC city page RSS: warnings plus the public forecast text, for the cross-check."""
    if not code:
        return None
    url = cfg["sources"]["ec_city_rss"].format(code=code)
    r = _get(url, log, f"EC city forecast {code}")
    if r is None:
        return None
    try:
        root = ET.fromstring(r.content)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = []
        for e in root.findall("a:entry", ns):
            entries.append({
                "title": e.findtext("a:title", namespaces=ns),
                "category": (e.find("a:category", ns).get("term") if e.find("a:category", ns) is not None else ""),
                "summary": re.sub("<[^>]+>", " ", e.findtext("a:summary", namespaces=ns) or "").strip(),
                "updated": e.findtext("a:updated", namespaces=ns),
            })
        return {"code": code, "url": url, "entries": entries}
    except Exception as e:  # noqa: BLE001
        log.fail(f"EC city forecast {code}", url, e)
        return None


def fetch_ec_marine(cfg, log):
    """EC marine forecast text for the configured areas: issue time and the forecast body."""
    import html as _html
    out = []
    for area in cfg["sources"]["ec_marine_areas"]:
        r = _get(area["url"], log, f"EC marine — {area['name']}")
        if r is None:
            continue
        txt = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
        txt = _html.unescape(re.sub(r"<[^>]+>", "\n", txt))
        txt = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", txt))
        a, b = txt.find("Issued"), txt.find("Stay connected")
        body = txt[a:b] if a >= 0 else ""
        # Keep the summary block and the extended block; drop the per-element repeats
        main = body.split("Winds\n")[0].strip()
        ext = body[body.find("Extended Forecast"):].strip() if "Extended Forecast" in body else ""
        warn = re.findall(r"([A-Z][a-z]+ (?:wind|gale|storm|freezing spray)[a-z ]* warning in effect)", body)
        out.append({"name": area["name"], "url": area["url"], "text": main, "extended": ext,
                    "warnings": sorted(set(warn))})
    return out


def marine_warning_alerts(ec_marine, alerts):
    """Warnings stated in the EC marine forecast text that the CAP feed did not carry.

    EC marine warnings (strong wind, gale, storm, freezing spray) are issued with the
    marine forecast and do not always appear in the CAP alert feed, so the header would
    otherwise say no alerts are in effect while the marine forecast carries one.
    """
    have = {(a.get("event") or "").lower() for a in alerts}
    out = []
    for m in ec_marine or []:
        for w in m.get("warnings", []):
            event = re.sub(r"\s+in effect$", "", w).strip().lower()
            if event in have:
                continue
            out.append({"id": f"marine:{m['name']}:{event}", "msg_type": "Alert", "event": event,
                        "headline": f"{w} — {m['name']}", "severity": None, "urgency": None, "onset": None,
                        "expires": None, "description": "", "areas": [f"{m['name']} (EC marine forecast)"],
                        "services": [], "url": m["url"], "source": "EC marine forecast text"})
    return out


# --------------------------------------------------------------------------- imagery

def fetch_radar_frames(cfg, extent, log, now_utc, kind="rain", frames=3, step_min=30, width=900):
    """Composite radar frames from MSC GeoMet WMS, newest last. Returns list of (time, png bytes)."""
    lon0, lat0, lon1, lat1 = extent
    aspect = (lat1 - lat0) / (lon1 - lon0) / 0.72  # rough cos(lat) correction
    height = int(width * aspect)
    layer = cfg["sources"]["radar_layers"][kind]
    t_last = now_utc.replace(second=0, microsecond=0)
    t_last -= dt.timedelta(minutes=t_last.minute % 6 + 6)  # radar composites every 6 min; step back one
    out = []
    for i in range(frames - 1, -1, -1):
        t = t_last - dt.timedelta(minutes=step_min * i)
        params = {
            "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": layer,
            "CRS": "EPSG:4326", "BBOX": f"{lat0},{lon0},{lat1},{lon1}",
            "WIDTH": width, "HEIGHT": height, "FORMAT": "image/png", "TRANSPARENT": "TRUE",
            "TIME": t.strftime("%Y-%m-%dT%H:%M:00Z"),
        }
        png = _get(cfg["sources"]["radar_wms"], log, f"EC radar {layer} {t:%H:%MZ}", params, binary=True)
        us = None
        if cfg["sources"].get("radar_us_wms"):
            p2 = dict(params, LAYERS=cfg["sources"]["radar_us_layer"], VERSION="1.1.1", SRS="EPSG:4326",
                      BBOX=f"{lon0},{lat0},{lon1},{lat1}")
            p2.pop("CRS")
            p2.pop("TIME")  # US mosaic: latest only
            if i == 0:
                us = _get(cfg["sources"]["radar_us_wms"], log, "NOAA MRMS reflectivity", p2, binary=True)
        if png:
            out.append({"time": t.isoformat(), "png": png, "us_png": us})
    return out


def fetch_image(url, log, name):
    return _get(url, log, name, binary=True)


def fetch_ice(cfg, log):
    """Latest CIS Great Lakes chart. The CIS product page links the image; we follow it."""
    page = cfg["sources"]["ice"]["cis_latest_page"]
    r = _get(page, log, "CIS ice product page")
    if r is None:
        return None
    ctype = r.headers.get("content-type", "")
    if ctype.startswith("image/"):
        return {"png": r.content, "url": r.url}
    m = re.search(r'(?:src|href)="([^"]+\.(?:gif|png|jpg))"', r.text, re.I)
    if not m:
        log.fail("CIS ice chart", page, "no image link on product page")
        return None
    img_url = requests.compat.urljoin(r.url, m.group(1))
    img = _get(img_url, log, "CIS ice chart")
    return {"png": img, "url": img_url} if img else None


def png_size(png):
    from PIL import Image
    return Image.open(io.BytesIO(png)).size
