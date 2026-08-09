# Historical Architecture Migration Safety Checkpoint

Checkpoint date: 2026-08-08

This document records the recovery point created before the modular-monolith migration.
Phase 1.5 later replaced the repository identity with the standalone `agmmnn/sub2gen`
repository; current remote information belongs in Git rather than this historical file.

## Repository recovery point

- Repository: `agmmnn/sub2gen`
- Branch: `main`
- Commit: `5b140e1d99b5dbed9f23efd642841be71ee54712`
- Commit subject: `docs: add architecture migration plan`
- Remote state at checkpoint: local `main` matched `origin/main`
- Tags: none
- Open pull requests: none
- GitHub collaborators with push access: only `agmmnn`
- Current GitHub repository type: standalone

The following old local branches were fully contained by `main` and had no unique commits:

- `codex/base-ui-migration`
- `codex/project-pinning`

## Current remote

The completed identity cutover keeps only `origin`, pointing to
`https://github.com/agmmnn/sub2gen.git`.

## Git storage before cleanup

- Packed Git objects: approximately 395.25 MiB
- Loose Git objects: approximately 1.52 MiB
- Tracked `niches/` files: 34
- Tracked `niches/` content size: 284,390,524 bytes
- Commit that introduced the directory: `8de6c2dc6f722d66a010f403283350d775db5355`
- Commit subject: `Add Runway integration support`

## External asset backup

- Archive: external to the repository
- Archive size: approximately 270 MiB compressed
- SHA-256: `2d66581cc55103e3984c626c5a7cb022d324183529a2bdb458b8fe45aa1f8a2d`
- Verification: archive extraction succeeded and all 34 tracked files matched their source SHA-256 checksums
- Additional archived local file: `niches/.DS_Store` (not tracked by Git)

Verify the archive before recovery:

```bash
shasum -a 256 /Users/agm/Documents/Github/sub2gen-niches-backup-2026-08-08.tar.gz
tar -tzf /Users/agm/Documents/Github/sub2gen-niches-backup-2026-08-08.tar.gz
```

Restore the content into a chosen directory:

```bash
tar -xzf /Users/agm/Documents/Github/sub2gen-niches-backup-2026-08-08.tar.gz -C /path/to/restore
```

## History-rewrite gate

Before rewriting history:

- [x] Architecture plan committed and pushed.
- [x] `main` recovery commit recorded.
- [x] Open pull requests checked.
- [x] Push collaborators checked.
- [x] `niches/` history and size inventoried.
- [x] External archive created.
- [x] External archive extracted and verified.
- [ ] User explicitly approves rewriting and force-pushing the fork's `main` history.

The rewrite must be executed from a fresh, origin-only clone. The current multi-remote checkout must not be used as the rewrite workspace.

## Rewrite result

The user approved the destructive rewrite on 2026-08-08.

- Rewritten `main`: `93a0e8b300491470645a9a5c04e00da1d1271384`
- Commits processed: 651
- Rewrite workspace: fresh origin-only clone
- Push protection: explicit `--force-with-lease` against checkpoint commit `96035568ea61d9630ff82965f5158e57291a7998`
- Remote verification: GitHub `refs/heads/main` resolved to the rewritten commit
- `niches/` commits remaining in rewritten history: 0
- Rewritten HEAD tree: byte-identical to checkpoint HEAD except for removal of `niches/`
- `git fsck --full`: clean
- Rewritten packed history: approximately 5.53 MiB

The primary checkout was reset to the verified rewritten `origin/main`. The two fully merged legacy local branches were removed. The `upstream` and `original` remotes were re-added without fetching their old tracking refs, and their push URLs remain disabled.

The Codex desktop application maintains internal `refs/codex/turn-diffs/...` refs for its own review history. Those local-only refs were not deleted, so this existing checkout may retain unreachable pre-rewrite objects until Codex releases them. They are not present on GitHub and do not affect fresh clone size.
