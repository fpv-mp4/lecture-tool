#!/bin/bash
# Lecture Tool — one-line install for macOS. Run with:
#   curl -fsSL https://raw.githubusercontent.com/fpv-mp4/lecture-tool/main/get.sh | bash
# Installs Homebrew if missing (that also brings git), fetches the tool into ~/lecture-tool and runs its installer.
set -euo pipefail
REPO="https://github.com/fpv-mp4/lecture-tool.git"
DEST="${LECTURE_TOOL_DIR:-$HOME/lecture-tool}"
say() { printf "\n\033[1m%s\033[0m\n" "$1"; }

[ "$(uname)" = "Darwin" ] || { echo "This installer is for macOS."; exit 1; }

if ! command -v brew >/dev/null 2>&1; then
  say "Installing Homebrew (asks for your Mac password once)…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/tty
  if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
  if [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"; fi
fi

if ! git --version >/dev/null 2>&1; then
  say "Installing the Apple command line tools (git)…"
  xcode-select --install 2>/dev/null || true
  echo "A window opened to install the tools. When it says it is done, run this command again."
  exit 0
fi

if [ -d "$DEST/.git" ]; then
  say "Updating $DEST…"
  git -C "$DEST" pull --ff-only
else
  say "Fetching Lecture Tool into $DEST…"
  git clone "$REPO" "$DEST"
fi

say "Running the installer…"
cd "$DEST" && bash install.sh </dev/tty
