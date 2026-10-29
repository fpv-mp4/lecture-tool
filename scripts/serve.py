#!/usr/bin/env python3
"""Local control panel for the lecture tool.

    python scripts/serve.py            # serves http://localhost:8765 and opens it

No dependencies beyond the standard library. The page lets you create a job
(upload PDF + options) and watch its output folder; running the pipeline is
done by typing `/lecture <job_dir>` in Claude Code.

API (all JSON unless noted):
    GET  /api/jobs                       list jobs with status
    POST /api/jobs                       create job {course, chapter, title, ratio,
                                         output, output_lang, research_scope, tts, theme, video,
                                         files: [{name, b64}, ...]}   (PDF, images, .md/.txt;
                                         each becomes sources/NN-name.pdf; legacy: pdf_name, pdf_b64)
    GET  /api/job?dir=<rel>              job.json + status.json + file list
    GET  /api/file?dir=<rel>&name=<f>    raw file (md/json/mp4/png) from the job
    POST /api/run        {dir, from}     run the pipeline: spawns `claude -p "/lecture jobs/<dir>"`
                                         (from = all | lecture | scenes); output goes to run.log
    POST /api/stop       {dir}           terminate a running pipeline
    POST /api/job/update {dir, ratio, output, output_lang, research_scope, tts, theme, video,
                          model (sonnet | opus | default), title}
    DELETE /api/jobs?dir=<rel>           delete a job folder (asks in the UI first)

Hosted mode (LT_HOSTED=1, see docs/HOSTED.md): the same server for several people. Everyone
signs in, has a workspace jobs/<user_id>/ under LT_DATA, and sees only their own lectures;
runs are metered into <job>/cost.json and held against a monthly budget. Extra routes:
    GET  /api/me                         who am I, usage this month ({hosted: false} locally)
    POST /api/auth/signup | signin | signout | password
    GET/POST /api/admin/users, GET/POST/DELETE /api/admin/invites, GET /api/admin/usage
Nothing in hosted mode changes how the tool behaves locally.
"""
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"

# ---- configuration from the environment (docs/HOSTED.md §2) ----------------------
# Local mode is the default and keeps everything in the repo: jobs/ and .env next to the
# scripts. LT_HOSTED=1 turns on hosted mode: people sign in, each has a workspace
# jobs/<user_id>/, all data lives under LT_DATA (the volume) and runs are metered.
HOSTED = False
DATA = ROOT
JOBS = ROOT / "jobs"
ENV_FILE = ROOT / ".env"
USERS_FILE = ROOT / "users.json"
INVITES_FILE = ROOT / "invites.json"
USAGE_DIR = ROOT / "usage"
SECRET = ""                 # signs the session cookies (hosted only)
ADMIN_EMAIL = ""            # the owner: first sign-up with this address becomes admin
USER_BUDGET_USD = 20.0      # default monthly Claude budget per person
MAX_PARALLEL_RUNS = 3       # runs at the same time on this machine, all people together
PORT = 8765
PUBLIC_HOST = ""            # host the panel is reached at, for the same-origin check


def configure(env=None):
    """Read the LT_* variables into the module settings. Runs at import; the tests call it
    again with their own environment."""
    global HOSTED, DATA, JOBS, ENV_FILE, USERS_FILE, INVITES_FILE, USAGE_DIR, SECRET, ADMIN_EMAIL
    global USER_BUDGET_USD, MAX_PARALLEL_RUNS, PORT, PUBLIC_HOST
    env = os.environ if env is None else env
    HOSTED = env.get("LT_HOSTED", "") == "1"
    DATA = Path(env["LT_DATA"]).expanduser().resolve() if env.get("LT_DATA") else ROOT
    JOBS, ENV_FILE = DATA / "jobs", DATA / ".env"
    USERS_FILE, INVITES_FILE, USAGE_DIR = DATA / "users.json", DATA / "invites.json", DATA / "usage"
    SECRET = env.get("LT_SECRET") or ""
    ADMIN_EMAIL = (env.get("LT_ADMIN_EMAIL") or "").strip().lower()
    try:
        USER_BUDGET_USD = float(env.get("LT_USER_BUDGET_USD") or 20)
    except ValueError:
        USER_BUDGET_USD = 20.0
    try:
        MAX_PARALLEL_RUNS = max(1, int(env.get("LT_MAX_PARALLEL_RUNS") or 3))
    except ValueError:
        MAX_PARALLEL_RUNS = 3
    # PORT from the environment is how Fly tells the image where to listen; locally the port
    # stays 8765 (or the number on the command line) so nothing changes for the owner's Mac.
    PORT = int(env["PORT"]) if HOSTED and str(env.get("PORT") or "").isdigit() else 8765
    PUBLIC_HOST = (env.get("LT_PUBLIC_HOST") or "").strip().lower()


def check_config():
    """Hosted mode cannot run without its secrets; say so in plain words instead of failing on
    the first sign-in."""
    if not HOSTED:
        return
    problems = []
    if len(SECRET) < 32:
        problems.append("LT_SECRET is missing or shorter than 32 characters (it signs the sign-in cookies)")
    if "@" not in ADMIN_EMAIL:
        problems.append("LT_ADMIN_EMAIL is missing (the owner's e-mail; the first sign-up with it becomes admin)")
    if problems:
        raise SystemExit("hosted mode cannot start:\n  - " + "\n  - ".join(problems)
                         + "\nSet them as secrets of the deployment (docs/HOSTED-SETUP.md).")


configure()

SLUG_RE = re.compile(r"[^a-z0-9-]+")
RATIOS = ["1:1", "lossless", "1:1.5", "1:2", "1:3", "1:4", "1:6"]
OUTPUTS = ["summary", "lecture", "both"]
SHOWN_FILES = ["summary.md", "lecture.md", "dropped.md", "research.md", "audit.md",
               "ledger.json", "transcript.md", "raw_text.md", "ingest.json",
               "scenes.json", "render.log", "run.log", "lecture.mp4", "lecture.srt",
               "chapters.json"]
MODELS = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "default": None}
EDITABLE = {"ratio": RATIOS, "output": OUTPUTS, "output_lang": ["en", "de"],
            "research_scope": ["trusted", "open", "both", "none"],
            "tts": ["say", "elevenlabs"], "video": [True, False],
            "theme": ["mix", "moss", "indigo", "amber", "dark", "linen", "chalk", "cloud", "light", "paper"],
            "model": list(MODELS)}

# ---- .env + narration settings ---------------------------------------------
_VOICES_CACHE: dict = {}


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")


def save_env(updates: dict):
    """Rewrite .env keeping unrelated lines; empty value removes the key."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    out = [ln for ln in lines if not any(ln.split("=", 1)[0].strip() == k for k in updates)]
    for k, v in updates.items():
        if v:
            out.append(f"{k}={v}")
        os.environ.pop(k, None)
    if any("\n" in str(v) or "\r" in str(v) for v in updates.values()):
        raise ValueError("value must be a single line")
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + ("\n" if out else ""))
    os.chmod(ENV_FILE, 0o600)
    _VOICES_CACHE.clear()
    load_env()


def elevenlabs_voices():
    """List the account's voices (cached per key); [] without a key, error string on failure."""
    import urllib.request
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return [], None
    import time
    if key in _VOICES_CACHE:
        return _VOICES_CACHE[key], None
    fail = _VOICES_CACHE.get(("fail", key))
    if fail and time.time() - fail[0] < 60:
        return [], fail[1]
    try:
        req = urllib.request.Request("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        voices = [{"voice_id": v["voice_id"], "name": v.get("name"),
                   "labels": " · ".join(str(x) for x in (v.get("labels") or {}).values()),
                   "category": v.get("category")} for v in data.get("voices", [])]
        voices.sort(key=lambda v: (v["category"] != "premade", v["name"] or ""))
        _VOICES_CACHE[key] = voices
        return voices, None
    except Exception as e:  # noqa: BLE001
        _VOICES_CACHE[("fail", key)] = (time.time(), f"ElevenLabs: {e}")
        return [], f"ElevenLabs: {e}"


def settings_payload():
    voices, err = elevenlabs_voices()
    return {"elevenlabs": {"has_key": bool(os.environ.get("ELEVENLABS_API_KEY")),
                           "voice_id": os.environ.get("ELEVENLABS_VOICE_ID") or "JBFqnCBsd6RMkjVDRZzb",
                           "voices": voices, "error": err}}


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
TEXT_EXT = {".md", ".txt", ".tex", ".csv"}


def to_pdf(name: str, data: bytes):
    """Return (pdf_bytes, kind): PDFs pass through, images become one page each, text is typeset."""
    ext = Path(name).suffix.lower()
    if ext == ".pdf" or data[:5] == b"%PDF-":
        return data, "pdf"
    try:
        import fitz
    except ImportError:
        raise ValueError("only PDF files are supported without pymupdf")
    doc = fitz.open()
    if ext in IMAGE_EXT:
        img = fitz.open(stream=data, filetype=ext.lstrip("."))
        rect = img[0].rect
        page = doc.new_page(width=rect.width, height=rect.height)
        page.insert_image(rect, stream=data)
        out = doc.tobytes()
        return out, "image"
    if ext in TEXT_EXT:
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines() or [""]
        per_page = 58
        for i in range(0, len(lines), per_page):
            page = doc.new_page(width=595, height=842)          # A4
            page.insert_textbox(fitz.Rect(48, 48, 547, 800), "\n".join(lines[i:i + per_page]),
                                fontsize=9.5, fontname="cour")
        return doc.tobytes(), "text"
    raise ValueError(f"unsupported file type {ext or '(none)'} — use PDF, images or text")


def guess_title(pdf_bytes: bytes) -> str:
    """Largest-type lines on the first pages: '<section heading> — <document title>'."""
    try:
        import fitz  # pymupdf
    except ImportError:
        return ""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001
        return ""
    lines = []  # (size, page, text) — consecutive lines of one size in a block are one heading
    for pno in range(min(3, len(doc))):
        for block in doc[pno].get_text("dict")["blocks"]:
            cur, cur_size = "", None
            for ln in block.get("lines", []):
                txt = "".join(sp["text"] for sp in ln["spans"]).strip()
                size = round(max((sp["size"] for sp in ln["spans"]), default=0), 1)
                if not txt:
                    continue
                if size == cur_size:
                    cur += " " + txt
                else:
                    if cur:
                        lines.append((cur_size, pno, cur))
                    cur, cur_size = txt, size
            if cur:
                lines.append((cur_size, pno, cur))
    # all-caps title lines split over several blocks ("INTRODUCTION TO" / "COMPUTER SCIENCE") → one line
    merged = []
    caps = re.compile(r"^[A-Z0-9 ,.:'&()-]+$")
    for s, p, t in lines:
        if merged and merged[-1][0] == s and merged[-1][1] == p and caps.match(t) and caps.match(merged[-1][2]) \
                and any(c.isalpha() for c in t) and len(merged[-1][2]) + len(t) < 90:
            merged[-1] = (s, p, merged[-1][2] + " " + t)
        else:
            merged.append((s, p, t))
    lines = [(s, p, t) for s, p, t in merged
             if 3 <= len(t) <= 90 and not re.fullmatch(r"[\d\s.:/–-]+", t)]
    if not lines:
        return ""
    lines.sort(key=lambda x: (-x[0], x[1]))
    top = lines[0][0]
    picks, seen_pages = [], set()
    for size, pno, txt in lines:
        if size < top * 0.75 or pno in seen_pages:
            continue
        picks.append((pno, txt))
        seen_pages.add(pno)
        if len(picks) == 2:
            break
    picks.sort(key=lambda x: -x[0])          # section heading (later page) first, document title after
    def nice(t):
        if not t.isupper():
            return t
        small = {"to", "of", "and", "in", "on", "for", "the", "a", "an", "und", "der", "die", "das", "zu"}
        words = [w.capitalize() if (i == 0 or w.lower() not in small) else w.lower()
                 for i, w in enumerate(t.split())]
        return " ".join(words)
    return " — ".join(nice(t) for _, t in picks)


def import_zip(name: str, path: Path, user=None):
    """Unpack a lecture zip (as made by the Zip button: <course>/<chapter>/…) into jobs/ — in hosted
    mode into the person's workspace. Refuses anything that would land outside it and does not
    overwrite an existing lecture."""
    import zipfile
    base = workspace(user)
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError("empty file")
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError(f"{name} is not a zip file") from None
    names = [n for n in z.namelist() if not n.endswith("/")]
    jobs = sorted({"/".join(n.split("/")[:2]) for n in names if n.count("/") >= 2 and n.split("/")[2] == "job.json"})
    if not jobs:
        raise ValueError("no lecture in this zip (expected <course>/<name>/job.json)")
    done = []
    for rel in jobs:
        course, chapter = rel.split("/")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", course) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", chapter):
            raise ValueError(f"unexpected folder name in zip: {rel}")
        d = base / course / chapter
        if d.exists():
            raise ValueError(f"{rel} already exists here — delete it first or rename the folder in the zip")
        for n in names:
            if not n.startswith(rel + "/"):
                continue
            target = (d / n[len(rel) + 1:])
            if ".." in Path(n).parts or not str(target.resolve()).startswith(str(d.resolve())):
                raise ValueError("zip contains an unsafe path")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        for stale in ("run.pid", "run.jsonl"):
            (d / stale).unlink(missing_ok=True)
        done.append(rel)
    return {"imported": done, "dir": done[0]}


# ---- version and updates -------------------------------------------------------
def _git(*args, timeout=30):
    import subprocess
    r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def version_info(check: bool = False):
    """Version of this copy and, for an installed copy (a git clone with an 'origin'), whether the
    person who shares the tool has published a newer one. The developer's own folder has
    scripts/publish.sh as its way out instead."""
    try:
        version = (ROOT / "VERSION").read_text().strip()
    except OSError:
        version = "?"
    info = {"version": version, "installed": False, "hosted": HOSTED,
            "developer": not HOSTED and (ROOT / "scripts" / "publish.sh").exists()
            and (Path(os.environ.get("LT_DIST") or Path.home() / "Documents/UNI/lecture-tool-dist") / ".git").exists()}
    if HOSTED:                 # updates come through a new deploy, never through the panel
        return info
    code, url, _ = _git("remote", "get-url", "origin", timeout=10)
    if code != 0 or not (ROOT / ".git").exists():
        return info
    info["installed"], info["remote"] = True, url
    if check:
        code, _, err = _git("fetch", "-q", "origin", "main", timeout=60)
        if code != 0:
            info["error"] = f"could not check: {err[:200]}"
            return info
        _, behind, _ = _git("rev-list", "--count", "HEAD..origin/main", timeout=10)
        _, latest, _ = _git("show", "origin/main:VERSION", timeout=10)
        info["behind"] = int(behind or 0)
        info["latest"] = latest.strip() or version
        info["update_available"] = info["behind"] > 0
    return info


def apply_update():
    """Pull the published version, refresh the Python packages, restart the service."""
    import subprocess
    code, out, err = _git("pull", "--ff-only", "origin", "main", timeout=120)
    if code != 0:
        raise ValueError(f"update failed: {err or out}")
    py = ROOT / ".venv" / "bin" / "python"
    if py.exists():
        subprocess.run([str(py), "-m", "pip", "install", "--quiet", "pymupdf", "pillow", "fonttools", "brotli"],
                       capture_output=True, timeout=600)
    version = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "?"
    # restart a second later, after this response went out; under launchd the service comes straight back
    if "--service" in sys.argv:
        subprocess.Popen(["/bin/sh", "-c", f"sleep 1; launchctl kickstart -k gui/{os.getuid()}/com.sprtk.lecture-tool"],
                         start_new_session=True)
    else:
        subprocess.Popen(["/bin/sh", "-c", f"sleep 1; kill {os.getpid()}"], start_new_session=True)
    return {"updated": True, "version": version, "restarting": True}


def publish_update(message: str):
    """Developer side: run scripts/publish.sh and return its output."""
    import subprocess
    r = subprocess.run(["/bin/bash", str(ROOT / "scripts" / "publish.sh"), message or "update"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    out = (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
    if r.returncode != 0:
        raise ValueError(out[-800:] or "publish failed")
    return {"ok": True, "output": out[-1500:], "version": (ROOT / "VERSION").read_text().strip()}


class ApiError(ValueError):
    """A refusal with its HTTP status; the message is shown to the person as it is."""
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def write_json_atomic(path: Path, obj, mode: int = 0o644):
    """Write JSON through a temp file and a rename, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ---- accounts and sessions (hosted mode, docs/HOSTED.md §3–4) ------------------
ACCOUNTS_LOCK = threading.RLock()     # one lock for users.json and invites.json
SESSION_DAYS = 30
SIGNIN_FAILS: dict = {}               # e-mail -> unix times of failed sign-ins (15-minute window)
SIGNIN_LOCK = threading.Lock()


def load_users() -> list:
    return read_json(USERS_FILE, {}).get("users", []) or []


def load_invites() -> list:
    return read_json(INVITES_FILE, {}).get("invites", []) or []


def save_users(users: list):
    write_json_atomic(USERS_FILE, {"users": users}, 0o600)


def save_invites(invites: list):
    write_json_atomic(INVITES_FILE, {"invites": invites}, 0o600)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        kind, salt, h = (stored or "").split("$")
        if kind != "scrypt":
            return False
        calc = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc.hex(), h)


def public_user(u: dict) -> dict:
    return {"id": u["id"], "email": u["email"], "name": u.get("name") or "", "admin": bool(u.get("admin"))}


def user_budget(u: dict) -> float:
    try:
        return float(u["budget_usd"]) if u.get("budget_usd") is not None else USER_BUDGET_USD
    except (TypeError, ValueError):
        return USER_BUDGET_USD


def find_user(users: list, *, uid: str = None, email: str = None):
    for u in users:
        if (uid and u.get("id") == uid) or (email and u.get("email") == email):
            return u
    return None


def valid_email(s: str) -> str:
    e = (s or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e) or len(e) > 254:
        raise ApiError("enter a valid e-mail address")
    return e


def check_new_password(pw) -> str:
    if not isinstance(pw, str) or len(pw) < 8:
        raise ApiError("the password must be at least 8 characters")
    if len(pw) > 1024:
        raise ApiError("that password is too long")
    return pw


def sign_up(email: str, password: str, name: str, invite: str) -> dict:
    """Create the account. The owner (LT_ADMIN_EMAIL) needs no invite for their first sign-up and
    becomes admin; everyone else burns one invite code."""
    email = valid_email(email)
    password = check_new_password(password)
    name = (name or "").strip()[:80] or email.split("@")[0]
    invite = (invite or "").strip()
    with ACCOUNTS_LOCK:
        users, invites = load_users(), load_invites()
        if find_user(users, email=email):
            raise ApiError("an account with this e-mail already exists — sign in instead", 409)
        is_owner = email == ADMIN_EMAIL
        used = None
        if not is_owner:
            if not invite:
                raise ApiError("an invite code is needed to sign up — ask the person who runs this tool")
            used = next((i for i in invites if i.get("code") == invite and not i.get("used_by")), None)
            if not used:
                raise ApiError("this invite code is not valid or was already used")
        uid = "u_" + secrets.token_hex(4)
        while find_user(users, uid=uid):
            uid = "u_" + secrets.token_hex(4)
        user = {"id": uid, "email": email, "name": name, "pw": hash_password(password), "admin": is_owner,
                "budget_usd": USER_BUDGET_USD, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "disabled": False}
        if used:
            used["used_by"] = uid
            save_invites(invites)
        users.append(user)
        save_users(users)
    (JOBS / uid).mkdir(parents=True, exist_ok=True)
    return user


def sign_in(email: str, password: str) -> dict:
    """Check the password; after 10 failures for one e-mail in 15 minutes, refuse for a while."""
    email = (email or "").strip().lower()
    now = time.time()
    with SIGNIN_LOCK:
        fails = [t for t in SIGNIN_FAILS.get(email, []) if now - t < 900]
        SIGNIN_FAILS[email] = fails
        if len(fails) >= 10:
            raise ApiError("too many attempts — try again later", 429)
    user = find_user(load_users(), email=email)
    if not user or not check_password(password or "", user.get("pw", "")):
        with SIGNIN_LOCK:
            SIGNIN_FAILS.setdefault(email, []).append(now)
        raise ApiError("wrong e-mail or password", 401)
    if user.get("disabled"):
        raise ApiError("this account is disabled — ask the admin", 403)
    with SIGNIN_LOCK:
        SIGNIN_FAILS.pop(email, None)
    return user


def change_password(uid: str, old: str, new: str):
    new = check_new_password(new)
    with ACCOUNTS_LOCK:
        users = load_users()
        user = find_user(users, uid=uid)
        if not user or not check_password(old or "", user.get("pw", "")):
            raise ApiError("the current password is wrong")
        user["pw"] = hash_password(new)
        save_users(users)


def session_cookie(uid: str) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    return f"{uid}.{exp}.{_session_sig(uid, exp)}"


def _session_sig(uid: str, exp: int) -> str:
    return hmac.new(SECRET.encode("utf-8"), f"{uid}.{exp}".encode("utf-8"), "sha256").hexdigest()


def parse_session(value: str):
    """(user_id, expiry) for a valid, unexpired cookie value; None otherwise."""
    try:
        uid, exp_s, sig = (value or "").split(".")
        exp = int(exp_s)
    except ValueError:
        return None
    if exp <= time.time() or not hmac.compare_digest(sig, _session_sig(uid, exp)):
        return None
    return uid, exp


# ---- workspaces: where a person's jobs live ----------------------------------------
def workspace(user) -> Path:
    """jobs/ locally, jobs/<user_id>/ in hosted mode."""
    if not HOSTED:
        return JOBS
    if not user:
        raise ApiError("sign in", 401)
    return JOBS / user["id"]


def job_key(user, rel: str) -> str:
    """The job's path relative to jobs/ — the RUNS key and what the skill is told
    (`/lecture jobs/<key>`): course/chapter locally, <user_id>/course/chapter in hosted mode."""
    return f"{user['id']}/{rel}" if HOSTED else rel


# ---- metering (docs/HOSTED.md §6): cost.json per job, usage per person per month ----
COST_LOCK = threading.Lock()
EL_CHARS_RE = re.compile(r"ElevenLabs \([^)]*\): (\d+) characters still to synthesize")


def render_chars(d: Path, started: str) -> int:
    """Narration characters this run sent to ElevenLabs. render.py starts render.log afresh for
    every render and writes one line "ElevenLabs (…): N characters still to synthesize", so the
    file describes the latest render only — it counts for this run if it was written during it."""
    try:
        st = (d / "render.log").stat()
        since = datetime.fromisoformat(started).timestamp()
    except (OSError, ValueError, TypeError):
        return 0
    if st.st_mtime < since - 5:
        return 0
    text = (d / "render.log").read_text(encoding="utf-8", errors="replace")
    return sum(int(m.group(1)) for m in EL_CHARS_RE.finditer(text))


def record_run_cost(d: Path, rec: dict, ev: dict = None):
    """Append one run to <job>/cost.json: {"runs": [{started, from, claude_usd, duration_s, model,
    elevenlabs_chars}], "claude_usd": total, "elevenlabs_chars": total}. `ev` is claude's result
    event; a render-only run has none and costs no Claude money."""
    ev = ev or {}
    try:
        duration = int(ev["duration_ms"] / 1000) if "duration_ms" in ev else \
            int(time.time() - datetime.fromisoformat(rec["started"]).timestamp())
    except (TypeError, ValueError):
        duration = 0
    model = ", ".join((ev.get("modelUsage") or {}).keys()) or rec.get("model") or "cli default"
    entry = {"started": rec["started"], "from": rec["from"],
             "claude_usd": round(float(ev.get("total_cost_usd") or 0), 6),
             "duration_s": duration, "model": model,
             "elevenlabs_chars": render_chars(d, rec["started"])}
    with COST_LOCK:
        data = read_json(d / "cost.json", {})
        runs = data.get("runs") if isinstance(data.get("runs"), list) else []
        runs.append(entry)
        write_json_atomic(d / "cost.json", {
            "runs": runs,
            "claude_usd": round(sum(float(r.get("claude_usd") or 0) for r in runs), 6),
            "elevenlabs_chars": sum(int(r.get("elevenlabs_chars") or 0) for r in runs)})


def month_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def scan_usage(uid: str) -> dict:
    """{month: {claude_usd, elevenlabs_chars, jobs}} over every cost.json in the workspace; a job
    counts for a month when one of its runs started in it. The job folders are the truth."""
    months: dict = {}
    for cf in (JOBS / uid).glob("*/*/cost.json"):
        seen = set()
        for r in read_json(cf, {}).get("runs", []) or []:
            m = str(r.get("started") or "")[:7]
            if not re.fullmatch(r"\d{4}-\d{2}", m):
                continue
            tot = months.setdefault(m, {"claude_usd": 0.0, "elevenlabs_chars": 0, "jobs": 0})
            tot["claude_usd"] = round(tot["claude_usd"] + float(r.get("claude_usd") or 0), 4)
            tot["elevenlabs_chars"] += int(r.get("elevenlabs_chars") or 0)
            if m not in seen:
                seen.add(m)
                tot["jobs"] += 1
    return months


def usage_for(uid: str, month: str = None) -> dict:
    """One person's usage in a month. Cached in LT_DATA/usage/<uid>/<month>.json and rebuilt
    whenever a cost.json is newer than the cache or the number of jobs changed."""
    month = month or month_now()
    costs = list((JOBS / uid).glob("*/*/cost.json"))
    newest = max((c.stat().st_mtime for c in costs), default=0)
    cache = USAGE_DIR / uid / f"{month}.json"
    try:
        cached = read_json(cache, None) if cache.stat().st_mtime > newest else None
    except OSError:
        cached = None
    if cached and cached.get("n") == len(costs):
        return {k: v for k, v in cached.items() if k != "n"}
    out = {"month": month, **scan_usage(uid).get(month, {"claude_usd": 0.0, "elevenlabs_chars": 0, "jobs": 0})}
    try:
        write_json_atomic(cache, {**out, "n": len(costs)})
    except OSError:
        pass
    return out


def live_runs() -> int:
    return sum(1 for r in RUNS.values() if r["proc"].poll() is None)


# ---- pipeline runs (claude -p in a subprocess) ------------------------------
RUNS: dict = {}          # job key (path under jobs/) -> {"proc": Popen, "started": iso, "exit": int|None, "from": str}
RUNS_LOCK = threading.Lock()


def run_info(rel: str):
    r = RUNS.get(rel)
    if not r:
        return {"running": False}
    alive = r["proc"].poll() is None
    code = None if r.get("attached") else r.get("exit", r["proc"].returncode)
    return {"running": alive, "started": r["started"], "exit": None if alive else code,
            "from": r["from"]}


def start_run(rel: str, frm: str, d: Path = None):
    """Spawn `claude -p "/lecture jobs/<rel> ..."` and stream its output to run.log. `rel` is the
    job's path under jobs/ (in hosted mode <user_id>/course/chapter — the skill runs with the repo
    as its working directory and finds the folder through jobs/)."""
    import shutil as _sh
    import subprocess
    exe = _sh.which("claude")
    if not exe and frm != "render":
        raise ValueError("claude CLI not found on PATH — install Claude Code first")
    d = d or JOBS / rel
    with RUNS_LOCK:
        if rel in RUNS and RUNS[rel]["proc"].poll() is None:
            raise ValueError("already running")
        if HOSTED:
            # one machine for everyone: the limit counts every person's live runs
            n = live_runs()
            if n >= MAX_PARALLEL_RUNS:
                raise ApiError(f"{n} lectures are being made right now — try again in a few minutes", 429)
        prompt = f"/lecture jobs/{rel}"
        if frm == "lecture":
            prompt += " — rerun from stage 5"
        elif frm == "audit":
            prompt += " — rerun from stage 6"
        elif frm == "scenes":
            prompt += " — rerun from stage 7"
        if frm == "render" and not (d / "scenes.json").exists():
            raise ValueError("no scenes.json yet — run 'video only' first")
        log = open(d / "run.log", "a", encoding="utf-8")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log.write(f"\n=== {now}  {prompt}\n")
        log.flush()
        load_env()
        keep = ("PATH", "HOME", "USER", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
                "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE")
        env = {k: v for k, v in os.environ.items()
               if k in keep or k.startswith(("ELEVENLABS_", "CLAUDE_CONFIG", "ANTHROPIC_", "XDG_"))}
        model = MODELS.get(read_json(d / "job.json", {}).get("model", "sonnet"))
        if frm == "render":          # slides + narration only: no Claude involved
            cmd = [sys.executable, str(ROOT / "scripts" / "render.py"), str(d)]
            log.write("render.py only (no claude)\n")
        else:
            cmd = [exe, "-p", prompt, "--permission-mode", "bypassPermissions",
                   "--output-format", "stream-json", "--verbose"] + (["--model", model] if model else [])
            log.write(f"model: {model or 'cli default'}\n")
        log.flush()
        # The child gets its own session and writes raw events to run.jsonl, so a restart of this
        # server never kills or detaches a running job; the tailer below (re)attaches at any time.
        raw = open(d / "run.jsonl", "a", encoding="utf-8")
        raw.seek(0, os.SEEK_END)
        start_pos = raw.tell()                                     # the tailer reads only this run's events
        proc = subprocess.Popen(cmd,
            cwd=str(ROOT), stdout=raw, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        raw.close()
        (d / "run.pid").write_text(f"{proc.pid}\n{now}\n{frm}\n")
        rec = RUNS[rel] = {"proc": proc, "started": now, "from": frm, "model": model or "cli default"}
    log.close()
    threading.Thread(target=tail_run, args=(rel, d, rec, start_pos), daemon=True).start()


def tail_run(rel: str, d: Path, rec: dict, pos: int):
    """Follow run.jsonl from `pos` and turn claude's stream-json events into readable lines in
    run.log; stops when the process is gone. `rec` is this run's RUNS entry — a newer run for the
    same job gets its own tailer, and this one then only finishes its own bookkeeping."""
    import time
    jl, out = d / "run.jsonl", d / "run.log"
    proc, pid = rec["proc"], rec["proc"].pid
    with open(out, "a", encoding="utf-8") as log:
        while True:
            try:
                with open(jl, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except OSError:
                chunk = ""
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    log.write(line + "\n")
                    continue
                t = ev.get("type")
                if t == "assistant":
                    for part in ev.get("message", {}).get("content", []):
                        if part.get("type") == "text" and part["text"].strip():
                            log.write(part["text"].strip()[:400] + "\n")
                        elif part.get("type") == "tool_use":
                            inp = part.get("input", {})
                            what = (inp.get("description") or inp.get("file_path") or inp.get("command")
                                    or inp.get("query") or inp.get("pattern") or inp.get("prompt") or "")
                            log.write(f"▸ {part.get('name')}  {str(what)[:160]}\n")
                elif t == "result":
                    secs = ev.get("duration_ms", 0) / 1000
                    cost = ev.get("total_cost_usd")
                    log.write(f"=== result after {secs / 60:.1f} min"
                              f"{f', ${cost:.2f}' if cost is not None else ''}\n")
                    if ev.get("result"):
                        log.write(str(ev["result"]).strip()[:2000] + "\n")
                    if HOSTED and not rec.get("metered"):   # the run's bill, held against the month's budget
                        rec["metered"] = True
                        try:
                            record_run_cost(d, rec, ev)
                        except (OSError, ValueError) as e:
                            log.write(f"(could not write cost.json: {e})\n")
            log.flush()
            code = proc.poll()                      # reaps our own child; a detached one is probed by pid
            if code is not None:
                log.write(f"=== exit {code if not rec.get('attached') else '?'}\n")
                log.flush()
                rec["exit"] = None if rec.get("attached") else code
                if HOSTED and rec["from"] == "render" and not rec.get("metered"):
                    rec["metered"] = True           # no Claude in a render-only run: narration characters only
                    try:
                        record_run_cost(d, rec)
                    except (OSError, ValueError) as e:
                        log.write(f"(could not write cost.json: {e})\n")
                try:                                # only remove run.pid if it is still ours
                    if int((d / "run.pid").read_text().split()[0]) == pid:
                        (d / "run.pid").unlink(missing_ok=True)
                except (OSError, ValueError, IndexError):
                    pass
                return
            time.sleep(1.0)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def pid_is_ours(pid: int) -> bool:
    """True if the pid runs one of our pipelines (claude or render.py) — a reused pid is not."""
    import subprocess
    try:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return ("/lecture jobs/" in cmd) or ("render.py" in cmd)


def kill_run(pid: int):
    """SIGTERM the run's whole process group (claude and everything it spawned, or render.py and
    its ffmpeg/say children); SIGKILL whatever is left after a few seconds."""
    import signal
    import time
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    for _ in range(30):
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class _Detached:
    """Stand-in for a Popen of a process another server instance started."""
    def __init__(self, pid: int):
        self.pid, self.returncode = pid, None

    def poll(self):
        return None if pid_alive(self.pid) else 0   # exit code unknown; run_info reports None

    def terminate(self):
        kill_run(self.pid)


def reattach_runs():
    """On start: find jobs with a live run.pid from a previous server and keep following them."""
    for pf in JOBS.glob("*/*/*/run.pid" if HOSTED else "*/*/run.pid"):
        try:
            pid_s, started, frm = (pf.read_text().splitlines() + ["", "", ""])[:3]
            pid = int(pid_s)
        except (OSError, ValueError):
            continue
        rel = str(pf.parent.relative_to(JOBS))
        if pid_alive(pid) and pid_is_ours(pid):
            rec = RUNS[rel] = {"proc": _Detached(pid), "started": started, "from": frm or "all", "attached": True}
            try:
                pos = (pf.parent / "run.jsonl").stat().st_size
            except OSError:
                pos = 0
            threading.Thread(target=tail_run, args=(rel, pf.parent, rec, pos), daemon=True).start()
            print(f"re-attached running job {rel} (pid {pid})")
        else:
            pf.unlink(missing_ok=True)


def stop_run(rel: str):
    r = RUNS.get(rel)
    if not r or r["proc"].poll() is not None:
        raise ValueError("not running")
    kill_run(r["proc"].pid)


def tail(path: Path, n=60):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return ""


def slug(s: str) -> str:
    s = SLUG_RE.sub("-", s.strip().lower()).strip("-")
    return s or "untitled"


def safe_job_dir(rel: str, user=None) -> Path:
    """Resolve a jobs-relative path and refuse anything outside jobs/ — in hosted mode outside the
    person's own workspace, so one person can never reach another's lectures."""
    base = workspace(user).resolve()
    p = (base / rel).resolve()
    if base not in p.parents and p != base:
        raise ValueError("path outside jobs/")
    return p


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def list_jobs(user=None):
    out = []
    base = workspace(user)
    for jf in sorted(base.glob("*/*/job.json")):
        d = jf.parent
        job = read_json(jf, {})
        status = read_json(d / "status.json", {"stage": "created"})
        rel = str(d.relative_to(base))
        out.append({
            "dir": rel,
            "title": job.get("title") or d.name,
            "course": job.get("course", d.parent.name),
            "chapter": job.get("chapter", d.name),
            "ratio": job.get("ratio"),
            "output": job.get("output", "lecture"),
            "output_lang": job.get("output_lang"),
            "created": job.get("created"),
            "stage": status.get("stage"),
            "message": status.get("message", ""),
            "updated": status.get("updated", ""),
            "has_video": (d / "lecture.mp4").exists(),
            "running": run_info(job_key(user, rel))["running"],
        })
    out.sort(key=lambda j: j.get("updated") or j.get("created") or "",
             reverse=True)
    return out


def _safe(fn):
    def wrapped(self):
        try:
            return fn(self)
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:  # noqa: BLE001 — a bug must not take the connection down silently
            import traceback
            traceback.print_exc()
            try:
                return self.send_json({"error": f"server error: {e}"}, 500)
            except Exception:  # noqa: BLE001
                return None
    return wrapped


PUBLIC_API = ("/api/me", "/api/auth/signup", "/api/auth/signin")   # reachable before signing in


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        self.user = None            # the signed-in person (hosted mode); None locally
        self._cookies: list = []    # Set-Cookie headers for the response being built
        super().__init__(*a, directory=str(UI), **kw)

    def log_message(self, fmt, *args):  # quieter console
        if args and "/api/" in str(args[0]):
            return
        super().log_message(fmt, *args)

    # ---- helpers -------------------------------------------------------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for c in self._cookies:
            self.send_header("Set-Cookie", c)
        self._cookies = []
        self.end_headers()
        self.wfile.write(body)

    # ---- sign-in state (hosted mode) --------------------------------------
    def begin(self, path: str):
        """Start of every request: who is calling, and may they call this route? Returns False
        after answering 401 — in hosted mode /api/* needs a session except the sign-in routes."""
        self._cookies = []
        self.user = self.current_user() if HOSTED else None
        if HOSTED and path.startswith("/api/") and not self.user and path not in PUBLIC_API:
            self.send_json({"error": "sign in"}, 401)
            return False
        return True

    def current_user(self):
        try:
            jar = SimpleCookie(self.headers.get("Cookie") or "")
            raw = jar["lt_session"].value if "lt_session" in jar else ""
        except Exception:  # noqa: BLE001 — a malformed cookie is just "not signed in"
            raw = ""
        parsed = parse_session(raw)
        if not parsed:
            return None
        uid, exp = parsed
        user = find_user(load_users(), uid=uid)
        if not user or user.get("disabled"):
            return None
        if exp - time.time() < (SESSION_DAYS - 1) * 86400:      # renew once a day while in use
            self.set_session(uid)
        return user

    def set_session(self, uid: str):
        attrs = f"Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax" + ("; Secure" if HOSTED else "")
        self._cookies = [c for c in self._cookies if not c.startswith("lt_session=")]
        self._cookies.append(f"lt_session={session_cookie(uid)}; {attrs}")

    def clear_session(self):
        self._cookies = [c for c in self._cookies if not c.startswith("lt_session=")]
        self._cookies.append("lt_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" + ("; Secure" if HOSTED else ""))

    def is_admin(self) -> bool:
        return bool(self.user and self.user.get("admin"))

    def me_payload(self) -> dict:
        if not HOSTED:
            return {"hosted": False}
        out = {"hosted": True, "signed_in": bool(self.user), "user": None, "usage": None,
               "limits": {"max_parallel_runs": MAX_PARALLEL_RUNS}}
        if self.user:
            out["user"] = public_user(self.user)
            out["usage"] = {**usage_for(self.user["id"]), "budget_usd": user_budget(self.user)}
        return out

    def admin_route(self, method: str, path: str, q: dict):
        """/api/admin/*: people, invites and usage — the admin's view (docs/HOSTED.md §5)."""
        if not self.is_admin():
            return self.send_json({"error": "admins only"}, 403)
        if path == "/api/admin/users" and method == "GET":
            users = [{**public_user(u), "budget_usd": user_budget(u), "disabled": bool(u.get("disabled")),
                      "created": u.get("created"), "usage": usage_for(u["id"])} for u in load_users()]
            return self.send_json({"users": users, "month": month_now(), "default_budget_usd": USER_BUDGET_USD})
        if path == "/api/admin/users" and method == "POST":
            b = self.body_json()
            with ACCOUNTS_LOCK:
                users = load_users()
                u = find_user(users, uid=str(b.get("id") or ""))
                if not u:
                    return self.send_json({"error": "no such person"}, 404)
                if "budget_usd" in b:
                    try:
                        u["budget_usd"] = max(0.0, float(b["budget_usd"]))
                    except (TypeError, ValueError):
                        return self.send_json({"error": "the budget must be a number of dollars"}, 400)
                if "disabled" in b:
                    if u["id"] == self.user["id"] and b["disabled"]:
                        return self.send_json({"error": "you cannot disable your own account"}, 400)
                    u["disabled"] = bool(b["disabled"])
                if "admin" in b:
                    if u["id"] == self.user["id"] and not b["admin"]:
                        return self.send_json({"error": "you cannot take away your own admin rights"}, 400)
                    u["admin"] = bool(b["admin"])
                if "name" in b and str(b["name"]).strip():
                    u["name"] = str(b["name"]).strip()[:80]
                save_users(users)
            return self.send_json({**public_user(u), "budget_usd": user_budget(u), "disabled": bool(u.get("disabled")),
                                   "created": u.get("created"), "usage": usage_for(u["id"])})
        if path == "/api/admin/invites":
            with ACCOUNTS_LOCK:
                invites = load_invites()
                if method == "GET":
                    return self.send_json({"invites": invites})
                if method == "POST":
                    b = self.body_json()
                    inv = {"code": secrets.token_urlsafe(12), "created_by": self.user["id"],
                           "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "used_by": None, "note": str(b.get("note") or "").strip()[:200]}
                    invites.append(inv)
                    save_invites(invites)
                    return self.send_json(inv, 201)
                if method == "DELETE":
                    code = q.get("code") or ""
                    keep = [i for i in invites if i.get("code") != code]
                    if len(keep) == len(invites):
                        return self.send_json({"error": "no such invite"}, 404)
                    save_invites(keep)
                    return self.send_json({"deleted": code})
        if path == "/api/admin/usage" and method == "GET":
            rows = [{**public_user(u), "budget_usd": user_budget(u), "months": scan_usage(u["id"])} for u in load_users()]
            return self.send_json({"month": month_now(), "users": rows})
        return self.send_json({"error": "unknown route"}, 404)

    def send_file(self, path: Path):
        """Serve a job file; supports HTTP Range so <video> can seek."""
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in (".md", ".log", ".txt", ".srt"):
            ctype = "text/plain; charset=utf-8"
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        partial = bool(m) and size > 0 and bool(m.group(1) or m.group(2))
        if partial:
            a, b = m.group(1), m.group(2)
            if a:
                start, end = int(a), (int(b) if b else size - 1)
            else:                       # suffix range: last N bytes
                start = max(size - int(b), 0)
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def send_zip(self, d: Path, rel: str):
        """The whole job as a zip — results, sources and the files a rerun needs; not the render
        scratch, the raw event log or the page images (regenerated by a rerun)."""
        import tempfile
        import zipfile
        skip_dirs = {"render", "pages", "__pycache__"}
        skip_files = {"run.jsonl", "run.pid"}
        # page images that the text embeds or the video's figure scenes use travel along (not all 400)
        wanted = set()
        for name in ("lecture.md", "summary.md"):
            try:
                wanted |= set(re.findall(r"pages/(page-\d+\.png)", (d / name).read_text(encoding="utf-8")))
            except OSError:
                pass
        try:
            for sc in json.loads((d / "scenes.json").read_text(encoding="utf-8")).get("scenes", []):
                if sc.get("type") == "figure" and isinstance(sc.get("page"), int):
                    wanted.add(f"page-{sc['page']:03d}.png")
        except (OSError, ValueError):
            pass
        tmp = tempfile.NamedTemporaryFile(prefix="lecture-", suffix=".zip", delete=False)
        tmp.close()
        try:
            with zipfile.ZipFile(tmp.name, "w") as z:
                for f in sorted(d.rglob("*")):
                    parts = f.relative_to(d).parts
                    if not f.is_file() or f.name in skip_files:
                        continue
                    if any(p in skip_dirs for p in parts) and not (parts[0] == "pages" and f.name in wanted):
                        continue
                    comp = zipfile.ZIP_STORED if f.suffix in (".mp4", ".pdf", ".png", ".m4a", ".mp3") else zipfile.ZIP_DEFLATED
                    z.write(f, arcname=str(Path(rel) / f.relative_to(d)), compress_type=comp)
            size = os.path.getsize(tmp.name)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{rel.replace("/", "-")}.zip"')
            self.end_headers()
            with open(tmp.name, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
        finally:
            os.unlink(tmp.name)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        b = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        if not isinstance(b, dict):
            raise ValueError("body must be a JSON object")
        return b

    def same_origin(self) -> bool:
        """State-changing requests must come from the panel itself, not from another website
        open in the same browser (which could otherwise start runs or change the key)."""
        origin = self.headers.get("Origin")
        if origin:
            host = urlparse(origin).netloc.lower()
            if HOSTED:
                # behind the hosting proxy the browser's Origin is https://<public host>; Host is
                # what the proxy forwarded, so it stands in when LT_PUBLIC_HOST is not set
                allowed = {h for h in (PUBLIC_HOST, (self.headers.get("Host") or "").strip().lower()) if h}
                return host in allowed
            return host in (f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}")
        site = self.headers.get("Sec-Fetch-Site")
        return site in (None, "same-origin", "none")

    # ---- routes --------------------------------------------------------
    @_safe
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self.begin(u.path):
            return None
        try:
            if u.path == "/api/me":
                return self.send_json(self.me_payload())
            if u.path.startswith("/api/admin/"):
                return self.admin_route("GET", u.path, q)
            if u.path == "/api/jobs":
                return self.send_json(list_jobs(self.user))
            if u.path == "/api/settings":
                if HOSTED and not self.is_admin():     # others only learn whether narration works
                    return self.send_json({"elevenlabs": {"has_key": bool(os.environ.get("ELEVENLABS_API_KEY"))}})
                return self.send_json(settings_payload())
            if u.path == "/api/version":
                return self.send_json(version_info(check=q.get("check") == "1"))
            if u.path == "/api/zip":
                d = safe_job_dir(q["dir"], self.user)
                if not (d / "job.json").exists():
                    return self.send_json({"error": "no such job"}, 404)
                return self.send_zip(d, q["dir"])
            if u.path == "/api/job":
                d = safe_job_dir(q["dir"], self.user)
                if not (d / "job.json").exists():
                    return self.send_json({"error": "no such job"}, 404)
                files = [f for f in SHOWN_FILES if (d / f).exists()]
                mtimes = {}
                for f in files:
                    try:
                        mtimes[f] = int((d / f).stat().st_mtime_ns // 1_000_000)
                    except OSError:
                        pass
                return self.send_json({
                    "dir": q["dir"],
                    "job": read_json(d / "job.json", {}),
                    "status": read_json(d / "status.json", {}),
                    "files": files,
                    "mtimes": mtimes,
                    "pages": len(list((d / "pages").glob("*.png")))
                    if (d / "pages").exists() else 0,
                    "run": run_info(job_key(self.user, q["dir"])),
                    "log": tail(d / "run.log"),
                    "render_log": tail(d / "render.log", 12),
                    "chapters": read_json(d / "chapters.json", None),
                    "audit_pass": (lambda p: (p.read_text(encoding="utf-8", errors="replace")[:40]
                                              .startswith("RESULT: PASS")) if p.exists() else None)(d / "audit.md"),
                })
            if u.path == "/api/file":
                d = safe_job_dir(q["dir"], self.user)
                f = (d / q["name"]).resolve()
                if d not in f.parents or not f.is_file():
                    return self.send_json({"error": "not found"}, 404)
                return self.send_file(f)
        except (KeyError, ValueError) as e:
            return self.send_json({"error": str(e)}, getattr(e, "status", 400))
        if u.path in ("/", "/index.html"):
            # always fresh: the panel is edited often and browsers would otherwise keep an old copy
            try:
                return self.send_file(UI / "index.html")
            except OSError as e:
                return self.send_json({"error": str(e)}, 500)
        return super().do_GET()

    @_safe
    def do_POST(self):
        u = urlparse(self.path)
        if not self.same_origin():
            return self.send_json({"error": "cross-site request refused"}, 403)
        if not self.begin(u.path):
            return None
        if HOSTED and u.path in ("/api/update", "/api/publish"):
            return self.send_json({"error": "unknown route"}, 404)       # a new deploy updates the service
        if u.path.startswith("/api/auth/"):
            if not HOSTED:
                return self.send_json({"error": "unknown route"}, 404)
            try:
                b = self.body_json()
                if u.path == "/api/auth/signup":
                    user = sign_up(b.get("email"), b.get("password"), b.get("name"), b.get("invite"))
                    self.user = user
                    self.set_session(user["id"])
                    return self.send_json(self.me_payload(), 201)
                if u.path == "/api/auth/signin":
                    user = sign_in(b.get("email"), b.get("password"))
                    self.user = user
                    self.set_session(user["id"])
                    return self.send_json(self.me_payload())
                if u.path == "/api/auth/signout":
                    self.clear_session()
                    self.user = None
                    return self.send_json(self.me_payload())
                if u.path == "/api/auth/password":
                    change_password(self.user["id"], b.get("old"), b.get("new"))
                    return self.send_json({"ok": True})
                return self.send_json({"error": "unknown route"}, 404)
            except (KeyError, ValueError, OSError) as e:
                return self.send_json({"error": str(e)}, getattr(e, "status", 400))
        if u.path.startswith("/api/admin/"):
            try:
                return self.admin_route("POST", u.path, {})
            except (KeyError, ValueError, OSError) as e:
                return self.send_json({"error": str(e)}, getattr(e, "status", 400))
        if u.path == "/api/import":
            import tempfile
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self.send_json({"error": "empty upload"}, 400)
            name = unquote(self.headers.get("X-File-Name") or "lecture.zip")
            tmp = tempfile.NamedTemporaryFile(prefix="import-", suffix=".zip", delete=False)
            try:
                remaining = n
                while remaining > 0:                      # stream to disk: never hold the whole zip in memory
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    tmp.write(chunk)
                    remaining -= len(chunk)
                tmp.close()
                if remaining > 0:
                    return self.send_json({"error": "upload stopped early — try again"}, 400)
                return self.send_json(import_zip(name, Path(tmp.name), self.user), 201)
            except (ValueError, OSError, KeyError) as e:
                return self.send_json({"error": str(e)}, 400)
            finally:
                tmp.close()
                os.unlink(tmp.name)
        if u.path == "/api/update":
            try:
                return self.send_json(apply_update())
            except (ValueError, OSError) as e:
                return self.send_json({"error": str(e)}, 400)
        if u.path == "/api/publish":
            try:
                b = self.body_json()
                return self.send_json(publish_update(str(b.get("message") or "")))
            except (ValueError, OSError) as e:
                return self.send_json({"error": str(e)}, 400)
        if u.path == "/api/run":
            try:
                b = self.body_json()
                d = safe_job_dir(b["dir"], self.user)
                if not (d / "job.json").exists():
                    return self.send_json({"error": "no such job"}, 404)
                frm = b.get("from", "all")
                if HOSTED and frm != "render":          # a render spends no Claude money
                    usage = usage_for(self.user["id"])
                    if usage["claude_usd"] >= user_budget(self.user):
                        return self.send_json({"error": "your monthly budget is used up — ask the admin for more"}, 402)
                key = job_key(self.user, b["dir"])
                start_run(key, frm, d)
                return self.send_json(run_info(key), 202)
            except (KeyError, ValueError) as e:
                return self.send_json({"error": str(e)}, getattr(e, "status", 400))
        if u.path == "/api/stop":
            try:
                b = self.body_json()
                safe_job_dir(b["dir"], self.user)
                stop_run(job_key(self.user, b["dir"]))
                return self.send_json({"stopped": b["dir"]})
            except (KeyError, ValueError) as e:
                return self.send_json({"error": str(e)}, 400)
        if u.path == "/api/settings":
            if HOSTED and not self.is_admin():
                return self.send_json({"error": "only the admin can change the narration settings"}, 403)
            try:
                b = self.body_json()
                upd = {}
                if "elevenlabs_api_key" in b:
                    upd["ELEVENLABS_API_KEY"] = str(b["elevenlabs_api_key"]).strip()
                if "elevenlabs_voice_id" in b:
                    upd["ELEVENLABS_VOICE_ID"] = str(b["elevenlabs_voice_id"]).strip()
                if upd:
                    save_env(upd)
                return self.send_json(settings_payload())
            except (KeyError, ValueError, OSError) as e:
                return self.send_json({"error": str(e)}, 400)
        if u.path == "/api/job/update":
            try:
                b = self.body_json()
                d = safe_job_dir(b["dir"], self.user)
                jf = d / "job.json"
                if not jf.exists():
                    return self.send_json({"error": "no such job"}, 404)
                job = read_json(jf, {})
                for k, allowed in EDITABLE.items():
                    if k in b:
                        if b[k] not in allowed:
                            return self.send_json({"error": f"{k} must be one of {allowed}"}, 400)
                        if HOSTED and k == "tts" and b[k] == "say":      # no macOS voice on the server
                            return self.send_json({"error": "narration on this server uses ElevenLabs"}, 400)
                        job[k] = b[k]
                if "title" in b and b["title"].strip():
                    job["title"] = b["title"].strip()
                if "notes" in b:
                    job["notes"] = str(b["notes"]).strip()[:4000]
                jf.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")
                return self.send_json(job)
            except (KeyError, ValueError) as e:
                return self.send_json({"error": str(e)}, 400)
        if u.path != "/api/jobs":
            return self.send_json({"error": "unknown route"}, 404)
        try:
            b = self.body_json()
            files = b.get("files") or [{"name": b.get("pdf_name", "source.pdf"), "b64": b["pdf_b64"]}]
            if not files:
                return self.send_json({"error": "no files"}, 400)
            # course and name are automatic unless given: name from the first file, course from the
            # document title on its first pages (or the most recently used course)
            first_name = Path(files[0].get("name") or "lecture").stem
            chapter = slug(b.get("chapter") or first_name)
            course_in = (b.get("course") or "").strip()
            course = slug(course_in) if course_in else ""
            if not course:
                try:
                    guessed = guess_title(base64.b64decode(files[0]["b64"]))
                except Exception:  # noqa: BLE001
                    guessed = ""
                doc_title = guessed.split(" — ")[-1] if " — " in guessed else ""
                recent = list_jobs(self.user)
                course = slug(doc_title) if doc_title and len(doc_title) <= 48 else \
                    (recent[0]["course"] if recent else "lectures")
            base = workspace(self.user)
            d = base / course / chapter
            n = 2
            while (d / "job.json").exists() and not b.get("chapter"):   # automatic name: never collide
                d = base / course / f"{chapter}-{n}"
                n += 1
            chapter = d.name
            ratio = b.get("ratio", "lossless")
            if ratio not in RATIOS:
                return self.send_json({"error": f"ratio must be one of {RATIOS}"}, 400)
            output = b.get("output", "lecture")
            if output not in OUTPUTS:
                return self.send_json({"error": f"output must be one of {OUTPUTS}"}, 400)
            model = b.get("model", "sonnet")
            if model not in MODELS:
                return self.send_json({"error": f"model must be one of {list(MODELS)}"}, 400)
            if (d / "job.json").exists():
                return self.send_json(
                    {"error": f"job {course}/{chapter} already exists — "
                              "pick another chapter name or delete it"}, 409)
            try:
                d.mkdir(parents=True, exist_ok=False)          # the atomic claim: two creates cannot share a folder
            except FileExistsError:
                return self.send_json({"error": f"job {course}/{chapter} already exists — pick another name"}, 409)
            (d / "sources").mkdir(exist_ok=True)
            sources, first_pdf = [], None
            for k, f in enumerate(files, start=1):
                name = Path(f.get("name") or f"file{k}").name
                try:
                    pdf_bytes, kind = to_pdf(name, base64.b64decode(f["b64"]))
                except ValueError as e:
                    shutil.rmtree(d, ignore_errors=True)
                    return self.send_json({"error": f"{name}: {e}"}, 400)
                rel = f"sources/{k:02d}-{slug(Path(name).stem)}.pdf"
                (d / rel).write_bytes(pdf_bytes)
                sources.append({"file": rel, "name": name, "kind": kind})
                first_pdf = first_pdf or pdf_bytes
            pdf_name = sources[0]["file"]
            title = (b.get("title") or "").strip() or guess_title(first_pdf) \
                or Path(sources[0]["name"]).stem.replace("_", " ").replace("-", " ") \
                or f"{course} — {chapter}"
            job = {
                "pdf": pdf_name,
                "original_pdf_name": sources[0]["name"],
                "sources": sources,
                "course": course,
                "chapter": chapter,
                "title": title,
                "title_auto": not (b.get("title") or "").strip(),
                "ratio": ratio,
                "output": output,
                "model": model,
                "notes": (b.get("notes") or "").strip()[:4000],
                "output_lang": b.get("output_lang", "en"),
                "research_scope": b.get("research_scope", "both"),
                "tts": "elevenlabs" if HOSTED else b.get("tts", "say"),
                "theme": b.get("theme") if b.get("theme") in EDITABLE["theme"] else "mix",
                "video": bool(b.get("video", False)),
                "created": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
            }
            (d / "job.json").write_text(json.dumps(job, indent=2,
                                                   ensure_ascii=False),
                                        encoding="utf-8")
            (d / "status.json").write_text(json.dumps({
                "stage": "created",
                "message": "waiting — press Run to make the lecture" if HOSTED else "waiting — run /lecture in Claude Code",
                "updated": job["created"],
                "history": [{"stage": "created", "message": "job created",
                             "at": job["created"]}],
            }, indent=2), encoding="utf-8")
            rel = f"{course}/{chapter}"
            return self.send_json({"dir": rel,
                                   "command": f"/lecture jobs/{job_key(self.user, rel)}"}, 201)
        except (KeyError, ValueError, base64.binascii.Error) as e:
            return self.send_json({"error": f"bad request: {e}"}, 400)

    @_safe
    def do_DELETE(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self.same_origin():
            return self.send_json({"error": "cross-site request refused"}, 403)
        if not self.begin(u.path):
            return None
        if u.path.startswith("/api/admin/"):
            try:
                return self.admin_route("DELETE", u.path, q)
            except (KeyError, ValueError, OSError) as e:
                return self.send_json({"error": str(e)}, getattr(e, "status", 400))
        if u.path != "/api/jobs":
            return self.send_json({"error": "unknown route"}, 404)
        try:
            d = safe_job_dir(q["dir"], self.user)
            if not (d / "job.json").exists():
                return self.send_json({"error": "no such job"}, 404)
            if run_info(job_key(self.user, q["dir"]))["running"]:   # bug: deleting a running job orphaned its pipeline
                return self.send_json({"error": "this lecture is still being made — stop it first"}, 409)
            shutil.rmtree(d)
            return self.send_json({"deleted": q["dir"]})
        except (KeyError, ValueError) as e:
            return self.send_json({"error": str(e)}, 400)


def link_repo_jobs():
    """Hosted mode: the skill addresses a job as jobs/<…> relative to the repo (claude runs with the
    repo as working directory), but the jobs live on the volume. A symlink <repo>/jobs → LT_DATA/jobs
    keeps those relative paths working. A jobs folder baked into the image is moved aside."""
    link = ROOT / "jobs"
    if JOBS == link:
        return
    if link.is_symlink():
        if link.resolve() == JOBS.resolve():
            return
        link.unlink()
    elif link.is_dir():
        if any(link.iterdir()):
            # Never touch somebody's real lectures: hosted mode belongs in the container image,
            # which ships without a jobs/ folder (.dockerignore). On a Mac this means the person
            # started hosted mode inside their own tool folder by mistake.
            raise SystemExit(f"hosted mode refuses to start: {link} is a real folder with lectures in it. "
                             f"Run hosted mode from a copy of the tool (or in the container), not here.")
        link.rmdir()
    elif link.exists():
        link.unlink()
    link.symlink_to(JOBS, target_is_directory=True)


def bind(port: int):
    """Bind the port; if an older serve.py still holds it, replace that one."""
    import subprocess
    import errno
    if HOSTED:                      # one process in a container: reachable from the proxy, no replacing
        return ThreadingHTTPServer(("0.0.0.0", port), Handler)
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
    import shutil as _sh
    lsof = _sh.which("lsof") or "/usr/sbin/lsof"
    out = subprocess.run([lsof, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout.split()
    for pid in out:
        cmd = subprocess.run(["ps", "-o", "command=", "-p", pid],
                             capture_output=True, text=True).stdout
        if "serve.py" not in cmd:
            raise SystemExit(f"port {port} is used by another program (pid {pid}): {cmd.strip()}\n"
                             f"stop it or start with another port:  python3 scripts/serve.py 8770")
        if "--service" not in sys.argv and "--service" in cmd:
            # the login service already serves the panel; a manual start would only fight launchd
            print(f"the panel is already running as a login service → http://localhost:{port}")
            if "--no-browser" not in sys.argv:
                webbrowser.open(f"http://localhost:{port}")
            raise SystemExit(0)
        print(f"replacing previous lecture-tool server (pid {pid})")
        os.kill(int(pid), 15)
    for _ in range(40):
        time.sleep(0.25)
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            continue
    raise SystemExit(f"port {port} did not become free")


def main():
    check_config()
    JOBS.mkdir(parents=True, exist_ok=True)
    if HOSTED:
        USAGE_DIR.mkdir(exist_ok=True)
        link_repo_jobs()
    load_env()
    reattach_runs()
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    port = int(nums[0]) if nums else PORT
    httpd = bind(port)
    url = f"http://localhost:{port}"
    if HOSTED:
        print(f"lecture-tool (hosted)  →  listening on 0.0.0.0:{port}, data in {DATA}", flush=True)
    else:
        print(f"lecture-tool UI  →  {url}   (Ctrl-C to stop)")
    if "--no-browser" not in sys.argv and not HOSTED:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
