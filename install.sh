#!/bin/bash
# lecture-tool — one-shot installer for macOS.
#
#   bash install.sh            install everything and open the panel
#   bash install.sh --check    only report what is present / missing
#
# What it sets up (all under this folder or your own user account, no sudo):
#   • Homebrew packages: ffmpeg, python@3.12 (Homebrew itself must exist — the script tells you if not)
#   • a private Python environment in .venv with pymupdf, pillow, fonttools
#   • Claude Code (the `claude` command) + sign-in with YOUR Claude subscription (no API key, no API cost)
#   • the panel as a login service at http://localhost:8765 (runs in the background, no terminal needed)
#   Updates: the panel's Settings shows "Update" when the person who gave you the tool published a new version.
# Narration: macOS voices work out of the box; for ElevenLabs paste your key in the panel's Settings.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
CHECK="${1:-}"
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
miss() { printf "  \033[31m✗\033[0m %s\n" "$1"; }
step() { printf "\n\033[1m%s\033[0m\n" "$1"; }

[ "$(uname)" = "Darwin" ] || { echo "This installer is for macOS."; exit 1; }
step "lecture-tool installer ($ROOT)"

# ---- Homebrew -------------------------------------------------------------
if command -v brew >/dev/null; then ok "Homebrew"; else
  miss "Homebrew is not installed. Install it first (one line, needs your password), then run this script again:"
  echo '     /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  [ "$CHECK" = "--check" ] || exit 1
fi

# ---- git (for updates) ----------------------------------------------------
if command -v git >/dev/null && git --version >/dev/null 2>&1; then ok "git"; else
  miss "git — run:  xcode-select --install   (then run this script again)"
  [ "$CHECK" = "--check" ] || exit 1
fi

# ---- ffmpeg + python ------------------------------------------------------
need_brew=()
command -v ffmpeg >/dev/null && ok "ffmpeg" || { miss "ffmpeg"; need_brew+=(ffmpeg); }
PY=""
for cand in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 python3.12 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    v="$("$cand" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)"
    if [ "$v" -ge 310 ]; then PY="$(command -v "$cand")"; break; fi
  fi
done
[ -n "$PY" ] && ok "Python ≥ 3.10 ($PY)" || { miss "Python ≥ 3.10"; need_brew+=(python@3.12); }
if [ ${#need_brew[@]} -gt 0 ] && [ "$CHECK" != "--check" ] && command -v brew >/dev/null; then
  step "Installing with Homebrew: ${need_brew[*]}"
  brew install "${need_brew[@]}"
  command -v ffmpeg >/dev/null && ok "ffmpeg"
  PY="$(command -v python3.12 || command -v python3)"
fi

# ---- Python environment ---------------------------------------------------
if [ "$CHECK" != "--check" ] && [ -n "$PY" ]; then
  step "Python environment (.venv)"
  if [ -d .venv ] && ! .venv/bin/python -c pass >/dev/null 2>&1; then rm -rf .venv; fi   # stale venv (Python removed/upgraded)
  [ -d .venv ] || "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet pymupdf pillow fonttools brotli
  ok "pymupdf, pillow, fonttools, brotli in .venv"
elif [ -d .venv ] && .venv/bin/python -c "import fitz, PIL, fontTools, brotli" >/dev/null 2>&1; then ok ".venv with pymupdf, pillow, fonttools, brotli"
elif [ -d .venv ]; then miss ".venv is broken or incomplete — run install.sh again"; else miss ".venv (Python environment) — created on install"; fi

# ---- Claude Code ----------------------------------------------------------
if command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ]; then ok "Claude Code (claude)"; else
  miss "Claude Code"
  if [ "$CHECK" != "--check" ]; then
    step "Installing Claude Code"
    curl -fsSL https://claude.ai/install.sh | bash
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi
export PATH="$HOME/.local/bin:$PATH"
if command -v claude >/dev/null; then
  if claude auth status 2>/dev/null | grep -q '"loggedIn": true'; then ok "signed in to Claude"; else
    miss "not signed in to Claude"
    if [ "$CHECK" != "--check" ]; then
      step "Sign in with your Claude subscription (a browser window opens)"
      claude auth login
    fi
  fi
fi

# ---- service ----------------------------------------------------------------
if [ "$CHECK" = "--check" ]; then
  bash scripts/install_service.sh --status | sed 's/^/  /'
  exit 0
fi
step "Starting the panel as a login service"
PYTHON_BIN="$ROOT/.venv/bin/python" bash scripts/install_service.sh
sleep 1
open "http://localhost:8765" 2>/dev/null || true
step "Done. The panel is at http://localhost:8765 — it starts by itself at every login."
echo "   Drop a PDF, press Create & run. Narration uses the macOS voice until you add an ElevenLabs key in Settings."
