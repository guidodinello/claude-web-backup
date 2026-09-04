"""Unit tests for the backup pipeline.

claude-client is mocked at its public API (`ClaudeClient.projects.pull_all`),
so internal changes in the SDK (transport, HTTP, resource layout) can't break
these tests. Multi-org fanout, output layout, and per-project failure isolation
are claude-client's own test coverage, not duplicated here.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from claude_client import AuthError
from claude_client.render import slugify
from curl_cffi.requests.exceptions import DNSError

from claude_web_backup.backup import (
    Account,
    _env_flag,
    _reconcile_account,
    backup_account,
    load_accounts,
    run_backup,
)

TOKEN = "sk-ant-sid01-test"


def _write_manifest(directory, filenames: list[str]) -> None:
    """Write a minimal claude-client-style manifest naming these filenames."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        f"uuid-{i}": {"filename": name, "updated_at": ""} for i, name in enumerate(filenames)
    }
    (directory / ".claude-pull-manifest.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------- slugify
# (slugify itself lives in and is tested by claude-client; this just sanity-checks
# the import claude-web-backup relies on still resolves.)


def test_slugify_importable_from_claude_client():
    assert slugify("My Cool Project!") == "my-cool-project"


# ------------------------------------------------------------------ load_accounts


def test_load_accounts_reads_prefixed_env_vars(monkeypatch):
    monkeypatch.setenv("CLAUDE_BACKUP_TOKEN_PERSONAL", "tok-a")
    monkeypatch.setenv("CLAUDE_BACKUP_TOKEN_WORK", "tok-b")
    monkeypatch.setenv("UNRELATED_VAR", "ignored")

    accounts = load_accounts()

    assert accounts == [
        Account(slug="personal", token="tok-a"),
        Account(slug="work", token="tok-b"),
    ]


def test_load_accounts_skips_empty_values(monkeypatch):
    monkeypatch.setenv("CLAUDE_BACKUP_TOKEN_PERSONAL", "")
    monkeypatch.setenv("CLAUDE_BACKUP_TOKEN_WORK", "tok-b")

    accounts = load_accounts()

    assert accounts == [Account(slug="work", token="tok-b")]


def test_load_accounts_ignores_unrelated_env_vars(monkeypatch):
    monkeypatch.delenv("CLAUDE_BACKUP_TOKEN_PERSONAL", raising=False)
    monkeypatch.delenv("CLAUDE_BACKUP_TOKEN_WORK", raising=False)
    monkeypatch.setenv("UNRELATED_VAR", "ignored")

    assert load_accounts() == []


# --------------------------------------------------------------------- backup_account
#
# backup_account is a thin wrapper over ClaudeClient.projects.pull_all, which
# returns dict[project_name, bool]. These tests confirm the wrapper maps that
# dict into BackupReport correctly; the pull itself is claude-client's job.


def _mock_client(results: dict[str, bool] | None = None, *, auth_error: bool = False) -> MagicMock:
    client = MagicMock()
    if auth_error:
        client.projects.pull_all.side_effect = AuthError("Session token is invalid or expired.")
    else:
        client.projects.pull_all.return_value = results or {}
    return client


@patch("claude_web_backup.backup.ClaudeClient")
def test_backup_account_maps_successful_projects_to_report(mock_client_cls, tmp_path):
    mock_client_cls.return_value = _mock_client({"Project A": True, "Project B": True})

    report = backup_account(Account(slug="personal", token=TOKEN), tmp_path)

    assert report.ok
    assert sorted(report.backed_up) == ["personal/Project A", "personal/Project B"]
    assert report.failures == []
    mock_client_cls.assert_called_once_with(TOKEN)
    mock_client_cls.return_value.projects.pull_all.assert_called_once_with(
        tmp_path / "personal", prune=False
    )


@patch("claude_web_backup.backup.ClaudeClient")
def test_backup_account_collects_failures_alongside_successes(mock_client_cls, tmp_path):
    mock_client_cls.return_value = _mock_client({"Project A": False, "Project B": True})

    report = backup_account(Account(slug="personal", token=TOKEN), tmp_path)

    assert not report.ok
    assert report.failures == ["personal/Project A"]
    assert report.backed_up == ["personal/Project B"]


@patch("claude_web_backup.backup.ClaudeClient")
def test_backup_account_records_auth_error_as_account_failure(mock_client_cls, tmp_path):
    mock_client_cls.return_value = _mock_client(auth_error=True)

    report = backup_account(Account(slug="personal", token=TOKEN), tmp_path)

    assert not report.ok
    assert report.backed_up == []
    assert any("personal" in f for f in report.failures)


@patch("claude_web_backup.backup.ClaudeClient")
def test_backup_account_records_unexpected_error_as_account_failure(mock_client_cls, tmp_path):
    client = _mock_client()
    client.projects.pull_all.side_effect = OSError("disk full")
    mock_client_cls.return_value = client

    report = backup_account(Account(slug="personal", token=TOKEN), tmp_path)

    assert not report.ok
    assert report.backed_up == []
    assert report.failures == ["personal: disk full"]


@patch("claude_web_backup.backup.time.sleep")
@patch("claude_web_backup.backup.ClaudeClient")
def test_backup_account_retries_transient_network_error_then_succeeds(
    mock_client_cls, mock_sleep, tmp_path
):
    client = _mock_client()
    client.projects.pull_all.side_effect = [
        DNSError("Could not resolve host: claude.ai"),
        DNSError("Could not resolve host: claude.ai"),
        {"Project A": True},
    ]
    mock_client_cls.return_value = client

    report = backup_account(Account(slug="personal", token=TOKEN), tmp_path)

    assert report.ok
    assert report.backed_up == ["personal/Project A"]
    assert client.projects.pull_all.call_count == 3
    assert mock_sleep.call_count == 2


@patch("claude_web_backup.backup.time.sleep")
@patch("claude_web_backup.backup.ClaudeClient")
def test_backup_account_gives_up_after_max_retries(mock_client_cls, mock_sleep, tmp_path):
    client = _mock_client()
    client.projects.pull_all.side_effect = DNSError("Could not resolve host: claude.ai")
    mock_client_cls.return_value = client

    report = backup_account(Account(slug="personal", token=TOKEN), tmp_path)

    assert not report.ok
    assert client.projects.pull_all.call_count == 4
    assert mock_sleep.call_count == 3


@patch("claude_web_backup.backup.ClaudeClient")
def test_backup_account_empty_results_is_ok(mock_client_cls, tmp_path):
    mock_client_cls.return_value = _mock_client()

    report = backup_account(Account(slug="personal", token=TOKEN), tmp_path)

    assert report.ok
    assert report.backed_up == []
    assert report.failures == []


# ---------------------------------------------------------------------- run_backup
#
# The process-level contract: exit non-zero if any account or project failed —
# a silent backup is worse than none.


@patch("claude_web_backup.backup.load_accounts")
def test_run_backup_no_accounts_is_an_error(mock_accounts):
    mock_accounts.return_value = []

    assert run_backup() == 1


@patch("claude_web_backup.backup._notify")
@patch("claude_web_backup.backup.load_accounts")
@patch("claude_web_backup.backup.backup_account")
def test_run_backup_returns_zero_when_all_succeed(
    mock_backup_account, mock_accounts, mock_notify, tmp_path
):
    mock_accounts.return_value = [Account(slug="personal", token=TOKEN)]
    mock_backup_account.return_value = MagicMock(backed_up=["personal/A"], failures=[])

    assert run_backup(tmp_path) == 0
    mock_notify.assert_not_called()


@patch("claude_web_backup.backup._notify")
@patch("claude_web_backup.backup.load_accounts")
@patch("claude_web_backup.backup.backup_account")
def test_run_backup_returns_one_when_any_failure(
    mock_backup_account, mock_accounts, mock_notify, tmp_path
):
    mock_accounts.return_value = [Account(slug="personal", token=TOKEN)]
    mock_backup_account.return_value = MagicMock(backed_up=["personal/A"], failures=["personal/B"])

    assert run_backup(tmp_path) == 1
    mock_notify.assert_called_once()


# ------------------------------------------------------------------------- prune


@patch("claude_web_backup.backup.ClaudeClient")
def test_backup_account_threads_prune_to_pull_all(mock_client_cls, tmp_path):
    mock_client_cls.return_value = _mock_client({"Project A": True})

    backup_account(Account(slug="personal", token=TOKEN), tmp_path, prune=True)

    mock_client_cls.return_value.projects.pull_all.assert_called_once_with(
        tmp_path / "personal", prune=True
    )


@patch("claude_web_backup.backup.load_accounts")
@patch("claude_web_backup.backup.backup_account")
def test_run_backup_threads_prune_to_backup_account(mock_backup_account, mock_accounts, tmp_path):
    mock_accounts.return_value = [Account(slug="personal", token=TOKEN)]
    mock_backup_account.return_value = MagicMock(backed_up=["personal/A"], failures=[])

    run_backup(tmp_path, prune=True)

    mock_backup_account.assert_called_once_with(
        Account(slug="personal", token=TOKEN), tmp_path, prune=True
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("yes", True),
        ("on", True),
        ("", False),
        ("0", False),
        ("false", False),
        ("garbage", False),
    ],
)
def test_env_flag_parses_truthy_values(monkeypatch, value, expected):
    monkeypatch.setenv("CLAUDE_BACKUP_PRUNE", value)
    assert _env_flag("CLAUDE_BACKUP_PRUNE") is expected


def test_env_flag_defaults_false_when_unset(monkeypatch):
    monkeypatch.delenv("CLAUDE_BACKUP_PRUNE", raising=False)
    assert _env_flag("CLAUDE_BACKUP_PRUNE") is False


@patch("claude_web_backup.backup._reconcile_account")
@patch("claude_web_backup.backup.load_accounts")
@patch("claude_web_backup.backup.backup_account")
def test_run_backup_skips_reconcile_when_account_has_failures(
    mock_backup_account, mock_accounts, mock_reconcile, tmp_path
):
    mock_accounts.return_value = [Account(slug="personal", token=TOKEN)]
    mock_backup_account.return_value = MagicMock(
        ok=False, backed_up=[], failures=["personal: boom"]
    )

    assert run_backup(tmp_path, reconcile=True) == 1
    mock_reconcile.assert_not_called()


@patch("claude_web_backup.backup._reconcile_account")
@patch("claude_web_backup.backup.load_accounts")
@patch("claude_web_backup.backup.backup_account")
def test_run_backup_reconciles_when_account_succeeds(
    mock_backup_account, mock_accounts, mock_reconcile, tmp_path
):
    mock_accounts.return_value = [Account(slug="personal", token=TOKEN)]
    mock_backup_account.return_value = MagicMock(ok=True, backed_up=["personal/A"], failures=[])
    mock_reconcile.return_value = ["personal/stale.md"]

    assert run_backup(tmp_path, reconcile=True) == 0
    mock_reconcile.assert_called_once_with(tmp_path / "personal", tmp_path)


# ------------------------------------------------------------------- _reconcile_account
#
# Pure filesystem logic — no SDK mocking needed. Manifests are hand-written in the
# same shape claude-client's own _manifest.save() produces.


def test_reconcile_removes_orphan_project_directory(tmp_path):
    account_dir = tmp_path / "personal"
    kept = [f"kept-project-{i}" for i in range(9)]
    _write_manifest(account_dir, kept)
    for name in kept:
        (account_dir / name).mkdir(parents=True)
    (account_dir / "orphan-project").mkdir(parents=True)  # 1 of 10 dirs, under the 20% tripwire

    removed = _reconcile_account(account_dir, tmp_path)

    assert not (account_dir / "orphan-project").exists()
    for name in kept:
        assert (account_dir / name).exists()
    assert str(account_dir / "orphan-project") in removed


def test_reconcile_removes_orphan_doc_and_conversation(tmp_path):
    account_dir = tmp_path / "personal"
    project_dir = account_dir / "my-project"
    _write_manifest(account_dir, ["my-project"])
    _write_manifest(project_dir / "docs", ["keep.md"])
    _write_manifest(project_dir / "conversations", ["keep-abcd1234.md"])
    (project_dir / "docs" / "keep.md").write_text("kept")
    (project_dir / "docs" / "stale.md").write_text("stale")
    (project_dir / "conversations" / "keep-abcd1234.md").write_text("kept")
    (project_dir / "conversations" / "stale-deadbeef.md").write_text("stale")
    (project_dir / "project.md").write_text("metadata")

    removed = _reconcile_account(account_dir, tmp_path)

    assert not (project_dir / "docs" / "stale.md").exists()
    assert not (project_dir / "conversations" / "stale-deadbeef.md").exists()
    assert (project_dir / "docs" / "keep.md").exists()
    assert (project_dir / "conversations" / "keep-abcd1234.md").exists()
    assert (project_dir / "project.md").exists()  # never tracked in any manifest, always kept
    assert len(removed) == 2


def test_reconcile_keeps_manifest_sidecars(tmp_path):
    account_dir = tmp_path / "personal"
    _write_manifest(account_dir, ["my-project"])
    (account_dir / "my-project").mkdir(parents=True)
    (account_dir / ".claude-pull-manifest.json.tmp").write_text("{}")
    (account_dir / ".claude-pull-manifest.json.corrupt").write_text("not json")

    removed = _reconcile_account(account_dir, tmp_path)

    assert removed == []
    assert (account_dir / ".claude-pull-manifest.json.tmp").exists()
    assert (account_dir / ".claude-pull-manifest.json.corrupt").exists()


def test_reconcile_refuses_when_manifest_missing(tmp_path):
    account_dir = tmp_path / "personal"
    account_dir.mkdir(parents=True)
    (account_dir / "some-project").mkdir()

    removed = _reconcile_account(account_dir, tmp_path)

    assert removed == []
    assert (account_dir / "some-project").exists()


def test_reconcile_refuses_dir_outside_backup_root(tmp_path):
    outside = tmp_path.parent / "not-under-backup-root"
    outside.mkdir(exist_ok=True)
    _write_manifest(outside, [])

    removed = _reconcile_account(outside, tmp_path)

    assert removed == []


def test_reconcile_never_touches_git(tmp_path):
    account_dir = tmp_path / "personal"
    _write_manifest(account_dir, ["my-project"])
    (account_dir / "my-project").mkdir(parents=True)
    git_dir = account_dir / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main")

    _reconcile_account(account_dir, tmp_path)

    assert (git_dir / "HEAD").exists()


def test_reconcile_tripwire_refuses_when_most_projects_would_be_removed(tmp_path):
    account_dir = tmp_path / "personal"
    # Manifest names only 1 of 5 existing project dirs — looks like a partial/incorrect
    # pull, not genuine web-side deletions. Must refuse rather than rmtree 4 of 5.
    _write_manifest(account_dir, ["kept-project"])
    for name in ["kept-project", "p2", "p3", "p4", "p5"]:
        (account_dir / name).mkdir(parents=True)

    removed = _reconcile_account(account_dir, tmp_path)

    assert removed == []
    for name in ["kept-project", "p2", "p3", "p4", "p5"]:
        assert (account_dir / name).exists()
