# claude-web-backup

A local, automatic nightly backup of Claude.ai projects and their conversations, so
web-side data is never the single point of failure.

Depends on [`claude-client`](https://github.com/guidodinello/claude-client) (a sibling
project, git dependency) for all Claude.ai API access — this project is just the
orchestration, config, and scheduling on top of it.

## What it reuses from claude-client

- `ClaudeClient.projects.pull_all(out_dir)` — pulls every project across every
  chat-capable org on an account in one call. Per project it writes:
  `project.md` (name, description, instructions, memory, controls), `docs/` (one file per
  knowledge doc), `conversations/` (one file per conversation). It **overwrites but never
  deletes** — accumulate-only, which is what you want from a backup. All the multi-org
  scoping is handled inside the SDK; this project just calls it once per account and maps
  the result into a report.

Scope: project-scoped conversations only. Standalone (non-project) chats aren't backed up
yet — see [`roadmap.md`](roadmap.md).

## Target layout

```
$CLAUDE_BACKUP_DIR/
  personal/
    {project-slug}/
      project.md          # title, description, instructions, memory, controls
      docs/                # knowledge files
      conversations/        # one .md per conversation
  work/
    {project-slug}/ ...
```

`{account_slug}` comes from the `CLAUDE_BACKUP_TOKEN_<SLUG>` env var name (lowercased).
`{project-slug}` is the sanitized project name (`claude_client.render.slugify`). A web-side
project rename orphans the old local dir — harmless for a plain mirror; the old copy is
just stale, not lost.

History model: **plain mirror**. Each run overwrites in place; there's no git commit and no
dated snapshots. Nothing removed on the web is ever deleted locally, so it still behaves as
an accumulate-only backup — it just has no diff history between runs.

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

## Running manually

```bash
uv run claude-backup                # uses CLAUDE_BACKUP_DIR / ./claude-backup
uv run claude-backup --help
```

Exit code is non-zero if **any** account or project failed to back up — a silent backup
is worse than none. Failures are logged and (best-effort, if `notify-send` is available)
surfaced as a desktop notification. One failing project never aborts the rest of the run;
failures are collected and reported at the end.

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
`pyproject.toml`), not local paths — `uv sync` needs to resolve them the same way in CI as
on this machine, and GitHub Actions runners don't have the sibling repos checked out.
`uv.lock` pins the exact commit of each; `uv sync` alone won't pick up a new commit on the
sibling repo's tracked branch. Run `uv lock --upgrade-package claude-client
--upgrade-package logger` to re-resolve against their latest commits, then `uv sync`.

**Testing an unpushed change to a sibling repo.** There's no persistent local override —
`uv` only reads `[tool.uv.sources]` from `pyproject.toml`, and `sources` isn't a valid key
in `uv.toml` (verified: uv rejects it there as "only applicable in the context of a
project"). To test against code that isn't pushed yet, temporarily edit the source back to
a local path (`{ path = "/home/guido/projects/claude-client" }`), `uv sync`, test — then
revert to the git source before committing. For anything more than a quick check, push the
sibling repo's change to a branch and point the source at that branch instead
(`{ git = "...", branch = "your-branch" }`); switch back to `branch = "main"` once it
merges.

**Careful with `--upgrade-package`.** It pulls in whatever the sibling repo's tracked
branch currently has, which may be newer (or broken) relative to what this backup was
written against. `uv run` and the systemd service only pick up the new commit after a
sync, so a stale lockfile is the *safe* failure mode. If you upgrade, verify a manual
`uv run claude-backup` afterwards — don't let a fresh upgrade run silently into the
nightly timer without checking the logs.
