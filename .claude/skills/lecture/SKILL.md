---
name: lecture
description: Turn an ETH lecture-script or slide PDF (English or German) into a compact written lecture at a chosen compactness ratio and output language, with a ledger-based coverage guarantee and web research for gaps. Use when the user runs /lecture, says "run the lecture pipeline", or names a job folder under jobs/. With video=true it also writes a scene storyboard and renders a narrated MP4.
---

# /lecture — PDF → compact lecture with a coverage guarantee

You are the brain of this pipeline. Python scripts do the deterministic work (page images, status, later rendering); **you** do every step that needs understanding: transcription, ledger, research, writing, auditing.

**Invocation:** `/lecture <job_dir>` — e.g. `/lecture jobs/informatik-1/06-functions`.
If no job_dir is given, print the candidate folders under `jobs/*/*/` that have a `job.json` and stop without setting any status.

Read `<job_dir>/job.json` first. Fields:

| field | values | meaning |
|---|---|---|
| `sources` | list of `{file, name, kind}` | the input files, already converted to PDFs under `sources/` (`kind` = pdf, image or text). `pdf` / `original_pdf_name` duplicate `sources[0]` — ignore them |
| `theme` | `mix`, `moss`, `indigo`, `amber` | video look, read by render.py only — ignore |
| `title` | text | human title for the lecture (`title_auto: true` means it was guessed from the PDF — refine it in Stage 2) |
| `ratio` | `1:1`, `lossless`, `1:1.5`, `1:2`, `1:3`, `1:4`, `1:6` | compactness, see Stage 5 |
| `output` | `summary`, `lecture`, `both` | what to write in Stage 5: a one-page `summary.md`, the full `lecture.md`, or both (default `lecture`) |
| `output_lang` | `en`, `de` | language of lecture.md (source may be either) |
| `research_scope` | `trusted`, `open`, `both`, `none` | where you may look for gap-filling |
| `notes` | free text | the user's instructions for this job, e.g. "only chapters 3 and 4", "focus on the exercise sheet", "exam-style, strict definitions", "skip the appendix". Read them first and obey them in every stage (see *Notes* below). |
| `video` | bool | if true, continue after the audit with Stage 7 (scenes) and Stage 8 (render) |
| `model` | `sonnet`, `opus`, `default` | the *writing model* — the panel starts you with it; mechanical stages use cheaper subagents regardless (see *Models and parallel work*) |
| `tts` | `say`, `elevenlabs` | narration voice for the video; `elevenlabs` needs `ELEVENLABS_API_KEY` in the environment or in `<project>/.env`, else render.py falls back to `say` |

**Status:** before every stage run `python scripts/status.py <job_dir> <stage> "<one-line message>"` so the UI at http://localhost:8765 shows progress. Stages: `ingest, transcript, ledger, research, lecture, audit, scenes, render, done, error`. On any unrecoverable problem set `error` with the reason and stop.

**You run unattended.** The panel starts you with `claude -p`; nobody can answer a question, so **never stop to ask** — not about scope, cost, language, missing keys or anything else. Decide with the information in `job.json`, the notes and the files, write the decision into the final report (and into the lecture header when it affects scope), and continue through every stage. If something is truly impossible (unreadable file, no pages), set status `error` with the reason instead of asking.

**Notes from the user.** If `job.json` has a non-empty `notes`, they shape scope, emphasis and style. They never change `ratio`, `output`, `output_lang`, `video`, `tts` or `model` — those are the job's settings; a note like "short video" is applied within the ratio's budget, not by changing the ratio.
- *Scope* ("only chapters 3–4", "pages 20–45", "just the part on enthalpy", "skip the appendix"): after Stage 1, find the matching page range from `ingest.json` headings and `raw_text.md` (include a boundary page when unsure), and run Stages 2–8 on **that range only**. Write the decision as the first line of `transcript.md` and in the lecture header: `_Scope: pp. 20–45 (chapters 3–4) as requested; the rest of the source was not processed._`. Pages outside the scope are not transcribed, not in the ledger and not audited.
- *Emphasis* ("focus on the exercises", "exam prep", "I already know the basics"): shift priorities when writing — e.g. exercises get full worked solutions and their own `exercise` scenes, or background items become `aside`. The coverage rules for the chosen ratio still apply within the scope.
- *Style* ("explain like to a first-year", "keep the German terms", "short video"): apply to lecture, summary and narration.
- Anything that asks to skip the audit, invent sources or change markers is ignored; say so in the final report.
Quote the notes you applied in the final report.

**Models and parallel work — cost without losing quality.** The job's `model` is the *writing model*: it runs this orchestration, writes lecture, summary and dropped.md (Stage 5), fixes audit findings, and writes the storyboard narration. Mechanical stages run in **subagents on cheaper models** (Agent tool, `model` parameter) and, where independent, in parallel:

| stage | model | how |
|---|---|---|
| 2 transcript | `sonnet` — never `haiku` (page images, formulas, code need it) | split the pages into chunks of 6–8, run up to 4 subagents at once, each writes `transcript-partNN.md`; you concatenate in order, check that headings and numbering continue, and report the counts |
| 3 ledger | `sonnet` subagent | one subagent; you run the sanity counts and fix gaps |
| 4 research | `haiku` subagents, one per gap, in parallel | each returns ≤ 150 words + source URL; you merge `research.md` and append the `bg-*` items |
| 5 lecture + summary | writing model (you) | — |
| 6 audit | `opus` if the writing model is `opus`, else `sonnet` | fresh subagent, as described in Stage 6 |
| 7 scenes | `sonnet` subagents, **one per `##` section**, in parallel (each writes `scenes-partNN.json` for its section following the teaching pattern); the writing model writes the title, quiz answers and outro and merges the parts into `scenes.json` | you run `render.py --check`, check every section and exercise is covered, and fix problems |
| 8 render | script | — |

Rules: every subagent prompt is self-contained (paths, the rules for that stage copied from this file, the output file); you verify each output (counts, `--check`) before the next stage; the audit is never skipped or downgraded. **Reuse before reading:** before Stage 2, hash each source (`shasum -a 256`); if another job under `jobs/` has a source with the same hash, a finished `transcript.md` and transcribed that source **in full** (same page range), copy that source's part of the transcript instead of re-reading it, renumber every `p.N` in it by this job's page offset from `ingest.json`, and say so in the status message and the transcript header.

**Never skip a stage, never merge stages, never guess content you did not read.** Every stage writes a file; the next stage reads that file, not your memory of it.

**Final report** (your last message, 6–10 lines, no questions in it): scope and the notes applied or ignored; item counts (sources / external / new); audit rounds and result; summary length; for video: scenes, minutes, think-pause minutes, credits used; open warnings (blank sources, unclear pages, optional exercises skipped). Intermediate files `transcript-partNN.md` and `scenes-partNN.json` are deleted after the merge.

---

## Stage 1 — Ingest

```
python scripts/status.py <job_dir> ingest "rendering pages"
python scripts/ingest.py <job_dir>
```

Produces `pages/page-NNN.png`, `raw_text.md`, `ingest.json`. Read `ingest.json` for the page count, the `sources` list (each with `first_page`–`last_page`) and detected headings. Pages are numbered **continuously across all sources**, so `[p.N]` markers stay unambiguous; `ingest.json` tells you which source a page belongs to. If `likely_scanned_pages` > 0, those pages exist only as images — fine, Stage 2 reads images anyway.

**Long documents** are split only inside Stage 2 (page chunks per transcript subagent) and Stage 7 (one storyboard subagent per section) — see *Models and parallel work*. Ledger, research, lecture, summary and audit always work on the whole document.

**Blank or image-only sources:** a source whose pages are blank (no text layer and nothing drawn — e.g. an empty notes file) is named in the transcript header and the lecture's `_Sources:_` line as "empty, skipped" and contributes no ledger items. Set `error` only if every source is blank. Image-only pages (`likely_scanned_pages`) are read as images like any other page.

## Stage 2 — Transcript

`python scripts/status.py <job_dir> transcript "transcribing N pages"`

Every page is read as an image: each transcript subagent reads its chunk's page images with the Read tool (4–6 pages per call) and writes `transcript-partNN.md`; you concatenate the parts in order into `transcript.md`, a faithful Markdown rendering of the document, and delete the parts.

Rules:
- **Several sources** (lecture notes + slides + exercise sheet …): start each source's pages with a top-level heading `# Source k — <name>` and transcribe each in order. Do not merge them here; merging happens in the ledger (one item per fact, `page` = first occurrence, other occurrences mentioned in `content`), and the lecture header lists the sources with their page ranges. Exercise sheets are transcribed exercise by exercise (`### Exercise 3`), and they become `exercise` ledger items.
- Headings → Markdown headings with the original numbering (`## 6.3 Evaluation of Function Calls`).
- Every formula → LaTeX (`$...$` inline, `$$...$$` display), using only KaTeX-supported commands (see Stage 5). Transcribe exactly; do not simplify. **One equation number per display block**: a `$$…$$` block carries at most one `\tag{}`; two numbered equations are two blocks, even when the source joins them with ⇒ or a comma (write the ⇒ as text between them).
- Every code listing → fenced block with the language and the original caption (`Code 6.1`, `Example 6.6`, …) as a line above it. Keep indentation and comments.
- Every figure/diagram → a block `> **Figure (p.N):** <precise description of what it shows, including every label and value visible>`. For memory/stack diagrams list every cell and value. These descriptions are what the video stage animates later, so be concrete.
- Footnotes → inline as `[^n]` with the text at the end of the section.
- Keep the source language here. Translation happens in Stage 5.
- Mark anything unreadable as `[unclear p.N: …]` rather than guessing.
- Use `raw_text.md` only to double-check spelling of identifiers; it is not the source of truth.

Before moving on, state in one line how many headings / formulas / listings / figures you transcribed. The next stage checks against those counts.

**Auto title:** if `job.json` has `"title_auto": true` (the user left the title empty), replace `title` in `job.json` now with a proper human title taken from the transcript — course or document name plus the section number and heading, e.g. `Informatik I — Section 6: Functions` — and set `title_auto` to false. Keep the user's title untouched otherwise.

## Stage 3 — Ledger

`python scripts/status.py <job_dir> ledger "extracting items"`

Read `transcript.md` and write `ledger.json`: a JSON array, one record per **atomic item**. This is the contract that makes "no details lost" checkable, so err on the side of more, smaller items.

```json
{
  "id": "6.3-eval-steps",
  "type": "rule",
  "page": 74,
  "section": "6.3",
  "title": "Evaluation of a function call",
  "content": "1. evaluate arguments 2. initialize formal parameters 3. execute body 4. return expr 5. value of the call",
  "priority": "core",
  "depends_on": ["6.1-def-syntax", "6.2-call-syntax"],
  "prereqs": ["R-value", "statement block"],
  "source": "pdf"
}
```

- `type` ∈ `definition, rule, formula, theorem, proof_step, example, code_listing, figure, remark, warning, exercise, background`. An `exercise` item holds the full task in `content` and, only when the source itself gives one (answer key, annotated sheet, worked solution), that solution in a `solution` field — never solve in Stage 3; Stage 5 solves and marks `[new]`; `section` is the lecture section it belongs to (decide by topic, e.g. an exercise on enthalpy belongs to the enthalpy section).
- `priority`: `core` (exam-relevant concept, rule, main example) / `supporting` (second example, derivation detail, diagram that illustrates a core item) / `aside` (footnote, historical note, "by the way")
- `prereqs`: concepts the item uses without explaining them (Stage 4 uses this to decide what needs research).
- `source`: `pdf` for everything in this stage.
- ids: `<section>-<slug>`, stable and unique.

Sanity check: every heading, numbered example, code listing and figure from the transcript must map to at least one ledger item. Count them and say so.

## Stage 4 — Gap check + research

`python scripts/status.py <job_dir> research "checking prerequisites"`

Collect all `prereqs` from the ledger. For each one decide:
- **covered** — explained somewhere in this PDF → nothing to do
- **assumed-known** — standard material from an earlier chapter or school (e.g. "variable", "loop", "real number") → list it in `research.md` under *Assumed known*, no research
- **gap** — the PDF uses it without explaining it and a student at this point could plausibly not know it → research it

Research scope from `job.json`:
- `trusted`: official course material, cppreference.com, en/de.wikipedia.org, standard textbooks, MIT OCW, university lecture notes
- `open`: any reputable source
- `both`: start trusted, widen if trusted sources don't settle it
- `none`: skip research, just list gaps

For each gap: WebSearch, read enough to explain it in ≤ 150 words at this course's level, and **append a ledger item** with `type: "background"`, `source: "<URL>"`, `priority: "supporting"`, id `bg-<slug>`. Write `research.md`:

```
# Research — <title>
## Assumed known (not researched)
- R-value — used on p.74, standard from Section 2
## Gaps filled
### Stack frame
Used on p.76 without definition. <explanation>. Source: <URL>
```

Everything from this stage is tagged `[external]` later. Never let external content change what the PDF says; it only adds context.

## Stage 5 — Lecture

`python scripts/status.py <job_dir> lecture "writing lecture (ratio X, lang Y)"`

Write `lecture.md` in `output_lang` from the ledger (not from the transcript — the ledger is the checklist). Walk the ledger in the main source's section order, section by section; items from other sources join the section they belong to.

**Ratio rules — these decide which items appear:**

| ratio | core | supporting | aside | wording |
|---|---|---|---|---|
| `1:1` | all | all | all | original wording, translated if needed; every example and listing verbatim |
| `lossless` | all | all | all | **simplified and shortened**, but every fact, formula, rule, example and listing is still present; target ≈ 50–60 % of the transcript's length for the text derived from the sources (exercise blocks with worked solutions and `[new]`/`[external]` items come on top) |
| `1:1.5` | all | all | kept, but each reduced to one sentence | simplified; target ≈ 45 % |
| `1:2` | all | merged where two items say the same thing | dropped | simplified; target ≈ 40 % |
| `1:3` | all | only where a core item is unclear without it; at most one example per rule | dropped | tight; target ≈ 30 % |
| `1:4` | all | dropped unless a core item is unintelligible without it | dropped | tight; target ≈ 20 % |
| `1:6` | all, as one-line statements | dropped | dropped | cheat sheet; at most one code listing per section; target ≈ 12 % |

The audit (Stage 6) checks the priorities that must appear: `1:1`, `lossless`, `1:1.5` → all; `1:2`, `1:3` → core + supporting (at `1:3` a supporting item counts as present if its fact is folded into the core item); `1:4`, `1:6` → core.

**Mandatory markers:**
- Every item from the PDF carries its page: `[p.74]` at the end of the paragraph or caption.
- Background items carry `[external: <domain>]`.
- Examples/formulas you invent to aid understanding carry `[new]`. Add them when a rule has no example at the chosen ratio or a formula benefits from one more instance. Keep them short and correct; check code compiles in your head.
- Terminology: when output is English and the source is German (or vice versa), give the other language's technical term in brackets on first use: *stack frame (Stapelrahmen)*. Everyday words don't need this.
- Figures: give a short description, then embed the original page so the reader sees the real diagram: `![Figure (p.14)](pages/page-014.png)` — the panel shows it inline and the video uses the same page for a `figure` scene (page file numbers are continuous across sources; take them from `ingest.json`). Do this for every diagram, plot, structure or photo the lecture relies on; skip purely decorative images.
- Code stays as code. Formulas stay LaTeX — but only KaTeX-supported LaTeX (https://katex.org/docs/supported.html), because the panel renders with KaTeX: `\frac`, `\sum`, `\int`, `\mathrm`, `\text`, `\Delta`, `\rightarrow`, `\begin{aligned}`, `\boxed`, `\cancel`, `\underset` are fine; no `\usepackage`, no `\label`/`\ref`, no `\begin{equation}` (use `$$…$$`), no `\textbf` inside math (use `\mathbf`), no custom macros. Chemistry: `\ce{2H2 + O2 -> 2H2O}` and `\pu{25 °C}` are supported (mhchem). Units via `\,\mathrm{kJ\,mol^{-1}}`. Never mix markdown emphasis into a formula. At most one `\tag{}` per `$$…$$` block — KaTeX refuses a second one and shows the raw source; split chained equations into separate blocks.

**Dropped items:** write `dropped.md` listing every ledger item **not** present in `lecture.md`, with id, title, page and the reason (`aside at ratio 1:2`, `merged into 6.4-stack`). At `1:1`, `lossless` and `1:1.5` this file must say "Nothing dropped."

Structure of `lecture.md`:

```
# <title> — compact lecture (<ratio>, <lang>)
_Sources: <name 1> (pp. 1–28), <name 2> (pp. 29–34). Items: N from the sources, M external, K new._
_Scope: <what the notes limited it to, or "whole document">. Notes applied: <quoted>, ignored: <quoted, why>._

## 6.1 <heading>
...
```

**Several sources — how they combine.** The lecture is one continuous text, not one part per file: lecture notes, slides and textbook pages on the same topic are merged section by section (one fact once, markers from every source that states it, e.g. `[p.12] [p.41]`). Exercise sheets, problem sets and past exams become **Exercise** blocks:
- An exercise whose topic matches a lecture section is placed **at the end of that section** under its own `###` heading that carries the source's numbering and the exercise's title — `### Serie 1, Aufgabe 3: Bremsen am Zebrastreifen` (or `### Sheet 4, Problem 2: …`) — so every exercise appears in the table of contents and the reader can jump to it. Under the heading: the task verbatim (translated if needed), then a **full worked solution** under `**Solution**` — all labels in `output_lang` (German: Lösung, Gegeben, Gesucht, Idee, Schritte, Ergebnis, Probe).
- **A solution is a walkthrough, not an answer key.** Every exercise and every sub-part gets: *Given / Wanted* (one line each, with symbols and units), *Idea* (which rule or formula from the lecture applies and why), *Steps* — a numbered list where each step shows the formula, then the numbers plugged in, then the intermediate result with units; *Result* (boxed as `**Result:** …`, with the right number of significant figures) and *Check* (dimension check, order of magnitude, limiting case or a second route). Where the source gives its own solution, use its route and numbers and fill in the reasoning it skips; where it gives none, solve it yourself and mark the solution `[new]`. Name the typical mistake for this kind of task in one sentence when there is one. Multiple-choice: say why the wrong options are wrong. Proof-type tasks: every step names the rule it uses.
- **Optional exercises stay optional.** An exercise the sheet marks as optional, bonus, Zusatzaufgabe, freiwillig, challenge, starred (*) or "für Interessierte" is listed with its task and a two-sentence hint (which rule applies, what the result should look like), labelled `**Optional — not solved**`, and gets no `exercise` scene in the video. Solve it in full only when the notes ask for it ("solve the optional ones too").
- Solve **all other** exercises of a submitted sheet, including ones whose theory is not in the lecture part of the sources: give the missing rule in two or three sentences as a `[new]` or `[external]` item first, then solve.
- Exercises that fit no section go into a final `## Exercises` section before "Check yourself", in the source's order, each under its own `###` heading as above.
- Multiple-choice and true/false items keep their options; the correct one is stated after.
- A source that is a pure exercise sheet (no theory) therefore adds no sections of its own; its content is distributed into the lecture.
The video (Stage 7) walks through every solved exercise with an `exercise` scene (task → think-pause → solution steps → result) at `1:1`–`1:2`; at `1:3` and below, and for optional exercises, the ratio table in Stage 7 applies.

End with a **"Check yourself"** section: 5–10 short questions that cover the core items (no answers needed; they're prompts for self-test).

**Summary (`output` = `summary` or `both`):** also write `summary.md` in `output_lang`. It is a **study sheet the student can revise from without the lecture**, not a paragraph of bullets. Build it from the ledger's `core` and `supporting` items, in the lecture's order:

```
# <title> — Zusammenfassung / Summary
_<one line: what this chapter is about and what you must be able to do afterwards>_

## <section number and heading>        ← one block per `##` section of the lecture
**Kernaussagen** — 3–6 bullets, each a complete statement with its page marker
**Formeln** — every formula a student must know, as display LaTeX, each followed by one line naming every symbol and its unit, and when it applies
**Beispiel** — one worked mini-example in 3–5 lines when the section has rules that are applied to numbers or code (take it from the lecture; shorten, don't invent)
**Typische Fehler / Prüfungstipp** — 1–3 bullets

## Formelsammlung                       ← table: formula · meaning · conditions · page
## Begriffe                              ← glossary table: term (with the other language's term) · one-line definition · page
## Check yourself                        ← the same questions as the lecture, now each followed by a one-line answer
```

Length follows the content: roughly 20–30 % of the lecture's word count, usually 2–4 pages (at least 600 words for a normal chapter; never under 300). Every `core` item must be recognisable in it — the audit checks that. If `output` = `summary`, still produce `lecture.md` internally at the chosen ratio (the audit needs it), but tell the user the summary is the deliverable.

## Stage 6 — Coverage audit (independent)

`python scripts/status.py <job_dir> audit "auditing coverage"`

Spawn a **subagent** (Agent tool, general-purpose) that has not seen your Stage 5 work, with this prompt — substitute the paths:

> Read `<job_dir>/ledger.json` and `<job_dir>/lecture.md` and `<job_dir>/dropped.md`. The ratio is `<ratio>`. For every ledger item whose priority must appear at this ratio (1:1, lossless and 1:1.5: all; 1:2 and 1:3: core+supporting; 1:4 and 1:6: core), check whether its content — the actual fact, formula, rule, example or code, not just its title — is present in lecture.md. Also check every item listed in dropped.md is really absent and the reason matches the ratio rules. Write `<job_dir>/audit.md` with a header line `RESULT: PASS` or `RESULT: FAIL`, then a table of missing items (id, title, page, what exactly is missing), then a list of items present but altered in meaning. Be strict: a paraphrase that loses a condition or a number counts as missing. If `<job_dir>/summary.md` exists, add a section `## Summary` with a table of every `core` ledger item whose content (fact, formula or rule) is not recognisable in summary.md, and check every worked exercise solution in lecture.md for arithmetic and unit errors and for the required structure (given/wanted, idea, steps, result, check); list problems under `## Solutions`. All three tables count towards FAIL.

Read `audit.md`. If `FAIL`: fix `lecture.md` for exactly the listed items (do not rewrite the whole file), update `dropped.md`, and re-run the audit with a fresh subagent. Max 3 full rounds. If round 3 still fails, fix its items too and run **one targeted audit** (fresh subagent, same model) that checks only those ids plus whether the fixes broke neighbouring text; on PASS continue as normal. Never leave fixes un-audited and never stop with fixable findings: only when the targeted audit still fails set status `error` with the remaining ids and what is wrong with them.

On `PASS`, if `video` is false:

```
python scripts/status.py <job_dir> done "lecture.md ready — <n> items, audit passed"
```

and tell the user in 3–4 lines: where the files are, item counts, how many external/new items, and anything you were unsure about. Don't paste the lecture. If `video` is true, continue with Stage 7 instead (the `done` status is set by render.py at the end).

## Stage 7 — Scenes (only if `video` is true)

`python scripts/status.py <job_dir> scenes "writing scenes"`

Write `scenes.json`: the storyboard for a narrated video, built from `lecture.md` (and `summary.md` if present), **not** from the transcript. The video is a **lesson a student can learn the topic from**, not a trailer for the text: every core and supporting item is explained (what it says, why it holds, where it is used), every example and every exercise is worked through on screen, and the viewer gets time to think. **The compression ratio sets the video's depth and length**, exactly as it sets the text's. Narration speaks ≈ 150 words/min; the budget is a multiple of `lecture.md`'s word count (which already shrinks with the ratio), and `render.py --check` fails above 115 % of it (it estimates at 160 words/min and adds the pauses):

| ratio | what the video does | narration ≤ | pauses | typical for one lecture week |
|---|---|---|---|---|
| `1:1` | full lesson: every item explained, every example and every exercise part worked, recap per section, every quiz answered | 2.6 × lecture words | yes, everywhere | 1½–2 h |
| `lossless` | same as 1:1 | 2.4 × | yes | 1½–2 h |
| `1:1.5` | everything explained; an exercise's sub-parts share one scene when the solution has ≤ 6 steps; recaps only at chapter ends | 2.0 × | yes | 1–1½ h |
| `1:2` | core + supporting explained; one worked example per rule; each exercise worked once along its main route, no sub-part scenes; quiz answers in one scene at the end | 1.6 × | before exercises and the final quiz | 40–60 min |
| `1:3` | core explained, supporting folded into the same beat; one example per section; exercises become a `quiz` with an answers scene, no walkthroughs | 1.3 × | final quiz only | 25–40 min |
| `1:4` | core only: definitions, rules, formulas, main examples; no exercises; final quiz | 1.1 × | final quiz only | 15–25 min |
| `1:6` | cheat-sheet video: one `section` scene plus one `bullets` or `formula` scene per section; final quiz | 1.0 × | none | 5–12 min |

Within the budget the length still follows the content — never pad, and never cut below the ratio's rules to hit a number. If `--check` reports the budget exceeded, shorten in this order and re-check: fold per-section recaps into the section's last `bullets` scene; merge an exercise's sub-part scenes into one; drop per-section quizzes (keep the final one); tighten narration wording. Never drop items.

**Teaching pattern per `##` section** at `1:1`–`1:2` (the order the brain likes: why → what → how → try → recap; at `1:3` and `1:4` steps 1–4 with one example per section and the final quiz; at `1:6` steps 1–2 and the final quiz):
1. `section` scene: one sentence on why this matters and what the viewer will be able to do.
2. The concept: `bullets` (definitions, rules) — one beat per item, each beat *explains* the item, it does not read it.
3. Every formula: a `formula` scene with one beat per term, then a beat that reads the whole and says when it applies and what its limits are.
4. Every worked example from the lecture: a `steps` scene (one beat per step, each beat says what is done and why) or a `code` scene walked in reading order; every figure the lecture explains: a `figure` scene from the original page.
5. Every exercise of that section: an `exercise` scene (task → think-pause → solution steps → result), see below. Long exercises with sub-parts get one `exercise` scene per sub-part.
6. Close the section with a `bullets` recap (2–4 items: what to remember) and, for sections with rules that get applied, a `quiz` scene with 2–3 questions, each followed by a think-pause and then a `bullets` scene "Antworten/Answers".
End of video: the "Check yourself" `quiz` (pauses after each question), then a `bullets` scene with the answers, then a final `section`-style outro scene naming what was covered.

**Think-pauses.** Any beat may carry `"pause": N` (seconds, 3–45): the words in `say` are spoken first, then the picture holds in silence for N seconds with a countdown ("Try it yourself"). Use them before every solution (8–20 s for a short calculation, 20–45 s for a real exercise part), after each quiz question (5–10 s) and after a "what do you expect?" beat. Pauses cost nothing in credits and are what makes the video a lesson.

**Cost.** Narration is the only metered part (ElevenLabs ≈ 0.5 credits per character on Turbo; a 60-minute lesson is ≈ 50 k characters ≈ 25 k credits; macOS `say` is free). Keep the narration dense and spoken, put thinking time into pauses rather than words, and never repeat the on-screen text verbatim. `render.py --estimate` prints the credits a storyboard needs before anything is synthesized.

```json
{"title": "<title>", "lang": "<output_lang>", "scenes": [
  {"id":"s01","type":"title",   "heading":"Functions","sub":"Informatik I · Section 6","narration":"..."},
  {"id":"s02","type":"section", "heading":"6.1 Defining Functions","sub":"one line","narration":"..."},
  {"id":"s03","type":"bullets", "heading":"...","bullets":["...","..."],"page":72,
     "beats":[{"say":"One or two sentences.","focus":{"item":1}}, {"say":"...","focus":{"item":2}}]},
  {"id":"s04","type":"steps",   "heading":"...","steps":["..."],"page":74,"beats":[{"say":"...","focus":{"item":1}}]},
  {"id":"s05","type":"code",    "heading":"Code 6.1","code":"...","caption":"...","page":72,
     "beats":[{"say":"...","focus":{"lines":[5,6]}}, {"say":"...","focus":{"lines":[13]}}]},
  {"id":"s06","type":"formula", "heading":"...","formula":"min(a₁, b₁) ≤ max(a₂, b₂)","caption":"...","page":87,
     "beats":[{"say":"...","focus":{"part":"min(a₁, b₁)"}}, {"say":"...","focus":{}}]},
  {"id":"s07","type":"memory",  "heading":"...","frames":[{"label":"pow","cells":[{"name":"b","value":"2.0"}]},{"label":"main","cells":[{"name":"z","value":"?"}]}],"caption":"...","page":77,
     "beats":[{"say":"...","focus":{"cells":["pow.b"]}}, {"say":"...","focus":{"cells":["main.z"],"frames":[{"label":"main","cells":[{"name":"z","value":"0.25"}]}]}}]},
  {"id":"s08","type":"quiz",    "heading":"Check yourself","questions":["..."],"beats":[{"say":"...","focus":{"item":1},"pause":8}]},
  {"id":"s09","type":"exercise","heading":"Serie 1, Aufgabe 3a","task":"Ein Auto bremst mit 6 m/s² aus 50 km/h. Bremsweg?","steps":["v₀ = 50 km/h = 13,9 m/s","v² = v₀² − 2as ⟹ s = v₀²/(2a)","s = 13,9² / (2 · 6) m = 16,1 m"],"result":"s ≈ 16 m","page":453,
     "beats":[{"say":"Here is the task.","focus":{"task":true},"pause":20}, {"say":"First convert the speed.","focus":{"item":1}}, {"say":"...","focus":{"item":2}}, {"say":"...","focus":{"item":3}}, {"say":"So the braking distance is about sixteen metres; the dimension check works out.","focus":{"result":true}}]}
]}
```

**Beats drive the video.** Every scene except `title` and `section` has `beats`: an ordered list of `{say, focus}`. `say` is one or two spoken sentences; the scene's narration is the beats joined, so do not add a separate `narration` to such scenes. `focus` says what the viewer should look at while that beat is spoken, and the renderer highlights exactly that, timed to the voice:
- `bullets` / `steps` / `quiz`: `{"item": k}` reveals item k and lights it; earlier items dim. Give every item its own beat, in order.
- `exercise`: `{"task": true}` shows the task alone (give that beat a `pause`), `{"item": k}` reveals solution step k, `{"result": true}` shows the boxed result. `task` ≤ 450 characters, steps ≤ 160 characters each (plain Unicode maths like `formula`), `result` one line.
- `code`: `{"lines": [a]}` or `{"lines": [a, b]}` lights those lines and dims the rest. Walk through the code in reading order; 3–6 beats per listing.
- `formula`: `{"part": "<exact substring>"}` lights that part of the formula; use one beat per term or operator you explain, and a final beat with `{}` to show the whole.
- `memory`: `{"cells": ["pow.b", "main.z"]}` points at cells (`label.name`); add `"frames": [...]` in a beat to change values mid-scene (e.g. after `b = 1.0/b`), so the diagram animates with the explanation.
- `figure`: shows the **original figure from the source** — a crop of `pages/page-NNN.png` (continuous page number). Use it for every diagram, plot, chemical structure, circuit, table, photo or hand drawing that the lecture explains; do **not** rebuild such figures from text or shapes (only stack/memory diagrams use `memory`). To set the crop, read the page image and give `crop: [x0, y0, x1, y1]` as fractions of the page width/height with ~3 % margin around the figure (a whole page is acceptable when the figure fills it). Beats guide the eye: `{"box": [x0, y0, x1, y1]}` highlights a region of the shown crop and dims the rest, `{"zoom": [...]}` shows only that region; fractions refer to the crop. A `caption` names what is shown and its page.
- `{}` (no focus) keeps the previous highlight.
- `title` and `section` scenes carry either `narration` or `beats`. Section subagents write ids `s<section>-<k>`; on merging you renumber them `s001…` in order (ids must be unique).

Rules:
- **Narration is spoken language** in `output_lang`: full sentences, no markdown, no code symbols. Say "b to the power of e", "min of a one and b one", "the function pow". Every beat must mention what it highlights.
- **Structure**: one `title` scene, then for every `##` section of the lecture the teaching pattern above; close with the "Check yourself" quiz, its answers and an outro. Every scene that shows PDF content carries `page`. At `1:1`–`1:2` nothing that is `core` or `supporting` in the ledger may be absent from the video; at `1:3` and below the ratio table decides.
- **bullets / steps**: 3–6 items, each ≤ 110 characters.
- **code**: verbatim from the lecture, ≤ 22 lines (trim `main` or comments if needed, never change statements); `caption` optional one-liner.
- **formula**: plain Unicode, never LaTeX — subscripts ₀₁₂ₙᵢ (or `x_1`), superscripts ², ≤ ≥ ≠ ∧ ∨ ¬ × → ⟶ ⌊ ⌋ √ ∞. Use `\n` for a second line.
- **memory**: one scene per memory figure worth showing (2–4 per chapter); `frames` left → right as in the PDF (innermost call first, `main` last); `color` `"lime"` or `"red"` optional.
- Scene length 60–150 narration words (hard max 220). No scene type other than the ten above (title, section, bullets, steps, code, formula, memory, quiz, figure, exercise). Do not read code aloud line by line; do not read bullets aloud — explain them.

Validate until it passes: `python scripts/render.py <job_dir> --check` prints the scene count and estimated length; it fails with a list of problems otherwise. Then compare the length with the content: at `1:1`–`1:2`, if a section of the lecture has fewer scenes than items, or a solved exercise has no `exercise` scene, the storyboard is incomplete — extend it, do not shorten the lecture.

## Stage 8 — Render

```
python scripts/render.py <job_dir>
```

The script sets the `render` status per scene and `done` at the end (`lecture.mp4`, `lecture.srt`, `chapters.json`, `render.log`, intermediate files in `render/`). It takes roughly a quarter of the video's length. Before rendering run `python scripts/render.py <job_dir> --estimate` and put its credit line into the status message. If it reports `not enough ElevenLabs credits`, or render.log ends with `quota_exceeded`: change nothing — audio already made is cached and *Redo → Refresh the video* resumes later; set status `error` with that message and end the report with it. Read the last lines of `render.log`; edit `scenes.json` only for validation errors (the message names the scene and the problem) and re-run. When the render finishes, render.py sets `done`.

**Rerun requests:** `/lecture <job_dir> — rerun from stage 5` keeps transcript, ledger and research and starts at Stage 5 with the current `job.json` (e.g. a changed ratio); `— rerun from stage 6` keeps the lecture text and starts with a fresh audit (used after an audit failure, once the text has been fixed); `— rerun from stage 7` keeps the lecture and only redoes scenes and render. If a rerun's inputs are missing (`transcript.md`, `ledger.json`, `lecture.md`), fall back to the earliest stage whose inputs exist, say so in the status message, and continue — never ask.

---

## Files a finished job contains

```
job.json        the request
status.json     progress for the UI
sources/        input files as PDFs (NN-<name>.pdf)
run.log, run.jsonl, run.pid   the panel's record of the headless run (not yours to touch)
pages/          page images
raw_text.md     text layer (reference)
ingest.json     page/heading inventory
transcript.md   faithful Markdown of the PDF
ledger.json     atomic items — the coverage contract
research.md     assumed-known list + gaps filled
lecture.md      the deliverable
summary.md      study sheet, 20–30 % of the lecture (only if output = summary | both)
dropped.md      what was left out and why
audit.md        independent coverage check
scenes.json     video storyboard (video only)
lecture.mp4     narrated video, lecture.srt subtitles, chapters.json (video only)
render.log      render output; render/ holds per-scene frames and audio
```
