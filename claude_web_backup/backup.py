"""Nightly backup pipeline: mirror every project (across accounts/orgs) to disk.

Env-driven so it runs unattended (systemd timer, cron, etc.):

- ``CLAUDE_BACKUP_TOKEN_<SLUG>`` — one per account to back up, e.g.
  ``CLAUDE_BACKUP_TOKEN_PERSONAL``, ``CLAUDE_BACKUP_TOKEN_WORK``.
- ``CLAUDE_BACKUP_DIR`` — output root (default ``./claude-backup``).
- ``CLAUDE_BACKUP_PRUNE`` — set to a truthy value (``1``/``true``/``yes``/``on``) to enable
  pruning on every run, same effect as ``--prune``.

Layout written: ``{out}/{account_slug}/{project_slug}/{project.md,docs/,conversations/}``,
plus ``{out}/{account_slug}/conversations/`` for standalone (non-project) chats, pulled
account-wide across every chat-capable org via ``client.conversations.pull_standalone``.
By default this mirrors ``pull_all``'s accumulate-only semantics — it overwrites but never
deletes, so the backup only ever grows. Pass ``--prune``/``CLAUDE_BACKUP_PRUNE=1`` to also
delete local files/directories removed on the web (claude-client does the deleting; see its
manifest-based pruning). A failing account or project is logged and skipped rather than
aborting the whole run, but any failure makes the process exit non-zero: a silent backup is
worse than none — and it also disables ``--reconcile`` for that run, since the manifests
pruning relies on may not reflect the web if the pull that wrote them was incomplete.
"""

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from claude_client import AuthError, ClaudeClient
from curl_cffi.requests.exceptions import RequestException
from logger import get_logger, init_logging

logger = get_logger(__name__)

_TOKEN_PREFIX = "CLAUDE_BACKUP_TOKEN_"
_DEFAULT_OUT_DIR = "./claude-backup"
_PRUNE_ENV = "CLAUDE_BACKUP_PRUNE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
# Covers transient network conditions (DNS not up yet, connection refused, timeouts) —
# e.g. the systemd timer firing at boot before the network is actually online.
_MAX_ATTEMPTS = 4
_INITIAL_BACKOFF_SECONDS = 5.0

# --reconcile sweep: names claude-client's own manifest sidecar plus the one file it writes
# unconditionally (project.md is tracked in no manifest). Never touched by the sweep.
_MANIFEST_NAME = ".claude-pull-manifest.json"
_ALWAYS_KEEP = frozenset(
    {_MANIFEST_NAME, f"{_MANIFEST_NAME}.tmp", f"{_MANIFEST_NAME}.corrupt", ".git", "project.md"}
)
# Sanity tripwire (see _reconcile_account): refuse a sweep that looks like it's reacting to
# a partial/incorrect manifest rather than genuine web-side deletions.
_MAX_PRUNE_FRACTION = 0.2

# The standalone-conversations directory lives alongside project dirs at the account level
# but isn't itself a project, so it never appears in the account's project-slug manifest.
# Reserved so the sweep never rmtree's it wholesale — it's still swept internally against
# its own manifest, just like any project's docs/conversations subdir. A project literally
# named "Conversations" would collide with this reserved name (claude-client's slug
# resolver has no reserved-name list) — a known, accepted edge case, not engineered around.
_RESERVED_ACCOUNT_DIRS = frozenset({"conversations"})


@dataclass
class Account:
    slug: str
    token: str


@dataclass
class BackupReport:
    backed_up: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    # Populated only by the --reconcile sweep, which is our own code. pull_all returns
    # dict[str, bool] and reports nothing about what it deleted, so an SDK-side prune
    # (--prune without --reconcile) is not observable here — see the module docstring
    # and README for why we don't fabricate a count. The real record is the git commit.
    pruned: list[str] = field(default_factory=list)
    # Filled directly from pull_standalone()'s own return value — a real, complete
    # per-conversation record (unlike pull_all's discarded per-project detail), since
    # pull_standalone actually returns one: filename -> created/updated/unchanged/deleted.
    standalone: dict[str, str] = field(default_factory=dict)

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


def _retry_with_backoff[T](fn: Callable[[], T]) -> T:
    """Retry `fn` with exponential backoff on transient network errors.

    AuthError isn't a RequestException, so it isn't retried — an expired token
    won't magically become valid on the next attempt.
    """
    delay = _INITIAL_BACKOFF_SECONDS
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return fn()
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


def backup_account(account: Account, out_root: str | Path, *, prune: bool = False) -> BackupReport:
    """Back up every project, plus every standalone conversation, in this account.

    Both calls share one try/except deliberately: a total failure in either (not a
    per-project/per-conversation failure — those are already isolated inside the SDK —
    but e.g. disk full) fails the whole account for this run rather than half-completing
    it, matching the existing all-or-nothing granularity for a single account.
    """
    report = BackupReport()
    out_dir = Path(out_root) / account.slug

    client = ClaudeClient(account.token)
    try:
        results = _retry_with_backoff(lambda: client.projects.pull_all(out_dir, prune=prune))
        report.standalone = _retry_with_backoff(
            lambda: client.conversations.pull_standalone(out_dir / "conversations", prune=prune)
        )
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


def _manifest_filenames(directory: Path) -> set[str] | None:
    """Filenames claude-client's manifest claims for `directory`, or None if unknown.

    None means "we have no idea what belongs here" — callers must not delete anything
    in that case. A missing/corrupt manifest is never read as "everything is stale".
    """
    path = directory / _MANIFEST_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return {entry["filename"] for entry in raw.values() if isinstance(entry, dict)}


def _sweep(directory: Path, keep: set[str]) -> list[str]:
    """Delete children of `directory` absent from `keep`/`_ALWAYS_KEEP`. Returns removed paths."""
    removed = []
    for child in sorted(directory.iterdir()):
        if child.name in _ALWAYS_KEEP or child.name in keep:
            continue
        logger.info("Reconcile: removing orphan %s", child)
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed.append(str(child))
    return removed


def _reconcile_account(account_dir: Path, backup_root: Path) -> list[str]:
    """Delete anything under `account_dir` that no pull manifest claims.

    Requires a just-completed, fully successful pull for this account, so the manifests
    it wrote reflect the web as of now — callers must gate on that (see run_backup).
    Never touches `.git`, `project.md`, or manifest sidecars; skips (never deletes) any
    subtree with no manifest, since a missing manifest means the sweep has no reliable
    knowledge of what belongs there.
    """
    account_dir = account_dir.resolve()
    backup_root = backup_root.resolve()
    if account_dir.parent != backup_root or not account_dir.is_dir():
        logger.error(
            "Reconcile: refusing to sweep %s — not a direct account dir under %s",
            account_dir,
            backup_root,
        )
        return []

    project_slugs = _manifest_filenames(account_dir)
    if project_slugs is None:
        logger.error(
            "Reconcile: no %s in %s — run a normal backup first to seed manifests.",
            _MANIFEST_NAME,
            account_dir,
        )
        return []

    existing_project_dirs = [
        p
        for p in account_dir.iterdir()
        if p.is_dir() and p.name != ".git" and p.name not in _RESERVED_ACCOUNT_DIRS
    ]
    if len(project_slugs) < len(existing_project_dirs) * (1 - _MAX_PRUNE_FRACTION):
        logger.error(
            "Reconcile: refusing to sweep %s — manifest names only %d of %d project dirs "
            "(more than %.0f%% would be removed); this looks like a partial pull, not "
            "genuine web-side deletions.",
            account_dir,
            len(project_slugs),
            len(existing_project_dirs),
            _MAX_PRUNE_FRACTION * 100,
        )
        return []

    removed = _sweep(account_dir, project_slugs | _RESERVED_ACCOUNT_DIRS)
    for slug in sorted(project_slugs):
        project_dir = account_dir / slug
        if not project_dir.is_dir():
            continue
        for sub in ("docs", "conversations"):
            sub_dir = project_dir / sub
            if not sub_dir.is_dir():
                continue
            names = _manifest_filenames(sub_dir)
            if names is None:
                logger.warning("Reconcile: no manifest in %s — skipping", sub_dir)
                continue
            removed += _sweep(sub_dir, names)

    # Account-level standalone conversations: reserved from wholesale removal above, but
    # still swept internally against its own manifest — same treatment as a project's
    # docs/conversations subdir, just one level up.
    standalone_dir = account_dir / "conversations"
    if standalone_dir.is_dir():
        names = _manifest_filenames(standalone_dir)
        if names is None:
            logger.warning("Reconcile: no manifest in %s — skipping", standalone_dir)
        else:
            removed += _sweep(standalone_dir, names)

    logger.info("Reconcile: %d orphan(s) removed under %s", len(removed), account_dir)
    return removed


def _notify(message: str) -> None:
    """Best-effort desktop notification. No-op if notify-send is unavailable (e.g. headless)."""
    with contextlib.suppress(FileNotFoundError, OSError):
        subprocess.run(["notify-send", "Claude backup failed", message], check=False, timeout=5)


def run_backup(
    out_dir: str | Path | None = None, *, prune: bool = False, reconcile: bool = False
) -> int:
    """Back up every configured account. Returns a process exit code."""
    out_dir = Path(out_dir or os.getenv("CLAUDE_BACKUP_DIR", _DEFAULT_OUT_DIR))
    accounts = load_accounts()

    if not accounts:
        logger.error(
            "No accounts configured. Set CLAUDE_BACKUP_TOKEN_<SLUG> for each account to back up."
        )
        return 1

    if prune:
        logger.info("Prune enabled: items removed on the web will be deleted locally.")

    all_failures: list[str] = []
    total_backed_up = 0
    total_standalone = 0
    total_pruned = 0
    for account in accounts:
        report = backup_account(account, out_dir, prune=prune)
        total_backed_up += len(report.backed_up)
        total_standalone += len(report.standalone)
        all_failures.extend(report.failures)

        if reconcile:
            if report.ok:
                report.pruned = _reconcile_account(out_dir / account.slug, out_dir)
                total_pruned += len(report.pruned)
            else:
                logger.error(
                    "Reconcile: skipping account '%s' — it had failures this run, so its "
                    "manifests may not reflect the web.",
                    account.slug,
                )

    if all_failures:
        message = f"{len(all_failures)} failure(s):\n" + "\n".join(all_failures)
        logger.error("Backup completed with failures. %s", message)
        _notify(message)
        return 1

    logger.info(
        "Backup complete: %d project(s), %d standalone conversation(s) across %d account(s)%s.",
        total_backed_up,
        total_standalone,
        len(accounts),
        f", {total_pruned} orphan(s) reconciled" if reconcile else "",
    )
    return 0


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


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
    parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "Delete local files/directories removed on the web (off by default; "
            f"also enabled by {_PRUNE_ENV}=1)"
        ),
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help=(
            "One-time sweep: delete anything on disk absent from the pull manifests. "
            "Requires a prior --prune (or plain) run to have written the manifests. "
            "Implies --prune. Run once, inspect the git diff, then stop using it."
        ),
    )
    return parser


def main() -> None:
    init_logging()
    args = _build_parser().parse_args()
    prune = args.prune or args.reconcile or _env_flag(_PRUNE_ENV)
    raise SystemExit(run_backup(args.out, prune=prune, reconcile=args.reconcile))


if __name__ == "__main__":
    main()
