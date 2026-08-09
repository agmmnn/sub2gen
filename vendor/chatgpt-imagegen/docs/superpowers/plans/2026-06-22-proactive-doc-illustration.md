# Proactive Document Illustration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Edit `SKILL.md` so agents proactively propose and generate illustrations (via background subagents) when authoring long-form content, and know how to write good figure prompts.

**Architecture:** Pure documentation change to one file, `SKILL.md`. No code, no tests. Four edits: (A) frontmatter `description` proactive clause + version bump, (B) proactive "When to use" bullet, (C) a new "Illustrating documents" workflow section, (D) a "Writing figure prompts" subsection inside it. Verification is `grep` for the inserted anchors/phrases plus a manual read-through.

**Tech Stack:** Markdown / YAML front matter. No build, no tests.

**Spec:** `docs/superpowers/specs/2026-06-22-proactive-doc-illustration-design.md`

---

## File structure

- **Modify** `SKILL.md` only. Sections touched: the YAML front matter (`description`,
  `version`), the body "## When to use" section, and a new "## Illustrating documents"
  section inserted between the existing "## Workflow" and "## Limits" sections.

> No line numbers are pinned here because each edit uses a unique anchor string. Match on
> the anchor text shown, not on a line number.

---

## Task 1: Frontmatter — proactive trigger clause + version bump

**Files:**
- Modify: `SKILL.md` (YAML front matter)

- [ ] **Step 1: Bump the version**

Change the front-matter version line:

```yaml
version: "0.7.0"
```
to
```yaml
version: "0.11.0"
```

- [ ] **Step 2: Append the proactive-trigger clause to `description`**

The `description:` value currently ends with the sentence:

> `… writing code-native graphics (HTML/CSS/canvas), or extending an established repo icon system.`

Append (inside the same YAML string, right after that final `system.`, one space then):

> ` Also use proactively: when authoring a document, blog post, technical proposal, design doc, README, or other long-form explanatory content, propose illustrations for the key concepts and generate them as background tasks — don't wait to be asked for an image.`

Keep it on the same logical YAML line (the `description` is a single inline double-quoted
string — append before the closing `"`). Do not introduce a literal newline into the value.

- [ ] **Step 3: Verify**

Run:
```bash
grep -n 'version: "0.11.0"' SKILL.md
grep -c 'Also use proactively' SKILL.md
python3 - <<'PY'
import re, sys
src = open("SKILL.md").read()
# front matter is between the first two --- lines
fm = src.split("---", 2)[1]
assert 'version: "0.11.0"' in fm, "version not bumped in front matter"
assert "Also use proactively" in fm, "proactive clause not in front matter description"
print("frontmatter OK")
PY
```
Expected: the version grep matches, the clause count is `1`, and `frontmatter OK` prints
(this confirms the clause landed inside the front matter, not the body).

- [ ] **Step 4: Commit**

```bash
git add SKILL.md
git commit -m "docs(skill): 主动触发——description 加配图触发句 + version bump 0.11.0"
```

---

## Task 2: "When to use" — proactive bullet

**Files:**
- Modify: `SKILL.md` ("## When to use" section)

- [ ] **Step 1: Add a proactive bullet**

In the "## When to use" section, the last existing bullet is:

> `- The deliverable is intended to be saved into the repo or build inputs.`

Add immediately after it:

```markdown
- You're authoring long-form or explanatory content — a blog post, technical proposal, design doc, tutorial, postmortem, or README — and a figure would help a concept land. **You don't need to be asked**: propose the figures and generate them (see *[Illustrating documents](#illustrating-documents)* below).
```

- [ ] **Step 2: Verify**

Run:
```bash
grep -n "You're authoring long-form" SKILL.md
grep -n "## When to use" SKILL.md
```
Expected: the new bullet appears, located after "## When to use" and before "## When not to use".

- [ ] **Step 3: Commit**

```bash
git add SKILL.md
git commit -m "docs(skill): When to use 加主动配图 bullet"
```

---

## Task 3: New "Illustrating documents" workflow section

**Files:**
- Modify: `SKILL.md` (insert a new section between "## Workflow" and "## Limits")

- [ ] **Step 1: Insert the section**

Find the start of the "## Limits" section (the line `## Limits`). Insert the following
block **immediately before** the `## Limits` line (so the new section sits after
"## Workflow" and before "## Limits"):

```markdown
## Illustrating documents

When you're authoring a document, blog post, technical proposal, design doc, or other long-form explanatory content, **proactively illustrate the key concepts** — you don't need to be asked. The flow:

1. **Announce a brief plan first.** In one or two lines, say where figures will go and what each depicts (e.g. *"I'll add two figures: (1) the request→SSE flow, (2) the token-refresh path."*). Then generate — don't wait for approval; the plan is the reader's chance to redirect.
2. **Fan out background subagents — one per figure.** Each runs the CLI with `--quiet -o <path>` so stdout is just the saved path; keep writing the prose while they render, and embed each image when it lands. Spawn them as background tasks with your own agent/task tooling — one figure per task, never blocking the writing.
3. **Parallelism depends on the user's backend — don't override it.** Honour the user's `--backend` / `CHATGPT_IMAGEGEN_BACKEND` (default `auto`). On the **`web`** backend, concurrency is **1** — background figures **queue** and render one at a time (still fine: it's in the background, and it spends no Codex-usage). On **`codex`**, up to **4** render in parallel but each bills the metered Codex-usage bucket. Which backend to spend is the user's trade-off, not yours.
4. **Choose a style to fit the document's tone.** There's no default illustration style. For informal or blog-style explainers, the built-in **`doodle`** look fits well — deliberately crude, content-accurate (`--style doodle`). For polished specs, pick a cleaner look or a style you've defined (see [Styles](#styles)).
5. **Don't over-illustrate.** At most one figure per major concept; never decorate for its own sake; and **never loop generating "variants" of the same figure** — that just burns subscription quota. If a figure comes out wrong, change the prompt once and regenerate, don't spray.

### Writing figure prompts

A vague prompt yields a useless figure. Make the prompt describe the figure's **content**, not just name it:

- Spell out the **boxes, arrows, labels, layout, and relationships** — "an architecture diagram" is too vague; say *what's in it* and how the parts connect.
- **One subject, one concept** per figure. Split a busy diagram into two.
- **Name the style** you want explicitly in the prompt or via `--style`.
- For the **`doodle`** look, remember **content accuracy beats polish** — it's supposed to look crude and hand-drawn, but the labels and structure must still be readable.
```

- [ ] **Step 2: Verify placement and content**

Run:
```bash
grep -n "^## Illustrating documents" SKILL.md
grep -n "^### Writing figure prompts" SKILL.md
grep -n "^## Workflow" SKILL.md
grep -n "^## Limits" SKILL.md
```
Expected order by line number: `## Workflow` < `## Illustrating documents` < `### Writing figure prompts` < `## Limits`.

Also confirm the key claims are present:
```bash
grep -c "Announce a brief plan first" SKILL.md   # 1
grep -c "Fan out background subagents" SKILL.md   # 1
grep -c "Parallelism depends on the user's backend" SKILL.md   # 1
grep -c "Don't over-illustrate" SKILL.md   # 1
grep -c "content accuracy beats polish" SKILL.md   # 1
```

- [ ] **Step 3: Commit**

```bash
git add SKILL.md
git commit -m "docs(skill): 新增「Illustrating documents」工作流 + 写图 prompt 指导"
```

---

## Task 4: Final consistency read-through

**Files:**
- Modify: `SKILL.md` (only if the read-through finds a problem)

- [ ] **Step 1: Check internal anchor links resolve**

The new content links to `#styles` (the existing "## Styles" section) and the "When to
use" bullet links to `#illustrating-documents`. Confirm both targets exist:
```bash
grep -n "^## Styles" SKILL.md            # target of [Styles](#styles)
grep -n "^## Illustrating documents" SKILL.md  # target of (#illustrating-documents)
```
Expected: both headings exist (GitHub slugifies "## Illustrating documents" → `illustrating-documents` and "## Styles" → `styles`).

- [ ] **Step 2: Read the whole file once for tone/flow**

Open `SKILL.md` and read it top to bottom. Confirm: the proactive framing is consistent
(no leftover "only when asked" wording that now contradicts the proactive trigger); the
new section reads in the same terse voice as the rest; no duplicated headings; the
front-matter `description` is still a single valid YAML string (no stray unescaped quotes
or newlines).

- [ ] **Step 3: Sanity-check the front matter parses**

```bash
python3 - <<'PY'
import sys
src = open("SKILL.md").read()
parts = src.split("---", 2)
assert len(parts) >= 3 and parts[0] == "", "front matter delimiters malformed"
fm = parts[1]
# crude check: description is one line, version present
assert 'name: "chatgpt-imagegen"' in fm
assert 'version: "0.11.0"' in fm
assert fm.count('description:') == 1
print("front matter parses OK")
PY
```
Expected: `front matter parses OK`.

- [ ] **Step 4: Commit (only if Step 2 required a fix)**

```bash
git add SKILL.md
git commit -m "docs(skill): 配图主动化——一致性收尾"
```

If the read-through found nothing to fix, skip this commit (Tasks 1–3 already captured all changes).

---

## Done criteria

- `SKILL.md` front matter: `version: "0.11.0"`, and `description` ends with the proactive-illustration clause (verified inside the front matter, not the body).
- "When to use" has the proactive long-form-authoring bullet linking to `#illustrating-documents`.
- A "## Illustrating documents" section exists between "## Workflow" and "## Limits", covering: announce-plan-then-generate, background fan-out (`--quiet -o`), the backend/parallelism trade-off (web=1/queue, codex=4/billed, no backend forced), style-per-tone (no default, `doodle` available), and the anti-over-illustration guardrail.
- A "### Writing figure prompts" subsection gives the content-precise / one-concept / name-the-style / doodle-content-over-polish guidance.
- `#styles` and `#illustrating-documents` anchor targets both exist.
- No code or test files changed (`git diff --name-only main..HEAD` shows only `SKILL.md` and the `docs/superpowers/` spec+plan).
