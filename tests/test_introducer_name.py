"""Names shown to Kory must not be raw email local parts.

LT-B1 surfaced "anjanakummetha" in the meeting title while the profile store
already held "Anjana Kummetha".
"""

from app.scheduling.introducer import _name_from_email, resolve_introducer_for_contact
from app.storage.recipient_profiles import record_display_name, upsert_introducer


def test_separators_become_spaces() -> None:
    assert _name_from_email("jane.doe@example.com") == "Jane Doe"
    assert _name_from_email("jane_doe@example.com") == "Jane Doe"
    assert _name_from_email("jane-doe@example.com") == "Jane Doe"


def test_trailing_digits_are_dropped() -> None:
    assert _name_from_email("jane.doe99@example.com") == "Jane Doe"


def test_runtogether_local_part_is_title_cased_not_left_bare() -> None:
    # Can't be split reliably, but it must not reach Kory as "anjanakummetha".
    assert _name_from_email("anjanakummetha@example.com") == "Anjanakummetha"


def test_stored_display_name_wins_over_the_local_part() -> None:
    email = "anjanakummetha@test-introducer.com"
    record_display_name(email, "Anjana Kummetha")
    assert _name_from_email(email) == "Anjana Kummetha"


def test_stale_localpart_introducer_row_is_repaired_on_read() -> None:
    # Rows written before the fix stored the bare local part; reading one must
    # not hand that value back.
    email = "guest@test-introducer.com"
    intro = "anjanakummetha@test-introducer.com"
    record_display_name(intro, "Anjana Kummetha")
    upsert_introducer(
        email=email, introducer_name="anjanakummetha", introducer_email=intro, source="intro_sender"
    )
    info = resolve_introducer_for_contact(email=email)
    assert info is not None
    assert info.name == "Anjana Kummetha"
