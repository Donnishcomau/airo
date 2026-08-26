# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- **Every source file states its own licence.** Two SPDX lines — the copyright holder and
  `AGPL-3.0-or-later` — now head every tracked `.py` and `.rs`. LICENSE has always quoted the
  FSF's guidance to attach a notice to the start of each file; only the root LICENSE actually
  carried one, so a module pasted into a gist arrived stating nothing about what it is.
  `test_contracts.py::TestEverySourceFileCarriesItsLicence` enumerates the tree by extension
  off `git ls-files` rather than from a list, and checks `tray/Cargo.toml` and
  `tauri.conf.json` still name the same licence, so one legal fact has one source.

### Fixed

- **The install instructions named a file that has never existed.** README told a first-time
  installer to download `Airo.dmg`. Every release asset carries its version and architecture
  in the name, so there has never been a file called that — the very first step of the very
  first install, for the readers least able to work around it.
- **The data-quality issue template applies its label again.** The template asked for
  `data-quality` and the label had never been created, so filing one silently dropped it.

### Changed

- CI declares `permissions: contents: read`. Nothing in that workflow publishes anything;
  `release.yml`, which does, already declared what it needs.
- ROADMAP's "Publish from a fresh history" moved out of `Open`. The repository is public and
  its history begins at one parentless commit — the item was describing a project that no
  longer exists, which is the failure ROADMAP's own preamble warns about.
- ROADMAP's finished table cites the release each item shipped in. Five rows said
  "CHANGELOG Unreleased", which stopped being true when 0.6.0 was cut and started naming
  whatever landed next. `test_contracts.py::TestTheRoadmapCitesWhereWorkLanded` refuses a
  finished item that cites a section which moves, and refuses a version the CHANGELOG has
  never had — checked at the release, which is the one moment the drift enters.

---

## [0.6.1] — 2026-08-26

### Fixed

- **The Linux AppImage builds.** The bundled Python runtime shipped Tk (`_tkinter`, linked
  against `libtcl9.0.so`), and the AppImage bundler walks every shared object in the tree,
  could not resolve that Tcl library, and aborted — so 0.6.0 shipped a `.deb` only. Airo has
  no Python GUI (a web dashboard and a Rust tray), so `fetch_runtime.py` now removes Tk from
  the runtime right after extraction, by precise name so `sqlite3`, `ssl` and the interpreter
  are untouched. Every platform's bundle is a little smaller as a result, and Linux now ships
  both a `.deb` and an `.AppImage`.

---

## [0.6.0] — 2026-08-25

### Fixed

- **Typing a place name into the first-run wizard works.** `setup.py` and `geocode()`
  disagreed about the shape of a search result — the wizard read Nominatim's raw keys
  (`display_name`, `lat`, `lon`) from results already normalised to
  `name`/`label`/`latitude`/`longitude`, so every candidate rendered as `?` and picking one
  raised `KeyError`. There is one shape now, and a test drives the place-name path end to end
  so the two cannot disagree again.

### Changed

- **The ventilation panel's default wording fits any home.** The window and purifier advice
  is written in terms any dwelling can read ("around sunset", "a fan on low helps mix the
  room") rather than around one house's sunset time and fittings. The hours behind it are a
  configurable prior, not a claim about your suburb.

### Added

- **Weather, and a six-hour outlook that can decline to speak.** `weather.py` captures
  hourly wind, temperature, humidity and pressure from **Open-Meteo** — keyless, worldwide,
  and carrying a historical archive, which is what makes backfilling against readings
  already collected possible. BOM was rejected for tying the flagship feature to one
  country and to an API with no documented public contract. `--backfill-weather` reaches
  back to the oldest reading by default, because a correlation needs history on both sides.
  `analyse.py correlate` then reproduces the finding from the user's *own* record — mean
  PM2.5 by wind band, the correlations with wind, temperature and humidity, and which way
  the wind was blowing on the twenty worst hours. It refuses below 72 paired hours and says
  how many it has, every coefficient carries its n including the summary's, and it can
  answer **no**: "your record does not show a clear weather signature" is a result, proven
  by a test that feeds it the opposite record and asserts it says the opposite. `--forecast`
  scores the forward hours against the user's own wind bands rather than anyone else's
  reference figures, stays silent until 30 predictions have been verified against what
  actually happened, and is scored against a persistence baseline so autocorrelation is not
  mistaken for skill.
- **One installer per platform.** A `.dmg` for Apple Silicon, plus `.msi`, `.deb` and
  `.AppImage`, each checksummed and built on every tag. The app carries its own interpreter,
  pinned by version and verified by SHA-256 in `tools/fetch_runtime.py` before anything is
  unpacked, so `python3: command not found` cannot end an install — a deliberate reversal of
  "never bundle a runtime", argued in ARCHITECTURE §4a. **Windows and Linux are built and
  nobody has run one.** They are labelled untested everywhere a user can see them rather
  than leaving somebody to find out, and macOS is not signed or notarised yet either.
- **A contributor licence agreement.** [CLA.md](CLA.md) states the terms — contributors keep
  their copyright and grant a perpetual, **sublicensable** licence, the specific word that
  makes dual licensing possible — and `git commit -s` records agreement, the lightest
  mechanism that leaves a durable per-commit record. The `sign-off` CI job checks every
  non-merge commit in a pull request, rejects one signed by anybody other than its author,
  and prints the `git rebase --signoff` that fixes it. The job fires only on pull requests, which is
  where contributions arrive; it was verified by a pull request that failed on an unsigned
  commit and passed once it was signed. The text has not been reviewed by a lawyer
  and says so at the top.
- **Screenshots, built from synthetic data.** `tools/demo.py` produces a populated install
  from invented readings, and the three images in the README come from it — a screenshot of
  a real install publishes a home location, chosen sensors and a year of when somebody was
  in. The tray is **printed rather than photographed**: the real output of
  `airo-tray --print-menu`, which can be searched, read by a screen reader and checked
  against what the program actually prints, none of which a picture allows.
- **Settings are a page now, not a terminal wizard.** Every configuration path used to end
  in a Terminal window spawned from the tray, which is intimidating, macOS-shaped and
  broken outright if the checkout has moved. `settings.html` is served locally and covers
  location, sources, keys, alerts, data and backup. `setup.py` still works and shares one
  validator with the page — two front ends onto one file cannot be allowed to disagree
  about what a valid setting is.
- **Export and import from the UI**, to any folder on disk including an external drive,
  with a native folder chooser on all three platforms. The archive is verified by checksum
  before it is called a backup, and an import shows what is inside — how many readings,
  which sources, whether it carries credentials — before replacing anything. Keys are
  excluded unless asked for, because a backup ends up on cloud drives and USB sticks in a
  way a settings file never does.
- **A per-process token, an `Origin` check, a loopback `Host` check and a required JSON
  content type** on every mutating request. Binding to `127.0.0.1` keeps other machines
  out, not other pages: without these, an ordinary web page could repoint someone's
  monitoring or move `data_dir` and quietly abandon years of readings.
- **The dashboard loads nothing from a third party.** Chart.js came from a CDN, which handed
  that CDN the IP of a machine viewing its own home air quality, gave a third party arbitrary
  code in a page rendering those coordinates, and meant a *local* logger could not draw local
  data offline. Replaced by a local canvas renderer implementing only the surface the two
  charts use.
- **`data_dir` in the config**, so readings can live on another volume without teaching every
  scheduler and shell an environment variable. Setup asks, and probes the path for writability
  at the moment it is chosen. The active directory is recorded, so pointing `data_dir`
  somewhere new reports the abandoned database by path and row count rather than silently
  starting an empty one.
- **`--repair` and `--verify`** for the store, and archive verification before backup rotation
  deletes anything — `create()` returning 0 only means it believed it succeeded.
- **Contract tests that enumerate.** `tests/test_contracts.py` discovers surfaces, modules and
  providers from disk rather than from a list, so a file that does not exist yet is already in
  scope. Documentation is included: documented flags must exist, stated test counts must be
  true, and the architecture layout table must list every shipped module.
- **`airo-tray --print-menu`** prints the tray readout as text, so the menu can be checked on a
  headless machine or anywhere screen recording is unavailable.
- **A risk register whose rows are mechanisms, not notes.** Every risk across
  legal, privacy, data loss, provider dependency, key handling, forecast
  liability and sustainability names the code or test that fails when its
  mitigation is undone — and a test checks the register itself, so a row citing
  a class or function that has since been renamed fails the build rather than
  quietly reporting coverage that is not there.
- **`forecast.py` — guardrails before the forecast.** ROADMAP #9 Phase C does
  not exist yet; both constraints on it do. `phrase()` refuses certainty
  wording, requires a stated basis, and refuses to speak at all until skill is
  measured over at least 30 verified outcomes against a persistence baseline
  (ACL s4 puts the burden of showing reasonable grounds on the maker).
  `training_sources()` excludes PurpleAir by construction, because ToS 4.4
  would otherwise grant them a perpetual sublicensable licence over any model
  trained on their data — and it explains the exclusion instead of silently
  dropping the user's nearest sensor.
- **Real credential protection on Windows.** `os.chmod(0o600)` only toggles the read-only
  attribute there, so key files were effectively unprotected while appearing otherwise.
  `poller.secure_path()` now calls `icacls` — which ships with Windows, so no dependency — to
  drop inheritance and grant only the current account. It reports whether it succeeded, and
  callers warn instead of continuing silently. `path_is_restricted()` reads the state back
  rather than assuming the call worked.
- **Port collisions are handled by cause.** Another Airo already serving means the user is told
  where it is and refused a second instance, because two servers make "which am I looking at?"
  unanswerable. An unrelated program holding the port means moving to the next free one and
  saying so — refusing to open a dashboard because something else took 8787 is a bad trade.
- **Provider coverage.** Each network declares where it actually has instruments, and setup
  orders and defaults by whether a network can reach your location. Found by simulating a user
  in Hobart: setup recommended the Queensland and NSW feeds — the only keyless ones — and the
  search widened to 200 km finding nothing, with no explanation. Now the two worldwide networks
  are recommended instead, the state feeds are marked "not your area", and a search that cannot
  succeed says which networks would cover you and offers to add them.
- **Data retention.** `retention_days` in config, asked during setup, defaulting to keep
  everything. `--prune` applies it, `--dry-run` reports first, and pruning runs after a poll
  only when a finite window is configured. `--where` reports size and policy.
- **Readings moved to `~/.airo/data/`**, out of the git checkout. A database inside a working
  tree is lost to a re-clone, a moved folder or a wiped untracked directory — and unlike
  config, it is years of history that cannot be regenerated. Existing installs keep working
  from `./data` until `python3 poller.py --migrate-data` is run, which copies, verifies the
  row count matches, and only then retires the original under a timestamped name.
  `--where` prints the resolved paths.
- **`backup.py`** — `create`, `inspect`, `restore`. One portable archive with configuration and
  every reading. API keys are excluded unless `--include-keys`, because a backup ends up on
  cloud drives and USB sticks in a way a config file never does; `inspect` always states
  whether an archive carries credentials. The database is snapshotted through SQLite's backup
  API rather than copied, so an archive taken mid-poll is coherent. Restore refuses to
  overwrite without `--force`, keeps a timestamped copy of what it replaces, and rejects
  archives containing traversal or absolute paths.
- **Guided setup** (`setup.py`): optional IP-based location detection (free, keyless, always
  confirmed rather than assumed), network selection *before* any account is requested,
  location-based site discovery, and full preference capture — fusion rule, poll interval,
  alert threshold in µg/m³, quiet hours, dashboard, history depth.
- **Provider accuracy tiers** (`reference` / `indicative` / `consumer`) with a plain-language
  caveat each. Setup suggests pairing the nearest *reference* monitor with the nearest
  *consumer* sensor rather than the two closest, because closest and most accurate are
  different questions and usually different instruments.
- **NSW Government provider** — hourly, CC BY 4.0, **no account needed**. Two keyless
  government networks now ship, so Airo is usable the moment it is cloned.
- Account setup opens the provider's registration page in a browser on request.
- **A user profile**, and ongoing account management. `setup.py --keys` reviews every network
  and sets up whatever is missing; `--prefs` and `--profile` allow targeted edits without
  redoing the wizard. Networks not yet in use are surfaced with a signup link in
  `poller.py --status`, in a dashboard panel, and in the tray's "Add a source" menu — a
  network buried in a setup command run once is a network nobody discovers.

### Changed

- Planning split: `ROADMAP.md` keeps features, priorities and the full risk register; the legal
  analysis and commercial plan move to private planning notes, outside the repository. The obligations arising from
  both remain public in `LICENSING.md` and remain enforced.

### Fixed

- **A forecast that could never be scored, and one that could be scored twice.** The
  prediction ledger sliced its hour key out of the raw timestamp string while the
  has-it-happened check parsed the same value properly, so an entry carrying a real offset
  asked the database for hour 10 when the reading was stored under hour 00 — it matched
  nothing, stayed pending, and could never be verified. Dedup compared raw strings as well,
  so an entry written before canonicalisation (`…Z`) and a promise made after it
  (`…+00:00`) were two entries for one hour: scored twice, inflating the skill figure that
  decides whether the outlook may speak at all, by exactly the rows an upgrade straddled.
  One parse feeds both now, and the timestamp contract walks the data directory for a `when`
  key at any depth rather than stopping at the database boundary.
- **A backfilled temperature recorded no unit at all.** `capture_reading()` normalised on the
  way in; `backfill_source()` copied the provider's number through and marked nothing — worse
  than storing `F`, because a migration that repairs a row marked `F` has nothing to key on
  when the mark is absent. A PurpleAir backfill therefore stored Fahrenheit permanently in a
  column documented as Celsius, and nothing could find it. Fixed at the writer, repaired by a
  migration keyed on the *missing* unit, and each provider now declares its own unit so no
  call site decides it from a name.
- **Alerting did nothing at all on Linux and Windows.** `notify()` shelled straight to
  `osascript`, raised `FileNotFoundError`, caught it, logged a warning and returned False —
  a headline feature, enabled by default, failing silently on two of the three platforms the
  installers target, and saying so only in a log nobody reads. Commands are built per
  platform by a pure function, so every platform's can be inspected from any platform; the
  message travels as data on all three, because it carries a site name that came out of a
  provider's JSON; and `--doctor` reports whether an alert can reach the screen.
- **The tray opened a hardcoded URL.** It held `http://127.0.0.1:8787/dashboard.html` as a
  literal, but `serve_port` is configurable *and* the server deliberately moves to the next
  free port when an unrelated program holds 8787 — so the Dashboard item could open a dead
  page, or a stranger's page. `poller.py --open` resolves it.
- **One unreadable timestamp blanked every chart.** A single unparseable `observed_utc`
  raised inside the series endpoint, which returned 500 for the whole request. The row is
  now skipped and counted, so the dashboard can say how many readings could not be placed.
- **Queensland timestamps meant a different instant per reader.** The feed publishes local
  time with no offset, and a naive datetime converted to UTC is read in the *machine's*
  zone — right in Brisbane, wrong by the reader's offset anywhere else.
- **A documented PurpleAir fallback did not exist.** ARCHITECTURE §3.1 has described
  falling back to the instantaneous value with `headline_is_fallback: true` since v0.4; it
  was never implemented, so a sensor publishing no 10-minute average reported nothing.
- **A lone source corroborated itself.** One reading was its own peer, so every
  single-source install reported "in line with nearby sources" while having none.
- **Exports could credit nobody.** Provider terms lived in a table keyed by slug, so a
  network missing from it exported an empty attribution line under "Licence unknown" —
  which for a CC BY feed omits what the licence requires.
- **A feed sentinel could render as "Very good"** (health-relevant). Queensland reports
  `-9999` when a station is offline. Stored as a concentration it became AQI −39,996 on the
  Australian scale, which falls below the first breakpoint, so the tool displayed the most
  reassuring label it has for air nobody measured. Reachable from a default setup: two of
  the nearest stations report the sentinel. Rejected at three independent
  layers — the provider boundary, ingest, and the scale conversion — each with a test that
  fails when only that layer is removed. `--repair` corrects databases written before the
  fix, clearing to NULL and re-asking the provider for the affected window without
  deleting a row.
- **Setup recommended stations that publish nothing.** It picked the nearest monitor per
  accuracy tier by distance alone, so a clean install at a test location chose the nearest
  station, which reports no PM2.5 — the first poll returned "every source failed". Setup now
  probes the nearest sites and excludes non-reporting ones from its suggestion, listing them
  marked so a manual pick is informed.
- **`--prune --dry-run` had never worked.** Documented twice in the README as the way to
  preview a destructive delete, and an argparse error for its whole life, because `--dry-run`
  sat in the mutually exclusive group with the modes it modifies.
- **Zero days of history meant seven.** `timedelta(days=days or 7)` turned an explicit 0 into
  the default.
- **The evening analysis could not work on an hourly feed.** It required 12 samples per
  bucket — correct as "2 hours at 10-minute resolution" — but the evening window is only 10
  hours long, so no hourly government source could ever satisfy it. Every user on a keyless
  government feed saw "—" permanently. Now counts distinct hours covered.
- **The dashboard footer credited PurpleAir to installs that do not use it**, burying the CC BY
  notice they do owe. Rendered from `latest.json` now. The guard that was supposed to catch
  this missed it because the markup split the literal across an `<a>` tag.
- **Two trays could run at once**, both polling and writing, with no way to tell which a click
  reached. The tray claims a pid lock and refuses to start alongside a live incumbent; a lock
  naming a dead process is reclaimed so a crash cannot lock the user out.
- **Near-empty installs rendered badly.** A single reading drew no chart at all, and what did
  draw was clipped at the axis. Fixed with the first-run state in mind, since that is every
  user's first impression.
- `setup.recommend()` assumed the caller had sorted by distance; an unsorted list silently
  suggested the wrong sites. It sorts internally now.

### Removed

- **The SwiftBar and Übersicht plugins.** Three menu-bar implementations meant every
  feature was written three times and drifted twice — SwiftBar hard-coded
  `Powered by PurpleAir`, so a Queensland-only user was attributed to a network they
  do not use and never saw the CC BY notice they do owe. Both were macOS-only. The
  Tauri tray replaces them on macOS, Windows and Linux with full control parity, and
  renders attributions from `latest.json` rather than a literal. A test now fails if a
  second widget implementation appears.

See [ROADMAP.md](ROADMAP.md).

---

## 0.5.0 — 2026-07-31

From a single-sensor logger to a multi-source platform. This is a large release with two
breaking changes and one reversed architecture decision.

### Added
- **Multiple sources per location.** `sources` is now a list. A hyperlocal consumer sensor and
  a regulatory monitor cross-check each other, which is the point — on the reference install
  they routinely differ by 4x.
- **Three providers.** `purpleair` (10-min, hyperlocal, BYO key), `qld` (Queensland Government
  regulatory network, hourly, **no key**, CC BY 4.0), `openaq` (global aggregator covering
  AU/US/CA/ZA/UK/DE/FR/IE/PH, hourly, BYO key). Adding a country is one class in `poller.py`.
- **Fusion with provenance** (`fusion.py`). Four user-selectable rules — `nearest` (default),
  `freshest`, `all`, `blend` — each reporting which instrument produced the number, how far
  away it is and how old. Staleness is judged against each source's *own* cadence, so 40
  minutes of silence is an outage for a 10-minute sensor and normal for an hourly feed.
- **False-positive detection** (ARCHITECTURE §2.5c). Three independent checks: PurpleAir's two
  laser channels against each other (an instrument fault looks nothing like bad air), each
  source against its neighbours, and each source against *its own history at this hour over 90
  days*. A valley sensor that always reads 3x after sunset is reported as `typical_for_site`;
  the same sensor reading 11x for the first time is `uncorroborated`. Flagged readings are
  always shown, never suppressed.
- **Configurable AQI scale** — `au`, `us_epa` (2024 revision) or `raw` µg/m³ (ROADMAP #4).
- **Cross-platform scheduling** (`scheduler.py`): launchd, `systemd --user` timers and Windows
  Task Scheduler behind one interface (ROADMAP #14).
- **Tauri tray** for macOS, Windows and Linux in `tray/`, built and tested (9 Rust tests,
  including that a real `latest.json` from the poller deserialises — the contract between the
  Python and Rust halves). Severity is carried by the glyph because no cross-platform tray API
  can colour its title. Install with `python3 scheduler.py install-tray`; it starts at login and
  is restarted if it crashes.
- **Test suite** — 103 tests, no dependencies (ROADMAP #1). Includes dashboard date handling
  exercised under `Australia/Sydney`, `America/New_York` and `Australia/Brisbane`.
- **CI across three operating systems** and two Python versions, plus an AST check that the
  Python side imports only the standard library, and guards against committing data or keys.
- `--list-sources`, `--export`, `--migrate-csv`; JSON API at `/api/latest`, `/api/sources`,
  `/api/series`.
- `scheduler.py install-tray` / `uninstall-tray`, using each platform's own login mechanism.
- `~/.airo` symlink verification in `poller.py --doctor`.

### Changed
- **BREAKING: SQLite replaces CSV as the store**, reversing ARCHITECTURE §2.5. The reasoning
  and benchmarks are documented in §2.5a rather than changed quietly: multi-source needs
  joins and time bucketing, and the Rust tray reading the same data would otherwise require
  the fusion rule implemented twice in two languages. `--export` writes per-source CSV,
  round-trip tested, so the preservation guarantee stands. Existing `readings.csv` files are
  imported by `--migrate-csv` (idempotent; the original file is left untouched).
- **BREAKING: relicensed MIT → AGPL-3.0-or-later**, with a commercial licence alongside. See
  [LICENSING.md](LICENSING.md). Note this *sharpens* the PurpleAir §4.5 tension rather than
  resolving it.
- Raw µg/m³ is now canonical everywhere; the AQI column is derived at presentation time.
- `latest.json` uses `aqi`/`band`/`averages_aqi`. The `au_*` keys remain as deprecated
  aliases for one release so a dashboard cached in a browser keeps rendering.
- API keys are per provider: `~/.airo/<provider>.key` or `$PURPLEAIR_API_KEY` /
  `$OPENAQ_API_KEY`. The old `~/.airo/apikey` still works for PurpleAir.
- The dashboard reads the JSON API instead of re-parsing the whole CSV every minute.
- No location is hardcoded anywhere; the dashboard title, menu bar and widget all read
  `location.name` from config.

### Fixed
- **Daylight saving in chart tick generation** (ROADMAP #5). Day-sized steps advanced by a
  fixed 86,400,000 ms, so every tick after a DST transition drifted an hour off midnight.
  Invisible in Queensland. Now steps by calendar days; verified by reintroducing the bug and
  confirming 6 of 8 tests fail.
- `http_get` sent `X-API-Key: None` for keyless providers, which urllib rejects outright.
- The QLD API silently ignores unknown query parameters — `from_date`/`to_date` returned the
  most recent 1000 rows instead of the requested window. Wrong data, no error.
- Gap detection used a fixed 25-minute threshold, which would fire on *every* poll against an
  hourly feed. Now scales with each provider's reporting interval.
- `serve_forever()` refuses to start when the port is busy. A stale server left running from
  another directory serves old data and old HTML, which looks exactly like a dead agent.
- Corroboration excluded every peer when sources lacked an id (`None != None` is false).
- `poller.py --doctor` reported row counts from the retired CSV rather than the live database.
- The scheduler's own logs (`launchd.*.log`) grew without bound — the only unbounded growth
  in the project. The poller now trims them alongside its own.

### Removed
- The CSV write path (`append_rows`, `read_csv_rows`). Reading is preserved for migration.
- Dead migration code for the project's pre-rename name, in `scheduler.py install`.

---

## 0.4.0 — 2026-07-31

The project was renamed.

### Changed
- **Renamed to Airo** — a name that says nothing about anyone's neighbourhood. The launch
  agent label, `~/.airo/`, the `.app` bundle and every document moved with it, and no
  hardcoded location was left behind in the code: the dashboard title, the menu bar and the
  widget all read `location.name` from the config.

*(Written after the fact. This version shipped without an entry, which is how the body above
came to reference "since v0.4" against a version this file did not list.)*

---

## 0.3.0 — 2026-07-31

Alerting, a single control surface, and legal compliance ahead of open-sourcing.

### Added
- **Threshold and trend alerts.** macOS notifications on three triggers: `crossed` (10-minute
  average enters the amber band, default AQI 67), `climbing` (early warning while still below
  threshold, when the 10-minute runs `rising_delta` above the 1-hour), and `cleared`.
  Hysteresis, `cooldown_minutes` and `quiet_hours` prevent repeat alerts.
- **`poller.py`** — one entry point for `start`, `stop`, `restart`, `status`, `poll`,
  `test-alert`, `dashboard`, `dashboard-stop`, `logs`, `alerts on|off`, `backfill N`.
  Bare `python3 poller.py --status` prints a summary.
- **Full SwiftBar action menu.** Agent controls, alert toggle (reading live state from
  config), test notification, config editor, data folder, backfill — all without a terminal.
- `--test-alert` CLI flag.
- `alerts` block in `config.json`, fully commented.
- **[RESEARCH.md](RESEARCH.md)** — the evidence base, with citations. Findings on valley
  drainage, sensor accuracy, AQI scales, indoor mitigation and product implications.
- **Attribution and health disclaimer** in README and dashboard, as required by PurpleAir
  ToS §4.8 and §7.3.
- **Warning against republishing PurpleAir data** (ToS §4.3), in README.
- ROADMAP §Legal — review of PurpleAir's terms across three commercialisation stages.
  *(The full review is not published; the obligations remain public in LICENSING.md.)*

### Changed
- LICENSE copyright holder is now Donnish Pty Ltd.
- ROADMAP #9 expanded into a three-phase weather-capture and forecasting plan, with the
  measured wind/temperature correlations and the ACL and §4.4 constraints that bound it.
- README restructured around `poller.py`; raw `launchctl` commands moved to a collapsible block.

### Fixed
- Alerting is wrapped so a notification failure can never interrupt data collection.

---

## 0.2.0 — 2026-07-31

Rearchitected around a periodic task rather than a resident daemon, and made the process
identifiable to macOS.

### Added
- **Health check** — `python3 poller.py --doctor` verifies agent registration, last exit code, data
  freshness, row count, key permissions and open ports. Knows that "no process running" is
  the correct resting state.
- **On-demand dashboard server** — `python3 poller.py --open` starts the server only when needed.
- **Named `.app` bundle** — macOS now reports "Airo" instead of `python3` in
  Background Task Management, Login Items and Activity Monitor. Ad-hoc code-signed.
- **Evening-window heatmap** — nights as rows, 3pm–1am as columns, cells coloured by band,
  switchable across 7 / 14 / 30 nights or the full record. Includes per-night mean and peak,
  and a median "typical evening" row.
- **Data-quality warning** — flags readings above ≈350 µg/m³ as implausible, naming the
  affected dates.
- `--serve` mode for the dashboard server alone.
- `poll_minutes` published in `latest.json` so widgets can compute staleness.
- Staleness indicator in the menu bar (`⚠︎` amber) and on the dashboard.
- Crosshair with a time chip pinned to the axis on chart hover.
- Full open-source documentation set.

### Changed
- **`launchd` now uses `StartInterval`, not `KeepAlive`.** The poller runs ~2s every 15
  minutes and exits. Idle memory dropped from ~20 MB resident to zero.
- **No network listener by default.** Previously the server ran permanently.
- Launch agent renamed `com.airo.poller` → `com.donnish.airo`. `scheduler.py install`
  removes the old agent automatically.
- Menu bar leads with a `●` carrying the band colour — far more legible than coloured text.
- Heatmap summaries use **medians**, so implausible spikes can't distort a typical evening.
- Evening-premium chart limited to the most recent 30 nights; "worst nights" still searches
  the whole record.
- Launch agent runs at `Nice 10` with low-priority I/O and a minimal explicit `PATH`.

### Fixed
- **Rolling averages always `null`.** PurpleAir nests them under a `stats` object; the code
  read the top level. Now checks `stats`, then top level, then `stats_a`, with a documented
  fallback to the instantaneous value.
- **Evening/daytime bucketing was wrong by up to 10×.** `toISOString()` converts to UTC, so
  in UTC+10 a 9am reading landed on the previous date while 9pm stayed on the current one.
  Now uses local date parts.
- **Charts rendered blank.** Chart.js `type:'time'` needs a date adapter that wasn't loaded.
  Replaced with a linear scale over epoch-ms and custom tick generation.
- **One chart failure blanked four unrelated panels.** Render steps now run in isolated
  `try/catch` blocks.
- **X-axis showed almost no time labels**, at meaningless positions like `06:44` — a linear
  scale picks round numbers of milliseconds. Ticks are now generated on local hour and day
  boundaries.
- **`poller.py --doctor` reported a false failure** on every run — the exit-code parser took the `=`
  sign instead of the value.
- **Dashboard contradicted itself**, showing "can't reach local data" beside live-looking
  numbers. Stale values now dim and are labelled "last known, not live", and the message
  points at `python3 poller.py --open`.
- Band colour now matches the displayed rounded value rather than the unrounded one.
- Incomplete nights (under 2 hours of data in either bucket) no longer produce meaningless
  evening-premium ratios.
- First/last reading dates include the year once the record spans more than ~4 months.

### Security
- API key permissions enforced to `600` on every install, not only on first write.
- Launch agent no longer inherits the shell environment.

---

## 0.1.0 — 2026-07-30

Initial working version.

### Added
- Polling of a PurpleAir sensor with append to a local CSV.
- **Gap detection and backfill** from PurpleAir's history endpoint — the core reliability
  feature. Sleep or downtime is repaired on the next poll rather than lost.
- Australian AQI conversion and banding (100 = 25 µg/m³ NEPM).
- Single-file dashboard: history chart, rolling averages, evening-premium ratios,
  worst-nights table, embedded PurpleAir widget as a cross-check.
- SwiftBar menu-bar plugin and Übersicht desktop widget.
- `scheduler.py install` with hidden key entry, test poll and `launchd` installation.
- `--once`, `--daemon`, `--backfill`, `--status` CLI modes.
- Zero runtime dependencies — Python standard library only.

[Unreleased]: https://github.com/Donnishcomau/airo/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/Donnishcomau/airo/releases/tag/v0.6.1
[0.6.0]: https://github.com/Donnishcomau/airo/releases/tag/v0.6.0

<!-- versions before 0.6.0 predate the public repository and have no tags -->
