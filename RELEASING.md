# Releasing

For maintainers. Two procedures live here: cutting a versioned release, and publishing the
repository. They are written down because both are rare, both are irreversible in the
directions that matter, and a procedure performed once a quarter from memory is a procedure
performed differently every time.

Everything below names the command that enforces a step rather than the intention behind it.
A checklist item you cannot run is a wish.

---

## 1. Cutting a version

### 1.1 The version numbers

Five places carry the project's version. Change all of them in one commit — a release where
the app reports one number and the installer another is not a release, it is a bug report
waiting to be written badly.

| Where | What it is |
|---|---|
| `poller.py` — `VERSION` | **Canonical.** The Python side derives everything from it: `poller.USER_AGENT`, the `User-Agent` the providers are sent, and `setup.py`'s geocoding request. Change it here and those follow. |
| `weather.py` — `USER_AGENT` | The one hand-copied literal. `weather.py` is a leaf that `poller` imports, so importing back would be a cycle, and the copy is deliberate. It said `airo/0.5` through the whole of a release before a contract test existed; now `test_contracts.py` asserts `poller.VERSION` appears in it, so forgetting this line is a red test rather than a stale string on somebody else's rate limiter. |
| `tray/Cargo.toml` — `version` | The crate. |
| `tray/Cargo.lock` — the `airo-tray` entry | Not edited by hand. Any `cargo` command in `tray/` refreshes it; commit the result, or the next build produces a diff nobody asked for. |
| `tray/tauri.conf.json` — `version` | What the bundler stamps into the artefact filenames, so this is the number a user sees on the file they downloaded. Two version numbers for one program is one too many; until that is fixed, this is the second one. |

Then `CHANGELOG.md`, which is §1.2.

**Three numbers a grep for `VERSION` finds that are not this.** Each moves on its own
schedule and changing it with a release would be wrong:

- `tools/fetch_runtime.py` — `VERSION` is the CPython interpreter that ships inside the app.
- `store.py` — `SCHEMA_VERSION` is the database schema. It belongs to migrations.
- `backup.py` — `FORMAT_VERSION` is the archive format, and a reader of an old archive
  depends on it not moving.

### 1.2 Promoting the CHANGELOG section

The file follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
claims [Semantic Versioning](https://semver.org/spec/v2.0.0.html), so the version number is
an assertion about compatibility and not a decoration.

1. Rename `## [Unreleased]` to `## [X.Y.Z] — YYYY-MM-DD`.
2. Open a fresh, empty `## [Unreleased]` above it. Leaving it out is how the next change
   ends up appended to a released section.
3. Read the promoted section as a user would. Entries written during development describe
   the change to a person who knew the old behaviour; a release note is read by someone who
   did not.

The footer is a set of links, and the repository's convention is not the one Keep a
Changelog's example uses. `[Unreleased]` is a **compare** link from the newest tag to `HEAD`;
every released version is a **tag** link:

```
[Unreleased]: https://github.com/Donnishcomau/airo/compare/vX.Y.Z...HEAD
[X.Y.Z]: https://github.com/Donnishcomau/airo/releases/tag/vX.Y.Z
```

So promotion means two edits down there: repoint `[Unreleased]` at the new tag, and add the
new version's tag line at the top of the list.

These links are a promise about tags that exist. A compare link whose base tag was never
pushed renders as an ordinary link and resolves to nothing, which reads as a broken changelog
rather than a missing tag. Verify them against the published repository — not locally, where
a tag you have not pushed still exists.

### 1.3 The tag

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

Annotated, so the tag carries its own author and date rather than borrowing the commit's.

The name is `vX.Y.Z` and nothing else. `.github/workflows/release.yml` triggers on `v*`, and
the CHANGELOG footer builds its links from the same string — a tag named differently produces
no build *and* a dead link, and neither failure announces itself.

Tag the commit you tested. Not `main` as it stands a few minutes later.

### 1.4 What CI then does on its own

`release.yml` runs on a tag and on nothing else. Deliberately: an installer built from an
arbitrary commit is one somebody will eventually hand to a user, and there would be no way to
say what is in it.

Three jobs, one per platform, each doing the same shape of work:

1. **The suite must pass before anything is published.** The job stops there if it fails, so
   a red test cannot become a download.
2. `python3 tools/fetch_runtime.py` — fetches the pinned Python runtime and verifies a
   recorded SHA-256 before extracting anything.
3. `python3 tools/stage_bundle.py` — assembles exactly what ships, and refuses to continue if
   the payload contains a config, a key, a database or a CSV. The runtime tree is copied
   wholesale, so this is the last automated thing standing between a stray file and a public
   artefact.
4. `cargo tauri build`, then a `.sha256` written beside every artefact.
5. Upload as a workflow artefact regardless, and attach to the release only for a tag —
   with `fail_on_unmatched_files: true`, so a platform that built nothing fails loudly
   instead of quietly publishing two downloads where the notes promise three.

A fourth job writes the release notes, and only after all three have attached their files, so
the notes cannot describe a platform whose build failed.

**Before tagging, if the packaging changed at all**, run the workflow by hand
(`workflow_dispatch`) on the commit you intend to tag. It builds everything and attaches
nothing. A workflow that only ever runs at release time is one whose breakage is discovered
at release time.

### 1.5 What CI cannot tell you, and you have to check by hand

`release.yml` labels two of its three jobs *untested*, and the release notes repeat it to the
reader. That is not modesty. macOS on Apple Silicon is the only platform anyone has installed
and used; Windows and Linux are built so those users have something to download and so build
breakage surfaces in CI rather than in a bug report. A green workflow does not change that,
and neither does a tag.

After the release page exists:

- **Download each artefact from the release page** and check it against the `.sha256` beside
  it: `shasum -a 256 -c Airo_*.dmg.sha256`. CI hashed the file it had just built and
  uploaded the two together, so a match proves the artefact you are holding is the one CI
  produced — the transfer, not the build. Whether that build is any good is what the rest of
  this list is for.
- **Install the macOS build on a machine that has never had Airo on it.** Open the disk
  image, drag it to Applications, right-click → *Open*, and confirm the Gatekeeper path the
  notes describe is the one you actually get. The build is unsigned; those two sentences in
  the notes are the whole difference between a first launch and a user concluding it is
  malware.
- **Read the notes against what was built.** They name Apple Silicon and say Intel is not
  supported; the macOS runner is pinned rather than `latest` precisely because `latest` has
  moved architecture before, and a notes file describing the wrong one is worse than none.
- **Click every link in the CHANGELOG footer**, on the published repository.
- The artefacts that should be there, and nothing else:

  | Platform | Files |
  |---|---|
  | macOS (Apple Silicon) | `Airo_*.dmg`, `Airo_*.dmg.sha256` |
  | Windows (x86_64) | `Airo_*.msi`, `Airo_*.msi.sha256` |
  | Linux (x86_64) | `*.deb`, `*.deb.sha256`, `*.AppImage`, `*.AppImage.sha256` |

- If someone has since installed a Windows or Linux build on real hardware, what changes is
  the *labelling* — the job names and the notes — not a checkbox here. Say what is true.

---

## 2. Publishing the repository

### 2.1 Why publication starts from a fresh root

CONVENTIONS hard rules 2b and 3, and DECISIONS D9, say that nothing of the user's enters the
repository: no real location, sensor id or coordinate, and no data, database, log, backup,
real config or key, under any name. What enforces them is a `.gitignore` that matches by
shape, a pre-commit hook, `tools/check.py`, CI, and `test_contracts.py`.

Every one of those acts on the working tree and on the commit being made. **None of them
reaches backwards.**

D9's own record is that the rule was written down and the repository drifted anyway — a
suburb in an example, a sensor index in a fixture, a site name in a test — because a
realistic example is easier to write than a synthetic one. Each was removed once it was
found. Removing a value from a file does not remove it from the commit that introduced it,
and the commit that removed it contains the value too, in its diff. A history that predates
its own enforcement is not made clean by the tree at its tip being clean.

That distinction costs nothing on any ordinary day and decides the shape of exactly one
event. So the repository is published from a fresh root: **one commit, containing the current
tree, with no parent.** Not a rewrite of the old history, not a filtered copy of it. Those
are attempts to prove a negative about every commit that came before, checked by hand,
once. The
tree you can read today is the only claim anyone can actually verify — and the whole of §3
verifies it.

### 2.2 The procedure

From a clone made for the purpose, not from a working checkout carrying build output and
scratch files:

```bash
git clone <this repository> airo-publish
cd airo-publish
./tools/install-hooks.sh

# Every gate in §3 goes here, and passes, before anything below runs.

git checkout --orphan public
git add -A
git commit -s -m "Airo X.Y.Z"
git ls-files                       # read this. it is exactly what becomes public.

git remote add public <public remote>
git push public public:main
git tag -a vX.Y.Z -m "vX.Y.Z" && git push public vX.Y.Z
```

### 2.3 The rules the steps are only one arrangement of

- **The contract suite passes before the push, never after.** `python3 -m unittest
  tests.test_contracts -v` is the personal-data gate. Afterwards, the only remedy is the one
  this entire procedure exists to avoid.
- **Read `git ls-files`; do not trust the ignore rules.** `git add -A` adds everything not
  ignored, and the failure worth catching is a file nobody thought to ignore — which is the
  shape rule 3 already failed in once, when `data/` did not match `data.migrated-<timestamp>/`.
- **Hooks are per-clone.** A fresh clone has none until `./tools/install-hooks.sh` runs, and
  the initial public commit is the one commit in the project's life that most deserves to be
  checked by the same thing every other commit is.
- **Never push, mirror or force-push the pre-publication history to a public remote** — not
  as a branch, not as a tag, not temporarily. `git push --mirror` and `git push --all` carry
  every ref there is; name the branch and the tag explicitly, every time. A repository that
  has once held those objects can still serve them by SHA long after the ref is gone, and
  deleting a public repository does not reliably unpublish what was in it.
- **The public remote does not belong in the working clone.** Adding it there puts one typo
  between the two histories.
- **Tags made before the cut point at commits the published repository does not contain.**
  Creating them there anyway means a tag claiming to be a release and pointing at something
  that is not it. Tag what the published history actually holds, then check the CHANGELOG
  footer (§1.2) against the published repository and accept a link that does not resolve
  rather than a tag that lies.

---

## 3. The pre-release checklist

The same list serves a version and a publication; a publication additionally requires §2.
Run them in this order — the cheap ones fail fastest.

1. **Every gate CI runs, reported at once.**

   ```bash
   python3 tools/check.py
   ```

   Not `--fast` here. `--fast` skips the Rust build, which is the right trade while
   iterating and exactly the wrong one before a release that ships a Rust binary. Read the
   closing *Only CI can answer* list rather than the tick above it: that list is the honest
   half of the output.

2. **The suite on Linux**, which the machine above cannot run:

   ```bash
   docker run --rm -v "$PWD":/src:ro python:3.12 sh -c 'mkdir /tmp/airo && cd /src && tar cf - --exclude=__pycache__ --exclude="data.migrated*" --exclude=export --exclude=Airo.app --exclude="tray/target" . | tar xf - -C /tmp/airo && chmod -R a+rwX /tmp/airo && useradd -m runner && su runner -c "cd /tmp/airo && git config --global --add safe.directory /tmp/airo && python3 -m unittest discover -s tests"'
   ```

   As an unprivileged user, because root is not subject to the file modes several tests
   assert on and a run as root reports those tests as passing without having tested them.
   The image has no `node`, so the tests that check the pages' JavaScript skip there; they
   run on macOS. This covers the Python side on Linux and nothing on Windows — that gap is
   CI's and stays CI's.

3. **The personal-data gate:**

   ```bash
   python3 -m unittest tests.test_contracts -v
   ```

   The whole file, not a selected class. It carries the zero-dependency rule, the
   docs-match-the-code checks and the no-real-place scans together, and which of them a given
   change threatens is not something to decide by eye.

4. **Prove the guards still fail when the thing they guard is broken:**

   ```bash
   python3 tools/faultcheck.py tools/faults/personal-data.json
   python3 tools/check.py --faults
   ```

   A test that has never failed is a claim nobody has checked, and this repository has
   produced several tests that were green against the bug they were written to catch. Before
   a release is the moment that matters most: the guard being trusted here is the one nobody
   will be able to re-run against the artefact afterwards.

5. **The version numbers agree** — §1.1, all five, plus the CHANGELOG heading.

6. **The tag and the CHANGELOG links**:

   ```bash
   git tag -l 'v*'
   ```

   Every version with a footer link has a tag; every tag has a section. Then click the links
   on the published repository.

7. **The artefacts**, after the workflow finishes — the table in §1.5, plus a downloaded
   file checked against its own `.sha256`, plus release notes that describe what was actually
   built.

8. **CI green on the tagged commit**, on all three operating systems and both Python
   versions. A green run on one platform has repeatedly meant nothing here; every expensive
   failure in this project's recent history was platform-specific and green locally.
