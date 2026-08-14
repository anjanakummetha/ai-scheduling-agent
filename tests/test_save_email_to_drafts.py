"""Parking an email in Kory's Outlook Drafts for him to review later.

Nothing is sent — the draft is the review step. What matters is that it actually
lands in his Drafts folder, so every path here asserts on the read-back rather
than on the create call's reply.
"""

import pytest
from unittest.mock import patch

from app.assistant import actions
from app.integrations import outlook_email


@pytest.fixture(autouse=True)
def _recipient_is_known():
    """These tests are about creating the draft, not vetting the address.

    The recipient guard is exercised explicitly further down; here it would
    otherwise reach the network, and its answer varies with whether the runner
    has HubSpot keys — green locally, red in CI.
    """
    with patch.object(
        outlook_email, "verify_recipient_address",
        return_value={"verified": True, "source": "test"},
    ):
        yield


def _live_settings(mock):
    mock.lexi_dry_run = False
    mock.lexi_write_mode = "kory"
    mock.sandbox_email_loopback = False
    mock.sandbox_mailbox_email = ""
    mock.cc_kory_enabled = True          # on in production — must NOT self-CC
    mock.hubspot_bcc_enabled = False
    mock.kory_cc_email = "kory.mitchell@iconicfounders.com"
    mock.kory_sender_emails = ("kory@ifg.vc", "kory@iconicfounders.com")
    return mock


def _draft(**overrides):
    base = {"id": "draft-1", "subject": "Follow up", "isDraft": True}
    base.update(overrides)
    return base


def test_draft_lands_and_is_confirmed_by_reading_it_back():
    calls = []

    def fake_execute(slug, args, role=None):
        calls.append(slug)
        if slug == "OUTLOOK_CREATE_DRAFT":
            return {"data": {"id": "draft-1"}, "log_id": "log-1"}
        if slug == "OUTLOOK_GET_MESSAGE":
            return {"data": _draft(), "log_id": "log-2"}
        return {"data": {}, "log_id": None}

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool", side_effect=fake_execute),
    ):
        _live_settings(settings)
        result = actions.save_email_to_drafts(
            to_email="anjanakummetha@gmail.com",
            subject="Follow up",
            body="Hi Anjana,\n\nCircling back on this.",
        )

    assert result["ok"] is True
    assert result["verified"] is True
    assert result["draft_id"] == "draft-1"
    assert result["is_draft"] is True
    assert "OUTLOOK_GET_MESSAGE" in calls, "must read the draft back, not trust the reply"
    assert "Saved to your Drafts" in result["kory_message"]


def test_nothing_is_ever_sent():
    """The whole feature is 'do not send'. A send slug here is a bug."""
    calls = []

    def fake_execute(slug, args, role=None):
        calls.append(slug)
        if slug == "OUTLOOK_CREATE_DRAFT":
            return {"data": {"id": "draft-1"}, "log_id": "l"}
        return {"data": _draft(), "log_id": None}

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool", side_effect=fake_execute),
    ):
        _live_settings(settings)
        actions.save_email_to_drafts(
            to_email="anjanakummetha@gmail.com", subject="S", body="B"
        )

    assert not any("SEND" in slug for slug in calls), calls


def test_kory_is_not_cc_d_on_his_own_draft():
    """merge_kory_cc_addresses would add him — right for Lexi's mail, wrong here."""
    captured = {}

    def fake_execute(slug, args, role=None):
        if slug == "OUTLOOK_CREATE_DRAFT":
            captured.update(args)
            return {"data": {"id": "draft-1"}, "log_id": "l"}
        return {"data": _draft(), "log_id": None}

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool", side_effect=fake_execute),
    ):
        _live_settings(settings)
        actions.save_email_to_drafts(
            to_email="anjanakummetha@gmail.com", subject="S", body="B"
        )

    assert captured["to_recipients"] == ["anjanakummetha@gmail.com"]
    assert "kory.mitchell@iconicfounders.com" not in captured.get("cc_recipients", [])


def test_draft_carries_korys_signature():
    captured = {}

    def fake_execute(slug, args, role=None):
        if slug == "OUTLOOK_CREATE_DRAFT":
            captured.update(args)
            return {"data": {"id": "draft-1"}, "log_id": "l"}
        return {"data": _draft(), "log_id": None}

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool", side_effect=fake_execute),
    ):
        _live_settings(settings)
        actions.save_email_to_drafts(
            to_email="anjanakummetha@gmail.com", subject="S", body="Hi there."
        )

    assert captured["is_html"] is True
    assert "Kory Mitchell - CEO" in captured["body"]
    assert "theturnpodcast.com" in captured["body"]


def test_unreadable_draft_is_not_reported_as_saved():
    """Created but unverifiable must not read as 'it's in your Drafts'."""

    def fake_execute(slug, args, role=None):
        if slug == "OUTLOOK_CREATE_DRAFT":
            return {"data": {"id": "draft-1"}, "log_id": "l"}
        raise RuntimeError("Graph 404")

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool", side_effect=fake_execute),
    ):
        _live_settings(settings)
        result = actions.save_email_to_drafts(
            to_email="anjanakummetha@gmail.com", subject="S", body="B"
        )

    assert result["verified"] is False
    assert "couldn't confirm" in result["kory_message"]


def test_missing_draft_id_is_a_failure():
    def fake_execute(slug, args, role=None):
        return {"data": {}, "log_id": "l"}

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool", side_effect=fake_execute),
    ):
        _live_settings(settings)
        result = actions.save_email_to_drafts(
            to_email="anjanakummetha@gmail.com", subject="S", body="B"
        )

    assert result["ok"] is False
    assert "nothing was saved" in result["error"]


def test_a_lost_logo_does_not_lose_the_draft():
    def fake_execute(slug, args, role=None):
        if slug == "OUTLOOK_CREATE_DRAFT":
            return {"data": {"id": "draft-1"}, "log_id": "l"}
        if slug == "OUTLOOK_ADD_MAIL_ATTACHMENT":
            raise RuntimeError("attachment rejected")
        return {"data": _draft(), "log_id": None}

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool", side_effect=fake_execute),
    ):
        _live_settings(settings)
        result = actions.save_email_to_drafts(
            to_email="anjanakummetha@gmail.com", subject="S", body="B"
        )

    assert result["ok"] is True and result["verified"] is True


def test_required_fields_are_checked_before_any_call():
    for kwargs in [
        {"to_email": "", "subject": "S", "body": "B"},
        {"to_email": "not-an-address", "subject": "S", "body": "B"},
        {"to_email": "a@b.com", "subject": "  ", "body": "B"},
        {"to_email": "a@b.com", "subject": "S", "body": "   "},
    ]:
        with patch.object(outlook_email, "execute_tool") as execute:
            result = actions.save_email_to_drafts(**kwargs)
        assert result["ok"] is False, kwargs
        execute.assert_not_called()


def test_dry_run_saves_nothing_and_says_so():
    """Dry run is pinned explicitly — outlook_email.settings can be a stale
    Settings instance once another test has reloaded app.config."""
    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool") as execute,
    ):
        _live_settings(settings)
        settings.lexi_dry_run = True
        result = actions.save_email_to_drafts(
            to_email="anjanakummetha@gmail.com", subject="S", body="B"
        )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["verified"] is False
    assert "test mode" in result["kory_message"]
    execute.assert_not_called()


def test_feature_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("LEXI_KORY_REVIEW_DRAFTS_ENABLED", "false")
    with patch.object(outlook_email, "execute_tool") as execute:
        result = actions.save_email_to_drafts(
            to_email="anjanakummetha@gmail.com", subject="S", body="B"
        )
    assert result["ok"] is False
    assert "disabled" in result["error"]
    execute.assert_not_called()


def test_cc_list_is_parsed_and_deduped_against_the_recipient():
    captured = {}

    def fake_execute(slug, args, role=None):
        if slug == "OUTLOOK_CREATE_DRAFT":
            captured.update(args)
            return {"data": {"id": "draft-1"}, "log_id": "l"}
        return {"data": _draft(), "log_id": None}

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool", side_effect=fake_execute),
    ):
        _live_settings(settings)
        actions.save_email_to_drafts(
            to_email="anjanakummetha@gmail.com",
            subject="S",
            body="B",
            cc_emails="matt.maley@iconicfounders.com; anjanakummetha@gmail.com",
        )

    assert captured["cc_recipients"] == ["matt.maley@iconicfounders.com"]


# ── recipient verification (live defect: drafted to an invented address) ─────


def _verify_patches(verified, suggestions=None):
    return patch.object(
        outlook_email, "verify_recipient_address",
        return_value={"verified": verified, "suggestions": suggestions or []},
    )


def test_an_invented_address_is_refused_with_candidates():
    """The live failure: Lexi drafted to angelo@morganstanley.com, which she made
    up. Plausible and wrong is the worst combination — nothing downstream can tell
    it from a real address."""
    suggestions = [{"name": "Angelo Amitsis", "email": "angelo.amitsis@morganstanleypwm.com",
                    "company": "Morgan Stanley"}]
    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool") as execute,
        _verify_patches(False, suggestions),
    ):
        _live_settings(settings)
        result = actions.save_email_to_drafts(
            to_email="angelo@morganstanley.com", subject="S", body="B"
        )

    assert result["ok"] is False
    assert result["error_code"] == "unverified_recipient"
    assert "angelo.amitsis@morganstanleypwm.com" in result["kory_message"]
    execute.assert_not_called(), "nothing should be written for an unverified address"


def test_a_known_address_saves_normally():
    def fake_execute(slug, args, role=None):
        if slug == "OUTLOOK_CREATE_DRAFT":
            return {"data": {"id": "draft-1"}, "log_id": "l"}
        return {"data": _draft(), "log_id": None}

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool", side_effect=fake_execute),
        _verify_patches(True),
    ):
        _live_settings(settings)
        result = actions.save_email_to_drafts(
            to_email="anjanakummetha@gmail.com", subject="S", body="B"
        )
    assert result["ok"] is True and result["verified"] is True


def test_kory_can_override_for_a_genuinely_new_contact():
    def fake_execute(slug, args, role=None):
        if slug == "OUTLOOK_CREATE_DRAFT":
            return {"data": {"id": "draft-1"}, "log_id": "l"}
        return {"data": _draft(), "log_id": None}

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool", side_effect=fake_execute),
        _verify_patches(False),
    ):
        _live_settings(settings)
        result = actions.save_email_to_drafts(
            to_email="brand.new@example.com", subject="S", body="B",
            allow_unverified_recipient=True,
        )
    assert result["ok"] is True


def test_an_unreachable_check_does_not_block_the_draft():
    """A mailbox or CRM outage is not evidence the address is wrong."""
    def fake_execute(slug, args, role=None):
        if slug == "OUTLOOK_CREATE_DRAFT":
            return {"data": {"id": "draft-1"}, "log_id": "l"}
        return {"data": _draft(), "log_id": None}

    with (
        patch.object(outlook_email, "settings") as settings,
        patch.object(outlook_email, "execute_tool", side_effect=fake_execute),
        patch.object(outlook_email, "verify_recipient_address",
                     return_value={"verified": True, "source": "unchecked"}),
    ):
        _live_settings(settings)
        result = actions.save_email_to_drafts(
            to_email="someone@example.com", subject="S", body="B"
        )
    assert result["ok"] is True


def test_korys_own_address_is_always_known():
    with patch.object(outlook_email, "settings") as settings:
        _live_settings(settings)
        assert outlook_email.verify_recipient_address(
            "kory.mitchell@iconicfounders.com"
        )["verified"] is True
