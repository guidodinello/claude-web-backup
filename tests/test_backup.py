"""Unit tests for the backup pipeline — HTTP layer mocked via unittest.mock."""

from unittest.mock import MagicMock, patch

from claude_client.render import slugify
from curl_cffi import requests as cffi_requests

from claude_web_backup.backup import Account, backup_account, load_accounts

TOKEN = "sk-ant-sid01-test"
ORG_ID = "org-uuid"
OTHER_ORG_ID = "other-org-uuid"
PROJECT_ID = "proj-uuid"
DOC_UUID = "doc-uuid"

ORGS_RESPONSE = [{"uuid": ORG_ID, "capabilities": ["chat"], "name": "Test Org"}]
TWO_ORGS_RESPONSE = [
    *ORGS_RESPONSE,
    {"uuid": OTHER_ORG_ID, "capabilities": ["chat"], "name": "Other Org"},
]
PROJECTS_RESPONSE = [
    {"uuid": PROJECT_ID, "name": "My Project", "description": "", "prompt_template": ""}
]
PROJECT_RESPONSE = {
    "uuid": PROJECT_ID,
    "name": "My Project",
    "description": "A test project",
    "prompt_template": "Be helpful.",
}
MEMORY_RESPONSE = {"memory": "Auto-memory content", "controls": [], "updated_at": "2024-01-01"}
DOC_META = {"uuid": DOC_UUID, "file_name": "notes.md", "created_at": "2024-01-01"}
DOC_FULL = {**DOC_META, "content": "hello world"}
CONV_PAGE_EMPTY = {
    "data": [],
    "pagination": {"total": 0, "limit": 30, "offset": 0, "has_more": False},
}


def _mock_response(json_data, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data
    if status_code >= 400:
        r.raise_for_status = MagicMock(side_effect=Exception(f"HTTP {status_code}"))
    else:
        r.raise_for_status = MagicMock()
    return r


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
# backup_account is now a thin wrapper over ClaudeClient.export_all_projects_to_dir,
# which is tested thoroughly in claude-client's own test suite (multi-org fanout,
# per-project failure isolation, output layout). These tests just confirm the wrapper
# maps that primitive's dict[str, bool] result into BackupReport correctly.


@patch("claude_client._transport.requests")
def test_backup_account_writes_expected_layout(mock_req, tmp_path):
    account = Account(slug="personal", token=TOKEN)

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # export_all_projects_to_dir -> list_organizations
        _mock_response(PROJECTS_RESPONSE),  # -> projects in org
        _mock_response(PROJECT_RESPONSE),  # export: get_project
        _mock_response(MEMORY_RESPONSE),  # get_memory
        _mock_response([DOC_META]),  # list_docs
        _mock_response(DOC_FULL),  # get_doc
        _mock_response(CONV_PAGE_EMPTY),  # list_conversations
    ]

    report = backup_account(account, tmp_path)

    assert report.ok
    assert report.backed_up == ["personal/My Project"]

    out = tmp_path / "personal" / "my-project"
    assert (out / "project.md").exists()
    assert (out / "docs" / "notes.md").exists()
    assert (out / "conversations").exists()


@patch("claude_client._transport.requests")
def test_backup_account_iterates_all_chat_capable_orgs(mock_req, tmp_path):
    account = Account(slug="personal", token=TOKEN)
    project_a = {"uuid": "proj-a", "name": "Project A", "description": "", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project B", "description": "", "prompt_template": ""}

    mock_req.get.side_effect = [
        _mock_response(TWO_ORGS_RESPONSE),  # export_all_projects_to_dir -> list_organizations
        _mock_response([project_a]),  # -> org 1 projects
        _mock_response([project_b]),  # -> org 2 projects
        _mock_response(project_a),  # export proj-a: get_project
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),  # list_docs
        _mock_response(CONV_PAGE_EMPTY),
        _mock_response(project_b),  # export proj-b: get_project
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(CONV_PAGE_EMPTY),
    ]

    report = backup_account(account, tmp_path)

    assert report.ok
    assert sorted(report.backed_up) == ["personal/Project A", "personal/Project B"]


@patch("claude_client._transport.requests")
def test_backup_account_one_project_failure_does_not_abort_others(mock_req, tmp_path):
    account = Account(slug="personal", token=TOKEN)
    project_a = {"uuid": "proj-a", "name": "Project A", "description": "", "prompt_template": ""}
    project_b = {"uuid": "proj-b", "name": "Project B", "description": "", "prompt_template": ""}

    mock_req.get.side_effect = [
        _mock_response(ORGS_RESPONSE),  # export_all_projects_to_dir -> list_organizations
        _mock_response([project_a, project_b]),  # -> projects in org
        cffi_requests.exceptions.RequestException("boom"),  # pull proj-a: get_project raises
        _mock_response(project_b),  # export proj-b: get_project
        _mock_response(MEMORY_RESPONSE),
        _mock_response([]),
        _mock_response(CONV_PAGE_EMPTY),
    ]

    report = backup_account(account, tmp_path)

    assert not report.ok
    assert report.failures == ["personal/Project A"]
    assert report.backed_up == ["personal/Project B"]


@patch("claude_client._transport.requests")
def test_backup_account_auth_error_listing_projects_is_recorded(mock_req, tmp_path):
    account = Account(slug="personal", token=TOKEN)
    mock_req.get.return_value = _mock_response({}, status_code=401)

    report = backup_account(account, tmp_path)

    assert not report.ok
    assert report.backed_up == []
    assert any("personal" in f for f in report.failures)
