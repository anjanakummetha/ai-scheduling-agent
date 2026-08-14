"""Tier 0 must never invent an employer.

Everything here resolves to a value a human already entered into HubSpot. The
tests that matter are the refusals: a wrong company written into the shared IFG
CRM reads as authoritative and nobody re-checks it, so "I don't know" has to
beat "probably".
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import app.integrations.hubspot_company_lookup as cl
from app.integrations.hubspot_manager import is_placeholder


@pytest.fixture(autouse=True)
def _clear_cache():
    cl.reset_cache()
    yield
    cl.reset_cache()


def _contact(cid, email, company=""):
    return {"id": cid, "email": email, "company": company}


# --- domains ----------------------------------------------------------------


def test_free_mail_is_never_treated_as_an_employer():
    """A Company object carrying gmail.com would name one employer for every
    personal address in the book."""
    for addr in ("a@gmail.com", "b@icloud.com", "c@me.com", "d@comcast.net", "e@proton.me"):
        assert not cl.is_corporate_domain(addr)


def test_a_company_domain_is_recognised():
    assert cl.is_corporate_domain("greg@holmesmurphy.com")
    assert cl.domain_of("Greg@HolmesMurphy.com ") == "holmesmurphy.com"


def test_a_malformed_address_has_no_domain():
    assert cl.domain_of("not-an-address") == ""
    assert cl.domain_of("") == ""
    assert cl.domain_of(None) == ""


# --- consensus across Kory's own book ---------------------------------------


def test_consensus_needs_more_than_one_contact_agreeing():
    """One contact is a single data point, and may be the typo we are avoiding."""
    one = [_contact("1", "a@alpineinvestors.com", "Alpine Investors")]
    assert cl.build_domain_consensus(one, is_placeholder=is_placeholder) == {}

    two = one + [_contact("2", "b@alpineinvestors.com", "Alpine Investors")]
    assert cl.build_domain_consensus(two, is_placeholder=is_placeholder) == {
        "alpineinvestors.com": "Alpine Investors"
    }


def test_disagreement_disqualifies_the_domain_entirely():
    """Both spellings are plausible. Picking one would be inventing a fact."""
    contacts = [
        _contact("1", "a@a-mcapital.com", "Alvarez & Marsal Capital"),
        _contact("2", "b@a-mcapital.com", "Alvarez & Marsal Capital"),
        _contact("3", "c@a-mcapital.com", "A&M Capital"),
    ]
    assert cl.build_domain_consensus(contacts, is_placeholder=is_placeholder) == {}


def test_punctuation_differences_are_not_disagreement():
    contacts = [
        _contact("1", "a@bokf.com", "BOK Financial"),
        _contact("2", "b@bokf.com", "BOK Financial."),
    ]
    out = cl.build_domain_consensus(contacts, is_placeholder=is_placeholder)
    assert out["bokf.com"] in {"BOK Financial", "BOK Financial."}


def test_placeholder_values_never_become_consensus():
    """Otherwise 'Prefer No Connection to Company' becomes the agreed employer."""
    contacts = [
        _contact("1", "a@imacorp.com", "Prefer No Connection to Company"),
        _contact("2", "b@imacorp.com", "Prefer No Connection to Company"),
    ]
    assert cl.build_domain_consensus(contacts, is_placeholder=is_placeholder) == {}


def test_free_mail_never_forms_a_consensus():
    contacts = [
        _contact("1", "a@gmail.com", "Acme"),
        _contact("2", "b@gmail.com", "Acme"),
        _contact("3", "c@gmail.com", "Acme"),
    ]
    assert cl.build_domain_consensus(contacts, is_placeholder=is_placeholder) == {}


# --- the association: HubSpot already knows -------------------------------


def _assoc(ids):
    return {"data": {"results": [{"toObjectId": i} for i in ids]}}


def _company(name, cid="77"):
    return {"data": {"results": [{"id": cid, "properties": {"name": name}}]}}


def test_an_associated_company_is_used_directly():
    with patch.object(cl, "execute_hubspot_tool", side_effect=[_assoc(["77"]), _company("IMA Financial Group")]):
        out = cl.company_from_association("9")
    assert out["value"] == "IMA Financial Group"
    assert out["source"] == "hubspot_company_association"
    assert "#77" in out["evidence"]


def test_two_associated_companies_is_a_question_not_a_coin_toss():
    with patch.object(cl, "execute_hubspot_tool", return_value=_assoc(["77", "88"])):
        assert cl.company_from_association("9") is None


def test_an_unnamed_company_object_is_not_an_answer():
    """Measured on Kory's portal: several Company objects carry a domain and no
    name at all."""
    with patch.object(cl, "execute_hubspot_tool", side_effect=[_assoc(["77"]), _company("")]):
        assert cl.company_from_association("9") is None


def test_a_failed_association_read_resolves_to_nothing():
    with patch.object(cl, "execute_hubspot_tool", side_effect=RuntimeError("Composio down")):
        assert cl.company_from_association("9") is None


# --- the domain -> Company object path -------------------------------------


def _search(names):
    return {"data": {"results": [{"properties": {"name": n}} for n in names]}}


def test_a_company_record_for_the_domain_resolves_it():
    with patch.object(cl, "execute_hubspot_tool", return_value=_search(["Holmes Murphy"])) as call:
        out = cl.company_from_domain_object("greg@holmesmurphy.com")
    assert out["value"] == "Holmes Murphy"
    filters = call.call_args[0][1]["filterGroups"][0]["filters"][0]
    assert filters == {"propertyName": "domain", "operator": "EQ", "value": "holmesmurphy.com"}


def test_two_company_records_for_one_domain_resolve_to_nothing():
    with patch.object(cl, "execute_hubspot_tool", return_value=_search(["Acme", "Acme Holdings"])):
        assert cl.company_from_domain_object("a@acme.com") is None


def test_free_mail_never_reaches_the_company_search():
    with patch.object(cl, "execute_hubspot_tool") as call:
        assert cl.company_from_domain_object("someone@gmail.com") is None
    call.assert_not_called()


def test_the_domain_lookup_is_cached_across_contacts():
    """Kory has ten contacts at alpineinvestors.com; that is one lookup."""
    with patch.object(cl, "execute_hubspot_tool", return_value=_search(["Alpine Investors"])) as call:
        first = cl.company_from_domain_object("a@alpineinvestors.com")
        second = cl.company_from_domain_object("b@alpineinvestors.com")
    assert first["value"] == second["value"] == "Alpine Investors"
    assert call.call_count == 1


def test_a_domain_that_resolved_to_nothing_is_not_retried():
    with patch.object(cl, "execute_hubspot_tool", return_value=_search([])) as call:
        assert cl.company_from_domain_object("a@nowhere.com") is None
        assert cl.company_from_domain_object("b@nowhere.com") is None
    assert call.call_count == 1


# --- the company's own website ----------------------------------------------


@pytest.mark.parametrize(
    "title,domain,expected",
    [
        ("Sky Group | Commercial Roofing Contractors", "skygroup-co.com", "Sky Group"),
        ("ConvergeCX - Customer Experience", "convergecx.com", "ConvergeCX"),
        ("True View Windows and Glass — Denver", "trueviewglass.com", "True View Windows and Glass"),
        ("SPQ Holdings", "spqholdings.com", "SPQ Holdings"),
    ],
)
def test_a_site_title_that_matches_its_domain_is_the_company_name(title, domain, expected):
    assert cl._company_name_from_title(title, domain) == expected


@pytest.mark.parametrize(
    "title,domain",
    [
        ("Coming Soon", "cawoodcapital.com"),          # squarespace parking page (real)
        ("Outlook", "mccombshq.com"),                   # webmail login, not a site (real)
        ("Home", "enviromotiv.com"),
        ("Welcome to our website", "cawoodcapital.com"),
        ("Buy this domain | Sedo", "skygroup-co.com"),  # domain squatter
        ("Acme Widgets Inc", "totallyunrelated.com"),   # title unrelated to the domain
    ],
)
def test_a_title_that_is_not_the_company_is_refused(title, domain):
    """All four of the first cases are live pages behind Kory's real contacts.

    Writing "Coming Soon" or "Outlook" into the company field would be worse
    than the blank it replaced — it looks like real data.
    """
    assert cl._company_name_from_title(title, domain) == ""


def test_a_failed_crawl_yields_nothing_rather_than_something():
    with patch("app.integrations.composio_search.search_enabled", return_value=True):
        with patch("app.integrations.composio_search.fetch_url_content",
                   return_value={"data": {"results": [], "statuses": [{"status": "error"}]}}):
            assert cl.company_from_website("a@skygroup-co.com") is None


def test_the_website_is_never_consulted_for_a_personal_address():
    with patch("app.integrations.composio_search.fetch_url_content") as fetch:
        assert cl.company_from_website("someone@gmail.com") is None
    fetch.assert_not_called()


def test_the_website_tier_is_opt_in():
    """resolve_company must not reach the open web unless asked to."""
    contact = _contact("9", "a@unheardof.com")
    with patch.object(cl, "company_from_association", return_value=None):
        with patch.object(cl, "company_from_domain_object", return_value=None):
            with patch.object(cl, "company_from_website") as web:
                assert cl.resolve_company(contact, consensus={}) is None
    web.assert_not_called()


# --- the whole resolution ---------------------------------------------------


def test_the_association_wins_over_the_domain():
    """The association is about this person; the domain is about their mail.

    Someone can use a company address and work somewhere else, and HubSpot's own
    link is the more specific fact.
    """
    contact = _contact("9", "greg@holmesmurphy.com")
    with patch.object(cl, "company_from_association", return_value={"value": "Real Employer", "source": "hubspot_company_association", "evidence": "x"}):
        with patch.object(cl, "company_from_domain_object") as domain:
            out = cl.resolve_company(contact)
    assert out["value"] == "Real Employer"
    domain.assert_not_called()


def test_consensus_is_the_last_resort():
    contact = _contact("9", "a@bowrivercapital.com")
    with patch.object(cl, "company_from_association", return_value=None):
        with patch.object(cl, "company_from_domain_object", return_value=None):
            out = cl.resolve_company(contact, consensus={"bowrivercapital.com": "Bow River Capital"})
    assert out["value"] == "Bow River Capital"
    assert out["source"] == "kory_book_consensus"


def test_nothing_known_means_nothing_proposed():
    contact = _contact("9", "someone@unheardof.com")
    with patch.object(cl, "company_from_association", return_value=None):
        with patch.object(cl, "company_from_domain_object", return_value=None):
            assert cl.resolve_company(contact, consensus={}) is None
