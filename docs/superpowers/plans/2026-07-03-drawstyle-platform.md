# drawstyle Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy `drawstyle.leeguoo.com` — a community platform for image-generation style presets: public gallery, login via account.leeguoo.com, submission → admin review queue, likes/forks, and a read API consumed by the `chatgpt-imagegen` CLI.

**Architecture:** One Cloudflare Worker (Hono, TypeScript) serves the JSON API, the server-rendered HTML pages, and an R2 image proxy. D1 (`drawstyle_db`) holds users/styles/tags/images/likes; R2 (`drawstyle-assets`) holds image bytes. Auth is one middleware accepting either an account.leeguoo.com Bearer token (CLI) or a signed session cookie (web). Traffic stats = one `visitor-beacon.js` script tag per page.

**Tech Stack:** Hono, TypeScript, Wrangler, D1, R2, vitest + `@cloudflare/vitest-pool-workers`, Web Crypto (JWT verify + cookie signing). No frontend framework — `hono/html` SSR.

**Spec:** `/Users/leo/github.com/chatgpt-imagegen/docs/superpowers/specs/2026-07-03-drawstyle-platform-design.md` — payload shapes, state machine, and limits live there; this plan cites it rather than restating.

**Repo:** create at `/Users/leo/github.com/drawstyle` (new git repo, `main` branch).

---

## Conventions (read first)

- Commit style: Chinese conventional commits (`feat: …` / `fix: …` / `docs: …`), matching the owner's other repos.
- Every task ends with `npm test` green and a commit. Tests use the Workers vitest pool so they run against real D1/R2 bindings in miniflare — no hand-rolled mocks for storage.
- All secrets/config via bindings: `ADMIN_EMAILS` (comma-separated), `SESSION_SECRET`, `OIDC_ISSUER` (`https://account.leeguoo.com`), `OIDC_CLIENT_ID` (`drawstyle-web`), `OIDC_CLIENT_SECRET` (empty if registered public+PKCE). Tests set them in `wrangler.jsonc`'s `vars` + `.dev.vars`.
- Numbers that must match the spec: uploads ≤5 MB each, 1–3 `example`, 0–4 `reference` images, png/jpeg/webp only (magic-byte sniff), 10 submissions/user/day, slug regex `^[a-z0-9][a-z0-9_-]*$`.

### File structure

```
drawstyle/
├── wrangler.jsonc               # worker name drawstyle, D1+R2 bindings, vars
├── package.json  tsconfig.json  vitest.config.ts
├── migrations/0001_init.sql
├── src/
│   ├── index.ts                 # Hono app assembly + route mounting only
│   ├── db.ts                    # typed D1 query helpers (one function per query)
│   ├── auth.ts                  # bearer JWT verify (JWKS), session cookie sign/verify, requireUser/requireAdmin middleware
│   ├── oidc.ts                  # web login: /auth/login, /auth/callback, /auth/logout
│   ├── images.ts                # sniffMime, upload validation, R2 put/get, /img/:key proxy
│   ├── api/styles-read.ts       # GET list/detail/package/meta
│   ├── api/styles-write.ts      # POST submit, PUT edit, like endpoints
│   ├── api/admin.ts             # pending/approve/reject/delist/official-example
│   └── pages/                   # SSR: layout.tsx, gallery.tsx, detail.tsx, submit.tsx, me.tsx, admin.tsx
├── scripts/seed-builtins.md     # how the admin seeds doodle/xiaohei/snoopy via the live site
└── test/
    ├── helpers.ts               # app fixture, makeUser/makeStyle/loginAs utilities
    ├── styles-read.test.ts  styles-write.test.ts  admin.test.ts
    ├── auth.test.ts  images.test.ts
```

Rationale: routes split by privilege level (read / user-write / admin) so the auth surface of each file is obvious; `db.ts` keeps SQL in one reviewable place; pages are display-only and call the same `db.ts` helpers as the API.

---

### Task 1: scaffold + CI-able test harness

**Files:** create `wrangler.jsonc`, `package.json`, `tsconfig.json`, `vitest.config.ts`, `src/index.ts`, `test/smoke.test.ts`, `.gitignore`.

- [ ] **Step 1:** `mkdir -p /Users/leo/github.com/drawstyle && cd $_ && git init`
- [ ] **Step 2:** `npm init -y && npm i hono && npm i -D wrangler typescript vitest @cloudflare/vitest-pool-workers @cloudflare/workers-types`
- [ ] **Step 3:** `wrangler.jsonc` — worker name `drawstyle`; `d1_databases: [{binding: "DB", database_name: "drawstyle_db", database_id: "TBD-at-deploy", migrations_dir: "migrations"}]`; `r2_buckets: [{binding: "ASSETS", bucket_name: "drawstyle-assets"}]`; `vars: {OIDC_ISSUER: "https://account.leeguoo.com", OIDC_CLIENT_ID: "drawstyle-web", ADMIN_EMAILS: ""}`; `compatibility_date` = today. `vitest.config.ts` uses `defineWorkersConfig` with `wrangler: {configPath: "./wrangler.jsonc"}` and `miniflare: {bindings: {SESSION_SECRET: "test-secret", ADMIN_EMAILS: "admin@test.dev"}}` — do NOT re-declare `d1Databases`/`r2Buckets` there; they come from `wrangler.jsonc` via `configPath`.
- [ ] **Step 3b: D1 migrations in tests.** The vitest workers pool does **not** auto-apply `migrations/`. Wire the documented pattern now so Task 2's tests find their tables: in `vitest.config.ts`, `const migrations = await readD1Migrations("./migrations")` (import from `@cloudflare/vitest-pool-workers/config`) and pass them via `miniflare.bindings.TEST_MIGRATIONS`; add `test/apply-migrations.ts` — `import {applyD1Migrations, env} from "cloudflare:test"; await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);` — and register it in `poolOptions.workers.setupFiles`. (An empty `migrations/` dir is fine at this point.)
- [ ] **Step 4:** `src/index.ts` — Hono app with `GET /healthz` → `{ok: true}`. `test/smoke.test.ts` asserts 200. Run: `npm test` → PASS.
- [ ] **Step 5:** Commit — `feat: 脚手架——Hono Worker + D1/R2 绑定 + vitest workers 池`

### Task 2: D1 schema

**Files:** create `migrations/0001_init.sql`, `src/db.ts`, `test/db.test.ts`, `test/helpers.ts` (app fixture + `makeUser`/`makeStyle`/`loginAs` seeding utilities — grown by later tasks).

- [ ] **Step 1:** Write `0001_init.sql` — the five tables **exactly as in the spec's Data model section** (`users`, `styles` incl. `name`, `pending_revision`, `review_note`, `forked_from`; `style_tags`; `style_images` incl. `pending`; `likes`) plus indexes: `styles(status)`, `styles(category)`, `styles(owner_user_id)`, `style_images(style_id)`, `style_tags(tag)`.
- [ ] **Step 2:** Failing test: apply migrations (the vitest workers pool auto-applies `migrations/`), insert a user + style + image row through `db.ts` helpers `createUser`, `createStyle`, `addImage`, read back via `getStyleBySlug`. Also assert the `status` CHECK rejects `'bogus'` and slug UNIQUE fires.
- [ ] **Step 3:** Implement `db.ts` helpers with typed row interfaces (`UserRow`, `StyleRow`, `ImageRow`). Every later task adds its queries HERE, never inline SQL in handlers.
- [ ] **Step 4:** `npm test` → PASS.
- [ ] **Step 5:** Commit — `feat: D1 schema——styles/users/tags/images/likes + 索引`

### Task 3: auth middleware (Bearer + cookie, admin gate)

**Files:** create `src/auth.ts`, `test/auth.test.ts`.

- [ ] **Step 1: Failing tests** — (a) request with no credential → `requireUser` 401; (b) valid Bearer (RS256 JWT signed by a test JWKS key, `iss` = OIDC_ISSUER) → user upserted by `oidc_sub`, handler sees `c.var.user`; (c) expired/bad-issuer JWT → 401; (d) signed session cookie → same user resolution; (e) cookie on a state-changing request **without** `X-Requested-With` → 403 (CSRF); (f) `requireAdmin` 403 for a non-allow-listed email, 200 for `admin@test.dev`.
   Test JWKS: generate an RSA keypair in the test with Web Crypto, serve its JWK via a stubbed `fetchJwks` (export a `setJwksFetcher` seam in `auth.ts`).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement.** `verifyBearer(token, env)` — resolve `jwks_uri` from `${OIDC_ISSUER}/.well-known/openid-configuration` rather than hardcoding the path (the live issuer's discovery doc reports `https://account.leeguoo.com/jwks.json` today, but discovery is the contract); cache both discovery and JWKS in module scope w/ 1 h TTL, `crypto.subtle.importKey("jwk", …, "RSASSA-PKCS1-v1_5")`, verify signature + `iss` + `exp`; accept `aud` of `drawstyle-web` or `drawstyle-cli`. `signSession(userId, env)` / `verifySession(cookie, env)` — HMAC-SHA256 over `userId.expiry`, base64url. Middleware `authOptional` → resolves user from either credential; `requireUser` → 401 without; `requireAdmin` → checks `user.email ∈ env.ADMIN_EMAILS.split(",")`. CSRF rule: cookie-authenticated non-GET requires header `X-Requested-With: drawstyle`.
- [ ] **Step 4:** `npm test` → PASS.
- [ ] **Step 5:** Commit — `feat: 双凭证鉴权中间件——JWKS Bearer + 签名会话 cookie + admin 白名单`

### Task 4: image handling + R2 proxy

**Files:** create `src/images.ts`, `test/images.test.ts`.

- [ ] **Step 1: Failing tests** — sniff png/jpeg/webp magic bytes correctly, reject others; reject >5 MB; `putImage` stores under a content-hash key (`sha256 hex + ext`) in R2; `GET /img/:key` streams with the stored content-type + `Cache-Control: public, max-age=31536000, immutable`; unknown key → 404.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3: Implement** — same magic-byte prefixes as the CLI (`\x89PNG\r\n\x1a\n`, `\xff\xd8\xff`, `RIFF….WEBP`). Content-addressed keys make replays/dedup free and keys unguessable-by-construction irrelevant (everything served is approved or proxied to its owner/admin only — enforce: `/img/:key` looks up `style_images` and refuses images belonging to non-approved styles unless the requester is owner/admin).
- [ ] **Step 4:** `npm test` → PASS.
- [ ] **Step 5:** Commit — `feat: 图片管线——魔数嗅探/5MB 上限/内容寻址 R2 + /img 代理`

### Task 5: public read API

**Files:** create `src/api/styles-read.ts`, `test/styles-read.test.ts`; modify `src/index.ts` (mount).

- [ ] **Step 1: Failing tests** (seed via `test/helpers.ts` fixtures):
  - `GET /api/styles` returns only `approved`; supports `q` (matches slug/name/snippet, LIKE), `category`, `tag`, `sort=likes|new|pulls` (default `new`), `page` (20/page).
  - `GET /api/styles/:slug` → full detail per spec (live fields only — a row with `pending_revision` still serves live content); 404 for pending/rejected/unknown.
  - `GET /api/styles/:slug/package` → `{slug, name, kind, snippet, version, refs:[{url, content_type}]}` with only live (`pending=0`) reference-role images, and `pulls_count` incremented (assert +1 in DB).
  - `GET /api/meta` → categories (the 8 spec keys with zh labels) + curated tag list.
  - Error shape everywhere: `{error: {code, message}}`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Category list lives in `src/api/styles-read.ts` as `export const CATEGORIES` (single source, pages import it too).
- [ ] **Step 4:** `npm test` → PASS.
- [ ] **Step 5:** Commit — `feat: 公开只读 API——列表/详情/package/meta`

### Task 6: submissions (`POST /api/styles`) + rate limit + fork provenance

**Files:** create `src/api/styles-write.ts`, `test/styles-write.test.ts`; modify `src/index.ts`.

- [ ] **Step 1: Failing tests:**
  - anonymous → 401; authed multipart with **plain form fields** `slug`, `name`, `kind`, `snippet`, `category`, repeated `tag`, optional `forked_from_slug` + 1 `example[]` file → 201, row `pending`, images stored (`example` role), tags rows written. (Field names are the cross-repo contract with the CLI plan's `style publish` — no JSON `meta` part.)
  - validation 400s: bad slug, slug taken, unknown category, 0 examples, 4 examples, >4 refs, oversize file, non-image bytes.
  - `forked_from_slug` resolves to the source style id in `forked_from`; unknown source → 400.
  - 11th submission by the same user in one day (UTC) → 429.
  - snippet-less but ref-having submission is valid (spec: snippet may be '').
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement — parse `multipart/form-data` via `c.req.parseBody({all: true})` (repeated `tag` and `example[]`/`ref[]` arrive as arrays with `all: true`); sniff + size-check every file through `images.ts`; single D1 batch insert. Rate limit = `SELECT COUNT(*) FROM styles WHERE owner_user_id=? AND created_at >= date('now')`.
- [ ] **Step 4:** `npm test` → PASS.
- [ ] **Step 5:** Commit — `feat: 投稿 API——multipart 校验/限流/fork 溯源,一律进待审`

### Task 7: owner edits (`PUT /api/styles/:slug`) + likes

**Files:** modify `src/api/styles-write.ts`, `test/styles-write.test.ts`.

- [ ] **Step 1: Failing tests** — the spec's Edit-flow state machine, verbatim:
  - PUT on someone else's style → 403; on own `approved` → live fields untouched, `pending_revision` blob stored, new ref images written `pending=1`; second PUT overwrites the blob.
  - PUT on own `pending` → fields edited in place, no blob.
  - PUT on own `rejected` → fields applied AND status back to `pending`.
  - `slug`/`kind` in the payload → 400 (immutable).
  - `POST /:slug/like` → likes_count 1, duplicate like idempotent; `DELETE` → 0. Like on non-approved → 404.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement per spec; `likes_count` maintained by the same statement batch as the `likes` row (D1 batch = atomic).
- [ ] **Step 4:** `npm test` → PASS.
- [ ] **Step 5:** Commit — `feat: owner 编辑(pending_revision 状态机) + 点赞`

### Task 8: admin API

**Files:** create `src/api/admin.ts`, `test/admin.test.ts`; modify `src/index.ts`.

- [ ] **Step 1: Failing tests:**
  - non-admin → 403 on every `/api/admin/*`.
  - `GET /api/admin/pending` lists new submissions AND rows with a `pending_revision` blob, each tagged `type: "new" | "revision"`.
  - approve(new) → `approved`, version stays 1; approve(revision) → blob applied to live columns, staged images flip `pending=0`, replaced refs deleted from D1+R2, `version` +1, blob cleared.
  - reject(new) → `rejected` + `review_note`; reject(revision) → blob + staged images discarded, row stays `approved`, `review_note` set.
  - delist(approved) → `delisted`, disappears from list/detail/package.
  - `POST …/official-example` (multipart, 1 file) → `official_example` image row on an approved style.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `npm test` → PASS.
- [ ] **Step 5:** Commit — `feat: 审核 API——待审列表/通过/驳回/下架/官方对比图`

### Task 9: web OIDC login

**Files:** create `src/oidc.ts`, extend `test/auth.test.ts`; modify `src/index.ts`.

- [ ] **Step 1: Failing tests** — `GET /auth/login` → 302 to `${OIDC_ISSUER}/authorize` with `response_type=code`, `code_challenge_method=S256`, `state`, and a short-lived signed `oidc_tx` cookie holding `{state, verifier}`; `GET /auth/callback?code&state` (token endpoint stubbed via a `setTokenFetcher` seam) → user upserted, session cookie set (HttpOnly, Secure, SameSite=Lax, 30 d), redirect `/`; mismatched state → 400; `GET /auth/logout` clears the cookie.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Redirect URI is `https://drawstyle.leeguoo.com/auth/callback` (from `new URL(c.req.url).origin` so `wrangler dev` works too). Identity comes from the ID token's `sub`/`email`/`name` claims (verified with the same JWKS path as Task 3).
- [ ] **Step 4:** `npm test` → PASS.
- [ ] **Step 5:** Commit — `feat: 网页端 OIDC 登录——PKCE 授权码 + 会话 cookie`

### Task 10: SSR pages

**Files:** create `src/pages/layout.tsx`, `gallery.tsx`, `detail.tsx`, `submit.tsx`, `me.tsx`, `admin.tsx`; modify `src/index.ts`; create `test/pages.test.ts`.

- [ ] **Step 1: Failing tests** (HTML smoke assertions, not pixel tests): `/` contains gallery cards for approved styles + category nav + the beacon script tag `blog.leeguoo.com/scripts/visitor-beacon.js`; `/s/:slug` shows snippet, `style pull` command block, like button; anonymous `/submit` → redirect to `/auth/login`; `/submit?fork=slug` pre-fills text fields only; `/me` lists own styles with status badges AND the user's liked styles (spec puts both on `/me`); `/admin` 403 for non-admin, shows pending cards + approve/reject forms for admin.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement with `hono/html` JSX. `layout.tsx`: one shared shell — `<style>` block (CSS custom props on `:root`, dark/light via `prefers-color-scheme`, font stack `-apple-system, …, "PingFang SC", sans-serif`, restrained one-accent palette to match leeguoo.com), beacon script tag, nav with login state. **All state-changing actions go through the inline script, never bare `<form action=…>` posts** — a plain HTML form can't set the `X-Requested-With: drawstyle` header Task 3 requires, can't send `PUT` (edit flow), and can't attach the JSON `meta` part. The shared inline helper (~30 lines) intercepts each form's `submit` event and does `fetch(url, {method, body: new FormData(form)  /* or JSON for like/admin */, headers: {"X-Requested-With": "drawstyle"}})`, then redirects/refreshes on success. Same helper drives like buttons, admin approve/reject, and copy-to-clipboard. Submit/edit/fork are ONE form template fed by mode (blank / live fields / source style text); the edit mode's fetch uses `PUT /api/styles/:slug`.
- [ ] **Step 4:** `npm test` → PASS. Also eyeball locally: `npx wrangler dev` + open `http://localhost:8787`.
- [ ] **Step 5:** Commit — `feat: SSR 页面——画廊/详情/投稿/我的/审核 + 博客统计 beacon`

### Task 11: deploy + ops

**Files:** create `README.md`, `scripts/seed-builtins.md`.

- [ ] **Step 1:** `npx wrangler d1 create drawstyle_db` → paste real `database_id` into `wrangler.jsonc`; `npx wrangler r2 bucket create drawstyle-assets`; `npx wrangler d1 migrations apply drawstyle_db --remote`.
- [ ] **Step 2:** Secrets: `npx wrangler secret put SESSION_SECRET`; set `ADMIN_EMAILS` var to the owner's email.
- [ ] **Step 3:** **Owner action (can't be done by the agent):** register OIDC clients on account.leeguoo.com — `drawstyle-web` (redirect `https://drawstyle.leeguoo.com/auth/callback`) and `drawstyle-cli` (public + PKCE, loopback `http://127.0.0.1:*/cb`). Ask the user to do this and confirm before Step 4.
- [ ] **Step 4:** `npx wrangler deploy`; add DNS route `drawstyle.leeguoo.com` → the worker (owner action in the Cloudflare dashboard, or `routes` in wrangler.jsonc if the zone is on the same account).
- [ ] **Step 5:** Seed built-ins: log in as admin, submit doodle/xiaohei/snoopy through `/submit` (snippets from `chatgpt-imagegen` `_BUILTIN_STYLES`, example images from `docs/styles/*.png` in the CLI repo), approve them in `/admin`. Write the exact steps into `scripts/seed-builtins.md`.
- [ ] **Step 6:** Live smoke: gallery renders; `curl https://drawstyle.leeguoo.com/api/styles | jq` lists 3 styles; `GET /api/styles/doodle/package` returns the snippet; a pull from the real CLI works end-to-end (`chatgpt-imagegen style pull doodle --as doodle2`); beacon events appear in blog.leeguoo.com's admin stats.
- [ ] **Step 7:** Commit — `docs: 部署手册 + 内置风格种子流程`; push to a new GitHub repo `leeguooooo/drawstyle`.

---

## Final verification

- [ ] `npm test` all green; `npx tsc --noEmit` clean.
- [ ] Full manual loop on production: register/login → submit a test style with images → see it pending in `/me` → approve in `/admin` → visible in gallery → `style pull` from the CLI → like/fork round-trip → edit → revision approve bumps version → `style update` picks it up.
- [ ] Reject + delist paths exercised once each.
