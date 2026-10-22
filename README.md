# lecture-tool

Turns an ETH lecture-script or slide PDF (EN or DE) into a **compact written lecture** at a chosen compactness ratio and output language — with a coverage ledger and an independent audit so nothing is silently dropped — plus an optional one-page summary and a **narrated video** with chapters and subtitles.

Runs through **Claude Code on your Mac**, on your subscription. No API key.

```
PDF ──ingest.py──▶ page images ──Claude──▶ transcript.md ──Claude──▶ ledger.json
                                                                        │
         lecture.md ◀──Claude (ratio, language)──◀ research.md ◀──Claude + web──┘
              │
         audit.md  ◀── independent subagent checks every ledger item is present
              │
         scenes.json ──render.py (Pillow slides + say/ElevenLabs + ffmpeg)──▶ lecture.mp4 + .srt + chapters
```

## Give it to a friend — and keep them updated

The tool is shared through a private GitHub repository that holds only the tool (scripts, panel,
skill, installer) — never your lectures, your keys or the Python environment.

**You, once:** `bash scripts/publish.sh "first version"` creates `~/Documents/UNI/lecture-tool-dist`
with its own git repository, creates the GitHub repository `lecture-tool` under your account
(needs `gh auth login`) and pushes. The repository is public: anyone with the link can install.

**Your friend, once** — one line in Terminal (installs Homebrew and git if missing, fetches the tool into
`~/lecture-tool`, runs the installer; no GitHub account needed):

```bash
curl -fsSL https://raw.githubusercontent.com/fpv-mp4/lecture-tool/main/get.sh | bash
```

Or by hand: `git clone https://github.com/fpv-mp4/lecture-tool.git ~/lecture-tool && cd ~/lecture-tool && bash install.sh`
(needs git, which comes with Homebrew or `xcode-select --install`). Send them `docs/Lecture-Tool-Install.md`.

The installer sets up ffmpeg, Python, Claude Code (sign-in with *their* Claude subscription) and the
panel as a login service at http://localhost:8765. An ElevenLabs key is optional (Settings → Narration).

**Every update:** you run `bash scripts/publish.sh "what changed"` (or press *Publish update* in the
panel's Settings). Their panel shows *Update to x.y.z* in Settings → one click pulls the new version,
refreshes the Python packages and restarts the service.

## One-time setup (Mac)

1. **Python 3.12** — `python3 --version`. If it's older than 3.10: `brew install python@3.12`
   (no Homebrew? install it from https://brew.sh first).
2. **pymupdf** — `pip3 install pymupdf`
3. **Claude Code** — installed and logged in with your Claude account (`claude --version` works).
   If not: follow https://docs.claude.com/en/docs/claude-code/overview.
4. Put this folder somewhere, e.g. `~/Lectures/lecture-tool/`.
5. Check: `cd ~/Lectures/lecture-tool && python3 scripts/check_env.py` — milestone 1 needs only the first three ticks.

Video additionally needs `brew install ffmpeg` and `pip3 install pillow fonttools brotli` (brotli lets the renderer read the woff2 brand fonts; without it every non-Latin symbol would come from the fallback fonts). Narration defaults to **ElevenLabs**: open any job's **Settings** and paste your ElevenLabs API key under *Narration*; it is stored in `.env` in this folder (git-ignored, mode 600) and a voice picker lists your account's voices. Without a key the renderer falls back to the macOS `say` voices (Samantha/Daniel for English, Anna for German). No LaTeX, no manim.

ElevenLabs bills per character: a 60-minute lesson video is about 50,000 characters, i.e. ≈ 25,000 credits with the default Turbo v2.5 model (think-pauses are silent and free; a 13-minute video is ≈ 9,500 credits) (set `ELEVENLABS_MODEL=eleven_multilingual_v2` in `.env` for the premium model at double cost). The free tier has 10,000 credits a month. The renderer checks your remaining credits before it starts, keeps every scene's audio it already made, and continues where it stopped on the next *re-render video*.

For the one-click **Run** button the `claude` CLI must be logged in: run `claude login` once in a terminal.

## Daily use

Install the panel once as a login service — it then runs in the background whenever the Mac is on, no terminal and no Claude app needed:
```
bash scripts/install_service.sh
```
Open **http://localhost:8765** (bookmark it, or in Chrome ⋮ → *Cast, save and share* → *Install page as app* for its own window and Dock icon). `bash scripts/install_service.sh --status` shows whether it is running, `--remove` uninstalls it; the log is `~/Library/Logs/lecture-tool.log`. After you edit `scripts/serve.py`, run the install script again to restart it.

Without the service, `python3 scripts/serve.py` in a terminal does the same while the terminal is open.

The pipeline runs through the `claude` command-line tool, which must be signed in once (`claude auth login`); the Claude desktop app does not need to be open.

Then:
1. In the panel: **New lecture** → drop one or several files (PDF, photos or screenshots of notes, Markdown/text; e.g. the lecture script plus the slides plus an exercise sheet — they become **one lesson**: notes and slides on the same topic are merged section by section, and each exercise lands at the end of the section it belongs to, with its solution from the sheet or a worked solution marked `[new]`; leftovers go into a final Exercises section. Pages are numbered continuously across the sources), fill course (`informatik-1`) and chapter (`06-functions`), pick output (summary / lecture / both), language, research scope, video on/off and the compression slider. **Instructions** is a free-text field the pipeline reads first: "only chapters 3 and 4" limits a big script to those pages, "focus on the exercises" or "exam-style" shifts the emphasis, "skip the appendix" drops it. Then **Create & run**.
2. The panel starts the pipeline itself (`claude -p "/lecture jobs/…"` in the background; its output streams into the **Log** tab). If you prefer, copy the command shown in the status line and paste it into an interactive Claude Code session instead.
3. Watch the stepper and the progress bar; while a run is going, its live output is shown in place of the result. When it reaches **done**, read **Summary**, **Lecture** (with table of contents and page markers) and **Video** (player with chapter list and subtitles; ← → skip 10 s, J/L 30 s, space pauses, C toggles subtitles; download MP4/SRT). **Details** next to the tabs unrolls **Dropped**, **Research** and **Audit**. Subtitles are off until you press the toggle (or C). Everything else the pipeline produces (transcript, ledger, logs) stays in the job folder.
4. **Cost**: the model you pick is the *writing* model (lecture, summary, storyboard, audit fixes). Transcription, fact extraction, research and the storyboard structure always run on cheaper models in parallel subagents, and identical source files are never read twice. Opus for writing roughly doubles the cost of a lecture versus Sonnet; it pays off for dense maths and for exam prep.
5. Several jobs can run at the same time: create or re-run the next one while the first is working; the sidebar footer shows how many are running. Each run is its own `claude` process, so only your Claude and ElevenLabs limits bound the parallelism.
5. **Model** picks which Claude runs the pipeline: Sonnet 5.5 (fast, default) or Opus 5.5 (best quality). The progress bar shows elapsed time, an estimate of the time left and the expected finish time.
6. The title is optional: left empty, it is read from the PDF (largest headings on the first pages) and refined by the pipeline. In the video player, ← → skip 10 s, J/L 30 s, space pauses, C toggles subtitles.
7. **Settings** (top right) changes compression, output, language, model or video for an existing job; **Re-run → lecture only** rewrites from stage 5 without re-reading the PDF; **video only** redoes stages 7–8 (the storyboard is written as *beats*: each spoken sentence names what to highlight — a bullet, code lines, a term of a formula, memory cells — and ElevenLabs' word timestamps keep the highlight in sync with the voice); **re-render video** redraws slides and narration from the existing scenes.json without Claude (use it after changing the voice).

Each job is a folder under `jobs/<course>/<chapter>/` — everything the pipeline produces is a plain file you can open, diff or edit.

## Compactness ratios

| ratio | what you get |
|---|---|
| `1:1` | everything, original wording (translated if needed) |
| `lossless` | everything, but explained simpler and shorter (~50–60 % of the original) |
| `1:1.5` | everything; asides reduced to one sentence |
| `1:2` | core + supporting items; asides dropped and listed in `dropped.md` |
| `1:3` | core + supporting only where needed; one example per rule |
| `1:4` | core only |
| `1:6` | cheat sheet: core only, one listing per section |

**Output** chooses what gets written: `summary.md` (a 2–4 page study sheet: key statements, every formula with its symbols, a worked mini-example and typical mistakes per section, a formula table, a glossary and answered check-yourself questions), the full `lecture.md`, or both. The video is always the full lesson, whatever is chosen.

**Exercise sheets** (Serien, Aufgabenblätter, past exams) dropped together with the script are solved completely: each exercise sits at the end of the matching section with a worked solution (given/wanted, idea, every step with units, boxed result, check), and the video walks through it after a think-pause. Exercises the sheet marks as optional or bonus are listed with a hint only, unless the job's notes ask for them.

**Video length follows the content**: a chapter is usually 25–45 minutes, a chapter plus an exercise sheet 45–75 minutes. Every rule is explained, every example and exercise worked on screen, with pauses to try it yourself.

`lossless` is the default and the one to use for exam prep.

## Markers in lecture.md

- `[p.74]` — from the PDF, that page
- `[external: cppreference.com]` — researched to fill a gap; source in `research.md`
- `[new]` — example or formula Claude added for understanding
- *stack frame (Stapelrahmen)* — technical term in both languages on first use

## A job that already exists

Creating a job with a course/chapter that exists is refused — delete it from the panel first or use another chapter name. To re-run only the writing stage on an existing job (e.g. a different ratio), use **Settings** + **Re-run → lecture only** in the panel, or tell Claude Code: `/lecture jobs/<course>/<chapter> — rerun from stage 5` (`— rerun from stage 7` for video only).

## Figures

Diagrams, plots, structures and photos are never redrawn: the lecture embeds the original page image under the figure's description (click it to see it full size), and the video shows a crop of the same page as a *figure* scene, with the narration highlighting or zooming into the part being explained. Only stack/memory diagrams are drawn by the tool itself.

## Converting to a product

`docs/SAAS-HANDOFF.md` describes what exists, every AI tool and service in use, the planned concept change (Study AI) and a SaaS architecture; `docs/SAAS-KICKOFF-PROMPT.md` is the first message for the session that builds it.

## Files

```
.claude/skills/lecture/SKILL.md   the pipeline Claude Code follows
scripts/ingest.py                 PDF → page PNGs + text layer
scripts/status.py                 progress file the UI reads
scripts/render.py                 scenes.json → lecture.mp4 (+ .srt, chapters.json)
scripts/serve.py                  local control panel + pipeline runner (stdlib only)
scripts/check_env.py              prerequisite check
scripts/install_service.sh        run the panel as a macOS login service
ui/index.html                     the panel (SPRTK theme, fonts in ui/fonts)
jobs/                             one folder per job
templates/manim/                  (milestone 2) animation templates
```

