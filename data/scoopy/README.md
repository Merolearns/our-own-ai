# Scoopy Doog agent data

This directory holds the knowledge base for the Scoopy Doog agent (agent 2):
services, pricing, booking, and FAQ. Facts come from scoopydoog.com
(homepage, service page, FAQ), read November 8, 2026.

## What the agent may say

Only what is in these files. Anything marked [VERIFY] is not confirmed and
must never be stated as fact until it is verified.

## Login (temporary shape)

Real Scoopy Doog customers will sign in with Google OAuth (Authlib). Creating
the Google OAuth client is a parent-side setup for later (about 10 minutes in
Google Cloud Console); the client secret goes in the VPS `.env`, never the repo.
Until that exists, logins fall back to PBKDF2 password accounts, the same
pattern as the Parcelco demo accounts.

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

## Standing rule

Never call Scoopy Doog a "residential sanitation service". That label is
rejected. It is a dog waste removal service.
