---
name: marine-weather-briefing
description: >
  Produce the daily Marine Weather Briefing for MTO Eastern Ontario ferry
  services (Wolfe Island, Amherst Island, Glenora, Howe Island, Simcoe Island):
  five-day meteograms per service from public weather models, green/yellow/red
  status against each vessel's MES wind thresholds and hazards (freezing spray,
  visibility, extreme temperature, snow, freezing rain), EC alerts in the header,
  a short synoptic summary with chart, radar when precipitation is coming, and
  CIS ice charts in winter. Built at 05:00, delivered by 06:00. Use whenever
  Nathan or a scheduled run says "weather briefing," "marine weather brief,"
  "daily weather brief," "fleet weather," "run the 05:00 brief," "weather for
  the ferries," "what's the wind doing for the boats," or asks whether any
  service will hit its wind threshold this week. Not for Nathan's personal
  morning weather (that's morning-briefing).
---

# Marine Weather Briefing

Daily forecast briefing for officers and managers across Marine Services. It
compares forecast data with MES **reference thresholds** to inform captains and
managers. **It never gives operational direction.** Colours draw attention;
they are not operating limits or go/no-go criteria. The Master makes the final
decision. `references/narrative-guide.md` sets the vocabulary; `render` flags
directive words.

The brief is ministry work product: formal declarative prose, Canadian
spelling, 24-hour time, knots, °C, mb, NM for visibility, vessel names in
capitals, no emoji, nothing personal.

## Files

- `config/services.yaml` — services, coordinates, wind rules, hazard levels, sources
- `scripts/brief.py` — `build`, `render`, `all`
- `scripts/check_sources.py` — reachability and model-run timing
- `references/status-criteria.md` — what green, yellow and red mean
- `references/model-schedule.md` — run times and why 05:00 is a 00Z brief
- `references/narrative-guide.md` — how to write each section

Requires Python 3.10+ and `requirements.txt` (`eccodes` decodes GFS GRIB);
`cartopy` optional (coastlines on radar). PDF uses headless Chromium.

Data comes straight from the issuing agencies, no API keys: ECCC GeoMet
(HRDPS, RDPS, GDPS), NOAA NOMADS (GFS), MET Norway (ECMWF-based). A build
makes roughly 2,000 small requests and takes a few minutes.

## Procedure

Work in a dated folder, e.g. `OUT=briefings/$(date +%F)`.

0. **Install** (fresh environments): `pip install -q -r requirements.txt`.
   Cartopy fetches coastlines from naturalearth.s3.amazonaws.com on first use.

1. **Build.** `python scripts/brief.py build --out $OUT`
   Fetches five models per service, EC alerts, synoptic charts, radar (if
   precipitation within 36 h or a precip-type alert), and the CIS ice chart (in
   season). Writes `data.json`, `img/`, and a draft `narrative.json`.
   Read the printed line: source failures and stale models.

2. **Look before writing.** Read `data.json` (events, daily status, peak
   wind, model runs, source log). View every meteogram, the synoptic charts and
   any radar or ice image. The status grid is computed; your job is to explain
   it, not to recompute it.

3. **Cross-check against EC.** `data.json` carries the EC marine forecasts
   for Eastern Lake Ontario and Kingston to Cornwall (`ec_marine`, with issue
   times) and the Kingston and Picton public forecasts (`ec_city`). Compare
   timing, strength and direction with the consensus. EC alerts are already in
   the header from the CAP feed; say whether the analysis agrees with them.

4. **Write `narrative.json`** following `references/narrative-guide.md`:
   headline, summary, synopsis, one paragraph per service, ec_crosscheck,
   radar (if shown), ice (in season), author. Under 500 words in total. Every
   number must come from `data.json`, a chart, or an EC product you read.

5. **Re-check alerts** close to delivery (05:45). If a new EC alert has been
   issued, re-run `build` into the same folder, keep your narrative, and update
   it.

6. **Render.** `python scripts/brief.py render --out $OUT` → `briefing.html`
   (self-contained) and `briefing.pdf`. If it prints any `LANGUAGE:` lines,
   rewrite those phrases in `narrative.json` and render again. Do not deliver
   with language flags outstanding.

7. **Deliver.** `python scripts/send_email.py --out $OUT` emails the brief from
   `mes-weather-briefing@agentmail.to` to the recipients in `distribution`,
   with `email.html` as the body and the PDF attached. It reads
   `AGENTMAIL_API_KEY` from the environment and refuses sample data or
   outstanding language flags. If the key is missing, send `email.html` as the
   body with the AgentMail `send_message` tool (no attachment; the PDF is too
   large to pass through a tool call) and add one line saying the PDF could not
   be attached.

If there is no time or no one to write, `python scripts/brief.py all --out $OUT`
produces a complete brief with the auto-drafted narrative.

## When things fail

- **No model data at all:** the draft headline says INCOMPLETE. Do not
  substitute EC text forecasts as if they were the analysis; say what is missing.
- **Some sources failed:** the brief lists them under *Data sources and run
  times*. Mention anything material (e.g. no EC alert feed) in the summary.
- **Stale model:** flagged automatically. Note it if the stale model drives a
  yellow or red.

## Changing thresholds or hazards

The authoritative wind thresholds live in Notion, **Operational Weather
Thresholds** (MES Command Centre, `collection://c81d64e1-dbf4-4ea7-8e8a-31b84274fae2`).
Mirror any change into `config/services.yaml` and commit. Hazard levels and the
yellow logic are proposals in the config; change them there.

## Testing

`python scripts/brief.py all --out /tmp/mwb --sample` runs the whole pipeline on
synthetic data. The output is stamped SAMPLE DATA throughout and must never be
distributed.
