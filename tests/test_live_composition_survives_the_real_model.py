"""Does the REAL drafting model's prose survive the new composition check?

@pytest.mark.live — excluded from CI and the default suite. Costs a handful of
calls on the drafting model.

This is the one thing the hermetic tests cannot answer. Composition now refuses
a draft that offers a time the engine did not stage, falling back to the
slot-derived template. The seven "normal draft" shapes pinned in
test_draft_cannot_diverge_from_slots.py are prose I wrote, which proves the
check is not absurdly strict — it does not prove the model's real output passes.

If it does not, every offer email silently becomes the template. That fails
SAFE, so nothing breaks and no wrong time is ever sent; it just reads like
boilerplate, and the only way to notice is to read a few. Hence this.

    .venv/bin/python -m pytest tests/test_live_composition_survives_the_real_model.py \
        -q -m live -s
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.scheduling.hermes_compose import compose_offer_email_with_hermes

MT = ZoneInfo("America/Denver")

pytestmark = pytest.mark.live


def _slots(count: int = 2) -> list[dict[str, str]]:
    day = date.today() + timedelta(days=12)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    out = []
    for i in range(count):
        d = day + timedelta(days=i * 2)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({
            "start": datetime(d.year, d.month, d.day, 9 + i, 0, tzinfo=MT).isoformat(),
            "end": datetime(d.year, d.month, d.day, 9 + i, 30, tzinfo=MT).isoformat(),
        })
    return out


# Real shapes of inbound ask, including the ones most likely to tempt the model
# into naming a time of its own.
ASKS = [
    ("a plain intro request",
     "Hi Lexi — Kory and I met at the summit. Could we find 30 minutes in the "
     "next couple of weeks for an intro call?"),
    ("one that names a day we are NOT offering",
     "Would Friday work for a quick call? If not, whatever is easiest."),
    ("one that asks for options beyond what we staged",
     "Happy to chat — could you send a few options, ideally including something "
     "late in the day or early next month?"),
    ("one with a hard constraint",
     "I'm only free before 10am my time, and I'm on the east coast. Can we find "
     "something in the next two weeks?"),
    ("a terse one",
     "Sure, let's talk. When?"),
]


@pytest.mark.parametrize("label, body", ASKS, ids=[a[0] for a in ASKS])
def test_the_real_model_draft_is_kept_not_replaced_by_the_template(label, body):
    slots = _slots()
    draft, source = compose_offer_email_with_hermes(
        proposal_sender="Dana Reyes <dana@example.com>",
        proposal_subject="Intro call",
        proposal_body=body,
        thread_id="live-compose-check",
        slots=slots,
        voice_mode="lexi",
        stored_recipient_timezone="America/Denver",
        intent="referral_or_intro",
    )
    print(f"\n--- {label} -> source={source}\n{draft}\n")

    # Whatever happens, the result must never offer an unstaged time.
    from app.scheduling.draft_slot_sync import draft_matches_slots

    ok, mismatch = draft_matches_slots(draft_body=draft, proposed_slots=slots)
    assert ok, f"composition emitted a draft the send gate would refuse: {mismatch}"

    assert source == "hermes", (
        f"the real model's draft for {label!r} was replaced by the template. "
        "That is safe but reads as boilerplate; if this fails across several "
        "shapes, the check is too strict for real prose."
    )
