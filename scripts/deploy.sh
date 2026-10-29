#!/bin/bash
# Deploy the Lecture Tool to Fly.io (contract: docs/HOSTED.md §8; owner's guide: docs/HOSTED-SETUP.md).
#
#   bash scripts/deploy.sh                    app name "lecture-tool" (the default in fly.toml)
#   bash scripts/deploy.sh my-lecture-tool    another app name (becomes https://my-lecture-tool.fly.dev)
#
# Safe to run as often as you like: it creates the app and the data volume only when they are
# missing, tells you which secrets are still unset (and the exact line to set each one), then
# deploys the current code and prints the address. Run it again to update the service.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-${LT_FLY_APP:-lecture-tool}}"
REGION="${LT_FLY_REGION:-fra}"
VOLUME="data"
VOLUME_GB="${LT_FLY_VOLUME_GB:-30}"
REQUIRED_SECRETS=(LT_SECRET LT_ADMIN_EMAIL ANTHROPIC_API_KEY)
OPTIONAL_SECRETS=(ELEVENLABS_API_KEY)

step() { printf '\n== %s\n' "$*"; }

# ---- 1. fly installed and signed in --------------------------------------------------------------
if ! command -v fly >/dev/null 2>&1; then
  cat <<MSG
The Fly command line tool is not installed. Two steps:
  1. Install it:   brew install flyctl
  2. Sign in:      fly auth login        (opens your browser)
Then run this script again.
MSG
  exit 1
fi
if ! fly auth whoami >/dev/null 2>&1; then
  cat <<MSG
You are not signed in to Fly. Two steps:
  1. Create a free account at https://fly.io/app/sign-up (a credit card is required)
  2. Sign in:      fly auth login        (opens your browser)
Then run this script again.
MSG
  exit 1
fi
step "signed in to Fly as $(fly auth whoami 2>/dev/null)"

# small helpers around fly's JSON output (python3 is always there: the tool itself needs it)
json_has_name() {  # stdin: JSON list; $1: name — exit 0 when an entry's name/Name equals $1
  python3 -c '
import json, sys
items = json.load(sys.stdin) or []
want = sys.argv[1]
sys.exit(0 if any((i.get("Name") or i.get("name")) == want for i in items if isinstance(i, dict)) else 1)
' "$1"
}

# ---- 2. the app ----------------------------------------------------------------------------------
if fly apps list --json 2>/dev/null | json_has_name "$APP"; then
  step "app $APP exists"
else
  step "creating app $APP"
  fly apps create "$APP" ${FLY_ORG:+--org "$FLY_ORG"}
fi

# ---- 3. the data volume --------------------------------------------------------------------------
if fly volumes list -a "$APP" --json 2>/dev/null | json_has_name "$VOLUME"; then
  step "volume $VOLUME exists"
else
  step "creating volume $VOLUME (${VOLUME_GB} GB in $REGION)"
  fly volumes create "$VOLUME" --app "$APP" --region "$REGION" --size "$VOLUME_GB" --yes
fi

# ---- 4. secrets ----------------------------------------------------------------------------------
step "checking secrets"
existing="$(fly secrets list -a "$APP" --json 2>/dev/null || echo '[]')"
missing_required=()
missing_optional=()
for name in "${REQUIRED_SECRETS[@]}"; do
  printf '%s' "$existing" | json_has_name "$name" || missing_required+=("$name")
done
for name in "${OPTIONAL_SECRETS[@]}"; do
  printf '%s' "$existing" | json_has_name "$name" || missing_optional+=("$name")
done

print_secret_line() {
  case "$1" in
    LT_SECRET)
      echo "  fly secrets set LT_SECRET=$(openssl rand -hex 32) -a $APP"
      echo "      (the cookie-signing key; the random value above was generated just now — use it as it is)";;
    LT_ADMIN_EMAIL)
      echo "  fly secrets set LT_ADMIN_EMAIL=you@example.com -a $APP"
      echo "      (your own e-mail; the first sign-up with this address becomes the admin)";;
    ANTHROPIC_API_KEY)
      echo "  fly secrets set ANTHROPIC_API_KEY=<paste the key> -a $APP"
      echo "      (from https://console.anthropic.com → API keys; set a monthly limit there)";;
    ELEVENLABS_API_KEY)
      echo "  fly secrets set ELEVENLABS_API_KEY=<paste the key> -a $APP"
      echo "      (narration voice; you can also paste it later under Settings → Narration in the panel)";;
  esac
}

if [ "${#missing_required[@]}" -gt 0 ]; then
  echo "These secrets are still missing. Run each line in this Terminal (paste the key where it says so,"
  echo "never anywhere else), then run this script again:"
  echo
  for name in "${missing_required[@]}"; do print_secret_line "$name"; done
  for name in "${missing_optional[@]}"; do print_secret_line "$name"; done
  echo
  echo "Nothing was deployed: the service refuses to start without LT_SECRET and LT_ADMIN_EMAIL, and"
  echo "without ANTHROPIC_API_KEY no lecture can be written."
  exit 1
fi
echo "all required secrets are set (${REQUIRED_SECRETS[*]})"
if [ "${#missing_optional[@]}" -gt 0 ]; then
  echo "optional, not set yet:"
  for name in "${missing_optional[@]}"; do print_secret_line "$name"; done
fi

# ---- 5. deploy -----------------------------------------------------------------------------------
step "deploying $APP (one machine, performance-2x, 8 GB, $REGION)"
cd "$ROOT"
fly deploy --app "$APP" --config "$ROOT/fly.toml" --ha=false --yes

# ---- 6. where it is and what to do first ---------------------------------------------------------
host="$(fly status -a "$APP" --json 2>/dev/null | python3 -c 'import json,sys; print((json.load(sys.stdin) or {}).get("Hostname") or "")' 2>/dev/null || true)"
url="https://${host:-$APP.fly.dev}"
cat <<MSG

== done: $url

First steps
  1. Open $url in your browser.
  2. Sign up with your admin e-mail (the LT_ADMIN_EMAIL you set) and a password of at least 8 characters.
     That account is the admin; nobody else can sign up without an invite.
  3. Invite a friend: Settings → People → "Invite" gives you a code; send it to them, they sign up with it.
  4. Settings → People also sets each person's monthly Claude budget (default 20 USD).

Later
  update the service:  bash scripts/deploy.sh $APP
  watch the logs:      fly logs -a $APP
  get the data back:   fly ssh console -a $APP      (your data is under /data)
MSG
