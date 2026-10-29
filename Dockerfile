# syntax=docker/dockerfile:1
# Lecture Tool — hosted image (docs/HOSTED.md §8). Built and deployed by `bash scripts/deploy.sh`.
#
# The same scripts/serve.py, ui/ and .claude/skills/lecture as on the Mac, on Debian: Python 3.12,
# ffmpeg, the Claude Code CLI (Node 22), DejaVu/Noto as glyph-fallback fonts. Personal data (jobs,
# users, keys, the CLI's state) lives on the Fly volume at /data, never in the image.
# The context is allow-listed in .dockerignore: only scripts/, ui/, .claude/skills/, VERSION, README.md.

ARG CLAUDE_CODE_VERSION=2.1.312

FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm
ARG CLAUDE_CODE_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# ffmpeg/ffprobe for the renderer; fonts for glyph fallback (render.py _FALLBACK); procps for the
# `ps` checks in serve.py, lsof for its port check; perl provides `shasum` (the skill runs
# `shasum -a 256` to reuse transcripts); git/curl/ca-certificates for the CLI and web research.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg fonts-dejavu-core fonts-noto-core curl git ca-certificates procps lsof perl \
 && rm -rf /var/lib/apt/lists/* \
 && command -v ffmpeg ffprobe ps lsof shasum setpriv >/dev/null \
 && shasum -a 256 /dev/null >/dev/null \
 && test -f /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf \
 && test -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf

# Node 22 from the official image (runtime only, no build tools), then the Claude Code CLI pinned
# to one version so a redeploy never changes the pipeline's behaviour behind our back.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && claude --version

# Python libraries: ingest (PyMuPDF), slides (Pillow with WOFF2 via brotli), font cmaps (fontTools).
RUN pip install pymupdf pillow fonttools brotli

# Non-root user with a writable HOME (the CLI keeps small files there); /data is the volume.
RUN useradd --create-home --uid 1000 --shell /bin/bash app \
 && mkdir -p /app /data \
 && chown app:app /app /data

WORKDIR /app
COPY --chown=app:app scripts/ scripts/
COPY --chown=app:app ui/ ui/
COPY --chown=app:app .claude/skills/ .claude/skills/
COPY --chown=app:app VERSION README.md ./

# Build-time self-check: the brand WOFF2 fonts must open (FreeType with brotli), the scripts must
# compile, and PyMuPDF must import. A broken image fails here, not at the first render.
RUN python3 -c "from PIL import ImageFont; ImageFont.truetype('ui/fonts/inter-500.woff2', 20); import pymupdf" \
 && python3 -m py_compile scripts/serve.py scripts/render.py scripts/ingest.py scripts/status.py \
 && rm -rf scripts/__pycache__

# Fly mounts the volume at /data owned by root. The container starts as root only to hand /data to
# `app` and then drops to that user with setpriv; serve.py and every `claude -p` run as `app`
# (the CLI refuses bypassPermissions as root).
COPY <<'ENTRY' /usr/local/bin/lt-entrypoint
#!/bin/sh
set -e
DATA="${LT_DATA:-/data}"
CFG="${CLAUDE_CONFIG_DIR:-$DATA/claude}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA/jobs" "$CFG"
  chown app:app "$DATA" "$DATA/jobs" "$CFG"
  exec setpriv --reuid=app --regid=app --init-groups -- "$@"
fi
exec "$@"
ENTRY
RUN chmod 755 /usr/local/bin/lt-entrypoint

ENV HOME=/home/app \
    CLAUDE_CONFIG_DIR=/data/claude \
    LT_HOSTED=1 \
    LT_DATA=/data \
    PORT=8765

EXPOSE 8765
ENTRYPOINT ["/usr/local/bin/lt-entrypoint"]
CMD ["python3", "scripts/serve.py", "--no-browser"]
