# Architecture

How `airo` is put together, and why. Read this before making structural changes —
several of the decisions here look odd until you know what they're avoiding.

---

## 0. Orientation — start here

**What this is.** A local air-quality monitor. It polls the sensor networks you configure,
stores every reading in SQLite on your own machine, fuses them into one number with
provenance, and shows it in a menu bar, a dashboard and a settings window. Python with **no
runtime dependencies**, plus an optional Rust tray. Nothing is uploaded anywhere.

**Why it is careful.** The number this tool displays is one someone may act on — closing a
window, keeping a child indoors, going for a run. A wrong number looks exactly like a right
one. That is why so much here is about *not silently being wrong*: sentinel rejection,
corroboration, provenance on every reading, and a strong preference for saying "I don't
know" over showing something plausible.

### Read in this order

| # | Read | For |
|---|---|---|
| 1 | [CONVENTIONS.md](CONVENTIONS.md) | The hard rules, the traps that have already bitten, and the verification block to run before committing |
| 2 | §1 below | How the pieces fit together |
| 3 | §2.5a, §2.5b, §2.5c, §2.5e | The four decisions most often undone by accident: why SQLite, why fusion is a decision, why a flagged reading is shown rather than hidden, and why an indoor sensor never speaks for the air outside |
| 4 | §3 | Bugs that have shipped once. Each is easy to reintroduce |
| 5 | §7a | How this is tested, and the mistakes the test suite is shaped around. Read before writing a test here |
| 6 | [ROADMAP.md](ROADMAP.md) | What is planned, what is deliberately not being done, and the risk register |

### Invariants — break these and something is wrong that tests may not catch

Each is enforced. If you change the enforcing test, you are changing the rule.

| Invariant | Why | Enforced by |
|---|---|---|
| Raw µg/m³ is what gets stored; an index is derived for display | The same air gives different index numbers on different national scales, so a stored index is a number whose meaning depends on a setting | `test_scales.py` |
| The reading path is append-only | Ingest idempotency is what makes gap repair safe. One deliberate exception: `store.repair_sentinels` | `test_store.py::TestGapRepair` |
| Nothing is silently discarded | A suspect reading is flagged and shown. If there is a fire next door, that is the air being breathed | `test_fusion.py`, ARCHITECTURE §2.5c |
| No air-quality logic in the tray or the pages | A second copy of a health decision drifts. They render `latest.json` | CI grep + `test_contracts.py` |
| No API key is logged, printed or committed | Keys live in `~/.airo/<provider>.key`, mode 600 | `test_settings_api.py` |
| Nothing of the user's enters the repository | Not data, not a config, not a real coordinate or sensor id | `test_contracts.py::TestNoRealPlaceIsCommitted`, `.gitignore`, a pre-commit hook, CI |
| The Python side imports only the standard library | The installer ships a bare interpreter; a dependency breaks it | CI AST check |
| Attribution and the health disclaimer stay | Required by PurpleAir ToS §4.8/§7.3 and CC BY | `test_obligations.py` |
| An indoor sensor is never the headline, never corroborates, never joins the weather correlation, never feeds the forecast, never raises an outdoor alert | `nearest` is the default rule and a sensor in the house is ~0 km away, so without this it wins every time and kitchen air is reported as the street | `test_indoor.py`, ARCHITECTURE §2.5e |
| A source that stops observing is reported, even while its provider answers | A dead sensor behind a live API is silent by default, and the record grows a hole nobody sees for days | `test_alerts.py`, `test_end_to_end.py`, §2.5g |
| No test may reach the network, the developer's `~/.airo`, a browser or the notification centre | Each has happened; the fourth destroyed data that was not recoverable | `tests/*guard.py`, `tools/check.py` user-data gate, §7a |
| A guard must be shown to fail when broken | A test that has never failed is a claim nobody has checked | `tools/faults/*.json`, run by CI on every push |

### If you are changing… read

| Changing | Read first |
|---|---|
| Ingest, backfill, gap detection | §2.3, `store.py` header, `test_store.py` |
| Which reading becomes the headline | §2.5b, §2.5c, `fusion.py` |
| A provider, or adding a network | §1, the `Provider` banner in `poller.py`, `test_providers.py` |
| Bands, scales, thresholds | §4, `SCALES` in `poller.py` — the single copy |
| The dashboard or settings page | §2.8, the header comment in the file itself |
| The tray | §2.8, hard rule 7. It renders; it does not decide |
| Adding a sensor, or anything indoor/outdoor | §2.5e, §2.5f, `test_indoor.py` |
| What a measurement is shown in | §2.4a, `units.py`. Storage units are rule 6 and are not negotiable |
| Alerting, or what counts as a source going quiet | §2.4, §2.5g |
| A dashboard panel | §7a "The pages", `test_page_render.py`. `node --check` passing means the file parsed, nothing more |
| A test, or a guard | §7a in full, especially "Things that keep going wrong" |
| Packaging, the installer, CI | §4a, ROADMAP §3f |
| Anything touching the OS | The Windows traps in CONVENTIONS.md. A platform fallback returning a constant is a feature that silently does nothing |

### Working on this safely

- **Run the full verification block before committing.** It is in CONVENTIONS.md and covers
  the Python suite, `py_compile`, the Rust tests, both JSON configs, and a syntax check of
  the JavaScript extracted from both pages.
- **Never let a test touch the real `~/.airo`.** Every harness overrides `HOME`,
  `AIRO_CONFIG` and `AIRO_DATA`. A test that missed those once moved a real database aside
  fifteen times. Paths resolved at import are the usual cause — see §3.
- **A test that passes for the wrong reason is worse than no test.** The habit here is to
  reintroduce the bug and confirm the test goes red. Several tests in this repository were
  green against code that did nothing.
- **Reversals are fine; undocumented ones are not.** Requirements change and decisions get
  revised — §2.5a reverses the original CSV decision, §2.8 revises where settings live.
  Record the reasoning where the old decision was written, so the next reader sees a
  decision rather than a contradiction.

---

## 1. Shape of the system

```
   PurpleAir  ┐
   QLD Gov    │
   NSW Gov    ├──▶  poller.py --once     runs ~2s on an interval, then EXITS
   OpenAQ     ┘     (one provider class per network)
                          │
                          │  per source: gap check → backfill → live reading
                          ▼
                    ┌───────────────┐
                    │   store.py    │   SQLite: one row per source per observation
                    │  data/airo.db │   deduped on (source, observed_utc)
                    └───────┬───────┘
                            │  latest reading per source
                            ▼
                    ┌───────────────┐
                    │   fusion.py   │   one headline number + provenance:
                    │               │   nearest | freshest | all | blend
                    │               │   + corroboration against peers & history
                    └───────┬───────┘
                            │ writes
                    ┌───────▼────────┐
                    │  latest.json   │  in ~/.airo/data — atomic write, the single
                    └───┬────────┬───┘  view everything else reads
          read (file)   │        │   read (HTTP: /api/latest, /api/series)
        ┌───────────────┘        └──────────────┐
   ┌────▼─────┐                        ┌────────▼────────┐
   │ tray/    │                        │ dashboard.html  │
   │ (Tauri)  │                        │ via --serve     │
   └──────────┘                        └─────────────────┘
```

**No consumer contains air-quality logic.** They render `latest.json`. The fusion rule and
every threshold live in Python exactly once — otherwise the tray and the dashboard could
disagree about what you are breathing. See §2.5a.

Scheduling is per-platform (`scheduler.py`): launchd on macOS, a `systemd --user` timer on
Linux, Task Scheduler on Windows. The data layer is identical on all three.

`poller.py` is the single control surface — start/stop/restart, poll, alerts, backfill, logs. The
tray shells out to `scheduler.py` and `poller.py` directly, so every action is available
without a terminal on every platform.

Two processes, never both required:

- **The poller** is a scheduled task. It is the only thing that matters. It does not depend
  on the server, the dashboard, or any widget.
- **The server** exists only while you're looking at the dashboard. Started on demand by
  `poller.py --open`, bound to `127.0.0.1`.

The widgets read the JSON file directly, so they work with no server and no resident process.

---

## 2. Key design decisions

### 2.1 `StartInterval`, not a resident daemon

The first version used `KeepAlive` with an internal `sleep` loop. That keeps a Python
process resident 24/7 purely to do nothing for 14 minutes and 58 seconds out of every 15.

Now `launchd` starts the process, it polls, it exits. **Between polls there is no process
at all.** Idle memory is zero rather than ~20 MB.

The trade-off: a periodic task can't host a long-lived HTTP server, which is why the server
became a separate on-demand concern. That turned out to be a security win too — no listening
socket unless you're actually using it.

> **Consequence for tooling:** `launchctl list` shows an empty PID column between runs.
> This is correct. `poller.py --doctor` exists partly to stop that looking like a fault.

### 2.2 The `.app` bundle

`launchd` reports the executable it runs. Pointed straight at Python, macOS Background Task
Management tells the user *"python3 can run in the background"* — meaningless and slightly
alarming.

The fix was a minimal `Airo.app` at the repository root — a shell-script executable that
`exec`s Python, with `CFBundleName` "Airo" so Background Task Management says something
meaningful.

**Nothing in the tree builds one any more.** It was written by the shell installer, deleted
in August 2026 when its jobs moved into Python; the bundle was the one job that did not
move. `scheduler.py`'s `macos_install()` still *prefers* such a bundle if the checkout
happens to have one, and otherwise points `launchd` straight at `python3 poller.py --once` —
so on any checkout made since, the Background Task Management entry reads "python3" again.
This section claimed `scheduler.py install` generated it for about as long, which is why it
is written out here rather than quietly corrected: the capability was re-attributed in a
table when the script was deleted, and nobody ran the check that would have caught it.

`Airo.app/` stays in `.gitignore` because an older checkout still has one, with absolute
paths baked in.

The macOS application that actually **ships** is a different artifact entirely: the Tauri
bundle, built from `tray/` into `tray/target/` by the pipeline in §4a. It is also called
`Airo.app`, which is worth knowing before reading either name as the other.

### 2.3 Gap backfill is the core feature

Any logger can poll. The thing that makes this trustworthy is that **downtime is repaired
rather than lost**.

On every poll, for each source in turn, `do_poll()`:

1. asks the store for that source's newest observation
2. measures the silence against `gap_threshold_for()` — the provider's own reporting
   interval, or the poll interval if that is longer, scaled by fusion's staleness
   tolerance. A flat 25 minutes suited PurpleAir's 10-minute average and fired on every
   poll against an hourly regulatory feed, where half an hour of quiet is what normal
   looks like
3. if the gap is real, calls `backfill_source()`, which asks *that provider's* history
   endpoint for the missing window — every `Provider` implements one, so repair is not a
   PurpleAir privilege
4. then takes the live reading

A source with no readings at all is seeded instead, `backfill_days_on_first_run` deep.

So a Mac asleep from 6pm to midnight loses nothing — the next poll recovers those six hours.
This is why the tool can make claims about "the whole week" without an always-on machine.

Repair is safe to repeat: the refetch starts two reporting intervals *before* the last
known reading, so a half-written interval is redone rather than straddled, and inserts are
idempotent on `(source_id, observed_utc)`, which makes the overlap free. How a window is
chunked is each provider's own business — PurpleAir splits it into 2-day requests, with a
pause between them, to stay inside its limits.

### 2.4 Alerting is threshold + trend, with hysteresis

`maybe_alert()` runs after each successful poll and never raises — a notification failure must
not break data collection.

Three triggers:

| Kind | Condition | Rationale |
|---|---|---|
| `crossed` | 10-min average enters the alert band (default AQI 67, bottom of amber "Fair") | The event the user cares about |
| `climbing` | Still below threshold, but 10-min runs `rising_delta` above the 1-hour average | Indoor levels lag outdoor, so warning early is worth more than warning accurately |
| `cleared` | Fell to below 85% of threshold after having been over | Tells the user when it's safe to ventilate |

**Hysteresis matters.** `over_threshold` is persisted in `~/.airo/data/alert_state.json`, so a value
hovering at the boundary produces one alert, not twenty. The `cleared` trigger uses 85% of
threshold rather than the threshold itself for the same reason. A `cooldown_minutes` floor and
configurable `quiet_hours` sit on top.

Notifications go via `osascript`. **macOS attributes them to "Script Editor"**, not to our
`.app` bundle, because `osascript` posts on behalf of the calling process. Users must allow
Script Editor in Notification settings. Posting from the bundle itself would fix this and is
on the roadmap.

The default threshold of **67** is not arbitrary — it is exactly the lower bound of the amber
band on the Australian scale (16.75 µg/m³). The scale is configurable (§4) and this default
does **not** move with it: `threshold_aqi` is read in whatever scale is set, so a config
tuned for Australia means something else entirely under US EPA. `threshold_pm25` is the
scale-independent form, and it wins when both are set.

### 2.4a Units are a display concern, resolved per quantity

`units.py` decides what to *show* a measurement in. It never decides what to store one
as — rule 6 keeps µg/m³, Celsius, m/s and km canonical in the database, and every
conversion is exactly invertible.

Per **quantity**, not a single metric/imperial flag, because no such flag is correct: the
UK is Celsius and miles per hour. Resolution order is the reader's explicit setting, then
their region, then metric. Nothing is cached and nothing is written at setup, so changing
region changes the next screen with no migration to run.

PM2.5 is deliberately absent from the conversion table. µg/m³ is the unit everywhere,
including the United States, and offering an alternative would invent a problem.

The AQI *scale* is a separate axis from units and is configured separately — an index is
not a concentration, and the dashboard says so next to the number, with the arithmetic
that produced it. "48" is not interpretable without knowing which scale it is on; the
maintainer asked exactly that question, which is why the explanation is on the page
rather than in a document.

### 2.4b No dependencies

*Numbered 2.4b rather than 2.5, which it shared with the CSV decision below for long
enough that three documents cited "§2.5" meaning two different things. 2.5 stays with
CSV, because 2.5a exists to reverse it and says so by number.*

Standard library only. No `pip install`, no virtualenv, no lockfile, no supply chain.

This is a deliberate constraint, not laziness: the tool has to survive being ignored for a
year and still run. Every dependency is a future breakage. `urllib` is uglier than `requests`
and that's an acceptable price.

There is no exception. Chart.js was the last one — loaded from a CDN by the dashboard, which
handed that CDN the IP of a machine viewing its own home air quality and gave a third party
arbitrary code in a page rendering those coordinates. It also meant a *local* logger could not
draw its own local data offline. Replaced by a canvas renderer implementing only the surface
the two charts use; see §3.3.

### 2.5 CSV, not SQLite — *superseded in v0.5, see 2.5a*

> **This decision was reversed.** It is kept here because the reasoning was sound for the
> system it described, and because a reader should be able to see what changed and why.

A CSV is greppable, diffable, openable in Excel, and readable in fifty years. At 10-minute
resolution a decade is ~500k rows — well within what a rewrite-on-append approach handles.

`append_rows()` read the whole file, merged, sorted and rewrote it. That's O(n) per poll,
which was fine at single-source scale.

### 2.5a SQLite as the store, CSV as the export (v0.5)

Three requirements arrived together and invalidated 2.5's premises:

1. **Several sources per location.** One `pm25_10min` column structurally means one source.
2. **Normalising sources onto a common time grid.** That is a join plus a time bucket — a
   query. Hand-rolling one over N CSV files in Python is building a worse query engine.
3. **A Tauri tray shell.** A *second language* now reads the same data.

The third is decisive and the easiest to overlook. With per-source CSVs, the fusion rule
("nearest usable source, skipping stale and faulty ones") would be implemented **twice** —
once in Python for the poller and dashboard, once in Rust for the tray. Two implementations
of a health-relevant decision, free to drift, where a disagreement means the menu bar and
the dashboard tell you different things about the air you are breathing. With SQLite the
rule is written once and both languages read its output.

The measured cost of staying on CSV, benchmarked on the real read-sort-rewrite cycle:

| Rows | Cycle | Rewritten per poll |
|---|---|---|
| 17,000 (v0.4 reality) | 44 ms | 1.5 MB |
| 160,000 (3 sources, 1 year, 10-min) | 399 ms | 13.7 MB |
| 500,000 | 1,228 ms | 42.9 MB |

At 15-minute polls the three-source case rewrites ~1.3 GB/day. Multi-source turned
[ROADMAP #8](ROADMAP.md) from a distant concern into an immediate one.

**The preservation argument in 2.5 is answered, not dismissed.** SQLite is one of only five
storage formats the US Library of Congress recommends for datasets — see
[sqlite.org/locrsf](https://www.sqlite.org/locrsf.html). It is a preservation format, not a
risky binary blob. `python3 poller.py --export` writes one plain CSV per source, and
`tests/test_store.py::TestExport` round-trip tests it, so the fifty-year guarantee stands —
we just stopped paying for it on every poll.

**What was genuinely lost:** the data is no longer directly `grep`-able. Inspecting it now
needs `sqlite3 data/airo.db` or `--export`. That is a real cost and 2.5 was right to value
it; it was simply outweighed.

`sqlite3` is in the Python standard library, so this costs no dependencies. Rust reads the
same file natively.

### 2.5b Fusion is a decision, not a calculation

When several instruments disagree, choosing what single number to show is a judgement with
consequences — someone may open or close a window because of it. `fusion.py` therefore holds
to three rules, and they are worth defending in review:

- **Never invent a measurement.** Only the opt-in `blend` rule produces a value no instrument
  reported, and it is labelled as computed wherever it appears.
- **Always report provenance.** Every result names its source, distance and age, so
  "12 from the sensor in my street two minutes ago" is distinguishable from "12 from a
  monitor across the city an hour ago".
- **Stale data is not current data.** A source quiet past twice its own reporting interval is
  skipped, not shown as now. Staleness is judged per source: 40 minutes of silence is an
  outage for a 10-minute consumer sensor and completely normal for an hourly regulatory feed.

The default rule is `nearest`, because the tool exists to describe *local* air. This is not
academic — on the reference install a consumer sensor nearby reads ~19 µg/m³ while
a government monitor across the city reads ~5. Picking the wrong source shows "Very good" to
someone breathing "Fair" air.

### 2.5c Corroboration — telling a real event from a false positive

A single sensor reading far above every neighbour is one of three things, and they look
identical in isolation:

1. a genuine very local source — a wood heater or fire next door
2. an instrument fault — a spider in the inlet is the classic cause
3. a real regional event that has not reached the other sensors yet

The distinction matters. One is worth closing a window for; one is worth cleaning a sensor
for; and reporting "Hazardous" for the whole suburb when one sensor has a blocked inlet
destroys trust in every future alert.

Airo applies three independent checks:

**Instrument self-consistency.** A PurpleAir contains *two* laser counters reading the same
air. When they disagree by more than 2× the instrument is faulty, not the air — this is the
single most reliable fault signal available and it is free. `store.assess_quality()` also
honours the provider's own confidence figure. Both are stored per reading (`pm25_a`,
`pm25_b`, `confidence`) so a fault is diagnosable after the fact.

**Peer agreement.** `fusion.corroborate()` compares each source against the median of the
others. Beyond 3× — and only when the absolute level is above 12 µg/m³, since ratios on noise
are meaningless — the reading needs explaining.

**Its own history.** This is what stops a valley sensor being permanently accused of lying.
`store.peer_ratio_history()` computes how this source has historically compared with its peers
*at this hour of day*, over 90 days. A sensor that always reads 3× its neighbours after sunset
is measuring a real drainage effect and is reported as `typical_for_site`. The same sensor
reading 11× for the first time in ninety days is `uncorroborated`. Where the same-hour sample
is too thin, it falls back to all hours and says which basis it used; below 20 comparisons it
admits it does not know rather than guessing.

**An uncorroborated reading is flagged, never discarded.** If there genuinely is a fire next
door, that is the air being breathed, and suppressing it would be the more dangerous error.
The number is shown as measured, the alert still fires, and every surface — dashboard, menu
bar, tray, notification — says plainly that the neighbours do not see it.

Worked example from the reference install:

| | |
|---|---|
| Consumer sensor (nearby) | ~80 µg/m³ — AQI ~320, "Hazardous" |
| Government monitor (across the city) | ~5 µg/m³ — AQI ~19 |
| Every other sensor in the city | AQI 0–16 |
| This site's usual ratio to peers | median ≈1.1×, p90 ≈2.6×, 90-day max ≈4.8× |
| **This reading** | **≈17×** — far beyond its own historical maximum |
| Channels A/B | ~107 / ~95 — agree, so *not* an instrument fault |

Conclusion presented to the user: real particulate, genuinely local, almost certainly a fire
or heater nearby rather than regional air quality.

### 2.5d Your data and the system's data are separate things

Two categories, kept physically apart, with no overlap:

| | **System** | **Yours** |
|---|---|---|
| Where | the git checkout | `~/.airo/` |
| What | code, `config.example.json`, docs, tests | settings, readings, API keys, logs |
| Lifetime | replaced on every `git pull` | outlives every clone, move and upgrade |
| Shareable | yes — that is the point | no, ever |
| If deleted | `git clone` again | **gone** |

```
<checkout>/                     ~/.airo/
  poller.py  store.py             config.json          your location, sources, preferences
  fusion.py  setup.py             data/airo.db         every reading you have taken
  dashboard.html  tray/           data/*.log           what the poller did
  config.example.json             <provider>.key       credentials, mode 600
  tests/  docs
```

Resolution order, so a developer can redirect either without editing anything:
`$AIRO_CONFIG` → `~/.airo/config.json` → `./config.json`, and `$AIRO_DATA` →
`~/.airo/data` → `./data`. The in-checkout paths come last and exist only so an
older install keeps working until `--migrate-data` is run.

**Why this matters more than tidiness.** Config in a working tree gets committed
— it holds a location, which is personal data. Readings in a working tree get
*destroyed* — by a re-clone, a moved folder, a `git clean`. Config leaking is
embarrassing; readings vanishing is unrecoverable, because they cannot be
regenerated at any price.

Both failures have actually happened here. The original `config.json` shipped
one person's suburb and sensor to everyone who cloned the repo. Later,
`--migrate-data` left the old folder as `data.migrated-<timestamp>/`, which the
`data/` gitignore rule did not match, and 16,995 rows of location history were
committed and pushed. Neither was noticed by a human reading a diff.

So the separation is **enforced, not merely documented**:

- `.gitignore` matches data by *shape* — `data.*/`, `**/data/`, `*.db`,
  `readings*.csv`, backup archives — not by one known directory name
- a pre-commit hook (`./tools/install-hooks.sh`) refuses the commit
- CI fails the build on any tracked database, log, readings export, backup
  archive, real config, credential, or any file whose first line looks like a
  readings CSV header
- `tests/test_fresh_install.py` asserts no source file contains an absolute
  home path and that the shipped example config carries no real values

Anything shipping user data past four independent checks is a bug worth a
post-mortem, not a typo.

### 2.5e Placement — an indoor sensor never speaks for the air outside

`sources.placement` is `outdoor`, `indoor` or `unknown` (schema v8). Three-valued, not a
boolean, and that is the load-bearing part: treating an unprobed sensor as outdoor is
precisely how the contamination below happens by default.

Detected, not asked. PurpleAir returns `location_type` per sensor and
`poller.purpleair_placement()` maps it; a setup question about "location type" is
answered wrongly by somebody who has just unboxed a sensor. Settable by hand where the
provider does not say.

**Why it exists.** `nearest` is the default fusion rule and a sensor in the house is
~0 km away, so without this an indoor sensor becomes the headline. Then:

- the tray, dashboard, band and advice report kitchen air as the local reading — "avoid
  outdoor exertion", rendered from a sensor next to a wok;
- alerts fire on cooking, as outdoor air-quality warnings;
- corroboration marks the *outdoor* sensors uncorroborated, because the indoor one
  disagrees and `fusion` cannot know one of them is measuring a different room;
- Phase B correlates PM2.5 against outdoor wind, and Phase C fits its forecast to Phase
  B's bands — so one contaminated join reaches every claim made about the future.

The rule, and it is absolute: **an indoor sensor is excluded from the headline, from
corroboration, from the weather correlation, from the forecast and from outdoor alerts —
and shown, prominently, in its own right.** Rule 5a one level up: nothing is discarded,
and nothing is allowed to mean something it does not.

**Where the exclusion goes matters.** `fusion.annotate()` attaches age, distance and
staleness — facts about an *instrument*. `fusion.fuse()` makes a claim about the air
outside. Only the second excludes indoor sensors. The split was once applied above
`annotate()`, so an indoor sensor was never annotated at all and its dashboard row showed
`— / —` with no stale tag however long it had been dead. An exclusion that silences a
failure alarm is worse than no exclusion, because an old reading with no age looks
current.

`unknown` is decided **per consumer**, with the reasoning where the decision is: excluded
from the headline (the safe direction is to say nothing), included in the display (the
safe direction is to show it).

### 2.5f Inside against outside — two failure modes with opposite remedies

`analyse.indoor_outdoor()` answers the question the placement column makes possible: *is
my indoor air staying clean, or is the outdoor air getting in?*

That is not a side-by-side chart. It is a claim about a building, with two answers that
call for opposite actions:

| Verdict | Signature | Remedy |
|---|---|---|
| `outdoor air getting in` | indoor tracks outdoor, lagged; the I/O ratio drifts toward 1 | close up, filter, **do not** ventilate |
| `indoor source` | indoor rises while outdoor stays flat; the ratio spikes well above 1 | ventilate — *if* outdoor is clean enough to ventilate with |
| `holding` | indoor stays well below outdoor | nothing |

Tell somebody the wrong one and you have advised them to open a window during a smoke
event, or to seal the house around a fire they lit. The indoor-source case is therefore
tested **first**, so the dangerous mistake cannot be reached by falling through.

Three constraints follow from that:

- **The lag is real.** Indoor levels follow outdoor by roughly an hour in an ordinary
  house. The comparison correlates at lags of 0, 1 and 2 hours and states which it used.
- **It is a decision, not a rendering** (rule 7). Every word — verdict, advice and basis
  — is computed in Python and served. Three surfaces describing one relationship three
  ways is how they drift, and one of those ways will be the wrong remedy.
- **It refuses when it cannot support a claim.** Fewer than 24 paired hours, or outdoor
  air below 3 µg/m³ for the whole window, and it says so instead of dividing two numbers
  near the instrument's floor and reporting the noise. Silence with a reason is an
  answer this project already knows how to give.

### 2.5g Reporting is not the same as responding

A source is silent in two different ways, and they need opposite advice:

| | What it looks like | What to tell the reader |
|---|---|---|
| unreachable | the fetch raises | check the key, check the network, run `--doctor` |
| stale | the fetch succeeds and the observation time has stopped moving | the provider is still answering; the sensor itself is offline |

The second was invisible for a long time. `record_source_result()` counted polls that
*raised*, and PurpleAir does not stop answering when a sensor drops off the network — it
serves that sensor's last reading, with its original timestamp, for as long as you keep
asking. So the fetch succeeded, the counter reset every poll, and the detector never
fired while the record developed exactly the hole its own docstring exists to prevent.

On the maintainer's install this ran for **about two days** in silence. The gap was being
computed one line above the call site, logged, and thrown away.

`source_is_reporting()` now derives it from whether the source *observed* anything,
judged against the provider's own cadence — for the same reason `fusion.is_stale` gives:
forty minutes of silence is an outage for a ten-minute consumer sensor and completely
normal for an hourly regulatory feed.

### 2.8 Settings live in the app's window; the dashboard stays a browser link

*Supersedes the installer plan's "the dashboard and settings move into the Tauri window,
one application". Recorded as a revision rather than left as a divergence, because the
plan and the code disagreed for a while and that is how a decision quietly becomes an
accident.*

**What changed.** The original objective was one window hosting both views. In practice the
two views want different things, and the requirement was refined once the installer made
non-technical users the default audience:

| | Where it lives | Why |
|---|---|---|
| **Settings** | Airo's own window | Configuring an app should not send you to a browser tab. It is also the first thing a new user meets, and a tab that opens behind the browser they already had open is a step people lose. |
| **Dashboard** | The browser, from a tray shortcut | A year of history, an evening heatmap and a worst-nights table are a wide reading surface. A 480px tray window is the wrong shape, and a browser gives zoom, print and a bookmark for free. |

Both are also reachable from `setup.py` and the command line, which remain fully supported.

**What did not change, and is the point.** The settings page is still *served by Python over
loopback* and merely displayed by the window. It is not bundled into the webview. Three
reasons, in order of how expensive they would be to get wrong:

1. **The validator stays in Python.** `validate_settings()` is shared with `setup.py`, so
   the two front ends cannot disagree about what a valid setting is. A copy compiled into
   the tray would be a second opinion in a second language.
2. **The page needs the per-process token** the server substitutes as it serves it. That
   token is what makes a write safe on a loopback port, and it is deliberately never
   written to disk.
3. **Rule 7 in spirit.** The tray renders; it does not decide. Hosting a view is rendering.
   Reimplementing what the view does would not be.

**The URL is always Python's answer.** The tray asks `poller.py --url settings` and points
a webview at what comes back. It must never build an address: `serve_port` is configurable
*and* the server moves itself when something else holds the port, so any URL assembled in
Rust is wrong for some users and eventually wrong for everyone — which is exactly what a
hardcoded `http://127.0.0.1:8787/dashboard.html` did for the whole of v0.5. A browser tab
pointed at a dead URL looks broken; an **app window** showing a connection error looks like
*the app* is broken, so this got sharper with the move, not softer. Enforced by
`test_contracts.py::TestTheTrayNeverBuildsItsOwnUrl`.

**The window is reused, not recreated.** Opening settings twice previously meant two
windows, each holding its own token-bearing page, and a save from the stale one failing for
reasons invisible to the user.

---

## 3. Traps and hard-won knowledge

Every item here was a real bug. Please don't reintroduce them.

### 3.1 PurpleAir nests rolling averages under `stats`

The single-sensor endpoint returns `pm2.5` at the top level but puts
`pm2.5_10minute`, `pm2.5_60minute` and friends inside a **`stats`** object. Requesting them as
fields works; reading them from the top level silently returns `None`.

`poll_current()` uses a `field()` helper that checks `stats`, then top level, then `stats_a`:

```python
def field(name):
    for src in (stats, s, stats_a):
        v = fnum(src.get(name))
        if v is not None:
            return v
    return None
```

If the 10-minute average is absent it falls back to the instantaneous value and sets
`headline_is_fallback: true` in `latest.json`.

### 3.2 `toISOString()` will silently corrupt day-bucketing

`Date.prototype.toISOString()` converts to **UTC**. In Brisbane (UTC+10), a 9am local reading
becomes the *previous* UTC date while a 9pm reading stays on the current one. Grouping
readings by `toISOString().slice(0,10)` therefore scrambles any evening-vs-daytime comparison.

Always use `localDateKey()`:

```js
const localDateKey = d =>
  d.getFullYear()+'-'+pad2(d.getMonth()+1)+'-'+pad2(d.getDate());
```

This produced an evening-premium chart that was wrong by up to 10× before it was caught.

Enforced by `test_dates.py::TestDayBucketing`, which runs the real helper under
`Australia/Brisbane` and `America/New_York` and asserts the *same instant* files under
different dates in each — if the key were UTC-derived both would agree and every other
assertion would be vacuous. `TestEveningsAreBucketedByNight` covers the harder half: 00:30
belongs to the evening that began the day before, and filing it under the new date splits
one episode across two rows and halves the peak of both.

Until this was written the only check was a grep for `const localDateKey` in the source,
which catches the helper being deleted and nothing else — a rename, a rewrite, or a caller
reaching for `toISOString()` elsewhere all passed.

### 3.3 The x-axis is a linear scale over epoch-ms, deliberately

Historically because Chart.js does not parse dates itself — a time scale required
`chartjs-adapter-date-fns`, and without it `drawChart()` threw, which (render steps then
running in sequence) blanked four unrelated panels.

Chart.js is gone: the renderer is now ~330 lines of canvas in `dashboard.html`, supporting
exactly one line chart and one bar chart. The reason to keep the linear-epoch axis is
unchanged and now structural — the renderer has no date handling at all, and the custom tick
generation is what keeps day-sized steps on calendar boundaries across DST (§3.2).

Two fixes, both retained:

- The x-axis is a **linear scale over epoch-ms** with `afterBuildTicks` generating ticks on
  local hour/day boundaries. A bare linear scale picks round numbers of *milliseconds*, which
  never land on clean clock times — hence axis labels like `06:44`.
- Every render step runs in its own `try/catch`, so one failure can't cascade. The guard
  belongs to the *act of running a step*, not to a list of steps: applied to a list, it
  drifted back — `renderHeader()` was being called bare, and it calls `renderFooter()`,
  the surface carrying attribution and the health disclaimer, so a throw in either blanked
  every panel after it. `render()` now does nothing but run `RENDER_STEPS` through one
  guard, and `test_dates.py::TestEveryRenderStepIsGuarded` fails if it ever calls anything
  else or if a drawing function becomes unreachable from a guarded step.
- **Reduction keeps the peaks, so it is min/max and not an average.** A year backfilled at
  ten-minute resolution is ~52,000 points and the "All" view goes sluggish without
  reduction. `store.series(bucket_minutes=…)` returns each bucket's own minimum and
  maximum, because on this data the spikes *are* the signal — an averaged bucket erases
  exactly the half-hour a reader opened the chart to look at. Decided in SQL rather than
  in the page, like every other judgement about what a number means.
- **The trend is not recomputed here.** `compute_trend()` in `poller.py` says in its own
  docstring that it lives there so the menu bar, the tray and the dashboard cannot
  disagree — and this page had its own copy, thresholds included. It renders
  `latest.trend` now.

### 3.4 Rounding vs classification

Display rounds; band classification must use the same rounded value, or a reading of
`33.2` shows as "33" while being coloured as the next band up.

This was written as an instruction to callers — *use `bandFor(Math.round(v))`* — and was
followed in **one** call site out of nine. An obligation on callers is a rule that gets
broken. Both `band_for()` in Python and `bandFor()` in the dashboard round their own input
now, so a caller cannot get it wrong.

The same audit found the boundaries written out **three** times: `BANDS` and `BANDS_D` in
the dashboard, plus the scale tables in Python. Both dashboard copies were the Australian
numbers regardless of the configured scale, so a US EPA install read its index against
Australian bands. `scale_bands()` serves them in `latest.json` and `/api/series`; the page
carries the colour ramp, which is presentation, and nothing else.

The advice sentences went the same way, for the same reason and one release later. Six of
them sat in a JS table and were joined to the served bands **by position** — so on the raw
µg/m³ scale the band "Above WHO guideline" was handed "Enjoy normal activities.", a sentence
written for an Australian band that stops at 16.5 µg/m³. Wording is a health decision, so it
lives in `SCALES` beside the band it describes and is rendered verbatim (rule 7, D8). Only
`au` carries advice today; `us_epa` and `raw` serve none and their surfaces show the band
name alone, because borrowing another scale's sentence is the bug, not the fix.

### 3.5 Data quality — some readings are not real

A PurpleAir record can contain readings above **1000 µg/m³**, which is not
plausible suburban ambient air (it exceeds Australian Black Summer peaks). Causes are
typically a blocked inlet, a sensor fault, or something burning directly beside the unit.

`store.assess_quality()` decides this **once, at ingest**, and stores the answer in the
`quality` column. It judges on the concentration, not on a derived index, and it uses more
than the level: a PurpleAir's two laser channels disagreeing is the most reliable fault
signal available and looks nothing like genuinely bad air, and the network's own confidence
figure is used when it publishes one.

**Three verdicts, not two**, and the split is the important part. `pm25 > SUSPECT_PM25`
used to answer `suspect`, which asserts *the instrument is broken* on the strength of a
number that is a statement about *the air*. Australian suburbs sat well past 350 µg/m³ for
days during Black Summer. On exactly those days every aggregate went quiet: the readings
that mattered most were filed as sensor faults and filtered out of the chart, the evening
analysis, fusion, and therefore the alert. A tool that goes silent when the air turns
dangerous is worse than one that never claimed to watch it.

| verdict | means | counted? |
|---|---|---|
| `ok` | nothing to say | yes |
| `extreme` | implausibly high, and **nothing suggests a fault** | **yes** — drawn, analysed, alerted, and marked |
| `suspect` | positive evidence the instrument is wrong — channels disagreeing, or low reported confidence | no |

So fault evidence is examined **first**, and the level is consulted only when there is
none. A single-value government feed cannot self-check, and that is not suspicious — it is
most of the network.

Whether an extreme value should be *believed* is a different question, and it already has
a different mechanism: `fusion.corroborate()` marks a reading no neighbour can confirm.
Quality is about the instrument; corroboration is about the value. Neither is asked to do
the other's job.

The policy is *surface, don't silently drop*:

- flagged readings are shown, with the reason, never removed
- the heatmap's summary row uses **medians**, so a handful of extreme values cannot distort
  "a typical evening"
- `series()` excludes instrument faults and includes extreme air; `include_suspect=True`
  returns everything, for auditing the record rather than reporting the air
- the chart's y-axis is a **high percentile**, not the maximum, so a lone spike is pinned
  to the top edge and marked rather than stretching the axis twentyfold and flattening an
  ordinary week — while air that is bad for a *sustained* period does move the axis, which
  is what should happen
- a database written before the split is re-assessed once, on the first open, from the
  channel and confidence columns it already stores (schema v5)

**The dashboard must not re-derive this.** It did: its own `IMPLAUSIBLE = 1400` threshold on
the *index*, converting back to µg/m³ by dividing by four — which is the Australian scale
written out as arithmetic, and wrong on any other. Two answers to "is this plausible?", and
the stored one is better informed because it saw the channels and the confidence. The page
renders the `quality` field now.

The threshold itself is a constant rather than a setting, deliberately. ROADMAP #6
originally promised "a configurable threshold"; a user who can raise it can silence a
genuine sensor fault, which is the one thing this check exists to prevent.

---

## 4. The Australian AQI

`AQI = µg/m³ ÷ 25 × 100`, where 25 µg/m³ is the NEPM 24-hour PM2.5 standard.

Bands: 33 / 66 / 99 / 149 / 200, then Hazardous.

This is **not** the US EPA AQI, which uses different breakpoints and a piecewise-linear
mapping. Comparing a number from one scale to the other is meaningless. The scale is
resolved in `poller.py` (`SCALES`) and rendered everywhere else, so it is configurable in
one place — CONVENTIONS.md hard rule 7, and the invariant in §0 that no surface converts
an index itself.

---

## 4a. The bundled Python runtime

Airo's own code has no dependencies and runs on a stock interpreter. The installed app
nevertheless carries its own Python, which **reverses** ROADMAP's "explicitly not doing:
bundling a Python runtime".

**Why the original decision was right.** macOS ships a Python; bundling one adds tens of MB
for a developer who already has a better one. That reasoning holds for the audience the
project had.

**What changed.** The audience did. `python3: command not found`, or a version too old for
the syntax, is where someone with no technical knowledge stops for good — and there is no
README wording that recovers them, because they are not reading a README at that point,
they are closing a window. An installer that works for that person cannot assume an
interpreter.

**What it does not change.** Hard rule 1 stands untouched: it forbids Python *packages*,
and it still does. Shipping an interpreter is not permission to depend on anything, and CI
still fails if a dependency manifest appears. `tools/fetch_runtime.py` is itself
standard-library only, and a test asserts it.

**What it costs, stated so whoever maintains this next inherits it honestly:**

| | |
|---|---|
| Source | `astral-sh/python-build-standalone`, relocatable CPython, PSF-licensed like CPython itself |
| Pinned | version and release date are literals in `tools/fetch_runtime.py`, never "latest" — a build that resolves at build time is not reproducible, and a bad upstream release would ship without anyone deciding to |
| Verified | SHA-256 recorded per architecture and checked **before extraction**. A rejected archive is never unpacked, because whatever runs next would find a plausible tree and use it |
| Size | ~69 MB per architecture, unstripped |
| Ongoing | its security updates are now ours. Refreshing is deliberate: change the pin, run `--print-checksums`, paste, commit on its own so the diff shows what moved |
| Not committed | gitignored and reproducible from the pin, so the repository does not carry 69 MB of someone else's binaries |

**Apple Silicon only**, deliberately: every Mac Apple has sold since 2020 is arm64, and
Intel means a second runtime, a second build and a second thing to test for a shrinking
population. An Intel user is told so in a sentence that names the alternative, rather than
meeting a stack trace. Windows and Linux are ROADMAP §3f.

### First run

Copying files is not the end of installing. Nothing is collected until a background poll is
scheduled, and nothing *useful* until the user has said where they are. `poller.py
--first-run` knows both, and the app calls it on **every** launch — so the property that
matters is not what it does the first time but that the second time is equally safe.

Idempotent by construction rather than by a flag, because a flag is a thing that can be
wrong: registering the agent replaces any existing registration, creating the data
directory is a `mkdir`, and the settings page opens only while nothing is configured.

Two orderings are deliberate:

- **The data directory is checked before anything is scheduled.** An agent registered for a
  directory it cannot write fails every fifteen minutes into a log nobody reads.
- **The poll is scheduled even when nothing is configured yet.** An unconfigured poll logs
  that it has nothing to do, which is harmless; the alternative is an app that silently
  collects nothing until somebody remembers to come back and switch it on.

The tray spawns it and never waits: the menu must appear even if this is slow or fails.

### One command surface, not two

`scheduler.py install`, `poller.py --doctor`, `poller.py` and `poller.py --open` were four
shell scripts — 759 lines, macOS-only, wrapping commands `poller.py` and `scheduler.py`
already exposed. They are gone, and CI fails if a `*.sh` reappears.

They were not ported. Every one of them was a **second copy of maintained behaviour**: the
alerts toggle edited `config.json` from a shell script, with its own idea of the file's
shape and no knowledge of the validator the settings page uses. On Windows and Linux none
of them ran at all, so half the documented workflow simply did not exist there.

Two things they did that Python could not, moved before anything was deleted:

- **Which folder the agent runs in.** The launchd label is fixed, so a second checkout or a
  moved folder means launchd reports *the other install* as healthy while this one never
  runs — everything looks fine and nothing is collected. Now
  `scheduler.agent_belongs_to_this_project()`, reported by `--doctor`. Read from the plist
  rather than parsed out of `launchctl print`, whose output format is not a contract.
- **Alerts on/off, the log tail, and stopping the server.** Now `--alerts`, `--logs` and
  `--stop-server`, so the tray runs one command per menu item and every decision stays in
  Python.

### Two audiences, two sentences

`how_to()` phrases guidance for whoever is reading it. Someone who downloaded a disk image
has no terminal open and no reason to want one, so `run: python3 setup.py` is, to them, an
error message — and that is exactly what a brand-new install said until an end-to-end test
from the `.dmg` showed it. In a checkout the command is still what you want.

Kept in one function so a new message cannot quietly reintroduce a terminal instruction
into an app with no terminal. Three were missed on the first pass and only surfaced because
a test read the whole of `--status` rather than the line being thought about.

**The disk image carries no licence agreement.** It did: `licenseFile` made `hdiutil`
present the full AGPL as a click-through before the image would mount, which is a wall of
legal text as the very first thing the software does, and legally unnecessary — nothing in
the AGPL conditions *use* on acceptance. It also blocked automated verification entirely,
which is how it was found. The licence ships inside the app, which is what the AGPL
actually requires.

**A disk-image build that fails on the leftovers of the last one.** `cargo tauri build
--bundles dmg` exits non-zero saying only `error running bundle_dmg.sh`, and the usual
cause is an interrupted earlier run: `hdiutil` still holds the read-write image and its
`/Volumes/dmg.XXXXXX` mount point, and every build after that fails identically until they
are detached. Nothing in the message suggests any of this, and the `.app` bundles fine
throughout — so it presents as a disk-image problem and is in fact a leftover.

```bash
hdiutil info | grep -B4 /Volumes/dmg.        # find the stale mount
hdiutil detach /Volumes/dmg.XXXXXX -force
rm -f tray/target/release/bundle/macos/rw.*.dmg
```

Worth knowing before a release rather than during one.

### Uninstalling

`--uninstall` stops both background jobs and **deletes nothing**. It prints where the
readings are and how many there are, so somebody who does want them gone makes that
decision themselves.

The asymmetry is the design: removing the software is reversible in ten minutes, and
removing the record is not reversible at all. An uninstaller is also reached at exactly the
moment nobody is reading carefully, which is the worst possible place to put an
irreversible action behind a confirmation nobody will read.

### Finding the payload

`resolve_root()` and `resolve_python()` take the executable's directory as an argument
rather than calling `current_exe()` themselves. That is the only reason they can be tested:
a resolver exercised solely in production is one nobody hears about until an installed app
reports "No reading yet" beside a full database — and a resolution failure looks identical
on screen to having no data at all.

Order in both: an explicit environment override, then a bundle, then a development
checkout. An **empty** override is ignored rather than obeyed, because `AIRO_HOME=""` is
how an unset variable arrives from a plist or a shell, and obeying it points the tray at
the filesystem root.

The checkout marker is `poller.py`. It used to be `config.json` or `data/`, and **both are
absent from a fresh clone** — the config is gitignored and the data directory is created on
the first poll — so the tray fell through to `"."` and found nothing.

Inside a bundle the shipped interpreter is used, never `python3` from the path. Falling
back would mean the app works on a machine that happens to have a Python and fails on the
machine it was built for, which is the one place that failure is invisible until it
matters.

### What ships, and how it gets there

`tools/stage_bundle.py` assembles the payload; the bundler ships the staged tree. Two
things were learned by doing it the obvious way first, and both failed **silently**:

- the bundler does not follow `..` out of its own directory, so `../poller.py` produced an
  empty folder and a build that succeeded
- a glob mapped to a destination *flattens* it, so `runtime/**/*` put
  `lib/python3.12/__future__.py` at `runtime/__future__.py` and destroyed the interpreter

Staging also makes "what ships" a list somebody wrote rather than whatever was lying about.
The modules are named individually; a glob over the repository would ship a scratch file, a
stale export or somebody's real `config.json`. After staging, the tree is scanned and the
build **fails** if it contains a config, a key, a database or a CSV — checked rather than
trusted, because the runtime tree is copied wholesale and the list is a human artefact.

## 5. Security model

| Concern | Approach |
|---|---|
| API key at rest | `~/.airo/apikey`, mode `600`, **outside the repo** so it can't be committed or synced |
| Key in transit | `X-API-Key` header over HTTPS; never a query parameter |
| Key in logs | Never logged, never printed, never included in error output |
| Network exposure | Server binds `127.0.0.1` only, and only runs on demand |
| **Another page reaching the server** | Binding to loopback keeps other *machines* out, not other *pages*: every site the user visits can reach it from inside their browser. Four independent checks on any mutating request — a loopback `Host` (a name the attacker owns, resolved to 127.0.0.1, is same-origin to the browser, so an origin check never sees it), our own `Origin`, a JSON content type so the request cannot be a preflight-free CORS "simple request", and a per-process token the settings page is handed when it is served. The `Host` check applies to reads too, because `/api/settings` describes the user's location |
| **The settings token** | Generated per process, never written to disk, never fetchable. A restart invalidates it, and the page says "reload" rather than "forbidden" |
| **Opening a native dialog** | `/api/choose-folder` puts a window on the user's desktop, so it sits behind the same guards as a write |
| Privilege | A user `LaunchAgent`. No root, no `LaunchDaemon`, no `sudo` anywhere |
| Environment | The plist sets a minimal explicit `PATH` rather than inheriting the shell |
| Data | Everything stays local. Outbound connections go only to the provider and weather hosts the configured sources need — the complete list is the hostname table in [SECURITY.md](SECURITY.md) |

Key resolution order is env var → `~/.airo/<provider>.key` → the legacy
`~/.airo/apikey`. A **private** PurpleAir sensor is the exception: its
`read_key` belongs to one source rather than to a network, so it lives inside
`config.json`. That makes `config.json` a file that can hold a credential,
which is why `settings_payload()` reports keys as presence only and
`scrub_secrets()` walks the result again before it is served.

`/api/keys` is the only route that accepts a credential, so it is the only one
to audit. Nothing echoes the value: the response reports presence and whether
the file could be restricted, and the log records that a key was set rather
than what it was — the log is the surface that outlives the session and gets
pasted into bug reports.

---

## 6. File map

| Path | Role |
|---|---|
| `poller.py` | Providers, polling, backfill, fusion wiring, alerting, HTTP server, CLI. Once "everything data"; the store and the fusion decision have since been separated out. |
| `store.py` | SQLite schema and every read or write of it. Ingest, dedup, gap detection, series and bucketing, export, retention, integrity checks. |
| `fusion.py` | Choosing one number from several sources, and corroborating it. Safety-critical — §2.5b, §2.5c. |
| `weather.py` | Hourly wind, temperature, humidity and pressure from Open-Meteo — the *cause* the readings are the effect of. Capture and backfill only; it correlates nothing and forecasts nothing. ROADMAP #9 Phase A. |
| `forecast.py` | Guardrails for anything said about the future, and the six-hour outlook that has to earn its way past them: hedged wording, a stated basis, silence until skill is measured over 30 verified outcomes, PurpleAir excluded from training by construction. ROADMAP #9 Phase C. |
| `units.py` | What to *show* a measurement in, never what to store it as. Resolves a display unit per quantity from the reader's region, with an explicit config override winning. Nothing here writes: rule 6 keeps µg/m³, Celsius, m/s and km canonical in the database, and every conversion is exactly invertible. |
| `setup.py` | First-run wizard: geocode, discover nearby monitors, probe which are reporting, write the config. |
| `scheduler.py` | Cross-platform background scheduling: launchd, systemd timers, Task Scheduler. |
| `backup.py` | Portable export/restore of config and readings. Keys excluded unless asked. |
| `analyse.py` | Evening-premium analysis and corroboration-threshold tuning, from the CLI. |
| `dashboard.html` | Single-file UI. No build step, no external assets — the chart renderer is local canvas. |
| `poller.py` | **Single control surface.** start/stop/restart, poll, alerts, backfill, logs. |
| `scheduler.py install` | Idempotent installer. Builds the bundle, writes the plist, verifies. |
| `poller.py --doctor` | Health check. Knows that "no process" is the correct resting state. |
| `poller.py --open` | Starts the server if needed, opens the browser. |
| `~/.airo/config.json` | Location, sources, scale, fusion rule, retention, `data_dir`. **Never the API key.** |
| `tray/` | The only widget. Tauri/Rust, cross-platform. Reads `~/.airo/data/latest.json` and renders it; `--print-menu` prints the readout without a window server. |
| `~/.airo/data/` | Readings, `latest.json`, logs. Outside the checkout since v0.6; a `data/` inside it is pre-v0.6 and gitignored. |
| `Airo.app/` | A leftover from the deleted shell installer; nothing rebuilds it. `scheduler.py install` uses one if it is already there and otherwise runs `poller.py` directly. Gitignored — it contains absolute paths. Not the shipped app, which is the Tauri bundle under `tray/target/`. §2.2. |
| `tools/check.py` | The gates. What CI runs; run it before every push. §7a. |
| `tools/faultcheck.py` | Breaks the product on purpose and reports which tests notice. §7a. |
| `tools/faults/*.json` | The committed faults, run by CI on every push. |
| `tools/stage_bundle.py` | Stages the payload the tray bundles. |
| `tests/*guard.py` | Block an effect — network, `~/.airo`, browser, notifications. Not stubs. §7a. |
| `tests/test_contracts.py` | Claims about the codebase rather than about a function. §7a. |
| `tests/test_end_to_end.py` | Journeys through the real CLI. §7a. |
| `tests/test_page_render.py` | The pages' own JavaScript, executed against a payload. §7a. |

### `poller.py` internals

Read off the module, and checked against it: `test_contracts.py::TestDocsMatchTheCode`
fails if this table names a function `poller.py` does not have. It described five that
had not existed since v0.5 — the CSV-era `poll_current()`, `backfill()` and
`append_rows()`, plus `au_aqi()`/`au_band()`, which survive only as deprecated keys in
the JSON payload.

| Function | Responsibility |
|---|---|
| `do_poll()` | One cycle, per source: gap check → backfill → live reading, then weather, fuse and publish, retention, routine backup, alerting. Each source isolated, so one failing does not stop the rest |
| `poll_source()` | One source's live reading: fetch, clean, insert, and learn where the sensor is (§2.5e) |
| `backfill_source()` | Chunked history fetch for one source. Dedup happens on insert, so repair is safe to repeat (§2.3) |
| `gap_threshold_for()` | How long a silence must be before it counts, scaled to the provider's own cadence — a fixed value fires every poll against an hourly feed |
| `record_source_result()` | Counts consecutive silent polls and says so once. A sensor that stopped observing behind a provider that keeps answering (§2.5g) |
| `capture_weather()` / `backfill_weather()` | The hours beside the readings — the cause the readings are the effect of. Forecast host for recent hours, archive host for old ones |
| `build_latest()` | Everything a surface renders, assembled once: the fusion decision, bands, trend, provenance and attribution. This is `latest.json` |
| `aqi_for()` / `band_for()` / `scale_bands()` | Scale conversion and banding, from `SCALES` — the single copy every surface renders (§4) |
| `maybe_alert()` | Threshold/trend evaluation with hysteresis, cooldown, quiet hours |
| `notify()` | Per-platform notification — `osascript`, PowerShell toast, `notify-send`. Best-effort, never raises |
| `get_api_key()` | Resolution order, never echoes the value |
| `http_get()` / `http_post_json()` | The two request helpers. `X-API-Key` only when there is a key — urllib rejects a `None` header and that broke every keyless provider |
| `serve_forever()` | `127.0.0.1` static server with `no-store`, plus the JSON API — read-only over `GET`, and the settings `POST` routes behind the four-check chain in §5's threat table (loopback `Host` → `Origin` → JSON content type → per-process token) |
| `current()` / `history()` / `discover()` | The `Provider` contract each network implements. Adding a network is meant to be one class and nothing else (§1) |

---

## 7a. Testing

About 1,760 Python tests across 34 files, plus 46 Rust tests for the tray. `unittest`
only — no pytest, no fixtures library, nothing to install. Run everything with
`python3 tools/check.py`, which is what CI runs and what must be green before a push.

*(This section said "there is no committed test suite yet" for far longer than it was
true. A document that states something false is worse than one that is silent, because
the reader believes it. If you find another claim here that has rotted, fix it in the
same commit as whatever you came to do.)*

### The gates

`tools/check.py` reports all of them rather than stopping at the first failure, because
two failures found together are cheaper than two rounds. It prints how many it is about to
run rather than stating a number here — `--tz-sweep` and `--faults` add one each, and a
count written down is a count that goes stale.

| Gate | What it protects |
|---|---|
| tests | The suite |
| compile | Every shipped module byte-compiles |
| json | Config and Tauri manifests parse |
| page scripts | The pages' inline JavaScript parses (`node --check`) |
| policy | No shell scripts at the root, no manifests, **no user data in the repo** |
| secrets | No credentials file tracked, and no key shape pasted into a source file, fixture or JSON example. Read off `git ls-files`, and it never echoes what it matched |
| rust | The tray |
| coverage | A floor per module, measured on Linux/3.12 and recorded in `tools/coverage-floor.json` |

The coverage floor may not be lowered to make something pass. Neither may a test or a
contract be relaxed, narrowed or deleted for that purpose. A failing check is
information.

### Guards: block the effect, do not stub the route

Four modules under `tests/`, each written *after* the thing it prevents had already
happened to the maintainer:

| Guard | Prevents |
|---|---|
| `netguard` | A test reaching the internet |
| `homeguard` | A test touching the developer's real `~/.airo` |
| `browserguard` | A test opening a browser window — a suite once opened fifteen real tabs, because it stubbed `webbrowser.open` while the code shelled out to `/usr/bin/open` |
| `notifyguard` | A test putting a notification on somebody's screen — a 400 µg/m³ fixture once delivered "Air quality: Hazardous, AQI 1600" on an ordinary Tuesday |

The distinction in the heading is the whole lesson. Stubbing a library covers one route
into the effect; a fifth caller finds another. These block the effect, and
`test_contracts.py` enumerates which suites must install which guard, so a new suite
that forgets is caught by a check rather than by a person remembering.

Beyond the guards, `tools/check.py` hashes the real `~/.airo` before and after the suite
and **fails the run on any difference**. Five routes into it have been found and closed
one at a time; the fourth deleted three of the maintainer's backup archives, which were
not recoverable. The register of known shapes cannot be the last line, so this is.

### Fault injection: a test that has never failed is a claim nobody has checked

`tools/faultcheck.py` breaks the product on purpose and reports which tests notice. The
faults are committed under `tools/faults/*.json` and CI runs them on every push, so a
guard that stops guarding fails the build instead of waiting to be discovered.

It exists because fault injection lied three times in one week:

- the baseline was already red, so every fault "failed" identically;
- the restore restored the file and not the bytecode — Python invalidates a `.pyc` by
  mtime **and size**, and a same-size rewrite within the same second changes neither;
- faults were injected into the tests, where deleting an assertion cannot fail.

So it refuses to start against a red baseline, clears bytecode on the way in *and* out,
refuses a `find` that matches more than once, refuses an edit that leaves the syntax tree
identical (every comment-only or whitespace change), and separates **UNRUN** from
**GREEN** — "nothing caught it" and "nothing ran it" look the same in a report and call
for opposite responses.

A fault under `tests/` is admitted only when the file is *itself* a guard
(`*guard.py`, `test_contracts.py`) and says so with `"edits_a_guard": true`. A harness is
not a guard. Widening that rule to fit a fault you want is the thing it is there to stop.

### Contracts

`test_contracts.py` asserts things about the codebase rather than about a function:
which suites install which guards, that no shipped module resolves a home-relative path
at import time, that no fixture's result depends on the wall clock, that every risk in
the register cites a test that exists, that every CLI flag is read back or declared a
deliberate synonym for the default.

Each is written because the same mistake had appeared three or more times. Two rules
they follow, both learned expensively:

- **Enumerate, never list.** From `PROVIDERS`, from the stager's own manifest, from the
  parser's `add_argument` calls — never from a list typed by hand, which stops covering
  the moment somebody adds something. The register's own citation check had fallen three
  modules behind before this was applied to it.
- **Show the filter a known-bad sample.** Once every real case is marked or fixed,
  "found nothing" and "the filter is broken" are the same result. A fault inverting one
  of these filters went unnoticed on exactly that account.

### Journeys

`test_end_to_end.py` drives the real CLI with a real `argv` against an isolated install:
first run, upgrade, CSV migration, the macOS lifecycle, the commands somebody reaches
for when worried, a sensor going dark and coming back.

Journeys exist because of a failure mode this repository has produced **five times**: a
helper is fully tested while its call site passes it the wrong thing. The most recent
was `record_source_result`, whose unit tests proved the counter and the message worked
while nothing asserted what its `ok_now` argument was *derived from* — and the answer
was wrong, so a sensor was dark for two days in silence. Unit tests could not have found
it. A journey does.

### The pages

`test_page_render.py` executes a page's own inline script against a payload and asserts
on the rendered cells. Before it, the pages were run through `node --check` and nothing
else, so a page that *parsed* was a page that passed — and every claim about what a
reader sees rested on somebody having looked recently.

Node is a development tool, not a dependency: these skip when it is absent, the same
bargain the syntax gate already makes. CI has node on every runner.

Two properties of the harness are load-bearing, and both were learned by getting them
wrong:

- it reads `innerHTML` **or** `textContent`, because the page uses whichever suits the
  panel — reading only the first returned an empty string for nine of thirteen panels,
  which every `assertNotIn` passed;
- it awaits, because `drawInsideOutside()` fetches its own data.

Both faults made tests pass while reading nothing, which is why the file asserts on
*itself*: that a null field still renders the dash, and that the payload's own data
reaches the output. A harness that quietly renders nothing satisfies every negative
assertion in the file.

### Things that keep going wrong

Read this list before writing a test here.

- **A helper fully tested while its call site is gone or wrong.** Five times. Cover the
  call site, or write a journey.
- **A test that greps source text cannot tell a comment from code.** Three times in one
  week, including one that matched the comment explaining why the thing it forbids is
  avoided. Parse the AST.
- **A test that enumerates from the thing under test shrinks with it.** Narrowing a
  tuple narrowed the loop checking the tuple. Membership that is a claim about the world
  gets written down literally.
- **A guard that changes global state cannot be verified by a test that reads that state
  afterwards.** Capture the "before" outside the guard.
- **A fixture whose result depends on the wall clock.** Readings written at the top of
  the current hour are up to fifty-nine minutes old when fusion judges them, so three
  tests passed at five past and failed at five to. Its neighbour: an assertion on a row
  count that a legitimate backfill changes depending on where the hour boundary falls.
  Both are marked `clock-independent:` with a reason where the truncation is deliberate.
- **Asserting something adjacent to the property instead of the property.** The two most
  recent Windows-only failures were both this shape.
- **A green run on one platform.** CI runs macOS, Linux and Windows on two Python
  versions, and has repeatedly caught what a green local run did not — path separators,
  `os.name` mutation, `sys.stdlib_module_names` being 3.10+, and subprocess output
  decoded with the locale's encoding rather than UTF-8.
- **A test that skips itself at exactly the moment it matters.** The bundle tests looked
  for `bundle/macos/Airo.app`, and `cargo tauri build --bundles dmg` *deletes* that
  directory once it has packaged it — so the whole file skipped after the build worth
  checking, with a stated reason telling the reader to run the build they had just run.
  It now falls back to the `Airo.app` inside the image, which is the stronger artefact
  anyway because it is the copy a person receives, and `TestTheseTestsActuallyRan` fails
  if an image was built and the bundle tests resolved nothing to run against. Its
  neighbour: a per-test `hdiutil detach -force`. Attaching one image twice hands back the
  same device, so cleanup "of its own mount" unmounted the shared one half way through the
  run and sent every later class back to "no bundle built". One mount per image, detached
  once at the end.
- **A measuring tool that ignored its own flag.** `coverage run --dynamic-context=…` is
  not a flag of that subcommand; it errored quietly, and the conclusion drawn from the
  empty result — that the statements ran outside any test — was both wrong and stated more
  confidently than the evidence allowed. Re-measured through a config file it named a
  test. Retracted in place rather than quietly corrected.
- **A harness assertion the code under test catches.** The fake transport raised
  `AssertionError` for a request nobody queued, and all six providers' `except Exception`
  handlers caught it, logged a warning and carried on — so three tests made unasserted
  calls and passed. The guard raises a `BaseException` now, which no handler catches, and
  a test fails if it is ever changed back.
- **A dispatch test that only asserts something was printed.** A fall-through prints too:
  the first version passed with `uninstall-tray` routed to `status`, reporting success for
  a tray that was never removed. Every action the parser accepts is asserted to reach *its
  own* backend, enumerated from the parser rather than from a hand-written list.
- **A diagnostic checked for its exit code rather than for what it says.** The point of
  `--doctor` is telling somebody *what* is wrong, so each diagnosis is asserted: 401
  points at the key, 404 at a retired site id and explicitly not at the key, 429 says to
  wait, a world-readable key file is called out. A wrong message here sends someone to
  reissue a credential that was working.
---

## 8. Legal constraints that shape the code

PurpleAir's terms impose requirements the code must satisfy. See
[LICENSING.md](LICENSING.md) for the obligations. The full legal analysis is not published.

| Requirement | Where it's implemented |
|---|---|
| Attribution — "Powered by PurpleAir" plus a link (ToS §4.8) | README, dashboard footer, tray menu — all rendered from `latest.json`, never hard-coded |
| Health disclaimer, mandated wording (ToS §7.3) | README, dashboard footer |
| No redistribution of PurpleAir data (ToS §4.3) | `.gitignore` excludes `data/`; README warns against committing CSVs or attaching them to issues |
| Keys must not be shared or distributed | Key lives outside the repo; never logged; never embedded |
| Fair-use rate limits | Default 15-minute interval; config documents that below 10 minutes wastes points |

**Do not remove the attribution or disclaimer when forking** — substitute your own product
name. Two clauses matter if you build commercially: **§4.5** restricts MIT-licensed materials
in creating data derivatives, and **§4.4** grants PurpleAir a perpetual sublicensable licence
over derived models. Neither affects personal use.
