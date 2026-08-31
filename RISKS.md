# Risks

**The register itself is in [ROADMAP.md § Risk register](ROADMAP.md#risk-register)**
— every row naming the test that enforces it.

One register, not two. A second copy would drift from the first, and the reader
would have no way to tell which had. This page is the entry point and the
explanation; the rows live there because that is where a contract already
checks them.

## How to read a row

Each row is three things: **what could go wrong**, **what stops it**, and
**which test fails if that stops working**.

The third column is the one that matters. A risk with no enforcing test is a
wish, and `tests/test_contracts.py` fails the build if a row names a test that
does not exist — a check added after the register's own citations had fallen
three modules behind.

## What goes in it

Incidents, not imagination. Almost every row in the register is something that
actually happened, usually to the maintainer's own install:

- an indoor sensor reporting kitchen air as the street
- a sensor dark for two days behind a provider that kept answering
- 16,995 rows of one person's readings committed, because the ignore rule
  named `data/` and the directory was `data.migrated-20260802-150345/`
- three backup archives destroyed by a test, because a module resolved a
  home-relative path at import time
- a page that parsed, passed, and rendered the wrong thing

A hypothetical risk with a plausible mitigation and no test is how a register
becomes decoration. If you cannot name the test, the row is not ready.

## Adding one

1. Write what went wrong, concretely. Dates and numbers, not categories.
2. Write what now prevents it — the mechanism, not the intention.
3. Name the test. Then break the code and watch that test fail:
   `python3 tools/faultcheck.py tools/faults/<spec>.json`.
4. Add the fault to `tools/faults/` so CI keeps asking.

## The standing ones worth knowing before you contribute

| | |
|---|---|
| **Personal data reaching the repository** | The most likely thing a pull request does wrong here, because a realistic fixture is easier to write than a synthetic one. Use `pa-1`, `oaq-1`, `Riverside`, `Northfield` — never a number that could be a real sensor index. Enforced by `TestNoRealPlaceIsCommitted`. |
| **A test that has never failed** | Not evidence. Fault-inject it. |
| **A helper tested while its call site is wrong** | Five occurrences. Cover the call site, or write a journey through the real entry point. |
| **A green run on one platform** | CI runs macOS, Linux and Windows on two Python versions, and has repeatedly caught what a green local run did not. |
| **Health-relevant wording in a renderer** | Two failure modes with opposite remedies. Decide in Python; render verbatim. |

## Security

Vulnerability reporting and the threat model are both in
[SECURITY.md](SECURITY.md) — one canonical copy, held against the code by a
test that fails if a host the source can reach goes unnamed. Neither belongs in
the register: the register is about being *wrong*, not about being *attacked*.
