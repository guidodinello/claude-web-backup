# Roadmap

- [ ] **Standalone (non-project) chats.** The backup is currently project-scoped only —
  conversations that don't belong to any project aren't captured. Needs a new
  "list all chat_conversations for an account" method in `claude-client` (the API only
  exposes conversations scoped to a project today, via `conversations_v2`) plus a
  top-level `conversations/` folder per account in the backup layout.
- [x] **Version history** — done 2026-08-17: `backup/` is its own git repo, committed nightly
  via `scripts/commit-backup.sh` (`ExecStartPost` in the systemd service) — one
  `--allow-empty` commit per run even when nothing changed, so history doubles as proof
  the backup ran. (Originally: plain mirror with no history; options were a git-committed
  mirror or dated snapshots.)
- [x] **Pruning** — done 2026-09-03: opt-in via `--prune`/`CLAUDE_BACKUP_PRUNE=1`, which
  threads `prune=True` into `claude-client`'s `pull_all` (manifest-based, fixed upstream in
  `edf0550`; tracked in `claude-client/docs/bugs/pull-never-prunes-deleted-items.md`). Since
  that mechanism only prunes entries recorded in a *previous* manifest — and the existing
  mirror had none — added a one-time `--reconcile` sweep (new code, this repo) to clean up
  pre-existing orphans by diffing disk against the manifests claude-client now writes. First
  real run found and removed 88 stale docs accumulated before manifests existed; 0 project
  dirs were orphaned. Off by default, gated on a fully successful pull, and recoverable via
  the mirror's git history (deletions land in that night's commit).
- [ ] **Token auto-refresh.** Session tokens expire after some weeks; today that's handled by
  failing loudly (non-zero exit + notify-send) and requiring a manual token re-paste into
  `.env.backup`. Could investigate refresh-token flows if the unofficial API exposes one.
- [x] **Revisit coupling with `claude-client`'s pull improvements** — done 2026-09-03,
  alongside the pruning upgrade (same `uv lock --upgrade-package claude-client` to `edf0550`).
  Confirmed via a manual run: the manifest-based incremental pull works as documented, and
  the semantic change is real — "web is source of truth" becomes "web is source of truth
  *when it changed*" (a local edit now survives unless `force=True`; `--force` isn't wired
  up in this repo yet, since nothing here needed it). `pull_all` still only returns
  `dict[str, bool]`, so a richer per-account report (e.g. what was pruned) remains an
  upstream ask, not something worth hacking around downstream — see `BackupReport.pruned`'s
  docstring in `backup.py` for why. The progress-bar consolidation
  (`docs/bugs/pull-progress-bars-accumulate.md`) is still open but cosmetic.
