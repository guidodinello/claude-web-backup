# Roadmap

- [x] **Standalone (non-project) chats** — done 2026-09-04, via two sequential PRs.
  `claude-client` #17 added `ConversationsResource.list_standalone()`/`pull_standalone()`,
  verified live against a previously-unwrapped endpoint
  (`GET /organizations/{org}/chat_conversations`) that lists every conversation in an org —
  project-scoped and standalone alike, distinguished by `project_uuid` — and fans out over
  every chat-capable org the same way `pull_all` does. This repo's PR then threaded it
  through `backup_account` into a new `{account}/conversations/` directory, reusing the
  existing `--prune`/`CLAUDE_BACKUP_PRUNE` opt-in. Also fixed a `--reconcile` landmine
  found before it shipped: the account-level sweep would have `rmtree`'d the new directory
  wholesale on its first run (absent from the project manifest, not in any keep-list) — now
  reserved from wholesale removal while still swept internally against its own manifest.
  First real run pulled all 403 standalone conversations on this account; a second run
  correctly reported them all "unchanged" via the incremental-pull manifest.
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
- [x] **Token auto-refresh** — won't do, decided 2026-09-04 after investigating four angles:
  1) Impersonating Claude Code's OAuth flow — ruled out: Anthropic explicitly bans using
     subscription OAuth tokens outside the official Claude Code client (server-side
     enforcement since 2026-01-09), and it authenticates to the wrong surface anyway
     (mints a Console API key for the Messages API, not a claude.ai web session).
  2) Scripting claude.ai's own login flow headlessly — same-surface auth, no ToS issue, but
     judged too much hassle (login challenge/Cloudflare to reproduce and maintain).
  3) Checked whether the session cookie has sliding/idle expiry — verified empirically that
     an authenticated request returns no `Set-Cookie` for `sessionKey` (only Cloudflare's
     unrelated `__cf_bm`). It's a fixed absolute TTL; the nightly backup's own traffic
     doesn't extend it.
  4) Piggybacking on an already-logged-in browser's cookie via extension APIs
     (`chrome.cookies`/`browser.cookies`, which can read `HttpOnly` cookies) — technically
     sound and ToS-clean, but this machine only has Firefox installed, and Firefox's
     unsigned/dev-mode WebExtensions are temporary (unloaded every restart unless
     submitted through Mozilla's signing process). Combined with needing a bridge to get
     the cookie out of the extension sandbox, this lands in the same effort bucket as (2).

  Staying with manual re-paste into `.env.backup` on the loud failure — happens once every
  few weeks, takes 30 seconds by hand. Revisit only if Anthropic ships an official
  personal-data-export API or Chrome becomes available on this machine.
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
