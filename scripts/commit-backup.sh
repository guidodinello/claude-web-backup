#!/usr/bin/env bash
# Commit the latest backup snapshot to the backup's own git repo.
# Runs after each successful backup run via systemd ExecStartPost.
set -euo pipefail

BACKUP_DIR="${CLAUDE_BACKUP_DIR:-/home/guido/projects/claude-web-backup/backup}"

git -C "$BACKUP_DIR" add -A
git -C "$BACKUP_DIR" commit --allow-empty -m "backup $(date +%Y-%m-%dT%H:%M:%S%z)"
