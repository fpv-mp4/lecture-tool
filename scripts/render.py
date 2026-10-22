#!/usr/bin/env python3
"""Stage 8 — render scenes.json into lecture.mp4 (+ lecture.srt, chapters.json).

    python scripts/render.py <job_dir>              full render
    python scripts/render.py <job_dir> --check      validate scenes.json only
    python scripts/render.py <job_dir> --scene s03  render one scene to render/s03/scene.mp4
    python scripts/render.py <job_dir> --silent     no TTS, 3 s per scene (layout preview)
    python scripts/render.py <job_dir> --estimate   ElevenLabs credits needed vs. remaining, then exit

Needs: Pillow, ffmpeg/ffprobe on PATH, macOS `say` (or ELEVENLABS_API_KEY in the
environment / <project>/.env when job.tts == "elevenlabs"). Slides are drawn with
Pillow in the SPRTK look using the fonts in ui/fonts.

Scenes are rendered by RENDER_WORKERS threads (default 3) — narration, slide drawing and the
per-scene encode run side by side; the output is frame-identical to a sequential render
(RENDER_WORKERS=1). A scene only waits for its predecessor's last frame, which it needs for
the entry cross-dissolve; the final join is sequential.

Scene audio in <job_dir>/render/<scene>/ is reused when narration, voice and model are
unchanged, so an interrupted ElevenLabs render (quota) continues where it stopped.
ELEVENLABS_MODEL (default eleven_turbo_v2_5 = 0.5 credits/char; eleven_multilingual_v2 = 1/char).

scenes.json schema (written by Claude in Stage 7) — every content scene carries `beats`:
{
  "title": "...", "lang": "en"|"de", "voice": "Samantha" (say only, optional),
  "scenes": [
    {"id":"s01","type":"title",   "heading":"...","sub":"...","narration":"..."},
    {"id":"s02","type":"section", "heading":"6.1 Defining Functions","sub":"...","narration":"..."},
    {"id":"s03","type":"bullets", "heading":"...","bullets":["...","..."],"page":72,
       "beats":[{"say":"First sentence(s).","focus":{"item":1}}, {"say":"...","focus":{"item":2}}]},
    {"id":"s04","type":"steps",   "heading":"...","steps":["..."],"page":74,"beats":[{"say":"...","focus":{"item":1}}]},
    {"id":"s05","type":"code",    "heading":"...","code":"...","page":72,
       "beats":[{"say":"...","focus":{"lines":[5,6]}}, {"say":"...","focus":{"lines":[13]}}]},
    {"id":"s06","type":"formula", "heading":"...","formula":"min(a₁, b₁) ≤ max(a₂, b₂)","caption":"...","page":87,
       "beats":[{"say":"...","focus":{"part":"min(a₁, b₁)"}}]},
    {"id":"s07","type":"memory",  "heading":"...","frames":[{"label":"pow","cells":[{"name":"b","value":"2.0"}]},{"label":"main","cells":[...]}],
       "beats":[{"say":"...","focus":{"cells":["pow.b"]}}, {"say":"...","focus":{"cells":["pow.b"],"frames":[...new values...]}}]},
    {"id":"s08","type":"quiz",    "heading":"Check yourself","questions":["..."],"beats":[{"say":"...","focus":{"item":1}}]},
    {"id":"s09","type":"figure",  "heading":"...","page":14,"crop":[0.08,0.30,0.92,0.62],"caption":"...",
       "beats":[{"say":"...","focus":{"box":[0.0,0.0,0.5,1.0]}}, {"say":"...","focus":{"zoom":[0.5,0.0,1.0,1.0]}}]},
    {"id":"s10","type":"exercise","heading":"Serie 1, Aufgabe 3a","task":"...","steps":["...","..."],"result":"s ≈ 16 m","page":453,
       "beats":[{"say":"Here is the task.","focus":{"task":true},"pause":20}, {"say":"...","focus":{"item":1}}, {"say":"...","focus":{"result":true}}]}
  ]
}
Any beat may carry "pause": N (seconds, 3–45): its words are spoken, then the picture holds in silence for N seconds
with a "Try it yourself" countdown; the silence is cut into the scene audio and costs no narration credits.
`figure` shows a crop of the original page image (pages/page-NNN.png, continuous numbering): `crop`
and the per-beat `zoom` / `box` are [x0, y0, x1, y1] as fractions of the page / of the shown crop.
A beat is one or two spoken sentences plus what to highlight while they are spoken.
Without `beats`, `narration` is used and lists are revealed proportionally (older storyboards).
"""
import base64
import concurrent.futures
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "ui" / "fonts"
W, H = 1920, 1080
FPS = 30
TWEEN = 0.28      # cross-dissolve between beats inside a scene
XFADE = 0.5       # cross-dissolve between scenes

# ---- SPRTK palette --------------------------------------------------------
BG = (20, 20, 20)
INSET = (15, 15, 15)
PANEL = (24, 24, 24)
PANEL2 = (31, 31, 31)
LINE = (48, 48, 48)
TEXT = (255, 255, 255)
MUTED = (150, 150, 150)
DIM = (105, 105, 105)
FAINT = (95, 95, 95)
LIME = (180, 233, 37)
LIME_DIM = (38, 44, 24)
LIME_DEEP = (143, 191, 20)
RED = (224, 83, 61)
CODE_STR = (214, 190, 255)   # string literals in code scenes
BOX_DIM = (0, 0, 0)          # what a figure fades towards outside the highlighted box
FIGURE_BORDER = None         # outline around a figure crop (none on the white base)

# Video themes. A pure-white base plus an accent. On white the
# accent is Moss, Indigo or Amber, or "mix", which moves to the next accent at every section so the
# video changes colour as it goes. Figures from the PDF sit borderless on the white background.
BASES = {
    "white": dict(BG=(255, 255, 255), INSET=(247, 247, 247), PANEL=(250, 250, 250), PANEL2=(243, 243, 243),
                  LINE=(222, 222, 222), TEXT=(24, 24, 24), MUTED=(104, 104, 104), DIM=(170, 170, 170),
                  FAINT=(165, 165, 165), RED=(192, 72, 52), CODE_STR=(120, 72, 160), BOX_DIM=(255, 255, 255),
                  FIGURE_BORDER=None),
}
globals().update(BASES["white"])        # the white base is the only one; older dark constants above are history
# accent, deeper accent (for passive markers), pale tint (for highlighted panels)
ACCENTS = {
    "moss":   ((79, 122, 58), (58, 92, 42), (228, 238, 220)),
    "indigo": ((74, 95, 193), (54, 70, 150), (226, 230, 248)),
    "amber":  ((178, 106, 0), (134, 80, 0), (247, 234, 208)),
    "plum":   ((140, 72, 150), (104, 52, 112), (240, 226, 242)),
}
MIX_ORDER = ["moss", "indigo", "amber", "plum"]
THEMES = {"mix": ("white", "mix"), "moss": ("white", "moss"), "indigo": ("white", "indigo"),
          "amber": ("white", "amber"),
          # older job.json values (the dark look is gone: it becomes mix)
          "dark": ("white", "mix"), "linen": ("white", "moss"), "chalk": ("white", "indigo"), "cloud": ("white", "amber"),
          "light": ("white", "moss"), "paper": ("white", "amber"), "cream": ("white", "amber"), "mist": ("white", "moss")}
DEFAULT_THEME = "mix"
THEME_ACCENT = "mix"


def set_theme(name: str):
    global THEME_ACCENT
    base, THEME_ACCENT = THEMES.get(name or DEFAULT_THEME, THEMES[DEFAULT_THEME])
    globals().update(BASES[base])


def accent_for(section_idx: int):
    """(accent, deep, tint) for a scene, given how many section scenes precede it."""
    name = MIX_ORDER[section_idx % len(MIX_ORDER)] if THEME_ACCENT == "mix" else THEME_ACCENT
    return ACCENTS[name]

TYPES = {"title", "section", "bullets", "steps", "code", "formula", "memory", "quiz", "figure", "exercise"}
LIST_KEY = {"bullets": "bullets", "steps": "steps", "quiz": "questions", "exercise": "steps"}
PAUSE_LABEL = {"en": "Try it yourself", "de": "Versuch es selbst"}
# narration words allowed per word of lecture.md, by compression ratio (the lecture shrinks with it too)
NARRATION_BUDGET = {"1:1": 2.6, "lossless": 2.4, "1:1.5": 2.0, "1:2": 1.6, "1:3": 1.3, "1:4": 1.1, "1:6": 1.0}
RESULT_LABEL = {"en": "RESULT", "de": "ERGEBNIS"}
VOICES = {"en": "Samantha", "de": "Anna"}


# ---- helpers ----------------------------------------------------------------
def load_env():
    """Read KEY=VALUE lines from <project>/.env into the environment (existing values win)."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_LOG_LOCK = threading.Lock()


def log(job: Path, msg: str):
    with _LOG_LOCK:
        print(msg, flush=True)
        with open(job / "render.log", "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def status(job: Path, stage: str, msg: str):
    subprocess.run([sys.executable, str(ROOT / "scripts" / "status.py"), str(job), stage, msg],
                   check=False, capture_output=True)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{r.stderr[-2000:]}")
    return r


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"{name}.woff2"), size)


# Glyph fallback: the brand fonts lack Greek, ≤ ∧ ← etc. Characters a font cannot draw are
# rendered from the first fallback that has them (Inter first: same sans style and proper
# Greek; STIX Two Math for the rest), at the same size and on the same baseline.
_FALLBACK = {"mono": ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Supplemental/STIXTwoMath.otf",
                      "/System/Library/Fonts/Apple Symbols.ttf", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"],
             "text": [str(FONTS / "inter-500.woff2"), "/System/Library/Fonts/Supplemental/STIXTwoMath.otf",
                      "/System/Library/Fonts/Apple Symbols.ttf", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"]}
# What the brand woff2 fonts are assumed to cover when their cmap cannot be read (fontTools needs
# the `brotli` module for woff2): Latin incl. extensions and general punctuation — everything else
# goes to the fallback chain. Better a Greek letter from STIX than a box.
_LATIN = set(range(0x20, 0x250)) | set(range(0x2000, 0x2070)) | {0x20AC}
_CMAPS: dict = {}
_CMAP_LOCK = threading.Lock()


def _cmap(path: str):
    with _CMAP_LOCK:                 # the sets are read-only once built, so sharing them across threads is fine
        if path not in _CMAPS:
            try:
                from fontTools.ttLib import TTFont
                _CMAPS[path] = set(TTFont(path, fontNumber=0).getBestCmap().keys())
            except Exception:  # noqa: BLE001 — no fontTools / no brotli for woff2
                _CMAPS[path] = _LATIN if path.endswith(".woff2") else None
        return _CMAPS[path]


# Sub- and superscripts are drawn from the brand font itself at reduced size,
# so a₁, Tₙ, pnameᵢ and x² look consistent instead of borrowing glyphs.
_SUB = dict(zip("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜᵢⱼᵣᵤᵥᵦᵧᵨᵩᵪ", "0123456789+-=()aeoxhklmnpstijruvβγρφχ"))
_SUP = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵗᵃᵇᵈᵉᵏᵐᵒᵖʳˢᵘʷˣʸ", "0123456789+-=()nitabdekmoprsuwxy"))
# Font objects are cached per thread: scenes render in parallel and a FreeType face must not be
# shared between threads (fresh `font()` objects are per call anyway, so only the caches need this).
_TL = threading.local()


def _tl_cache(name: str) -> dict:
    d = getattr(_TL, name, None)
    if d is None:
        d = {}
        setattr(_TL, name, d)
    return d


def _small(f: ImageFont.FreeTypeFont):
    cache = _tl_cache("small")
    key = (f.path, f.size)
    if key not in cache:
        cache[key] = ImageFont.truetype(f.path, max(8, int(f.size * 0.62)))
    return cache[key]


def _segments(s: str, scripts: bool = True):
    """Split into (text, script) with script in {None, 'sub', 'sup'}; `_x` after a word also subscripts."""
    out, cur, mode = [], "", None
    i = 0
    while i < len(s):
        ch = s[i]
        if ch in _SUB:
            m, c = "sub", _SUB[ch]
        elif ch in _SUP:
            m, c = "sup", _SUP[ch]
        elif scripts and ch == "_" and i + 1 < len(s) and s[i + 1].isalnum() and i > 0 and s[i - 1].isalnum() \
                and not (i + 2 < len(s) and s[i + 2].isalnum()):   # x_1, a_n — not snake_case identifiers
            j = i + 1
            while j < len(s) and s[j].isalnum():
                j += 1
            if cur:
                out.append((cur, mode))
            out.append((s[i + 1:j], "sub"))
            cur, mode, i = "", None, j
            continue
        else:
            m, c = None, ch
        if m != mode and cur:
            out.append((cur, mode))
            cur = ""
        cur, mode = cur + c, m
        i += 1
    if cur:
        out.append((cur, mode))
    return out


def _fallback_font(path: str, size: int):
    cache = _tl_cache("fallback")
    key = (path, size)
    if key not in cache:
        cache[key] = ImageFont.truetype(path, size)
    return cache[key]


def _fallback_runs(s: str, f: ImageFont.FreeTypeFont):
    cm = _cmap(f.path)
    if cm is None:
        return [(s, f)]
    kind = "mono" if "mono" in Path(f.path).name else "text"
    chain = [p for p in _FALLBACK[kind] if Path(p).exists() and p != f.path]

    def font_for(ch):
        if ord(ch) in cm or ch.isspace():
            return f
        for p in chain:
            c2 = _cmap(p)
            if c2 is None or ord(ch) in c2:
                return _fallback_font(p, f.size)
        return _fallback_font(chain[-1], f.size) if chain else f

    runs, cur, cur_f = [], "", f
    for ch in s:
        g = font_for(ch)
        if g is not cur_f and cur:
            runs.append((cur, cur_f))
            cur = ""
        cur, cur_f = cur + ch, g
    if cur:
        runs.append((cur, cur_f))
    return runs


def _runs(s: str, f: ImageFont.FreeTypeFont):
    """(text, font, dy) runs: scripts use the small font with a vertical offset."""
    out = []
    for text, mode in _segments(s):
        if mode is None:
            out += [(t, g, 0) for t, g in _fallback_runs(text, f)]
        else:
            sf = _small(f)
            # offsets relative to the main baseline: subscripts a little below, superscripts well above
            dy = f.size * 0.16 if mode == "sub" else -f.size * 0.42
            out += [(t, g, dy) for t, g in _fallback_runs(text, sf)]
    return out


def text_width(s: str, f: ImageFont.FreeTypeFont) -> float:
    return sum(g.getlength(t) for t, g, _ in _runs(s, f))


def draw_text(d: ImageDraw.ImageDraw, xy, s: str, f: ImageFont.FreeTypeFont, fill=None, anchor="la"):
    """Draw s with glyph fallback and sub/superscripts; every run sits on the main font's baseline,
    so a Greek letter from a fallback font lines up with the Latin text around it."""
    fill = TEXT if fill is None else fill                     # resolved at call time: themes swap TEXT
    x, y = xy
    w = text_width(s, f)
    if anchor[0] == "m":
        x -= w / 2
    elif anchor[0] == "r":
        x -= w
    ascent, descent = f.getmetrics()
    v = anchor[1]
    base = y + ascent if v == "a" else y + (ascent - descent) / 2 if v == "m" else y
    for t, g, dy in _runs(s, f):
        d.text((x, base + dy), t, font=g, fill=fill, anchor="ls")
        x += g.getlength(t)


def duration_of(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)])
    return float(r.stdout.strip())


def sentences(text: str):
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


# ---- beats ------------------------------------------------------------------
def narration_of(s: dict) -> str:
    if s.get("beats"):
        return " ".join(str(b.get("say") or "").strip() for b in s["beats"] if str(b.get("say") or "").strip())
    return (s.get("narration") or "").strip()


def beats_of(s: dict):
    """Normalised beats: [{say, focus}] — derived from the narration for storyboards without beats."""
    if s.get("beats"):
        return [{"say": str(b.get("say") or "").strip(), "focus": b.get("focus") or {},
                 "pause": float(b.get("pause") or 0)} for b in s["beats"]
                if isinstance(b, dict) and (str(b.get("say") or "").strip() or b.get("pause"))]
    sents = sentences(narration_of(s))
    items = s.get(LIST_KEY.get(s["type"], ""), [])
    out = []
    for i, snt in enumerate(sents):
        focus = {}
        if items:
            focus = {"item": min(len(items), max(1, round((i + 1) / len(sents) * len(items))))}
        out.append({"say": snt, "focus": focus, "pause": 0.0})
    return out or [{"say": "", "focus": {}, "pause": 0.0}]


# ---- validation -------------------------------------------------------------
PAGE_COUNT = 0   # set from ingest.json in main(); 0 = unknown


def _rect_ok(r):
    return (isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) for v in r)
            and 0 <= r[0] < r[2] <= 1 and 0 <= r[1] < r[3] <= 1)


def validate(sc: dict):
    errs = []
    if not isinstance(sc.get("scenes"), list) or not sc["scenes"]:
        return ["scenes must be a non-empty list"]
    ids = set()
    for i, s in enumerate(sc["scenes"]):
        where = f"scene {i} ({s.get('id', '?')})"
        if not isinstance(s, dict):
            errs.append(f"scene {i}: must be an object")
            continue
        if not isinstance(s.get("id"), str) or not re.fullmatch(r"[\w-]+", s["id"]):
            errs.append(f"{where}: id must be a short name like s012")
        if s.get("id") in ids:
            errs.append(f"{where}: duplicate id")
        ids.add(s.get("id"))
        for key in ("heading", "sub", "caption", "task", "result", "formula", "code"):
            if key in s and s[key] is not None and not isinstance(s[key], str):
                errs.append(f"{where}: {key} must be text")
        if s.get("type") in LIST_KEY and isinstance(s.get(LIST_KEY[s["type"]]), list) \
                and any(not isinstance(x, str) for x in s[LIST_KEY[s["type"]]]):
            errs.append(f"{where}: every {LIST_KEY[s['type']]} item must be text")
        t = s.get("type")
        if t not in TYPES:
            errs.append(f"{where}: unknown type {t!r}")
            continue
        text = narration_of(s)
        if not text and t not in ("title", "section"):
            errs.append(f"{where}: narration/beats missing")
        if t in LIST_KEY and not s.get(LIST_KEY[t]):
            errs.append(f"{where}: {LIST_KEY[t]} missing")
        if t == "code" and not s.get("code"):
            errs.append(f"{where}: code missing")
        if t == "code" and s.get("code") and len(s["code"].splitlines()) > 22:
            errs.append(f"{where}: code has {len(s['code'].splitlines())} lines (max 22)")
        if t == "formula" and not s.get("formula"):
            errs.append(f"{where}: formula missing")
        if t == "memory" and not s.get("frames"):
            errs.append(f"{where}: frames missing")
        if t == "memory" and s.get("frames") and any(not isinstance(fr, dict) or not isinstance(fr.get("cells"), list)
                                                      for fr in s["frames"]):
            errs.append(f"{where}: every frame needs a 'cells' list")
        if t == "code" and s.get("code") and max(len(ln) for ln in s["code"].splitlines() or [""]) > 100:
            errs.append(f"{where}: a code line is longer than 100 characters — wrap it")
        if t in LIST_KEY and s.get(LIST_KEY[t]) and all(isinstance(x, str) for x in s[LIST_KEY[t]]) \
                and isinstance(s.get("heading", ""), str) and not list_fits(s):
            errs.append(f"{where}: the {LIST_KEY[t]} do not fit on one slide — split the scene or shorten the items")
        if t == "exercise":
            if not isinstance(s.get("task"), str) or not s["task"].strip():
                errs.append(f"{where}: exercise needs 'task' (the problem statement)")
            elif len(s["task"]) > 450:
                errs.append(f"{where}: task is {len(s['task'])} characters (max 450 — split into sub-parts)")
            for k, st_ in enumerate(s.get("steps") or [], 1):
                if len(st_) > 160:
                    errs.append(f"{where}: step {k} is {len(st_)} characters (max 160)")
            if "result" in s and not isinstance(s["result"], str):
                errs.append(f"{where}: result must be a string")
        if t == "figure":
            if not isinstance(s.get("page"), int) or s["page"] < 1:
                errs.append(f"{where}: figure needs 'page' (continuous page number from ingest.json)")
            elif PAGE_COUNT and s["page"] > PAGE_COUNT:
                errs.append(f"{where}: page {s['page']} > {PAGE_COUNT} pages")
            if "crop" in s and not _rect_ok(s["crop"]):
                errs.append(f"{where}: crop must be [x0, y0, x1, y1] as fractions 0..1 with x0<x1, y0<y1")
        if t != "code" and not s.get("heading"):
            errs.append(f"{where}: heading missing")
        n = len(text.split())
        if n > 220:
            errs.append(f"{where}: narration is {n} words (max 220 — split the scene)")
        # beats
        for k, b in enumerate(s.get("beats") or []):
            bw = f"{where} beat {k + 1}"
            if not isinstance(b, dict) or (not str(b.get("say") or "").strip() and not b.get("pause")):
                errs.append(f"{bw}: 'say' missing")
                continue
            if b.get("say") is not None and not isinstance(b["say"], str):
                errs.append(f"{bw}: 'say' must be text")
                continue
            if "\n" in (b.get("say") or ""):
                errs.append(f"{bw}: 'say' must not contain line breaks (they split subtitles)")
            if "pause" in b and (not isinstance(b["pause"], (int, float)) or not 3 <= b["pause"] <= 45):
                errs.append(f"{bw}: pause must be a number of seconds between 3 and 45")
            if t == "exercise" and b.get("focus", {}).get("result") and not s.get("result"):
                errs.append(f"{bw}: focus.result but the scene has no 'result'")
            if len((b.get("say") or "").split()) > 70:
                errs.append(f"{bw}: {len(b['say'].split())} words — keep a beat to one or two sentences")
            fo = b.get("focus") or {}
            if t in LIST_KEY and "item" in fo:
                n_items = len(s.get(LIST_KEY[t], []))
                if not isinstance(fo["item"], int) or not 1 <= fo["item"] <= n_items:
                    errs.append(f"{bw}: item must be 1..{n_items}")
            if t == "code" and "lines" in fo:
                n_lines = len(s.get("code", "").splitlines())
                ls = fo["lines"]
                if not isinstance(ls, list) or not ls or len(ls) > 2 or any(not isinstance(x, int) for x in ls) \
                        or any(not 1 <= x <= n_lines for x in ls) or (len(ls) == 2 and ls[0] > ls[1]):
                    errs.append(f"{bw}: lines must be [a] or [a, b] within 1..{n_lines}")
            if t == "formula" and "part" in fo and fo["part"] not in s.get("formula", ""):
                errs.append(f"{bw}: part {fo['part']!r} is not a substring of the formula")
            if t == "figure":
                for key in ("zoom", "box"):
                    if key in fo and not _rect_ok(fo[key]):
                        errs.append(f"{bw}: {key} must be [x0, y0, x1, y1] as fractions 0..1 of the shown crop")
            if t == "memory":
                frames = fo.get("frames") or s.get("frames") or []
                names = {f"{fr.get('label')}.{c.get('name')}" for fr in frames for c in fr.get("cells", [])}
                for c in fo.get("cells", []):
                    if c not in names:
                        errs.append(f"{bw}: cell {c!r} not in frames ({', '.join(sorted(names))})")
    return errs


def _wrap_lines(text: str, f, maxw: int) -> int:
    lines, cur = 0, ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if text_width(trial, f) <= maxw or not cur:
            cur = trial
        else:
            lines, cur = lines + 1, word
    return lines + (1 if cur else 0)


def list_fits(s: dict) -> bool:
    """Would every item of a list-type scene be drawn above the footer? Mirrors _list / exercise."""
    t = s["type"]
    items = s.get(LIST_KEY[t]) or []
    if t == "exercise":
        y = 130 + int(46 * 1.15) * _wrap_lines(s.get("heading", ""), font("space-grotesk-600", 46), W - 160) + 26
        ftask, lh = font("inter-400", 34), int(34 * 1.35)
        y += lh * sum(_wrap_lines(para, ftask, W - 240) or 1 for para in (s.get("task") or "").split("\n")) + 40 + 28
        fbody = font("inter-400", 34)
        for item in items:
            y += int(34 * 1.3) * _wrap_lines(item, fbody, W - 150 - 120) + 14
        if s.get("result"):
            y += int(40 * 1.3) * _wrap_lines(s["result"], font("space-grotesk-600", 40), W - 400) + 36 + 8
        return y <= H - 92
    y = 150 + int(58 * 1.15) * _wrap_lines(s.get("heading", ""), font("space-grotesk-600", 58), W - 160) + 40
    fbody = font("inter-400", 38)
    tx = 80 + (70 if t in ("steps", "quiz") else 40)
    for item in items:
        n = _wrap_lines(item, fbody, W - tx - 120)
        if t == "quiz":
            y += int(38 * 1.3) * n + 28 + 16
        else:
            y += int(38 * 1.3) * n + 18
    return y <= H - 140


def expand_long_lists(sc: dict):
    """Split list scenes (bullets, steps, quiz, exercise) whose items do not fit on one slide into
    continuation scenes "… (1/2)", "… (2/2)". Beats follow their item; beats without an item stay
    with the first part. Returns the number of scenes that were split."""
    out, n_split = [], 0
    for s in sc.get("scenes", []):
        t = s.get("type")
        key = LIST_KEY.get(t)
        items = s.get(key) if key else None
        if not (isinstance(items, list) and len(items) > 1 and all(isinstance(x, str) for x in items)
                and isinstance(s.get("heading", ""), str)) or list_fits(s):
            out.append(s)
            continue
        chunks, start = [], 0
        while start < len(items):
            end = len(items)
            while end > start + 1 and not list_fits({**s, key: items[start:end]}):
                end -= 1
            chunks.append((start, end))
            start = end
        beats = s.get("beats") or []
        parts = []
        for k, (a, b) in enumerate(chunks, 1):
            part = dict(s)
            part["id"] = f"{s['id']}-{k}"
            part["heading"] = f"{s.get('heading', '')} ({k}/{len(chunks)})"
            part[key] = items[a:b]
            if beats:
                mine = []
                for bt in beats:
                    it = (bt.get("focus") or {}).get("item")
                    if isinstance(it, int) and 1 <= it <= len(items):
                        if a < it <= b:
                            nb = dict(bt)
                            nb["focus"] = {**bt["focus"], "item": it - a}
                            mine.append(nb)
                    elif k == 1:
                        mine.append(bt)
                part["beats"] = mine or [{"say": "", "pause": 3}]
            elif s.get("narration") and k > 1:
                part["narration"] = part.get("narration", "")
            parts.append(part)
        out.extend(parts)
        n_split += 1
    sc["scenes"] = out
    return n_split


# ---- drawing ----------------------------------------------------------------
class Slide:
    """Draws one frame of a scene for a given focus; `state` carries reveal progress across beats."""

    def __init__(self, sc: dict, s: dict, idx: int, total: int, section: int = 0):
        self.sc, self.s, self.idx, self.total = sc, s, idx, total
        self.ACC, self.ACC_DEEP, self.ACC_DIM = accent_for(section)
        self.state = {"revealed": 0, "item": None}
        self.im = None
        self.d = None

    # text helpers
    def T(self, xy, s, font, fill=None, anchor="la"):
        draw_text(self.d, xy, str(s), font, TEXT if fill is None else fill, anchor)

    def wrap(self, text: str, f, maxw: int):
        lines, cur = [], ""
        for word in text.split():
            trial = (cur + " " + word).strip()
            if text_width(trial, f) <= maxw or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    def text(self, xy, s, f, fill=None, maxw=None, lh=1.3, anchor="la"):
        fill = TEXT if fill is None else fill
        x, y = xy
        lines = self.wrap(s, f, maxw) if maxw else [s]
        step = int(f.size * lh)
        for ln in lines:
            self.T((x, y), ln, font=f, fill=fill, anchor=anchor)
            y += step
        return y

    def new(self):
        self.im = Image.new("RGB", (W, H), BG)
        self.d = ImageDraw.Draw(self.im)

    # chrome shared by all slides
    def chrome(self, page=None, crumb=None):
        d = self.d
        d.rectangle([80, 62, 96, 78], fill=self.ACC)
        d.text((112, 58), "LECTURE", font=font("space-grotesk-700", 24), fill=TEXT)
        d.text((230, 64), "TOOL", font=font("space-mono-400", 15), fill=FAINT)
        if crumb:
            self.T((W - 80, 70), crumb.upper(), font=font("space-mono-400", 17), fill=FAINT, anchor="ra")
        d.rectangle([80, H - 70, W - 80, H - 68], fill=LINE)
        frac = (self.idx + 1) / self.total
        d.rectangle([80, H - 70, 80 + int((W - 160) * frac), H - 68], fill=self.ACC)
        self.T((80, H - 52), self.sc.get("title", ""), font=font("space-mono-400", 15), fill=FAINT)
        right = f"{self.idx + 1} / {self.total}"
        if page:
            right = f"p.{page}   ·   " + right
        self.T((W - 80, H - 52), right, font=font("space-mono-400", 15), fill=FAINT, anchor="ra")

    def heading(self, s, size=58, y=150, fill=None):
        return self.text((80, y), s, font("space-grotesk-600", size), fill=TEXT if fill is None else fill, maxw=W - 160, lh=1.15)

    # --- scene types (each returns the drawn image for `focus`) ---
    def title(self, focus):
        s = self.s
        self.new()
        self.chrome()
        self.d.rectangle([80, 400, 140, 406], fill=self.ACC)
        y = self.text((80, 440), s.get("heading", ""), font("space-grotesk-700", 92), maxw=W - 300, lh=1.08)
        if s.get("sub"):
            self.text((80, y + 24), s["sub"], font("inter-400", 36), fill=MUTED, maxw=W - 300)
        return self.im

    def section(self, focus):
        s = self.s
        self.new()
        self.chrome(crumb="section")
        self.d.text((80, 390), "SECTION", font=font("space-mono-400", 22), fill=self.ACC)
        y = self.text((80, 430), s.get("heading", ""), font("space-grotesk-700", 84), maxw=W - 300, lh=1.08)
        if s.get("sub"):
            self.text((80, y + 24), s["sub"], font("inter-400", 36), fill=MUTED, maxw=W - 300)
        return self.im

    def _list(self, focus, numbered=False, boxed=False):
        s, st = self.s, self.state
        items = s[LIST_KEY[s["type"]]]
        if focus.get("item"):
            st["item"] = focus["item"]
            st["revealed"] = max(st["revealed"], focus["item"])
        elif st["revealed"] == 0:          # a beat without focus before any item: show nothing yet
            st["item"] = None
        shown, cur = st["revealed"], st["item"]
        self.new()
        self.chrome(page=s.get("page"), crumb=s.get("crumb"))
        y = self.heading(s.get("heading", "")) + 40
        fbody, fnum = font("inter-400", 38), font("space-mono-700", 30)
        x = 80
        for i, item in enumerate(items[:shown]):
            on = (i + 1) == cur
            col = TEXT if on else (MUTED if cur else TEXT)
            top = y
            if numbered:
                self.T((x, y + 6), f"{i + 1:02d}", font=fnum, fill=self.ACC if on else self.ACC_DEEP)
                tx = x + 70
            else:
                self.d.rectangle([x, y + 16, x + 14, y + 30], fill=self.ACC if on else self.ACC_DEEP)
                tx = x + 40
            if boxed:
                lines = self.wrap(item, fbody, W - tx - 120)
                hgt = int(fbody.size * 1.3) * len(lines) + 28
                if y - 8 + hgt > H - 100:
                    break
                self.d.rounded_rectangle([tx - 20, y - 8, W - 100, y - 8 + hgt], 12,
                                         fill=PANEL if not on else self.ACC_DIM,
                                         outline=LINE if not on else self.ACC_DEEP)
                self.text((tx, y + 6), item, fbody, fill=col, maxw=W - tx - 120)
                y = y - 8 + hgt + 16
            else:
                if y + int(fbody.size * 1.3) * len(self.wrap(item, fbody, W - tx - 120)) > H - 100:
                    break
                y = self.text((tx, y), item, fbody, fill=col, maxw=W - tx - 120) + 18
            if on and not boxed:               # active marker: lime bar in the margin
                self.d.rounded_rectangle([x - 28, top + 4, x - 20, y - 22], 4, fill=self.ACC)
            if y > H - 140:
                break
        return self.im

    def bullets(self, focus):
        return self._list(focus)

    def steps(self, focus):
        return self._list(focus, numbered=True)

    def quiz(self, focus):
        return self._list(focus, numbered=True, boxed=True)

    def exercise(self, focus):
        """Task panel, then the solution steps revealed one by one, then the boxed result."""
        s, st = self.s, self.state
        if focus.get("task"):
            st["task"], st["item"] = True, None
        if focus.get("item"):
            st["task"], st["item"] = True, focus["item"]
            st["revealed"] = max(st["revealed"], focus["item"])
        if focus.get("result"):
            st["task"], st["result"], st["item"] = True, True, None
        self.new()
        self.chrome(page=s.get("page"), crumb=s.get("crumb") or "exercise")
        y = self.heading(s.get("heading", ""), size=46, y=130) + 26
        ftask, lh = font("inter-400", 34), int(34 * 1.35)
        if st.get("task"):
            lines = [ln for para in s["task"].split("\n") for ln in (self.wrap(para, ftask, W - 240) or [""])]
            hgt = lh * len(lines) + 40
            active = st["revealed"] == 0 and not st.get("result")
            self.d.rounded_rectangle([80, y, W - 80, y + hgt], 14, fill=self.ACC_DIM if active else PANEL,
                                     outline=self.ACC_DEEP if active else LINE)
            self.d.rectangle([80, y + 14, 86, y + hgt - 14], fill=self.ACC if active else LINE)
            ty = y + 20
            for ln in lines:
                self.T((116, ty), ln, font=ftask, fill=TEXT if active else MUTED)
                ty += lh
            y += hgt + 28
        fbody, fnum = font("inter-400", 34), font("space-mono-700", 26)
        shown, cur = st["revealed"], st["item"]
        for i, item in enumerate(s["steps"][:shown]):
            on = (i + 1) == cur
            col = TEXT if on else (MUTED if cur else TEXT)
            top = y
            self.T((80, y + 6), f"{i + 1:02d}", font=fnum, fill=self.ACC if on else self.ACC_DEEP)
            y = self.text((150, y), item, fbody, fill=col, maxw=W - 150 - 120) + 14
            if on:
                self.d.rounded_rectangle([52, top + 4, 60, y - 16], 4, fill=self.ACC)
            if y > H - 210:
                break
        if st.get("result") and s.get("result"):
            fres, flab = font("space-grotesk-600", 40), font("space-mono-400", 20)
            label = RESULT_LABEL.get(self.sc.get("lang", "en"), RESULT_LABEL["en"])
            lw = text_width(label, flab) + 36
            lines = self.wrap(s["result"], fres, W - 240 - lw)
            hgt = int(40 * 1.3) * len(lines) + 36
            ry = min(y + 8, H - 92 - hgt)
            self.d.rounded_rectangle([80, ry, W - 80, ry + hgt], 14, fill=self.ACC_DIM, outline=self.ACC)
            self.T((110, ry + 30), label, font=flab, fill=self.ACC)
            self.text((110 + lw, ry + 16), s["result"], fres, fill=TEXT, maxw=W - 240 - lw)
        return self.im

    def code(self, focus):
        s = self.s
        self.new()
        self.chrome(page=s.get("page"), crumb=s.get("crumb") or "code")
        y = self.heading(s.get("heading", ""), size=46, y=130) + 24 if s.get("heading") else 130
        lines = s["code"].rstrip("\n").split("\n")
        n = len(lines)
        pad = 28
        avail = (H - 110 - (70 if s.get("caption") else 0)) - y - 2 * pad
        size = max(18, min(34, int(avail / n / 1.45)))
        f = font("space-mono-400", size)
        lh = int(size * 1.45)
        box_top = y
        box_h = n * lh + 2 * pad
        self.d.rounded_rectangle([80, box_top, W - 80, box_top + box_h], 16, fill=INSET, outline=LINE)
        if focus.get("lines"):
            a, b = focus["lines"][0], focus["lines"][-1]
            hl = set(range(a, b + 1))
        else:
            hl = set(s.get("highlight") or [])
        gutter = f.getlength("000") + 36
        for i, ln in enumerate(lines, 1):
            ly = box_top + pad + (i - 1) * lh
            if i in hl:
                self.d.rectangle([96, ly - 4, W - 96, ly + lh - 6], fill=self.ACC_DIM)
                self.d.rectangle([96, ly - 4, 101, ly + lh - 6], fill=self.ACC)
            self.d.text((110, ly), f"{i:>3}", font=f, fill=self.ACC if i in hl else FAINT)
            self._code_line(110 + gutter, ly, ln, f, bright=(not hl or i in hl))
        if s.get("caption"):
            self.text((80, box_top + box_h + 22), s["caption"], font("inter-400", 28), fill=MUTED, maxw=W - 160)
        return self.im

    def _code_line(self, x, y, ln, f, bright=True):
        kw = {"int", "double", "float", "bool", "void", "return", "if", "else", "for", "while",
              "namespace", "using", "assert", "true", "false", "const", "auto", "#include"}
        base = TEXT if bright else DIM
        m = re.match(r"^(.*?)(//.*)$", ln)
        code_part, comment = (m.group(1), m.group(2)) if m else (ln, "")
        for tok in re.findall(r"\"[^\"]*\"|\w+|\s+|[^\w\s]+", code_part):
            col = base
            if tok.strip() in kw:
                col = self.ACC if bright else self.ACC_DEEP
            elif tok.startswith('"'):
                col = CODE_STR if bright else DIM
            self.T((x, y), tok, font=f, fill=col)
            x += text_width(tok, f)
        if comment:
            self.T((x, y), comment, font=f, fill=FAINT)

    def formula(self, focus):
        s = self.s
        self.new()
        self.chrome(page=s.get("page"), crumb=s.get("crumb"))
        self.heading(s.get("heading", ""), size=50, y=140)
        lines = s["formula"].split("\n")
        part = focus.get("part")
        fh = int(64 * 1.4) * len(lines)
        top = (H - fh) // 2 + 20
        self.d.rounded_rectangle([80, top - 50, W - 80, top + fh + 50], 18, fill=PANEL, outline=LINE)
        self.d.rectangle([80, top - 50, 86, top + fh + 50], fill=self.ACC)
        y = top
        for ln in lines:
            f = font("space-grotesk-500", 64)
            if text_width(ln, f) > W - 240:
                f = font("space-grotesk-500", 48)
            x = (W - text_width(ln, f)) / 2
            if part and part in ln:
                pre, post = ln.split(part, 1)
                dimmed = DIM
                self.T((x, y), pre, font=f, fill=dimmed)
                x += text_width(pre, f)
                pw = text_width(part, f)
                self.d.rounded_rectangle([x - 10, y - 8, x + pw + 10, y + f.size + 14], 10, fill=self.ACC_DIM)
                self.d.rectangle([x - 10, y + f.size + 10, x + pw + 10, y + f.size + 14], fill=self.ACC)
                self.T((x, y), part, font=f, fill=self.ACC)
                x += pw
                self.T((x, y), post, font=f, fill=dimmed)
            else:
                self.T((x, y), ln, font=f, fill=TEXT)
            y += int(64 * 1.4)
        if s.get("caption"):
            self.text((W // 2, top + fh + 90), s["caption"], font("inter-400", 30), fill=MUTED, anchor="ma", maxw=W - 300)
        return self.im

    def memory(self, focus):
        """Stack frames as rows of cells, like the figures on pp.76–78; `focus.cells` lights up cells."""
        s = self.s
        self.new()
        self.chrome(page=s.get("page"), crumb=s.get("crumb") or "memory")
        self.heading(s.get("heading", ""), size=46, y=130)
        frames = focus.get("frames") or s["frames"]
        lit = set(focus.get("cells") or [])
        cell_w, cell_h = 150, 92
        fval, fname, flab = font("space-mono-700", 34), font("space-mono-400", 24), font("space-mono-400", 24)
        total_w = sum(len(fr["cells"]) * cell_w + 90 for fr in frames) + 60
        x = max(80, (W - total_w) // 2)
        y = 520
        ex = x - 40
        while ex > 80:
            self.d.rectangle([ex - 40, y, ex, y + cell_h], outline=LINE)
            ex -= 40
        for fi, fr in enumerate(frames):
            col = self.ACC if fi == 0 and len(frames) > 1 else RED if len(frames) > 1 else self.ACC
            if fr.get("color") == "red":
                col = RED
            if fr.get("color") == "lime":
                col = self.ACC
            for c in fr["cells"]:
                key = f"{fr.get('label')}.{c.get('name')}"
                hl = (key in lit) if lit else bool(c.get("hl"))
                self.d.rectangle([x, y, x + cell_w, y + cell_h], fill=PANEL2 if not hl else self.ACC_DIM, outline=col, width=3)
                self.T((x + cell_w / 2, y + cell_h / 2), c.get("value", ""), font=fval,
                       fill=TEXT if not hl else self.ACC, anchor="mm")
                self.T((x + cell_w / 2, y - 36), c.get("name", ""), font=fname, fill=col, anchor="mm")
                self.d.line([x + cell_w / 2, y - 18, x + cell_w / 2, y - 4], fill=col, width=2)
                if hl and lit:                   # pointer above the cell the narration talks about
                    cx = x + cell_w / 2
                    self.d.polygon([(cx - 14, y - 78), (cx + 14, y - 78), (cx, y - 58)], fill=self.ACC)
                x += cell_w
            self.d.rectangle([x, y, x + 60, y + cell_h], outline=LINE)
            self.T((x + 30, y + cell_h / 2), "…", font=fval, fill=FAINT, anchor="mm")
            x += 60
            self.d.line([x, y - 10, x, y + cell_h + 50], fill=col, width=3)
            self.T((x - 12, y + cell_h + 22), fr.get("label", "") + " ←", font=flab, fill=col, anchor="ra")
            x += 30
        if s.get("caption"):
            self.text((80, y + cell_h + 110), s["caption"], font("inter-400", 30), fill=MUTED, maxw=W - 160)
        return self.im

    def figure(self, focus):
        """A crop of an original page image (diagram, plot, structure, photo of notes), fitted to the
        slide; beats can zoom into a region and draw a highlight box around what is being explained."""
        s = self.s
        self.new()
        self.chrome(page=s.get("page"), crumb=s.get("crumb") or "from the source")
        y = self.heading(s.get("heading", ""), size=46, y=130) + 20 if s.get("heading") else 130
        pages_dir = Path(self.job) / "pages" if getattr(self, "job", None) else None
        src = pages_dir / f"page-{s['page']:03d}.png" if pages_dir else None
        box_top, box_bottom = y, H - 110 - (70 if s.get("caption") else 0)
        if not src or not src.exists():
            self.T((W // 2, (box_top + box_bottom) // 2), f"page image {s.get('page')} not found", font("inter-400", 30),
                   fill=RED, anchor="ma")
            return self.im
        im = Image.open(src).convert("RGB")
        pw, ph = im.size
        c = s.get("crop") or [0, 0, 1, 1]
        im = im.crop((int(c[0] * pw), int(c[1] * ph), int(c[2] * pw), int(c[3] * ph)))
        if focus.get("zoom"):
            z = focus["zoom"]
            cw, ch = im.size
            im = im.crop((int(z[0] * cw), int(z[1] * ch), int(z[2] * cw), int(z[3] * ch)))
        # fit into the content box keeping the aspect ratio
        maxw, maxh = W - 200, box_bottom - box_top - 24
        scale = min(maxw / im.width, maxh / im.height)
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
        x0 = (W - im.width) // 2
        y0 = box_top + 12 + (maxh - im.height) // 2
        if focus.get("box"):
            b = focus["box"]
            dark = Image.new("RGB", im.size, BOX_DIM)
            mask = Image.new("L", im.size, 110)                  # darken everything…
            ImageDraw.Draw(mask).rectangle([int(b[0] * im.width), int(b[1] * im.height),
                                            int(b[2] * im.width), int(b[3] * im.height)], fill=0)   # …except the box
            im = Image.composite(dark, im, mask)
        # rounded corners + hairline frame
        corner = Image.new("L", im.size, 0)
        ImageDraw.Draw(corner).rounded_rectangle([0, 0, im.width - 1, im.height - 1], 14, fill=255)
        self.im.paste(im, (x0, y0), corner)
        if FIGURE_BORDER:
            self.d.rounded_rectangle([x0 - 1, y0 - 1, x0 + im.width, y0 + im.height], 14, outline=FIGURE_BORDER)
        if focus.get("box"):
            b = focus["box"]
            self.d.rounded_rectangle([x0 + int(b[0] * im.width) - 4, y0 + int(b[1] * im.height) - 4,
                                      x0 + int(b[2] * im.width) + 4, y0 + int(b[3] * im.height) + 4],
                                     8, outline=self.ACC, width=4)
        if s.get("caption"):
            self.text((W // 2, box_bottom + 20), s["caption"], font("inter-400", 28), fill=MUTED, anchor="ma",
                      maxw=W - 300)
        return self.im

    def frame(self, focus: dict):
        return getattr(self, self.s["type"])(focus or {})


# ---- audio ------------------------------------------------------------------
EL_DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"      # George
EL_DEFAULT_MODEL = "eleven_turbo_v2_5"         # half the credits of multilingual_v2, near-identical quality
EL_CREDIT_FACTOR = {"eleven_turbo_v2_5": 0.5, "eleven_flash_v2_5": 0.5, "eleven_flash_v2": 0.5}


def el_config(sc: dict):
    load_env()
    return (sc.get("voice_id") or os.environ.get("ELEVENLABS_VOICE_ID") or EL_DEFAULT_VOICE,
            os.environ.get("ELEVENLABS_MODEL", EL_DEFAULT_MODEL))


def audio_key(sc: dict, s: dict, tts_mode: str) -> str:
    """Hash of everything that changes the narration audio; used to reuse scene audio across runs."""
    import hashlib
    text = narration_of(s)
    if tts_mode == "elevenlabs":
        vid, model = el_config(sc)
        src = f"el|{vid}|{model}|{text}"
    else:
        src = f"say|{s.get('voice') or sc.get('voice') or VOICES.get(sc.get('lang', 'en'))}|{sc.get('rate', 172)}|{text}"
    return hashlib.sha1(src.encode()).hexdigest()


def cached(sdir: Path, key: str) -> bool:
    return (sdir / "audio.m4a").exists() and (sdir / "audio.key").read_text().strip() == key \
        if (sdir / "audio.key").exists() else False


def elevenlabs_quota():
    load_env()
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return None
    try:
        req = urllib.request.Request("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": key})
        d = json.load(urllib.request.urlopen(req, timeout=20))
        return d["character_limit"] - d["character_count"], d["character_limit"]
    except Exception:  # noqa: BLE001
        return None


def el_estimate(job: Path, sc: dict, workdir: Path):
    _, model = el_config(sc)
    factor = EL_CREDIT_FACTOR.get(model, 1.0)
    chars = sum(len(narration_of(s)) for s in sc["scenes"]
                if not cached(workdir / s["id"], audio_key(sc, s, "elevenlabs")))
    return chars, int(chars * factor), model


def tts(job: Path, sc: dict, s: dict, out: Path, silent: bool):
    """Narration audio as 44.1 kHz stereo AAC at `out`. Returns (duration, char_start_times | None)."""
    text = narration_of(s)
    sdir = out.parent
    if silent or not text:
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "3.0",
             "-c:a", "aac", str(out)])
        return duration_of(out), None
    tts_mode = json.loads((job / "job.json").read_text()).get("tts", "say")
    load_env()
    if tts_mode == "elevenlabs" and not os.environ.get("ELEVENLABS_API_KEY"):
        log(job, "ELEVENLABS_API_KEY not set (environment or .env) — falling back to macOS say")
        tts_mode = "say"
    key = audio_key(sc, s, tts_mode)
    if cached(sdir, key):
        al = sdir / "alignment.json"
        return duration_of(out), (json.loads(al.read_text()) if al.exists() else None)
    (sdir / "alignment.json").unlink(missing_ok=True)
    align = None
    if tts_mode == "elevenlabs":
        vid, model = el_config(sc)
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}/with-timestamps?output_format=mp3_44100_128",
            data=json.dumps({"text": text, "model_id": model,
                             "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                                                "style": 0.2, "use_speaker_boost": True}}).encode(),
            headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json"})
        tmp = out.with_suffix(".mp3")
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    resp = json.load(r)
                break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if isinstance(e, urllib.error.HTTPError):
                    raise
                if attempt < 5:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise RuntimeError(f"ElevenLabs unreachable for {s['id']}: {e}") from e
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code == 429 and "quota" not in body and attempt < 5:
                    # several scenes synthesize at once; above the plan's concurrency limit ElevenLabs
                    # answers 429 (too_many_concurrent_requests) — back off and try again
                    time.sleep(2 * (attempt + 1))
                    continue
                hint = ""
                if "quota_exceeded" in body:
                    hint = " — ElevenLabs credits used up; audio already made is kept, rerun 're-render video' when credits are back"
                raise RuntimeError(f"ElevenLabs {e.code} for {s['id']}{hint}: {body[:300]}")
        tmp.write_bytes(base64.b64decode(resp["audio_base64"]))
        a = resp.get("alignment") or {}
        chars, starts = a.get("characters") or [], a.get("character_start_times_seconds") or []
        if chars and len(chars) == len(starts):
            # map every index of `text` to a start time (alignment chars follow the input text)
            align = starts if "".join(chars) == text else _project_times("".join(chars), starts, text)
            (sdir / "alignment.json").write_text(json.dumps(align))
    else:
        voice = s.get("voice") or sc.get("voice") or VOICES.get(sc.get("lang", "en"), "Samantha")
        txt = out.with_suffix(".txt")
        txt.write_text(text, encoding="utf-8")
        tmp = out.with_suffix(".aiff")
        run(["say", "-v", voice, "-r", str(sc.get("rate", 172)), "-f", str(txt), "-o", str(tmp)])
    # pad 0.6 s of silence after the narration so scenes breathe
    run(["ffmpeg", "-y", "-i", str(tmp), "-af", "apad=pad_dur=0.6", "-ar", "44100", "-ac", "2",
         "-c:a", "aac", "-b:a", "128k", str(out)])
    (sdir / "audio.key").write_text(key)
    return duration_of(out), align


def _project_times(spoken: str, starts, text: str):
    """Alignment text differs slightly from input (normalisation): map by relative position."""
    n, m = len(spoken), len(text)
    return [starts[min(n - 1, int(i / max(m, 1) * n))] for i in range(m)]


# ---- per-scene video --------------------------------------------------------
def beat_times(s: dict, dur: float, align):
    """Start time of each beat within the scene audio."""
    beats = beats_of(s)
    text = narration_of(s)
    offs, pos = [], 0
    for b in beats:
        j = text.find(b["say"], pos)
        offs.append(j if j >= 0 else pos)
        pos = (j if j >= 0 else pos) + len(b["say"])
    speech = max(dur - 0.6, 0.5)
    if align and len(align) >= len(text) > 0:
        times = [float(align[min(o, len(align) - 1)]) for o in offs]
    else:
        times = [speech * o / max(len(text), 1) for o in offs]
    times[0] = 0.0
    return beats, times


def pause_frame(im, remaining: float, total: float, lang: str, acc=None):
    acc = acc or ACCENTS["moss"][0]
    """The beat's frame with a small 'try it yourself' countdown panel bottom-right."""
    im = im.copy()
    d = ImageDraw.Draw(im)
    bw, bh = 540, 96
    x0, y0 = W - 80 - bw, H - 70 - 26 - bh
    d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], 16, fill=PANEL2, outline=LINE)
    draw_text(d, (x0 + 24, y0 + 16), PAUSE_LABEL.get(lang, PAUSE_LABEL["en"]), font("space-grotesk-600", 28), TEXT)
    draw_text(d, (x0 + bw - 24, y0 + 18), f"{int(math.ceil(remaining))} s", font("space-mono-400", 26), acc, anchor="ra")
    d.rectangle([x0 + 24, y0 + bh - 26, x0 + bw - 24, y0 + bh - 18], fill=LINE)
    d.rectangle([x0 + 24, y0 + bh - 26, x0 + 24 + int((bw - 48) * max(remaining, 0) / max(total, 0.1)), y0 + bh - 18], fill=acc)
    return im


def insert_pauses(audio: Path, out: Path, dur: float, times, pauses):
    """Cut `pauses[i]` seconds of silence into the narration right after beat i's words (i.e. where
    beat i+1 would start). Returns (new audio path, new duration, shifted beat start times)."""
    n = len(times)
    cuts = [(times[i + 1] if i + 1 < n else dur, p) for i, p in enumerate(pauses) if p > 0]
    filt, labels, prev, k = [], [], 0.0, 0
    for cut, p in cuts:
        cut = max(cut, prev + 0.01)
        filt.append(f"[0:a]atrim=start={prev:.3f}:end={cut:.3f},asetpts=PTS-STARTPTS[a{k}]")
        labels.append(f"[a{k}]")
        filt.append(f"anullsrc=r=44100:cl=stereo:d={p:.3f}[s{k}]")
        labels.append(f"[s{k}]")
        prev, k = cut, k + 1
    filt.append(f"[0:a]atrim=start={prev:.3f},asetpts=PTS-STARTPTS[a{k}]")
    labels.append(f"[a{k}]")
    filt.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]")
    run(["ffmpeg", "-y", "-i", str(audio), "-filter_complex", ";".join(filt), "-map", "[out]",
         "-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "128k", str(out)])
    shift, shifted = 0.0, []
    for i, t in enumerate(times):
        shifted.append(t + shift)
        shift += pauses[i]
    return out, duration_of(out), shifted


def render_scene(job: Path, sc: dict, s: dict, idx: int, total: int, workdir: Path, silent: bool,
                 prev_last=None, publish=None):
    """Encode one scene. If `prev_last` (last frame of the previous scene) is given, the scene opens
    with a cross-dissolve from it and its audio is delayed by the same amount, so joining scenes
    with the plain concat demuxer yields seamless transitions — no black, no xfade chains.

    `prev_last` may be an Image or a Future that resolves to one: everything that does not depend on
    the previous scene (narration, beat frames) is done first, then this scene's own last frame is
    handed to `publish(image)` so the next scene can go on, and only then the entry dissolve is
    drawn. That is what lets scenes render in parallel while the frames stay exactly the same."""
    sid = s["id"]
    sdir = workdir / sid
    sdir.mkdir(parents=True, exist_ok=True)
    audio = sdir / ("audio_silent.m4a" if silent else "audio.m4a")
    dur, align = tts(job, sc, s, audio, silent)
    beats, times = beat_times(s, dur, align)
    pauses = [b.get("pause", 0.0) for b in beats]
    if silent:
        pauses = [0.0] * len(pauses)                            # a layout preview does not wait out the pauses
    if any(pauses):                                             # think-pauses: silence cut into the narration
        audio, dur, times = insert_pauses(audio, sdir / "audio_p.m4a", dur, times, pauses)
    section = sum(1 for x in sc["scenes"][:idx + 1] if x["type"] == "section")
    slide = Slide(sc, s, idx, total, section=max(section - 1, 0))
    slide.job = job
    for old in sdir.glob("f*.png"):
        old.unlink()
    lst = sdir / "frames.txt"
    cues = []
    prev = first = None
    lead = XFADE if prev_last is not None else 0.0
    times = [t + lead for t in times]
    dur += lead
    lines = []                                                  # frames.txt entries for the beats
    for i, (b, t0) in enumerate(zip(beats, times)):
        t1 = times[i + 1] if i + 1 < len(times) else dur
        pz = pauses[i]
        d = max(t1 - t0 - pz, 0.05)                             # the spoken part; the pause follows it
        im = slide.frame(b["focus"])
        if first is None:
            first = im                                          # beat 1 drawn from a fresh reveal state
        # cross-dissolve from the previous beat's frame: short, so the highlight glides instead of jumping
        if prev is not None and d > 0.6:
            steps = 8
            for k in range(1, steps + 1):
                mid = Image.blend(prev, im, k / (steps + 1))
                p = sdir / f"f{i:02d}_{k:02d}.png"
                mid.save(p)
                lines.append(f"file '{p.name}'\nduration {TWEEN / steps:.4f}\n")
            d -= TWEEN
        fp = sdir / f"f{i:02d}.png"
        im.save(fp)
        lines.append(f"file '{fp.name}'\nduration {d:.3f}\n")
        if b["say"]:
            cues.append((t0, min(t1 - pz, dur - 0.3), b["say"]))
        prev = im
        if pz > 0:                                              # countdown frames, one per second
            k, left = 0, pz
            while left > 0.01:
                seg = min(1.0, left)
                pf = pause_frame(im, left, pz, sc.get("lang", "en"), slide.ACC)
                pp = sdir / f"f{i:02d}_p{k:02d}.png"
                pf.save(pp)
                lines.append(f"file '{pp.name}'\nduration {seg:.3f}\n")
                prev, k, left = pf, k + 1, left - seg
    lines.append(f"file '{Path(lines[-1].split(chr(39))[1]).name}'\n")      # concat demuxer: repeat the last frame
    if publish is not None:
        publish(prev)                                           # the next scene may start its entry dissolve now
    entry = []
    if prev_last is not None:
        if hasattr(prev_last, "result"):
            prev_last = prev_last.result()                      # wait for the previous scene's last frame
        steps = 10
        for k in range(1, steps + 1):
            p = sdir / f"f00_in{k:02d}.png"
            Image.blend(prev_last, first, k / (steps + 1)).save(p)
            entry.append(f"file '{p.name}'\nduration {XFADE / steps:.4f}\n")
        delayed = sdir / "audio_in.m4a"
        run(["ffmpeg", "-y", "-i", str(audio), "-af", f"adelay={int(XFADE * 1000)}|{int(XFADE * 1000)}",
             "-c:a", "aac", "-b:a", "128k", str(delayed)])
        audio = delayed
    with open(lst, "w") as f:
        f.write("".join(entry + lines))
    vf = f"scale={W}:{H}"                                      # the title page shows from the first frame: no fade from black
    if idx == total - 1:
        vf += f",fade=t=out:st={max(dur - 0.8, 0):.3f}:d=0.8"
    out = sdir / "scene.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(audio),
         "-vf", vf + ",format=yuv420p", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-c:a", "aac", "-b:a", "128k", "-frames:v", str(round(dur * FPS)), "-t", f"{dur + 1:.3f}",
         "-movflags", "+faststart", str(out)])
    for fp in sdir.glob("f*.png"):                              # frames are only needed for the encode
        fp.unlink(missing_ok=True)
    return out, dur, cues, None


# ---- main ---------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    job = Path(sys.argv[1]).resolve()
    flags = set(sys.argv[2:])
    only = None
    if "--scene" in sys.argv:
        k = sys.argv.index("--scene") + 1
        if k >= len(sys.argv):
            sys.exit("--scene needs a scene id, e.g. --scene s012")
        only = sys.argv[k]
    sc = json.loads((job / "scenes.json").read_text(encoding="utf-8"))
    global PAGE_COUNT
    try:
        PAGE_COUNT = int(json.loads((job / "ingest.json").read_text()).get("page_count", 0))
    except (OSError, ValueError):
        PAGE_COUNT = 0
    n_split = expand_long_lists(sc)
    if n_split:
        print(f"note: {n_split} list scene(s) were too long for one slide and continue on a second slide")
    errs = validate(sc)
    if errs:
        print("scenes.json has problems:")
        for e in errs:
            print(" -", e)
        sys.exit(1)
    if "--check" in flags:
        n = len(sc["scenes"])
        words = sum(len(narration_of(s).split()) for s in sc["scenes"])
        with_beats = sum(1 for s in sc["scenes"] if s.get("beats"))
        pauses = sum(b.get("pause", 0.0) for s in sc["scenes"] for b in beats_of(s))
        ex = sum(1 for s in sc["scenes"] if s["type"] == "exercise")
        print(f"ok: {n} scenes ({with_beats} with beats, {ex} exercises), {words} narration words "
              f"≈ {words / 160 + pauses / 60:.1f} min incl. {pauses / 60:.1f} min of think-pauses")
        # the compression ratio caps the narration: a multiple of the lecture's own word count
        try:
            ratio = json.loads((job / "job.json").read_text()).get("ratio", "lossless")
            lw = len((job / "lecture.md").read_text(encoding="utf-8").split())
        except OSError:
            ratio, lw = None, 0
        if lw:
            cap = int(lw * NARRATION_BUDGET.get(ratio, 2.4))
            print(f"budget at {ratio}: {cap} narration words ({NARRATION_BUDGET.get(ratio, 2.4)} × {lw} lecture words)")
            if words > cap * 1.15:
                print(f"too long: {words} narration words exceed the {ratio} budget of {cap} by more than 15 % — "
                      f"fold or drop scenes as the ratio rules say")
                sys.exit(1)
        return

    silent = "--silent" in flags
    set_theme(json.loads((job / "job.json").read_text()).get("theme", DEFAULT_THEME))
    workdir = job / "render"
    workdir.mkdir(exist_ok=True)       # scene audio is kept between runs and reused when unchanged
    scenes = sc["scenes"]
    total = len(scenes)
    tts_mode = json.loads((job / "job.json").read_text()).get("tts", "say")
    msg = ""
    if tts_mode == "elevenlabs" and not silent and only is None:
        chars, credits, model = el_estimate(job, sc, workdir)
        quota = elevenlabs_quota()
        msg = f"ElevenLabs ({model}): {chars} characters still to synthesize ≈ {credits} credits"
        if quota:
            msg += f"; {quota[0]} of {quota[1]} credits left this month"
        print(msg)
        if "--estimate" in flags:
            return
        if quota and credits > quota[0]:
            (job / "render.log").unlink(missing_ok=True)
            raise RuntimeError(f"not enough ElevenLabs credits: need ≈ {credits}, have {quota[0]} "
                               f"— top up / wait for the monthly reset, or switch the job's voice to macOS say")
    elif "--estimate" in flags:
        print("voice: macOS say — no credits needed")
        return
    if only:
        s = next((x for x in scenes if x["id"] == only), None)
        if s is None:
            sys.exit(f"no scene {only!r} in scenes.json")
        out, dur, _, _ = render_scene(job, sc, s, scenes.index(s), total, workdir, silent)
        print(f"{out}  ({dur:.1f}s)")
        return
    (job / "render.log").unlink(missing_ok=True)
    if msg:
        log(job, msg)

    workers = max(1, int(os.environ.get("RENDER_WORKERS") or 3))
    status(job, "render", f"rendering {total} scenes")
    log(job, f"render: {total} scenes, tts={'silent' if silent else tts_mode}, workers={workers}")

    # Scenes render in a thread pool. Scene i only depends on scene i-1 through its last frame
    # (entry cross-dissolve); `lasts[i]` is resolved by render_scene as soon as scene i has drawn
    # its beat frames, long before its encode finishes, so the chain never stalls the pool. Tasks
    # are submitted in order and a running task publishes before it waits, so there is no deadlock.
    lasts = [concurrent.futures.Future() for _ in scenes]
    finished, lock = set(), threading.Lock()

    def heading(k):
        return scenes[k].get("heading", scenes[k]["id"])[:60]

    def work(i):
        s = scenes[i]
        me = lasts[i]
        try:
            res = render_scene(job, sc, s, i, total, workdir, silent,
                               prev_last=lasts[i - 1] if i else None, publish=me.set_result)
        except BaseException as e:  # noqa: BLE001 — unblock the successor, which then fails too
            if not me.done():
                me.set_exception(e)
            raise
        if i:
            lasts[i - 1] = None                                 # free the predecessor's frame
        with lock:
            finished.add(i)
            # progress = scenes completed; "scene N/M" means N-1 scenes are done, as in the sequential render
            nxt = next((k for k in range(total) if k not in finished), None)
            if nxt is not None:
                status(job, "render", f"scene {nxt + 1}/{total}: {heading(nxt)}")
        return res

    status(job, "render", f"scene 1/{total}: {heading(0)}")
    parts, t, chapters, srt = [], 0.0, [], []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futs = [pool.submit(work, i) for i in range(total)]
        for i, (s, fut) in enumerate(zip(scenes, futs)):
            out, dur, cues, _ = fut.result()
            parts.append(out)
            if s["type"] in ("title", "section", "exercise") or i == 0:
                # every section and every exercise is a chapter, so the viewer can jump straight to a task
                chapters.append({"start": round(t, 3), "title": s.get("heading", s["id"]), "scene": s["id"],
                                 "kind": "exercise" if s["type"] == "exercise" else "section"})
            srt += [(t + a, t + b, x) for a, b, x in cues]
            log(job, f"{s['id']:>4} {s['type']:<8} {dur:6.1f}s  {len(cues):2d} beats  {s.get('heading', '')[:46]}")
            t += dur
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)         # scenes already running finish; nothing new starts
        raise
    pool.shutdown(wait=True)

    status(job, "render", "joining scenes")
    lst = workdir / "all.txt"
    lst.write_text("".join(f"file '{p.relative_to(workdir)}'\n" for p in parts))
    joined = workdir / "joined.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)])
    meta = workdir / "chapters.ffmeta"
    with open(meta, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        f.write(f"title={sc.get('title', '')}\n")
        for k, ch in enumerate(chapters):
            end = chapters[k + 1]["start"] if k + 1 < len(chapters) else t
            f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(ch['start'] * 1000)}\nEND={int(end * 1000)}\n"
                    f"title={ch['title']}\n")
    final = job / "lecture.mp4"
    run(["ffmpeg", "-y", "-i", str(joined), "-i", str(meta), "-map_metadata", "1",
         "-c", "copy", "-movflags", "+faststart", str(final)])
    joined.unlink(missing_ok=True)
    with open(job / "lecture.srt", "w", encoding="utf-8") as f:
        for n, (a, b, x) in enumerate(srt, 1):
            f.write(f"{n}\n{srt_time(a)} --> {srt_time(max(b, a + 0.3))}\n{x}\n\n")
    (job / "chapters.json").write_text(json.dumps(
        {"duration": round(t, 3), "chapters": chapters}, indent=2, ensure_ascii=False), encoding="utf-8")
    log(job, f"done: lecture.mp4 {t / 60:.1f} min, {len(chapters)} chapters, {len(srt)} subtitle cues")
    status(job, "done", f"lecture.mp4 ready — {t / 60:.1f} min, {total} scenes")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        job = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
        if job and job.exists():
            log(job, f"ERROR: {e}")
            status(job, "error", f"render failed: {str(e).splitlines()[0][:120]}")
        raise
