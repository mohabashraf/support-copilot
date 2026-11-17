# Knowledge-base findings (4 help pages, read 2026-11-17)
Raw HTML in kb/raw/, extracted text in kb/*.txt.

## What the KB actually covers
- Deposit: 4 steps; FAQ: min deposit from 8 USD (depends on account type); any currency into USD account at that day's rate; "instant", balance visible within 60 min; trade once balance updated; history in Member Area.
- Withdraw: FAQ only: 'pending' = in queue, notified when approved; rejection reasons = unverified account, data error, insufficient margin (email explains); not received within 24 h → contact support.
- Open account: 4 steps; documents = proof of ID + proof of address (bank statement/utility bill, < 6 months, name visible); opening + verification within 48 h; account types/min deposit "see here" (page NOT in KB); leverage up to 1:2000 by account type; all accounts Islamic (no swap).
- Verify: 3 steps only (fill KYC profile + upload → under review → done). No timeline, no document list on this page.

## Gaps (must abstain / say unknown)
- Fees: none mentioned anywhere (deposit, withdrawal, conversion).
- Payment methods: not listed.
- Withdrawal minimum, limits, processing time: not stated (only the 24 h "contact support" line).
- Account types table: linked but not in KB.
- Why verification is rejected / how to fix: not covered.

## Overlaps
- Deposit and withdraw pages share the SAME 9-question FAQ → dedupe at ingestion or citations point to two pages.
- Documents + 48 h timeline live on the open-account page, not the verify page.

## Inconsistencies (good talking points)
- Withdraw page steps are copy-pasted from deposit: "Enter ... amount to deposit", "Start Trading!" → do not use those steps to answer withdrawal questions.
- "Deposits are instant!" vs "within 60 minutes".
- Withdrawals "easy and instant" vs 'pending' queue and the 24 h line.
- Min deposit: "from 8 USD" (deposit FAQ) vs "depends on account type, see here" (open-account FAQ).
- Typos: "insifficient", "in in queue".
- Open-account page: "Open your account in **3** quick and simple steps" but **4** steps are listed.
- **Trading before verification:** sign-up FAQ says "Opening a live account allows you to begin trading **immediately**", but the same page says "account opening and document verification will be completed within **48 hours**", and withdrawals can be rejected because the "account has not been verified". → "Can I trade before verification?" gets the conservative answer + `kb_conflict` flag.
- Withdrawal timing: the only time figure for withdrawals is the **24 hours** "not received → contact support" line; it is not a processing-time promise.

## Content outside the main body
- **"Trade from every device"** promo block (deposit + withdraw pages): kept as chunk `promo#devices`; it implies device access but names no app, so "Do you have a mobile app?" abstains.
- **Important Access Notice** (on every page): site only for residents where access is permitted; Valetax not established in the visitor's jurisdiction; reverse solicitation. Kept as chunk `notice#access` (flags `rule_sensitive`, `compliance`): it is the KB basis for routing jurisdiction/eligibility questions to compliance, never for deciding them.

## Rule-sensitive content
- "What leverage settings should I select?" answer touches leverage → co-pilot may state the documented maximum, never suggest a setting.
- Withdrawal rejection due to unverified account = KYC → route to human.
- Access notice (reverse solicitation / jurisdiction) → jurisdiction questions route to compliance.
