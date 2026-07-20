# claude-web-backup

A local, automatic nightly backup of Claude.ai projects and their conversations, so
web-side data is never the single point of failure.

Depends on [`claude-client`](../claude-client) (a sibling project, path dependency) for
all Claude.ai API access — this project is just the orchestration, config, and
scheduling on top of it.

## What it reuses from claude-client

- `ClaudeClient.export_all_projects_to_dir(out_dir)` — exports every project across every
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

- `claude-backup.service` — oneshot, loads `.env.backup`, runs `uv run claude-backup`.
- `claude-backup.timer` — fires daily at 23:30, `Persistent=true`.

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

`claude-client` is a path dependency (see `[tool.uv.sources]` in `pyproject.toml`). After
changing its source, `uv sync` alone won't always pick up the new code in this project's
venv — run `uv sync --reinstall-package claude-client` to force it.
