#!/usr/bin/env bash
# Commit the latest backup snapshot to the backup's own git repo.
# Runs after each successful backup run via systemd ExecStartPost.
# Best-effort: any git failure is logged but never fails the unit, so the
# service result reflects the backup itself, not the commit step.
set -uo pipefail

BACKUP_DIR="${CLAUDE_BACKUP_DIR:-./claude-backup}"
# Optional second copy: a bare git repo on another disk (e.g. a USB HDD). Pushed to
# when present; skipped with a log line when the disk isn't mounted. Never created
# here, so an unmounted mountpoint can't silently become a mirror on the local disk.
MIRROR="${CLAUDE_BACKUP_MIRROR:-}"

commit_backup() {
    if [ ! -d "$BACKUP_DIR/.git" ]; then
        mkdir -p "$BACKUP_DIR"
        git -C "$BACKUP_DIR" init -q
    fi
    git -C "$BACKUP_DIR" add -A
    git -C "$BACKUP_DIR" commit --allow-empty -m "backup $(date +%Y-%m-%dT%H:%M:%S%z)"
}

push_mirror() {
    [ -n "$MIRROR" ] || return 0
    if [ ! -f "$MIRROR/HEAD" ]; then
        echo "mirror: $MIRROR not reachable (disk not mounted?); skipping, next run catches up" >&2
        return 0
    fi
    git -C "$BACKUP_DIR" push --quiet "$MIRROR" HEAD:refs/heads/main &&
        echo "mirror: pushed to $MIRROR"
}

if ! commit_backup; then
    echo "warning: failed to commit backup snapshot (see output above); backup itself succeeded" >&2
fi
if ! push_mirror; then
    echo "warning: failed to push backup to mirror $MIRROR; local backup is fine" >&2
fi
