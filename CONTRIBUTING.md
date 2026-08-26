# Contributing

Thanks for taking an interest. This project is deliberately small and hackable — the aim is
that a competent developer can read all of it in an afternoon.

Please read [ARCHITECTURE.md](ARCHITECTURE.md) first, especially **§3 Traps and hard-won
knowledge**. Every item there was a real bug that shipped, and several are easy to
reintroduce.

---

## Licensing — please read before opening a PR

Airo is **dual-licensed**: [AGPL-3.0-or-later](LICENSE) for everyone, plus a commercial
licence for people who need different terms. See [LICENSING.md](LICENSING.md).

### What you agree to, and why

For dual licensing to work, contributions must be usable under **both** licences. Sign off
each commit:

```bash
git commit -s          # appends: Signed-off-by: Your Name <your@email>
```

That line records your agreement to **[CLA.md](CLA.md)** — one page, worth reading. The short
version:

| | |
|---|---|
| **You keep** | Your copyright. This is a licence, not an assignment. You can reuse your own work anywhere, under any terms, forever. |
| **You grant** | A perpetual, irrevocable, **sublicensable** licence, plus a patent licence limited to your contribution. |
| **We undertake** | The AGPL version never goes away. The commercial licence exists alongside it, never instead of it. |

**Why "sublicensable" is the word that matters.** Offering someone a commercial licence means
granting rights over *all* the code, including your contribution. Something contributed only
under the AGPL cannot be included in a commercially licensed copy — the project would have no
right to relicense it — and a single such contribution anywhere in the tree removes that option
for the whole project.

**Why it is asked before the first merge rather than later.** Consent cannot be retrofitted.
Afterwards it means tracking down every contributor — including ones who have changed jobs,
changed addresses or lost interest — and getting each to agree, with the only alternative being
to remove their work.

CI checks every commit in a pull request and fails with instructions if a sign-off is missing
or names someone other than the author. To fix a branch you have already pushed:

```bash
git rebase --signoff origin/main
git push --force-with-lease
```

### If you would rather not

That is a legitimate position and it will not be held against you — say so in the issue or PR
rather than quietly not signing, and say it before doing the work rather than after.

There is still plenty that involves no licence grant at all: bug reports, reproductions,
documentation corrections filed as issues, testing on a platform we lack, and reviewing other
people's pull requests. Small fixes can often be handled another way.

> The data Airo retrieves is **not** covered by the code licence, and the terms differ per
> source — PurpleAir's forbid redistribution outright. See [LICENSING.md](LICENSING.md)
> before adding any provider.

---

## Ground rules

1. **No new Python runtime dependencies.** Standard library only, in Python and in shell.
   A tool that must survive a year of neglect can't have a supply chain. CI enforces this
   with an AST check over every import. The Rust tray in `tray/` is the one deliberate
   exception — it's a separate, optional binary.
2. **The poller must never lose data.** Any change to `store.insert_readings()`,
   `backfill_source()` or `do_poll()` needs a test proving gaps are still detected and
   repaired. See `tests/test_store.py::TestGapRepair`.
3. **Never silently discard a reading.** Faults and uncorroborated readings are *flagged and
   shown*, never hidden. If there's a fire next door, that's genuinely the air being
   breathed — suppressing it is the more dangerous error. See ARCHITECTURE §2.5c.
4. **Raw µg/m³ is canonical.** Never store a derived AQI as the source of truth. The same air
   gives very different index numbers on different national scales.
5. **Never log, print or commit an API key.** See [SECURITY.md](SECURITY.md).
5a. **No real location, sensor id or coordinates in the repo.** Settings live in
   `~/.airo/config.json`; the repo ships `config.example.json` with empty values. Test
   fixtures use a synthetic coordinate frame. CI fails if `config.json` is ever tracked.
6. **No air-quality logic in the tray.** It renders `latest.json`. A threshold or band
   boundary implemented in Rust is a second copy of a health-relevant decision, free to drift
   out of step with the dashboard.
7. **Fail loudly on data, quietly on cosmetics.** A bad reading should be surfaced; a chart
   that won't render should not take the page down with it.

---

## Development setup

No virtualenv, no install step. Clone it and run it.

```bash
git clone https://github.com/Donnishcomau/airo.git
cd airo
python3 poller.py --list-sources    # works before any configuration
python3 -m unittest discover -s tests -v
```

To develop without touching your installed agent or your real data, point the module at a
scratch directory:

```python
import poller, store
from pathlib import Path

tmp = Path('/tmp/airo-dev'); tmp.mkdir(exist_ok=True)
poller.DATA        = tmp
poller.LATEST_PATH = tmp / 'latest.json'
poller.LOG_PATH    = tmp / 'poller.log'
poller.CONFIG_PATH = tmp / 'config.json'
conn = store.connect(tmp / 'airo.db')
```

Stop the installed agent while working on scheduling:

```bash
python3 scheduler.py uninstall     # any platform
```

---

## Layout

| Path | Role |
|---|---|
| `poller.py` | Providers, polling, fusion wiring, alerting, HTTP server + JSON API, CLI |
| `store.py` | SQLite schema, ingest, dedup, series/bucketing, export, quality assessment |
| `fusion.py` | Choosing one number from several sources, and corroboration |
| `scheduler.py` | Cross-platform scheduling: launchd, systemd timers, Task Scheduler |
| `dashboard.html` | Single-file UI, no build step |
| `tray/` | Tauri tray for macOS/Windows/Linux |
| `setup.py` | First-run wizard: geocode, discover nearby monitors, write the config |
| `tests/` | `unittest`, no dependencies |

---

## Testing

```bash
python3 -m unittest discover -s tests -v
```

At least 1493 Python tests plus 46 Rust tests, no dependencies. Budget **about two minutes**
for the Python suite on a current laptop — it said "under fifteen seconds" for a long time,
which was true of a suite a fraction of this size and is the kind of claim that makes a
contributor think something has hung. There is no fast subset; `python3 tools/check.py --fast`
runs every gate except the Rust build, which is the part worth skipping while iterating.
CI runs them on Ubuntu, macOS and Windows across Python 3.9 and 3.12.

**A test that passes on both the fixed and the broken code is worthless.** When fixing a bug,
reintroduce it and confirm the test fails before you commit. The DST tests in
`tests/test_dates.py` were verified this way — 6 of 8 fail against the old fixed-millisecond
tick stepping.

Some tests shell out to `node` to exercise the dashboard's JavaScript under two timezones.
They skip cleanly when Node isn't installed rather than failing.

### What needs covering

| Area | Where |
|---|---|
| Scale conversion, all three scales | `test_scales.py` |
| Config migration across three shapes | `test_scales.py` |
| Ingest, dedup, gap repair, migration, export | `test_store.py` |
| Fusion rules, staleness, corroboration | `test_fusion.py` |
| Date bucketing under DST | `test_dates.py` |

Live API calls are never made in tests. Mock at the provider boundary:

```python
class FakeProvider(poller.Provider):
    slug, needs_key, resolution_minutes = "fake", False, 10
    def current(self, src, key):
        return {"headline": 12.0, "now": 12.0}, {"site_id": src["site_id"]}
    def history(self, src, key, start, end):
        return [{"utc": start, "pm25": 12.0}]
```

---

## Adding a data source

One class in `poller.py`, nothing else. `QldProvider` is the reference implementation for a
government feed; copy it.

You must declare `resolution_minutes` (gap detection depends on it), `needs_key`,
`attribution` and `licence`. The licence string is surfaced to users in `--list-sources`, so
get it right — check whether the source is a single-licence feed or an aggregator with
per-station terms.

**Verify your date filtering actually filters.** The QLD API silently ignores unknown query
parameters and returns the most recent 1000 rows instead of the window you asked for — wrong
data, no error. Assert on the returned range in a manual check before trusting it.

---

## Pull requests

- One concern per PR.
- Explain the *reasoning*, not just the change. Commit messages in imperative mood.
- Run the verification block in [CONVENTIONS.md](CONVENTIONS.md) before pushing.
- If you change behaviour a user could notice, update the README in the same PR.

Issues tagged `good first issue` are a reasonable entry point once the tracker
has some; until then, [ROADMAP.md](ROADMAP.md) says what is open and why.

Maintainers: cutting a version and publishing the repository are in
[RELEASING.md](RELEASING.md).
