# Airo — conventions

The rules this project holds itself to, the traps it has already fallen into, and where
everything lives. Read this before changing anything; it is the shortest path to not
reintroducing a bug that has shipped once already.

Local, multi-source air quality monitoring for macOS, Windows and Linux. Polls every
configured source on an interval, stores readings in SQLite, fuses them into one honest
number with provenance, and alerts when air quality worsens.

New here? [CONTRIBUTING.md](CONTRIBUTING.md) is the front door — what to read, in what order,
and how to set up. This file is the rules themselves.

## If you are a coding agent

Five things, before anything else. Each is enforced, so the cost of skipping one is a
failed push or a repository that needs its history rewritten — not a note in review.

1. **Run `./tools/install-hooks.sh` once after cloning.** Hooks are per-clone and cannot
   be committed, so nothing installs it for you. It refuses user data *before* it
   becomes a commit somebody has to rewrite history to remove.
2. **Run `python3 tools/check.py` before every commit** — `--fast` while iterating. It
   reports every failure at once rather than the first, and it ends by naming what only
   CI can answer, which is the part worth reading.
3. **Never point a test at the real `~/.airo`.** Fixtures get a temp directory:
   `tests/homeguard.py` redirects the paths *and* `HOME`, and `netguard`, `browserguard`,
   `notifyguard` and `schedguard` block the internet, a browser window, a desktop
   notification and the logged-in session manager respectively. `tools/check.py` hashes
   the real install before and after the suite and fails the run if anything moved, and
   CI repeats that on every push. You will be caught. The last time this rule broke it
   destroyed three of the maintainer's backup archives.
4. **Every commit needs a `Signed-off-by` trailer matching its author.** CI checks every
   commit in a pull request. The project is dual licensed and that line is the grant —
   see [CLA.md](CLA.md). `git commit -s`, or `git rebase --signoff origin/main`.
5. **Write synthetic fixtures.** `Riverside`, `Northfield`, `pa-1`, `oaq-1`, and the
   shifted coordinate frame the tests already use — never a real place, sensor id,
   coordinate or key. The tests scan by *shape*, so a realistic fixture fails without
   anybody having listed it. See [RISKS.md](RISKS.md): this is the most likely way a
   pull request goes wrong here, because a realistic fixture is easier to write than a
   synthetic one.

---

## Hard rules

1. **The Python side has no *runtime* dependencies.** Standard library only, in Python and
   in shell. CI enforces this with an AST check over every shipped module and every
   `tools/` script, read off disk rather than listed, and fails if a dependency manifest
   appears. The Rust tray in `tray/` is the one deliberate exception — a separate, optional
   binary.

   *Amended 4 Aug 2026: "runtime" was implicit and is now stated.* The guarantee this rule
   exists for is that the installed app runs on the bare CPython it ships with — no package
   manager, no site-packages — so a shipped module importing a package fails on first launch
   on the user's machine and never reproduces on a developer's. A **development** tool has
   nothing to do with that: CI already installs a Rust toolchain and the Tauri CLI, and
   `coverage.py` is the same kind of thing. `tools/check.py` degrades with a clear message
   when it is absent rather than silently skipping.

   The line is enforced, not merely stated: `test_contracts.py::TestTheZeroDependencyRuleHolds`
   fails if any shipped module or tool imports outside the standard library, and if a
   manifest appears at the root.
2. **Never log, print or commit an API key.** Keys live in `~/.airo/<provider>.key` (mode 600),
   deliberately outside the repo.
2a. **Nothing of the user's lives in the repo.** Settings at `~/.airo/config.json`, readings at
   `~/.airo/data/`, keys at `~/.airo/<provider>.key`. Shell scripts must ask
   `python3 -c "import poller;print(poller.DATA)"` rather than assuming `$PROJECT/data` —
   hardcoding it created a stray empty database and reported zero rows.
2b. **No real location, sensor id or coordinates in the repo — ever.** Settings live in
   `~/.airo/config.json`, written by `setup.py`; the repo ships `config.example.json` with
   empty values. `config.json` is gitignored and CI fails if it is tracked. Test fixtures use
   a shifted synthetic coordinate frame for the same reason.
3. **User data never enters the repository.** Not `data/`, not `data.migrated-*/`, not a
   database, log, readings CSV, backup archive, real config or key — *anywhere*, under any
   name. Enforced by `.gitignore` shape-matching, a pre-commit hook
   (`./tools/install-hooks.sh`), and CI. The narrow version of this rule already failed once:
   `data/` did not match `data.migrated-<timestamp>/`, and 16,995 rows of location history
   were committed. **Match by shape, never by one known path.** See ARCHITECTURE §2.5d.
4. **Don't remove attribution or the health disclaimer** from README, dashboard or tray. The
   provider terms that require them, and what else those terms forbid, are in
   [LICENSING.md](LICENSING.md). Enforced by `test_obligations.py`.
5. **The poller must never lose data.** Any change to `store.insert_readings()`,
   `backfill_source()` or `do_poll()` needs a test proving gaps are still detected and
   repaired. See `tests/test_store.py::TestGapRepair`.
5a. **Never silently discard a reading.** Faults and uncorroborated readings are *flagged and
   shown*, never hidden — if there is a fire next door that is genuinely the air being
   breathed. See ARCHITECTURE §2.5c.
6. **Raw µg/m³ is canonical.** Never store a derived AQI as the source of truth — the same air
   gives very different index values on different national scales.
7. **No air-quality logic in the tray.** It reads `~/.airo/data/latest.json` and renders it. A
   threshold or band boundary implemented there is a second copy of a health-relevant decision,
   free to drift out of step with the dashboard. Put it in Python.

## Traps that have bitten before

- **`data_dir` is configurable, which is a way to abandon a database.** Point it at a path
  that does not exist yet — a typo, an unmounted drive, a synced folder that has not appeared —
  and the poller starts a blank database beside the full one, forever. Nothing is deleted,
  which is why it is not a data-loss bug in the strict sense and worse in practice. The active
  directory is recorded in `~/.airo/data-location` so `other_databases()` can name the
  abandoned path; `--status`, `--where` and `--doctor` all say so.
- **Government feeds signal "offline" with an out-of-range sentinel, not a null.** Queensland
  sends -9999. Stored as a reading it becomes AQI -39996 on the Australian scale, which falls
  below the first breakpoint and renders as **"Very good"** — the most reassuring label there
  is, for air nobody measured. Rejected at three layers now (`clean_measures`,
  `insert_readings`, `aqi_for`); `--repair` corrects databases written before that.
- **PurpleAir nests rolling averages under `stats`**, not at the top level. Reading the top
  level silently returns `None`. Use the `field()` helper in `PurpleAirProvider.current()`.
  ARCHITECTURE §3.1.
- **The QLD API silently ignores unknown query parameters.** `from_date`/`to_date` are wrong —
  it wants `start_date`/`end_date` — and it returns the most recent 1000 rows instead of the
  window you asked for. Wrong data, no error. Always verify a date filter actually filtered.
- **NSW stamps hours 1..24, where 24 means midnight *ending* that date** — i.e. 00:00 the next
  day. Using it as an hour value raises; clamping it to 23 silently misplaces every midnight
  reading by an hour. See `NswProvider._observed_utc()`.
- **A platform fallback that returns a constant is a feature that silently does nothing.**
  Four separate Windows-only failures this project: locale-encoded reads, `chmod` doing
  nothing, unclosed SQLite handles blocking a temp-dir delete, and a process-liveness stub
  returning `false` for every non-unix target — which made the tray's single-instance guard a
  no-op there, because a lock whose owner never looks alive is always treated as stale. Each
  read as safe and was invisible on macOS. Anything touching the OS — file modes, encodings,
  handles, process queries — needs a real per-platform implementation, or a test asserting the
  property so the gap fails loudly rather than degrading quietly.
- **Windows has no POSIX file modes.** `os.chmod(0o600)` only toggles read-only there, so
  tests asserting modes must skip on `os.name == "nt"`, and `Path.home()` reads `USERPROFILE`
  rather than `HOME` — a test setting only one runs against the real home directory on the
  other platform. The security consequence is documented in SECURITY.md rather than hidden.
- **`SO_REUSEADDR` means different things per platform.** On Windows it permits binding a port
  another socket is *actively* using, so a free-port probe with it set returns an occupied
  port. Probe without it (and with `SO_EXCLUSIVEADDRUSE` where available); a POSIX port in
  TIME_WAIT then looks busy and gets skipped, which is the safe direction.
- **Python writes JSON that is not JSON.** `json.dumps` emits `Infinity` and `NaN` as bare
  literals by default, and its own parser reads them back, so nothing looks wrong from
  inside Python. They are not valid JSON and every other language rejects the *whole file*.
  A band ceiling of infinity in `latest.json` made the Rust tray show "No reading yet"
  beside 17,000 readings. `write_json_atomic()` passes `allow_nan=False` so this raises
  instead; use it for anything another program reads.
- **A long-running `--serve` serves the code it started with.** Change a route or the HTML
  and the running process keeps answering with the old one — a 404 on a page you just
  added looks like the page is broken. Restart it after changing anything served.
- **The tray binary is not rebuilt by anything automatic.** Editing `tray/src/*.rs` changes
  nothing until `cargo build --release` and `scheduler.py install-tray`. A stale binary
  shows the old menu and the old behaviour, which reads as the change never having worked.
- **Console encoding is not universal.** Windows defaults to cp1252, which cannot encode a
  tick; printing one raises and kills the command. Use `poller.TICK` / `CROSS` / `WARN`, which
  degrade to ASCII.
- **`http_get` must not send `X-API-Key: None`.** urllib rejects it outright, which breaks every
  keyless provider.
- **Gap thresholds must scale with the provider's cadence.** A fixed 25 minutes is right for
  PurpleAir's 10-minute average and fires on *every poll* against an hourly feed.
- **`toISOString()` corrupts day-bucketing.** It converts to UTC, so in UTC+10 a 9am reading
  lands on the previous date. Always use `localDateKey()`. ARCHITECTURE §3.2.
- **The dashboard chart renderer is local canvas, not Chart.js.** ~330 lines at the top of the
  inline script, supporting one line chart and one bar chart. Don't reintroduce a CDN: it
  leaked the viewer's IP, made a local tool need the internet to draw local data, and
  `test_contracts.py` fails on any external subresource. The x-axis stays a linear scale over
  epoch-ms with custom tick generation — the renderer has no date handling, and that tick
  generation is what keeps day steps on calendar boundaries across DST. ARCHITECTURE §3.3.
- **Every dashboard render step runs in its own `try/catch`.** One failure previously blanked
  four unrelated panels.
- **Band colour must match the displayed rounded value** — `bandFor(Math.round(v))`.
  ARCHITECTURE §3.4.
- **A registered launchd agent is not necessarily *this* checkout's.** The label is fixed, so
  with two clones — or a moved folder — `launchctl` reports the other install as healthy while
  this one never runs. `poller.py --doctor` compares the agent's `WorkingDirectory` against the project
  and says so. Same failure family as the next one.
- **A stale `--serve` process from another directory serves old data and old HTML**, which
  looks exactly like "the agent died". `serve_forever()` now refuses to start on a busy port.
- **Date logic assumes no DST.** Correct in Queensland, wrong elsewhere. See ROADMAP #5.

## Layout

The file map — every shipped module, the pages, `tools/`, `tray/`, `tests/` and what lives
under `~/.airo/` — is **ARCHITECTURE §6**, and only there. A second copy went stale twice.

## Commands

```bash
python3 poller.py --once --status --list-sources --backfill N --export --serve
python3 poller.py --open settings        # the settings page, server started if needed
python3 poller.py --verify --repair [--dry-run]   # integrity, and correcting stored sentinels
python3 scheduler.py install|uninstall|status|start|stop|restart|install-tray|uninstall-tray
python3 analyse.py evening|agreement|correlate
python3 setup.py                   # reconfigure location and sources
python3 poller.py --status          # macOS
./tray/target/release/airo-tray --print-menu   # what the tray menu says
```

Verification before any commit — **one command**:

```bash
python3 tools/check.py            # every gate CI runs, all reported at once
python3 tools/check.py --fast     # skip the Rust build while iterating
python3 tools/check.py --tz-sweep # the date-sensitive suites in four awkward zones
```

It reports every failure rather than stopping at the first, because CI stops at
the first and each round costs four minutes. It also states what it *cannot*
check: this machine is one platform, and every expensive failure in this
project's recent history was platform-specific and green locally.

The individual commands, if you want them separately:

```bash
python3 -m unittest discover -s tests -v
python3 -m unittest tests.test_store -k TestGapRepair   # one module, one class or test
# Every shipped module and every tool, globbed off disk rather than listed --
# which is what tools/check.py does. A hand-written list here went three modules
# stale (units.py, weather.py, backup.py) and read as complete the whole time.
python3 -m py_compile *.py tools/*.py
cd tray && cargo test && cd ..     # if Rust is installed
python3 -c "import json; json.load(open('config.example.json')); json.load(open('tray/tauri.conf.json'))"
# extract and check the dashboard JS (the settings page too)
python3 -c "
import re,pathlib
h=pathlib.Path('dashboard.html').read_text()
js=re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>',h,re.S)[-1]
pathlib.Path('/tmp/d.js').write_text(js)" && node --check /tmp/d.js
```

## Architecture decisions worth not undoing

- **SQLite is the store; CSV is the export.** Reverses the original CSV decision — the
  reasoning and the benchmarks are in ARCHITECTURE §2.5a. Don't revert without reading it.
- **Schedulers use intervals, not resident daemons.** ~2s every 15 min, then exit. A scheduler
  reporting no PID between polls is *correct*.
- **The `.app` bundle exists purely so macOS reports "Airo" rather than "python3".** Written
  by the old shell installer; nothing rebuilds it since that script was deleted, so a fresh
  checkout gets "python3" again. `scheduler.py install` uses a leftover one if it finds it.
  Gitignored. Not to be confused with the Tauri bundle of the same name under `tray/target/`,
  which is what ships. See ARCHITECTURE §2.2.
- **The dashboard server runs on demand only**, bound to `127.0.0.1`. `--open` starts one
  if none is running and resolves the port it actually got — the tray must never build a
  URL of its own, which it did as a literal until the port became configurable.
- **Settings are a served page, not a terminal wizard.** `settings.html` is rendered by
  `poller.py`, which substitutes a per-process token so writes cannot come from another
  origin. `setup.py` still works and shares one validator with the page, so the two front
  ends cannot disagree about what a valid setting is.
- **`nearest` is the default fusion rule** because the tool describes *local* air. On the
  reference install the two sources differ 4×; picking wrong shows "Very good" to someone
  breathing "Fair" air.

## State of play

- v0.5 added: SQLite store, multi-source fusion, four providers, configurable scales,
  cross-platform scheduling, AGPL relicence, Tauri tray. Since then: sentinel rejection,
  a local chart renderer replacing the CDN, configurable `data_dir` with orphan
  detection, `--repair`, forecast guardrails, and enumerating contract tests. How big the
  suite is now is stated once, in ARCHITECTURE §7a, and held to a floor by
  `test_contracts.py::TestDocsMatchTheCode` — so adding tests needs no documentation edit
  and removing them fails loudly.
- The tray builds and runs; installed via `python3 scheduler.py install-tray`. CI compiles and
  tests it on all three platforms and rejects numeric air-quality comparisons in Rust.
- Roadmap priorities: CLA before accepting external PRs (#3a), screenshots (#3b), then the
  weather forecast (#9). A hosted service is explicitly not planned — see ROADMAP "Explicitly not doing".
- [RESEARCH.md](RESEARCH.md) holds the evidence base with citations — check it before making
  any empirical claim about air quality behaviour.
- **Licensing is AGPL-3.0 + commercial.** [LICENSING.md](LICENSING.md) covers the data terms,
  which differ per source and are not the same as the code licence. PurpleAir §4.5 tension is
  unresolved and got sharper, not softer, with the AGPL move — the obligations are in
  LICENSING.md; the review itself is not published.

## Style

Python: PEP 8, 4 spaces, ~100 col. Shell: `set -uo pipefail`, quote everything, absolute paths
in anything a scheduler touches. JS: no framework, no build. Rust: keep it dumb — it renders,
it does not decide. Comment *why*, not *what* — the tricky parts here are all "why".

Commits: imperative mood, explain the reasoning, not just the change.
