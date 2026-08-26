# Decisions

Choices a reasonable contributor might otherwise undo, with what was rejected
and what would reverse them.

A decision without its rejected alternative is an assertion. A decision without
a reversal condition is dogma. Both invite a well-meaning pull request that
quietly removes something load-bearing — which has happened here, more than
once, which is why this file exists.

Reversals are welcome and are recorded rather than hidden: §2.5a below reverses
an earlier decision outright. The rule is that the reasoning survives, so the
next reader sees a decision and not a contradiction.

**The detail lives in [ARCHITECTURE.md](ARCHITECTURE.md).** This is the index a
contributor should read before proposing a change to any of it.

---

## D1 · The Python side has no runtime dependencies

**Chosen** — standard library only. No `pip install`, no virtualenv, no
lockfile.

**Rejected** — `requests`, `pandas`, a chart library. Every one would have
saved code.

**Why** — the installer ships a bare interpreter. A dependency breaks it for
everyone who installed the app rather than the checkout, and a supply chain is
a thing that can be compromised on somebody else's schedule. The Rust tray is
the single exception, and it renders rather than decides.

**Reversed by** — a dependency that the bundled runtime can carry, whose
absence degrades rather than breaks, and which earns more than it costs. None
so far has.

**Enforced by** — an AST check in CI. See ARCHITECTURE §2.4b.

---

## D2 · Raw µg/m³ is canonical; an index is derived for display

**Chosen** — store the concentration. Compute the index when showing it.

**Rejected** — storing the AQI alongside, which would make the dashboard
simpler and the daily aggregates cheaper.

**Why** — the same air produces different index numbers on different national
scales. A stored index is a number whose meaning depends on a setting that can
change, and changing it would silently rewrite history.

**Reversed by** — nothing plausible. If it ever happens, every stored index
needs the scale stored beside it, and a migration to re-derive.

**Enforced by** — `test_scales.py`.

---

## D3 · `StartInterval`, not a resident daemon

**Chosen** — the scheduler runs the poller every 15 minutes; it takes about
two seconds and exits. Between polls there is no process.

**Rejected** — a long-running service.

**Why** — nothing to crash, nothing to leak, nothing to restart after a
reboot, and no memory held for a job that is idle 99% of the time. It also
means an uninstall is a plist and a folder.

**Reversed by** — a requirement for sub-minute sampling, which no air-quality
use case here has.

**Note** — `poller.py --daemon` exists and loops, for a terminal or a system
whose scheduler nobody has taught it about. It is not the installed path.

---

## D4 · SQLite as the store, CSV as the export *(reverses the original CSV decision)*

**Chosen** — SQLite, from v0.5.

**Rejected, and originally chosen** — a CSV file, on the grounds that it needs
no library and anyone can open it.

**Why the reversal** — append-only CSV cannot dedup on ingest, and dedup is
what makes gap repair safe. Backfilling a night twice produced two of every
reading. Export remains CSV, because the original argument was right about
that part.

**Reversed by** — nothing; the migration is one-way and the export covers the
portability concern.

---

## D5 · Fusion is a decision, not a calculation

**Chosen** — one headline number, chosen by a stated rule (`nearest`,
`freshest`, `all`, `blend`), with its provenance carried alongside.

**Rejected** — averaging every source.

**Why** — averaging hides disagreement, and disagreement is information. Two
sensors a kilometre apart on this project's own record peaked at 87 and 102
AQI in the same hour. An average would have reported neither and flagged
nothing.

**Reversed by** — nothing. `blend` exists for those who want it, and says so.

**Enforced by** — `test_fusion.py`. See ARCHITECTURE §2.5b.

---

## D6 · A suspect reading is shown, not hidden

**Chosen** — flag it, exclude it from averages, keep drawing it.

**Rejected** — dropping readings that fail a quality check.

**Why** — if there is a fire next door, that is the air being breathed. A
sensor disagreeing with its neighbours is sometimes broken and sometimes the
only one telling the truth, and the two are indistinguishable at the moment of
reading. Rule 5a: nothing is silently discarded.

**Reversed by** — nothing.

**Enforced by** — `test_fusion.py`, ARCHITECTURE §2.5c.

---

## D7 · An indoor sensor never speaks for the air outside

**Chosen** — `placement` is `outdoor`, `indoor` or `unknown`, detected from
the provider where possible. Indoor sensors are excluded from the headline,
corroboration, the weather correlation, the forecast and outdoor alerts — and
shown prominently in their own right.

**Rejected** — a boolean, and treating unknown as outdoor.

**Why** — `nearest` is the default rule and a sensor in the house is ~0 km
away, so without this it wins every time and kitchen air is reported as the
street, under "avoid outdoor exertion", with alerts firing on cooking.
Treating unknown as outdoor is precisely how that happens by default.

**Boundary that matters** — the exclusion sits *below* `fusion.annotate()` and
*above* `fusion.fuse()`. Age, distance and staleness are facts about an
instrument; the headline is a claim about the air. Applying the exclusion too
high once left a dead indoor sensor showing its last reading undated forever.

**Reversed by** — nothing.

**Enforced by** — `test_indoor.py`. See ARCHITECTURE §2.5e.

---

## D8 · Health-relevant wording is decided in Python and served

**Chosen** — verdicts, advice and the grounds for both are computed server-side
and rendered verbatim by the surfaces.

**Rejected** — letting each surface phrase its own.

**Why** — inside-against-outside has two failure modes with opposite remedies.
Get it the wrong way round and somebody opens a window during a smoke event.
Three surfaces describing one relationship three ways is how they drift, and
one of those ways will be the wrong remedy.

**Reversed by** — nothing. This is hard rule 7.

**Enforced by** — `test_indoor.py`, `test_page_render.py`, a CI grep.

---

## D9 · Nothing of the user's enters the repository

**Chosen** — settings, readings, keys and logs all live in `~/.airo`, outside
the checkout. The maintainer's own working notes are not tracked at all — the ignore
rules refuse the whole folder they live in by shape.

**Rejected** — ignoring personal files individually, and trusting the rule.

**Why** — the rule was written down and the repository drifted anyway: a
suburb in an example, a sensor index in a fixture, a site name in a test. Each
arrived innocently, because a realistic example is easier to write than a
synthetic one. Ignoring a whole folder makes the safe thing the default.

**Reversed by** — nothing.

**Enforced by** — `test_contracts.py::TestNoRealPlaceIsCommitted` (coordinates,
sensor indices *and* site names), plus a `tools/check.py` gate that hashes the
real `~/.airo` before and after the suite.

---

## D10 · A guard is shown to fail when broken

**Chosen** — faults are committed under `tools/faults/` and CI reintroduces
each one on every push, asserting that a test goes red.

**Rejected** — trusting that a passing suite means a working guard.

**Why** — a test that has never failed is a claim nobody has checked, and this
repository has produced several tests that were green against the bug they
were written to catch.

**Reversed by** — nothing, though the gate's runtime is worth watching; it is
the long pole on every PR.

**Enforced by** — `test_faultcheck.py`. See ARCHITECTURE §7a.

---

## D11 · The pages are executed in tests, not only parsed

**Chosen** — `test_page_render.py` runs a page's own script against a payload
and asserts on the rendered cells.

**Rejected** — `node --check` alone, which is what existed.

**Why** — a page that parses is not a page that works. The server stopped
sending one field and the row rendered an em dash: correct for a missing
field, and completely wrong as an answer to "is this sensor collecting data?".

**Reversed by** — nothing. Node stays a development tool: the tests skip when
it is absent, the same bargain the syntax gate makes.

---

## D12 · The maintainer's own install runs from the checkout

**Chosen** — the generated `Airo.app` is a launcher that runs `poller.py` from
the working tree.

**Revised, Aug 2026** — the decision stands; the mechanism is gone. The shell
installer generated that launcher and was deleted when its work moved into
Python, and the bundle was the one part that did not move. `scheduler.py install` now points
`launchd` at `poller.py` directly, which runs from the working tree just the same
— so the property this entry chose is intact and the artifact it named is not.
Recorded rather than rewritten, because the gap between the two went unnoticed
for months. See ARCHITECTURE §2.2.

**Rejected** — bundling a copy of the Python into the app for local use.

**Why** — merging a fix reaches the running install without a rebuild, which
makes the feedback loop minutes rather than a release. The shipped installer
does bundle a payload; this is the developer's own arrangement.

**Reversed by** — shipping to users who are not the maintainer, at which point
the bundled payload is the only correct answer.

---

## D13 · The measurement code stays Python; Rust stays the shell

**Chosen** — every judgement about air — parsing a provider, fusing sources,
assessing quality, corroborating, phrasing a forecast — is Python. The Rust in
`tray/` renders `latest.json` and decides nothing, which CI enforces by grep.

**Rejected** — rewriting the poller in Rust for a single self-contained binary,
which would drop the install to one file with no interpreter to ship.

**Why** — the install-size win costs the whole test suite and the fusion,
sentinel and corroboration layers, which are the parts that took the longest to
get right and the parts a wrong answer would come from. It also puts
health-relevant wording behind a compiler, so the loop between noticing a bad
message and fixing it becomes a release. The interpreter it would save is
already pinned and checksummed (ARCHITECTURE §4a); a second implementation of the same
decisions in a second language is the cost this project has repeatedly refused
— see D5 and the three menu-bar widgets that became one.

**Reversed by** — nothing on the horizon. A platform that cannot run Python at
all would force it, and at that point the tray's own tests are the model:
Rust that renders a decision Python made, not Rust that makes one.
