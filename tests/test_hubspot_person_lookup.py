"""The person tier must refuse far more often than it answers.

Every case here is real. They come from a probe run against Kory's actual book
on 2026-08-14 in which a candidate LinkedIn profile was found for 8 of 8
contacts and 6 of those were the wrong human or not a human at all. The tests
that matter are the refusals: a wrong job title on a real person in the shared
IFG CRM reads as authoritative and nobody re-checks it.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

import app.integrations.hubspot_person_lookup as pl


@pytest.fixture(autouse=True)
def _clear_cache():
    pl.reset_cache()
    yield
    pl.reset_cache()


def _entity(name, work):
    return {
        "name": name,
        "workHistory": [
            {"title": t, "company": {"name": c}, "dates": d} for t, c, d in work
        ],
    }


def _contact(name, email, **extra):
    return {"id": "1", "name": name, "email": email, **extra}


# --- is this even a person --------------------------------------------------


@pytest.mark.parametrize(
    "name,email",
    [
        ("Dunes Point Capital Team", "team@dunespoint.com"),
        ("Exuma Funds General Mailbox", "info@exumafunds.com"),
        ("Accounting Department", "accounting@acme.com"),
        ("Front Desk", "reception@acme.com"),
    ],
)
def test_a_shared_mailbox_is_never_given_a_job_title(name, email):
    """Both of the real ones resolved to a genuine executive at the right
    company, so the employer check passes. Only the name check stops it."""
    assert pl.non_person_reason(name, email)


def test_a_role_mailbox_is_caught_by_its_address_even_with_a_human_name():
    assert pl.non_person_reason("Sales Enquiries", "sales@acme.com")
    assert pl.non_person_reason("", "info@acme.com")


def test_one_word_is_not_enough_to_identify_someone():
    assert pl.non_person_reason("Madonna", "m@acme.com")


@pytest.mark.parametrize(
    "name,email",
    [
        ("Chris Lefkovitz", "chris@leftbankholdings.com"),
        ("Vincent Flaska", "vince@forkliftexchange.com"),
        ("Mary-Kate O'Brien", "mk@acme.com"),
        ("Timothy J. White", "tim@dunespoint.com"),
    ],
)
def test_a_real_person_is_not_blocked(name, email):
    assert pl.non_person_reason(name, email) == ""


@pytest.mark.parametrize(
    "name",
    [
        "Karen Groupman",   # substring of a blocked word, not the word
        "Sarah Board",      # all of these are real surnames
        "Peter Capital",
        "Jane Partners",
        "Tom Fund",
        "Alice Trust",
        "Bill Holdings",
        "Kim Office",
        "Jim Staff",
        "Ann Desk",
        "Dana Group",
    ],
)
def test_an_organisational_word_that_is_also_a_surname_does_not_block_a_person(name):
    """Blocking Sarah Board from enrichment because "board" reads corporate is a
    worse failure than missing a mailbox — and both real mailbox cases are
    caught by the strong words regardless."""
    assert pl.non_person_reason(name, "someone@acme.com") == ""


# --- is it the same human ---------------------------------------------------


def test_the_nickname_that_cost_us_a_real_fill():
    """Chris Lefkovitz is Christopher Lefkovitz, Founder and President at
    Leftbank Holdings. An earlier draft refused him on this alone."""
    assert pl.compare_names("Chris Lefkovitz", "Christopher Lefkovitz")["match"]


@pytest.mark.parametrize(
    "crm,profile",
    [
        ("Bob Smith", "Robert Smith"),
        ("Peggy Olson", "Margaret Olson"),
        ("Bill Turner", "William Turner"),
        ("Matt Hale", "Matthew Hale"),
        ("Tim White", "Timothy White"),
        ("Steve Quinn", "Stephen Quinn"),
        ("Dick Hall", "Richard Hall"),
    ],
)
def test_short_forms_are_the_same_person(crm, profile):
    assert pl.compare_names(crm, profile)["match"]


def test_a_middle_name_is_not_a_difference():
    assert pl.compare_names("Timothy White", "Timothy J. White")["match"]


def test_a_suffix_is_not_a_difference():
    assert pl.compare_names("John Rourke Jr", "John Rourke")["match"]


def test_a_one_character_surname_difference_is_accepted_but_flagged():
    """HubSpot has "Pelligrino" where LinkedIn has "Pellegrino". Same man, and
    the CRM has the typo -- which the reviewer should be told about."""
    out = pl.compare_names("Dan Pelligrino", "Dan Pellegrino")
    assert out["match"]
    assert "typo" in out["note"].lower()


def test_a_short_surname_gets_no_spelling_latitude():
    """Ross and Rose are different families; Pelligrino and Pellegrino are not."""
    assert not pl.compare_names("Dan Ross", "Dan Rose")["match"]


def test_different_surnames_are_different_people():
    assert not pl.compare_names("Bianca Martins", "Bianca Santos")["match"]


def test_different_first_names_are_different_people():
    assert not pl.compare_names("Chris Gavora", "Michael Gavora")["match"]


def test_a_group_name_never_matches_a_real_executive():
    """Left to itself this pairs "Dunes Point Capital Team" with the founder."""
    assert not pl.compare_names("Dunes Point Capital Team", "Timothy J. White")["match"]


# --- does the employer corroborate -----------------------------------------


def test_the_wrong_chris_gavora_is_refused():
    """The single most important test here.

    Kory's book contains two Chris Gavoras -- chris@threeshadows.co and
    cgavora@bockmanninc.com. A live search for the first returned the second's
    profile. The names match perfectly. Only the employer says otherwise.
    """
    entity = _entity("Chris Gavora", [("Chief Financial Officer", "Bockmann Inc.", None)])
    assert (
        pl.role_at_known_employer(entity, known_company="Three Shadows", domain="threeshadows.co")
        is None
    )


def test_a_wrong_person_at_a_different_company_is_refused():
    """Bianca Martins @ Ontra matched a tech recruiter in Brazil."""
    entity = _entity("Bianca Martins", [("Tech recruit", "ESIG Software e Consultoria em TI", None)])
    assert pl.role_at_known_employer(entity, known_company="Ontra", domain="ontra.ai") is None


def test_the_known_employer_resolves_the_title():
    entity = _entity("Christopher Lefkovitz", [("Founder and President", "Leftbank Holdings", None)])
    role = pl.role_at_known_employer(entity, known_company="Leftbank Holdings")
    assert role["title"] == "Founder and President"
    assert role["current"]


def test_the_email_domain_alone_can_corroborate():
    """Someone with no company on file but a corporate address is still
    checkable -- holmesmurphy.com against "Holmes Murphy & Associates"."""
    entity = _entity("Greg Vance", [("Managing Partner", "Holmes Murphy & Associates", None)])
    role = pl.role_at_known_employer(entity, domain="holmesmurphy.com")
    assert role["title"] == "Managing Partner"


def test_legal_suffixes_do_not_break_corroboration():
    entity = _entity("Vincent Flaska", [("President", "Forklift Exchange, Inc.", None)])
    assert pl.role_at_known_employer(entity, known_company="Forklift Exchange Inc.")


def test_a_membership_is_not_a_job():
    """Vincent Flaska's most recent open-ended role was "Member, YPO Chicago
    Chapter". Taking the current role writes a peer network into his title."""
    entity = _entity(
        "Vincent Flaska",
        [
            ("President", "Forklift Exchange Inc.", {"from": "2015-01-01"}),
            ("Member", "YPO Chicago Chapter", {"from": "2021-01-01"}),
        ],
    )
    role = pl.role_at_known_employer(entity, known_company="Forklift Exchange Inc.")
    assert role["title"] == "President"


def test_a_membership_at_the_corroborating_org_is_still_refused():
    entity = _entity("Vincent Flaska", [("Member", "YPO Chicago Chapter", None)])
    assert pl.role_at_known_employer(entity, known_company="YPO Chicago Chapter") is None


def test_a_current_role_beats_one_they_have_left():
    entity = _entity(
        "Rich Halvas",
        [
            ("Chief Financial Officer", "AdvancedPCB", {"from": "2024-01-01", "to": "2025-12-01"}),
            ("VP Finance", "AdvancedPCB", {"from": "2026-01-01"}),
        ],
    )
    role = pl.role_at_known_employer(entity, known_company="AdvancedPCB", today=date(2026, 8, 14))
    assert role["title"] == "VP Finance"


@pytest.mark.parametrize(
    "company,domain",
    [
        ("McCombs Enterprises", "mccombshq.com"),      # found live — "hq" is not identity
        ("Sky Group", "skygroup-co.com"),
        ("Holmes Murphy & Associates", "holmesmurphy.com"),
        ("True View Windows and Glass", "trueviewglass.com"),
        ("SPQ Holdings", "spqholdings.com"),
        ("RedCloud Capital", "redcloudcap.com"),
        ("IMA Financial Group, Inc.", "imacorp.com"),   # found live — "ima" is the whole name
        ("Solamere Capital, LLC", "solamerecapital.com"),
    ],
)
def test_a_domain_corroborates_the_company_it_belongs_to(company, domain):
    assert pl.employer_matches(company, domain=domain)


def test_a_short_key_corroborates_only_on_an_exact_match():
    """"ima" may equal IMA Financial Group. It must not be *found inside*
    Primation or Optima and pass as the same employer."""
    assert pl.employer_matches("IMA Financial Group", domain="imacorp.com")
    assert pl.employer_matches("Optima Partners", domain="imacorp.com") == ""
    assert pl.employer_matches("Primation Health", domain="imacorp.com") == ""


@pytest.mark.parametrize(
    "company,domain",
    [
        ("Bockmann Inc.", "threeshadows.co"),          # the wrong Chris Gavora
        ("ESIG Software e Consultoria em TI", "ontra.ai"),
        ("Deloitte Consulting", "mccombshq.com"),
        ("The Phoenix Firestorm Project", "icci.com"),
    ],
)
def test_an_unrelated_employer_never_corroborates(company, domain):
    assert pl.employer_matches(company, domain=domain) == ""


def test_stripping_a_suffix_never_leaves_a_stub_that_matches_anything():
    """"co.com" must not reduce to "" and then match every company on earth."""
    assert pl.employer_matches("Acme Widgets", domain="co.com") == ""


def test_free_mail_never_corroborates():
    """Otherwise every gmail contact corroborates against any company whose
    name happens to contain "gmail"-ish characters."""
    assert pl.employer_matches("Gmail Ltd", domain="gmail.com") == ""


# --- the whole resolution ---------------------------------------------------


def _resolve(contact, entity, urls=("https://linkedin.com/in/someone",), **kw):
    with patch.object(pl, "find_profile_urls", return_value=list(urls)):
        with patch.object(pl, "fetch_person", return_value=entity):
            return pl.resolve_person(contact, **kw)


def test_a_corroborated_person_is_proposed_with_evidence():
    out = _resolve(
        _contact("Chris Lefkovitz", "chris@leftbankholdings.com"),
        _entity("Christopher Lefkovitz", [("Founder and President", "Leftbank Holdings", None)]),
        known_company="Leftbank Holdings",
    )
    assert out["fields"]["jobtitle"] == "Founder and President"
    ev = out["evidence"]["jobtitle"]
    assert ev["source"] == "linkedin_profile_corroborated"
    assert "linkedin.com/in/" in ev["detail"]
    assert "Leftbank Holdings" in ev["detail"]


def test_the_linkedin_url_rides_along_only_when_missing():
    contact = _contact("Chris Lefkovitz", "chris@leftbankholdings.com")
    entity = _entity("Christopher Lefkovitz", [("Founder and President", "Leftbank Holdings", None)])
    out = _resolve(contact, entity, known_company="Leftbank Holdings")
    assert out["fields"]["hs_linkedin_url"] == "https://linkedin.com/in/someone"

    contact["hs_linkedin_url"] = "https://linkedin.com/in/already-known"
    out = _resolve(contact, entity, known_company="Leftbank Holdings")
    assert "hs_linkedin_url" not in out["fields"]


def test_company_is_filled_only_when_it_was_the_domain_that_corroborated():
    """If we matched on a company name we already had, there is no gap to fill."""
    out = _resolve(
        _contact("Greg Vance", "greg@holmesmurphy.com"),
        _entity("Greg Vance", [("Managing Partner", "Holmes Murphy & Associates", None)]),
    )
    assert out["fields"]["company"] == "Holmes Murphy & Associates"

    out = _resolve(
        _contact("Greg Vance", "greg@holmesmurphy.com"),
        _entity("Greg Vance", [("Managing Partner", "Holmes Murphy & Associates", None)]),
        known_company="Holmes Murphy",
    )
    assert "company" not in out["fields"]


def test_a_mailbox_short_circuits_before_any_network_call():
    with patch.object(pl, "find_profile_urls") as search:
        out = pl.resolve_person(_contact("Dunes Point Capital Team", "team@dunespoint.com"))
    assert out["skip"] == "not_a_person"
    search.assert_not_called()


def test_a_personal_address_with_no_employer_is_never_attempted():
    """Nothing to corroborate against -- this is precisely where a confident
    wrong title would sail through."""
    with patch.object(pl, "find_profile_urls") as search:
        assert pl.resolve_person(_contact("Sean Fitzgerald", "sean@gmail.com")) is None
    search.assert_not_called()


def test_someone_who_has_left_is_reported_not_filled():
    out = _resolve(
        _contact("Stephen Daubert", "stephen@cawoodcapital.com"),
        _entity("Stephen Daubert", [("Analyst", "Cawood Capital", {"to": "2024-03-01"})]),
        today=date(2026, 8, 14),
    )
    assert out["skip"] == "may_have_moved"
    assert "left" in out["reason"]


def test_a_second_candidate_is_tried_when_the_first_is_the_wrong_person():
    """Trying more candidates only ever finds more *true* positives -- a
    stranger still has to clear the employer check."""
    wrong = _entity("Chris Gavora", [("CFO", "Bockmann Inc.", None)])
    right = _entity("Chris Gavora", [("Owner", "Three Shadows", None)])
    with patch.object(pl, "find_profile_urls", return_value=["u1", "u2"]):
        with patch.object(pl, "fetch_person", side_effect=[wrong, right]):
            out = pl.resolve_person(
                _contact("Chris Gavora", "chris@threeshadows.co"), known_company="Three Shadows"
            )
    assert out["fields"]["jobtitle"] == "Owner"


def test_nothing_corroborating_yields_nothing():
    assert (
        _resolve(
            _contact("Chris Gavora", "chris@threeshadows.co"),
            _entity("Chris Gavora", [("CFO", "Bockmann Inc.", None)]),
            known_company="Three Shadows",
        )
        is None
    )


def test_a_failed_fetch_resolves_to_nothing_rather_than_something():
    assert (
        _resolve(_contact("Greg Vance", "greg@holmesmurphy.com"), None) is None
    )


# --- the LinkedIn URL already on the record ---------------------------------


def _on_record(**over):
    base = {
        "id": "1", "name": "Rich Halvas", "email": "rich@gmail.com", "company": "",
        "hs_linkedin_url": "https://www.linkedin.com/in/richhalvas",
    }
    base.update(over)
    return base


def test_a_stored_url_is_used_without_searching():
    """741 of Kory's contacts already carry one. Searching for a profile we
    have been handed is three network calls for nothing."""
    entity = _entity("Rich Halvas", [("CFO Advisory Services", "REH Consulting, LLC", None)])
    with patch.object(pl, "find_profile_urls") as search:
        with patch.object(pl, "fetch_person", return_value=entity):
            out = pl.resolve_person(_on_record())
    search.assert_not_called()
    assert out["fields"]["jobtitle"] == "CFO Advisory Services"
    assert out["fields"]["company"] == "REH Consulting, LLC"


def test_a_stored_url_is_labelled_as_the_weaker_source_that_it_is():
    """There is no employer on the record to check it against, so this rests on
    HubSpot's own URL plus a name match. It must not read as corroborated."""
    entity = _entity("Rich Halvas", [("CFO Advisory Services", "REH Consulting, LLC", None)])
    with patch.object(pl, "fetch_person", return_value=entity):
        out = pl.resolve_person(_on_record())
    ev = out["evidence"]["jobtitle"]
    assert ev["source"] == "linkedin_profile_on_record"
    assert ev["confidence"] == "on_record"
    assert "not independently corroborated" in ev["detail"].lower()


def test_the_stored_url_is_not_taken_on_trust():
    """Measured live: Phil Holland's stored URL resolves to Brian Holland.

    HubSpot's own LinkedIn URL can be a bad Sales Navigator match — the same
    failure that put an Australian software engineer on Thomas Heckler's
    record. Without the name check this writes a stranger's job onto him.
    """
    entity = _entity("Brian Holland", [("Owner", "Holland Construction", None)])
    with patch.object(pl, "fetch_person", return_value=entity):
        assert pl.resolve_person(_on_record(name="Phil Holland")) is None


def test_several_current_roles_is_a_question_not_a_pick():
    """Jeremy Boka is a VP, a brewery co-owner and a city councillor. Picking
    one would be inventing which relationship Kory has with him."""
    entity = _entity(
        "Jeremy Boka",
        [
            ("Vice President of Business Development", "Sustainable Sites Snow and Maintenance", None),
            ("Co Owner", "Reclaimed Rails Brewing Co.", None),
            ("National VP", "EIS Holdings, LLC", {"to": "2025-09-01"}),
        ],
    )
    with patch.object(pl, "fetch_person", return_value=entity):
        out = pl.resolve_person(_on_record(name="Jeremy Boka"), today=date(2026, 8, 14))
    assert out["skip"] == "several_current_roles"
    assert len(out["options"]) == 2
    assert "Reclaimed Rails" in out["reason"]


def test_a_council_seat_does_not_count_as_a_second_job():
    """Same contact, but the membership filter has to run first or every
    community role turns a clean answer into a question."""
    entity = _entity(
        "Jeremy Boka",
        [
            ("Vice President of Business Development", "Sustainable Sites", None),
            ("Member", "City of Altoona", None),
        ],
    )
    with patch.object(pl, "fetch_person", return_value=entity):
        out = pl.resolve_person(_on_record(name="Jeremy Boka"))
    assert out["fields"]["company"] == "Sustainable Sites"


def test_an_employer_on_file_that_the_profile_contradicts_is_never_downgraded():
    """The stored-URL path must not become a way around the employer check."""
    entity = _entity("Chris Gavora", [("CFO", "Bockmann Inc.", None)])
    with patch.object(pl, "fetch_person", return_value=entity):
        out = pl.resolve_person(
            _on_record(name="Chris Gavora", email="chris@threeshadows.co"),
            known_company="Three Shadows",
        )
    assert out is None


def test_a_stored_url_with_no_current_role_yields_nothing():
    entity = _entity("Rich Halvas", [("CFO", "AdvancedPCB", {"to": "2025-12-01"})])
    with patch.object(pl, "fetch_person", return_value=entity):
        assert pl.resolve_person(_on_record(), today=date(2026, 8, 14)) is None


# --- credentials in the name field ------------------------------------------


def test_a_credential_in_the_profile_name_is_not_a_first_name():
    """Found live: James Hite's profile comes back as "CRIS James Hite"."""
    assert pl.compare_names("James Hite", "CRIS James Hite")["match"]


def test_a_credential_on_the_crm_name_is_not_a_surname():
    """Found live: HubSpot holds "Jason Buesing PE"."""
    assert pl.compare_names("Jason Buesing PE", "Jason Buesing")["match"]


@pytest.mark.parametrize(
    "crm,profile",
    [
        ("Greg Krier CPA", "Greg Krier"),
        ("Karen Ho, MBA", "Karen Ho"),
        ("Dan Ruiz, PE", "Daniel Ruiz"),
    ],
)
def test_credentials_do_not_stop_a_match(crm, profile):
    assert pl.compare_names(crm, profile)["match"]


def test_stripping_credentials_never_empties_a_name_into_a_match():
    """"CPA" against "PE" must not reduce to two empty names that compare equal."""
    assert not pl.compare_names("CPA", "PE")["match"]


def test_the_search_is_never_run_without_a_name():
    with patch.object(pl, "find_profile_urls") as search:
        assert pl.resolve_person(_contact("", "greg@holmesmurphy.com"))["skip"] == "not_a_person"
    search.assert_not_called()
