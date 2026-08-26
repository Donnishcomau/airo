# See [CONVENTIONS.md](CONVENTIONS.md)

This file exists only so agent tooling that looks for a fixed filename finds the
project's conventions. It holds no content of its own — everything is in
[CONVENTIONS.md](CONVENTIONS.md), which is where edits go.

Two pointers, because the tools disagree about the name: most coding agents look for
this one, and the editor assistant this project was written with looks for a file
named after itself. Both redirect to the same page. Neither is a second place for a
rule to live, and a test fails if either grows content of its own.

A pointer rather than a symlink: git symlinks need `core.symlinks` and developer mode
on Windows, and where that is off the file arrives as text containing a path — which
reads as a broken instruction rather than an obvious redirect. This project has been
bitten enough times by things that degrade quietly on Windows.
