# Email to Kory and Heidi — HubSpot cleanup and Sales Navigator

**Status: not sent. Waiting on Anjana.** Blocked deliberately — Anjana cannot
change Sales Navigator or HubSpot settings without Kory and Heidi's sign-off, so
this exists to get the decisions made rather than to report progress.

Source material and every measurement behind it: `HUBSPOT_DATA_CLEANUP_FINDINGS.md`.
That document is long and complete; this one is what actually goes in an inbox.

---

## Before sending — three things to get right

**Include Kelley.** `kelley.johnson@iconicfounders.com` is the Sales Navigator
admin, not Kory. Decisions 1–3 below are hers to execute. An email to Kory and
Heidi alone cannot action them.

**Do not say "Sales Navigator doesn't work."** It works; it solves a different
problem than the one we have. It watches contacts attached to live deals and
tells you when they move — and it caught 43 of Kory's who have left their
companies. Saying it is broken invites a correction and buries the real asks.
The accurate framing is *"it's scoped to deal contacts, and one setting is
unsafe."*

**Lead with the safety ask, not the statistics.** Decision 1 is the only item
here that can make the data actively worse, and it is a five-minute change.
Everything else can wait a week without cost.

---

## Draft

> **Subject:** HubSpot contact data — two decisions and one setting to change
>
> Hi Kory, Heidi — copying Kelley since two of these are Sales Navigator admin
> settings.
>
> I've spent the past week measuring what's actually wrong with the contact data
> and testing every route we have to fix it. Short version: the problem isn't the
> one we thought, one setting is currently unsafe, and the cheapest fix is already
> built and free.
>
> **The starting assumption was wrong, in a useful way.** We believed Sales
> Navigator was logging contacts and leaving fields blank. It isn't — Kory's book
> is 92–93% populated on company and job title. What's actually wrong is
> narrower: about 30 contacts carry the text *"Prefer No Connection to Company"*
> in the company field, which is what LinkedIn writes when someone hides their
> employer. It isn't blank, so it survives every "fill empty fields" tool and
> every report we had. It looks like data and isn't.
>
> **One setting needs changing, and I'd do this regardless of everything else.**
> In Sales Navigator, *"Require email address when updating contacts"* is
> currently **off**, while bulk update is on. That means it can match a LinkedIn
> profile to a HubSpot contact on name alone.
>
> Here is what that produced on a real record. Thomas Heckler — Heidi's contact,
> on the ICCI opportunity, based in Parker, Colorado — is flagged in Sales
> Navigator with an "Update CRM" badge. The update it proposes would write
> **"Open Source Software Engineer"** at **"The Phoenix Firestorm Project"** in
> **Gold Coast, Queensland, Australia**. Same name, different human, other side of
> the world.
>
> Both fields on that record are currently empty, so nothing visible would be
> overwritten — two honest blanks would quietly become two confident wrong
> values, with no attribution and no undo. We also have two different people
> called Chris Gavora in the database and 27 duplicate pairs, so this is not a
> one-off risk.
>
> **Turning that setting on costs us some matches and makes every remaining match
> the right person.** I'd recommend it before anyone runs an update from Sales
> Navigator.
>
> **What each tool can actually do**, all tested against the live portal:
>
> - **HubSpot's built-in enrichment** is switched on, correctly configured, and
>   has 10,000 credits a month that we have never used. It returns nothing for
>   this book — I enriched Chris Veum (President of a San Diego architecture
>   firm) and got "RECORD SKIPPED" with his title still blank. These datasets
>   cover tech and large enterprise; Kory's book is regional construction,
>   roofing, landscaping and insurance. It also doesn't offer company or phone as
>   fillable fields at all. Cost was never the blocker.
> - **Sales Navigator** matches 91% of our contacts, which is the good news. But
>   its data-quality view is scoped to accounts and open opportunities — 73 of
>   Kory's 1,023 contacts, not the whole book — and there's no book-wide bulk
>   backfill; every update is a person clicking Confirm. Where it *is* genuinely
>   valuable is the 43 contacts it has already identified as having left their
>   companies. Those records are confidently wrong rather than merely empty, and
>   that list already exists.
> - **Lexi** now fills 65 values across 59 contacts — company, job title and
>   phone — from HubSpot's own records, Kory's email signatures, and LinkedIn
>   profiles matched against the employer already on file. Every value carries
>   the evidence it came from, nothing is written without Kory approving it, and
>   all of it can be undone. It's also the only thing that touches those ~30
>   placeholder contacts, because their fields aren't empty.
>
> **The one thing nothing can fix: phone numbers.** About 285 contacts have none.
> HubSpot doesn't offer phone as an enrichable field, LinkedIn doesn't publish
> it, and the only automated source is a signature in Kory's own inbox. That's a
> wall, not a backlog. Worth deciding whether we need numbers for the whole book
> or only the hundred Kory actually deals with.
>
> **What I need from you:**
>
> | # | Decision | Who |
> |---|---|---|
> | 1 | Turn on "Require email address when updating contacts" in Sales Navigator | Kelley |
> | 2 | Confirm Kory has completed his own Sales Navigator authentication to HubSpot | Kory |
> | 3 | Review what the "Create contacts" mapping writes, so new gaps stop appearing | Kelley |
> | 4 | OK for Kory to start approving Lexi's fills in Teams | Kory |
> | 5 | Whether to work the 43 "left their company" contacts, and who does it | Kory / Heidi |
> | 6 | Whether phone numbers are wanted book-wide or only for active contacts | Kory |
>
> Nothing here changes anything until you say so. Happy to walk through any of it.
>
> Anjana

---

## Numbers, if anyone asks

| | |
|---|---|
| Kory's contacts | 1,023 of 2,224 portal-wide |
| Missing phone | 285–307 |
| Missing company / job title | 97 / 80 |
| Carrying a placeholder | ~30 |
| Duplicate pairs | 27, of which 16 involve colleagues' records |
| Contacts with a LinkedIn URL on file | 741 |
| Sales Nav match rate | 91% (2,375 of 2,615) |
| Sales Nav CRM-synced *contacts* | **73** — against 748 of 748 accounts |
| HubSpot enrichment credits used | 0 of 10,000, resets monthly |
| Lexi fills | 65 values / 59 contacts — company 36, phone 16, title 13 |

## Two things to be careful not to overclaim

**Lexi's 28%.** She reaches about 28% of the title and company gaps. An earlier
draft of the findings said 40%; that was an estimate and the measured figure is
lower. Quote 28%, or just quote the raw counts.

**"LinkedIn data" is not the same as "Sales Navigator".** Lexi reads public
profile data through a search provider, not through Sales Navigator, and there is
no API that would let her drive Sales Navigator. If anyone asks whether we can
automate the Sales Navigator side: no, and that isn't a licensing question.
