# Contributing

Thanks for taking an interest. This project is deliberately small and hackable — the aim is
that a competent developer can read all of it in an afternoon.

This page is the front door. Everything a contributor needs is here or linked from here, and
each fact is kept in exactly one place: two copies of a rule is how they come to disagree.

## Read in this order

| # | Read | For |
|---|---|---|
| 1 | [CONVENTIONS.md](CONVENTIONS.md) | The hard rules, the traps that have already bitten, and the verification block to run before committing |
| 2 | [ARCHITECTURE.md](ARCHITECTURE.md) §1 | How the pieces fit together |
| 3 | ARCHITECTURE §2.5a, §2.5b, §2.5c, §2.5e | The four decisions most often undone by accident: why SQLite, why fusion is a decision, why a flagged reading is shown rather than hidden, and why an indoor sensor never speaks for the air outside |
| 4 | ARCHITECTURE §3 | Bugs that have shipped once. Each is easy to reintroduce |
| 5 | ARCHITECTURE §7a | How this is tested, and the mistakes the test suite is shaped around. Read before writing a test here |
| 6 | [ROADMAP.md](ROADMAP.md) | What is planned, what is deliberately not being done, and the risk register |

---

## Licensing — please read before opening a PR

Airo is **dual-licensed**: [AGPL-3.0-or-later](LICENSE) for everyone, plus a commercial
licence for people who need different terms. See [LICENSING.md](LICENSING.md).

For that to work, contributions must be usable under **both** licences. Sign off each commit:

```bash
git commit -s          # appends: Signed-off-by: Your Name <your@email>
```

That line records your agreement to **[CLA.md](CLA.md)** — one page, and the whole of it is
worth reading. The short version:

| | |
|---|---|
| **You keep** | Your copyright. This is a licence, not an assignment. You can reuse your own work anywhere, under any terms, forever. |
| **You grant** | A perpetual, irrevocable, **sublicensable** licence, plus a patent licence limited to your contribution. |
| **We undertake** | The AGPL version never goes away. The commercial licence exists alongside it, never instead of it. |

CI checks every commit in a pull request and fails with instructions if a sign-off is missing
or names someone other than the author. [CLA.md](CLA.md) has the fix for a branch you have
already pushed, and the ways to help that involve no licence grant at all if you would rather
not sign — say so in the issue or PR before doing the work rather than after.

---

## The rules

The hard rules — no runtime dependencies, never lose a reading, never log a key, nothing of
the user's in the repository, raw µg/m³ canonical, no air-quality logic in the tray — are
numbered in [CONVENTIONS.md](CONVENTIONS.md) under **Hard rules**, which is the only copy;
tests and CI failure messages cite those numbers.

---

## Development setup

No virtualenv, no install step. Clone it and run it.

```bash
git clone https://github.com/Donnishcomau/airo.git
cd airo
./tools/install-hooks.sh            # first, and once per clone
python3 poller.py --list-sources    # works before any configuration
python3 -m unittest discover -s tests -v
```

The hook is the user-data guard: it refuses a database, a log, a real config or a key
*before* it becomes a commit somebody has to rewrite history to remove. Hooks are per-clone
and cannot be committed, so nothing installs it for you.

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

What each file does is the file map in **ARCHITECTURE §6**.

---

## Running the tests

```bash
python3 -m unittest discover -s tests -v
python3 tools/check.py             # every gate CI runs, reported at once
python3 tools/check.py --fast      # skips the Rust build, which is the part worth skipping
```

It is a large suite — ARCHITECTURE §7a says how it is built and what it is shaped around.
Budget **about two minutes** for the Python side on a current laptop; it said "under fifteen
seconds" for a long time, which was true of a suite a fraction of this size and is the kind of
claim that makes a contributor think something has hung. There is no fast subset. CI runs
everything on Ubuntu, macOS and Windows across Python 3.9 and 3.12.

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

## Building the installer

Needs Rust and the Tauri CLI:

```bash
python3 tools/fetch_runtime.py       # the Python interpreter that ships inside the app
python3 tools/stage_bundle.py        # assemble exactly what ships
cd tray && cargo tauri build
```

Cutting a version and publishing the repository are in [RELEASING.md](RELEASING.md).

---

## Adding a data source

One class in `poller.py`, nothing else. `QldProvider` is the reference implementation for a
government feed; copy it.

You must declare `resolution_minutes` (gap detection depends on it), `needs_key`,
`attribution` and `licence`. The licence string is surfaced to users in `--list-sources`, so
get it right — check whether the source is a single-licence feed or an aggregator with
per-station terms.

The data Airo retrieves is **not** covered by the code licence, and the terms differ per
source — PurpleAir's forbid redistribution outright. Read
[LICENSING.md](LICENSING.md) before adding any provider, and CONVENTIONS' trap list before
trusting a feed's query parameters.

---

## Pull requests

- One concern per PR.
- Explain the *reasoning*, not just the change. Commit messages in imperative mood.
- Run the verification block in [CONVENTIONS.md](CONVENTIONS.md) before pushing.
- If you change behaviour a user could notice, update the README in the same PR.
- Merging needs one approving review and every required CI check green. `main` is protected;
  there is no other route in.

Issues tagged `good first issue` are a reasonable entry point once the tracker
has some; until then, [ROADMAP.md](ROADMAP.md) says what is open and why.
