# Status criteria — green, yellow, red

The colour compares forecast data with **MES reference thresholds** for each vessel
and terminal, not the Environment Canada warning system. Colours draw attention to
data; they are not operating limits or go/no-go criteria, and the Master makes the
final decision. EC alerts appear in the header and the
cross-check; they do not set the colour. Where the two disagree, the narrative
says so.

Status is computed hourly from the model consensus, then rolled up to the worst
hour of each day for the summary grid.

## Wind (per service, from the Notion Operational Weather Thresholds database)

| Service | Rule | Threshold | Sector (wind from) | Source |
|---|---|---|---|---|
| Wolfe Island | SW to NW | 30 kt | 210–315° | Notion row |
| Wolfe Island | Kingston Terminal, hard west | 30 kt | 248–292°* | Notion row |
| Wolfe Island | WOLFE ISLANDER IV, Kingston SVR | **gusts**: yellow 20 kt, red 25 kt | 180–270° | Kongsberg study, May 2026; applied to gusts, yellow 20 / red 25 (Nathan, 14 Nov 2026) |
| Amherst Island | Dead west / dead east | 30 kt | 248–292° / 068–112°* | Notion row |
| Glenora | East quadrant | 20 kt | 045–135° | Notion row |
| Howe Island | Hard east (cable tension) | 25 kt | 068–112°* | Notion row |
| Howe Island | Hard west (cable tension) | 30 kt | 248–292°* | Notion row |
| Simcoe Island | — | none defined | — | — |

\* Interpreted as one compass point (22.5°) either side of the named direction.
Confirm or replace with explicit bearings.

Thresholds are treated as **sustained** wind, except a rule marked
`measure: gust` in the config, which is compared with gusts. The WOLFE
ISLANDER IV secondary ramp rule is the only gust rule: gusts set her off the
dock. It also sets its own yellow (`yellow_kt: 20`) in place of the 5 kt
margin: yellow from 20 kt gusts, red from 25 kt. For a gust rule, read "gusts"
for "sustained" in the colours below. The informal 30 kt Wolfe Island
figure is an internal capability judgement and master's discretion, not a
Transport Canada limit (Nathan and Rob, 16 Oct 2026).

**Red** — consensus sustained wind in sector at or over the threshold.

**Yellow** — any of:
- sustained in sector within 5 kt of the threshold;
- gusts in sector at or over the threshold;
- sustained at or over the threshold within 20° outside the sector (a small
  veer or back puts it in);
- any single model at or over the threshold in sector while the consensus is
  under it (model disagreement worth watching).

**Green** — none of the above.

## Hazards (all services)

| Hazard | Yellow | Red | Basis |
|---|---|---|---|
| Visibility | < 2.0 NM | < 0.5 NM | Model visibility; low skill — treat as indicative |
| Freezing spray | light | moderate or worse | Overland (1990) predictor; red matches the EC freezing spray warning criterion |
| Wind chill | ≤ −20 | ≤ −30 | ECCC wind chill formula |
| Humidex | ≥ 35 | ≥ 40 | ECCC humidex formula |
| Snowfall | ≥ 5 cm in 12 h | ≥ 15 cm in 24 h | Consensus snowfall |
| Freezing rain / drizzle | any hour (2+ models agree) | 2+ consecutive hours | Model weather codes |
| Thunderstorms | model thunder code, or CAPE ≥ 1000 J/kg with precipitation | — (EC warning governs) | |

These hazard levels are MES Weather Briefing proposals, set to be conservative
for awareness. They are not EC warning criteria and not operating limits.

## Freezing spray

Overland (1990) icing predictor, fresh water (freezing point 0 °C):

    PPR = Va × (Tf − Ta) / (1 + 0.3 × (Tw − Tf))

Va wind speed (m/s), Ta air temperature, Tw water temperature, Tf freezing point.

| PPR | Class | Approximate icing rate |
|---|---|---|
| ≤ 0 | none | — |
| 0 – 22.4 | light | < 0.7 cm/h |
| 22.4 – 53.3 | moderate | 0.7 – 2.0 cm/h |
| 53.3 – 83.0 | heavy | 2.0 – 4.0 cm/h |
| > 83.0 | extreme | > 4.0 cm/h |

Water temperature comes from the ECCC GDPS lake surface analysis (nearest water
cell to each route), otherwise a monthly eastern-Lake-Ontario estimate (config
`water_temp_fallback_c`). The brief
states which was used. Once ice cover forms, open-water spray risk falls; read
the CIS chart alongside.

Which vessels are most sensitive to freezing spray is not yet recorded in Notion.
Until it is, spray potential is reported for every service.

## Ice

During the ice season (1 Dec – 30 Apr, configurable) the latest Canadian Ice
Service Great Lakes chart is embedded and summarized. Ice does not set a colour
automatically; the narrative describes concentration and stage of development in
the service areas from the chart.

## Changing a threshold

Change the Notion database first, then `config/services.yaml` in the same sitting,
and commit with the reason. The brief prints the rules it used under each service,
so a stale threshold is visible to readers.
