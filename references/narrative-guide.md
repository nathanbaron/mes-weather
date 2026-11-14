# Writing the narrative

The briefing is ministry work product. It may be FOI'd. Formal declarative prose,
Canadian spelling, 24-hour time, vessel names in capitals, no emoji, nothing
personal.

## Data, not direction

The briefing informs captains; it never directs them. The Master makes the
final decision on every crossing. Colours draw attention to forecast data. They
are not operating limits and not go/no-go criteria.

Call the Notion values **reference thresholds**. Say what the forecast shows
against them, and when. Do not explain the hazard or its consequences; masters
know their vessels and routes.

| Write | Not |
|---|---|
| Sustained winds reach 31 kt from the west-southwest by 16:00, at or above the 30 kt reference threshold. | Expect to suspend service at 16:00. |
| Gusts reach the 20 kt WOLFE ISLANDER IV reference threshold at the secondary ramp. | Gusts hit the WI4 limit. |
| Models differ by 9 kt at the peak; confidence in timing is moderate. | Consider swapping vessels. |
| Light freezing spray potential from Wednesday 03:00. | Crews should de-ice. |
| East winds of 18 kt at Glenora from 14:00. | Unsafe at the narrows. |

Avoid: limit, safe/unsafe, exceed, breach, cancel, suspend, shut down, tie up,
should, must, recommend, advise, consider, no-go, "do not sail". `render`
checks the narrative for these and prints a LANGUAGE line for each; rewrite
until none remain.

## Fields in narrative.json

**headline** — one sentence. The single most important thing in the period.
"Westerly gale reaches the Wolfe Island and Amherst Island reference thresholds
Monday afternoon; cold northwest flow follows midweek."

**summary** — three to five sentences, chronological. Which services, which days,
when it starts, when it eases, how confident. Mention any EC alert and whether
the analysis agrees with it.

**synopsis** — one short paragraph a ferry officer can picture. Name the
systems (low, front, ridge), where they are now, where they go, and what that
does to wind direction and temperature locally. No jargon beyond "low",
"front", "ridge", "trough". No millibar-by-millibar analysis.

**services.<id>** — two or three sentences. What is happening and when: the
status, the time it changes, peak values with direction, model agreement. Do not
explain why a condition matters to the vessel or route; masters know their
routes. Green services get one sentence.

**ec_crosscheck** — where EC's public and marine forecasts agree or differ from
the model consensus: timing, strength, direction. If EC carries a warning the
analysis does not support (or the reverse), say so plainly. Name the EC
products read and their issue times.

**radar** — when present: what is on radar now, where it is heading, and when it
reaches the routes. Omit when no precipitation is expected.

**ice** — in season: concentration and ice type near each route from the latest
CIS chart, and the trend. Omit out of season.

**author** — "Prepared by the MES Weather Briefing (Claude), reviewed against EC
forecasts".

## Confidence language

- Model spread ≤ 5 kt at the peak: "models agree".
- 5–10 kt: "moderate agreement".
- > 10 kt, or only one model shows it: "low confidence; one model is an outlier".
- Beyond 72 h: say timing may shift.

## Length

The summary section fits on the first page with the grid and key times. The
whole narrative is under 500 words.
