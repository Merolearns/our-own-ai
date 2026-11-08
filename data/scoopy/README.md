# Scoopy Doog agent data

This directory holds the knowledge base for the Scoopy Doog agent (agent 2):
services, pricing, booking, and FAQ. Facts come from scoopydoog.com
(homepage, service page, FAQ), read November 8, 2026.

## What the agent may say

Only what is in these files. Anything marked [VERIFY] is not confirmed and
must never be stated as fact until it is verified.

## Login

Real Scoopy Doog customers sign in with Google OAuth (Authlib, `oauth.py`).
Creating the Google OAuth client is a parent-side setup for later (about
10 minutes in Google Cloud Console); the client secret goes in the VPS
`.env`, never the repo. Until that exists, logins fall back to PBKDF2
password accounts, the same pattern as the Parcelco demo accounts, and
the "Continue with Google" link stays hidden. Exact parent steps:
`deploy/CHECKLIST.md` §5b.

## Domain note

The Scoopy Doog domain on record is dooscooping.com, but a direct fetch of it
failed on November 8, 2026. All facts here come from scoopydoog.com, which
carries identical branding and the same contact details. Use scoopydoog.com
links until the dooscooping.com redirect is confirmed. [VERIFY]

## To verify before the agent quotes it

- Exact per-frequency prices (weekly, bi-weekly, one-time, 2x/week). The site
  publishes only "$60 per month starting" and "$100-$135 typical".
- The exact list of Orange County cities covered.
- Whether any satisfaction or money-back guarantee exists.
- Whether any first-cleanup-free or other promotion exists.
- Year founded / how long in business.
- Instagram follower and post counts today.
- Whether dooscooping.com redirects to scoopydoog.com.

## Re-read, November 8, 2026 (agent build)

The site was re-read during the build. Confirmed firsthand from the
homepage (crawled Nov 8, 2026) and the service-page search record:
frequencies (2x/week, weekly, bi-weekly, monthly), the two-times-per-week
recommendation, Orange County only, hours Mon–Sat 7am–6pm, the Free Quote
flow (zip code + signup), billing on the 1st of the month before services,
automatic payments (bank/card), invoice + email receipt, removal from the
schedule when payment is not received, and the contact details
((714) 933-8654, (714) 933-5771, service@scoopydoog.com). The detailed
process/FAQ text (gate photo, disinfectant, 60-minute text, aggressive
dogs, $60 start / $100–$135 typical) is carried over from the Nov 8
morning read — the crawler returns homepage content for the sub-pages,
so it could not be re-confirmed line by line in this pass. Two flags for
the next browser pass (do not change the figures on a hunch):
- The "$100–$135 typical" range: sibling sites on the same template
  (Swoop Scoop, Poopy Pup LA) publish "$95–$135". Scoopy's own figure
  needs one direct look at its FAQ before the next pricing change.
- One-time visits are in the FAQ/service data but not listed on the
  homepage's frequency line — confirm they are still offered.
Also confirmed in this pass: dooscooping.com still does not resolve
(fetch failed again Nov 8) — the redirect stays [VERIFY].

Housekeeping from the build: the "Source:" provenance lines were removed
from the five content files (provenance lives here, in this README) and
every sentence was put on a single line — the demo stand-in quotes
sentences, and it splits on line breaks, so a sentence wrapped over two
lines quotes broken. No fact changed in that rewrite. This README itself
is NOT ingested (rag.SKIP_DOCS): it is builder notes, not customer
knowledge.

## Standing rule

Never call Scoopy Doog a "residential sanitation service". That label is
rejected. It is a dog waste removal service.
