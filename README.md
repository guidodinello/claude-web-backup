# claude-web-backup

A local, automatic nightly backup of Claude.ai projects and their conversations, so
web-side data is never the single point of failure.

Depends on [`claude-client`](https://github.com/guidodinello/claude-client) (a sibling
project, git dependency) for all Claude.ai API access — this project is just the
orchestration, config, and scheduling on top of it.

## What it reuses from claude-client

- `ClaudeClient.projects.pull_all(out_dir, prune=...)` — pulls every project across every
  chat-capable org on an account in one call. Per project it writes:
  `project.md` (name, description, instructions, memory, controls), `docs/` (one file per
  knowledge doc), `conversations/` (one file per conversation). By default it
  **overwrites but never deletes** — accumulate-only. Pass `--prune` (see below) to also
  delete local files/directories removed on the web; the SDK does the deleting, tracked via
  a `.claude-pull-manifest.json` sidecar it writes alongside each pulled directory. All the
  multi-org scoping is handled inside the SDK; this project just calls it once per account
  and maps the result into a report.
- `ClaudeClient.conversations.pull_standalone(out_dir, prune=...)` — pulls every
  conversation that doesn't belong to any project, account-wide across every chat-capable
  org, into one flat `conversations/` directory per account. Same incremental/prune
  semantics as project conversations, sharing one manifest across orgs (conversation uuids
  are unique account-wide).

## Target layout

```
$CLAUDE_BACKUP_DIR/
  personal/
    conversations/         # standalone (non-project) chats, one .md per conversation
    {project-slug}/
      project.md          # title, description, instructions, memory, controls
      docs/                # knowledge files
      conversations/        # one .md per conversation in this project
  work/
    {project-slug}/ ...
```

Note: a project literally named "Conversations" would slugify to `{project-slug}` =
`conversations`, colliding with the reserved account-level directory above (claude-client's
slug resolver has no reserved-name list). A known, accepted edge case — none of this
account's projects hit it — rather than something engineered around. `--reconcile` is
hardened so it can never delete the `conversations/` directory outright either way.

`{account_slug}` comes from the `CLAUDE_BACKUP_TOKEN_<SLUG>` env var name (lowercased).
`{project-slug}` is the sanitized project name (`claude_client.render.slugify`), kept stable
across runs by uuid once a project has a manifest entry. A web-side project rename is
tracked correctly once pruning is enabled (see below); without `--prune` it orphans the old
local dir — harmless for a plain mirror, since the old copy is just stale, not lost.

History model: default is **accumulate-only** — nothing removed on the web is deleted
locally, so a plain run only ever grows the mirror. Pass `--prune`/`CLAUDE_BACKUP_PRUNE=1`
to make it a true mirror instead (see "Pruning" below). Every run is also committed to the
backup's own git history (see "Git-versioned history"), so even pruned files stay
recoverable.

## Installation

```bash
uv sync
```

## Configuration

Copy `env.backup.example` to `.env.backup` (already gitignored via the `.env*` rule) and
fill in real session tokens:

```
CLAUDE_BACKUP_TOKEN_PERSONAL=sk-ant-sid01-...
CLAUDE_BACKUP_TOKEN_WORK=sk-ant-sid01-...
CLAUDE_BACKUP_DIR=/home/guido/claude-backup
```

One `CLAUDE_BACKUP_TOKEN_<SLUG>` per account to back up — add or remove accounts by
adding/removing env vars, no code changes needed. Tokens expire after some weeks; when
they do, the backup run fails loudly (see below) and you re-paste fresh tokens here.

Currently only the personal account is backed up — the work token is deliberately left
unset in `.env.backup`. Set it (and re-add the corresponding `<SLUG>` var) whenever work
backups are wanted; no code changes needed.

## Running manually

```bash
uv run claude-backup                # uses CLAUDE_BACKUP_DIR / ./claude-backup
uv run claude-backup --help
```

Exit code is non-zero if **any** account or project failed to back up — a silent backup
is worse than none. Failures are logged and (best-effort, if `notify-send` is available)
surfaced as a desktop notification. One failing project never aborts the rest of the run;
failures are collected and reported at the end.

A transient network error (DNS not resolving, connection refused, timeout — e.g. the
nightly timer firing right as the machine wakes up, before Wi-Fi has actually come up) is
retried with exponential backoff (4 attempts, starting at 5s) before being counted as a
failure. An expired/invalid session token is not retried — no amount of waiting fixes
that.

## Pruning

Off by default. Two ways to enable it:

- `--prune` (or set `CLAUDE_BACKUP_PRUNE=1` in `.env.backup`, which the nightly systemd run
  reads via `EnvironmentFile`) — threads `prune=True` into both `pull_all` and
  `pull_standalone`. claude-client deletes local docs/conversations/project-dirs (and
  standalone conversations) removed on the web, tracked via its own
  `.claude-pull-manifest.json` manifests. A project whose own pull fails that run is never
  pruned, even with this on — its manifest entry is carried forward unchanged.
- `--reconcile` — this repo's own one-time sweep, for cleaning up orphans that accumulated
  *before* manifests existed (see the gotcha below). Deletes anything on disk that no
  manifest claims, including stale standalone conversations — but the account-level
  `conversations/` directory itself is reserved and never removed wholesale, even though
  it never appears in the project manifest. Skipped entirely (with an error, exit code 1)
  if the run had any failures, since a failed pull means the manifest it just wrote may not
  reflect the web. It also refuses to run if the manifest looks implausibly short (a sanity
  tripwire against sweeping on a partial/broken pull) — see `_reconcile_account` in
  `backup.py`.

**Gotcha: the first `--prune` run only seeds manifests, it doesn't delete anything.**
claude-client's prune only removes entries that were in a *previous* manifest; a mirror with
no manifests yet (e.g. this repo's history before this feature) has nothing to diff against.
That first run is harmless and necessary — after it, `--prune` prunes going forward. Any
orphans that predate the first manifest are cleaned up once via `--reconcile`, not by
`--prune`.

**There's no dry-run for `--prune`** — claude-client deletes internally and offers no
preview hook. Lean on git instead: run manually, inspect before the nightly
`scripts/commit-backup.sh` would commit it:

```bash
uv run claude-backup --prune
git -C "$CLAUDE_BACKUP_DIR" status --short
git -C "$CLAUDE_BACKUP_DIR" diff --stat
# looks wrong? undo before anything commits it:
git -C "$CLAUDE_BACKUP_DIR" checkout -- . && git -C "$CLAUDE_BACKUP_DIR" clean -fd
```

`--reconcile` logs every path it removes at INFO before deleting it, so the journal is a
readable record even after it's committed. Recovering something pruned in error, any time
after it's committed:

```bash
git -C "$CLAUDE_BACKUP_DIR" log --diff-filter=D --name-only   # find which commit deleted it
git -C "$CLAUDE_BACKUP_DIR" show <commit>~1:<path>             # view its last content
git -C "$CLAUDE_BACKUP_DIR" checkout <commit>~1 -- <path>      # restore it
```

## Scheduling — systemd user timer

Chosen over cron because `Persistent=true` reruns a backup that was missed while the
machine was off, and `journalctl` gives you a searchable log for free.

Unit files live in `systemd/` in this repo:

- `claude-backup.service` — oneshot, loads `.env.backup`, runs `uv run claude-backup`, then
  `scripts/commit-backup.sh`.
- `claude-backup.timer` — fires daily at 23:30, `Persistent=true`.

## Git-versioned history

The backup directory (`$CLAUDE_BACKUP_DIR`, default `./claude-backup`) is its own git
repository, nested inside this repo but ignored by it (`.gitignore` → `/backup/`). After
every successful run, `scripts/commit-backup.sh` does `git add -A` and commits with
`--allow-empty`, so there is one commit per run even on nights nothing changed — the
history doubles as a record that the backup actually ran that day. The script
initializes the repository on first run if it doesn't exist yet.

```bash
git -C "$CLAUDE_BACKUP_DIR" log --oneline        # daily snapshots
git -C "$CLAUDE_BACKUP_DIR" diff <commit>~1 <commit>   # what changed that night
git -C "$CLAUDE_BACKUP_DIR" fsck                 # sanity-check the repository
```

Install:

```bash
mkdir -p ~/.config/systemd/user
ln -s /home/guido/projects/claude-web-backup/systemd/claude-backup.service ~/.config/systemd/user/
ln -s /home/guido/projects/claude-web-backup/systemd/claude-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-backup.timer

# so the timer fires even when logged out:
loginctl enable-linger "$USER"
```

Check status:

```bash
systemctl --user list-timers claude-backup.timer
systemctl --user status claude-backup.service
journalctl --user -u claude-backup.service
```

Run it once immediately (without waiting for the timer):

```bash
systemctl --user start claude-backup.service
```

## Development

```bash
uv run ruff check --fix .   # lint
uv run pytest tests/ -v     # tests
```

`claude-client` and `logger` are git dependencies (see `[tool.uv.sources]` in
`pyproject.toml`), not local paths — a future CI workflow will need to resolve them the
same way it does on this machine, and GitHub Actions runners don't have the sibling repos
checked out. `uv.lock` pins the exact commit of each; `uv sync` alone won't pick up a new
commit on the sibling repo's tracked branch. Run `uv lock --upgrade-package claude-client
--upgrade-package logger` to re-resolve against their latest commits, then `uv sync`.

**Testing an unpushed change to a sibling repo.** There's no persistent local override —
`uv` only reads `[tool.uv.sources]` from `pyproject.toml`, and `sources` isn't a valid key
in `uv.toml` (verified: uv rejects it there as "only applicable in the context of a
project"). Push the sibling repo's change to a branch and point the source at that branch
instead (`{ git = "...", branch = "your-branch" }`), `uv sync`, test — then switch back to
`branch = "main"` once it merges. This is the safer default: it can be committed and shared
without breaking anyone else's `uv sync`.

For a quick local-only check, you can instead temporarily edit the source to a local path
(`{ path = "/home/guido/projects/claude-client" }`), `uv sync`, test — then revert to the
git source before committing. This re-introduces the machine-specific absolute path the
git-source migration removed, and nothing stops it from being committed by accident, so
prefer the branch-pin approach above unless you need a fast local iteration loop.

**Careful with `--upgrade-package`.** It pulls in whatever the sibling repo's tracked
branch currently has, which may be newer (or broken) relative to what this backup was
written against. `uv run` and the systemd service only pick up the new commit after a
sync, so a stale lockfile is the *safe* failure mode. If you upgrade, verify a manual
`uv run claude-backup` afterwards — don't let a fresh upgrade run silently into the
nightly timer without checking the logs.
