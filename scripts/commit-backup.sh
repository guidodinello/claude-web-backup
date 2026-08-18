#!/usr/bin/env bash
# Commit the latest backup snapshot to the backup's own git repo.
# Runs after each successful backup run via systemd ExecStartPost.
# Best-effort: any git failure is logged but never fails the unit, so the
# service result reflects the backup itself, not the commit step.
set -uo pipefail

BACKUP_DIR="${CLAUDE_BACKUP_DIR:-./claude-backup}"

commit_backup() {
    if [ ! -d "$BACKUP_DIR/.git" ]; then
        mkdir -p "$BACKUP_DIR"
        git -C "$BACKUP_DIR" init -q
    fi
    git -C "$BACKUP_DIR" add -A
    git -C "$BACKUP_DIR" commit --allow-empty -m "backup $(date +%Y-%m-%dT%H:%M:%S%z)"
}

if ! commit_backup; then
    echo "warning: failed to commit backup snapshot (see output above); backup itself succeeded" >&2
fi
