## What this changes

<!-- One or two sentences. Link the issue or ROADMAP item if there is one. -->

## Why

<!-- The reasoning. ARCHITECTURE.md documents several decisions that look odd
     until you know what they're avoiding — if you're changing one, say why. -->

## How it was tested

<!-- Include manual verification. Some things can only be checked on a real machine. -->

- [ ] `python3 poller.py --doctor` passes on a real install
- [ ] Tested a sleep/wake cycle if backfill or scheduling changed
- [ ] Tested under a second timezone if date logic changed

## Checklist

- [ ] **No new runtime dependencies** (standard library only)
- [ ] No API key can reach a log, a print, or the repo
- [ ] Date handling uses local parts, not `toISOString()` (ARCHITECTURE §3.2)
- [ ] Band colour matches the *displayed* rounded value (ARCHITECTURE §3.4)
- [ ] Render steps stay in isolated `try/catch` blocks
- [ ] Docs updated — ARCHITECTURE.md for design changes, ROADMAP.md if an item closes
- [ ] CHANGELOG.md updated under Unreleased

## Does this require users to reinstall or re-register the background agent?

<!-- Yes/no. Anything touching the plist, the .app bundle or config schema does. -->
