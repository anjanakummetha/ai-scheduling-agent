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

### LinkedIn Sales Navigator — the one that fits this book

**91% of contacts matched — 2,375 of 2,615. Accounts 98% — 1,961 of 1,996.**

This is the crucial number. LinkedIn *has* these people, because profiles are
self-reported by the individuals themselves — so a president of a small San
Diego firm has a profile whether or not any data vendor covers his employer.
That is precisely where HubSpot's enrichment failed.

CRM sync is **connected** to HubSpot (Production), access level **read and
write**, "You can export Sales Navigator data to your CRM."

Under **Exported to CRM → Update contacts → Customize**:

- ✅ **Bulk update contacts — ON.** This is the bulk mechanism.
- ⚠️ **Require email address when updating contacts — OFF.** See §3.

Data Validation exists for HubSpot (confirmed in LinkedIn's own integration
description), but note what it does: *"Identify when CRM Contacts are
out-of-date and no longer with their company."* **It flags stale records; it
does not fill blank ones.** Valuable for accuracy over time, not a backfill.

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

---

## 4. Still unknown

1. **What fields does "Update contacts" actually write?** The Customize dialog
   showed matching behaviour, not a field list. Until this is answered we do not
   know whether it fills job title and company.
2. **What does "Create contacts" → Customize map?** If new contacts arrive from
   Sales Navigator with a thin field mapping, that is the *source* of the whole
   problem, and fixing it stops new gaps appearing.
3. **What does the Companies tab of the HubSpot enrichment mapping cover?**
   HubSpot will not fill a contact's company field, but it can enrich Company
   *records* — and Lexi's best source is already the contact→company
   association. Enriching companies could improve that lookup.
4. **Did the other enrichment attempts also skip?** Only one Activity entry was
   opened. If all say skipped, HubSpot enrichment is not worth pursuing here.

---

## 5. Decisions needed from Kory and Heidi

| # | Decision | Owner | Note |
|---|---|---|---|
| 1 | Turn ON "Require email address when updating contacts" | Kelley / Sales Nav admin | Safety. Recommended. |
| 2 | Confirm and set the field mapping for **Update contacts** | Kelley | Decides whether this works at all |
| 3 | Review the field mapping for **Create contacts** | Kelley | Stops new gaps at source |
| 4 | Run a bulk update from Sales Navigator | Kory / Heidi | The actual fix, once 1–3 are settled |
| 5 | Turn ON "Automatically enrich recently engaged contact" in HubSpot | Heidi | Free; helps going forward, not the backlog |
| 6 | Apply Lexi's 39 staged fills (`hs-8c09e003f9b8`) | Kory | Free, reversible, already verified |
| 7 | Decide whether to merge any of the 11 duplicate pairs Kory owns | Kory | **Permanent.** The other 16 are colleagues' records |

---

## 6. Recommended sequence

1. **Sales Navigator first** — it is the only source that covers this book, it is
   already paid for, and the bulk mechanism is already on. Settle the field
   mapping and the email-match setting, then run it.
2. **Then re-run Lexi's scan.** She only fills blanks and re-checks at apply
   time, so she naturally mops up whatever LinkedIn leaves — including the ~30
   placeholder contacts, which *neither* HubSpot enrichment nor a bulk update
   will touch, because those fields are not empty.
3. **Phone stays open.** No source available fills it: HubSpot does not offer it,
   Sales Navigator does not carry it. Lexi's 16 signature-derived numbers are the
   only automated coverage. Worth asking whether Kory needs phone numbers for all
   1,023 contacts or only the ~100 he actually deals with.

---

## 7. What Lexi keeps doing regardless

She was never going to be a good bulk data *source* — 9% proves that. What she
covers that nothing else does:

- **The ~30 placeholder contacts** — every other tool skips non-empty fields
- **Phone from Kory's own signatures** — no other source offers phone
- **Meeting notes**, and **duplicate proposals labelled with the record owner**
- **Evidence, ownership guard and full undo** on everything she writes
- New contacts as they arrive, so the gap does not reopen

See `SESSION_HANDOFF.md` for the engineering detail.
