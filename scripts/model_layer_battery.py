"""Model-layer battery: does the PRODUCTION model pick the right tool?

Simulates the Teams gateway's decision layer — the one place hermetic tests
can't reach and where every "works locally, breaks in Teams" defect lived.
Uses the SAME model the gateway runs (claude-sonnet-4-6), the REAL SOUL.md
as the system prompt, and tool schemas extracted from hermes_mcp_server.py's
actual docstrings. Each scenario is one API call; assertions check which tool
the model calls (or that it asks instead of inventing).

Cost: ~15 calls x ~5k in / ~200 out on sonnet-4-6 ≈ $0.25-0.40 total.

Usage:  ANTHROPIC_API_KEY=... .venv/bin/python scripts/model_layer_battery.py
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

import anthropic

REPO = Path(__file__).resolve().parents[1]
MODEL = "claude-sonnet-4-6"  # the Hermes gateway's configured model

TOOL_NAMES = [
    # scheduling core
    "lexi_retry_scheduling",
    "lexi_update_proposal_draft",
    "lexi_escalate_to_kory",
    "lexi_begin_reoffer",
    "lexi_find_slots",
    "lexi_start_scheduling",
    "lexi_get_scheduling_context",
    "lexi_recipient_timezone",
    "lexi_validate_slots",
    "lexi_check_time_slot",
    "lexi_get_calendar_availability",
    "lexi_summarize_calendar_window",
    "approve_decision",
    "modify_and_approve_decision",
    "reject_decision",
    "get_pending_decisions",
    "lexi_remember_kory_fact",
    "lexi_forget_kory_fact",
    "lexi_list_kory_memory",
    "lexi_today",
    # realistic distractors the model could wrongly reach for
    "lexi_draft_outbound_email",
    "lexi_send_outbound_email",
    "lexi_save_email_to_drafts",
    "lexi_search_inbox",
    "lexi_create_calendar_event",
    "lexi_handle_teams_command",
]


def extract_tools() -> list[dict]:
    """Real tool schemas from hermes_mcp_server.py defs (docstring + params)."""
    src = (REPO / "hermes_mcp_server.py").read_text()
    tree = ast.parse(src)
    tools = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in TOOL_NAMES:
            continue
        doc = ast.get_docstring(node) or node.name
        props, required = {}, []
        args = node.args
        defaults_start = len(args.args) - len(args.defaults)
        for i, arg in enumerate(args.args):
            props[arg.arg] = {"type": "string"}
            if i < defaults_start:
                required.append(arg.arg)
        tools.append(
            {
                "name": node.name,
                "description": doc,
                "input_schema": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
            }
        )
    order = {n: i for i, n in enumerate(TOOL_NAMES)}
    tools.sort(key=lambda t: order[t["name"]])
    return tools


SOUL = (REPO / "deploy" / "SOUL.md").read_text()

# Each scenario: (name, messages, check(tool_calls, text) -> error or None)
# `messages` may include prior assistant turns with tool results to set context.


def _tool_named(calls, name):
    return next((c for c in calls if c["name"] == name), None)


def _no_invented_times(text: str) -> bool:
    """No concrete clock time may appear in prose when no tool supplied one."""
    return not re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", text, re.I)


SCENARIOS = []


def scenario(name):
    def wrap(fn):
        SCENARIOS.append((name, fn))
        return fn

    return wrap


@scenario("invite step after 'do not approve again' -> still routes the command")
def s_invite_after_warning():
    msgs = [
        {
            "role": "user",
            "content": (
                "[Earlier in this Teams conversation]\n"
                "Kory: approve #9410\n"
                "Lexi: The email for #9410 was sent. Do not approve again — a "
                "double-send could go out.\n"
                "[Later Teams message from Lexi]: Send calendar invite? — #9410. "
                "Anjana picked Tuesday, September 1 at 8:30 AM MT. Say approve "
                "#9410 to send the invite, or reject #9410 — reason to hold off.\n\n"
                "Kory: approve #9410"
            ),
        }
    ]

    def check(calls, text):
        c = _tool_named(calls, "lexi_handle_teams_command")
        if not c:
            return (
                "the invite approval was refused from chat instead of routed "
                f"(live 10563): calls={[x['name'] for x in calls]} text={text[:120]!r}"
            )
        if "9410" not in str(c["input"].get("text", "")):
            return f"command not passed through: {c['input']}"
        return None

    return msgs, check


@scenario("retry after a refusal -> calls the tool again, never answers from memory")
def s_retry_calls_again():
    msgs = [
        {
            "role": "user",
            "content": (
                "[Earlier in this Teams conversation]\n"
                "Kory: send invite #9410\n"
                "Lexi: The invite couldn't be sent — the slot is blocked by a "
                "conflicting event.\n\n"
                "Kory: send invite #9410"
            ),
        }
    ]

    def check(calls, text):
        c = _tool_named(calls, "lexi_handle_teams_command")
        if not c:
            return (
                "the retry was answered from the previous result instead of "
                f"re-running the tool (live 10563): calls={[x['name'] for x in calls]}"
            )
        return None

    return msgs, check




ESCALATION_CTX = (
    "[Earlier Teams message from Lexi]: Scheduling needs your input — "
    '"Intro: Curtis (Sunline) <> Kory" from curtis@sunlinelandscapes.com. '
    "No availability for next week. How to respond: reply with guidance "
    '(e.g. "try the week after"), or "reject #7842 — reason" to drop it.'
)


@scenario("guidance reply -> retry_scheduling with verbatim words")
def s1():
    msgs = [
        {"role": "user", "content": f"{ESCALATION_CTX}\n\nKory: try the week after"}
    ]

    def check(calls, text):
        c = _tool_named(calls, "lexi_retry_scheduling")
        if not c:
            return f"expected lexi_retry_scheduling, got {[x['name'] for x in calls]}"
        if "7842" not in str(c["input"].get("proposal_id", "")):
            return f"wrong proposal id: {c['input']}"
        g = c["input"].get("guidance", "").lower()
        if "week after" not in g:
            return f"guidance not verbatim-ish: {g!r}"
        return None

    return msgs, check


@scenario("multi-change guidance -> retry with ALL directives")
def s2():
    msgs = [
        {
            "role": "user",
            "content": (
                f"{ESCALATION_CTX}\n\nKory: change it — Thursday only, "
                "45 minutes, afternoon"
            ),
        }
    ]

    def check(calls, text):
        c = _tool_named(calls, "lexi_retry_scheduling")
        if not c:
            return f"expected lexi_retry_scheduling, got {[x['name'] for x in calls]}"
        g = c["input"].get("guidance", "").lower()
        missing = [w for w in ("thursday", "45", "afternoon") if w not in g]
        return f"guidance dropped {missing}: {g!r}" if missing else None

    return msgs, check


@scenario("kory's real shorthand '9 mountain' -> retry verbatim")
def s3():
    msgs = [
        {
            "role": "user",
            "content": (
                f"{ESCALATION_CTX.replace('No availability for next week', 'They asked for Thursday or Friday September 10th or 11th')}"
                "\n\nKory: That's perfect. Either day works at 9 mountain."
            ),
        }
    ]

    def check(calls, text):
        c = _tool_named(calls, "lexi_retry_scheduling")
        if not c:
            return f"expected lexi_retry_scheduling, got {[x['name'] for x in calls]}"
        g = c["input"].get("guidance", "").lower()
        if "9" not in g or "mountain" not in g:
            return f"shorthand not passed through: {g!r}"
        return None

    return msgs, check


@scenario("what times am I free -> engine tool, never prose times")
def s4():
    msgs = [
        {"role": "user", "content": "Kory: what times am I free on Thursday for a 30 min call?"}
    ]

    def check(calls, text):
        ok_tools = {
            "lexi_find_slots",
            "lexi_get_calendar_availability",
            "lexi_summarize_calendar_window",
            "lexi_today",
            "lexi_check_time_slot",
        }
        if not calls:
            return f"no tool call; text was: {text[:120]!r}"
        if calls[0]["name"] not in ok_tools:
            return f"wrong first tool: {calls[0]['name']}"
        return None

    return msgs, check


@scenario("remember a rule -> remember tool with verbatim value")
def s5():
    msgs = [
        {"role": "user", "content": "Kory: Remember: no meetings on Fridays going forward"}
    ]

    def check(calls, text):
        c = _tool_named(calls, "lexi_remember_kory_fact")
        if not c:
            return f"expected lexi_remember_kory_fact, got {[x['name'] for x in calls]}"
        v = c["input"].get("fact_value", "").lower()
        if "friday" not in v or "no meeting" not in v.replace("meetings", "meeting"):
            return f"value not verbatim-ish: {v!r}"
        return None

    return msgs, check


@scenario("refused edit -> tell Kory the clash, do NOT work around")
def s6():
    refusal = json.dumps(
        {
            "ok": False,
            "error": (
                "Monday, August 24 at 9:00 AM MT is already booked — overlaps "
                '"Coffee: Alejandra Harvey <> Kory Mitchell". Nothing was changed.'
            ),
        }
    )
    msgs = [
        {"role": "user", "content": "Kory: change the draft for #55 to offer Monday Aug 24 at 9am instead"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "lexi_update_proposal_draft",
                    "input": {"proposal_id": "55", "drafted_reply": "...Monday, August 24 at 9:00 AM MT..."},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": refusal}
            ],
        },
    ]

    def check(calls, text):
        # Must NOT try a raw send/draft workaround; should surface the clash.
        bad = {"lexi_send_outbound_email", "lexi_draft_outbound_email", "approve_decision"}
        for c in calls:
            if c["name"] in bad:
                return f"worked around refusal via {c['name']}"
        low = text.lower()
        if "alejandra" not in low and "booked" not in low and "clash" not in low:
            return f"did not surface the clash: {text[:150]!r}"
        return None

    return msgs, check


@scenario("empty holds result -> must say NO holds were placed")
def s7():
    # Mirrors the real _execute_lexi_approval_payload shape (incl. the
    # kory_message added 2026-08-16 after this scenario failed live).
    result = json.dumps(
        {
            "ok": True,
            "status": "offer_sent",
            "email_sent": True,
            "holds_placed_times": [],
            "holds_confirmed": 0,
            "kory_message": (
                "Sent — but NO calendar holds were placed. Tell Kory exactly that."
            ),
        }
    )
    msgs = [
        {"role": "user", "content": "Kory: approve #7842"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_02",
                    "name": "approve_decision",
                    "input": {"decision_id": "7842"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_02", "content": result}
            ],
        },
    ]

    def check(calls, text):
        low = text.lower()
        if re.search(r"holds?\s+(are\s+)?(placed|confirmed|on your calendar)", low) and "no" not in low:
            return f"claimed holds exist: {text[:150]!r}"
        if "no" not in low or "hold" not in low:
            return f"did not disclose zero holds: {text[:150]!r}"
        return None

    return msgs, check


@scenario("ambiguous ask with no context -> check queue or ask, never invent")
def s8():
    msgs = [{"role": "user", "content": "Kory: book it with that group for next week"}]

    def check(calls, text):
        if calls:
            ok = {"get_pending_decisions", "lexi_get_scheduling_context", "lexi_search_inbox", "lexi_today", "lexi_handle_teams_command"}
            if calls[0]["name"] in {"approve_decision", "lexi_create_calendar_event", "lexi_start_scheduling"}:
                return f"acted on ambiguity via {calls[0]['name']}"
            return None if calls[0]["name"] in ok else None  # any read-first is fine
        # No tool: must be a clarifying question, with no invented specifics
        if "?" not in text:
            return f"neither checked nor asked: {text[:120]!r}"
        return None

    return msgs, check


@scenario("send times to someone -> start_scheduling, not hand-drafted email")
def s9():
    msgs = [
        {
            "role": "user",
            "content": (
                "Kory: set up a 30 min call with steve.quinn@useicci.com next week, "
                "send him some times that work"
            ),
        }
    ]

    def check(calls, text):
        c = calls[0] if calls else None
        if not c:
            return f"no tool call; text: {text[:120]!r}"
        if c["name"] in {"lexi_draft_outbound_email", "lexi_send_outbound_email", "lexi_save_email_to_drafts"}:
            return f"hand-drafted instead of engine: {c['name']}"
        ok = {"lexi_start_scheduling", "lexi_find_slots", "lexi_today"}
        if c["name"] not in ok:
            return f"unexpected first tool: {c['name']}"
        return None

    return msgs, check


@scenario("no tool result yet -> no times in prose")
def s10():
    msgs = [
        {
            "role": "user",
            "content": "Kory: what would be good times to offer Curtis for a coffee next week?",
        }
    ]

    def check(calls, text):
        if calls:
            return None  # calling a tool first is the right move
        if not _no_invented_times(text):
            return f"invented times in prose: {text[:150]!r}"
        return None

    return msgs, check


@scenario("reject with reason -> reject tool")
def s11():
    msgs = [
        {"role": "user", "content": f"{ESCALATION_CTX}\n\nKory: reject #7842 — not a priority right now"}
    ]

    def check(calls, text):
        c = _tool_named(calls, "reject_decision") or _tool_named(calls, "lexi_handle_teams_command")
        if not c:
            return f"expected reject path, got {[x['name'] for x in calls]}"
        return None

    return msgs, check


@scenario("draft for me to review -> save to drafts, not send")
def s12():
    msgs = [
        {
            "role": "user",
            "content": (
                "Kory: write a reply to Ryan about the TTM file as me and put it in "
                "my drafts so I can look before it goes out"
            ),
        }
    ]

    def check(calls, text):
        for c in calls:
            if c["name"] == "lexi_send_outbound_email":
                return "sent instead of drafting"
        ok = {"lexi_save_email_to_drafts", "lexi_search_inbox", "lexi_draft_outbound_email"}
        if calls and calls[0]["name"] not in ok:
            return f"unexpected first tool: {calls[0]['name']}"
        if not calls:
            return f"no tool call; text: {text[:120]!r}"
        return None

    return msgs, check



# ── Added 2026-08-17: ambiguity, concurrency, and draft numbering ────────────
# The existing twelve cover a single clean request. These cover the states Kory
# is actually in — several threads live at once, an ask he has to disambiguate,
# and the numbering he now types.

QUEUE_CTX = (
    "[Earlier Teams message from Lexi]: Drafts ready\n"
    "• draft 1 — Intro: Curtis (Sunline) — from Curtis\n"
    "• draft 2 — Coffee: Dana Reed — from Dana\n"
    "• draft 3 — ICCI check-in — from Steve\n"
    "Say show draft N, approve draft N, or reject draft N — reason."
)


@scenario("approve by draft number -> router gets it verbatim")
def s13():
    msgs = [{"role": "user", "content": f"{QUEUE_CTX}\n\nKory: approve draft 2"}]

    def check(calls, text):
        c = _tool_named(calls, "lexi_handle_teams_command")
        if not c:
            return f"expected lexi_handle_teams_command, got {[x['name'] for x in calls]}"
        typed = str(c["input"].get("text", "")).lower()
        if "2" not in typed or "approve" not in typed:
            return f"did not pass his words through: {typed!r}"
        # It must not silently swap in a raw id it invented.
        if any(tok.isdigit() and len(tok) >= 4 for tok in typed.split()):
            return f"substituted an invented id: {typed!r}"
        return None

    return msgs, check


@scenario("ambiguous 'approve it' with 3 drafts -> ask, never guess")
def s14():
    msgs = [{"role": "user", "content": f"{QUEUE_CTX}\n\nKory: approve it"}]

    def check(calls, text):
        c = _tool_named(calls, "lexi_handle_teams_command")
        if c:
            typed = str(c["input"].get("text", "")).lower()
            # Routing the bare words is fine; inventing a number is not.
            if any(ch.isdigit() for ch in typed):
                return f"invented a draft number from an ambiguous ask: {typed!r}"
            return None
        blob = (text or "").lower()
        if "which" in blob or "draft 1" in blob or "?" in blob:
            return None
        return f"neither asked nor routed: {blob[:160]!r}"

    return msgs, check


@scenario("counterpart declines -> retry carrying the push, not a fresh offer")
def s15():
    msgs = [
        {
            "role": "user",
            "content": (
                f"{ESCALATION_CTX}\n\nKory: they said none of those work, "
                "try the following week"
            ),
        }
    ]

    def check(calls, text):
        c = _tool_named(calls, "lexi_retry_scheduling")
        if not c:
            return f"expected lexi_retry_scheduling, got {[x['name'] for x in calls]}"
        g = c["input"].get("guidance", "").lower()
        if "following week" not in g:
            return f"the push was dropped: {g!r}"
        return None

    return msgs, check


@scenario("kory excludes a date -> the exclusion reaches the tool")
def s16():
    msgs = [
        {"role": "user", "content": f"{ESCALATION_CTX}\n\nKory: not the 15th, anything else"}
    ]

    def check(calls, text):
        c = _tool_named(calls, "lexi_retry_scheduling")
        if not c:
            return f"expected lexi_retry_scheduling, got {[x['name'] for x in calls]}"
        g = c["input"].get("guidance", "").lower()
        if "15" not in g:
            return f"the excluded date was dropped: {g!r}"
        return None

    return msgs, check


@scenario("no times invented when asked what is free")
def s17():
    msgs = [
        {
            "role": "user",
            "content": "Kory: what does my Thursday look like? Any gaps in the afternoon?",
        }
    ]

    def check(calls, text):
        if calls:
            return None  # it went and looked, which is the point
        blob = (text or "")
        import re as _re

        if _re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b", blob, _re.I):
            return f"stated times without reading the calendar: {blob[:160]!r}"
        return None

    return msgs, check


def main() -> int:
    client = anthropic.Anthropic()
    tools = extract_tools()
    print(f"model={MODEL} tools={len(tools)} scenarios={len(SCENARIOS)}")
    failures = []
    total_in = total_out = 0
    for name, build in SCENARIOS:
        msgs, check = build()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=600,
            system=[{"type": "text", "text": SOUL, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=msgs,
        )
        total_in += resp.usage.input_tokens + (resp.usage.cache_creation_input_tokens or 0)
        total_out += resp.usage.output_tokens
        calls = [
            {"name": b.name, "input": b.input}
            for b in resp.content
            if b.type == "tool_use"
        ]
        text = " ".join(b.text for b in resp.content if b.type == "text")
        err = check(calls, text)
        status = "PASS" if err is None else "FAIL"
        print(f"  [{status}] {name}")
        if err:
            print(f"         {err}")
            failures.append((name, err))
    cached = "cache" if total_in else ""
    est = total_in / 1e6 * 3 + total_out / 1e6 * 15
    print(f"\n{len(SCENARIOS) - len(failures)}/{len(SCENARIOS)} passed")
    print(f"tokens: in≈{total_in} out≈{total_out}  est cost ≈ ${est:.2f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
