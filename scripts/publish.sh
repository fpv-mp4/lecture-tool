#!/bin/bash
# Publish the personal tool to the shared repository your friends install from.
#
#   bash scripts/publish.sh "what changed"        copy the tool files, bump the patch version, commit, push
#   bash scripts/publish.sh --minor "…"           bump the minor version instead (1.0.x → 1.1.0)
#
# The shared copy lives in a separate folder (default ~/Documents/UNI/lecture-tool-dist, override with
# LT_DIST) with its own git repository and GitHub remote. Only the tool goes there: scripts, panel,
# skill, installer, docs. Never your lectures (jobs/), your keys (.env) or the Python environment.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DIST="${LT_DIST:-$HOME/Documents/UNI/lecture-tool-dist}"
REPO="${LT_REPO:-lecture-tool}"           # GitHub repository name under your account
BUMP="patch"
if [ "${1:-}" = "--minor" ]; then BUMP="minor"; shift; fi
MSG="${1:-update}"

# ---- the shared folder: create it (and the GitHub repository) on first use ------------------------
if [ ! -d "$DIST/.git" ]; then
  mkdir -p "$DIST"
  git -C "$DIST" init -q -b main
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    OWNER="$(gh api user -q .login)"
    if ! gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
      gh repo create "$OWNER/$REPO" --private --description "Lecture Tool — PDF to compact lecture, summary and narrated video, on your own Mac" >/dev/null
      echo "created private GitHub repository $OWNER/$REPO"
    fi
    git -C "$DIST" remote add origin "https://github.com/$OWNER/$REPO.git"
  else
    echo "gh is not signed in: the folder $DIST was created without a GitHub remote. Run 'gh auth login' and publish again, or add a remote yourself."
  fi
fi

# ---- copy the tool (and nothing personal) ------------------------------------------------------------
rsync -a --delete \
  --exclude '.git' --exclude 'jobs' --exclude '.env' --exclude '.env.*' --exclude '.venv' --exclude '__pycache__' \
  --exclude '.DS_Store' --exclude 'node_modules' --exclude 'apps' --exclude 'packages' --exclude 'infra' --exclude 'evals' \
  --exclude 'pnpm-lock.yaml' --exclude 'pnpm-workspace.yaml' --exclude 'package.json' --exclude 'Makefile' --exclude '.github' \
  --exclude '.data' --exclude 'docs/PLAN.md' --exclude 'docs/STATUS.md' --exclude 'docs/CONTRACTS.md' --exclude 'docs/runbooks' \
  --exclude 'docs/SAAS-*' --exclude '.claude/settings.local.json' \
  "$SRC/" "$DIST/"
cat > "$DIST/.gitignore" <<'G'
jobs/
.env
.env.*
.venv/
__pycache__/
*.pyc
.DS_Store
G
mkdir -p "$DIST/jobs"; touch "$DIST/jobs/.keep"
sed -i '' '/^jobs\/$/d' "$DIST/.gitignore"; printf 'jobs/*\n!jobs/.keep\n' >> "$DIST/.gitignore"

# ---- version bump ----------------------------------------------------------------------------------
V="$(cat "$SRC/VERSION" 2>/dev/null || echo 1.0.0)"
IFS=. read -r MA MI PA <<<"$V"
if [ "$BUMP" = "minor" ]; then MI=$((MI+1)); PA=0; else PA=$((PA+1)); fi
NEW="$MA.$MI.$PA"
if [ -n "$(git -C "$DIST" status --porcelain)" ] || [ -z "$(git -C "$DIST" log --oneline -1 2>/dev/null)" ]; then
  [ -z "$(git -C "$DIST" log --oneline -1 2>/dev/null)" ] && NEW="$V"      # first publish keeps the version as it is
  echo "$NEW" > "$SRC/VERSION"; echo "$NEW" > "$DIST/VERSION"
  git -C "$DIST" add -A
  git -C "$DIST" -c user.name="$(git config user.name || echo lecture-tool)" -c user.email="$(git config user.email || echo lecture-tool@localhost)" \
    commit -q -m "v$NEW — $MSG" -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
  if git -C "$DIST" remote get-url origin >/dev/null 2>&1; then
    git -C "$DIST" push -q -u origin main
    echo "published v$NEW → $(git -C "$DIST" remote get-url origin)"
  else
    echo "committed v$NEW locally in $DIST (no remote yet)"
  fi
else
  echo "nothing changed since v$V — nothing published"
fi
