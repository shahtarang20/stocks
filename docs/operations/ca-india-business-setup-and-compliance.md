# ComputeCoin: what the CA must do to set up and keep the business safe in India

**For:** the Chartered Accountant (CA) who will register the company, keep its books and audit it.
**From:** the project owner. **Written:** 2026-11-14. **Status:** DRAFT for the CA to review and correct.

## How to read this document

- It is written in simple words on purpose. Each step says **what to do**, **why**, and **how you know it is done**.
- Items marked **[VERIFY]** are rules I wrote from general knowledge, not from the current official text. Law and
  tax rates change, and the Income-tax Act, 2025 replaced the 1961 Act from 1 April 2026, so **section numbers below
  are from the old Act and have new numbers now**. Please check every [VERIFY] item against the latest law before relying on it.
- Items marked **[LAWYER]** are not CA work. They need a lawyer who knows crypto and securities law.
- This document is not legal or tax advice. The CA decides. If the CA disagrees with anything here, the CA is right.
- The companion file [ca-verification-pack.md](ca-verification-pack.md) holds the token numbers and the signature page.

## Part 1. The business in plain words

| Question | Answer (from the project's own files) |
|---|---|
| What do we do? | An online marketplace for GPU computing. People who own GPUs ("hosts" or providers) rent them to developers and AI teams ("customers"). We schedule the jobs, measure the usage and charge for it |
| How do customers pay? | Today: **test credits only**. A Stripe payment system is built in test mode. Live payments are **not** switched on |
| How are hosts paid? | The code has withdrawals and Stripe Connect payouts. Hosts can also be paid through a smart contract (`ComputeSettlement`) in COMPUTE tokens |
| What is the token? | COMPUTE on BNB Smart Chain. 1,000,000,000 minted once. No mint function. Used to pay for compute and as a security deposit ("stake") from hosts in the privacy tier |
| Where will it trade? | PancakeSwap V2. We add the first liquidity ourselves (plan: 100,000,000 COMPUTE plus 10 BNB) |
| Does the project hold a signing key? | Yes. The platform holds a key that signs payment vouchers. This is a business-control risk, not only a technical one |
| What is done and what is not? | Contracts are tested but **not audited**. No legal review of the token. No company, bank account or tax registration yet (to be confirmed by the owner) |

## Part 2. The ten things that can hurt us, in order

If you only read one table, read this one.

| # | Risk | Why it matters | What fixes it |
|---|---|---|---|
| 1 | **No company yet.** The owner is personally running the project | The owner is personally liable for debts, tax and claims. Code, domain and tokens belong to the owner, not a business | Part 3: incorporate, then move everything into the company |
| 2 | **Tax on the token is heavy and not what normal business tax looks like** | Income from transferring a virtual digital asset (VDA) was taxed at a flat 30% with almost no deductions, no loss set-off, and 1% TDS on transfers [VERIFY]. Tokens we mint have nil cost, so selling them is taxed on the full amount | Part 6: CA models the tax **before** any sale. Do not sell or even move tokens until this is decided |
| 3 | **Anti-money-laundering rules (PMLA)** | Since 2023, businesses that exchange, transfer or safekeep VDAs, or help an issuer sell a VDA, must register with FIU-IND, name a Principal Officer and do KYC [VERIFY]. We may be in scope | Part 7: get a written view from counsel **before** launch |
| 4 | **The token may be treated as a security or an investment scheme** | Unregistered collective investment schemes and deposit-taking are offences (SEBI Act, Banning of Unregulated Deposit Schemes Act) [LAWYER] | Part 7: no promises of profit or returns, no buy-back claims, no pooling of buyers' money. The whitepaper already had false claims removed. Keep it that way |
| 5 | **Source of money is unclear** | Banks and the tax department ask where the 10 BNB (and later real money) came from | Part 5: money goes bank to company to wallet, with paper for each step |
| 6 | **Prepaid credits and withdrawals** | Customers buy credits and hosts withdraw cash. If credits can be moved between people or cashed out, RBI prepaid-instrument rules may apply [VERIFY, LAWYER] | Part 7: keep credits usable only on our platform, or get advice |
| 7 | **Foreign money in and out (FEMA)** | Foreign customers pay us, and we may pay hosts abroad. Receiving crypto for services is a grey area [VERIFY] | Part 5 and Part 7: receive foreign payments through banks only, keep the paperwork |
| 8 | **Marketplace taxes** | We connect hosts and customers. That can make us an "e-commerce operator" with GST collection and TDS duties [VERIFY] | Part 4 and Part 6: CA decides the model |
| 9 | **One person controls the wallets or the signing key** | If a key is lost or stolen, the company loses real money and cannot prove it was careful | Part 8: board-approved wallet policy, multisig with at least two signers |
| 10 | **Weak records** | If the government asks, we must show every rupee and every token. Missing records look like hiding something | Part 9: the audit file, kept from day one |

## Part 3. Set up the company (do this first)

### 3.1 Choose the type

| Option | Good | Bad | Our view |
|---|---|---|---|
| **Private Limited Company** | Limited liability. Can take investors. Looks credible to banks, exchanges and auditors | More yearly filings and audit | **Recommended.** Needed if we ever raise funds or give shares to a team |
| LLP | Cheaper, fewer filings | Harder to raise equity money | Fine only if we will never take equity investors |
| One Person Company | One owner only | Cannot add co-founders easily | Not suitable for a team and a token |
| Sole proprietor / partnership | Instant and cheap | **Owner is fully liable.** Banks and regulators dislike it for crypto | No |

If the CA prefers a different structure (for example a foreign foundation for the token and an Indian company for
the operations), write the reasons down. That structure raises tax-residency and FEMA questions, so it needs
counsel as well [LAWYER].

### 3.2 Step by step (Private Limited)

| Step | What to do | Form / place | Done when |
|---|---|---|---|
| 1 | Pick 2 directors. At least one must have lived in India 182 days or more in the year [VERIFY] | | Names and ID proof ready |
| 2 | Get a Digital Signature Certificate (DSC) for each director | Certifying agency | DSCs in hand |
| 3 | Reserve the name. Check it does not clash with another company or trademark | MCA name reservation | Name approved |
| 4 | Choose the **objects clause** carefully. Describe the real business (software, cloud and compute services, technology platform). Add digital-asset activity only as the CA and counsel advise, not as a vague "crypto" line | MOA drafting | Objects clause approved by CA and counsel |
| 5 | Incorporate with PAN, TAN, DIN, EPFO and ESIC options, bank account if offered | **SPICe+ (INC-32), with INC-33 MOA and INC-34 AOA** [VERIFY form names] | Certificate of Incorporation, PAN and TAN received |
| 6 | Registered office proof (rent agreement or ownership paper, utility bill, NOC) | Part of SPICe+ | Accepted |
| 7 | Open a **current account** in the company's name. Tell the bank honestly what the business does, including that a token is planned. Some banks refuse crypto-linked businesses, so ask first | Bank | Account active |
| 8 | Pay in share capital from the founders' own bank accounts into the company account. Keep the receipts | Bank | Capital shown in books |
| 9 | Issue share certificates. Create the registers (members, directors, charges) | Company | Registers in the minute book |
| 10 | Hold the first board meeting within 30 days. Appoint the auditor, approve bank signatories, adopt the wallet policy (Part 8) | Board | Signed minutes |
| 11 | File auditor appointment | **ADT-1**, within 30 days of appointment [VERIFY] | Filed |
| 12 | File declaration that the share capital has been paid, so the company may start business | **INC-20A**, within 180 days of incorporation [VERIFY] | Filed |
| 13 | Each director files KYC every year | **DIR-3 KYC** | Filed |

### 3.3 Optional

- **DPIIT "Startup India" recognition:** can give tax and compliance benefits, but a token business may not
  qualify, and the benefits change [VERIFY]. CA decides whether to apply.
- **Trademark** for "ComputeCoin" and the logo. Apply in the company's name, not the owner's.

## Part 4. Other registrations

| Registration | Why | Who decides | Note |
|---|---|---|---|
| **GST** | Selling compute is a service. Registration is needed above the turnover limit, for inter-state sales above certain conditions, and if we act as an e-commerce operator [VERIFY]. Exporting services to foreign customers needs a Letter of Undertaking (LUT) to avoid paying GST first | CA | Register early. It is easier to be registered than to explain late sales |
| GST on credits and tokens | Is a credit a voucher? Is a token a good, a service or neither? Rules are unclear [VERIFY] | CA | Write the decision in the tax policy note (Part 6) |
| **TAN** | To deduct and deposit TDS | CA | Comes with incorporation |
| **Professional tax**, **Shops and Establishment** | State rules, depends on the office location | CA | |
| **EPFO / ESIC** | When we have employees above the limits | CA | |
| **FIU-IND (FINnet)** | Only if counsel says we are a reporting entity under PMLA (Part 7) | Counsel | Do this **before** we handle other people's VDAs |
| **IEC (Import Export Code)** | Often needed to receive foreign payments for services through some banks [VERIFY] | CA | |
| **Payment gateway** | Stripe India may be invite-only. Razorpay or another gateway may be needed for INR | Owner and CA | Each gateway has its own business rules and may reject crypto-related merchants. Declare honestly |

## Part 5. Money: how it must flow

The rule: **fiat comes in through the company bank account, goes to the company wallet only with a paper trail,
and personal money is never mixed with company money.**

| Flow | Right way | Wrong way |
|---|---|---|
| Owner funds the company (including the 10 BNB for liquidity) | Owner puts money in the bank as **share capital** (best), or as a **documented director's loan** with a signed declaration that it is the owner's own money [VERIFY the declaration rules]. The company then buys BNB from a regulated exchange using its own bank account | Owner sends BNB from a personal wallet to the pool and calls it "company money" |
| Buying BNB | Use a registered Indian exchange or other lawful route. Keep the invoice, KYC, bank debit and transaction hash | Peer-to-peer cash deals, unknown sellers, cash |
| Customers pay in INR | Through a gateway into the company account. Issue GST invoices | Customers sending money to a personal UPI |
| Customers pay in foreign currency | Through a bank or gateway that issues a foreign inward remittance advice. Keep FIRC or e-FIRC [VERIFY] | Receiving foreign payments in a personal account |
| Customers pay in tokens | **Decide first** with the CA (Part 6). Value each receipt in INR on the day it arrives and book it as income | Letting tokens arrive with no record of who paid for what |
| Hosts are paid | From the company, with TDS and GST handled as the CA decides. Keep each host's PAN or tax ID and bank details. For hosts outside India, ask the CA about TDS on foreign payments and FEMA | Paying hosts in cash or from a personal account |
| Team tokens | Vest through the vesting contract. Record the grant letter, the vesting terms and the tax treatment for the receiver (a token gift or salary can be taxable) [VERIFY] | A handshake promise |

## Part 6. Accounting and tax work for the CA

### 6.1 Write a "Tax and accounting policy note" before real money

One short document, signed by the CA and approved by the board, answering these:

| # | Question | Why we need the answer |
|---|---|---|
| 1 | How is the COMPUTE we mint recorded? (Cost is nil. Is it inventory, an intangible asset, or off-balance-sheet until sold?) No specific Indian accounting standard covers crypto assets [VERIFY], so the **policy must be written and consistent** | The auditor needs a rule to audit against |
| 2 | How is BNB held by the company valued each year-end? (cost, or market value) | Year-end accounts |
| 3 | Is moving BNB and COMPUTE into the pool a "transfer" of VDAs? Is receiving LP tokens a taxable event? | This decides if tax is due at launch |
| 4 | When someone buys COMPUTE from the pool, do we earn anything? (No, unless we sell our own tokens. LP fees accrue to LP holders, which is us, and are income when received or earned) | Revenue recognition |
| 5 | Tax on any sale of our own COMPUTE (Section 115BBH, 30% flat, only cost of acquisition allowed, no loss set-off) [VERIFY] | We could owe tax on 100% of the sale price |
| 6 | TDS on VDA transfers (Section 194S, 1%) [VERIFY]. Who deducts, and what about on-chain trades with unknown buyers? | Compliance and penalties |
| 7 | Payments to hosts: TDS on service payments, and e-commerce operator TDS (194-O, rate changed in 2024, and may be restructured from 1 April 2026) [VERIFY] | Marketplace duties |
| 8 | GST: tax on compute services, export of services, e-commerce operator collection (Section 52 CGST TCS) [VERIFY] | GST returns |
| 9 | Revenue in tokens: when and at what rate is it booked in INR? | Books and tax |
| 10 | Staking deposits from hosts (ComputeStakeVault) and customer prepaid credits: are they **liabilities** (money we owe back), not income? | They must not be counted as profit |
| 11 | Corporate tax rate and regime (for example the lower 22% regime under Section 115BAA [VERIFY]) and whether VDA income is excluded from it | Tax planning |
| 12 | Transfer pricing, if we deal with a related foreign entity | Only if a foreign foundation or subsidiary exists |

### 6.2 What the company's financial statements must show

Schedule III of the Companies Act requires disclosure about crypto and virtual currency held or traded: profit or loss
on transactions, the amount held at the reporting date, and any deposits or advances taken from anyone for trading or
investing in them [VERIFY the current wording]. Prepare these notes from day one, even when the amount is nil.

### 6.3 Books and records

- Books on accrual basis, in an accounting package, with a **separate ledger per wallet** and per token.
- A **monthly reconciliation**: wallet balance on the blockchain explorer against the ledger. Print or save the
  explorer page with date and time.
- Keep the **transaction hash, date, INR value (with the rate source), counterparty and purpose** for every on-chain movement.
- Keep records for the periods the law requires. Companies Act books: 8 years. Income-tax: at least 6 years. GST: at
  least 72 months. When unsure, keep them longer [VERIFY].
- The platform already keeps a double-entry ledger in its database. The CA should sample it and agree it to the
  bank, the gateway and the chain once a month.

## Part 7. Rules outside tax: get these answered **before** real money

| Area | Question | Who | What we do meanwhile |
|---|---|---|---|
| **PMLA / FIU-IND** | Are we a "reporting entity" because we help sell or transfer a VDA? | [LAWYER] | Do not handle other people's VDAs or sell tokens until answered |
| **SEBI / securities** | Could COMPUTE count as a security or collective investment scheme? | [LAWYER] | No marketing about price, profit, returns, buy-back or "investment". No private token sale to individuals. Whitepaper must stay accurate |
| **BUDS Act (deposits)** | Do host stakes or customer credits look like deposits that earn returns? | [LAWYER] | Stake is a refundable security bond for good behaviour. It must **not** pay interest or rewards |
| **RBI prepaid instruments** | Can credits be sold to others or withdrawn as cash? | [LAWYER] | Keep credits usable only for our services until advised |
| **FEMA** | Receiving foreign payments, paying foreign hosts, holding foreign-exchange-linked assets, any overseas entity | CA and [LAWYER] | Use banks only. Keep paperwork |
| **Companies Act** | A private company cannot invite the public to buy its shares. Tokens must give **no** share, dividend, profit-share or voting rights in the company | [LAWYER] | Whitepaper and terms must say so |
| **Data protection (DPDP Act 2023)** | We store user accounts, logs and payment data | [LAWYER] and the developer | Privacy policy, consent, security measures, a grievance contact [VERIFY the current rules] |
| **IT Act and CERT-In directions (2022)** | Log retention, incident reporting within 6 hours, KYC and transaction records kept for 5 years for virtual-asset businesses [VERIFY] | Developer and CA | Keep logs. Name an incident contact |
| **Consumer law** | Refunds, clear pricing, grievance officer | [LAWYER] | Terms and refund policy |
| **Sanctions and export rules** | We should not serve sanctioned countries or persons | [LAWYER] | Terms block them. Add checks if live payments start |
| **Terms of Service and Privacy Policy** | Both are drafts | [LAWYER] | Lawyer finalises before live payments |

## Part 8. Make the company safe (governance)

| # | Control | Details |
|---|---|---|
| 1 | **IP assignment** | The owner wrote the code before the company existed. Sign a deed assigning all code, designs, domain names, social accounts and trademarks to the company. Every later contractor and employee signs an IP assignment too. Without this the company may not own its own product |
| 2 | **Founders' agreement** | Roles, shareholding, vesting of founder shares, what happens if someone leaves |
| 3 | **Board resolutions** | One for each major act: open bank account, approve wallets and signers, approve liquidity funding and amount, approve the token launch, approve the tokenomics file, appoint the auditor. Keep signed copies |
| 4 | **Wallet and key policy** | The token and its admin powers sit in a **multisig (Safe) with at least 2 signers on separate devices**, as the launch checklist requires. Write down who the signers are, how keys are backed up, what happens if a signer leaves, and approval limits. Never one person alone |
| 5 | **Platform signing key** | The platform holds a hot key that signs payment vouchers. For real money keep it in a managed key service or hardware module. The contract caps the platform fee at 30%, which limits but does not remove the loss |
| 6 | **Separate wallets** | Deployer (gas only), liquidity wallet, treasury Safe, team wallet. Do not reuse the throwaway testnet wallets on mainnet |
| 7 | **Insurance** | Ask about cyber and directors' (D&O) insurance. Many do not cover crypto losses, so read the exclusions |
| 8 | **Related-party rule** | Any dealing between the company and a director (loans, token purchases, payments) is recorded and approved by the board |
| 9 | **Written policies** | Anti-money-laundering, sanctions, complaints, data protection and incident response, short and signed |
| 10 | **Honest public statements** | Everything on the website, whitepaper and social media must be true and match the code. The repo already removed false claims about audits, staking and buy-backs. Keep a person responsible for checking new statements |

## Part 9. The audit file (what the CA needs ready for any audit or government question)

Create one folder, shared and backed up. Fill it as events happen, not at year end.

| # | Document | Status |
|---|---|---|
| 1 | Certificate of Incorporation, MOA, AOA, PAN, TAN, GST certificate | [ ] |
| 2 | Registers (members, directors), share certificates, minute book | [ ] |
| 3 | Board and shareholder resolutions, in date order | [ ] |
| 4 | Bank statements, gateway statements, foreign-payment advices | [ ] |
| 5 | Share capital and loan paperwork showing the owner's own funds | [ ] |
| 6 | Exchange invoices and KYC for every BNB purchase, with transaction hashes | [ ] |
| 7 | Wallet register: every address, its role, signers, creation date | [ ] |
| 8 | `launch.56.json`, `pool.56.json` and the approved `tokenomics.json` (the repo's launch records) | [ ] |
| 9 | BscScan links showing verified contract source and the LP lock | [ ] |
| 10 | Monthly wallet-to-ledger reconciliation, signed by the CA | [ ] |
| 11 | Tax and accounting policy note (Part 6.1) | [ ] |
| 12 | Written opinions from counsel on PMLA, SEBI, RBI and FEMA (Part 7) | [ ] |
| 13 | Independent smart-contract audit report, every finding fixed and re-checked | [ ] |
| 14 | IP assignment deeds, founder and team agreements, vesting letters | [ ] |
| 15 | Customer and host terms accepted (with version and date), privacy policy | [ ] |
| 16 | GST returns, TDS returns and challans, income-tax returns, tax-audit report if required | [ ] |
| 17 | ROC filings (AOC-4, MGT-7, ADT-1, INC-20A, DIR-3 KYC) with acknowledgements | [ ] |
| 18 | The signed [ca-verification-pack.md](ca-verification-pack.md) | [ ] |

## Part 10. Yearly and monthly compliance calendar (for the CA to confirm and keep)

| When | What |
|---|---|
| Monthly | GST returns. TDS deposit by the 7th of next month. Wallet reconciliation. Payroll and PF/ESIC if applicable |
| Quarterly | TDS returns. Advance tax instalments. A board meeting (at least 4 a year, with no more than 120 days between two) [VERIFY] |
| Yearly | Hold the AGM. Audited financial statements. ROC forms AOC-4 and MGT-7. Income-tax return for a company. Tax audit if turnover crosses the limit [VERIFY]. GST annual return. DIR-3 KYC. Update the registers |
| On each event | Allotment of shares, change of director, change of address, new charge, new bank account |

## Part 11. Order of work (a simple plan)

1. **Now:** incorporate the company. Open the bank account. Sign the IP assignment and founders' agreement.
2. **Next:** register GST and TAN. Choose a payment gateway. Start the books and the audit file.
3. **Before any real token sale or mainnet launch:** counsel's written views (Part 7), the tax and accounting policy
   note (Part 6.1), the board-approved wallet policy (Part 8), the independent smart-contract audit, the filled
   [ca-verification-pack.md](ca-verification-pack.md), and the technical checklist in
   [launch-checklist.md](launch-checklist.md) (Track B).
4. **Testnet is allowed now.** Test coins have no value, so a testnet launch carries no tax or money risk. Use it
   to practise the launch and the record keeping.
5. **After launch:** monthly reconciliation, yearly filings, and a CA review any time the business model changes.

## Part 12. Plain questions the CA should answer in writing

1. Which entity type do you advise, and why?
2. Will you act as statutory auditor, or should we appoint a separate auditor?
3. How should we book the minted tokens, the BNB and the LP position?
4. What tax is due when we add liquidity? When we sell tokens? When customers pay in tokens?
5. Do we need GST registration now, and how should we treat credits and tokens?
6. Are we an e-commerce operator, and what TDS or TCS follows?
7. Do you advise a foreign structure? If yes, what are the tax and FEMA consequences?
8. What are the filing deadlines and penalties for each registration above?
9. Which items here are wrong or out of date? (Please mark them.)

## What this document cannot do

- It cannot tell you what the law is today. The rules marked [VERIFY] come from general knowledge and the Income-tax
  Act, 2025 has renumbered the income-tax sections.
- It does not replace a lawyer for PMLA, securities, RBI or FEMA questions.
- It does not replace a smart-contract audit. A CA cannot say the code is safe.
- Following it does not guarantee the government will not raise a question. It makes sure that if someone asks, the
  company has the answers, the paper and the money trail ready.

## Sign-off tracker

| Part | Reviewed by CA | Date | Notes |
|---|---|---|---|
| 3 Company set-up | | | |
| 4 Registrations | | | |
| 5 Money flow | | | |
| 6 Accounting and tax policy | | | |
| 7 Other rules (with counsel) | | | |
| 8 Governance | | | |
| 9 Audit file | | | |
| 10 Calendar | | | |
