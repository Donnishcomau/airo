# Roadmap

What is still open, what is deliberately not being done, and the register of
risks with the mechanism that closes each one.

**Finished work is not tracked here.** It is in [CHANGELOG.md](CHANGELOG.md)
under the release it shipped in, and the reasoning behind it is in
[ARCHITECTURE.md](ARCHITECTURE.md) and [DECISIONS.md](DECISIONS.md). This file
kept its own account of every closed item for a while and the two accounts
drifted — a roadmap that disagrees with the repository is worse than no roadmap,
because it is a confident description of a project that does not exist.

Status: `todo` · `blocked on the maintainer` · `parked`

---

## Where this stands

Airo works end to end from a clean clone and an empty home: setup by street
address, coverage-aware network choice, live probing so a dead station is never
recommended, first poll, dashboard, tray, alerts, weather capture, a correlation
computed over your own record, and a six-hour outlook that stays silent until it
has earned the right to speak. At least 1493 Python tests and 46 Rust tests,
green on macOS, Windows and Linux across two Python versions.

On macOS nothing a user needs requires a terminal: a `.dmg` carrying its own
Python, settings in the app's own window, location by address rather than by
latitude. Windows and Linux installers are built and checksummed on every tag and
**nobody has run one** — they are labelled untested everywhere a user can see
them, rather than leaving somebody to find out.

Two things stand between that and installers anyone would call supported, and
both are the maintainer's rather than work someone can pick up:

1. **Signing and notarisation**, which needs an Apple Developer account. What
   stops a non-technical user is Gatekeeper, not packaging.
2. **Somebody opening the Windows and Linux installers** on real hardware. A
   contained, high-value job for anyone who has one of those machines.

Decisions here do get revised, and a revision is written where the old decision
was, with its reasoning, so the next reader meets a decision rather than a
contradiction — [ARCHITECTURE §2.5a](ARCHITECTURE.md) is the model.

---

## Open

### 3c. More national networks — `blocked on the maintainer`

`qld` and `nsw` are in and need no account. Adding a country is one `Provider`
subclass — `NswProvider` is the reference for a POST-a-query-document API,
`QldProvider` for GET-with-params — and the contract tests enumerate from
`PROVIDERS`, so a new provider inherits every obligation rather than needing its
own review. **Every keyless candidate has been probed and there are none left**;
what remains needs an account somebody has to register for.

| Network | Status | Detail |
|---|---|---|
| **US AirNow** | `todo` — viable | `airnowapi.org/aq/observation/latLong/current` is live and returns 401 for a bad key, so the endpoint is right. Needs a free key, and cannot be verified end to end without one |
| **VIC EPA** | `todo` | `gateway.api.epa.vic.gov.au/environmentMonitoring/v1/sites` 404s unauthenticated; needs a subscription key and the correct path |
| **SA** | `todo` | data.sa.gov.au publishes 47 air-quality *datasets* through CKAN, not a live observations API — per-dataset work rather than one adapter |
| **UK DEFRA** | `parked` — blocked by format | The UK-AIR SOS API is open and keyless, but exposes only *"Volatile PM2.5"* and *"Non-volatile PM2.5"* with no plain total. Deriving one means summing fractions, which is a domain judgement, and a provider silently reporting one fraction as total PM2.5 would feed wrong numbers into corroboration — worse than no provider. Needs somebody who knows the UK network; OpenAQ covers the UK meanwhile |
| **EEA** | `parked` — blocked by format | The download service is live and keyless and **serves Parquet files, not readings**. There is no Parquet reader in the standard library, so this would be the project's first runtime dependency, against rule 1, for one region OpenAQ already covers. The shape is wrong as well: it is a bulk archive, and this poller asks what a station is reading now. Revisit if the EEA publishes a per-station current endpoint |

> **Do not ship a provider you cannot verify end to end.** Every provider in the
> tree was checked live for discovery, current reading and history before it was
> committed. An unverified adapter looks identical to a working one until
> somebody relies on it.

### 3f. Signing, and installers nobody has opened — `blocked on the maintainer`

Packaging, payload, first run and uninstall are done, and the lifecycle has been
exercised from a downloaded `.dmg` — install, configure, poll, export, back up,
wipe, restore, uninstall ([ARCHITECTURE §4a](ARCHITECTURE.md)). What is left is
the two items above. Until signing lands, the release notes tell users what the
warning is and how to get past it. One loose end that blocks nobody:
`tauri.conf.json` carries its own version number, which is one too many.

### 6. Data quality — what is still wanted — `todo`

Quarantine ships, and quality is decided once at ingest by
`store.assess_quality()` under a *surface, don't silently drop* policy
([ARCHITECTURE §3.5](ARCHITECTURE.md)). Two checks from the original list are
still unbuilt: **flat-line detection**, for a sensor stuck at one value, and a
**`last_seen` staleness check** telling "the sensor is offline" apart from "our
poller stopped". A/B channel disagreement is done.

### 10. Alerts say "Script Editor" on macOS — `todo`

`osascript` posts on behalf of the calling process, so that is whose name
appears. Posting from the `.app` bundle would fix it
([ARCHITECTURE §2.4](ARCHITECTURE.md)).

### Parked

- **15. Homebrew tap** — `brew install donnish/tap/airo`. The prerequisites it
  named are done; nobody has picked it up, and a signed installer helps more people first.
- **16. Docker** — would suit a NAS or an always-on box and conflicts with the
  desktop-widget model. Worth doing only if headless logging becomes a real use case.

### Known issues

Everything else from this table is fixed and recorded in
[CHANGELOG.md](CHANGELOG.md). These two are open and both are the maintainer's.

| | Issue | Status |
|---|---|---|
| I | Two CI checks run without being required | `Coverage floor` and CodeQL's `Analyze (python)` are not in branch protection. The `timezones` job was in exactly that state, and a DST bug survived two releases while every runner was UTC |
| J | One open Dependabot alert: `glib` 0.18.5 | `wont-fix`, dismissed on GitHub as `tolerable_risk` with the chain recorded in the dismissal rather than left unexplained. Unsoundness in `VariantStrIter`'s `Iterator`/`DoubleEndedIterator` impls, medium, patched in 0.20.0 and not upgradable: `airo-tray → tauri → gtk 0.18.2 → glib`, and gtk requires `^0.18`, so `--precise 0.20.0` fails. gtk is Linux-only in Tauri, and this project's Rust calls neither `glib` nor any `Variant` API — the tray renders `latest.json` and decides nothing. Revisit when Tauri moves to gtk 0.19+ |

---

## Where the finished items went

The numbers survive because tests, code comments and other documents cite them.

| | Item | Where the story is now |
|---|---|---|
| #0 | Renamed to Airo | CHANGELOG 0.4.0 |
| #1, #2 | Test suite, and CI on three platforms | ARCHITECTURE §7a; CHANGELOG 0.5.0 |
| #3 | AGPL-3.0-or-later, with a commercial licence alongside | [LICENSING.md](LICENSING.md); CHANGELOG 0.5.0 |
| #3a | Contributor licence agreement | [CLA.md](CLA.md), [CONTRIBUTING.md](CONTRIBUTING.md); CHANGELOG 0.6.0. Not yet reviewed by a lawyer, which CLA.md says at the top |
| #3b, #17 | Screenshots and a demo | README; CHANGELOG 0.6.0 |
| #3d | Settings without a terminal | ARCHITECTURE §2.8 and §5; CHANGELOG 0.6.0 |
| #3e | Published from a fresh root | [RELEASING §2](RELEASING.md); DECISIONS D9. The public repository's history begins at one parentless commit holding the current tree |
| #4 | Configurable AQI scale | ARCHITECTURE §4; DECISIONS D2 |
| #5 | Daylight saving | ARCHITECTURE §3.2, and two rows of the register below |
| #6 | Data-quality quarantine | ARCHITECTURE §3.5; DECISIONS D6 — what is still wanted is above |
| #7 | Chart reduction that keeps the peaks | ARCHITECTURE §3.3; `store.series()` |
| #8 | CSV scaling | superseded by SQLite — ARCHITECTURE §2.5a; DECISIONS D4 |
| #9 | Weather capture, correlation, six-hour outlook | CHANGELOG 0.6.0; the original 22-day finding is in [RESEARCH.md](RESEARCH.md) §3 |
| #10 | Threshold and trend alerting | ARCHITECTURE §2.4; CHANGELOG 0.3.0 — the remaining niggle is above |
| #11, #13 | Multiple sources per location, and export for analysis | README; DECISIONS D5; ARCHITECTURE §2.5a; CHANGELOG 0.5.0 |
| #12 | One widget, not three | CHANGELOG 0.6.0 |
| #14, #14a | Linux and Windows, and the cross-platform tray | CHANGELOG 0.5.0 |
| #18, #19 | Interpretation guide, and sensor siting | README |
| A–H, K, L | Known issues | CHANGELOG, and the register below |

---

## Risk register

Every risk below has a mechanism, not a note. The rule this project follows is
that a documented risk is an unmitigated one: prose is not executable, nobody
re-reads it, and the person who reintroduces the problem in two years will
never have seen it. So each row names the code or test that fails when the
mitigation is undone.

Where a risk genuinely cannot be closed in code — a provider changing its
terms, say — the mitigation is the thing that makes the change *survivable*,
and that part is testable even when the event is not preventable.

### Legal

| Risk | Mitigation | Enforced by |
|---|---|---|
| PurpleAir attribution dropped (ToS §4.8) | Attribution is rendered from `latest.json`, never written as a literal, so it is always the networks actually in use | `test_obligations.py::TestAttribution` — a hard-coded attribution string in any surface fails the build |
| CC BY notice missing for government feeds | Each `Provider` carries its own `attribution`; every UI renders the list | `TestAttribution::test_each_surface_renders_attributions` |
| Health disclaimer removed | Required in README, dashboard, tray window, `LICENSING.md` and every CSV export header | `TestHealthDisclaimer` (5 tests) |
| PurpleAir data redistributed (ToS §4.3) | Shape-matching `.gitignore`, a pre-commit hook, and a five-branch CI guard including a content check for a readings header | `tools/pre-commit`, `.github/workflows/ci.yml` |
| A claim we cannot support (ACL s4) | Forbidden patterns — health claims, safety assurances, guarantees — scanned across every user-visible surface | `TestNoUnsupportableClaims` |
| A forecast without reasonable grounds (ACL s4) | `forecast.phrase()` is the only sanctioned way to say anything forward-looking: it refuses certainty wording, demands a stated basis, and refuses to speak at all until skill is measured over ≥30 verified outcomes | `test_forecast.py::TestReasonableGrounds` |
| **Telling someone the air is clean when we do not know** — Queensland reports `-9999` when a station is offline; stored, that became AQI −39,996, which falls below the first breakpoint and rendered as **"Very good"** | Rejected at three independent layers — the provider boundary, ingest, and the scale conversion — each with a test that fails when only that layer is removed | `test_scales.py::TestSentinelNeverReadsAsSafe` |
| **An alert firing inside quiet hours** — the failure that gets the whole feature switched off, after which no warning arrives at all | Suppression tested at both boundaries and across midnight, since a window like 22:00–07:00 breaks a naive comparison and would suppress nothing | `test_alerts.py::TestQuietHours` |
| **Quiet hours kept against the wrong clock.** `config.json` has carried `location.timezone` since the first version and nothing ever read it: six places resolved local time by calling `.astimezone()` with no argument, which is *the machine's* zone. A NAS or a Pi is usually left on UTC, a VM inherits its host, a laptop reports the zone it woke up in. In Brisbane that silenced 08:00–17:00 local — the middle of the day — and notified at 3am; the risk window the project is built around slid by the same ten hours | One resolver, `poller.resolve_zone()`, used by quiet hours, the risk window, `latest.json`'s local stamp, the evening analysis and the by-hour breakdown. It never raises: a typo degrades to the machine's zone with a note rather than stopping a poll. Where `zoneinfo` has no database — Windows, without a package that would be a runtime dependency — the configured zone cannot be applied, and `--doctor` reports that as a problem rather than leaving it to be discovered | `test_timezone.py` |
| **An alert that never fires at all.** The quiet-hours and suppression rows below were enforced by *grepping poller.py's source* for the right strings — which survives any change that keeps the words and alters the logic, the change most likely to be made by accident. `maybe_alert()` itself was never run | Crossing, climbing, clearing, cooldown, disabled, and no-reading are each exercised through `maybe_alert()`, with a control proving it can decline to notify | `test_alerts.py::TestTheAlertItselfActuallyFires` |
| **A warning that never arrives** — suppression skipping the bookkeeping, so the band change goes unrecorded and the next crossing is not detected either | The state write happens whether or not the notification does, and suppression is logged rather than silent | `test_alerts.py::TestSuppressionKeepsTheStateHonest` |
| PurpleAir owning a derived model (ToS §4.4) | `forecast.training_sources()` excludes model-encumbered providers by construction, and explains the exclusion rather than silently dropping the user's nearest sensor | `test_forecast.py::TestModelLicence` |
| Weather stored in the wrong unit | The response's declared units are compared against what was asked for on every fetch, and a mismatch is refused rather than converted. Phase B's finding is stated in m/s and the API sends km/h by default — a silent change would move every threshold by 3.6× without failing anything | `test_weather.py::TestUnitsAreNeverAssumed` |
| A missing hour of weather read as calm | Nulls are stored as NULL, never zero, and an hour with nothing in it is dropped rather than invented. Calm is the condition the whole premise turns on, so a gap that looks calm would corrupt the finding | `test_weather.py::TestMissingHoursStayMissing` |
| Weather capture costing a reading | Every failure in the weather path is logged and swallowed. A missing reading is the product failing; a missing hour of wind is not | `test_weather.py::TestWeatherNeverCostsAReading` |
| **A file copied out of the tree stating no licence.** Only the root LICENSE carried the notice — and LICENSE itself quotes the FSF's guidance to attach one to the start of each source file. A module lifted into a gist or into somebody else's project arrived saying nothing about what it is, which is the one case a root LICENSE cannot reach | Two SPDX lines at the head of every tracked `.py`, `.rs`, `.html` and `.sh` file, enumerated by extension off `git ls-files` rather than from a list that stops being true when someone adds a file. The holder, year and identifier are declared once, in the check, and `tray/Cargo.toml` and `tauri.conf.json` are compared against them rather than trusted to still agree | `test_contracts.py::TestEverySourceFileCarriesItsLicence` |

### Privacy

The tool knows where you live, at street resolution, updated every fifteen
minutes. This is the highest-consequence category and the least visible when
it goes wrong.

| Risk | Mitigation | Enforced by |
|---|---|---|
| A third party gets your IP whenever you open the dashboard | Chart.js was replaced with a local canvas renderer. The page loads **no** external subresource; the only remaining external reference is the attribution link, which fires only if clicked | `TestPrivacy::test_the_dashboard_loads_nothing_from_a_third_party` |
| Third-party JS running in a page that displays your address | Same mechanism — there is no `<script src>` to another host to compromise | as above, plus the equivalent test for the tray window |
| Location lookup readable, or forgeable, in transit | Three HTTPS geolocation services tried in order, coordinates bounds-checked, and the prompt names who is contacted before asking | `TestPrivacy::test_every_ip_geolocation_service_is_https` |
| Any other plaintext request | No `http://` URL anywhere but loopback | `TestPrivacy::test_no_outbound_url_is_plaintext_http` |
| Telemetry added later "just for metrics" | No analytics vendor may be named in any module | `TestPrivacy::test_there_is_no_telemetry` |
| Coordinates logged by intermediate proxies | Location never travels in a query string except to the provider that needs it to answer | `TestPrivacy::test_location_never_travels_in_a_url_query_string` |
| Personal data committed | User data lives in `~/.airo`, never the repo; blocked by shape at commit time and again in CI | `tools/pre-commit` |
| **A web page reading your address off the local server.** Binding to `127.0.0.1` keeps other machines out, not other *pages* — every site the user visits can reach the server from inside their browser. A hostname the attacker owns, resolved to loopback, is same-origin as far as the browser is concerned, so an origin check never sees it | The `Host` header is checked against loopback literals on reads as well as writes, because the attacker controls the name and not the literal | `test_settings_api.py::TestTheServerRefusesWhatABrowserCanBeMadeToSend::test_reads_are_guarded_too` |

### Key handling

| Risk | Mitigation | Enforced by |
|---|---|---|
| **A tampered or swapped Python runtime.** The app ships an interpreter now, so the project has a supply chain of exactly one item — and a binary someone else built is the classic place to hide something | The version is pinned in `tools/fetch_runtime.py`, never resolved as "latest"; the SHA-256 is recorded there and checked **before** anything is extracted, so a rejected archive is never unpacked anywhere for something else to find; a mismatch is fatal and says whether to investigate or to update the pin deliberately | `test_runtime.py::TestAnUnverifiedRuntimeIsNeverUnpacked` |
| A key committed | Keys never enter `config.json`. They live in `~/.airo/<provider>.key` or an env var, both outside the repo | `tools/pre-commit` rule 5 |
| A key printed, logged, or put in `latest.json` | `get_api_key()` returns; nothing formats it. Audited across `--status`, `--where`, `--doctor`, `--list-sources`, `latest.json`, the log and a backup archive | audit reproduced in `test_fresh_install.py` |
| A key leaving in a backup | Excluded by default; `--include-keys` is an explicit opt-in and says so | `test_backup.py` |
| A key file world-readable | `secure_path()` — `chmod 600` on POSIX, `icacls` on Windows where `chmod` only toggles the read-only bit — and `path_is_restricted()` reads the state back rather than assuming | `test_fresh_install.py` |
| **A key served to a browser by the settings API.** Describing the whole installation to a settings page means serialising the config, and one credential lives *inside* it: `read_key`, for a private PurpleAir sensor, sits per source in `config.json` rather than in `~/.airo/<provider>.key` with every other key | Two layers. `settings_payload()` builds its output field by field and reports presence only; `scrub_secrets()` then walks the result and replaces anything credential-shaped with a `has_*` flag. Verified by removing each layer alone — with only the payload broken the backstop still holds, and the credential reaches the served body only when both are gone | `test_settings_api.py::TestNoCredentialIsEverServed` |

### Data loss

| Risk | Mitigation | Enforced by |
|---|---|---|
| **One unreadable timestamp blanking every chart.** A stored `observed_utc` is only as good as whatever wrote it — `migrate_from_csv()` passes the old file's column through untouched — and a single unparseable value raised inside the series endpoint, which returned 500 for the whole request. Every chart, every source, from one bad row | The row is skipped and **counted**, so the dashboard can say how many readings could not be placed in time rather than quietly drawing a shorter line | `test_settings_api.py::TestTheApiSurfaceEndToEnd::test_one_unreadable_timestamp_does_not_blank_every_chart` |
| A gap never repaired | Gap detection and backfill on the next poll | `test_store.py::TestGapRepair` |
| Double-counting on overlapping backfill | Inserts idempotent on `(source_id, observed_utc)` | `test_store.py::TestIngest` |
| Corruption noticed too late | `--verify` runs an integrity check; `--doctor` reports it | `store.verify()` |
| Retention pruning too much | `--prune` supports `--dry-run` and reports what it would remove before doing it | `test_store.py` |
| A sentinel already in the database, written before the guards | `--repair` clears them and re-asks the provider for the window; NULL rather than a guess, and no row is ever deleted | `test_store.py::TestSentinelRepair` |
| **Uninstalling taking the readings with it.** Removing the software is a statement about wanting it to stop, not about wanting years of measurements destroyed — and an uninstaller is reached at exactly the moment nobody is reading carefully | `--uninstall` stops both background jobs and deletes nothing. The data directory and its row count are printed so somebody who *does* want it gone decides that themselves | `test_cli.py::TestUninstall::test_it_never_deletes_a_reading` |
| Toggling a source off destroying its history | `remove_source()` can no longer be asked to delete. It took `delete_readings=True`, and `source_id` is `ON DELETE CASCADE`, so one argument erased every reading a source had produced | `test_retention.py::TestDisablingASourceKeepsItsHistory` |
| Purging a source with no way back | `forget_source()` exports the doomed rows to CSV before deleting, and returns the count so a caller cannot ignore it | `test_retention.py::TestDisablingASourceKeepsItsHistory` |
| Backup rotation running on a corrupt archive | The database checksum is recomputed against the manifest before any older archive is deleted; a failure keeps every existing backup and says so | `test_retention.py::TestBackupRotation` |
| The preview for a destructive delete not working | `--prune --dry-run` was an argparse error for its whole life, documented twice in the README. `--dry-run` is a modifier now, and two modes are still refused together | `test_store.py::TestDryRunIsAModifierNotAMode` |
| **The tray opening a page that is not there.** It held `http://127.0.0.1:8787/dashboard.html` as a literal, but `serve_port` is configurable and the server deliberately moves to the next free port when an unrelated program holds 8787 — so the menu opened a dead page, or a stranger's page | `poller.py --open <page>` resolves the URL: it finds a running server, starts one if there is none, and reads back the port it actually bound | `test_obligations.py::TestTheTrayDecidesNothingAboutUrls`, `test_cli.py::TestOpeningAPage` |
| **A web page rewriting your settings.** Once the local server accepts writes, an ordinary page in the user's browser could repoint monitoring at another suburb, or move `data_dir` and quietly abandon years of readings — no dialog, no trace | Four independent checks before any routing: loopback `Host`, own `Origin`, a JSON content type so the request cannot be a preflight-free CORS "simple request", and a per-process token the page is handed when served and never written to disk. Each verified by removing it alone | `test_settings_api.py::TestTheServerRefusesWhatABrowserCanBeMadeToSend` |
| Readings abandoned by pointing `data_dir` somewhere new | The active directory is recorded, so the orphaned path can be named rather than guessed; `--status`, `--where` and `--doctor` all report it with the row count | `test_retention.py::TestOrphanedDatabaseIsNoticed` |
| A data directory that cannot be written | Setup probes it — mkdir, write, delete — at the moment it is chosen, not on the first poll | `test_retention.py::TestSetupAsksWhereDataLives` |
| A backup that cannot be restored | Backups use the SQLite backup API, not a file copy of a live database, and `inspect()` reads an archive back before `restore()` touches anything | `test_backup.py` |

### Provider dependency

| Risk | Mitigation | Enforced by |
|---|---|---|
| One network dies or changes terms | Four providers behind one interface; nothing in the core loop names a concrete provider | `test_sustainability.py::TestNoSingleProviderCanEndTheProject` |
| Every network requiring an account | At least one keyless network must remain, so a terms change cannot lock out every new user at once | `test_sustainability.py::TestNoSingleProviderCanEndTheProject` |
| A recommended station that reports nothing | Setup probes the nearest sites and excludes non-reporting ones from its suggestion, listing them marked so a manual pick is informed. Several of the nearest stations publish no PM2.5 | `test_fresh_install.py::TestSetupNeverSuggestsADeadStation` |
| **A provider misreading its own feed.** Every trap in ARCHITECTURE §3 lives in provider parsing — PurpleAir's `stats` nesting, Queensland's `-9999`, NSW's hour 24 — and nothing had ever stubbed `http_get`, so the parsing was checked for shape and never for correctness | Each provider is run against realistic payloads, including the malformed and offline cases | `test_providers.py` |
| **A Queensland timestamp meaning a different instant per reader.** The feed publishes local time with no offset, and a naive datetime converted to UTC is read in the *machine's* zone — right in Brisbane, wrong by the reader's offset anywhere else | `QLD_TIMEZONE` is written down as a constant `+10:00` (Queensland has no DST); a timestamp arriving *with* an offset is trusted as given | `test_providers.py::TestQueenslandSentinelAndPaging::test_a_naive_timestamp_is_queensland_time_not_the_readers_time` |
| A silently wrong feed | Cross-source corroboration, per-site historical ratios, and PurpleAir A/B channel disagreement | `test_fusion.py::TestCorroboration` |
| An API answering with the wrong window | `--doctor` checks each provider's history call really honours its date range — this is how the QLD `from_date`/`start_date` bug was caught | `run_doctor()` |

### Sustainability

The realistic failure is not a bug. It is the repository going quiet while
someone's four years of readings sit inside it. The mitigation is not a
promise to keep maintaining it — it is making leaving free, so staying is
never a trap.

| Risk | Mitigation | Enforced by |
|---|---|---|
| Data stranded if the project stops | The store is plain SQLite, verified by its file magic, readable by anything | `test_sustainability.py::TestTheDataOutlivesTheTool` |
| An export that needs Airo to read | Plain CSV a spreadsheet opens, carrying its own provenance and licence header | `test_sustainability.py::TestTheDataOutlivesTheTool` |
| Leaving requires a running service | `backup.py` and `--export` are standalone scripts; there is no daemon to be alive | `test_sustainability.py::TestNoLockIn` |
| A stalled project nobody may continue | AGPL-3.0-or-later — a fork needs no permission | `test_sustainability.py::TestNoLockIn` |
| A hosted service shutting down | There is no server. Everything binds loopback | `test_sustainability.py::TestTheDataOutlivesTheTool` |
| Two trays running at once — both poll, both write, and a click reaches whichever the OS picked. A launchd agent plus one manual run from the checkout is all it takes | The tray claims a pid lock before showing anything, and refuses to start with the pid of the incumbent. A lock naming a dead process is reclaimed, so a crash cannot lock the user out forever | `tray/src/airo.rs` tests |
| **A CLI flag that exists but does not work.** The contract test asserts every documented flag is in argparse, which catches a rename and never catches a broken command — `--prune --dry-run` was an argparse error for its whole life while being documented twice as the way to preview a destructive delete | Every branch of `main()` is run and asserted on what it prints, since for a CLI that is the product | `test_cli.py` |
| **An export with no attribution.** CC BY requires the notice to travel with the data, and `store.py` held the wording in a table keyed by provider — a provider with no entry exported an empty attribution line under "Licence unknown" | Export terms are enumerated from `PROVIDERS`, so a new network carries its own into every export whether or not the table was updated | `test_obligations.py::TestEveryProviderAttributesItsExport` |
| **A report that quietly answers a different question.** `analyse.py` is how the central claim is meant to be *checked* rather than trusted, and it had no tests at all. `agreement --by-hour` selected UTC-hour buckets and relabelled them using the offset in force on 1 January — so every hour was named an hour out for half the year anywhere observing DST, and out year-round in India, Adelaide, Nepal and Newfoundland, where the offset is not a whole number of hours | Local hours are asked for as local hours: each bucket is converted from its own date, so DST is accounted for, and attributed by its midpoint, so a half-hour offset lands on the hour holding most of it. Verified by running the suite in six zones, including one at +12:45 | `test_analyse.py`, `test_store.py::TestAnHourOfTheDayIsNotAnOffset` |
| **A schedule that installs cleanly and never runs.** systemd splits `ExecStart=` on whitespace, so a project folder containing a space — "My Air Quality" — produced a unit systemd read as five arguments. The units wrote, the timer enabled, `systemctl` reported success, and every poll failed in the journal of a user unit nobody thinks to read. The symptom is simply that no readings arrive. macOS and Windows were immune by accident rather than design: a launchd plist holds an argv *array* and the schtasks command was already quoted — only the backend that built a command by string interpolation was exposed | Every path in a systemd unit is quoted and `%` escaped, checked by splitting the generated `ExecStart` the way systemd does rather than by matching a string. The launchd and schtasks backends have the same test, so their immunity is now asserted rather than assumed | `test_scheduler.py::TestAPathWithASpaceDoesNotBreakTheSchedule` |
| **A regression between two correct layers.** Every failure this project has shipped lived in the path *between* parts that each had passing tests: `weather.py` absent from the installer's module list, extreme air flagged at ingest and therefore never reaching the alert, `--prune --dry-run` an argparse error for its whole life while documented twice | An end-to-end suite drives the real entry points — `poller.main()` with a real argv, real SQLite, real files, a real loopback server — through whole journeys, and re-checks the project's own rules after *every* step: no derived index stored, no unknown quality verdict, no key in any written file, nothing of the user's in the repository. Its own guards are verified by deliberately breaking each one | `test_end_to_end.py` |
| **A test that passes because a third party answered.** One suite run made 25 real requests to Open-Meteo. Nothing reported it: `capture_weather()` swallows every failure by design, so the calls succeeded quietly and would have failed just as quietly — the weather path was not being tested at all, and CI depended on somebody else's uptime on six jobs on every push | Outbound HTTP is refused in every suite that drives a poll; loopback is allowed, because that is the product rather than the internet. The list of suites is enumerated from disk, so a new one is in scope without anyone remembering | `tests/netguard.py`, `test_contracts.py::TestNoSuiteQuietlyUsesTheInternet` |
| **An alert nobody could receive.** `notify()` shelled to `osascript`, so on Linux and Windows it raised FileNotFoundError, caught it, logged a warning and returned False. Alerting is enabled by default and is a headline feature; it did nothing at all on two of the three platforms the installers target, and said so only in a log nobody reads. The same outcome as the "alert that never fires" row above, reached by a different route — correct firing logic, and the notification went nowhere | Commands are built per platform by a pure function, so every platform's can be inspected from any platform — two thirds of this was unreachable in CI, which is how it survived two releases. Text travels as data on all three (osascript `on run argv`, notify-send argv, PowerShell environment), because `message` carries a `site_name` that came from a provider's JSON. `--doctor` reports whether an alert can reach the screen | `test_notifications.py` |
| **Tests writing into the developer's own install.** CONVENTIONS forbids it and it was happening: a suite run appended two fixture strings to a live install's log, between two real polls, and the maintainer read that tail as their monitor having died. Three routes — `poller`'s paths are module-level and resolved at import, so anything reaching `log()` wrote to the real file; a test spawned an interpreter running `--prune` against the real config, outside any in-process guard; and `run_doctor()` scans for orphaned databases via the home directory, which a redirect of the module paths does not cover — the same route `get_api_key()` uses to read a real key | `tests/homeguard.py` redirects poller's paths *and* HOME for every suite; subprocesses are given `AIRO_DATA`, `AIRO_CONFIG` and `HOME`. Enforced by three contracts that enumerate from disk and from poller's own globals: every suite calls the hook, every spawned interpreter passes an environment, and every path poller resolves under `~/.airo` is in the guard. Verified by a full run leaving the real install byte-identical | `test_contracts.py::TestNoSuiteTouchesTheDevelopersOwnInstall` |
| **A correlation read as a cause, or as a finding at all.** Phase B computes coefficients over somebody's own record, and a coefficient is trivially easy to over-read: eight samples yield r = −0.9 out of noise, and the person most likely to act on that is the one who installed the tool yesterday | Refused below 72 paired hours, with the count and the requirement both stated; every coefficient carries its n, including the summary's — which it did not at first, and a test caught; the summary is computed from the coefficients rather than printed, so the report can and does say "no clear weather signature", proven by a test that feeds it the opposite record; instrument faults are excluded from the join and extreme air is not; and the report states in as many words that correlation is not cause | `test_correlate.py` |
| **One instant stored two ways.** Times are text and compared as text, so how an instant is *spelled* decides both sorting and identity. `_iso()` was documented as "the only sanctioned writer" and ended `return str(v)`, and `insert_readings()` normalised a datetime while passing a string through — so OpenAQ's `current()`, which returns the API's own `...Z`, put 73 rows of a real database in a form nothing else used. The primary key is `(source_id, observed_utc)`, so **64 of them were a second row for an instant already held** — rule 5's "overlapping backfill costs nothing" does not survive that. `'+'` is 0x2B and `'Z'` is 0x5A, so they also sort around each other | One `canonical_utc()` that every writer goes through, accepting any offset and refusing anything unparseable rather than storing it in a key; a v6 migration that merges collisions **field by field, preferring a value over a NULL**, because one row of each pair carried a humidity the other lacked; and a contract enumerated over every `*_utc` column in every table rather than the one that broke | `test_timestamp_format.py` |
| **Shipping a bundle that cannot run.** Everything else in the suite runs the code from the checkout, which is not what anybody downloads — and the one defect this project has shipped that no unit test could see was exactly that gap: `weather.py` missing from the staged payload, every test green, `ModuleNotFoundError` on launch | The built `.app` is exercised as an artefact: its own interpreter imports every module found *in the payload* rather than in a list, the pages and licence it must carry are checked, nothing from `tests/` or `tools/` is shipped, `--doctor` runs from inside it, uninstall leaves the readings, and the shipped `store.py` is proven to hold the current canonicaliser rather than a stale copy. It also asserts the app never writes inside itself, which would break on any read-only mount. Skipped with a stated reason when no bundle is built, so the Python suite stays runnable without a Rust toolchain | `test_macos_bundle.py` |
| **A tray menu item that fails silently.** Twenty-seven handlers discarded their error with `let _ = ...`. The one that mattered is the Python spawn refusing to start — a missing interpreter or an incomplete payload — because that makes *every* item in the menu do nothing, and a bundle has shipped with a module missing from its payload before. The maintainer reported exactly this shape: "Open dashboard in browser is not working", with nothing anywhere saying why | Every handler reports through `report_problem()`, which writes to stderr — captured by launchd into `tray.err.log` — and fires a desktop notification naming the menu item that failed. Deliberately not routed through Python, since the case being reported is that Python would not run. A contract fails on any `let _ = airo::` in the tray, on a report that names no action, and on the Windows notification stub losing the comment that says it is one | `test_obligations.py`, `airo.rs` |
| **A test opening real browser windows.** `open_page()` used the standard library's webbrowser module, which the tests stubbed. Adding `/usr/bin/open` ahead of it — correctly, since `webbrowser` reaches `osascript -e 'open location'` and does not necessarily bring the window forward — went straight past every stub. Each suite run opened a tab on the maintainer's machine; they collected about fifteen, and the tests that recorded *which* URL was opened saw nothing and failed on an empty list | `tests/browserguard.py` blocks the effect and records the attempt, rather than stubbing one route to it. A contract fails if any shipped module reaches a browser outside `poller.launch_browser`, so the guard has exactly one thing to cover, and a second contract fails if any suite driving a page-opening path has not installed it. `setup.py` was reaching a browser directly in two places and now goes through the seam | `test_contracts.py`, `test_cli.py` |
| **A browser tab opened at a URL with nothing behind it.** The URL was handed over on the strength of having *started* a server, not on the server answering — so a failed or exited server produced a tab reading "Problem loading page", and clicking again produced another | `open_page()` asks `page_answers()` first, and says so in the log instead of opening anything. It delegates to `_serving_this_project`, which every other caller already uses: the first version made its own HTTP request and broke three tests that legitimately simulate a running server, which is the same escaped-seam mistake one layer down | `test_cli.py` |
| **Bundle tests passing against the wrong build.** They sorted the built images by *name* and took the first, which read as "the build" and meant "alphabetically first". A stray `Airo_0.5 (1).0_aarch64.dmg` four days old sorts ahead of `Airo_0.5.0_aarch64.dmg`, because a space precedes a full stop — so eighteen tests ran against the previous week's artefact, **passed**, and were quoted as evidence a fresh build was sound | Newest by modification time, never by name: version strings do not sort chronologically. A test asserts the newest image is the one chosen, using the two real filenames as the fixture, and a second surfaces the presence of more than one image rather than silently preferring one | `test_macos_bundle.py` |
| **Telling somebody to ventilate during a smoke event.** Inside failing against outside has two modes with *opposite* remedies: infiltration, where indoor tracks outdoor and the answer is to close up and filter, and an indoor source, where indoor spikes while outdoor is flat and the answer is to ventilate — but only while outside is cleaner. Reporting a ratio and leaving the reader to infer which is how the wrong one gets acted on | `analyse.indoor_outdoor()` names which is happening in words and carries the matching advice, decided in Python because it is health-relevant and rule 7 keeps those out of a renderer. The indoor-source case is checked first, so the dangerous mistake cannot be reached by falling through. Both cases are proven by a test, and by a third that asserts them against each other; the verdict states its hours, its ratio and that it is not a claim about cause | `test_indoor.py` |
| **A healthy sensor condemned as broken.** PurpleAir derives `confidence` from how far its two laser counters disagree, and `assess_quality` applied it whatever the reading carried — so an indoor PA-I reporting a single channel at confidence 30 had every live reading filed as an instrument fault, and `suspect` rows are excluded from the chart, the evening analysis and the inside/outside comparison. PurpleAir's own map showed it healthy throughout | The figure is heeded with two channels, and with none — a provider doubting its own reading without saying why is still worth hearing. It is *one* channel present that makes the number uninterpretable, which is the principle `assess_quality`'s docstring already stated about single-valued government feeds. A v9 migration re-derives the rows already stored, restricted to that case so it cannot redo the v5 smoke reassessment's work | `test_indoor.py` |
| **An indoor sensor speaking for the air outside.** `nearest` is the default rule and a sensor in the house is ~0 km away, so it becomes the headline — kitchen air under "avoid outdoor exertion", alerts firing on cooking, and the *outdoor* sensors marked uncorroborated for disagreeing with it. Quieter: Phase B correlates PM2.5 against outdoor wind and Phase C fits its forecast to Phase B's bands, so one contaminated join reaches every claim made about the future | A `placement` on each source (`outdoor`/`indoor`/`unknown`, never a boolean — treating unknown as outdoor is how it happens by default), detected from PurpleAir's `location_type` rather than asked. Excluded from the headline, corroboration, the weather join and outdoor alerts; still stored, still served, still shown, because refusing to display it would be discarding it | `test_indoor.py` |
| **A fault-injection run that proves nothing.** Three ways in one week, each producing a confident wrong answer: a baseline that was already failing so every fault reported the same two names; a restore that restored the file and not the bytecode, since Python invalidates a `.pyc` by mtime and size and a same-size rewrite in the same second changes neither; and faults injected into the tests, where deleting an assertion cannot fail | `tools/faultcheck.py` refuses to inject until the baseline is green, clears bytecode on the way in *and* out, refuses a `find` that matches more than once, and reports a suite that could not start as a failure rather than as "nothing caught it" Two more ways a green means nothing are now detected rather than described: an edit that leaves the syntax tree identical is refused before it runs, and a fault on a line the suite never executes is reported as UNRUN rather than as a missing test — opposite problems that read the same. The specs live in `tools/faults/` and run in CI on every push, so a guard that stops guarding fails the build instead of waiting to be noticed. | `test_faultcheck.py` |
| **A sensor going dark behind a provider that keeps answering.** `record_source_result` counted polls that *raised*, and PurpleAir does not stop answering when a sensor drops off the network — it serves that sensor's last reading, with its original timestamp, indefinitely. So the fetch succeeded, the counter reset every poll, and the detector never fired while the record developed exactly the hole its own docstring exists to prevent. Found on the maintainer's install, not by reading code: their nearest sensor and headline source was dark for about two days, every poll logged `0 new` against the same frozen reading, and the first visible sign was blank cells on a heatmap three days later | `ok_now` is now derived from whether the source *observed* anything, judged against the provider's own cadence, not from whether the call raised — the gap was already being computed one line above the call site, logged, and thrown away. The two silences are named separately because they need opposite responses: an unreachable provider sends you to your key and your network, a stale sensor tells you the provider is still answering and to go and look at the instrument. The existing tests missed it by driving the helper directly with `ok_now=False`, proving the counter worked while nothing asserted what `ok_now` was derived from — the fifth instance of a helper fully tested with its call site wrong, and now covered by a journey rather than only a unit test | `test_alerts.py`, `test_end_to_end.py` |
| **A page that renders the wrong thing, checked only for parsing.** The dashboard's inline script was run through `node --check` and nothing else, so a page that parsed was a page that passed. The failure that exposed it: the server stopped sending `age_minutes` for indoor sensors and the row rendered an em dash — correct behaviour for an absent field, and completely wrong as an answer to "is this sensor collecting data?". The maintainer asked that question about their own dashboard. Every claim about what a reader sees rested on someone having looked recently | The page's own script is executed against a payload and the resulting cells asserted on, with the payload assigned to the page's own `latest` so it is the real render path rather than a function called by hand. The harness asserts on itself — that a null field still renders the dash, and that the payload's data reaches the output — because a harness quietly rendering nothing would satisfy every negative assertion. Node stays a dev-only tool and the suite skips without it, the same bargain the syntax check already makes. Committed faults cover both halves, so `dashboard.html` is now inside the fault gate rather than outside it | `test_page_render.py` |
| **A page that degrades instead of breaking.** Two shapes, both silent. A `var(--x)` nothing defines is not an error — CSS drops the declaration and the element inherits, so `--bad` was written three times in `dashboard.html` and defined nowhere; two of the three carried a fallback hex and looked right, and the third did not, so the "N readings at an extreme level" warning — the loudest thing the page ever says — rendered in the body's grey. And every judgement on these pages is made in Python and matched here by string literal (`s.quality === 'suspect'`, a lookup keyed on `trend.direction`, a wording table keyed on the indoor/outdoor verdict); none of those enums can be a shared constant across a Python module and a build-free HTML file, so a rename in Python does not break the page — the tag stops appearing, the arrow falls through to its default, the verdict prints as its own raw slug, and everything still renders | Every custom property referenced in a page must be defined in that same page, there being no shared stylesheet to fall back on; and the enums are read out of the Python that owns them at run time — `compute_trend` and `assess_quality` driven, `corroborate`'s docstring set checked against its own branches, `store.PLACEMENTS` imported — then compared both ways against the literals harvested from the page script. Two-way on purpose: a page literal Python no longer produces is the rename, and a Python value the page no longer handles is a distinction the surface has quietly stopped drawing. The file holds no copy of either list, because a third copy is how the drift would arrive inside the guard against it | `test_surface_parity.py` |
| **A fixture whose result depends on the minute it runs at.** Readings written at the top of the current hour are up to fifty-nine minutes old when fusion judges them, and fusion declines to headline a stale one — so three tests passed at five past and failed at five to, and the failure named a placement bug that did not exist. The same shape made the coverage gate fail one run in three | A contract flags any fixture that truncates `now` to the hour *and* stores the result as a reading, unless it says why. The filter is shown a known-bad sample rather than only the repository, because once every real fixture is marked, "found nothing" and "the filter is broken" are the same result — a fault inverting it went unnoticed on exactly that account | `test_contracts.py` |
| **A test destroying the user's data, by a route nobody has thought of yet.** Four have been found and closed one at a time — redirected paths, a redirected HOME, launchctl keyed on uid, a path frozen at import. The fourth deleted three of the maintainer's backup archives. Every one was invisible until after it happened, so a register of known shapes cannot be the last line | `tools/check.py` hashes the real `~/.airo` before the suite and after it and **fails the run** on any difference, naming what was deleted, modified or created. CI does the same as a required check on every platform, so it cannot be skipped by not running `check.py` locally. Content-hashed rather than stat-compared, because the case that happened replaced a file with one of the same size in the same second The first version failed on every run, because the maintainer's own agent polls on a schedule and does not stop for a test — a check that fires on normal operation is switched off within a day. Agent-owned files under `data/` are checked differently rather than skipped: the database may not lose readings, a log may only grow, nothing may be deleted, and everything else is byte-identical or it is a failure. | `test_user_data_gate.py` |
| **A path under the user's home resolved at *import* time.** Redirecting `HOME` covers anything that answers when asked; it cannot cover a module constant, which froze the developer's real home before any guard could be installed. `backup.BACKUP_DIR` did this and the suite wrote archives into their real `~/.airo/backups` and rotated the genuine ones away; `setup.CONFIG_DIR` had the identical shape and had simply not been driven yet | Both resolve per call now, and a contract walks every shipped module — enumerated from the stager's own list — for a home-relative path resolved at module level. `backup.py`'s manual and automatic paths also disagreed about where backups live, so `create()` defaulted to the working directory, which for anyone running it from the checkout put a backup archive in the repository against rule 3 | `test_backup.py`, `test_contracts.py` |
| **A test unloading the developer's own background agent.** The fourth route into their install, and the one that survived the first three being closed. `homeguard` redirects paths and HOME; launchd cannot be redirected, because `launchctl` addresses agents as `gui/<uid>/<label>` — keyed on the uid and a fixed label. A bundle test running `--uninstall` under a temp HOME deleted a plist that was never there and unloaded the live agent. The evidence was unhelpful in every direction: plists untouched on disk, `launchctl list` empty, and the last line in the log a clean successful poll | `scheduler.run()` refuses `launchctl`, `systemctl` and `schtasks` when `HOME` is not this user's passwd home, and says why. The guard is in the real runner rather than at the call sites, so a test that stubs `run` to assert on arguments is unaffected while a genuine invocation is stopped | `test_scheduler.py` |
| **A measurement shown in a unit the reader does not think in.** A wind speed in the wrong unit is still a plausible number, so nothing looks wrong — and the same applies to a temperature. A single metric/imperial flag cannot express it either: the UK is Celsius and miles per hour | `units.py` resolves a display unit **per quantity** from the reader's region, with an explicit setting winning over it, and nothing is cached or written at setup — changing region changes the next screen with no migration. Rule 6 is untouched: µg/m³, Celsius, m/s and km stay canonical in the database, every conversion is exactly invertible, and PM2.5 is deliberately absent because µg/m³ is the unit everywhere including the US | `test_units.py` |
| **A number stored as PM2.5 that is not PM2.5.** OpenAQ's `discover()` filters to `parameter.name == "pm25"`, but `current()` re-fetches by sensor id and never asked again — and §3d made sources editable from a settings page, so an id can be typed or pasted afterwards. An NO₂ or ozone sensor would have looked entirely ordinary: plausible small numbers, no error, straight into corroboration and Phase B | Both `current()` and `history()` check the parameter name and unit the sensor declares, and refuse loudly rather than storing or silently dropping. A sensor declaring neither is accepted — an absent field is missing metadata, not a contradiction, and refusing on silence would break working installs to guard a case nobody has seen | `test_providers.py` |
| **A temperature whose unit is not recorded.** Found 6 Aug 2026 while checking whether the end-to-end invariants could actually fail — the Fahrenheit one could not, because no journey ever stored a temperature | `backfill_source()` normalises through `to_celsius()` and labels the row, the provider declares its own unit so no call site decides it from a name, a v7 migration repairs existing rows keyed on the unit being absent, and the journey invariant now fails on a temperature with no unit rather than only on one labelled `F` — which is the state that was actually dangerous, since an absent label leaves no evidence a migration could act on | `test_store.py`, `test_end_to_end.py` |
| **A forecast without reasonable grounds (ACL s4), now that there is one.** The guardrails shipped a year before the feature; Phase C is the part that has to earn its way past them | The outlook is gated on measured skill and stays silent at 29 verified outcomes, speaking at 30 — both sides tested, because a gate that never opens is a disabled feature and one that always opens is not a gate. It is scored against persistence, so beating autocorrelation is not mistaken for skill. Its grounds are the user's **own** wind bands with the hour count stated, reusing Phase B's table so the two cannot disagree. Predictions are written down and verified against what actually happened — an hour with no reading stays pending rather than being scored against a gap, and nothing is counted twice. PurpleAir is excluded from training by construction (ToS §4.4) and the refusal says so, which is a different sentence from "no data yet" | `test_forecast_outlook.py` |
| A new surface, module or provider added without its guards | Contracts enumerate from disk and from `PROVIDERS` rather than from a list, so a file that does not exist yet is already in scope | `test_contracts.py` |
| Contributor onboarding cost | at least 1493 Python and 46 Rust tests, no runtime dependencies, no build step | CI |
| **A finished item pointing at a section that has moved.** Five rows of the finished table cited "CHANGELOG Unreleased" — true on the day each was written, and false the moment 0.6.0 was cut, because promotion renames the heading and the citations do not follow. The table then sent the reader with the best reason to check where a feature shipped to somebody else's release notes | A finished item must cite a released version, and every version it cites must be a section the CHANGELOG actually has. Enforced at the moment the drift enters — cutting a release runs the check — rather than written down as another step in RELEASING §1.2, which is prose and cannot fail | `test_contracts.py::TestTheRoadmapCitesWhereWorkLanded` |

### Not mitigated, deliberately

*Every entry below re-checked 6 Aug 2026 and still true, mechanically rather
than by re-reading the prose: `peer_ratio_history(hour_is_local=False)` is
still the default, `secure_path` still applies `0o600`/`0o700`, and the
PurpleAir accuracy disclosure — which is the whole of that risk's mitigation —
is still enforced by `test_the_purpleair_accuracy_disclaimer_is_conditional`.
An accepted risk that quietly stopped being accepted is worse than an open one.*

- **DST.** Narrower than it was. Day-bucketing steps by calendar days rather
  than fixed milliseconds, and hour-of-day *reporting* now converts each
  bucket from its own date — that second one was listed here as accepted
  until a test run in Los Angeles showed `agreement --by-hour` naming every
  hour wrongly for half the year, which is the finding itself misplaced. It
  has moved to the register above.

  What remains accepted: `peer_ratio_history()`'s default selects by **UTC**
  hour, which is what the live corroboration check in `poller.py` wants — it
  asks about the hour happening now and compares like with like. Across a
  changeover, ninety days of "the same UTC hour" spans two local hours, so
  the comparison smears by an hour twice a year. Changing it would change a
  safety-critical input to fusion on the strength of an argument nobody has
  measured yet; the honest position is that it is known, bounded, and not
  worth a blind fix.
- **Consumer sensor accuracy.** PurpleAir over-reads 20–40% in humidity. This
  is disclosed at the point the number is shown rather than corrected, because
  a correction factor is a claim of its own.
- **A malicious local user.** Anyone with your account can read `~/.airo`. File
  permissions are the boundary; encrypting at rest against an attacker who
  already has your login is theatre.

---

## Planning that is not published

Commercial strategy and the legal analysis of third-party terms are not
published. The *obligations* arising from those terms are
public — see [LICENSING.md](LICENSING.md), enforced by
`tests/test_obligations.py`. What is withheld is reasoning and negotiating
position, not anything a user or contributor needs.

The risk register above is published on purpose: every row is mitigated and
every mitigation is enforced by a named test.

## Explicitly not doing

- **Making the local tool depend on a server.** It works completely offline with
  no account, and that is not a stage it passes through. A hosted option may
  exist one day; if it ever does it is additive, and nothing here waits on it.
- **Telemetry or analytics in the local tool.** Ever.
- ~~**Bundling a Python runtime.**~~ **Reversed, 3 Aug 2026.** The original reasoning —
  macOS ships one, so bundling adds tens of MB for nothing — is true for a developer and
  false for the audience the installer is for. `python3: command not found`, or a version
  too old for the syntax, is where someone with no technical knowledge stops for good, and
  no README wording recovers them. The cost is real and now ours: a shipped interpreter
  means its security updates are our problem, so the build pins a version, records the
  source and licence, and verifies a checksum. Hard rule 1 is untouched — it forbids Python
  *packages*, and it still does. See [ARCHITECTURE §4a](ARCHITECTURE.md).
- **A frontend framework.** The dashboard is one file with no build step, deliberately.
- **Accepting a write-scoped API key.** Read-only, always.
- **Rewriting the measurement code in Rust** for a single binary. See DECISIONS D13.
