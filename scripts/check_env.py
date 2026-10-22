#!/usr/bin/env python3
"""Check which prerequisites are installed. Milestone 1 needs only Python + pymupdf.

    python3 scripts/check_env.py
"""
import importlib
import shutil
import sys

M1 = [("python >= 3.10", sys.version_info >= (3, 10), f"python {sys.version.split()[0]}",
       "install Python 3.12 from python.org or `brew install python@3.12`")]
PY = [("pymupdf", "pymupdf", "pip3 install pymupdf"),
      ("manim (M2)", "manim", "pip3 install manim"),
      ("playwright (optional, UI tests)", "playwright", "pip3 install playwright")]
BIN = [("claude (Claude Code)", "claude", "see README"),
       ("ffmpeg (M2)", "ffmpeg", "brew install ffmpeg"),
       ("latex (M2)", "latex", "brew install --cask basictex, then restart the terminal"),
       ("say (macOS TTS, M2)", "say", "built into macOS"),
       ("pkg-config (M2, for manim)", "pkg-config", "brew install pkg-config cairo pango")]


def ok(flag):
    return "✓" if flag else "✗"


print("Milestone 1 (written pipeline):")
for name, flag, found, fix in M1:
    print(f"  {ok(flag)} {name:34} {found if flag else 'missing → ' + fix}")
for name, mod, fix in PY[:1]:
    try:
        m = importlib.import_module(mod)
        print(f"  ✓ {name:34} {getattr(m, 'version', getattr(m, '__version__', ''))}")
    except ImportError:
        print(f"  ✗ {name:34} missing → {fix}")
for name, b, fix in BIN[:1]:
    p = shutil.which(b)
    print(f"  {ok(p)} {name:34} {p or 'missing → ' + fix}")

print("\nMilestone 2 (video) — not needed yet:")
for name, mod, fix in PY[1:]:
    try:
        importlib.import_module(mod)
        print(f"  ✓ {name}")
    except ImportError:
        print(f"  ✗ {name:34} → {fix}")
for name, b, fix in BIN[1:]:
    p = shutil.which(b)
    print(f"  {ok(p)} {name:34} {p or '→ ' + fix}")
