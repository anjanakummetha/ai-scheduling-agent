"""Looking up a person Kory named the way his briefing names them.

The briefing says "Follow up with Angelo (Morgan Stanley)". Searching that whole
string returned "No HubSpot contact" — the name filter required every word,
including "(morgan" and "stanley)", to appear in the contact's *name*. Lexi
reported no CRM record and drafted the follow-up to a placeholder address.
"""

from unittest.mock import patch

import pytest

from app.integrations.hubspot_manager import (
    _org_matches,
    _split_name_and_org,
    enrich_prebrief_from_hubspot,
)

ANGELO = {
    "id": "1", "name": "Angelo Amitsis", "email": "angelo.amitsis@morganstanleypwm.com",
    "company": "Morgan Stanley", "jobtitle": "Managing Director",
}
CHRIS = {
    "id": "2", "name": "Chris Angelo", "email": "changelo@staygreen.com",
    "company": "Stay Green, Inc.", "jobtitle": "President & CEO",
}


@pytest.mark.parametrize(
    "described,person,org",
    [
        ("Angelo (Morgan Stanley)", "Angelo", "Morgan Stanley"),
        ("Angelo at Morgan Stanley", "Angelo", "Morgan Stanley"),
        ("Pete (H&F)", "Pete", "H&F"),
        ("Angelo Amitsis", "Angelo Amitsis", ""),
        ("Bruce Krinsky", "Bruce Krinsky", ""),
    ],
)
def test_a_described_person_splits_into_name_and_company(described, person, org):
    assert _split_name_and_org(described) == (person, org)


def test_company_hint_matches_the_right_contact():
    assert _org_matches("Morgan Stanley", ANGELO) is True
    assert _org_matches("Morgan Stanley", CHRIS) is False


def _lookup(name, contacts):
    with (
        patch("app.integrations.hubspot_manager.hubspot_configured", return_value=True),
        patch("app.integrations.hubspot_manager.search_contacts",
              return_value={"contacts": contacts}),
        patch("app.integrations.hubspot_manager.contact_deals", return_value=[]),
    ):
        return enrich_prebrief_from_hubspot(name=name)


def test_the_briefings_phrasing_now_resolves():
    """The live failure: this returned "No HubSpot contact"."""
    result = _lookup("Angelo (Morgan Stanley)", [ANGELO, CHRIS])
    assert result["found"] is True
    assert "Angelo Amitsis" in result["kory_message"]


def test_the_company_picks_between_two_people_with_the_same_first_name():
    result = _lookup("Angelo at Morgan Stanley", [ANGELO, CHRIS])
    assert result["found"] is True
    assert "Chris Angelo" not in result["kory_message"]


def test_a_bare_first_name_still_asks_rather_than_guessing():
    """Without a company there is no evidence — showing the wrong person's record
    is worse than asking."""
    result = _lookup("Angelo", [ANGELO, CHRIS])
    assert result["found"] is False
    assert result["ambiguous"] is True
    assert "which one" in result["kory_message"].lower()


def test_a_full_name_is_unaffected():
    result = _lookup("Chris Angelo", [ANGELO, CHRIS])
    assert result["found"] is True
    assert "Chris Angelo" in result["kory_message"]


def test_a_wrong_company_does_not_force_a_match():
    """The hint narrows a real list; it must not invent a match."""
    result = _lookup("Angelo (Goldman Sachs)", [ANGELO, CHRIS])
    assert result["found"] is False


def test_nobody_matching_still_reports_nothing_found():
    result = _lookup("Zebedee (Nowhere Ltd)", [ANGELO, CHRIS])
    assert result["found"] is False
    assert "No HubSpot contact" in result["kory_message"]
