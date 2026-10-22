# Audit — 22 Oct 2026

Four independent reviews (server + install scripts, renderer, panel, pipeline skill) plus static checks
(py_compile, pyflakes, `node --check` on the panel's script). Every confirmed bug below is fixed; the
"still open" lists are suggestions that were not done and are ordered by value.

## Fixed

### Renderer (`scripts/render.py`)
1. **Video track ended early in every scene.** The last frame sat exactly at the scene's end and
   `-t` cut it, so the picture stopped a few frames after the last beat while the audio ran on
   (29 minutes of missing video track in the Physik job; the final fade never rendered). Frames are
   now counted (`-frames:v`) instead.
2. `--silent` previews overwrote the cached narration with silence → separate `audio_silent.m4a`,
   and previews skip think-pauses.
3. Lists longer than the slide were cut off silently → `--check` simulates the layout and rejects
   overflowing bullets/steps/quiz/exercise scenes; the drawing loop stops before the footer.
4. `snake_case` identifiers in code were drawn as subscripts → the subscript rule only applies to
   `x_1`-style single characters.
5. `"say": null` crashed the validator → every `say` read is null-safe; non-text and line breaks
   are rejected (line breaks split subtitles).
6. `--check` passed storyboards that crashed in render (missing `id`, memory frames without
   `cells`, non-text headings/items, over-wide code lines) → all validated.
7. A worker's failure could be masked by its successor clearing the shared future → each worker
   keeps its own.
8. All last frames (6 MB each) were kept until the end (~1 GB on a 160-scene job), and frame PNGs
   stayed on disk (1.2 GB) → frames are deleted after the encode, nothing is retained.
9. Also: ElevenLabs network errors are retried with back-off and sockets closed; subtitle cues
   end with the spoken part; `--scene` reports a missing or unknown id instead of a traceback.

### Server (`scripts/serve.py`, service and installer)
1. Every rerun re-parsed the whole `run.jsonl` into `run.log` (31 MB, duplicated result lines) →
   the tailer starts at the file position of its own run.
2. Deleting a job that was still running orphaned the pipeline → 409 "stop it first".
3. Stop only signalled the leader; ffmpeg/say or Claude's children survived → the whole process
   group gets SIGTERM, then SIGKILL after 3 s.
4. After a server restart a finished run showed "claude exited with -1" → detached runs report an
   unknown exit, not a failure.
5. A new run started within a second of the old one could have its `run.pid` deleted by the old
   tailer → tailers only act on their own record and pid.
6. Exited children counted as alive until some request reaped them → owned processes are polled.
7. Reattach trusted a bare pid (a reused pid meant "running forever", Stop could kill a random
   process) → the pid's command line must be ours.
8. Any website open in the browser could start runs, stop them or overwrite the ElevenLabs key
   (CORS "simple requests") → POST/DELETE require the panel's own origin.
9. A manual `python3 scripts/serve.py` fought the login service (kill loop every 5 s) → the manual
   start defers to the service and opens the browser.
10. The launchd plist broke on paths containing `&`; launchctl errors were hidden → XML-escaped,
    errors shown; `--status` now checks that the port answers.
11. A stale `.venv` (Python upgraded) aborted the installer cryptically → recreated; `--check`
    verifies the venv's modules.
12. `Range: bytes=-` crashed; a bad render-only start left a bogus header in run.log → fixed.
    `.env` is created with mode 600 from the start and refuses multi-line values.

### Panel (`ui/index.html`)
1. Defaults stored by an older build (e.g. look "dark") broke the composer: no slider, "Make
   lecture" reloaded the page → stored defaults are validated against the current options.
2. The chapter → text jump's numeric fallback never ran (variable cleared too early).
3. ETA and "about 1 s left" were shown for failed and interrupted runs → only while running; a
   failed run shows the stage it failed in.
4. On narrow windows the sidebar could not be closed and stayed over the content after choosing a
   job.
5. Options → Save silently rewrote an old job's look to Mix → unknown looks are kept as a labelled
   option.
6. A slow video-tab load could overwrite a newer tab → stale-fetch guard also in the video branch.
7. Raw HTML in generated Markdown (an LLM reading an untrusted PDF) and `$…$` math placeholders
   were inserted unescaped → HTML is shown as text, math is escaped before KaTeX, `esc()` escapes
   quotes, inline `onclick` strings replaced by bound handlers.
8. A truncated `chapters.json` threw on every `timeupdate`.
9. Dark-theme leftovers (black card shadow, dialog shadow, blue external tag) tuned for white.
10. Also: an offline indicator with poll back-off; a job you watch finish is no longer badged
    "new"; video shortcuts ignore dialogs and buttons; notifications-blocked text explains how to
    allow them.

### Skill (`.claude/skills/lecture/SKILL.md`)
20 contradictions and gaps fixed, among them: "ask which job" vs "never ask"; translation stage
wrong; chunking rule that contradicted the per-stage split; reuse-by-hash without page renumbering;
exercise rules that contradicted the ratio table; budget wording that did not match `--check`;
credits running out mid-render (now: change nothing, cached audio resumes later); reruns with
missing inputs (fall back to the earliest possible stage, never ask); blank sources; labels in the
output language; a final-report template with no questions; the file list.

## Done after the reviews (23 Oct 2026)
- Text tabs refetch only when the file changed on disk (`mtimes` in `/api/job`), so the page keeps
  its scroll position while a run is still writing other files.
- Error stepper marks the stage that failed; earlier stages stay green.
- Failed ElevenLabs voice lookups are remembered for a minute; the request times out after 8 s.
- The pipeline child gets a small environment instead of the server's whole one.
- Every route answers `{"error": …}` with 500 on an unexpected exception.
- Job creation claims its folder atomically.
- Video: a playing video survives a tab or job switch in a floating dock (picture-in-picture in
  Chrome when the switch is a click); keyboard shortcuts are handled before the native controls,
  so the timeline can no longer add its own one-minute step on top.
- Composer: fixed-height readouts (text time, video length, ready time) to the right of the slider;
  the card no longer changes height between stops.

## Still open (suggestions, by value)

### Pipeline / quality
- Per-section narration allocation (section words × factor) so storyboard subagents stay within
  budget on their own.
- Audit check that every `[p.N]` lies inside the scope and page count.
- A "what changed" diff after Redo → Rewrite, so a second run shows which paragraphs moved.

### Renderer
- Split a long list scene automatically onto a continuation slide instead of failing `--check`.
- Treat `elevenlabs_quota()` failures explicitly (log; proceed as "unknown").
- Per-run `run-<timestamp>.jsonl` so job folders stop growing.
- A short intro card per exercise with the sheet's original figure when it has one.

### Panel
- Upload as `FormData` with a progress bar instead of base64 JSON (a 100 MB PDF becomes a 135 MB string).
- Accessibility: tabs and jobs as real buttons, labels bound to inputs, a focus ring for chip
  selects, `role="dialog"` with focus trap on Settings.
- Flip-card reading mode for the summary's glossary and formula table (parked earlier).
- Playback speed control and "resume where you left off" per video (localStorage).
- Search inside a lecture's text (the sidebar search only finds lectures).
- Export: lecture + summary as PDF, or the whole job as a zip.
