# See [CONVENTIONS.md](CONVENTIONS.md)

This file exists only so editor tooling that looks for a fixed filename finds the
project's conventions. It holds no content of its own — everything is in
[CONVENTIONS.md](CONVENTIONS.md), which is where edits go.

A pointer rather than a symlink: git symlinks need `core.symlinks` and developer mode
on Windows, and where that is off the file arrives as text containing a path — which
reads as a broken instruction rather than an obvious redirect. This project has been
bitten enough times by things that degrade quietly on Windows.
