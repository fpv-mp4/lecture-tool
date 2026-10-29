# Lecture Tool — run it as a hosted service (Fly.io)

_For the owner. One Linux machine at Fly.io runs the same tool as your Mac does; you and the people
you invite open it in the browser, sign in, and each has their own workspace. The technical contract
is in `docs/HOSTED.md`; this page is the step-by-step._

Everything below happens in **Terminal** on your Mac, inside the tool's folder:

```
cd ~/Documents/UNI/lecture-tool
```

## 1. One-time setup (about 20 minutes)

**Fly account.** Go to https://fly.io/app/sign-up and create an account. Add a credit card — a machine
of this size is not covered by any free allowance.

**The Fly command line tool** is already installed on this Mac (via Homebrew). On another Mac:
`brew install flyctl`. Sign in once:

```
fly auth login
```

A browser window opens; approve it, then come back to Terminal.

**Anthropic API key.** The service writes lectures with its own key, not with your Claude subscription.
Open https://console.anthropic.com, create an API key, and — important — under *Settings → Limits*
set a monthly spending limit you are comfortable with (for example 100 USD). The panel also has
per-person budgets (step 3), but the limit at Anthropic is the hard stop.

**ElevenLabs key.** You already have one (the narration voice). You can give it to the service now
(step 2) or later under *Settings → Narration* in the panel. Without it no video can be narrated:
the Mac voice does not exist on Linux, so a job set to it stops with "Narration is not set up".

## 2. Create the service

```
bash scripts/deploy.sh
```

The first run creates the app `lecture-tool` and its 30 GB disk in Frankfurt, then stops and prints
four lines like these — one per secret the service needs:

```
fly secrets set LT_SECRET=<a long random value, generated for you> -a lecture-tool
fly secrets set LT_ADMIN_EMAIL=you@example.com -a lecture-tool
fly secrets set ANTHROPIC_API_KEY=<paste the key> -a lecture-tool
fly secrets set ELEVENLABS_API_KEY=<paste the key> -a lecture-tool
```

Run each line in Terminal, replacing the `<…>` part with the real value. Paste keys **only there**:
never into a chat, an e-mail or a document. The `LT_SECRET` line comes with a value already filled in;
use it as it is. `LT_ADMIN_EMAIL` is your own e-mail: the first person to sign up with it becomes the admin.

Then run the script again:

```
bash scripts/deploy.sh
```

It builds the machine image (3–6 minutes the first time) and ends with the address, normally
**https://lecture-tool.fly.dev**. If the name is taken, pick another: `bash scripts/deploy.sh my-name`
(the address becomes `https://my-name.fly.dev`; use that name in every later command).

## 3. First steps in the panel

1. Open the address. Click **Sign up**, use your admin e-mail and a password of at least 8 characters.
2. **Invite a friend:** *Settings → People → Invite* gives you a code. Send them the code and the
   address; they sign up with it. Nobody can sign up without a code.
3. **Budgets:** *Settings → People* shows each person's usage this month and their monthly Claude
   budget (default 20 USD). Change it there. When a budget is used up, that person's runs stop
   until the next month or until you raise it.
4. **Narration:** if you did not set the ElevenLabs key as a secret, paste it under *Settings → Narration*.

## 4. What it costs

Prices at Fly change; check https://fly.io/docs/about/pricing/. As of October 2026, about:

| item | about per month |
|---|---|
| machine `performance-2x` (2 dedicated CPUs) with 6 GB RAM, running all the time: fast for 2–3 people, up to three lectures at once | about USD 78 (66 for 4 GB + 6 per extra GB) |
| 30 GB disk | USD 4.50 (0.15 per GB) |
| traffic (videos people download) | USD 0.02 per GB — a few dollars at most |

So roughly **USD 85 per month** for the machine, plus what the people actually use:
Claude (billed by Anthropic to your API key; the per-person budgets cap it) and ElevenLabs credits.

The machine runs around the clock on purpose (a lecture run takes up to an hour and must not be cut
off). To pause the service and stop paying for the machine — the disk and all data stay —
run `fly machine stop -a lecture-tool`; `fly machine start -a lecture-tool` brings it back.

## 5. Everyday

**Update the service** to the current code on this Mac (same as the first time):

```
bash scripts/deploy.sh
```

A deploy restarts the machine; wait until nobody has a run going (the panel shows running jobs).

**See what is happening** (live log; Ctrl+C to leave):

```
fly logs -a lecture-tool
```

**Look inside the machine** — a shell on it; all data is under `/data`:

```
fly ssh console -a lecture-tool
```

**Copy data back to your Mac**, for example one finished job or everything:

```
fly sftp get /data/jobs/<user-id>/<course>/<chapter>/lecture.mp4 -a lecture-tool
fly sftp shell -a lecture-tool          # then:  get -R /data/jobs ./jobs-backup
```

Fly keeps daily snapshots of the disk for five days (`fly volumes snapshots list <volume id> -a lecture-tool`).
A proper off-site backup is the next step once the service has users (see `docs/HOSTED.md` §9).

**If something is wrong**: `fly status -a lecture-tool` shows whether the machine is up and healthy;
`fly logs` shows why it is not. The service refuses to start when `LT_SECRET` or `LT_ADMIN_EMAIL`
is missing — `fly secrets list -a lecture-tool` shows which secrets exist (names only, never values).
