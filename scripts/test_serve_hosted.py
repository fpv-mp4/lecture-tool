"""Hosted-mode tests for scripts/serve.py (docs/HOSTED.md).

Run from the repo root with either interpreter that has pytest:
    .venv/bin/python -m pytest scripts/test_serve_hosted.py
    python3 -m pytest scripts/test_serve_hosted.py

The server runs in a thread on a free port with LT_HOSTED=1 and a temporary LT_DATA. `claude`
is a fake script on PATH that prints one stream-json result event (total_cost_usd 1.25) and
exits, so a run goes end to end and produces cost.json. Standard library + pytest only.
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import serve  # noqa: E402

ADMIN = "owner@example.com"
SECRET = "s" * 40
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
FAKE_CLAUDE = """#!/bin/sh
echo "ARGS: $*"
printf '%s\\n' '{"type":"system","subtype":"init"}'
printf '%s\\n' '{"type":"assistant","message":{"content":[{"type":"text","text":"pretend work"}]}}'
printf '%s\\n' '{"type":"result","total_cost_usd":1.25,"duration_ms":1500,"result":"done","modelUsage":{"claude-sonnet-5-5":{"inputTokens":1}}}'
"""


class Client:
    """Tiny HTTP client with a cookie jar (urllib would drop Secure cookies over plain http)."""

    def __init__(self, port):
        self.port, self.cookies = port, {}

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = dict(headers or {})
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        for sc in r.headers.get_all("Set-Cookie") or []:
            name, _, rest = sc.partition("=")
            if "Max-Age=0" in sc:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = rest.split(";")[0]
        conn.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body if body is not None else {})

    def delete(self, path):
        return self.request("DELETE", path)


class State:
    tmp: Path
    port: int
    admin: Client
    bob: Client
    admin_id: str
    bob_id: str
    invite_for_bob: str


S = State()


def hosted_env(tmp: Path) -> dict:
    return {"LT_HOSTED": "1", "LT_DATA": str(tmp), "LT_SECRET": SECRET, "LT_ADMIN_EMAIL": ADMIN,
            "LT_USER_BUDGET_USD": "20", "LT_MAX_PARALLEL_RUNS": "3"}


@pytest.fixture(scope="module", autouse=True)
def server():
    tmp = Path(tempfile.mkdtemp(prefix="lt-hosted-"))
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}:{old_path}"
    serve.configure(hosted_env(tmp))
    serve.check_config()
    serve.JOBS.mkdir(parents=True)
    httpd = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    S.tmp, S.port = tmp, httpd.server_address[1]
    S.admin, S.bob = Client(S.port), Client(S.port)
    try:
        yield
    finally:
        httpd.shutdown()
        httpd.server_close()
        os.environ["PATH"] = old_path
        serve.RUNS.clear()
        serve.configure()                      # back to whatever the real environment says
        shutil.rmtree(tmp, ignore_errors=True)


def make_job(client, course, chapter, title):
    import base64
    return client.post("/api/jobs", {"course": course, "chapter": chapter, "title": title,
                                     "files": [{"name": "notes.pdf", "b64": base64.b64encode(PDF).decode()}]})


# ---- start-up -----------------------------------------------------------------------
def test_refuses_to_start_without_secret():
    """LT_HOSTED=1 without LT_SECRET must stop with a readable message, before touching anything."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LT_")}
    env.update({"LT_HOSTED": "1", "LT_DATA": str(S.tmp / "unused-data"), "LT_ADMIN_EMAIL": ADMIN})
    r = subprocess.run([sys.executable, str(HERE / "serve.py"), "8798", "--no-browser"],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "LT_SECRET" in r.stderr and "hosted mode cannot start" in r.stderr
    assert not (S.tmp / "unused-data").exists()
    repo_jobs = serve.ROOT / "jobs"
    assert not repo_jobs.is_symlink()          # the real repo's jobs/ was left alone


# ---- accounts ------------------------------------------------------------------------
def test_unauthenticated_api_is_401_but_page_is_public():
    code, body = Client(S.port).get("/api/jobs")
    assert (code, body) == (401, {"error": "sign in"})
    code, body = Client(S.port).get("/api/me")
    assert code == 200 and body["hosted"] is True and body["signed_in"] is False
    assert body["limits"] == {"max_parallel_runs": 3}
    code, _ = Client(S.port).get("/")
    assert code == 200


def test_admin_first_signup_needs_no_invite():
    code, body = S.admin.post("/api/auth/signup", {"email": ADMIN, "password": "owner-pass-1", "name": "Owner"})
    assert code == 201, body
    assert body["signed_in"] and body["user"]["admin"] is True and body["user"]["email"] == ADMIN
    assert body["usage"] == {"month": serve.month_now(), "claude_usd": 0.0, "elevenlabs_chars": 0,
                             "jobs": 0, "budget_usd": 20.0}
    assert "lt_session" in S.admin.cookies
    S.admin_id = body["user"]["id"]
    assert S.admin_id.startswith("u_") and len(S.admin_id) == 10
    assert (S.tmp / "jobs" / S.admin_id).is_dir()
    users = json.loads((S.tmp / "users.json").read_text())["users"]
    assert users[0]["pw"].startswith("scrypt$") and "owner-pass-1" not in users[0]["pw"]
    assert oct((S.tmp / "users.json").stat().st_mode & 0o777) == "0o600"
    # a second sign-up with the owner's address is a duplicate, not a second admin
    code, body = Client(S.port).post("/api/auth/signup", {"email": ADMIN, "password": "owner-pass-2"})
    assert code == 409


def test_second_signup_needs_a_valid_invite_and_burns_it():
    code, body = S.bob.post("/api/auth/signup", {"email": "bob@example.com", "password": "bobs-password", "name": "Bob"})
    assert code == 400 and "invite" in body["error"]
    code, body = S.bob.post("/api/auth/signup", {"email": "bob@example.com", "password": "bobs-password",
                                                 "name": "Bob", "invite": "nope"})
    assert code == 400 and "not valid" in body["error"]
    code, inv = S.admin.post("/api/admin/invites", {"note": "for Bob"})
    assert code == 201 and inv["used_by"] is None and inv["created_by"] == S.admin_id and inv["note"] == "for Bob"
    code, body = S.bob.post("/api/auth/signup", {"email": "Bob@Example.com", "password": "short", "invite": inv["code"]})
    assert code == 400 and "8 characters" in body["error"]
    code, body = S.bob.post("/api/auth/signup", {"email": "Bob@Example.com", "password": "bobs-password",
                                                 "name": "Bob", "invite": inv["code"]})
    assert code == 201, body
    assert body["user"]["admin"] is False and body["user"]["email"] == "bob@example.com"
    S.bob_id = body["user"]["id"]
    # the same code cannot be used again
    code, body = Client(S.port).post("/api/auth/signup", {"email": "carol@example.com", "password": "carols-password",
                                                          "invite": inv["code"]})
    assert code == 400 and "already used" in body["error"]
    code, body = S.admin.get("/api/admin/invites")
    assert code == 200 and body["invites"][0]["used_by"] == S.bob_id


def test_cookie_works_and_signout_clears_it():
    code, body = S.bob.get("/api/me")
    assert code == 200 and body["signed_in"] and body["user"]["id"] == S.bob_id
    code, body = S.bob.get("/api/jobs")
    assert (code, body) == (200, [])
    code, body = S.bob.post("/api/auth/signout")
    assert code == 200 and body["signed_in"] is False
    assert "lt_session" not in S.bob.cookies
    code, body = S.bob.get("/api/jobs")
    assert code == 401
    # a forged cookie is ignored
    S.bob.cookies["lt_session"] = f"{S.bob_id}.{int(time.time()) + 999999}.deadbeef"
    assert S.bob.get("/api/jobs")[0] == 401
    S.bob.cookies.clear()
    code, body = S.bob.post("/api/auth/signin", {"email": "bob@example.com", "password": "bobs-password"})
    assert code == 200 and body["user"]["id"] == S.bob_id and "lt_session" in S.bob.cookies


def test_signin_errors_and_rate_limit():
    c = Client(S.port)
    code, body = c.post("/api/auth/signin", {"email": "bob@example.com", "password": "wrong"})
    assert (code, body) == (401, {"error": "wrong e-mail or password"})
    for _ in range(10):
        code, _b = c.post("/api/auth/signin", {"email": "nobody@example.com", "password": "x"})
        assert code == 401
    code, body = c.post("/api/auth/signin", {"email": "nobody@example.com", "password": "x"})
    assert code == 429 and "try again later" in body["error"]
    code, _b = c.post("/api/auth/signin", {"email": "nobody@example.com", "password": "x"})
    assert code == 429


def test_local_only_routes_are_gone_in_hosted_mode():
    assert S.admin.post("/api/update")[0] == 404
    assert S.admin.post("/api/publish", {"message": "x"})[0] == 404
    code, body = S.admin.get("/api/version")
    assert code == 200 and body["hosted"] is True and body["developer"] is False


def test_non_admin_cannot_use_admin_routes_or_change_settings():
    assert S.bob.get("/api/admin/users")[0] == 403
    assert S.bob.get("/api/admin/invites")[0] == 403
    assert S.bob.get("/api/admin/usage")[0] == 403
    assert S.bob.post("/api/admin/invites", {"note": "x"})[0] == 403
    assert S.bob.post("/api/admin/users", {"id": S.bob_id, "budget_usd": 1000})[0] == 403
    assert S.bob.delete("/api/admin/invites?code=x")[0] == 403
    code, body = S.bob.post("/api/settings", {"elevenlabs_voice_id": "abc"})
    assert code == 403 and "admin" in body["error"]
    code, body = S.bob.get("/api/settings")
    assert code == 200 and body == {"elevenlabs": {"has_key": False}}
    code, body = S.admin.get("/api/settings")
    assert code == 200 and "voices" in body["elevenlabs"] and "voice_id" in body["elevenlabs"]
    code, body = S.admin.get("/api/admin/users")
    assert code == 200 and {u["email"] for u in body["users"]} == {ADMIN, "bob@example.com"}
    assert all("pw" not in u for u in body["users"])
    assert body["default_budget_usd"] == 20.0


# ---- workspaces -------------------------------------------------------------------------
def test_jobs_are_scoped_to_the_signed_in_user():
    code, body = make_job(S.admin, "math", "ch1", "Owner's chapter")
    assert code == 201 and body["dir"] == "math/ch1", body
    assert (S.tmp / "jobs" / S.admin_id / "math" / "ch1" / "job.json").exists()
    assert not (S.tmp / "jobs" / "math").exists()
    job = json.loads((S.tmp / "jobs" / S.admin_id / "math" / "ch1" / "job.json").read_text())
    assert job["tts"] == "elevenlabs"                  # no macOS voice on a server
    # Bob can use the same course/chapter name: it is his own workspace
    code, body = make_job(S.bob, "math", "ch1", "Bob's chapter")
    assert code == 201 and body["dir"] == "math/ch1"
    assert (S.tmp / "jobs" / S.bob_id / "math" / "ch1" / "job.json").exists()
    code, jobs = S.bob.get("/api/jobs")
    assert code == 200 and [j["title"] for j in jobs] == ["Bob's chapter"]
    code, jobs = S.admin.get("/api/jobs")
    assert [j["title"] for j in jobs] == ["Owner's chapter"]
    code, body = S.admin.get("/api/job?dir=math/ch1")
    assert code == 200 and body["job"]["title"] == "Owner's chapter"
    # Bob cannot reach the owner's folder by any path
    code, body = S.bob.get(f"/api/job?dir=../{S.admin_id}/math/ch1")
    assert code == 400 and "outside" in body["error"]
    code, body = S.bob.get(f"/api/file?dir=../{S.admin_id}/math/ch1&name=job.json")
    assert code == 400
    code, body = S.bob.get(f"/api/zip?dir=../{S.admin_id}/math/ch1")
    assert code == 400
    code, body = S.bob.post("/api/job/update", {"dir": f"../{S.admin_id}/math/ch1", "title": "hacked"})
    assert code == 400
    code, body = S.bob.post("/api/job/update", {"dir": "math/ch1", "tts": "say"})
    assert code == 400 and "ElevenLabs" in body["error"]
    assert S.bob.delete(f"/api/jobs?dir=../{S.admin_id}/math/ch1")[0] == 400
    # deleting his own job leaves the owner's untouched
    code, body = make_job(S.bob, "math", "ch2", "Bob's second")
    assert code == 201
    code, body = S.bob.delete("/api/jobs?dir=math/ch2")
    assert code == 200 and body == {"deleted": "math/ch2"}
    assert not (S.tmp / "jobs" / S.bob_id / "math" / "ch2").exists()
    assert (S.tmp / "jobs" / S.admin_id / "math" / "ch1" / "job.json").exists()


def test_zip_roundtrip_stays_inside_the_workspace():
    code, raw = S.admin.get("/api/zip?dir=math/ch1")
    assert code == 200 and isinstance(raw, bytes) and raw[:2] == b"PK"
    conn = http.client.HTTPConnection("127.0.0.1", S.port, timeout=15)
    conn.request("POST", "/api/import", body=raw,
                 headers={"Cookie": f"lt_session={S.bob.cookies['lt_session']}", "X-File-Name": "x.zip",
                          "Content-Length": str(len(raw))})
    r = conn.getresponse()
    body = json.loads(r.read())
    assert r.status == 409 or r.status == 400, body        # Bob already has math/ch1
    assert "already exists" in body["error"]
    code, body = S.bob.delete("/api/jobs?dir=math/ch1")
    assert code == 200
    conn = http.client.HTTPConnection("127.0.0.1", S.port, timeout=15)
    conn.request("POST", "/api/import", body=raw,
                 headers={"Cookie": f"lt_session={S.bob.cookies['lt_session']}", "X-File-Name": "x.zip",
                          "Content-Length": str(len(raw))})
    r = conn.getresponse()
    body = json.loads(r.read())
    assert r.status == 201 and body["dir"] == "math/ch1", body
    assert (S.tmp / "jobs" / S.bob_id / "math" / "ch1" / "job.json").exists()
    assert not (S.tmp / "jobs" / "math").exists()


# ---- runs and metering -------------------------------------------------------------------
def test_run_end_to_end_writes_cost_json_and_usage():
    code, body = S.admin.post("/api/run", {"dir": "math/ch1", "from": "all"})
    assert code == 202 and body["running"] is True, body
    d = S.tmp / "jobs" / S.admin_id / "math" / "ch1"
    for _ in range(150):
        if (d / "cost.json").exists() and not serve.run_info(f"{S.admin_id}/math/ch1")["running"]:
            break
        time.sleep(0.1)
    cost = json.loads((d / "cost.json").read_text())
    assert cost["claude_usd"] == 1.25 and cost["elevenlabs_chars"] == 0
    run = cost["runs"][0]
    assert run["from"] == "all" and run["claude_usd"] == 1.25 and run["duration_s"] == 1
    assert run["model"] == "claude-sonnet-5-5" and run["started"].startswith(serve.month_now())
    log = (d / "run.log").read_text()
    assert f"ARGS: -p /lecture jobs/{S.admin_id}/math/ch1 " in log      # the real path, repo-relative
    assert "=== result after" in log and "$1.25" in log
    assert f"{S.admin_id}/math/ch1" in serve.RUNS and "math/ch1" not in serve.RUNS
    code, me = S.admin.get("/api/me")
    assert me["usage"] == {"month": serve.month_now(), "claude_usd": 1.25, "elevenlabs_chars": 0,
                           "jobs": 1, "budget_usd": 20.0}
    assert (S.tmp / "usage" / S.admin_id / f"{serve.month_now()}.json").exists()
    code, body = S.admin.get("/api/admin/usage")
    row = next(u for u in body["users"] if u["id"] == S.admin_id)
    assert row["months"][serve.month_now()] == {"claude_usd": 1.25, "elevenlabs_chars": 0, "jobs": 1}
    code, body = S.admin.get("/api/admin/users")
    assert next(u for u in body["users"] if u["id"] == S.admin_id)["usage"]["claude_usd"] == 1.25
    code, body = S.bob.get("/api/me")
    assert body["usage"]["claude_usd"] == 0.0                   # the owner's spending is not Bob's


def test_render_chars_are_recorded_from_render_log():
    d = S.tmp / "jobs" / S.admin_id / "math" / "ch1"
    (d / "render.log").write_text("ElevenLabs (eleven_turbo_v2_5): 8620 characters still to synthesize ≈ 4310 credits\n")
    rec = {"started": serve.datetime.now(serve.timezone.utc).isoformat(timespec="seconds"), "from": "render"}
    serve.record_run_cost(d, rec)
    cost = json.loads((d / "cost.json").read_text())
    assert cost["elevenlabs_chars"] == 8620 and cost["claude_usd"] == 1.25 and len(cost["runs"]) == 2
    assert cost["runs"][1]["claude_usd"] == 0 and cost["runs"][1]["model"] == "cli default"
    # a render.log older than the run is a previous render and does not count twice
    old = {"started": "2030-01-01T00:00:00+00:00", "from": "render"}
    assert serve.render_chars(d, old["started"]) == 0
    code, me = S.admin.get("/api/me")
    assert me["usage"]["elevenlabs_chars"] == 8620 and me["usage"]["claude_usd"] == 1.25


def test_run_refused_over_budget():
    d = S.tmp / "jobs" / S.bob_id / "math" / "ch1"
    (d / "cost.json").write_text(json.dumps({"runs": [{"started": serve.month_now() + "-02T10:00:00+00:00",
                                                       "from": "all", "claude_usd": 50.0, "duration_s": 600,
                                                       "model": "x", "elevenlabs_chars": 0}], "claude_usd": 50.0}))
    code, body = S.bob.post("/api/run", {"dir": "math/ch1"})
    assert code == 402 and body["error"] == "your monthly budget is used up — ask the admin for more"
    assert not serve.run_info(f"{S.bob_id}/math/ch1")["running"]
    # a render-only run spends no Claude money: it is not held against the budget (it fails later
    # for the plain reason that there is no storyboard yet)
    code, body = S.bob.post("/api/run", {"dir": "math/ch1", "from": "render"})
    assert code == 400 and "scenes.json" in body["error"]
    # the admin raises the budget and Bob may run again
    code, body = S.admin.post("/api/admin/users", {"id": S.bob_id, "budget_usd": 60})
    assert code == 200 and body["budget_usd"] == 60.0 and body["usage"]["claude_usd"] == 50.0
    code, body = S.bob.get("/api/me")
    assert body["usage"]["budget_usd"] == 60.0 and body["usage"]["claude_usd"] == 50.0
    code, body = S.bob.post("/api/run", {"dir": "math/ch1"})
    assert code == 202
    for _ in range(150):                                     # the tailer records the bill ~1 s after exit
        if len(json.loads((d / "cost.json").read_text()).get("runs", [])) == 2:
            break
        time.sleep(0.1)
    code, body = S.bob.get("/api/me")
    assert body["usage"]["claude_usd"] == 51.25


class _Live:
    """Stands in for a Popen that is still running."""
    pid = 999999

    def poll(self):
        return None


def test_run_refused_at_parallel_limit():
    saved = dict(serve.RUNS)
    try:
        for i in range(3):
            serve.RUNS[f"u_other{i}/c/ch"] = {"proc": _Live(), "started": "2026-10-29T10:00:00+00:00", "from": "all"}
        code, body = S.admin.post("/api/run", {"dir": "math/ch1"})
        assert code == 429 and body["error"] == "3 lectures are being made right now — try again in a few minutes"
        code, body = S.admin.post("/api/run", {"dir": "math/ch1", "from": "render"})
        assert code == 429                                   # renders count against the machine too
        assert S.admin.get("/api/jobs")[1][0]["running"] is False
    finally:
        serve.RUNS.clear()
        serve.RUNS.update(saved)


# ---- account management ------------------------------------------------------------------
def test_password_change():
    code, body = S.bob.post("/api/auth/password", {"old": "wrong", "new": "bobs-new-password"})
    assert code == 400 and "current password" in body["error"]
    code, body = S.bob.post("/api/auth/password", {"old": "bobs-password", "new": "short"})
    assert code == 400
    code, body = S.bob.post("/api/auth/password", {"old": "bobs-password", "new": "bobs-new-password"})
    assert (code, body) == (200, {"ok": True})
    c = Client(S.port)
    assert c.post("/api/auth/signin", {"email": "bob@example.com", "password": "bobs-password"})[0] == 401
    assert c.post("/api/auth/signin", {"email": "bob@example.com", "password": "bobs-new-password"})[0] == 200


def test_admin_edits_people_and_invites():
    code, body = S.admin.post("/api/admin/users", {"id": S.admin_id, "disabled": True})
    assert code == 400 and "your own account" in body["error"]
    code, body = S.admin.post("/api/admin/users", {"id": S.admin_id, "admin": False})
    assert code == 400 and "your own admin" in body["error"]
    assert S.admin.post("/api/admin/users", {"id": "u_nobody", "budget_usd": 1})[0] == 404
    assert S.admin.post("/api/admin/users", {"id": S.bob_id, "budget_usd": "lots"})[0] == 400
    code, body = S.admin.post("/api/admin/users", {"id": S.bob_id, "disabled": True})
    assert code == 200 and body["disabled"] is True
    code, body = S.bob.get("/api/me")
    assert code == 200 and body["signed_in"] is False        # the session died with the account
    assert S.bob.get("/api/jobs")[0] == 401
    code, body = Client(S.port).post("/api/auth/signin", {"email": "bob@example.com", "password": "bobs-new-password"})
    assert code == 403 and "disabled" in body["error"]
    code, body = S.admin.post("/api/admin/users", {"id": S.bob_id, "disabled": False})
    assert code == 200 and body["disabled"] is False
    assert S.bob.get("/api/jobs")[0] == 200
    code, inv = S.admin.post("/api/admin/invites", {"note": "spare"})
    assert code == 201
    code, body = S.admin.delete(f"/api/admin/invites?code={inv['code']}")
    assert (code, body) == (200, {"deleted": inv["code"]})
    assert S.admin.delete(f"/api/admin/invites?code={inv['code']}")[0] == 404
    assert all(i["code"] != inv["code"] for i in S.admin.get("/api/admin/invites")[1]["invites"])


def test_same_origin_check_uses_public_host_or_host_header():
    c = Client(S.port)
    c.cookies = dict(S.admin.cookies)
    code, body = c.request("POST", "/api/admin/invites", {"note": "x"}, {"Origin": "https://evil.example"})
    assert code == 403 and "cross-site" in body["error"]
    code, body = c.request("POST", "/api/admin/invites", {"note": "x"}, {"Origin": f"http://127.0.0.1:{S.port}"})
    assert code == 201                                       # Origin matches the Host header
    code, body = c.request("POST", "/api/admin/invites", {"note": "x"}, {"Sec-Fetch-Site": "cross-site"})
    assert code == 403


# ---- local mode is untouched -------------------------------------------------------------
def test_local_mode_has_no_auth_and_uses_repo_jobs():
    serve.configure({})                                      # no LT_* at all = the owner's Mac
    try:
        assert serve.HOSTED is False
        assert serve.JOBS == serve.ROOT / "jobs" and serve.ENV_FILE == serve.ROOT / ".env"
        assert serve.PORT == 8765
        c = Client(S.port)
        code, body = c.get("/api/me")
        assert (code, body) == (200, {"hosted": False})
        code, body = c.get("/api/jobs")
        assert code == 200 and isinstance(body, list)
        assert all("/" in j["dir"] and j["dir"].count("/") == 1 for j in body)
        assert c.post("/api/auth/signin", {"email": ADMIN, "password": "x"})[0] == 404
        assert c.get("/api/admin/users")[0] == 403           # no admin without accounts
        code, body = c.get("/api/version")
        assert code == 200 and body["hosted"] is False
        code, body = c.get("/api/settings")
        assert code == 200 and "voices" in body["elevenlabs"]
        serve.configure({"PORT": "9999"})
        assert serve.PORT == 8765                            # PORT from the environment is hosted-only
    finally:
        serve.configure(hosted_env(S.tmp))
