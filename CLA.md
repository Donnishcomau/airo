# Contributor Licence Agreement

**This is the agreement your `Signed-off-by:` line refers to.** One page, and
the whole of it matters, so it is worth the two minutes.

> **Not legal advice, and not yet reviewed by a lawyer.** This document was
> drafted to be honest and readable rather than to be bulletproof. Before the
> first external contribution is merged it should be reviewed by someone
> qualified — the item is in
> [ROADMAP's finished table](ROADMAP.md#where-the-finished-items-went), with the
> review itself still outstanding. If you are contributing on
> behalf of an employer, check with them regardless of what this says.

---

## Why this exists at all

Airo is dual licensed: [AGPL-3.0-or-later](LICENSE) for everyone, and a
separate commercial licence for organisations that cannot accept the AGPL's
network-copyleft obligation. That second licence is what could fund the
project's maintenance.

Offering someone a commercial licence means granting them rights over *all* of
the code, including your contribution. A contribution offered only under the
AGPL cannot be included in a commercially licensed copy, because the project
would have no right to relicense it. One such contribution anywhere in the
tree makes the commercial option impossible for the whole project.

So the choice is not "CLA or no CLA". It is "a licence grant broad enough to
dual licence, or no dual licence at all".

**Retrofitting this is the part that actually hurts.** Consent has to be
collected before code is merged; afterwards it means finding every contributor,
including ones who have changed jobs, changed email addresses, or lost interest,
and getting each of them to agree — with the alternative being to rip their work
out. That is why this is in place before the first external pull request rather
than after.

## What you keep

**You keep your copyright.** This is a licence, not an assignment. You can use
your own contribution anywhere else, under any terms you like, forever. Nothing
here is exclusive and nothing here takes anything away from you.

## What you grant

By adding `Signed-off-by:` to a commit you confirm, for that contribution:

1. **It is yours to give.** You wrote it, or you otherwise have the right to
   submit it — including permission from your employer if your contract assigns
   what you write to them.

2. **A copyright licence.** You grant Donnish Pty Ltd and every recipient of the
   project a perpetual, worldwide, non-exclusive, royalty-free, irrevocable
   licence to reproduce, modify, publicly display, sublicense and distribute
   your contribution and works derived from it.

   *"Sublicense" is the word that makes dual licensing possible.* It is what
   allows the same code to be offered under the AGPL to everyone and under a
   commercial licence to those who need one. Without it, item 3 below cannot
   happen.

3. **Permission to offer it commercially.** You agree your contribution may be
   included in copies of Airo distributed under Donnish Pty Ltd's commercial
   licence, on terms that differ from the AGPL.

4. **A patent licence.** You grant a perpetual, worldwide, non-exclusive,
   royalty-free, irrevocable patent licence covering your contribution, limited
   to claims you can license that are necessarily infringed by your contribution
   alone or by its combination with the project. This terminates for anyone who
   starts patent litigation alleging the project infringes.

5. **No warranty.** You provide your contribution as-is, with no warranties of
   any kind. You are not expected to support it.

You are not granting trademark rights, and you are not agreeing to any
obligation to contribute anything in future.

## What Donnish Pty Ltd undertakes in return

1. **The AGPL version stays.** Every contribution accepted here remains
   available under AGPL-3.0-or-later. The commercial licence exists alongside
   it, never instead of it. Nothing in this agreement permits taking the open
   version away.

2. **Attribution.** You are credited in the commit history and, if you would
   like, in a contributors list.

3. **No relicensing to something more restrictive for the open version.** If the
   open licence ever changes, it changes to another OSI-approved licence with a
   comparable copyleft obligation, or not at all.

## How to sign

Add a sign-off to each commit:

```bash
git commit -s -m "your message"
```

which appends:

```
Signed-off-by: Your Name <your@email.example>
```

The name and email must be real and must match the commit author. CI checks
every commit in a pull request and fails with instructions if any is missing.

To fix commits you have already made:

```bash
git rebase --signoff origin/main    # sign off everything on your branch
git push --force-with-lease
```

## If you cannot agree

That is a legitimate position, and there are still useful ways to help that
involve no licence grant at all: bug reports, reproductions, documentation
corrections filed as issues, testing on a platform we lack, and reviewing other
people's pull requests. Say so in the issue and it will not be held against the
contribution.

## Scope

This covers contributions to the Airo repository: code, documentation,
configuration and tests. It applies to contributions you have already made as
well as future ones, from the date you first sign off.

**Who needs to sign.** Everyone except the copyright holder. Donnish Pty Ltd
already owns the copyright in its own commits, and cannot meaningfully grant
itself a licence it already holds — so those commits carry no sign-off. This is
a legal fact rather than an exemption, but it is stated here because an
unexplained asymmetry in a contribution rule reads badly, and should.

The CI check runs on pull requests, which is how contributions from anyone else
arrive. It does not run on direct pushes to `main`, which only the copyright
holder can make — and that is now a setting rather than a description: `main` is
protected, requiring a pull request, an approving review and every CI check
above, with administrators exempt so the holder can still push directly. Anyone
else's work therefore arrives through a pull request and past this gate.

## Automated commits

A commit authored by a bot (an author name ending in `[bot]`, such as
Dependabot's version pins) is exempt from the sign-off requirement. A bot
cannot hold or grant copyright, and a machine-written version pin carries no
copyrightable expression for the grant to attach to. The maintainer who merges
such a pull request takes responsibility for its content. Human commits in the
same pull request still require a matching sign-off.
