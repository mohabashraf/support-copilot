# KB chunk plan and inquiry map

The four pages split into **26 chunks** (one per section or FAQ, plus the shared access notice and the device promo block). This is the target output of `kb.ingest` and the ID scheme every test uses. Source text is never edited; flags describe how the co-pilot handles each chunk.

| Chunk ID | Page(s) | Section | Flags | What it says | Customer questions it should answer |
|---|---|---|---|---|---|
| `deposit#intro` | deposit | How to Deposit (intro) | `conflict` | Range of instant, simple and secure funding methods. | Are deposits instant? |
| `deposit#steps` | deposit | Fund Account steps 1-4 | – | Log in to Member Area → Deposit → choose payment channel → enter account details and amount → start trading. | How do I deposit? · Where is the deposit button? |
| `faq#min-deposit` | deposit, withdraw | FAQ: minimum deposit amount | – | Minimum deposit starts from 8 USD, depending on account type. | What is the minimum deposit? · Can I start with 5 dollars? |
| `faq#how-to-deposit` | deposit, withdraw | FAQ: how can I deposit funds | – | Log in to member area, select payment option and deposit size. | How can I fund my account? |
| `faq#deposit-currencies` | deposit, withdraw | FAQ: deposit currencies | – | Any currency into the USD account at that day's exchange rate. | Can I deposit in EUR? · Which currencies do you accept? |
| `faq#deposit-credit-time` | deposit, withdraw | FAQ: how long until deposit is credited | `conflict` | Deposits are instant; balance updated within 60 minutes. | My deposit isn't showing · How long does a deposit take? |
| `faq#funds-available` | deposit, withdraw | FAQ: when can I trade the funds | – | Trade immediately once the balance is updated. | When can I trade after depositing? |
| `faq#history` | deposit, withdraw | FAQ: deposit and withdrawal history | – | View all deposits, withdrawals and transfers in the member area. | Where can I see my transactions? |
| `faq#withdrawal-pending` | deposit, withdraw | FAQ: withdrawal status pending | – | Request is in the queue being processed; you will be notified once approved. | What does pending mean? |
| `faq#withdrawal-rejected` | deposit, withdraw | FAQ: why withdrawal rejected | `rule_sensitive` | Possible reasons: account not verified, data entry error, insufficient margin; email explains the reason. | Why was my withdrawal rejected? |
| `faq#withdrawal-not-received` | deposit, withdraw | FAQ: funds not received after withdrawal | `route` | If not received within 24 hours, contact support. | I didn't get my withdrawal |
| `withdraw#intro` | withdraw | How to Withdraw (intro) | `conflict` | Withdrawals 'easy and instant'. | Are withdrawals instant? |
| `withdraw#steps` | withdraw | Withdraw steps 1-4 | `defective` | Copy of deposit steps: log in, select channel, 'enter amount to deposit', 'Start Trading!'. | What are the steps to withdraw? |
| `open#intro` | open-account | How to Open an Account (intro) | `conflict` | Open your account in '3 quick and simple steps' (but 4 steps are listed). | How many steps to open an account? |
| `open#steps` | open-account | Open account steps 1-4 | – | Click Create Account on valetax.com → enter details → access Member Area → make first deposit. | How do I sign up? |
| `open#faq-sign-up` | open-account | FAQ: how do I sign up | `conflict` `gap_link` | Follow step-by-step to verify and fund; live account lets you trade immediately; read Risk Disclosures, Privacy Policy, Terms. | How do I register? |
| `open#faq-account-type` | open-account | FAQ: which account type | `gap_link` | Depends on strategy and instruments; account-types page linked (not in KB); can open another type later. | Which account should I choose? |
| `open#faq-min-initial-deposit` | open-account | FAQ: minimum initial deposit to open | `gap_link` | Depends on account type; see account-types page (not in KB). | Minimum to open an account? |
| `open#faq-documents` | open-account | FAQ: documents to open an account | – | Live accounts: proof of ID + proof of address (bank statement or utility bill) not older than 6 months; name clearly displayed. | What documents do I need? |
| `open#faq-opening-time` | open-account | FAQ: how long to open an account | – | Account opening and document verification completed within 48 hours. | How long does verification take? |
| `open#faq-leverage` | open-account | FAQ: which leverage settings | `rule_sensitive` `gap_link` | Depends on strategy and goals; up to 1:2000 depending on account type. | What is the maximum leverage? |
| `open#faq-islamic` | open-account | FAQ: Islamic-friendly account | – | All accounts are Islamic friendly; no swap credited or withdrawn. | Do you have swap-free accounts? |
| `verify#intro` | verify | How to Verify Account (intro) | – | Identity verification protects against identity theft and fraud. | Why do I need to verify? |
| `verify#steps` | verify | Verify steps 1-3 | – | Complete fields and upload documents (KYC profile) → account under review → verification completed. | How do I verify my account? |
| `notice#access` | deposit, withdraw, open-account, verify | Important Access Notice (all pages) | `rule_sensitive` `compliance` | Site only for residents of jurisdictions where permitted; Valetax not established in your jurisdiction; reverse solicitation. | Can I open an account from my country? |
| `promo#devices` | deposit, withdraw | Trade from every device (promo block) | `promo` | Access your trading account on the go: monitor portfolio, place trades, track progress. No app names or download details. | Do you have a mobile app? · Can I trade on my phone? |

**Flags:** `conflict` (disagrees with another chunk) · `defective` (wrong/copy-pasted content, never answer from it) · `rule_sensitive` (touches a hard rule) · `compliance` · `gap_link` (points to a page not in the KB) · `route` (KB tells the customer to contact support, so a person must act) · `promo` (marketing text, low answer value).

## ID scheme
- `<page>#<slug>` for page-specific chunks; `faq#<slug>` for the 9-question FAQ shared by the deposit and withdraw pages (stored once, **both page URLs** as sources); `notice#access` for the access notice present on every page; `promo#devices` for the "Trade from every device" block (deposit and withdraw pages).
- IDs are stable across runs; the KB hash changes only if the source pages change.

## Inquiries the KB cannot answer (expected to abstain)
Fees (deposit, withdrawal, conversion) · payment methods list · withdrawal minimum/maximum/limits · demo accounts · mobile app details (only implied by `promo#devices`) · withdrawal processing time · account-types table · spreads and trading conditions · platform login / password · bonuses and promotions · partner / IB programme · account closure · religious rulings · market hours · crypto withdrawal specifics.

These are the no-answer queries (`answerable: false`) in `eval/retrieval_queries.jsonl` and the out-of-scope tickets in both eval sets.
