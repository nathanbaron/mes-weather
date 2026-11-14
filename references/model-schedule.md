# Model runs and the 05:00 build

Typical figures from published schedules. **Confirm with live numbers:** run
`python scripts/check_sources.py --runs --log runs.jsonl` at 05:00 daily for a
week. It reports the newest run each source holds at that moment.

Observed 25 Oct 2026 at 13:00 EDT: HRDPS, RDPS, GDPS and GFS all on the 12Z run
(5 h old); MET Norway updated 13:17Z.

Models are fetched directly from the agencies: ECCC GeoMet (HRDPS, RDPS, GDPS),
NOAA NOMADS (GFS), MET Norway (ECMWF-based). Open-Meteo was dropped: its free
tier is rate-limited per IP address, and the cloud environment's shared
addresses had already exhausted the daily quota.

| Model | Grid | Cycles (UTC) | Range | Typically available |
|---|---|---|---|---|
| HRDPS (ECCC) | 2.5 km | 00, 06, 12, 18 | 48 h | ~3.5–4.5 h after init |
| RDPS (ECCC) | 10 km | 00, 06, 12, 18 | 84 h | ~3–4 h after init |
| GDPS (ECCC) | 15 km | 00, 12 | 10 d | ~3.5–5 h after init |
| GFS (NOAA) | 0.25° | 00, 06, 12, 18 | 16 d | ~4–5 h after init (complete) |
| ECMWF IFS open data | 0.25° | 00, 06, 12, 18 | 15 d (00/12) | ~7–8 h after init |

MET Norway publishes its ECMWF-based forecast roughly hourly and lags ECMWF by a few hours.

## What 05:00 local catches

| | 05:00 EDT = 09:00 UTC | 05:00 EST = 10:00 UTC (from 1 Nov 2026) |
|---|---|---|
| HRDPS / RDPS | 00Z certain; 06Z not yet | 00Z certain; 06Z borderline |
| GDPS | 00Z | 00Z |
| GFS | 00Z | 00Z (06Z not until ~10:30 UTC) |
| ECMWF | 00Z, if published on time | 00Z |

So the 05:00 build is a **00Z-cycle briefing** in all seasons. That is the
right cycle: it is the freshest full set and every model in it shares one
initialization time, which keeps the consensus honest. Moving the build later
gains little until about 11:00 local, when the 06Z set is complete.

Sensitivity: ECMWF 00Z is the tightest margin in summer (EDT). If the run
metadata shows it still on 12Z at build time, the brief flags it in
*Data sources and run times*; the consensus carries on with the other four.

## EC products

- **Alerts (CAP, Ontario Storm Prediction Centre, CWTO):** issued as needed,
  read fresh at build. The skill re-reads them immediately before delivery.
- **Marine forecasts (Eastern Lake Ontario; Kingston to Cornwall):** the
  extended forecast is issued at 03:00 and the regular forecast is updated
  through the day (10:30 observed). The 03:00 issue is in hand at 05:00. The
  brief records the issue time it read.
- **Public forecasts:** regular early-morning issue around 05:00–05:30 local.
  A 05:00 build may see the overnight issue; the pre-delivery re-check picks
  up the morning one if it is out.

## Timeline

| Local | Step |
|---|---|
| 05:00 | Build: models, alerts, charts, radar, ice |
| 05:00–05:40 | Narrative written; EC cross-check |
| 05:45 | Re-check EC alerts and morning forecast issue; amend if changed |
| 05:50 | Render HTML + PDF |
| 06:00 | Delivered |
