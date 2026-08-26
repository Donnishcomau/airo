---
description: Validate open PRs against their acceptance criteria, and keep them honest
---

# Watch the open PRs

Run in a **separate session** from whoever is writing the code. That separation
is the point: an author checking their own work against their own criteria
grades a paper they wrote. This session's job is to be the reader who was not
there.

Paired with `/goal`. That one produces work; this one refuses to let it land
until it does what it claimed.

## What you may and may not do

**You may**: read anything, run the full verification, run the fault gate,
comment on a PR, and say plainly that something does not meet its criteria.

**You may not**: merge, close, approve, force-push, rewrite history, change
branch protection, or edit the PR's own branch. If a PR is wrong, say why. The
author fixes it.

That restriction is deliberate. A reviewer who can also fix will fix, and then
nobody has reviewed it.

## Each pass

### 1. What is open

```
gh pr list --state open --json number,title,headRefName,isDraft
```

Nothing open is a valid result. Say so and stop; do not go looking for work.

### 2. Find the acceptance criteria

In order: the PR body's own "done when" list, the issue it closes, the `/goal`
or command file that produced it, `ROADMAP.md`.

**A PR with no discoverable criteria is the finding.** Say so, and ask for
them. "It looks fine" is not a review — without criteria there is nothing to
check against, only taste.

### 3. Check the claims against the tree, not the description

Read the diff. Then, for each criterion, find the thing that makes it true.
The PR body is the author's account of the diff and is not evidence.

Ask, in this order:

- **Does a test cover it, and would that test fail if the code were wrong?**
  Use the fault gate — `python3 tools/faultcheck.py tools/faults/<spec>.json`.
  A test that has never failed is a claim nobody has checked.
- **Is the call site covered, or only the helper?** Five bugs in this
  repository were a fully tested helper whose caller passed the wrong thing.
  The most recent left a sensor dark for two days in silence.
- **Does an assertion check the property, or something next to it?** A row
  count that a legitimate backfill changes. Text decoded with the wrong
  encoding. Both passed locally and meant nothing.
- **Could this test pass while reading nothing?** A harness that renders an
  empty string satisfies every `assertNotIn` in the file.
- **Is any claim in a document verified?** Counts drift and nothing fails.

### 4. The checks, on the head commit

`gh pr checks --watch` has reported results from a *previous* run for a commit
whose checks had not started. It nearly produced a merge on stale evidence.
Verify against the SHA:

```
HEAD=$(gh pr view <n> --json headRefOid -q .headRefOid)
gh api repos/{owner}/{repo}/commits/$HEAD/check-runs \
  -q '.check_runs[] | "\(.conclusion)\t\(.name)"'
```

Green on an earlier commit is not green.

### 5. Personal data — every PR, no exceptions

The repository is public. A pull request is the way personal data gets in,
because a realistic fixture is easier to write than a synthetic one, and it
has happened repeatedly.

```
python3 tools/check.py    # the contracts run inside this
```

Then read the diff for what a contract cannot catch: log excerpts carrying a
sensor index, a screenshot filename, a real path, a new file at the root that
should be under `private/`.

**A single real identifier is a blocking finding**, whatever else the PR does.
Say it first and say it plainly. Removing it from the branch is not enough if
it has already been pushed — flag that, and leave the decision to the
maintainer.

### 6. Report

Per PR, short:

- **Meets / does not meet**, criterion by criterion
- **What is missing**, specifically enough to act on
- **What you verified and how** — "fault-injected, went red on
  `test_x`" beats "looks tested"
- **What you could not check**, and why

Say when a PR is good. A reviewer who only ever objects gets ignored, and then
the one that matters is ignored too.

### 7. Drift

A PR that has grown past its criteria is a finding, not a bonus. Say what has
been added and ask whether it belongs here or in its own PR. The reasons are
the project's own: PRs here conflict on test-count lines when they touch the
same files, and a diff nobody can hold in their head is a diff nobody reviews.

## Pacing

Self-paced. After each pass, decide whether to continue and say which.

Do not poll for CI — the notification arrives on its own. When waiting on a run,
wait on the run; a wake-up every minute to re-read the same pending state is
noise.

Stop and ask when:

- a PR's criteria and its diff disagree and you cannot tell which is intended
- a finding would require the author to relax a contract to satisfy you — that
  is a decision for a human, and the answer is usually that the contract is
  right
- personal data has already been pushed
- there is nothing open
