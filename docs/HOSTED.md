# Lecture Tool — hosted mode (contract)

_Written 2026-10-29. How the personal tool becomes a hosted, multi-person service without a rewrite: the same `scripts/serve.py`, `ui/index.html`, `.claude/skills/lecture`, `scripts/render.py` and `scripts/ingest.py` run on a Linux machine. Everything here is binding for the three parts (server, panel, deployment). Local mode stays exactly as it is: nothing below applies unless `LT_HOSTED=1`._

## 1. Principles
- One codebase, two modes. `LT_HOSTED=1` switches the server into hosted mode; without it the panel behaves as today on a Mac (no sign-in, one workspace, Settings with the ElevenLabs key, Update/Publish buttons).
- The pipeline is unchanged: a job is still a folder with `job.json`, `status.json`, `run.log`; a run is still `claude -p "/lecture jobs/<dir>"` (`--permission-mode bypassPermissions`, stream-json), now with `ANTHROPIC_API_KEY` from the environment. The skill is executed by the Claude Code CLI installed in the image.
- Plain language in the panel (the owner's rule): "Sign in", "Invite", "Your usage this month", never a command to paste.
- Secrets: `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `LT_SECRET` (cookie signing) come from the environment (Fly secrets). The admin may still set the ElevenLabs key and voice in Settings; it is stored in `<LT_DATA>/.env` as today. Keys are never shown back.

## 2. Environment
| variable | meaning | default |
|---|---|---|
| `LT_HOSTED` | `1` = hosted mode | unset (local) |
| `LT_DATA` | data root on the volume: `jobs/`, `users.json`, `.env`, `invites.json` | `<repo>/` in local mode, `/data` in the image |
| `LT_SECRET` | HMAC key for session cookies (≥ 32 random chars) | required in hosted mode (refuse to start without it) |
| `LT_ADMIN_EMAIL` | the owner's e-mail: first sign-up with this address becomes admin without an invite | required in hosted mode |
| `LT_USER_BUDGET_USD` | default monthly Claude budget per person | `20` |
| `LT_MAX_PARALLEL_RUNS` | runs at the same time on this machine (all users) | `3` |
| `PORT` | listen port; hosted mode binds `0.0.0.0` | `8765` |
| `LT_PUBLIC_HOST` | host the panel is reached at (for the same-origin check), e.g. `lecture-tool.fly.dev` | derived from the `Host` header if unset |

## 3. Data layout (`LT_DATA`)
```
users.json        {"users": [{"id": "u_ab12", "email": "…", "name": "…", "pw": "<scrypt$salt$hash>", "admin": bool,
                              "budget_usd": 20, "created": iso, "disabled": false}]}
invites.json      {"invites": [{"code": "…", "created_by": "u_…", "created": iso, "used_by": null|"u_…", "note": "…"}]}
jobs/<user_id>/<course>/<chapter>/…    exactly today's job folder
.env              ELEVENLABS_API_KEY=…, ELEVENLABS_VOICE_ID=… (admin-managed)
```
Writes to `users.json` and `invites.json` go through one lock and a temp-file rename. The user id is `u_` + 8 hex chars; a user's workspace is `jobs/<user_id>/`.

## 4. Accounts and sessions (stdlib only)
- Password hashing: `hashlib.scrypt(pw, salt=16 random bytes, n=2**14, r=8, p=1)`; stored as `scrypt$<salt hex>$<hash hex>`; passwords ≥ 8 characters.
- Session cookie `lt_session` = `<user_id>.<expiry unix>.<hmac-sha256(secret, user_id.expiry)>`, `HttpOnly; Secure; SameSite=Lax; Path=/`, 30 days, renewed on use. Sign-out clears it.
- Sign-up needs an invite code, except the first sign-up with `LT_ADMIN_EMAIL`. One invite = one account. Admin creates invites in Settings → People.
- Rate limit sign-in: 10 failures per e-mail per 15 minutes → "try again later".
- Same-origin check in hosted mode: `Origin`/`Sec-Fetch-Site` must match `LT_PUBLIC_HOST` (or the request `Host`).

## 5. API in hosted mode
Every existing route keeps its path and payload; `dir` values stay `course/chapter` and are resolved inside the signed-in user's workspace (`safe_job_dir` prefixes `jobs/<user_id>/`). Unauthenticated calls to `/api/*` return `401 {"error": "sign in"}`; the panel then shows the sign-in screen. Static files and `/` are public (the panel itself decides what to show).

New routes:
| route | who | purpose |
|---|---|---|
| `GET /api/me` | anyone | `{hosted: true, signed_in: bool, user: {id, email, name, admin}, usage: {month: "2026-10", claude_usd, budget_usd, elevenlabs_chars, jobs}, limits: {max_parallel_runs}}`; in local mode `{hosted: false}` |
| `POST /api/auth/signup` `{email, password, name, invite}` | anyone | creates the account, sets the cookie |
| `POST /api/auth/signin` `{email, password}` | anyone | sets the cookie |
| `POST /api/auth/signout` | signed in | clears the cookie |
| `POST /api/auth/password` `{old, new}` | signed in | change password |
| `GET /api/admin/users` | admin | users with this month's usage and budget |
| `POST /api/admin/users` `{id, budget_usd?, disabled?, admin?}` | admin | edit a person |
| `GET /api/admin/invites`, `POST /api/admin/invites` `{note}`, `DELETE /api/admin/invites?code=` | admin | invite codes |
| `GET /api/admin/usage` | admin | per-user, per-month totals (claude_usd, elevenlabs_chars, jobs) |

Hidden in hosted mode for non-admins: `POST /api/settings` (403), `/api/update`, `/api/publish` (404 for everyone in hosted mode). `GET /api/settings` returns `has_key` and voices only for the admin; for others `{elevenlabs: {has_key: true|false}}`.

## 6. Metering and budgets
- When a run's `result` event arrives, the tailer writes `cost.json` in the job folder: `{"runs": [{"started": iso, "from": "all|lecture|audit|scenes|render", "claude_usd": float, "duration_s": int, "model": "…"}], "claude_usd": total}`. Narration characters come from `render.log` (the line "ElevenLabs (…): N characters still to synthesize") summed into `elevenlabs_chars`.
- A person's month usage = sum over their jobs' `cost.json` for runs started in that month (`LT_DATA/usage/<user_id>/<YYYY-MM>.json` is a cache rebuilt lazily; the job folders are the truth).
- `POST /api/run` refuses with `402 {"error": "your monthly budget is used up — ask the admin for more"}` when `claude_usd >= budget_usd`, and with `429 {"error": "N lectures are being made right now — try again in a few minutes"}` when `LT_MAX_PARALLEL_RUNS` is reached. `from: render` (no Claude) is only limited by parallel runs.
- The sidebar footer shows "Your usage this month: $x of $y" (hosted only).

## 7. Panel (ui/index.html) in hosted mode
- On load call `GET /api/me`. If `hosted` and not signed in: show the sign-in/sign-up screen (e-mail, password, name and invite code on sign-up; link between the two; errors in plain words). After sign-in, the normal panel.
- Sidebar footer: name, "Your usage this month: $x of $y", Sign out. Settings → "Narration" only for admin; "Updates" section hidden in hosted mode; new "People" section for admin: invites (create, copy link `https://<host>/?invite=<code>`, delete) and the people table (name, e-mail, usage, budget, disabled toggle).
- `?invite=<code>` in the URL pre-fills the sign-up form.
- `tts`: hosted mode offers only ElevenLabs (no `say`); if the server reports `has_key: false`, the video option explains "Narration is not set up yet" and stays off.
- Zip export/import stay. Notifications stay. The theme picker stays.

## 8. Deployment (Fly.io)
- `Dockerfile` at the repo root (published with the tool): `python:3.12-slim-bookworm`, apt `ffmpeg fonts-dejavu-core fonts-noto-core curl git`, Node 22 (from `node:22-bookworm-slim`), `npm i -g @anthropic-ai/claude-code`, `pip install pymupdf pillow fonttools brotli`, non-root user, `LT_DATA=/data`, `CMD python3 scripts/serve.py --no-browser` with `PORT` from the environment.
- `fly.toml`: app `lecture-tool`, region `fra`, `[mounts] source="data" destination="/data"`, `[http_service] internal_port=8765 force_https=true auto_stop_machines=false min_machines_running=1`, machine `performance-2x` 8 GB, health check `GET /api/me`. Volume snapshots are Fly's daily ones.
- `scripts/deploy.sh`: creates the app and the 50 GB volume on first use, prints the three `fly secrets set` lines (names only), deploys, prints the URL. `docs/HOSTED-SETUP.md`: the owner's steps in plain words (Fly account, `fly auth login`, where the two keys go, how to invite the first friend, how to change a budget).
- Renderer on Linux: `say` does not exist; when `ELEVENLABS_API_KEY` is missing, `render.py` must stop with a readable error ("Narration is not set up") instead of trying `say`; in hosted mode the panel never offers `say`. Glyph fallback fonts: DejaVu (installed) instead of Menlo/STIX.
- The Claude Code CLI needs `HOME` writable (`/home/app`) for its config; `ANTHROPIC_API_KEY` in the run environment makes it headless (no login). `CLAUDE_CONFIG_DIR` may point at `/data/claude` so the CLI's state survives deploys.

## 9. Out of scope now
Payments, public sign-up, sharing links, e-mail sending, multiple machines. Backups beyond Fly volume snapshots (a nightly tar to R2 is the next step once the service has users).
