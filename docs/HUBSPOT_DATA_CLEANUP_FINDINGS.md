# HubSpot data cleanup — findings

**Investigated 2026-08-14.** Everything below is measured against the live portal
or read directly from the settings pages, not inferred. Written up as source
material for a decision email to Kory and Heidi.

---

## 1. The starting assumption was wrong, in a useful way

The premise was: *LinkedIn Sales Navigator auto-logs contacts into HubSpot and a
lot of the information is missing.*

**Sales Navigator is not leaving job title and company blank.** Kory's book is
92–93% populated on both. What is actually wrong is narrower and different:

| Gap | Count | % of his book |
|---|---:|---:|
| Missing phone | 307 | 30% |
| Never contacted | 102 | 10% |
| Missing job title | 80 | 8% |
| Missing company | 97* | 9% |
| Marked Do Not Contact | 62 | 6% |

\* 76 genuinely blank, plus ~21 holding a placeholder.

**The placeholder problem is the one nobody could see.** `Prefer No Connection
to Company` — what LinkedIn writes when a member hides their employer — appears
12 times per 400 contacts sampled, roughly 30 book-wide. It is *not blank*, so
it survives every "fill empty fields" filter in HubSpot and every blank-only
check we had. HubSpot's own health report counted 2 placeholder values before
we widened the detection; it now reports 14 per 400.

**Portal totals:** 2,224 contacts across IFG · **1,022 owned by Kory** ·
741 of his (72%) already carry a LinkedIn URL · 27 duplicate pairs on a
complete scan, **16 of which involve records owned by Natalie, Matt, or a
deactivated user (owner id 159291600)**.

---

## 2. What each tool can actually do

### Lexi — ceiling of ~9%, and it is a hard ceiling

A full sweep of the book produced **43 contacts / 46 field values**:

| Field | Gaps | Lexi fills | Source |
|---|---:|---:|---|
| Company | 97 | 26 | HubSpot's own company associations (21), company websites (5) |
| Job title | 80 | 4 | Email signatures |
| Phone | 307 | 16 | Email signatures |

Her only inputs are HubSpot itself and Kory's inbox. Most of these people have
never emailed him, so there is no signature to read. **This is structural, not
a tuning problem — re-running finds nothing new.**

Four of the 46 were rejected as junk: `firstbiz.org`, `clutchanchor.com`,
`freshpowertidip.org` and `eastlandlinelink.com` all serve an identical
"Growth is a system" template and one has no contact name. **Defensible set: 39
fills**, staged as batch `hs-8c09e003f9b8`, not yet applied.

### HubSpot Data Enrichment — enabled, and it found nothing

Settings are **correct**: both auto-enrich toggles on, "fill empty values only",
Job Title *is* mapped.

**But it returns no data for Kory's contacts.** A manual enrichment of Chris
Veum (President, AVRP Studios — a San Diego architecture firm) came back
`RECORD SKIPPED — Existing property values were not overwritten`, with his job
title still blank.

The likely reason: these commercial datasets are built on tech and large
enterprises. Kory's book is small regional construction, roofing, landscaping
and insurance firms. **Coverage on this book appears to be very low.**

Two hard limits that are *not* settings and cannot be toggled away:

- **Company is not offered** as a contact enrichment property at all
- **Phone is not offered** at all

The 12 mapped contact properties: First Name, Last Name, Employment Role,
Employment Sub Role, Employment Seniority, City, State/Region Code,
Country/Region Code, LinkedIn URL, State/Region, Country/Region, Job Title.

**Credits: 10,000 per month, `0 of 10,000` used, resets Aug 30.** Cost was never
the blocker. Unused credits do not roll over.

### LinkedIn Sales Navigator — the only source that covers this book, but not the way we assumed

**91% of contacts matched — 2,375 of 2,615. Accounts 98% — 1,961 of 1,996.**

This is the crucial number. LinkedIn *has* these people, because profiles are
self-reported by the individuals themselves — so a president of a small San
Diego firm has a profile whether or not any data vendor covers his employer.
That is precisely where HubSpot's enrichment failed.

CRM sync is **connected** to HubSpot (Production), access level **read and
write**, "You can export Sales Navigator data to your CRM."

**The mechanism is "Next Generation CRM Data Validation."** LinkedIn's help
documentation is explicit that this is the HubSpot variant: *"Sales Navigator
Advanced Plus customers using HubSpot or Oracle Sales use the Next Generation
CRM Data Validation feature"* — Salesforce and Dynamics get the older, weaker
version. IFG has exactly the plan combination this requires.

What it covers, in LinkedIn's own words: it validates *"more than just the
account on the contact record; flagging when job title, account details and
account location are out of date **or missing**."*

**That "or missing" matters — it means Sales Navigator does address blank
fields, not only stale ones.** An earlier draft of this document said the
opposite. Correcting it changes the conclusion: this is a real backfill route
for job title and company.

**Three limits, all verified against LinkedIn's documentation:**

1. **Three fields only — job title, company/account, account location.**
   **Phone is not covered.** No tool available to IFG fills phone. That is the
   single largest gap in the book (307 contacts) and it stays open.

2. **It is not a background job. Every write is a human click.** A contact whose
   data is missing or stale shows a *"Update CRM"* badge with a red exclamation
   mark in search results, profile views and lead lists. The user opens it,
   LinkedIn pre-fills the correct values, and the user clicks **Confirm
   updates**. Nothing is written until someone confirms it.

3. **"Bulk update contacts" is far narrower than the name suggests.** It is
   scoped to a **Relationship Map** — a single saved account, with people added
   to it manually by a rep. LinkedIn's admin help: *"enable updating lead
   information in bulk from a Relationship Map."* There is **no book-wide bulk
   backfill** across all accounts. With ~1,961 matched accounts, building a
   Relationship Map per account is not a viable path.

4. **It is opportunity-scoped, and that is the biggest limit of all.** Measured
   in Kory's own Sales Navigator:

   | List | Count |
   |---|---:|
   | My CRM Accounts | 748 |
   | **My CRM Leads and Contacts** | **73** |
   | Contacts Who Have Left Open Opportunities | 43 |
   | Changed jobs in past 90 days | 2 |

   The account side mirrors HubSpot exactly — 748 accounts owned, 748 synced.
   The contact side does not: **73 of his 1,023 contacts.** LinkedIn's own
   description says why — *"Includes key CRM Leads and Contacts associated with
   your Accounts and/or Opportunities."*

   **Sales Navigator watches contacts attached to live deals.** Kory's 177
   blank-field contacts are overwhelmingly in the long tail that is *not*
   attached to an open opportunity — which is why they were neglected in the
   first place. They can still be found one at a time by search, but they do not
   appear in any ready-made list.

**So Sales Navigator is very good at a problem we do not have — keeping deal
contacts current — and largely absent from the one we do.** The realistic shape
of the work is a manual pass, one contact at a time, over a list we build
ourselves. See §6.

**Two prerequisites that are easy to miss:**

- **Kory must individually authenticate his own seat.** From the enablement
  guide: *"Lead Creation, Contact Creation, and Activity Writeback requires
  users to individually authenticate (user-auth) with the CRM. This is in
  addition to admin-level authentication."* Kelley's admin connection is not
  enough. If Kory has not done this, none of it works for him.
- **He needs read and write permission on each record.** *"Sales Navigator
  respects CRM permissions."* The 16 duplicate pairs owned by colleagues are
  therefore out of reach for him anyway.

**There is a sandbox, and it solves our testing problem.** The guide: *"You can
connect to a CRM Sandbox environment to initially test CRM Sync with Sales
Navigator and/or test new CRM features before releasing to all your users."*
The field mapping can be proven there before anything touches Kory's real
contacts.

**It also works from inside HubSpot.** *"Create new or update existing CRM
contacts using Sales Navigator data both from Sales Navigator into your CRM and
from the Embedded Experience into your CRM."* The Embedded Experience is the
Sales Navigator panel on the HubSpot contact record — likely the least
disruptive place for Kory to work, since he never leaves the CRM.

Under **Exported to CRM → Update contacts → Customize**:

- ✅ **Bulk update contacts — ON** (Relationship-Map-scoped, per above)
- ⚠️ **Require email address when updating contacts — OFF.** See §3.

Note: the badges themselves are *"automatically enabled for all customers that
have established their CRM Sync connection"* — which is why there is no
"Data Validation" page to find in the settings UI. It surfaces as badges in the
Sales Navigator interface, not as a toggle.

**Sales Navigator admin is `kelley.johnson@iconicfounders.com`** — not Kory.

---

## 3. The one setting that worries me

**"Require email address when updating contacts from Sales Navigator" is OFF.**

With it off, Sales Navigator can match a LinkedIn profile to a HubSpot contact
without an email match, presumably falling back to name plus company.

That is risky in *this* database specifically:

- There are **two Chris Gavoras** (`chris@threeshadows.co` and
  `cgavora@bockmanninc.com`)
- There are **27 known duplicate pairs**
- A wrong match writes one person's job title onto another person's record

Unlike Lexi's writes, a Sales Navigator update carries **no evidence trail and
no undo**. Recommend turning it on: fewer matches, but every match is the right
human. It can be loosened later if coverage disappoints.

### This is no longer hypothetical — here is a live one

Opened the **Update CRM** badge on the first flagged contact in Kory's list,
**Thomas Heckler**. What Sales Navigator reported about the CRM record:

| Field | CRM value |
|---|---|
| Title | *N/A* |
| Account | *None* |
| Location | Parker |
| Contact owner | **Heidi Heckler** |
| Most recent opportunity | ICCI |

And what LinkedIn proposed to write, from the lead it matched him to:
**"Open Source Software Engineer"** at **"The Phoenix Firestorm Project, Inc"**,
**Gold Coast, Queensland, Australia**.

Three separate things are wrong with that, and each one is worth its own line in
the email:

1. **The person is almost certainly not the same human.** The CRM has him in
   Parker (Colorado) attached to an ICCI opportunity — industrial commissioning,
   Greater Houston. LinkedIn has an open-source software engineer in Australia.
   Same name, nothing else in common. **This is exactly the failure mode the
   email-match setting prevents, occurring on a real record.**
2. **The record is not Kory's.** The contact owner is Heidi. It appears in his
   list because he owns the *opportunity*, not the contact. Sales Navigator has
   no ownership guard — a **Confirm updates** click from Kory would write to
   Heidi's contact.
3. **It would have looked correct afterwards.** Both fields are currently empty,
   so nothing would be visibly overwritten. A blank field would become a
   confident, wrong, unattributed value with no undo.

**This also settles the open question about blank fields.** Title is *N/A* and
Account is *None*, and the badge fired anyway. Data Validation does flag missing
values, not only stale ones — the help documentation is right and the enablement
guide's shorter summary was incomplete. It also confirms the field set by
demonstration: **Title, Account, Location. No phone.**

---

## 4. Still unknown

1. **Has Kory individually authenticated his Sales Navigator seat to HubSpot?**
   This is the first thing to check — admin-level connection is not enough, and
   without it nothing in §6 works for him. Cheapest possible test: open one of
   his contacts in Sales Navigator and look for an **Update CRM** badge.
2. **How many of his 177 title/company gaps actually show a badge?** 91% of
   contacts are matched, but matched is not the same as flagged. Checking twenty
   contacts by hand would turn the estimate below into a real number.
3. **What does "Create contacts" → Customize map?** If new contacts arrive from
   Sales Navigator with a thin field mapping, that is the *source* of the whole
   problem, and fixing it stops new gaps appearing.
4. **What does the Companies tab of the HubSpot enrichment mapping cover?**
   HubSpot will not fill a contact's company field, but it can enrich Company
   *records* — and Lexi's best source is already the contact→company
   association. Enriching companies could improve that lookup.
5. **Did the other enrichment attempts also skip?** Only one Activity entry was
   opened. If all say skipped, HubSpot enrichment is not worth pursuing here.

One documented tension, recorded honestly: the enablement guide summarises Data
Validation as *"identify when CRM contacts are out-of-date"* with no mention of
missing values, while the dedicated help article says *"out of date or
missing."* The help article is more specific and describes the newer
HubSpot-specific version, so it is the one relied on above — but item 2 is what
actually settles it, and it should be checked before anyone commits to this
plan.

---

## 5. Decisions needed from Kory and Heidi

| # | Decision | Owner | Note |
|---|---|---|---|
| 1 | Confirm Kory has completed Sales Navigator **user-auth** to HubSpot | Kory | Prerequisite for everything below |
| 2 | Turn ON "Require email address when updating contacts" | Kelley / Sales Nav admin | Safety. Recommended. |
| 3 | Spot-check 20 contacts for an **Update CRM** badge | Kory / Anjana | Turns the estimate into a number before committing |
| 4 | Review the field mapping for **Create contacts** | Kelley | Stops new gaps at source |
| 5 | Decide who does the manual Sales Navigator pass, and over which contacts | Kory / Heidi | ~177 records; not a background job — see §6 |
| 6 | Turn ON "Automatically enrich recently engaged contact" in HubSpot | Heidi | Free; helps going forward, not the backlog |
| 7 | Apply Lexi's fills — she generates a fresh batch on request in Teams | Kory | Free, reversible, evidence on every value |
| 8 | Decide whether to merge any of the 11 duplicate pairs Kory owns | Kory | **Permanent.** The other 16 are colleagues' records |
| 9 | Decide whether phone numbers are wanted for the whole book or just active contacts | Kory | Nothing automated fills phone — see §6.3 |

---

## 6. Recommended sequence

1. **Prove the route on twenty contacts before planning the work.** Check
   user-auth, open twenty of the 177 gap contacts in Sales Navigator, and count
   how many show an **Update CRM** badge. If it is most of them, this works and
   the size of the job is known. If it is few, the plan changes and almost no
   effort has been spent. This costs under an hour and removes the only real
   uncertainty left.

2. **Then the manual pass — and it should be a worklist, not a hunt.** There is
   no book-wide bulk update, so someone clicks through roughly 177 contacts.
   That is tedious but bounded, and it does not have to be Kory: it needs a
   Sales Navigator seat with write permission, not judgement about the
   relationships. **Lexi can generate the exact worklist** — the specific
   contacts missing title or company, with their LinkedIn URLs (741 of Kory's
   contacts already carry one) — so whoever does the pass works a list instead
   of searching. Doing it from the **Embedded Experience inside HubSpot** keeps
   it in one window.

3. **Then re-run Lexi's scan.** She only fills blanks and re-checks at apply
   time, so she naturally mops up whatever LinkedIn leaves — including the ~30
   placeholder contacts, which *neither* HubSpot enrichment nor Sales Navigator
   will reliably touch, because those fields are not empty.

4. **Phone stays open, and there is no route to close it.** HubSpot enrichment
   does not offer phone as a property; Sales Navigator validates only title,
   account and location. Lexi's 16 signature-derived numbers are the only
   automated coverage that exists. This is worth stating plainly to Kory rather
   than leaving as an open action, because the honest answer is that 307 missing
   phone numbers will not be filled by any tool IFG currently owns — the choice
   is manual effort, a different vendor, or accepting the gap for contacts he
   does not actively work.

**Test in the sandbox first if there is any doubt about the field mapping.**
LinkedIn supports connecting Sales Navigator to a HubSpot sandbox precisely so
this can be validated without touching production.

---

## 7. What Lexi keeps doing regardless

She was never going to be a good bulk data *source* — 9% proves that. What she
covers that nothing else does:

- **The ~30 placeholder contacts** — every other tool skips non-empty fields
- **Phone from Kory's own signatures** — no other source offers phone
- **Meeting notes**, and **duplicate proposals labelled with the record owner**
- **Evidence, ownership guard and full undo** on everything she writes
- New contacts as they arrive, so the gap does not reopen
- **The worklist for the Sales Navigator pass** — this turned out to be her most
  valuable contribution to the cleanup. She cannot fill these fields, but she
  can say exactly which contacts need attention and hand over a list, which is
  the difference between a bounded afternoon and an open-ended chore

See `SESSION_HANDOFF.md` for the engineering detail.

---

## Sources

Everything about Sales Navigator's capabilities above is from LinkedIn's own
documentation, not inference:

- [Next Generation CRM Data Validation in Sales Navigator](https://www.linkedin.com/help/sales-navigator/answer/a738351) — the HubSpot-specific feature; "out of date or missing"; job title, account, account location
- [Update Leads and Contacts](https://www.linkedin.com/help/sales-navigator/answer/a594009) — the Update CRM badge, and "Confirm updates" as a human action
- [Sales Navigator Admin Settings for CRM](https://www.linkedin.com/help/sales-navigator/answer/a107066) — bulk update is "from a Relationship Map"; the email-address requirement
- [Relationship Maps in Sales Navigator Account Pages](https://www.linkedin.com/help/sales-navigator/answer/a456397) — per-account scope, leads added manually
- [Sales Navigator CRM Sync Permissions](https://www.linkedin.com/help/sales-navigator/answer/a158016) — read and write permission per record
- [CRM Sync for HubSpot Enablement Guide](https://business.linkedin.com/sales-solutions/sales-navigator-customer-hub/resources/crm-sync-hubspot-enablement-guide) (PDF) — user-auth requirement, sandbox support, Embedded Experience
- [HubSpot: Connect HubSpot and LinkedIn CRM sync](https://knowledge.hubspot.com/integrations/connect-hubspot-and-linkedin-crm-sync) — plan requirements (Sales Hub Pro/Enterprise + Advanced Plus)
