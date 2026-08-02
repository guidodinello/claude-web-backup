"""Unit tests for the backup pipeline.

claude-client is mocked at its public API (`ClaudeClient.projects.pull_all`),
so internal changes in the SDK (transport, HTTP, resource layout) can't break
these tests. Multi-org fanout, output layout, and per-project failure isolation
are claude-client's own test coverage, not duplicated here.
"""

from unittest.mock import MagicMock, patch

from claude_client import AuthError
from claude_client.render import slugify

from claude_web_backup.backup import Account, backup_account, load_accounts, run_backup

TOKEN = "sk-ant-sid01-test"


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
    mock_client_cls.return_value.projects.pull_all.assert_called_once_with(tmp_path / "personal")


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
