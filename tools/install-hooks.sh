#!/bin/bash
# Install Airo's git hooks into this checkout.
#
# Hooks are per-clone and cannot be committed into .git/hooks, so this has to
# be run once after cloning. CI enforces the same rules regardless, but the
# hook catches a mistake before it becomes a commit that needs rewriting.

set -uo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS="$PROJECT/.git/hooks"

if [ ! -d "$PROJECT/.git" ]; then
  echo "Not a git checkout — nothing to install."
  exit 1
fi

mkdir -p "$HOOKS"
cp "$PROJECT/tools/pre-commit" "$HOOKS/pre-commit"
chmod +x "$HOOKS/pre-commit"
printf "  \033[32m✓\033[0m pre-commit hook installed at %s\n" "$HOOKS/pre-commit"
echo "    It refuses to commit databases, readings, logs, backups or keys."
echo "    Bypass a false positive with: git commit --no-verify"
