---
description: Refresh the working-knowledge documents from the code and the record
---

# Refresh the working knowledge

Rewrite four documents so they describe what is actually true today, and stop
the public repository carrying anything personal.

Two audiences, and the split is the whole point:

| Where | For | Holds |
|---|---|---|
| the repository | anyone on the internet | `ARCHITECTURE.md`, `DECISIONS.md`, `RISKS.md`, `ROADMAP.md` |
| `private/` (gitignored, and only in a maintainer checkout) | the maintainer alone | `TODO.md`, session logs, install snapshots, anything naming a real sensor or place |

A contributor needs the architecture, the decisions and the risks. Nobody
outside needs the maintainer's todo list, and nobody at all should be able to
read a real sensor index out of this repository.

## Before writing anything

**Check, do not remember.** Every count, every claim and every "this is
enforced by" is a statement about the code as it stands, and the last review
found a document that had opened with "there is no committed test suite yet"
for months while 1,760 tests ran on every push. A document that states
something false is worse than one that is silent, because the reader believes
it.

- `python3 tools/check.py` — green before you start, or you are describing a
  broken tree
- `git log --oneline -30` — what has actually landed since the last pass
- the risk register in `ROADMAP.md` — which rows have been closed
- `private/session-logs/` — what went wrong, which is the part worth keeping. Only if
  `private/` exists; a public clone has none, and that is not a missing step

## The four documents

### `ARCHITECTURE.md` — how it fits together, and why

Structural decisions and the reasoning behind them. Update the sections that
have drifted; do not rewrite what is still true.

- every shipped module appears in the layout table (a contract enforces this)
- every stated count is current (a contract enforces this too, and reads
  thousands separators)
- a new subsystem gets a section, not a sentence — `placement`, `units` and
  the fault gate each went months with no explanation, and each is something a
  contributor could undo by accident
- §7a is where somebody learns how this is tested before writing a test

### `DECISIONS.md` — what was decided, when, and what would reverse it

One entry per decision that a reasonable contributor might otherwise undo.
Each carries: the date, what was chosen, **what was rejected and why**, and
what evidence would change it.

A decision without its rejected alternative is an assertion. A decision
without a reversal condition is dogma. Both invite a well-meaning pull request
that quietly undoes something load-bearing.

Reversals stay: strike the old entry through and write the new one beneath it,
with the reasoning. `§2.5a` reversing the CSV decision is the model.

### `RISKS.md` — what could go wrong and what stops it

The register lives in `ROADMAP.md` today. Keep it there or move it here, but
not both: two registers drift and the reader trusts the wrong one.

Every row needs a **naming test**. A risk with no enforcing test is a wish. A
row whose test no longer exists is worse than no row, and a contract already
checks for that.

New rows come from what actually happened, not from imagination. The best
rows in the register are all incidents.

### `private/knowledge/TODO.md` — the maintainer's own list

Only if `private/` exists (a maintainer checkout). Skip this document entirely
in a public clone rather than creating it.

Grouped by who is blocked:

- **ready to pick up** — anyone could start these
- **blocked on the maintainer** — accounts, hardware, decisions only they can make
- **out of scope by instruction** — say why, so it is not re-proposed

Public work belongs in `ROADMAP.md` or an issue, where a contributor can see
it. This file is for the rest.

## The sweep — every time, not once

Personal data has reached this repository repeatedly, and always the same way:
a realistic example is easier to write than a synthetic one.

```
python3 tools/check.py          # the contracts run inside this
```

`tests/test_contracts.py::TestNoRealPlaceIsCommitted` enforces:

- **coordinates** outside the synthetic frame
- **sensor indices** — a numeric site id in a fixture is a pin on a public map
- **site names** outside the synthetic vocabulary

If it fails, fix the fixture. Do not add to the allowlist unless the real value
is genuinely the subject of the test, and then say so in the file.

Then look where the contracts cannot:

- new `.md` files at the root — is this public or `private/`?
- log excerpts pasted into a document — they carry sensor indices
- screenshots and their filenames
- `git log -S"<a real identifier>"` if anything looks doubtful

**History is not covered by any of this.** Removing an identifier today leaves
it in every commit that carried it. If something has leaked, say so plainly and
leave it for the maintainer: rewriting history is not an agent's call.

## Definition of done

- [ ] `python3 tools/check.py` green, twice
- [ ] every count and claim in the four documents verified against the code
- [ ] no real sensor index, coordinate, site name or install path in any
      tracked file — checked, not assumed
- [ ] `private/` still ignored: `git check-ignore private/README.md` answers
- [ ] `git status` shows nothing personal staged
- [ ] each new risk row names a test that exists
- [ ] each new decision names what it rejected and what would reverse it

## May not

- weaken, narrow or delete a contract to make the sweep pass
- move a public document into `private/` — contributors need the architecture,
  the decisions and the risks
- commit anything under `private/`
- rewrite git history, force-push, or change branch protection
- publish, tag or release
