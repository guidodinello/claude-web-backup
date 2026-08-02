# Roadmap

- **Standalone (non-project) chats.** The backup is currently project-scoped only —
  conversations that don't belong to any project aren't captured. Needs a new
  "list all chat_conversations for an account" method in `claude-client` (the API only
  exposes conversations scoped to a project today, via `conversations_v2`) plus a
  top-level `conversations/` folder per account in the backup layout.
- **Version history.** Currently a plain mirror (overwrite in place, no history). Options:
  git-committed mirror (nightly `git add -A && git commit`, free diffs, tiny storage since
  it's just text) or dated snapshots (`{backup}/{date}/...`, simpler but no diffing and
  much more disk).
- **Pruning.** `export_project_to_dir` never deletes, so projects/docs/conversations
  removed on the web stay in the local backup forever (arguably correct for a backup, but
  worth an explicit opt-in prune mode for people who want a true mirror).
- **Token auto-refresh.** Session tokens expire after some weeks; today that's handled by
  failing loudly (non-zero exit + notify-send) and requiring a manual token re-paste into
  `.env.backup`. Could investigate refresh-token flows if the unofficial API exposes one.
- **Revisit coupling when `claude-client` lands its tracked pull improvements.** When
  `claude-client` ships the manifest-based network-incremental pull (tracked in
  `claude-client/docs/bugs/pull-re-fetches-unchanged-content.md`), re-verify the backup
  behavior documented here: the manifest skip changes "web is source of truth" semantics
  (a local edit won't be overwritten when the web hasn't changed), and `pull_all` may
  gain statuses worth surfacing in the per-account report. The progress-bar consolidation
  (`docs/bugs/pull-progress-bars-accumulate.md`) is cosmetic — a sanity check under
  systemd is all that's needed.
