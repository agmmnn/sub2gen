# Style Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add named, reusable prompt-style presets to `chatgpt-imagegen` — a global config file, one built-in `doodle` style, an optional active default, per-run `--style`/`--no-style`, and a `style` management subcommand.

**Architecture:** Everything stays in the single-file CLI (`chatgpt-imagegen`) plus its test file. New pure helpers (name validation, prompt composition, style resolution) are unit-tested. Persistence lives in `~/.config/chatgpt-imagegen/styles.json` (honoring `$XDG_CONFIG_HOME`), seeded once with built-ins, never auto-overwritten. The `style` subcommand is routed *before* the generation argparse so the existing bare-positional `prompt` parser is untouched. The composed style snippet flows only into the backend dispatch — the output filename and stderr preview keep using the raw user prompt.

**Tech Stack:** Python 3.10+ stdlib only (`json`, `re`, `os`, `argparse`, `pathlib`). Tests: stdlib `unittest`, loaded via `SourceFileLoader` as module `cig` (existing pattern in `test_chatgpt_imagegen.py`).

**Spec:** `docs/superpowers/specs/2026-06-22-style-presets-design.md`

---

## File structure

- **Modify** `chatgpt-imagegen` — add a constants block + helper functions near the other module-level helpers (e.g. just after the imports / existing small helpers, before `main()`), wire 3 lines in `main()`, change 2 existing lines (1318, 1701) to read the composed prompt, add 2 generation flags.
- **Modify** `test_chatgpt_imagegen.py` — add test classes for the new pure + storage + resolution + management helpers, with a temp-`XDG_CONFIG_HOME` isolation helper.
- **Modify** `SKILL.md`, `README.md`, `README.zh-CN.md` — document styles.
- **Create** `docs/superpowers/RELEASE-style-presets.md` — bilingual release-notes draft (not shipped to users; a working doc for the GitHub Release).

> Line numbers below reflect the current file and will drift as you insert code — re-grep (`grep -nF 'args.prompt' chatgpt-imagegen`) before each modify step rather than trusting the number.

---

## Task 1: Pure helpers — name validation & prompt composition

**Files:**
- Modify: `chatgpt-imagegen` (add constants + `_valid_style_name`, `_compose_prompt`)
- Test: `test_chatgpt_imagegen.py`

- [ ] **Step 1: Write the failing tests**

Add to `test_chatgpt_imagegen.py` (after the existing `IsUrl` class):

```python
class ValidStyleName(unittest.TestCase):
    def test_accepts_slugs(self):
        for ok in ("doodle", "flat-icon", "v2", "a", "my_style"):
            self.assertTrue(cig._valid_style_name(ok), ok)

    def test_rejects_bad(self):
        for bad in ("", "Doodle", "has space", "-leading", "_leading", "with.dot", "藝術"):
            self.assertFalse(cig._valid_style_name(bad), bad)


class ComposePrompt(unittest.TestCase):
    def test_appends_with_comma(self):
        self.assertEqual(cig._compose_prompt("a cat", "watercolor"), "a cat, watercolor")

    def test_none_or_blank_snippet_unchanged(self):
        self.assertEqual(cig._compose_prompt("a cat", None), "a cat")
        self.assertEqual(cig._compose_prompt("a cat", "   "), "a cat")

    def test_strips_one_trailing_punct(self):
        self.assertEqual(cig._compose_prompt("a cat.", "watercolor"), "a cat, watercolor")
        self.assertEqual(cig._compose_prompt("a cat, ", "watercolor"), "a cat, watercolor")

    def test_empty_prompt_yields_snippet(self):
        self.assertEqual(cig._compose_prompt("", "watercolor"), "watercolor")

    def test_snippet_is_trimmed(self):
        self.assertEqual(cig._compose_prompt("a cat", "  watercolor  "), "a cat, watercolor")
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python3 -m unittest test_chatgpt_imagegen -v 2>&1 | grep -E "ValidStyleName|ComposePrompt|AttributeError"`
Expected: FAIL — `AttributeError: module 'cig' has no attribute '_valid_style_name'`.

- [ ] **Step 3: Implement the constants + helpers**

In `chatgpt-imagegen`, add this block near the other module-level helpers (before `main()`; the doodle text is the exact snippet approved in the spec):

```python
# ── Style presets ──────────────────────────────────────────────────────────
# Named, reusable prompt snippets appended (as a comma-joined suffix) to the
# user's prompt. Stored in ~/.config/chatgpt-imagegen/styles.json; seeded once
# from the built-ins below and never auto-overwritten (use `style reset`).
_BUILTIN_STYLES = {
    "doodle": (
        "drawn as a deliberately crude doodle using the biggest possible blocks "
        "of color, leaning hard into a scribbly, pathetically bad look. White "
        "background, as if drawn with a mouse in an old-school computer paint "
        "program. It should be faintly recognizable yet not quite right — like "
        "it almost matches but everything is subtly off, awkward and confusing. "
        "Low-res, smeared together pixel by pixel, showing off just how absurdly "
        "bad it is. Honestly, draw it however you want — but the content must "
        "still be readable."
    ),
}

_STYLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _valid_style_name(name: str) -> bool:
    """A style name is a lowercase slug: alnum/-/_ , starting alphanumeric."""
    return bool(_STYLE_NAME_RE.match(name or ""))


def _compose_prompt(user_prompt: str, snippet: str | None) -> str:
    """Append a style snippet to the prompt as a comma-joined suffix.

    Blank/None snippet → prompt unchanged. A single trailing ','/'.' on the
    prompt is dropped so the join reads cleanly. Pure; no I/O.
    """
    if not snippet or not snippet.strip():
        return user_prompt
    base = user_prompt.rstrip()
    if base and base[-1] in ",.":
        base = base[:-1].rstrip()
    snippet = snippet.strip()
    return f"{base}, {snippet}" if base else snippet
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `python3 -m unittest test_chatgpt_imagegen.ValidStyleName test_chatgpt_imagegen.ComposePrompt -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add chatgpt-imagegen test_chatgpt_imagegen.py
git commit -m "feat(style): 风格名校验 + prompt 拼接纯函数"
```

---

## Task 2: Storage — path, seed, load, atomic save

**Files:**
- Modify: `chatgpt-imagegen` (add `_styles_path`, `_builtin_styles`, `_default_styles_doc`, `_load_styles`, `_save_styles`)
- Test: `test_chatgpt_imagegen.py`

- [ ] **Step 1: Write the failing tests**

First add a temp-XDG isolation helper near the top of `test_chatgpt_imagegen.py` (after `_in_tmp_cwd`):

```python
@contextmanager
def _tmp_xdg():
    """Isolate styles.json under a temp XDG_CONFIG_HOME so tests never touch ~/.config."""
    prev = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as d:
        os.environ["XDG_CONFIG_HOME"] = d
        try:
            yield Path(d)
        finally:
            if prev is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = prev
```

Then add the test class:

```python
class StyleStorage(unittest.TestCase):
    def test_path_honors_xdg(self):
        with _tmp_xdg() as d:
            self.assertEqual(cig._styles_path(),
                             Path(d) / "chatgpt-imagegen" / "styles.json")

    def test_load_seeds_builtins_when_missing(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            self.assertEqual(doc["default"], "")
            self.assertIn("doodle", doc["styles"])
            self.assertTrue(cig._styles_path().exists())  # seeded to disk

    def test_existing_file_not_reseeded(self):
        with _tmp_xdg():
            cig._load_styles()                    # seed
            doc = cig._load_styles()
            del doc["styles"]["doodle"]           # user removes the builtin
            cig._save_styles(doc)
            again = cig._load_styles()
            self.assertNotIn("doodle", again["styles"])  # stays deleted

    def test_save_roundtrip_and_atomic(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            doc["styles"]["custom"] = "neon glow"
            doc["default"] = "custom"
            cig._save_styles(doc)
            reread = cig._load_styles()
            self.assertEqual(reread["styles"]["custom"], "neon glow")
            self.assertEqual(reread["default"], "custom")
            # no leftover temp file beside the target
            self.assertEqual(list(cig._styles_path().parent.glob("*.tmp")), [])

    def test_corrupt_file_raises(self):
        with _tmp_xdg():
            p = cig._styles_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{not json", encoding="utf-8")
            with self.assertRaises(SystemExit):
                cig._load_styles()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python3 -m unittest test_chatgpt_imagegen.StyleStorage -v`
Expected: FAIL — `AttributeError: module 'cig' has no attribute '_styles_path'`.

- [ ] **Step 3: Implement storage**

Add to `chatgpt-imagegen` (below the Task 1 helpers):

```python
def _styles_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base) if base else Path.home() / ".config"
    return root / "chatgpt-imagegen" / "styles.json"


def _builtin_styles() -> dict:
    return dict(_BUILTIN_STYLES)


def _default_styles_doc() -> dict:
    return {"version": 1, "default": "", "styles": _builtin_styles()}


def _save_styles(doc: dict) -> None:
    """Atomically write the styles doc (temp file + os.replace)."""
    path = _styles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _load_styles() -> dict:
    """Read the styles doc, seeding built-ins on first use. Raises SystemExit
    (clear message) on a corrupt/unreadable file rather than clobbering it."""
    path = _styles_path()
    if not path.exists():
        doc = _default_styles_doc()
        _save_styles(doc)
        return doc
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise SystemExit(f"error: cannot read styles file {path}: {e}")
    if not isinstance(doc, dict) or not isinstance(doc.get("styles"), dict):
        raise SystemExit(
            f"error: malformed styles file {path} "
            f"(expected an object with a 'styles' map). "
            f"Fix it by hand or run `chatgpt-imagegen style reset`.")
    doc.setdefault("version", 1)
    doc.setdefault("default", "")
    return doc
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `python3 -m unittest test_chatgpt_imagegen.StyleStorage -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add chatgpt-imagegen test_chatgpt_imagegen.py
git commit -m "feat(style): styles.json 存储——XDG 路径、首次 seed、原子写"
```

---

## Task 3: Resolution logic (the truth table)

**Files:**
- Modify: `chatgpt-imagegen` (add `_resolve_style_name`, `_die_unknown_style`)
- Test: `test_chatgpt_imagegen.py`

- [ ] **Step 1: Write the failing tests**

```python
class ResolveStyleName(unittest.TestCase):
    DOC = {"version": 1, "default": "doodle",
           "styles": {"doodle": "d-snippet", "neon": "n-snippet"}}

    def test_no_style_wins_over_everything(self):
        self.assertIsNone(cig._resolve_style_name(
            self.DOC, style_arg="neon", no_style=True))

    def test_style_arg_overrides_default(self):
        self.assertEqual(cig._resolve_style_name(
            self.DOC, style_arg="neon", no_style=False), "neon")

    def test_falls_back_to_default(self):
        self.assertEqual(cig._resolve_style_name(
            self.DOC, style_arg=None, no_style=False), "doodle")

    def test_empty_default_is_none(self):
        doc = {"default": "", "styles": {"neon": "x"}}
        self.assertIsNone(cig._resolve_style_name(
            doc, style_arg=None, no_style=False))

    def test_unknown_style_arg_raises(self):
        with self.assertRaises(SystemExit):
            cig._resolve_style_name(self.DOC, style_arg="nope", no_style=False)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python3 -m unittest test_chatgpt_imagegen.ResolveStyleName -v`
Expected: FAIL — attribute missing.

- [ ] **Step 3: Implement resolution**

Add to `chatgpt-imagegen`:

```python
def _die_unknown_style(name: str, styles: dict) -> "NoReturn":
    avail = ", ".join(sorted(styles)) or "(none)"
    raise SystemExit(f"error: unknown style {name!r}. available: {avail}")


def _resolve_style_name(doc: dict, *, style_arg: str | None,
                        no_style: bool) -> str | None:
    """Pick the style NAME to apply (or None). Order:
    1. --no-style       → None
    2. --style NAME     → NAME (error if unknown)
    3. active default   → it
    4. otherwise        → None
    """
    if no_style:
        return None
    styles = doc.get("styles", {})
    name = style_arg or (doc.get("default") or "")
    if not name:
        return None
    if name not in styles:
        _die_unknown_style(name, styles)
    return name
```

(If `NoReturn` import is undesirable, drop the annotation on `_die_unknown_style` and use `-> None`. Keep it simple — `-> None` is fine.)

- [ ] **Step 4: Run tests, verify they pass**

Run: `python3 -m unittest test_chatgpt_imagegen.ResolveStyleName -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add chatgpt-imagegen test_chatgpt_imagegen.py
git commit -m "feat(style): 风格解析顺序（no-style > --style > default）"
```

---

## Task 4: `style` management subcommand

**Files:**
- Modify: `chatgpt-imagegen` (add `_style_command`)
- Test: `test_chatgpt_imagegen.py`

- [ ] **Step 1: Write the failing tests**

These drive `_style_command(argv)` directly (it returns an int exit code and writes the file). Capture stdout where the verb prints to it.

```python
import io
from contextlib import redirect_stdout

class StyleCommand(unittest.TestCase):
    def test_add_then_show(self):
        with _tmp_xdg():
            self.assertEqual(cig._style_command(["add", "neon", "neon glow"]), 0)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cig._style_command(["show", "neon"]), 0)
            self.assertEqual(out.getvalue().strip(), "neon glow")

    def test_add_invalid_name_raises(self):
        with _tmp_xdg():
            with self.assertRaises(SystemExit):
                cig._style_command(["add", "Bad Name", "x"])

    def test_use_and_clear_default(self):
        with _tmp_xdg():
            cig._style_command(["add", "neon", "x"])
            cig._style_command(["use", "neon"])
            self.assertEqual(cig._load_styles()["default"], "neon")
            cig._style_command(["clear"])
            self.assertEqual(cig._load_styles()["default"], "")

    def test_rm_clears_default_if_pointed_there(self):
        with _tmp_xdg():
            cig._style_command(["add", "neon", "x"])
            cig._style_command(["use", "neon"])
            cig._style_command(["rm", "neon"])
            doc = cig._load_styles()
            self.assertNotIn("neon", doc["styles"])
            self.assertEqual(doc["default"], "")

    def test_rm_unknown_raises(self):
        with _tmp_xdg():
            with self.assertRaises(SystemExit):
                cig._style_command(["rm", "ghost"])

    def test_list_marks_default(self):
        with _tmp_xdg():
            cig._style_command(["add", "neon", "x"])
            cig._style_command(["use", "neon"])
            out = io.StringIO()
            with redirect_stdout(out):
                cig._style_command(["list"])
            lines = out.getvalue()
            self.assertIn("neon", lines)
            self.assertIn("*", lines)   # default marker

    def test_reset_restores_builtins(self):
        with _tmp_xdg():
            cig._style_command(["add", "neon", "x"])
            cig._style_command(["rm", "doodle"])
            self.assertEqual(cig._style_command(["reset", "-y"]), 0)
            doc = cig._load_styles()
            self.assertIn("doodle", doc["styles"])
            self.assertNotIn("neon", doc["styles"])
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `python3 -m unittest test_chatgpt_imagegen.StyleCommand -v`
Expected: FAIL — attribute missing.

- [ ] **Step 3: Implement the subcommand**

Add to `chatgpt-imagegen`:

```python
def _style_command(argv: list[str]) -> int:
    """`chatgpt-imagegen style <verb> ...` — manage style presets."""
    p = argparse.ArgumentParser(
        prog="chatgpt-imagegen style",
        description="Manage reusable prompt-style presets "
                    f"(stored in {_styles_path()}).")
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("list", help="list all styles (the active default is marked *)")
    sp = sub.add_parser("show", help="print one style's snippet in full")
    sp.add_argument("name")
    sp = sub.add_parser("add", help="create or overwrite a style")
    sp.add_argument("name")
    sp.add_argument("snippet")
    sp = sub.add_parser("rm", help="delete a style")
    sp.add_argument("name")
    sp = sub.add_parser("use", help="set the active default style")
    sp.add_argument("name")
    sub.add_parser("clear", help="unset the active default style")
    rp = sub.add_parser("reset", help="re-seed the built-in styles (discards edits)")
    rp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    a = p.parse_args(argv)

    if not a.verb:
        p.print_help()
        return 2

    doc = _load_styles()
    styles = doc["styles"]

    if a.verb == "list":
        if not styles:
            print("(no styles)")
            return 0
        default = doc.get("default") or ""
        for name in sorted(styles):
            preview = " ".join(styles[name].split())
            if len(preview) > 60:
                preview = preview[:59] + "…"
            print(f"{'*' if name == default else ' '} {name}: {preview}")
        if default:
            print(f"\n* = active default ({default})")
        else:
            print("\n(no active default — pass --style NAME to apply one)")
        return 0

    if a.verb == "show":
        if a.name not in styles:
            _die_unknown_style(a.name, styles)
        print(styles[a.name])
        return 0

    if a.verb == "add":
        if not _valid_style_name(a.name):
            raise SystemExit(
                f"error: invalid style name {a.name!r}; use lowercase letters, "
                f"digits, '-' or '_', starting with a letter or digit.")
        existed = a.name in styles
        styles[a.name] = a.snippet
        _save_styles(doc)
        print(f"{'updated' if existed else 'added'} style {a.name!r}", file=sys.stderr)
        return 0

    if a.verb == "rm":
        if a.name not in styles:
            _die_unknown_style(a.name, styles)
        del styles[a.name]
        cleared = doc.get("default") == a.name
        if cleared:
            doc["default"] = ""
        _save_styles(doc)
        tail = " (was the active default; default cleared)" if cleared else ""
        print(f"removed style {a.name!r}{tail}", file=sys.stderr)
        return 0

    if a.verb == "use":
        if a.name not in styles:
            _die_unknown_style(a.name, styles)
        doc["default"] = a.name
        _save_styles(doc)
        print(f"active default style set to {a.name!r}", file=sys.stderr)
        return 0

    if a.verb == "clear":
        doc["default"] = ""
        _save_styles(doc)
        print("active default style cleared", file=sys.stderr)
        return 0

    if a.verb == "reset":
        if not a.yes:
            sys.stderr.write(
                "This discards all style edits and restores built-ins. "
                "Continue? [y/N] ")
            sys.stderr.flush()
            if sys.stdin.readline().strip().lower() not in ("y", "yes"):
                print("aborted", file=sys.stderr)
                return 1
        _save_styles(_default_styles_doc())
        print("styles reset to built-ins", file=sys.stderr)
        return 0

    return 0
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `python3 -m unittest test_chatgpt_imagegen.StyleCommand -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add chatgpt-imagegen test_chatgpt_imagegen.py
git commit -m "feat(style): style 子命令（list/show/add/rm/use/clear/reset）"
```

---

## Task 5: Wire styles into generation (`main()`)

**Files:**
- Modify: `chatgpt-imagegen` — route the `style` verb, add 2 flags, compose the gen prompt, add the stderr `style:` line, switch 2 reads to the composed prompt.

No new unit tests (consistent with the repo — `main()` and the dispatch paths aren't unit-tested). Verification is the full suite + a manual smoke test.

- [ ] **Step 1: Route the `style` subcommand at the top of `main()`**

In `chatgpt-imagegen`, make the first lines of `def main() -> int:` (currently line ~1839) read:

```python
def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "style":
        return _style_command(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description="Generate an image with your ChatGPT subscription (no API key)."
    )
```

- [ ] **Step 2: Add the `--style` / `--no-style` mutually-exclusive flags**

Right after the `parser.add_argument("prompt", ...)` line (~1847), insert:

```python
    style_grp = parser.add_mutually_exclusive_group()
    style_grp.add_argument(
        "--style", default=None, metavar="NAME",
        help="Apply a saved style preset's text to this prompt "
             "(see `chatgpt-imagegen style list`). Overrides the active "
             "default for this run.",
    )
    style_grp.add_argument(
        "--no-style", action="store_true",
        help="Skip styles for this run even if an active default is set.",
    )
```

- [ ] **Step 3: Resolve + compose, just after `args = parser.parse_args()`**

After `args = parser.parse_args()` (~1952) and before the `if progress:` preview block, insert:

```python
    _styles_doc = _load_styles()
    _style_name = _resolve_style_name(
        _styles_doc, style_arg=args.style, no_style=args.no_style)
    _snippet = _styles_doc["styles"][_style_name] if _style_name else None
    args.gen_prompt = _compose_prompt(args.prompt, _snippet)
```

- [ ] **Step 4: Surface the applied style in the stderr preamble**

Inside the existing `if progress:` block, after the `prompt:` print (~1964), add:

```python
        if _style_name:
            print(f"           style: {_style_name}", file=sys.stderr)
```

(The `prompt:` preview keeps using the **raw** `args.prompt` — unchanged.)

- [ ] **Step 5: Feed the composed prompt to the backends only**

Re-grep first: `grep -nF 'args.prompt' chatgpt-imagegen`. Change the two **dispatch** reads (NOT the preview at ~1958, NOT `_default_out_path` at ~1968):

- `_build_web_text(args.prompt, ...)` (was line 1318) → `_build_web_text(args.gen_prompt, ...)`
- `_build_payload(args.prompt, ...)` (was line 1701) → `_build_payload(args.gen_prompt, ...)`

Leave `_default_out_path(args.prompt, args.format)` and the `prompt_preview` line on the raw `args.prompt`.

- [ ] **Step 6: Run the full suite**

Run: `python3 -m unittest test_chatgpt_imagegen -v`
Expected: PASS (all — old + new).

- [ ] **Step 7: Byte-compile + manual smoke (no image needed for routing/flags)**

```bash
python3 -m py_compile chatgpt-imagegen && echo "compile ok"
./chatgpt-imagegen style list
./chatgpt-imagegen style add neon "neon-lit, teal and magenta glow"
./chatgpt-imagegen style use neon
./chatgpt-imagegen style list          # neon marked *
./chatgpt-imagegen --help | grep -A1 -- --style
# Dry sanity: an unknown style must error fast, before any browser/codex work:
./chatgpt-imagegen "a cat" --style ghost ; echo "exit=$?"   # expect: error + non-zero
./chatgpt-imagegen style clear
./chatgpt-imagegen style rm neon
```

Expected: `style list` shows `doodle` (and `neon` until removed); the default marker tracks `use`/`clear`; `--style ghost` prints `error: unknown style 'ghost'. available: ...` and exits non-zero **without** trying to generate.

> The `--style ghost` fast-fail works because `_resolve_style_name` runs in `main()` before `_dispatch`. Confirm it does not open a browser.

- [ ] **Step 8: Commit**

```bash
git add chatgpt-imagegen
git commit -m "feat(style): 生成时套用风格——style 路由 + --style/--no-style 接入 main"
```

---

## Task 6: Docs — SKILL.md + README (EN/zh)

**Files:**
- Modify: `SKILL.md`, `README.md`, `README.zh-CN.md`

- [ ] **Step 1: SKILL.md — teach agents the mechanism**

Add a short "Styles" subsection. Cover: `--style NAME` / `--no-style` at generation; that a style is pure appended prompt text; that an agent can `chatgpt-imagegen style add <name> "<snippet>"` once to capture a project-specific look and reuse it across a session; that the active default (if any) auto-applies, and `--no-style` opts out. Note the built-in `doodle` and that there is **no** default out of the box.

- [ ] **Step 2: README.md — add a "Styles" section**

After the relevant CLI/usage section, add a "Styles" section: the `style` subcommand table (list/show/add/rm/use/clear/reset), the generation flags (`--style`, `--no-style`), the resolution order in one line, the storage path, and a `doodle` example. If easy, reference `docs/example-doodle.png` as the sample output.

- [ ] **Step 3: README.zh-CN.md — mirror the section in Chinese**

Same content, Chinese. Keep the table columns identical to the EN version.

- [ ] **Step 4: Verify links/headings render**

Run: `grep -n "style" README.md README.zh-CN.md SKILL.md | head`
Expected: the new sections present in all three.

- [ ] **Step 5: Commit**

```bash
git add SKILL.md README.md README.zh-CN.md
git commit -m "docs: 风格预设——SKILL/README 双语说明"
```

---

## Task 7: Release notes draft

**Files:**
- Create: `docs/superpowers/RELEASE-style-presets.md`

- [ ] **Step 1: Write the bilingual release-notes draft**

A polished, bilingual draft for the GitHub Release (working doc; the actual Release is published by the user). Include:
- One-line headline: reusable prompt-style presets.
- What's new: `style` subcommand, `--style`/`--no-style`, built-in `doodle`.
- **Backward-compat callout:** nothing changes for existing users — there is no default style; existing commands behave identically.
- Quickstart snippet (add → use → generate → clear).
- Where styles live (`~/.config/chatgpt-imagegen/styles.json`) and the `style reset` escape hatch.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/RELEASE-style-presets.md
git commit -m "docs: style presets release notes 草稿（双语）"
```

---

## Done criteria

- `python3 -m unittest test_chatgpt_imagegen -v` — all pass (old + ~20 new assertions).
- `python3 -m py_compile chatgpt-imagegen` — clean.
- `chatgpt-imagegen "a cat"` behaves exactly as before (no style, since default is empty).
- `--style doodle` appends the doodle snippet to the backend text only; filename + `prompt:` preview stay raw.
- `style` subcommand manages presets and persists to `~/.config/chatgpt-imagegen/styles.json`.
- Unknown `--style` fails fast (before any browser/codex work).
- SKILL.md + both READMEs document the feature; release-notes draft committed.
