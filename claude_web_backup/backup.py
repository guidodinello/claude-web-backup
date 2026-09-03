"""Nightly backup pipeline: mirror every project (across accounts/orgs) to disk.

Env-driven so it runs unattended (systemd timer, cron, etc.):

- ``CLAUDE_BACKUP_TOKEN_<SLUG>`` — one per account to back up, e.g.
  ``CLAUDE_BACKUP_TOKEN_PERSONAL``, ``CLAUDE_BACKUP_TOKEN_WORK``.
- ``CLAUDE_BACKUP_DIR`` — output root (default ``./claude-backup``).

Layout written: ``{out}/{account_slug}/{project_slug}/{project.md,docs/,conversations/}``.
Mirrors ``pull_all`` semantics — it overwrites but never deletes, so the backup only ever
grows. A failing account or project is logged and skipped rather than aborting the whole
run, but any failure makes the process exit non-zero: a silent backup is worse than none.
"""

import argparse
import contextlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_client import AuthError, ClaudeClient
from curl_cffi.requests.exceptions import RequestException
from logger import get_logger, init_logging

logger = get_logger(__name__)

_TOKEN_PREFIX = "CLAUDE_BACKUP_TOKEN_"
_DEFAULT_OUT_DIR = "./claude-backup"
# Covers transient network conditions (DNS not up yet, connection refused, timeouts) —
# e.g. the systemd timer firing at boot before the network is actually online.
_MAX_ATTEMPTS = 4
_INITIAL_BACKOFF_SECONDS = 5.0


@dataclass
class Account:
    slug: str
    token: str


@dataclass
class BackupReport:
    backed_up: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def load_accounts() -> list[Account]:
    """Read one Account per CLAUDE_BACKUP_TOKEN_<SLUG> env var, sorted by slug."""
    accounts = []
    for key, value in os.environ.items():
        if key.startswith(_TOKEN_PREFIX) and value:
            slug = key[len(_TOKEN_PREFIX) :].lower()
            accounts.append(Account(slug=slug, token=value))
    return sorted(accounts, key=lambda a: a.slug)


def _pull_all_with_retry(client: ClaudeClient, out_dir: Path) -> dict[str, bool]:
    """Retry `pull_all` with exponential backoff on transient network errors.

    AuthError isn't a RequestException, so it isn't retried — an expired token
    won't magically become valid on the next attempt.
    """
    delay = _INITIAL_BACKOFF_SECONDS
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return client.projects.pull_all(out_dir)
        except RequestException as exc:
            if attempt == _MAX_ATTEMPTS:
                raise
            logger.warning(
                "Network error on attempt %d/%d (retrying in %.0fs): %s",
                attempt,
                _MAX_ATTEMPTS,
                delay,
                exc,
            )
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def backup_account(account: Account, out_root: str | Path) -> BackupReport:
    """Back up every project in every chat-capable org of this account."""
    report = BackupReport()
    out_dir = Path(out_root) / account.slug

    client = ClaudeClient(account.token)
    try:
        results = _pull_all_with_retry(client, out_dir)
    except AuthError as exc:
        logger.error("Account '%s': auth failed: %s", account.slug, exc)
        report.failures.append(f"{account.slug}: {exc}")
        return report
    except Exception as exc:
        logger.exception("Account '%s': backup failed: %s", account.slug, exc)
        report.failures.append(f"{account.slug}: {exc}")
        return report

    if not results:
        logger.warning("Account '%s': no projects found in any chat-capable org", account.slug)

    for name, ok in results.items():
        label = f"{account.slug}/{name}"
        if ok:
            report.backed_up.append(label)
            logger.info("Backed up %s", label)
        else:
            # Per-project failure detail was already logged by export_all_projects_to_dir.
            logger.error("Failed to back up %s", label)
            report.failures.append(label)

    return report


def _notify(message: str) -> None:
    """Best-effort desktop notification. No-op if notify-send is unavailable (e.g. headless)."""
    with contextlib.suppress(FileNotFoundError, OSError):
        subprocess.run(["notify-send", "Claude backup failed", message], check=False, timeout=5)


def run_backup(out_dir: str | Path | None = None) -> int:
    """Back up every configured account. Returns a process exit code."""
    out_dir = out_dir or os.getenv("CLAUDE_BACKUP_DIR", _DEFAULT_OUT_DIR)
    accounts = load_accounts()

    if not accounts:
        logger.error(
            "No accounts configured. Set CLAUDE_BACKUP_TOKEN_<SLUG> for each account to back up."
        )
        return 1

    all_failures: list[str] = []
    total_backed_up = 0
    for account in accounts:
        report = backup_account(account, out_dir)
        total_backed_up += len(report.backed_up)
        all_failures.extend(report.failures)

    if all_failures:
        message = f"{len(all_failures)} failure(s):\n" + "\n".join(all_failures)
        logger.error("Backup completed with failures. %s", message)
        _notify(message)
        return 1

    logger.info(
        "Backup complete: %d project(s) across %d account(s).", total_backed_up, len(accounts)
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-backup",
        description="Back up every Claude.ai project across all configured accounts to disk.",
    )
    parser.add_argument(
        "--out",
        metavar="DIR",
        help="Output directory (default: $CLAUDE_BACKUP_DIR or ./claude-backup)",
    )
    return parser


def main() -> None:
    init_logging()
    args = _build_parser().parse_args()
    raise SystemExit(run_backup(args.out))


if __name__ == "__main__":
    main()
