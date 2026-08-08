"""Chat-initiated outbound proposals must send a NEW email, not a draft reply.

Live bug (proposal 7997): start_outbound_scheduling creates a synthetic
thread id (lexi-outbound-<hex>) because no inbound message exists; the
dispatch path passed it to OUTLOOK_CREATE_DRAFT_REPLY, which rejected it as
a malformed message id — so "set up a call with X" offers could never send.
"""

from __future__ import annotations

import app.agents.comms_agent as ca


class _Result:
    warnings: list = []


def _proposal(thread_id):
    return {
        "thread_id": thread_id,
        "drafted_reply": "Would any of these times work?",
        "sender": "anjanakummetha@gmail.com",
        "subject": "[TEST] Kory Mitchell — intro call",
        "send_channel": "kory",
    }


def test_synthetic_outbound_thread_sends_new_email(monkeypatch):
    sent = {}

    def fake_send(**kw):
        sent.update(kw)
        return "new-msg-1", "log-1"

    def boom(*a, **k):
        raise AssertionError("must not call the reply path for synthetic threads")

    monkeypatch.setattr(ca, "send_outbound_email", fake_send)
    monkeypatch.setattr(ca, "create_draft_reply", boom)
    monkeypatch.setattr(ca, "send_reply_in_thread", boom)

    ok, err = ca._send_drafted_reply(_proposal("lexi-outbound-f0f762e0"), _Result())
    assert ok is True and err is None
    assert sent["to_email"] == "anjanakummetha@gmail.com"
    assert sent["subject"] == "[TEST] Kory Mitchell — intro call"
    assert sent["approved_send"] is True


def test_real_thread_still_uses_reply_path(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ca, "settings",
        type("S", (), {"sandbox_email_loopback": False, "lexi_write_mode": "kory"})(),
    )
    monkeypatch.setattr(ca, "create_draft_reply", lambda t, b, send_channel: (calls.append(t) or ("d1", "l1")))
    monkeypatch.setattr(ca, "send_draft", lambda d, send_channel: "sent")
    monkeypatch.setattr(
        ca, "send_outbound_email",
        lambda **k: (_ for _ in ()).throw(AssertionError("real threads must reply in-thread")),
    )

    ok, err = ca._send_drafted_reply(_proposal("AAMkAGRlYWwtcmVhbC1pZA=="), _Result())
    assert ok is True and err is None
    assert calls == ["AAMkAGRlYWwtcmVhbC1pZA=="]


def test_synthetic_thread_without_recipient_errors(monkeypatch):
    monkeypatch.setattr(
        ca, "send_outbound_email",
        lambda **k: (_ for _ in ()).throw(AssertionError("must not send without a recipient")),
    )
    p = _proposal("lexi-outbound-abc")
    p["sender"] = ""
    ok, err = ca._send_drafted_reply(p, _Result())
    assert ok is False and "no recipient" in err
