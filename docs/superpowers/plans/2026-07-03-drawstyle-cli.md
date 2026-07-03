# drawstyle CLI Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `style search` / `style pull` / `style update` / `style publish` subcommands to `chatgpt-imagegen` so styles can be discovered on, fetched from, and submitted to the drawstyle platform (`drawstyle.leeguoo.com`).

**Architecture:** All platform I/O goes through one choke-point helper (`_platform_request`) using stdlib `urllib` — tests patch that single function. Pulled styles land in the existing `styles.json` v2 entries with a new optional `origin` field; reference images download atomically (temp dir first). Publish authenticates via OIDC authorization-code + PKCE against `account.leeguoo.com` with a loopback redirect, token cached at `~/.config/chatgpt-imagegen/drawstyle-auth.json` (0600).

**Tech Stack:** Python 3.11+ stdlib only (`urllib.request`, `http.server`, `hashlib`, `secrets`, `webbrowser`). Tests: `unittest` + `unittest.mock.patch`, run via `pytest`.

**Spec:** `docs/superpowers/specs/2026-07-03-drawstyle-platform-design.md`

**Companion plan (platform repo):** `docs/superpowers/plans/2026-07-03-drawstyle-platform.md`. This plan is independently shippable — every network test mocks `_platform_request`, so nothing here requires the platform to be deployed.

---

## Conventions (read first)

- The CLI is the single file `chatgpt-imagegen` (repo root, no `.py` extension). Tests load it as module `cig` via `SourceFileLoader` — see `test_chatgpt_imagegen.py:22-26`.
- Tests are `unittest.TestCase` classes; isolate config with the existing `_tmp_xdg()` context manager (`test_chatgpt_imagegen.py:40-52`). Run with `python3 -m pytest test_chatgpt_imagegen.py -v -k <name>`.
- All new code goes in the existing "Style presets" region of the file (after `_style_command`'s helpers, before the parser wiring at `chatgpt-imagegen:2579`). Match the file's style: module-level `_snake` helpers with docstrings explaining *why*, `raise SystemExit("error: …")` for user-facing failures, stderr for progress.
- Commit after every green test, message in Chinese conventional-commit style (see `git log`).

### File structure

| File | Change |
|---|---|
| `chatgpt-imagegen` | new section "Platform (drawstyle) integration": constants, `_platform_request`, search/pull/update/publish verbs wired into `_style_command` |
| `test_chatgpt_imagegen.py` | new test classes: `PlatformRequest`, `StyleSearch`, `StylePull`, `StyleUpdate`, `OidcPkce`, `StylePublish`, `OriginRoundTrip` |
| `SKILL.md` | new "Platform styles" subsection under Styles & assets |
| `README.md` / `README.zh-CN.md` | short "Community styles" paragraph linking drawstyle.leeguoo.com |

---

### Task 1: `origin` survives `_normalize_entry`

The platform provenance field must round-trip through normalization before anything else is built on it.

**Files:**
- Modify: `chatgpt-imagegen:2405-2424` (`_normalize_entry`)
- Test: `test_chatgpt_imagegen.py` (new class `OriginRoundTrip`)

- [ ] **Step 1: Write the failing test**

```python
class OriginRoundTrip(unittest.TestCase):
    def test_origin_preserved(self):
        e = cig._normalize_entry({
            "kind": "style", "snippet": "s", "refs": ["a.png"],
            "origin": {"platform": "drawstyle", "slug": "pip", "version": 3}})
        self.assertEqual(e["origin"],
                         {"platform": "drawstyle", "slug": "pip", "version": 3})

    def test_bad_origin_dropped(self):
        for bad in ("str", 7, ["x"], {"platform": "drawstyle"}):  # missing slug/version
            e = cig._normalize_entry({"kind": "style", "snippet": "s",
                                      "refs": [], "origin": bad})
            self.assertNotIn("origin", e)

    def test_absent_origin_absent(self):
        e = cig._normalize_entry({"kind": "style", "snippet": "s", "refs": []})
        self.assertNotIn("origin", e)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest test_chatgpt_imagegen.py -v -k OriginRoundTrip`
Expected: FAIL — `_normalize_entry` currently rebuilds `{kind, snippet, refs}` and drops `origin`.

- [ ] **Step 3: Implement**

In `_normalize_entry`, before the final `return` of the dict branch, add:

```python
        out = {"kind": kind, "snippet": snippet, "refs": refs}
        origin = value.get("origin")
        if (isinstance(origin, dict) and isinstance(origin.get("slug"), str)
                and isinstance(origin.get("version"), int)):
            out["origin"] = {"platform": str(origin.get("platform") or "drawstyle"),
                             "slug": origin["slug"], "version": origin["version"]}
        return out
```

(Replace the current `return {"kind": kind, "snippet": snippet, "refs": refs}`.)

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest test_chatgpt_imagegen.py -v`
Expected: all PASS (existing StyleStorage tests confirm no regression).

- [ ] **Step 5: Commit** — `feat: styles 条目支持 origin 溯源字段并在归一化中保留`

---

### Task 2: platform HTTP choke point

**Files:**
- Modify: `chatgpt-imagegen` (new section after `_read_last_output`, ~line 2200)
- Test: `test_chatgpt_imagegen.py` (new class `PlatformRequest`)

- [ ] **Step 1: Write the failing test**

```python
class PlatformRequest(unittest.TestCase):
    def test_base_default_and_env(self):
        os.environ.pop("DRAWSTYLE_API", None)
        self.assertEqual(cig._platform_base(), "https://drawstyle.leeguoo.com")
        os.environ["DRAWSTYLE_API"] = "http://localhost:8787/"
        try:
            self.assertEqual(cig._platform_base(), "http://localhost:8787")
        finally:
            os.environ.pop("DRAWSTYLE_API", None)

    def test_error_payload_surfaced(self):
        import urllib.error
        body = json.dumps({"error": {"code": "not_found",
                                     "message": "no such style"}}).encode()
        err = urllib.error.HTTPError("u", 404, "Not Found", {},
                                     io.BytesIO(body))
        with unittest.mock.patch.object(cig, "_urlopen", side_effect=err):
            with self.assertRaises(SystemExit) as cm:
                cig._platform_request("GET", "/api/styles/nope")
            self.assertIn("no such style", str(cm.exception))
```

Add `import unittest.mock` at the top of the test file if absent.

- [ ] **Step 2: Run to verify it fails** — `python3 -m pytest test_chatgpt_imagegen.py -v -k PlatformRequest` → FAIL (`_platform_base` not defined).

- [ ] **Step 3: Implement**

```python
# ── Platform (drawstyle) integration ────────────────────────────────────────
# All drawstyle.leeguoo.com I/O funnels through _platform_request so tests can
# patch one function and offline failures produce one consistent message.

DRAWSTYLE_API_DEFAULT = "https://drawstyle.leeguoo.com"
PLATFORM_TIMEOUT = 15  # seconds


def _platform_base() -> str:
    return (os.environ.get("DRAWSTYLE_API", "").strip()
            or DRAWSTYLE_API_DEFAULT).rstrip("/")


def _urlopen(req, timeout):  # tiny seam so tests can fake the network
    return urllib.request.urlopen(req, timeout=timeout)


def _platform_request(method: str, path: str, *, data: bytes | None = None,
                      headers: dict | None = None) -> dict:
    """One HTTP round-trip to the platform, JSON in/out.

    Raises SystemExit with the server's error.message (or a network hint) —
    callers never see raw urllib exceptions.
    """
    url = _platform_base() + path
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with _urlopen(req, timeout=PLATFORM_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8"))["error"]["message"]
        except Exception:  # noqa: BLE001
            msg = f"HTTP {e.code}"
        raise SystemExit(f"error: platform: {msg}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SystemExit(
            f"error: cannot reach {_platform_base()} ({e}). "
            "Generation with local styles is unaffected; retry when online.")
```

`urllib.request` / `urllib.error` are already imported at the top of the file — verify, add if missing.

- [ ] **Step 4: Run** — `python3 -m pytest test_chatgpt_imagegen.py -v -k PlatformRequest` → PASS.

- [ ] **Step 5: Commit** — `feat: 平台 HTTP 单一入口 _platform_request(超时/错误统一)`

---

### Task 3: `style search`

**Files:**
- Modify: `chatgpt-imagegen` (new `_style_search` + parser wiring in `_style_command`)
- Test: `test_chatgpt_imagegen.py` (new class `StyleSearch`)

- [ ] **Step 1: Write the failing test**

```python
_SEARCH_PAYLOAD = {"styles": [
    {"slug": "pip", "name": "Pip the fox", "kind": "character",
     "category": "avatar-ip", "likes_count": 12, "pulls_count": 90,
     "snippet": "a round orange fox named Pip, thick outlines"},
]}

class StyleSearch(unittest.TestCase):
    def test_renders_rows_and_pull_hint(self):
        with unittest.mock.patch.object(
                cig, "_platform_request", return_value=_SEARCH_PAYLOAD) as m:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cig._style_command(["search", "fox", "--category",
                                         "avatar-ip", "--tag", "cute"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("pip", out)
        self.assertIn("character", out)
        self.assertIn("style pull pip", out)
        path = m.call_args[0][1]
        self.assertIn("q=fox", path)
        self.assertIn("category=avatar-ip", path)
        self.assertIn("tag=cute", path)

    def test_empty_result(self):
        with unittest.mock.patch.object(
                cig, "_platform_request", return_value={"styles": []}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cig._style_command(["search", "nothing"])
        self.assertEqual(rc, 0)
        self.assertIn("no styles found", buf.getvalue())
```

- [ ] **Step 2: Run to verify it fails** — parser rejects `search` verb.

- [ ] **Step 3: Implement**

Parser wiring inside `_style_command` (after the `reset` parser, `chatgpt-imagegen:2611`):

```python
    sp = sub.add_parser("search", help="search the drawstyle platform")
    sp.add_argument("query", nargs="?", default="")
    sp.add_argument("--category", default=None)
    sp.add_argument("--tag", action="append", default=None)
```

Handler (a new branch in `_style_command`, placed with the other verbs; note
`search` must run BEFORE `doc = _load_styles()` needs anything — it doesn't
touch local state at all, so handle it right after `parse_args`):

```python
    if a.verb == "search":
        params = {"q": a.query} if a.query else {}
        if a.category:
            params["category"] = a.category
        for t in (a.tag or []):
            params.setdefault("tag", []).append(t)
        qs = urllib.parse.urlencode(params, doseq=True)
        data = _platform_request("GET", "/api/styles" + (f"?{qs}" if qs else ""))
        rows = data.get("styles") or []
        if not rows:
            print("no styles found")
            return 0
        for s in rows:
            preview = " ".join((s.get("snippet") or "").split())[:60]
            print(f"{s['slug']} [{s['kind']}] ({s['category']}) "
                  f"♥{s.get('likes_count', 0)} ⇩{s.get('pulls_count', 0)} — {preview}")
            print(f"    chatgpt-imagegen style pull {s['slug']}")
        return 0
```

- [ ] **Step 4: Run** — `python3 -m pytest test_chatgpt_imagegen.py -v -k StyleSearch` → PASS. Then the full suite.

- [ ] **Step 5: Commit** — `feat: style search——检索 drawstyle 平台风格`

---

### Task 4: `style pull` (happy path + collision + `--as` + atomicity)

**Files:**
- Modify: `chatgpt-imagegen` (`_style_pull` helper + parser wiring)
- Test: `test_chatgpt_imagegen.py` (new class `StylePull`)

- [ ] **Step 1: Write the failing tests**

```python
_PKG = {"slug": "pip", "name": "Pip the fox", "kind": "character",
        "snippet": "a round orange fox", "version": 3,
        "refs": [{"url": "https://drawstyle.leeguoo.com/img/abc",
                  "content_type": "image/png"}]}
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

class StylePull(unittest.TestCase):
    def _pull(self, argv, pkg=None, blobs=None):
        def fake_request(method, path, **kw):
            return pkg or _PKG
        def fake_download(url):
            if blobs is not None and url in blobs:
                raise OSError("boom")
            return _PNG
        with unittest.mock.patch.object(cig, "_platform_request", fake_request), \
             unittest.mock.patch.object(cig, "_download_bytes", fake_download):
            return cig._style_command(argv)

    def test_happy_path_writes_entry_refs_and_origin(self):
        with _tmp_xdg():
            rc = self._pull(["pull", "pip"])
            self.assertEqual(rc, 0)
            doc = cig._load_styles()
            e = doc["styles"]["pip"]
            self.assertEqual(e["kind"], "character")
            self.assertEqual(e["origin"],
                             {"platform": "drawstyle", "slug": "pip", "version": 3})
            self.assertEqual(len(e["refs"]), 1)
            self.assertTrue((cig._asset_dir("pip") / e["refs"][0]).exists())

    def test_collision_aborts_with_as_hint(self):
        with _tmp_xdg():
            self._pull(["pull", "pip"])
            with self.assertRaises(SystemExit) as cm:
                self._pull(["pull", "pip"])
            self.assertIn("--as", str(cm.exception))

    def test_as_renames_locally_keeps_origin_slug(self):
        with _tmp_xdg():
            self._pull(["pull", "pip", "--as", "fox2"])
            doc = cig._load_styles()
            self.assertIn("fox2", doc["styles"])
            self.assertEqual(doc["styles"]["fox2"]["origin"]["slug"], "pip")

    def test_failed_ref_download_leaves_no_entry(self):
        with _tmp_xdg():
            with self.assertRaises(SystemExit):
                self._pull(["pull", "pip"],
                           blobs={"https://drawstyle.leeguoo.com/img/abc"})
            doc = cig._load_styles()
            self.assertNotIn("pip", doc["styles"])
            self.assertFalse(cig._asset_dir("pip").exists())

    def test_non_image_payload_refused(self):
        with _tmp_xdg():
            with unittest.mock.patch.object(cig, "_platform_request",
                                            return_value=_PKG), \
                 unittest.mock.patch.object(cig, "_download_bytes",
                                            return_value=b"<html>nope</html>"):
                with self.assertRaises(SystemExit) as cm:
                    cig._style_command(["pull", "pip"])
            self.assertIn("not an image", str(cm.exception))
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement**

Parser wiring:

```python
    sp = sub.add_parser("pull", help="fetch a style from the drawstyle platform")
    sp.add_argument("slug")
    sp.add_argument("--as", dest="local_name", default=None, metavar="NAME",
                    help="store under a different local name")
```

Helpers + handler:

```python
def _download_bytes(url: str) -> bytes:
    """Fetch one reference image (seam for tests)."""
    req = urllib.request.Request(url)
    with _urlopen(req, timeout=PLATFORM_TIMEOUT) as resp:
        return resp.read()


def _pull_refs_to_tmp(refs: list[dict], tmp: Path) -> list[str]:
    """Download every ref into tmp, MIME-sniffed. All-or-nothing: any failure
    raises SystemExit before styles.json is touched (pull atomicity per the
    spec). The wrap lives HERE, not in _download_bytes — tests replace
    _download_bytes wholesale, so a raw OSError from the seam must still
    surface as a clean SystemExit."""
    stored: list[str] = []
    for i, ref in enumerate(refs, 1):
        try:
            data = _download_bytes(ref["url"])
        except SystemExit:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise SystemExit(f"error: failed to download ref {ref['url']}: {e}")
        mime = _sniff_mime(data)
        ext = _REF_EXT_BY_MIME.get(mime or "")
        if not ext:
            raise SystemExit(f"error: ref {ref['url']} is not an image "
                             f"(sniffed {mime or 'unknown'})")
        fname = f"ref-{i}.{ext}"
        (tmp / fname).write_bytes(data)
        stored.append(fname)
    return stored
```

```python
    if a.verb == "pull":
        name = a.local_name or a.slug
        if not _valid_style_name(name):
            raise SystemExit(f"error: invalid local name {name!r}")
        if name in styles:
            raise SystemExit(
                f"error: local style {name!r} already exists — "
                f"pass --as OTHERNAME or `style rm {name}` first.")
        pkg = _platform_request("GET", f"/api/styles/{a.slug}/package")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            stored = _pull_refs_to_tmp(pkg.get("refs") or [], tmp)
            asset_dir = _asset_dir(name)
            if stored:
                asset_dir.mkdir(parents=True, exist_ok=True)
                for f in stored:
                    shutil.move(str(tmp / f), asset_dir / f)
        styles[name] = {"kind": pkg["kind"], "snippet": pkg.get("snippet") or "",
                        "refs": stored,
                        "origin": {"platform": "drawstyle", "slug": pkg["slug"],
                                   "version": pkg["version"]}}
        _save_styles(doc)
        print(f"pulled {a.slug!r} v{pkg['version']} as {name!r} "
              f"({len(stored)} ref{'s' if len(stored) != 1 else ''})",
              file=sys.stderr)
        return 0
```

`tempfile` needs importing at the top of the CLI if not already there (check — the CLI may already import it).

- [ ] **Step 4: Run** — StylePull all PASS, then the full suite.

- [ ] **Step 5: Commit** — `feat: style pull——按 slug 拉取平台风格(参考图原子落地+origin 溯源)`

---

### Task 5: `style update`

**Files:**
- Modify: `chatgpt-imagegen` (handler + parser)
- Test: `test_chatgpt_imagegen.py` (new class `StyleUpdate`)

- [ ] **Step 1: Write the failing tests**

```python
class StyleUpdate(unittest.TestCase):
    def test_version_check_uses_detail_not_package(self):
        calls = []
        def fake_request(method, path, **kw):
            calls.append(path)
            if path.endswith("/package"):
                return dict(_PKG, version=4)
            return {"slug": "pip", "version": 4}
        with _tmp_xdg():
            # Seed a locally-pulled entry at v3 directly — don't pull through the
            # fake (it serves v4, which would make update a no-op).
            doc = cig._load_styles()
            doc["styles"]["pip"] = {
                "kind": "character", "snippet": "old", "refs": [],
                "origin": {"platform": "drawstyle", "slug": "pip", "version": 3}}
            cig._save_styles(doc)
            with unittest.mock.patch.object(cig, "_platform_request", fake_request), \
                 unittest.mock.patch.object(cig, "_download_bytes",
                                            return_value=_PNG):
                rc = cig._style_command(["update", "pip"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], "/api/styles/pip")        # cheap check first
        self.assertIn("/api/styles/pip/package", calls[1])   # then re-pull
        self.assertEqual(cig._load_styles()["styles"]["pip"]["origin"]["version"], 4)

    def test_up_to_date_skips_package(self):
        def fake_request(method, path, **kw):
            if path.endswith("/package"):
                return _PKG
            return {"slug": "pip", "version": 3}             # same version
        with _tmp_xdg():
            with unittest.mock.patch.object(cig, "_platform_request", fake_request), \
                 unittest.mock.patch.object(cig, "_download_bytes",
                                            return_value=_PNG):
                cig._style_command(["pull", "pip"])
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cig._style_command(["update"])       # no-arg sweep
        self.assertEqual(rc, 0)
        self.assertIn("up to date", buf.getvalue())

    def test_entry_without_origin_skipped(self):
        with _tmp_xdg():
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cig._style_command(["update"])           # only built-ins
        self.assertEqual(rc, 0)
        self.assertIn("no pulled styles", buf.getvalue())
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement**

Parser: `sp = sub.add_parser("update", help="re-pull platform styles that have a newer version"); sp.add_argument("name", nargs="?", default=None)`.

Handler sketch — for each candidate entry (one named, or all with `origin`):
`GET /api/styles/{origin.slug}` → compare `version`; if newer, run the same
re-pull logic as Task 4 but **replacing in place**: download refs to temp,
`shutil.rmtree(_asset_dir(name))`, move refs in, overwrite the entry (keep the
local name, update `origin.version`), single `_save_styles(doc)` at the end.
Refactor the pull body into `_apply_package(doc, name, pkg)` shared by pull and
update rather than duplicating it — this **replaces Task 4's inline pull body**
(edit the already-committed `pull` branch to call `_apply_package` too; rerun
the StylePull tests to prove the refactor is behavior-neutral). Named-but-not-pulled → `SystemExit("error:
{name!r} has no platform origin")`. No candidates → print `no pulled styles`.
Every up-to-date entry prints `{name}: up to date (v{n})`.

- [ ] **Step 4: Run** — StyleUpdate PASS + full suite.

- [ ] **Step 5: Commit** — `feat: style update——按版本号检查并就地更新已拉取风格`

---

### Task 6: OIDC PKCE loopback login (auth for publish)

**Files:**
- Modify: `chatgpt-imagegen` (new `_oidc_*` helpers)
- Test: `test_chatgpt_imagegen.py` (new class `OidcPkce`)

- [ ] **Step 1: Write the failing tests** — pure-logic parts only (no real browser/server in tests):

```python
class OidcPkce(unittest.TestCase):
    def test_challenge_is_s256_of_verifier(self):
        import hashlib, base64
        v, c = cig._pkce_pair()
        want = base64.urlsafe_b64encode(
            hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(c, want)
        self.assertGreaterEqual(len(v), 43)

    def test_token_cache_roundtrip_and_mode(self):
        with _tmp_xdg():
            cig._save_platform_auth({"access_token": "at", "refresh_token": "rt",
                                     "expires_at": 9999999999})
            p = cig._platform_auth_path()
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(cig._load_platform_auth()["access_token"], "at")

    def test_expired_token_triggers_refresh(self):
        with _tmp_xdg():
            cig._save_platform_auth({"access_token": "old", "refresh_token": "rt",
                                     "expires_at": 1})
            with unittest.mock.patch.object(
                    cig, "_oidc_token_request",
                    return_value={"access_token": "new", "refresh_token": "rt2",
                                  "expires_in": 3600}) as m:
                tok = cig._platform_access_token(interactive=False)
        self.assertEqual(tok, "new")
        self.assertEqual(m.call_args[0][0]["grant_type"], "refresh_token")
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement**

Constants: `OIDC_ISSUER = "https://account.leeguoo.com"`, `OIDC_CLIENT_ID = "drawstyle-cli"` (public client + PKCE — registered per the spec's ops checklist; loopback redirect).

```python
def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge

def _platform_auth_path() -> Path:
    return _styles_path().parent / "drawstyle-auth.json"

def _save_platform_auth(tokens: dict) -> None:   # atomic + chmod 0600
def _load_platform_auth() -> dict | None:

def _oidc_token_request(form: dict) -> dict:
    """POST OIDC_ISSUER/token (urlencoded). Seam for tests."""

def _oidc_login_interactive() -> dict:
    """Open the browser to /authorize (code + PKCE + state), catch the code on
    a one-shot http.server bound to 127.0.0.1:<random port>, exchange it."""

def _platform_access_token(*, interactive: bool = True) -> str:
    """Cached access token; refresh via refresh_token when expired; fall back
    to interactive login (or SystemExit if interactive=False and no refresh)."""
```

Implementation notes for the executor:
- `_oidc_login_interactive`: bind `http.server.HTTPServer(("127.0.0.1", 0), Handler)` to get a random free port; redirect_uri = `http://127.0.0.1:{port}/cb`; verify `state`; `webbrowser.open(url)` and print the URL to stderr as fallback; `handle_request()` once with a 180 s socket timeout; respond with a tiny "you can close this tab" HTML page.
- Token exchange form: `grant_type=authorization_code, code, redirect_uri, client_id, code_verifier`. Store `expires_at = time.time() + expires_in - 60`.
- New imports needed: `base64`, `secrets`, `hashlib`, `webbrowser`, `http.server` (check which already exist at the top of the file; add missing ones).

- [ ] **Step 4: Run** — OidcPkce PASS + full suite.

- [ ] **Step 5: Commit** — `feat: account.leeguoo.com OIDC PKCE 回环登录+token 缓存(0600)`

---

### Task 7: `style publish`

**Files:**
- Modify: `chatgpt-imagegen` (multipart encoder + handler + parser)
- Test: `test_chatgpt_imagegen.py` (new class `StylePublish`)

- [ ] **Step 1: Write the failing tests**

```python
class StylePublish(unittest.TestCase):
    def _setup_local(self):
        cig._style_command(["add", "mylook", "soft watercolor"])

    def test_category_required(self):
        with _tmp_xdg():
            self._setup_local()
            with self.assertRaises(SystemExit) as cm:
                cig._style_command(["publish", "mylook", "--example", "x.png"])
            self.assertIn("--category", str(cm.exception))
            self.assertIn("report", str(cm.exception))   # lists valid keys

    def test_example_required(self):
        with _tmp_xdg():
            self._setup_local()
            with self.assertRaises(SystemExit) as cm:
                cig._style_command(["publish", "mylook", "--category", "cute"])
            self.assertIn("--example", str(cm.exception))

    def test_republish_own_origin_errors(self):
        with _tmp_xdg():
            doc = cig._load_styles()
            doc["styles"]["mine"] = {"kind": "style", "snippet": "s", "refs": [],
                                     "origin": {"platform": "drawstyle",
                                                "slug": "mine", "version": 1}}
            cig._save_styles(doc)
            with self.assertRaises(SystemExit) as cm:
                cig._style_command(["publish", "mine", "--category", "cute",
                                    "--example", "x.png"])
            self.assertIn("edit", str(cm.exception))

    def test_happy_path_posts_multipart(self):
        with _tmp_xdg() as root:
            self._setup_local()
            ex = Path(root) / "ex.png"; ex.write_bytes(_PNG)
            with unittest.mock.patch.object(cig, "_platform_access_token",
                                            return_value="tok"), \
                 unittest.mock.patch.object(
                     cig, "_platform_request",
                     return_value={"slug": "mylook", "status": "pending"}) as m:
                rc = cig._style_command(["publish", "mylook", "--category",
                                         "cute", "--example", str(ex)])
        self.assertEqual(rc, 0)
        method, path = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual((method, path), ("POST", "/api/styles"))
        hdrs = m.call_args[1]["headers"]
        self.assertEqual(hdrs["Authorization"], "Bearer tok")
        self.assertIn("multipart/form-data", hdrs["Content-Type"])
        body = m.call_args[1]["data"]
        self.assertIn(b'name="category"', body)
        self.assertIn(b"cute", body)
        self.assertIn(_PNG, body)
```

Define the platform category keys once in the CLI as `_PLATFORM_CATEGORIES = ("report", "slides", "tech-explainer", "social-cover", "avatar-ip", "cute", "retro-comic", "photo-real")` — mirrors the spec's list; the server revalidates anyway.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement**

Parser:

```python
    sp = sub.add_parser("publish", help="submit a local style to the platform")
    sp.add_argument("name")
    sp.add_argument("--category", default=None)
    sp.add_argument("--tag", action="append", default=None)
    sp.add_argument("--example", action="append", default=None, metavar="IMG")
    sp.add_argument("--from-last", action="store_true",
                    help="use the most recently generated image as an example")
```

Handler order: validate entry exists → refuse if the entry has ANY `origin` (message: `already published as {slug!r} — edit it on the web: {base}/submit?edit={slug}`; deliberately broader than "own style only" — a pulled copy of someone else's style shouldn't be re-published from the CLI either, fork on the web instead. Don't "fix" this to check ownership) → `--category` in `_PLATFORM_CATEGORIES` else SystemExit listing keys (no interactive prompt) → collect examples (`--example` paths + `--from-last` via `_resolve_from_last()`), 1–3 required, each must exist and sniff as an image → build multipart body with **plain form fields** — `slug` (=local name), `name` (=local name for CLI submissions), `kind`, `snippet`, `category`, repeated `tag` fields — plus file parts `example[]` and `ref[]` (the entry's pinned refs from `_asset_dir(name)`). **No JSON `meta` part** — this is the cross-repo contract: the platform's `POST /api/styles` parses these exact field names (see companion plan Task 6). → `_platform_access_token()` → `_platform_request("POST", "/api/styles", data=body, headers={...})` → print `submitted {name!r} for review` to stderr.

Multipart encoder (stdlib, ~15 lines): random boundary via `secrets.token_hex(16)`; each part `--{b}\r\nContent-Disposition: form-data; name="…"[; filename="…"]\r\n[Content-Type: …]\r\n\r\n{payload}\r\n`; terminator `--{b}--\r\n`.

- [ ] **Step 4: Run** — StylePublish PASS + full suite.

- [ ] **Step 5: Commit** — `feat: style publish——本地风格一键投稿(必填示例图+分类,OIDC 鉴权)`

---

### Task 8: docs — SKILL.md + READMEs

**Files:**
- Modify: `SKILL.md` (Styles & assets section, after the built-ins paragraph ~line 150)
- Modify: `README.md` + `README.zh-CN.md` (short paragraph near the styles gallery link)

- [ ] **Step 1: SKILL.md** — add a "Platform styles (drawstyle)" subsection: when the user asks for a look not in `style list`, run `style search <keywords>`; pull with `style pull <slug>`; suggest `style publish` when a user-crafted style turns out well; note `style update` for freshness and that offline generation is unaffected. Include one example flow. Keep the existing tone (imperative guidance to the agent).
- [ ] **Step 2: READMEs** — 3-4 lines: community styles live at drawstyle.leeguoo.com; `style search/pull` one-liner example; both languages.
- [ ] **Step 3: Bump `__version__`** in `chatgpt-imagegen` (minor bump, e.g. 0.16.1 → 0.17.0) and add a `# WHATSNEW[0.17.0]: style search/pull/update/publish — drawstyle 平台集成` line following the existing WHATSNEW comment format (`chatgpt-imagegen:55-61`).
- [ ] **Step 4: Full suite one last time** — `python3 -m pytest test_chatgpt_imagegen.py -v` → all PASS.
- [ ] **Step 5: Commit** — `docs: SKILL/README 增补 drawstyle 平台风格工作流 + v0.17.0`

---

## Final verification

- [ ] `python3 -m pytest test_chatgpt_imagegen.py -v` — all green.
- [ ] `python3 -m py_compile chatgpt-imagegen` — compiles.
- [ ] Manual smoke (requires the platform from the companion plan, or `DRAWSTYLE_API=http://localhost:8787` against `wrangler dev`): `style search doodle`, `style pull <slug>`, generate with `--style <slug>`, `style update`.
