"""Outreach campaigns — stage drafts locally; never send in UAT."""

from __future__ import annotations

from unittest.mock import patch

def _init_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LEXI_DATABASE_PATH", str(tmp_path / "lexi.db"))
    monkeypatch.setenv("LEXI_DRY_RUN", "true")
    monkeypatch.setenv("LEXI_OUTREACH_LIVE_SENDS_ENABLED", "false")
    monkeypatch.setenv("LEXI_OUTREACH_OUTLOOK_DRAFTS_ENABLED", "false")
    # The feature is parked in production. These tests keep its behaviour covered
    # so it can be picked back up without re-deriving it; test_campaigns_are_paused
    # below covers the parked state itself.
    monkeypatch.setenv("LEXI_OUTREACH_CAMPAIGNS_ENABLED", "true")
    import importlib

    import app.config

    importlib.reload(app.config)
    from scripts.init_lexi_db import init_lexi_db

    init_lexi_db(tmp_path / "lexi.db")


def test_parse_pasted_contacts():
    from app.scheduling.outreach_campaign import parse_pasted_contacts

    rows = parse_pasted_contacts(
        "Name, email, company\n"
        "Jane Doe, jane@co.com, Acme\n"
        "bob@other.io\n"
        "bad line without email\n"
        "Jane Doe, jane@co.com, Acme\n"
    )
    assert len(rows) == 2
    assert rows[0]["email"] == "jane@co.com"
    assert rows[0]["name"] == "Jane Doe"
    assert rows[0]["company"] == "Acme"
    assert rows[1]["email"] == "bob@other.io"


def test_ypo_template_personalizes(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    from app.scheduling.outreach_campaign import compose_outreach_email

    composed = compose_outreach_email(
        contact={"name": "Alex Rivera", "email": "a@x.com", "company": "BuildCo"},
        template_key="ypo_the_turn",
    )
    assert "Alex" in composed["subject"] or "YPO" in composed["subject"]
    assert "BuildCo" in composed["body"]
    assert "the Turn" in composed["body"]
    assert "Kory" in composed["body"]


@patch("app.integrations.outlook_email.create_outbound_draft")
@patch("app.integrations.outlook_email.send_outbound_email")
def test_create_campaign_stages_without_send(
    mock_send, mock_draft, tmp_path, monkeypatch
):
    _init_db(tmp_path, monkeypatch)
    mock_draft.return_value = ("dry-run-outreach-draft-abc", "dry-run-no-log")

    from app.scheduling.outreach_campaign import create_outreach_campaign, get_campaign

    result = create_outreach_campaign(
        name="YPO batch",
        goal="podcast intros",
        template_key="ypo_the_turn",
        pasted_list="Sam Lee, sam@firm.com, Firm LLC\nPat, pat@x.com",
    )
    assert result["ok"] is True
    assert result["draft_count"] == 2
    assert result["sends_blocked"] is True
    assert mock_send.call_count == 0
    # Staging calls create_outbound_draft with approved=False (dry-run path)
    assert mock_draft.call_count == 2
    for call in mock_draft.call_args_list:
        assert call.kwargs.get("approved") is False

    detail = get_campaign(result["campaign_id"])
    assert detail is not None
    assert len(detail["drafts"]) == 2
    assert all(d["status"] == "staged" for d in detail["drafts"])


@patch("app.integrations.outlook_email.send_outbound_email")
def test_approve_and_send_never_send(mock_send, tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    from app.scheduling.outreach_campaign import (
        approve_outreach_campaign,
        create_outreach_campaign,
        send_outreach_campaign,
    )

    created = create_outreach_campaign(
        name="Test",
        pasted_list="a@b.com",
        template_key="generic",
    )
    cid = created["campaign_id"]

    approved = approve_outreach_campaign(campaign_id=cid)
    assert approved["ok"] is True
    assert approved["sent"] == 0
    assert approved["sends_blocked"] is True
    assert "nothing was sent" in approved["kory_message"].lower()

    sent = send_outreach_campaign(campaign_id=cid, approved=True)
    assert sent["ok"] is True
    assert sent["sent"] == 0
    assert sent["sends_blocked"] is True
    assert mock_send.call_count == 0


def test_remove_recipient(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    from app.scheduling.outreach_campaign import (
        create_outreach_campaign,
        get_campaign,
        remove_outreach_recipient,
    )

    created = create_outreach_campaign(
        name="Rm",
        pasted_list="keep@x.com\ndrop@x.com",
    )
    remove_outreach_recipient(campaign_id=created["campaign_id"], email="drop@x.com")
    detail = get_campaign(created["campaign_id"])
    statuses = {d["recipient_email"]: d["status"] for d in detail["drafts"]}
    assert statuses["drop@x.com"] == "removed"
    assert statuses["keep@x.com"] == "staged"


def test_campaigns_are_paused_by_default(tmp_path, monkeypatch):
    """The parked state: nothing stages, nothing approves, nothing sends."""
    _init_db(tmp_path, monkeypatch)
    monkeypatch.delenv("LEXI_OUTREACH_CAMPAIGNS_ENABLED", raising=False)
    from app.scheduling.outreach_campaign import (
        approve_outreach_campaign,
        campaigns_paused,
        create_outreach_campaign,
        outreach_sends_blocked,
        remove_outreach_recipient,
        send_outreach_campaign,
    )

    assert campaigns_paused() is True
    # The outer switch subsumes the send switch: a parked feature never gets
    # far enough to consult it.
    assert outreach_sends_blocked() is True

    created = create_outreach_campaign(name="should not stage", pasted_list="a@b.com")
    assert created["ok"] is False
    assert created["paused"] is True
    assert created["campaign_id"] is None

    for result in (
        approve_outreach_campaign(campaign_id="camp-whatever"),
        send_outreach_campaign(campaign_id="camp-whatever", approved=True),
        remove_outreach_recipient(campaign_id="camp-whatever", email="a@b.com"),
    ):
        assert result["paused"] is True
        assert result["sent"] == 0

    # Nothing reached the database — a paused create must not leave a row behind.
    from app.scheduling.outreach_campaign import list_campaigns

    assert not [c for c in list_campaigns()["campaigns"] if c["name"] == "should not stage"]


def test_paused_send_refuses_before_the_approval_gate(tmp_path, monkeypatch):
    """Unapproved + paused answers "campaigns are off", not "needs approval"."""
    _init_db(tmp_path, monkeypatch)
    monkeypatch.delenv("LEXI_OUTREACH_CAMPAIGNS_ENABLED", raising=False)
    from app.scheduling.outreach_campaign import send_outreach_campaign

    result = send_outreach_campaign(campaign_id="camp-whatever", approved=False)
    assert result["paused"] is True
    assert "switched off" in result["kory_message"]


def test_paused_campaign_tools_are_not_registered_with_hermes(monkeypatch):
    """Hermes should not see a tool it cannot use — an unregistered tool cannot
    be offered to Kory or hallucinated into having run."""
    monkeypatch.delenv("LEXI_OUTREACH_CAMPAIGNS_ENABLED", raising=False)
    import importlib

    import hermes_mcp_server

    importlib.reload(hermes_mcp_server)
    names = {tool.name for tool in hermes_mcp_server.mcp._tool_manager.list_tools()}

    parked = {
        "lexi_create_outreach_campaign",
        "lexi_list_outreach_campaigns",
        "lexi_get_outreach_campaign",
        "lexi_approve_outreach_campaign",
        "lexi_send_outreach_campaign",
        "lexi_remove_outreach_recipient",
        "lexi_hubspot_outreach_batch",
    }
    assert not (names & parked), sorted(names & parked)
    # Scheduling and the read-only CRM tools are untouched by the pause.
    assert "lexi_hubspot_find_contacts" in names
    assert "lexi_hubspot_outreach_candidates" in names


def test_teams_outreach_commands_parse():
    from app.bot.teams_text import parse_teams_command

    assert parse_teams_command("outreach") == {"action": "outreach_list"}
    assert parse_teams_command("outreach list") == {"action": "outreach_list"}
    assert parse_teams_command("outreach camp-abc123def0") == {
        "action": "outreach_get",
        "campaign_id": "camp-abc123def0",
    }
    assert parse_teams_command("approve outreach camp-abc123def0") == {
        "action": "outreach_approve",
        "campaign_id": "camp-abc123def0",
    }
    assert parse_teams_command("send outreach camp-abc123def0") == {
        "action": "outreach_send",
        "campaign_id": "camp-abc123def0",
    }


@patch("app.assistant.actions.send_outreach_campaign_action")
@patch("app.assistant.actions.approve_outreach_campaign_action")
def test_teams_handlers_no_cards(mock_approve, mock_send):
    from app.teams.commands import handle_teams_command

    mock_approve.return_value = {
        "ok": True,
        "kory_message": "approved, nothing sent",
        "sent": 0,
    }
    mock_send.return_value = {
        "ok": True,
        "kory_message": "Send blocked",
        "sent": 0,
        "sends_blocked": True,
    }

    r1 = handle_teams_command("approve outreach camp-testhere1")
    assert r1["ok"] is True
    assert "card" not in r1
    assert mock_approve.call_args.kwargs["confirm"] is True

    r2 = handle_teams_command("send outreach camp-testhere1")
    assert r2["ok"] is True
    assert "blocked" in r2["message"].lower() or "Send blocked" in r2["message"]
    assert "card" not in r2
