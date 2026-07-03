# drawstyle — Community Style Platform & CLI Integration — Design

**Date:** 2026-07-03
**Status:** Approved (design)
**Scope:** A public community platform (`drawstyle.leeguoo.com`, new repo `drawstyle`) hosting shareable image-generation style presets, plus new `chatgpt-imagegen` subcommands (`style search` / `style pull` / `style update` / `style publish`) that consume it. Both halves ship in one effort.

---

## Problem

Today styles live inside the `chatgpt-imagegen` script (`_BUILTIN_STYLES`) and each
user's private `styles.json`. Shipping a new style means editing the script,
releasing a version, and waiting for every install to update. Styles cannot be
shared between users at all, and there is no place to *browse* what looks are
available.

This project moves styles onto a public community platform:

- A **gallery site** at `drawstyle.leeguoo.com` where anyone can browse styles by
  use-case and aesthetic, and logged-in users can submit, like, and fork them.
- **CLI integration** so an agent or user pulls a style from the platform on
  demand — no script update, no skill re-install.
- Login via the existing `account.leeguoo.com` SSO; traffic stats via the existing
  `blog.leeguoo.com` central analytics.

## Goals

- Browse/search styles without an account; category + tag filtering.
- Submissions from any logged-in user enter a **review queue**; only
  admin-approved styles become public.
- Each style: text snippet and/or pinned reference images, 1–3 required example
  images, a use-case category, aesthetic tags, like/pull counters (a fork count,
  where shown, is derived via `COUNT(forked_from = id)` — no stored column), an
  integer version.
- CLI: `style search`, `style pull <slug>` (fetch snippet + refs into the local
  library), `style update` (re-check pulled styles), `style publish` (submit a
  local style).
- Reuse verified infrastructure: `account.leeguoo.com` is standard OIDC
  (authorization code + PKCE, public clients supported — confirmed via
  `/.well-known/openid-configuration`); `blog.leeguoo.com/scripts/visitor-beacon.js`
  is a self-locating cross-subdomain beacon (confirmed by reading the script) —
  embedding it is the entire stats integration.
- CLI remains one-file, stdlib-only, and fully functional offline (built-ins and
  already-pulled styles keep working; only search/pull/publish need the network).

## Non-goals (YAGNI)

- No GitHub-style *synced* forks. Fork = copy into your own account with
  `forked_from` provenance; edits re-enter review independently.
- No comments, follows, notifications, or ranking algorithms in v1.
- No platform-side image generation. Example images come from submitters
  (mandatory); the admin may attach optional standard-prompt comparison images
  generated with their own `chatgpt-imagegen`.
- No email/password auth — OIDC only.
- No versioned history browsing; only the current version is served. The version
  integer exists so the CLI can detect updates.
- `chatgpt-imagegen` gains no new Python dependencies.

---

## Architecture

Two repositories, one API contract:

```
┌──────────────────────────── drawstyle (new repo) ───────────────────────────┐
│ Cloudflare Worker (Hono, TypeScript)                                        │
│   • gallery frontend (/, /s/:slug, /submit, /me, /admin) — SSR'd HTML       │
│   • JSON API under /api/*                                                   │
│   • /img/:key — R2 proxy with cache headers (single-domain assets)          │
│ D1: drawstyle_db          R2: drawstyle-assets                              │
│ OIDC RP of account.leeguoo.com (client: drawstyle-web)                      │
│ <script src="https://blog.leeguoo.com/scripts/visitor-beacon.js" defer>     │
└─────────────────────────────────────────────────────────────────────────────┘
                    ▲ HTTPS JSON (public read; Bearer for writes)
┌────────────────── chatgpt-imagegen (this repo) ─────────────────────────────┐
│ new subcommands: style search / pull / update / publish                     │
│ pulled entries land in ~/.config/chatgpt-imagegen/styles.json + assets/     │
└─────────────────────────────────────────────────────────────────────────────┘
```

Naming is prefixed with the project everywhere it shares an account-level
namespace: D1 database `drawstyle_db`, R2 bucket `drawstyle-assets`, OIDC client
`drawstyle-web`, Worker name `drawstyle`.

---

## Data model (D1)

```sql
users(
  id INTEGER PRIMARY KEY,
  oidc_sub TEXT UNIQUE NOT NULL,      -- subject from account.leeguoo.com
  email TEXT NOT NULL,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
)

styles(
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,          -- ^[a-z0-9][a-z0-9_-]*$, same rule as the CLI
  name TEXT NOT NULL,                 -- human display title (spaces/CJK fine); slug is the machine id
  owner_user_id INTEGER NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL CHECK (kind IN ('character','style')),
  snippet TEXT NOT NULL DEFAULT '',   -- may be '' when the style is refs-only
  category TEXT NOT NULL,             -- use-case category key (see below)
  status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','delisted')),
  version INTEGER NOT NULL DEFAULT 1, -- bumped on approval of an edit
  review_note TEXT,                   -- admin note on the most recent reject
  pending_revision TEXT,              -- JSON blob of an owner edit awaiting review (see Edit flow)
  forked_from INTEGER REFERENCES styles(id),
  likes_count INTEGER NOT NULL DEFAULT 0,
  pulls_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)

style_tags(style_id, tag TEXT, PRIMARY KEY(style_id, tag))

style_images(
  id INTEGER PRIMARY KEY,
  style_id INTEGER NOT NULL REFERENCES styles(id),
  r2_key TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('example','reference','official_example')),
  content_type TEXT NOT NULL,         -- png/jpeg/webp only, server-sniffed
  pending INTEGER NOT NULL DEFAULT 0, -- 1 = staged by a pending revision, not yet live
  sort INTEGER NOT NULL DEFAULT 0
)

likes(user_id, style_id, created_at, PRIMARY KEY(user_id, style_id))
```

- **Image roles:** `example` = submitter's showcase output (1–3 required, drives
  the gallery card); `reference` = pinned reference images shipped to the CLI on
  pull (≤4, matching the CLI's `REF_ATTACH_CAP`); `official_example` = optional
  admin-added standard-prompt comparison image.
- **Categories** (use-case, single choice) are a code constant served by the API,
  not a table: `report` 领导汇报, `slides` 专业PPT, `tech-explainer` 技术图解,
  `social-cover` 社交媒体封面, `avatar-ip` 头像/IP形象, `cute` 可爱治愈,
  `retro-comic` 复古漫画, `photo-real` 写实摄影. Adding one is a code change —
  acceptable, categories are curated by design.
- **Aesthetic tags** are free-form lowercase slugs (手绘/watercolor/pixel/3d/…),
  normalized by the admin during review.
- **Slug vs name:** `slug` is the globally-unique machine id (first-come-first-served;
  the admin gate resolves squatting/quality disputes since nothing is public without
  approval) and is what the CLI pulls by; `name` is a display title shown on cards
  and detail pages. On pull, the CLI's local entry key defaults to the slug.
- **Edit flow:** editing an approved style stores the proposed changes as a JSON
  blob in `pending_revision` — `{name, snippet, category, tags, ref_image_ids}` —
  with newly staged images written as `style_images` rows flagged `pending=1`.
  The approved version stays live (gallery and `/package` keep serving the live
  fields) until the admin **approves** the edit: the blob is applied to the live
  columns, staged images flip `pending=0` (replaced refs are deleted), `version`
  bumps, and the blob is cleared. **Rejecting an edit** clears the blob and staged
  images and sets `review_note`; the row stays `approved` with its live content
  untouched. Editable fields: name, snippet, category, tags, reference images.
  `slug` and `kind` are immutable after submission. One pending revision at a
  time per style (a second `PUT` overwrites the blob). `PUT` on the owner's own
  `pending` submission edits it in place (no blob — it isn't live yet); `PUT` on
  a `rejected` one applies the changes and resubmits (status back to `pending`).

## API contract

Public, no auth:

| Endpoint | Purpose |
|---|---|
| `GET /api/styles?category=&tag=&q=&sort=likes\|new\|pulls&page=` | list approved styles |
| `GET /api/styles/:slug` | full detail: snippet, images, tags, counters, version |
| `GET /api/styles/:slug/package` | pull payload: `{slug, name, kind, snippet, version, refs:[{url, content_type}]}` (live reference-role images only); increments `pulls_count`. `style update` checks versions via the cheap `GET /api/styles/:slug` and hits `/package` only when actually re-pulling — the resulting counter inflation on real re-pulls is accepted. |
| `GET /api/meta` | categories + curated tag list |

Authenticated. Two interchangeable credentials, one auth middleware:
the **CLI** sends `Authorization: Bearer <access_token>` from account.leeguoo.com
(validated via JWKS with issuer/audience checks); the **web frontend** sends the
signed HttpOnly session cookie minted at login (SameSite=Lax; state-changing
requests additionally require a custom `X-Requested-With` header as CSRF
protection). Either way the Worker resolves the caller and upserts `users` by
`oidc_sub`:

| Endpoint | Purpose |
|---|---|
| `POST /api/styles` | submit: JSON metadata + images as multipart; status=pending. Optional `forked_from_slug` records fork provenance — **fork is just a pre-filled submission**: the detail page's Fork button opens `/submit` pre-populated from the source style, and the new style enters the review queue like any other. No separate fork endpoint. |
| `PUT /api/styles/:slug` | owner edits → pending revision (see Edit flow). Consumed by the web edit form (`/submit?edit=slug`, owner-only, pre-filled from live fields). |
| `POST /api/styles/:slug/like` / `DELETE …/like` | toggle like |

Admin (same auth middleware — cookie from the `/admin` page or Bearer — plus
email allow-list from the Worker env var `ADMIN_EMAILS`):

| Endpoint | Purpose |
|---|---|
| `GET /api/admin/pending` | review queue (new + edit revisions) |
| `POST /api/admin/styles/:id/approve` | publish / apply revision, bump version |
| `POST /api/admin/styles/:id/reject` | reject with `review_note` — a new submission becomes `rejected`; an edit revision is discarded and the row stays `approved` (see Edit flow) |
| `POST /api/admin/styles/:id/official-example` | attach admin comparison image |
| `POST /api/admin/styles/:id/delist` | pull an approved style from public view |

Errors are JSON `{error: {code, message}}` with proper status codes; the CLI
surfaces `message` verbatim.

## Frontend pages

Server-rendered HTML from the same Worker (no SPA build step), styled to match
the leeguoo.com / blog.leeguoo.com look. Every page embeds the blog's
`visitor-beacon.js` (one line — the script self-locates and posts back to
`blog.leeguoo.com/api/traffic/collect`).

- `/` — gallery: category nav, tag filter chips, search box, card grid. Each card
  shows the primary example image, name, author, kind badge, likes/pulls, and a
  copy-button for `chatgpt-imagegen style pull <slug>`.
- `/s/:slug` — detail: example carousel, snippet (copyable), reference-image
  count, like/fork buttons, version, author, copy-command block. Owner sees an
  Edit button (→ `/submit?edit=slug`).
- `/submit` — submission form (login-gated): slug, name, kind, category, tags,
  snippet, example uploads (1–3, required), reference uploads (0–4). Also serves
  fork (`?fork=slug` — pre-fills **text fields only**; the forker uploads their
  own example and reference images) and owner edit (`?edit=slug`, submits via
  `PUT`).
- `/me` — my submissions (with status), my likes.
- `/admin` — review queue: pending cards with full preview, approve/reject (+note)
  buttons. Rendered only for allow-listed emails; usable from a phone.
- Login: standard OIDC authorization-code + PKCE redirect flow; session held in a
  signed HttpOnly cookie minted by the Worker after the code exchange.

## CLI integration (this repo)

New verbs under the existing `style` subcommand. Platform base URL defaults to
`https://drawstyle.leeguoo.com`, overridable via `$DRAWSTYLE_API`. All HTTP uses
stdlib `urllib` with short timeouts and clear error messages; generation paths
never touch the network for styles.

- **`style search <query> [--category X] [--tag Y]`** — calls `GET /api/styles`,
  prints slug, kind, category, likes/pulls, first line of snippet, and the
  pull command.
- **`style pull <slug> [--as NAME]`** — calls `/package`; writes the snippet into
  `styles.json` and downloads reference images into
  `~/.config/chatgpt-imagegen/assets/<name>/` (MIME-sniffed via the existing
  `_REF_EXT_BY_MIME` allow-list). The entry records provenance:

  ```json
  "pip": {"kind": "character", "snippet": "…", "refs": ["ref-1.png"],
           "origin": {"platform": "drawstyle", "slug": "pip", "version": 3}}
  ```

  `origin` is a new optional field; the new `_normalize_entry` preserves it.
  **Accepted limitation:** an *older* CLI version's mutating commands rebuild
  entries as exactly `{kind, snippet, refs}` and will silently strip `origin`
  (the entry keeps working; only `style update` tracking is lost — re-pull to
  restore). A local name collision aborts with a hint to use `--as`; `--as`
  renames locally while `origin.slug` keeps pointing at the platform entry.
- **`style update [NAME]`** — for entries with `origin`, compares the remote
  `version` via `GET /api/styles/:slug`; newer → re-pulls snippet + refs in place
  via `/package` (old refs for that entry are replaced). No args → checks all
  pulled entries and reports a summary.
- **`style publish <NAME> --category X --example IMG [--example IMG]… [--tag Y]…`**
  — submits the local entry (snippet + its pinned refs + the required example
  images). `--category` is required; omitting it errors listing the valid
  category keys (no interactive prompt — agent-friendly). Publishing an entry
  whose `origin` already points at the caller's own platform style errors with
  a hint to edit on the web (`/submit?edit=slug`) — CLI-side editing is out of
  scope for v1. Auth: OIDC authorization-code + PKCE against `account.leeguoo.com`
  as a public client (`drawstyle-cli`), with the registered loopback
  `http://127.0.0.1:45898/cb`
  redirect — the CLI opens the browser, catches the code, exchanges it, and
  caches the refresh token in `~/.config/chatgpt-imagegen/drawstyle-auth.json`
  (0600). `--from-last` works as an `--example` source, mirroring `style add`.
- **Built-ins stay** as the offline seed; nothing about existing generation,
  stacking, or `--style` resolution changes.

SKILL.md gains a "Platform styles" section teaching agents to `style search`
when the user asks for a look that isn't in `style list`, and to suggest
`style publish` when a user-crafted style turns out well.

## Security & abuse controls

- Nothing user-submitted is publicly visible before admin approval (the primary
  abuse gate).
- Uploads: ≤5 MB each, ≤3 example + ≤4 reference images, server-side magic-byte
  sniffing to png/jpeg/webp only (same allow-list philosophy as the CLI); images
  are re-keyed to content-addressed R2 keys (no user-controlled paths).
- Submission rate limit: 10/day per user (D1 count check).
- Bearer tokens validated against account.leeguoo.com JWKS with issuer/audience
  checks; admin = verified email ∈ `ADMIN_EMAILS`.
- The `/package` endpoint and gallery only ever serve `approved` styles.
- CLI refuses non-image payloads on pull (sniffs before writing into assets/).

## Error handling

- Platform API errors: structured JSON; CLI prints the server `message` and
  exits non-zero.
- CLI offline / platform down: `search/pull/update/publish` fail fast with a
  clear message; generation with local styles is unaffected.
- Pull is atomic per entry: refs download to a temp dir first; `styles.json` is
  updated only after all refs land (reusing the existing atomic
  `_save_styles` write).

## Testing

- **Platform:** vitest + miniflare (workers pool): API list/detail/package
  shapes, auth gating (anon vs user vs admin), review state machine
  (pending→approved/rejected, edit revisions, delist), upload validation
  (size/MIME/count), rate limit, like/fork semantics, slug uniqueness.
- **CLI:** extend `test_chatgpt_imagegen.py` with a mocked HTTP layer: search
  rendering, pull happy path + collision + `--as` + atomicity on mid-download
  failure, update version comparison, publish auth-cache reuse, `origin`
  round-trip through `_normalize_entry`, offline behavior.

## Deployment / ops (one-time)

1. Create repo `drawstyle`; `wrangler` config with D1 `drawstyle_db` (migrations
   in-repo) + R2 `drawstyle-assets`.
2. Register OIDC clients on account.leeguoo.com: `drawstyle-web`
   (confidential or public + PKCE, redirect `https://drawstyle.leeguoo.com/auth/callback`)
   and `drawstyle-cli` (public + PKCE, redirect `http://127.0.0.1:45898/cb`).
3. DNS: `drawstyle.leeguoo.com` → Worker route.
4. Set `ADMIN_EMAILS` (and session-cookie signing secret) as Worker secrets.
5. Seed the three built-ins (doodle/xiaohei/snoopy) as the admin account's first
   approved entries so the gallery isn't empty on day one.
