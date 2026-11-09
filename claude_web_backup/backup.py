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
from pathlib import Path, PurePosixPath

from claude_client import AuthError, ClaudeClient, ProjectPullResult
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
# pull_all() has no per-project retry entry point — it pulls every project in one call — so
# a per-project retry means re-running the whole account pull. Bounded to one extra pass:
# incremental pulls make re-running cheap for projects that already succeeded (unchanged
# docs/conversations are skipped), but a project still failing after this deserves surfacing
# in the failure report, not more silent retrying.
_PROJECT_RETRY_BACKOFF_SECONDS = 10.0

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

# claude-client saves conversation images under `conversations/attachments/<conv_uuid>/`,
# recording each conversation's saved paths (relative to the conversations dir) in its
# manifest entry's `attachments` field. A download that failed is reported as "failed" in
# the pull results (standalone) and leaves the entry with an empty `updated_at` so the
# next pull retries it (the only trace pull_all leaves for project conversations).
_ATTACHMENTS_DIR = "attachments"
_ATTACHMENTS_KEY = "attachments"
_FAILED_STATUS = "failed"


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
    # pull_standalone actually returns one: filename -> created/updated/unchanged/deleted,
    # plus `attachments/...` keys for images (created/deleted, or "failed").
    standalone: dict[str, str] = field(default_factory=dict)

    @property
    def standalone_conversations(self) -> int:
        """Conversation files in `standalone`, excluding its `attachments/...` keys."""
        return sum(1 for key in self.standalone if not key.startswith(f"{_ATTACHMENTS_DIR}/"))

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


def _retry_failed_projects(
    client: ClaudeClient,
    out_dir: Path,
    *,
    prune: bool,
    results: dict[str, ProjectPullResult],
) -> dict[str, ProjectPullResult]:
    """Retry the account pull once for projects whose result carries an error.

    pull_all() surfaces each project's RequestException via ProjectPullResult.error (see
    claude-client's own fix for this) instead of silently absorbing it, which is what makes
    a targeted retry decision possible here at all. There's still no way to pull just the
    failed subset, so this re-runs pull_all() in full — cheap for the already-succeeded
    projects, since claude-client's incremental pull skips unchanged docs/conversations.
    """
    failed = {name: result for name, result in results.items() if not result}
    if not failed:
        return results

    logger.warning(
        "%d project(s) failed, retrying the account pull once: %s",
        len(failed),
        ", ".join(f"{name} ({result.error})" for name, result in failed.items()),
    )
    time.sleep(_PROJECT_RETRY_BACKOFF_SECONDS)
    retry_results = client.projects.pull_all(out_dir, prune=prune)
    return {
        **results,
        **{name: retry_results[name] for name in failed if retry_results.get(name)},
    }


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
        results = _retry_failed_projects(client, out_dir, prune=prune, results=results)
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

    for name, result in results.items():
        label = f"{account.slug}/{name}"
        if result:
            report.backed_up.append(label)
            logger.info("Backed up %s", label)
        else:
            logger.error("Failed to back up %s: %s", label, result.error)
            report.failures.append(label)

    report.failures += _failed_attachments(account.slug, out_dir, report.standalone)
    return report


def _failed_attachments(account_slug: str, out_dir: Path, standalone: dict[str, str]) -> list[str]:
    """One failure label per image download that failed this run, for the whole account.

    Standalone conversations report it directly ("failed" under the image's path stem).
    pull_all discards per-project file results (ProjectPullResult carries only ok/error),
    so for project conversations the only trace is claude-client's retry marker in the
    conversations manifest: an entry scanned for attachments whose `updated_at` was blanked.
    One label per such conversation, since the stems themselves aren't recorded there.
    Never raises on a missing/odd manifest — a failed image must not abort anything else.
    """
    failures = [
        f"{account_slug}/conversations/{key}: image download failed"
        for key, status in standalone.items()
        if status == _FAILED_STATUS
    ]
    for slug in sorted(_manifest_filenames(out_dir) or ()):
        conversations_dir = out_dir / slug / "conversations"
        entries = _load_manifest(conversations_dir) or {}
        failures += [
            f"{account_slug}/{slug}/conversations/{entry.get('filename', uuid)}: "
            "image download failed"
            for uuid, entry in sorted(entries.items())
            if _ATTACHMENTS_KEY in entry and entry.get("updated_at") == ""
        ]
    for failure in failures:
        logger.error("Failed to back up %s", failure)
    return failures


def _load_manifest(directory: Path) -> dict[str, dict] | None:
    """claude-client's manifest for `directory` as {uuid: entry}, or None if unknown.

    None means "we have no idea what belongs here" — callers must not delete anything
    in that case. A missing/corrupt manifest is never read as "everything is stale".
    """
    path = directory / _MANIFEST_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Unreadable manifest %s, treating as unknown: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("Unexpected manifest shape in %s, treating as unknown", path)
        return None
    return {uuid: entry for uuid, entry in raw.items() if isinstance(entry, dict)}


def _manifest_filenames(directory: Path) -> set[str] | None:
    """Filenames claude-client's manifest claims for `directory`, or None if unknown."""
    entries = _load_manifest(directory)
    if entries is None:
        return None
    return {entry["filename"] for entry in entries.values()}


def _attachment_keep_set(entries: dict[str, dict]) -> dict[str, set[str]] | None:
    """{conv_uuid: {image filename}} the manifest claims under `attachments/`, or None.

    None (fail closed: keep everything under attachments/) unless every entry has a
    well-formed `attachments` list whose paths are all `attachments/<that uuid>/<name>`.
    An entry without the field was written by a pre-attachments client, so its images
    (if any) are simply not known yet.
    """
    keep: dict[str, set[str]] = {}
    for uuid, entry in entries.items():
        paths = entry.get(_ATTACHMENTS_KEY)
        if not isinstance(paths, list) or uuid in {"", ".", ".."}:
            return None
        names = set()
        for relpath in paths:
            if not isinstance(relpath, str):
                return None
            parts = PurePosixPath(relpath).parts
            if len(parts) != 3 or parts[:2] != (_ATTACHMENTS_DIR, uuid):
                return None
            names.add(parts[2])
        keep[uuid] = names
    return keep


def _reconcile_conversations_dir(conversations_dir: Path) -> list[str]:
    """Sweep a conversations dir and its `attachments/` tree against its manifest.

    `attachments/` itself is never removed wholesale. Inside it, a uuid dir of a
    conversation absent from the manifest goes, and so does any file not listed for its
    conversation — but only when the manifest fully describes the attachments (see
    `_attachment_keep_set`); otherwise nothing under `attachments/` is touched.
    """
    entries = _load_manifest(conversations_dir)
    if entries is None:
        logger.warning("Reconcile: no manifest in %s — skipping", conversations_dir)
        return []
    filenames = {entry["filename"] for entry in entries.values()}
    removed = _sweep(conversations_dir, filenames | {_ATTACHMENTS_DIR})

    attachments_dir = conversations_dir / _ATTACHMENTS_DIR
    if not attachments_dir.is_dir():
        return removed
    keep = _attachment_keep_set(entries)
    if keep is None:
        logger.warning(
            "Reconcile: manifest in %s doesn't list attachments for every conversation "
            "(pre-attachments pull?) — leaving %s untouched",
            conversations_dir,
            attachments_dir,
        )
        return removed
    removed += _sweep(attachments_dir, set(keep))
    for uuid, names in sorted(keep.items()):
        conversation_attachments = attachments_dir / uuid
        if conversation_attachments.is_dir():
            removed += _sweep(conversation_attachments, names)
    return removed


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
        docs_dir = project_dir / "docs"
        if docs_dir.is_dir():
            names = _manifest_filenames(docs_dir)
            if names is None:
                logger.warning("Reconcile: no manifest in %s — skipping", docs_dir)
            else:
                removed += _sweep(docs_dir, names)
        if (project_dir / "conversations").is_dir():
            removed += _reconcile_conversations_dir(project_dir / "conversations")

    # Account-level standalone conversations: reserved from wholesale removal above, but
    # still swept internally against its own manifest — same treatment as a project's
    # conversations subdir, just one level up.
    standalone_dir = account_dir / "conversations"
    if standalone_dir.is_dir():
        removed += _reconcile_conversations_dir(standalone_dir)

    logger.info("Reconcile: %d orphan(s) removed under %s", len(removed), account_dir)
    return removed


def _notify(message: str) -> None:
    """Best-effort desktop notification. No-op if notify-send is unavailable (e.g. headless)."""
    # SubprocessError covers TimeoutExpired: a hung notify-send (e.g. right after resume,
    # before the desktop session is responsive) must not crash the run.
    with contextlib.suppress(OSError, subprocess.SubprocessError):
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
        total_standalone += report.standalone_conversations
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
