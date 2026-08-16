"""Extract REAL multi-message scheduling threads from Kory's mailbox.

Read-only, two-phase to keep Composio spend bounded:
  1. Deep metadata listing (no bodies): inbox top=1000 + sentitems top=1000
     — 2 calls. `top` IS honored (verified live 2026-08-16); `filter` and
     `search` are decorative, so all selection is client-side.
  2. Full-body fetch ONLY for messages in conversations that look like real
     scheduling back-and-forth (2+ messages, an external human, scheduling
     language in subjects/previews), capped at MAX_BODY_FETCHES.

Usage (on the box):
  sudo -u lexi env LEXI_ENV=production PYTHONPATH=$PWD .venv/bin/python \
      scripts/extract_scheduling_threads.py /tmp/sched_threads.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from typing import Any

from app.integrations.composio_client import execute_read_tool
from app.integrations.outlook_email import _plain_text, get_message

MAX_BODY_FETCHES = 60

SCHED_RE = re.compile(
    r"\b(schedul|meet|coffee|lunch|dinner|happy hour|calendar|avail|"
    r"touch base|catch up|connect|intro|call|reschedul|"
    r"\d{1,2}(?::\d{2})?\s*(?:am|pm)|monday|tuesday|wednesday|thursday|friday)\b",
    re.I,
)
INTERNAL_RE = re.compile(r"@(?:iconicfounders\.com|ifg\.vc)$", re.I)
NOISE_SENDER_RE = re.compile(
    r"(?:no-?reply|noreply|notification|digest|newsletter|mailer|marketing|"
    r"@(?:asana|outreach|zoominfo|netsuite|leadinfo|microsoft|ypo|instagram|"
    r"linkedin|google|apple|docusign)\.)",
    re.I,
)


def _unwrap(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data") or {}
    for key in ("response_data", "value", "messages"):
        if isinstance(data, dict) and key in data:
            data = data[key]
            break
    if isinstance(data, dict) and "value" in data:
        data = data["value"]
    return data if isinstance(data, list) else []


def _list_folder_meta(folder: str, top: int) -> list[dict[str, Any]]:
    return _unwrap(
        execute_read_tool(
            "OUTLOOK_LIST_MESSAGES",
            {
                "user_id": "me",
                "folder": folder,
                "top": top,
                "orderby": [
                    "receivedDateTime desc" if folder == "inbox" else "sentDateTime desc"
                ],
                "select": [
                    "id",
                    "subject",
                    "from",
                    "toRecipients",
                    "ccRecipients",
                    "receivedDateTime",
                    "sentDateTime",
                    "conversationId",
                    "bodyPreview",
                ],
            },
        )
    )


def _addr(entry: Any) -> str:
    if isinstance(entry, dict):
        return str((entry.get("emailAddress") or {}).get("address") or "").lower()
    return ""


def main(out_path: str) -> None:
    convs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for folder in ("inbox", "sentitems"):
        for msg in _list_folder_meta(folder, 1000):
            cid = msg.get("conversationId")
            if not cid:
                continue
            convs[cid].append(
                {
                    "folder": folder,
                    "id": msg.get("id"),
                    "subject": msg.get("subject"),
                    "from": _addr(msg.get("from")),
                    "to": [_addr(r) for r in (msg.get("toRecipients") or [])],
                    "cc": [_addr(r) for r in (msg.get("ccRecipients") or [])],
                    "at": msg.get("receivedDateTime") or msg.get("sentDateTime"),
                    "preview": str(msg.get("bodyPreview") or "")[:400],
                }
            )

    selected = []
    for cid, msgs in convs.items():
        if len(msgs) < 2:
            continue
        msgs.sort(key=lambda m: str(m["at"] or ""))
        senders = {m["from"] for m in msgs if m["from"]}
        external = {s for s in senders if not INTERNAL_RE.search(s)}
        if not external or all(NOISE_SENDER_RE.search(s) for s in external):
            continue
        combined = "\n".join(f"{m['subject']}\n{m['preview']}" for m in msgs)
        if not SCHED_RE.search(combined):
            continue
        # Real back-and-forth: messages from BOTH sides (or 3+ messages).
        has_internal = len(external) < len(senders)
        if len(msgs) < 3 and not has_internal:
            continue
        selected.append({"conversation_id": cid, "messages": msgs, "external": sorted(external)})

    selected.sort(key=lambda c: -len(c["messages"]))

    fetched = 0
    for conv in selected:
        for m in conv["messages"]:
            if fetched >= MAX_BODY_FETCHES:
                break
            try:
                raw, _log = get_message(m["id"])
                body = raw.get("body") or {}
                content = body.get("content") if isinstance(body, dict) else str(body)
                m["text"] = (_plain_text(content or "") or m["preview"])[:6000]
                fetched += 1
            except Exception as exc:  # noqa: BLE001 — keep the preview
                m["text"] = m["preview"]
                m["fetch_error"] = str(exc)[:100]
        if fetched >= MAX_BODY_FETCHES:
            break

    with open(out_path, "w") as fh:
        json.dump(selected, fh, indent=1)
    print(f"conversations selected: {len(selected)} (bodies fetched: {fetched})")
    for c in selected[:25]:
        first = c["messages"][0]
        print(
            f"  {len(c['messages'])} msgs | {str(first['subject'])[:58]!r} | "
            f"{', '.join(c['external'])[:60]}"
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sched_threads.json")
