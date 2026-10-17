# ComputeCoin — Your Launch Story (Small-Budget, Global Path)

_A simple, honest guide: how to put ComputeCoin in front of the whole world on a
budget of ₹5,000–₹10,000 — what that money CAN do, what it genuinely cannot, and
the smartest order to do it in._

---

## The story so far

You built something real. Not a landing page, not a fake dashboard — a working
GPU compute marketplace: people can sign up, list a GPU, rent a GPU, run a job,
and the money is tracked in a double-entry ledger that always balances. It's
tested. That's rare. Most "projects" at this stage are screenshots. Yours runs.

Now you want the world to see it, and you have about **₹5,000–₹10,000** to make it
happen. Good news: **that is enough to put the product online, globally, for real
people to use.** Let's be equally honest about the one thing it is *not* enough
for — the coin — and why rushing that would hurt you, not help you.

---

## The big truth, up front (read this twice)

There are **two different launches** hiding inside your head, and people confuse
them:

1. **Launch the PRODUCT** — the GPU marketplace, as a real website the world can
   use. 👉 **This fits your budget. Do it now.**
2. **Launch the COIN** — a tradable token on exchanges. 👉 **This does NOT fit
   ₹5,000–₹10,000. Not even close.** A single smart-contract audit costs lakhs
   (₹8,00,000+), and a lawyer to tell you the token is legal in your country costs
   more. Skipping those is how founders get rugged or arrested — not a shortcut,
   a trap.

So the winning move is simple: **launch the product now for almost nothing, keep
the coin on a free test network, and only touch the real coin later when you have
real users and real money for an audit + a lawyer.** Users first. Coin later.

---

## Your ₹5,000–₹10,000 plan (what the money buys)

Almost everything you need has a **free tier**. The only real cost is a domain and
a few hours of GPU time for a demo.

| What | Tool (free tier) | Cost (₹) |
|---|---|---|
| Website hosting | **Vercel** (free) | 0 |
| Backend + workers | **Railway** (free/trial) or **Render** free | 0 |
| Database + file storage | **Supabase** (free) | 0 |
| Redis (cache/leases) | **Upstash** (free) | 0 |
| Email (verify/reset) | **Resend** or **Brevo** (free ~3k/mo) | 0 |
| Error monitoring | **Sentry** (free) | 0 |
| **Domain name** (.com/.xyz) | Namecheap/Cloudflare | **200–900** |
| **Cloud GPU** for a real demo (rent by the hour) | **RunPod / Vast.ai** (~₹30–80/hr) | **300–1,500** |
| Payments (test mode) | **Razorpay** (India) or **Stripe** | 0 |
| **Buffer** (small overruns) | — | rest |
| **Total** | | **~₹1,000–₹2,500 used; well under ₹10,000** |

You will likely spend **less than ₹2,500** and keep the rest as buffer. The
expensive things (audit, legal, mainnet, exchange) are simply **not on this list**
— on purpose.

---

## The step-by-step path (in order)

Think of it like opening a shop: first the shop works and customers walk in; the
fancy signboard and the franchise come later.

### Step 1 — Put the product online (Week 1) 🌍
- Deploy the website to **Vercel**, the backend to **Railway**, database to
  **Supabase**, Redis to **Upstash**. (These are the hosts already chosen in the
  launch plan.)
- Buy **one domain** (e.g. `computecoin.xyz` — ₹200ish). Point Vercel at it.
- Turn on **real email** (Resend free) so sign-up/verify works.
- Turn on **Sentry** (free) so you see errors.
- Result: **anyone in the world can open your site and sign up.** This is your
  global launch — for the product.

### Step 2 — Make it honest and safe (same week)
- Run it in **"beta" mode**: jobs run as **simulated** execution and money is
  **test credits** — and the site already says so clearly. That's not a weakness;
  it's honesty, and it's exactly how serious products start.
- Make sure production secrets are set (the app refuses to boot with dev secrets —
  good, keep it that way).

### Step 3 — Prove ONE real GPU job (a few hours, ~₹500) 🔥
- Rent **one cloud GPU by the hour** (RunPod/Vast.ai), run the provider agent on
  it, and execute **one real job end-to-end**. Record a short screen video.
- Why: this is your proof that it's not a toy. One real job + one video is worth
  more than any whitepaper.

### Step 4 — Get your first 10–50 users (Weeks 2–4) 👥
- Share it: dev communities (Reddit r/MachineLearning, Discords), X/Twitter,
  LinkedIn, your college/network, Indie Hackers, Product Hunt (free).
- Tell the true story: "I built a working decentralized GPU marketplace — here's a
  real job running on it." People reward *real* + *honest*.
- Let people use test credits. Collect feedback. Fix what hurts.

### Step 5 — Turn on real money when you have demand (only then) 💳
- Wire **Razorpay** (easiest in India) or **Stripe** for real credit purchases.
  Test mode is free; going live needs a **registered business / KYC** but little
  upfront cash.
- Now real customers can actually pay to run real jobs. This is a real business.

### Step 6 — The coin (LATER, separate budget) 🪙
- Keep the token on a **free test network (Sepolia)** for now — it costs nothing
  and shows the tech works.
- Touch the **real coin only when** you have (a) real users + revenue, and (b)
  money + reason for an **independent audit** and a **short legal consult** for
  your country. Until then, a public coin is a liability with your name on it, not
  an asset.
- When that day comes, the deploy is already scripted — it becomes one careful
  step, not a gamble.

---

## What "global" really means here
You don't need permission to go global. The moment Step 1 is live on a domain,
**someone in Berlin, São Paulo, or Lagos can sign up.** The internet is already
global; free hosting is already global. You don't buy "global" — you just ship and
share. Your ₹ goes to the domain and a GPU demo, not to "going worldwide."

---

## The honest "cannot" list (so no one fools you)
On ₹5,000–₹10,000 you **cannot**:
- Pay for a **smart-contract audit** (₹8L+). 
- Pay a **crypto/securities lawyer** to clear the token for your country.
- Deploy the token to **mainnet** or get it **listed on an exchange** safely.
- Run a **large real GPU fleet** 24/7 (hourly GPU for demos only).

Anyone who says you can do all that for ₹10k is selling you a meme-coin fantasy —
and most of those end in a rugpull or a legal notice.

---

## One-line summary
> **Launch the product to the world now for under ₹2,500 (domain + a few GPU
> hours), get real users on honest beta + real payments, and keep the coin on a
> free testnet until real revenue pays for an audit + lawyer.** Users first. Coin
> later. That's the path that actually works — and it fits your budget with money
> to spare.

---

# How to actually launch a coin — the whole story, step by step

A lot of people think you "buy" a coin from somewhere or ask some company to make
it. No. **A coin is just a small program (a smart contract) that YOU deploy onto a
blockchain.** The moment you deploy it, it creates ("mints") the whole supply into
your wallet. That's the moment your coin is *born*. Nobody gives it to you — **you
create it.**

And here's the good news: **your coin already exists as code.** It's the file
`contracts/src/ComputeCoinToken.sol` (the COMPUTE token). It's written and tested.
It just hasn't been "switched on" (deployed) to a public network yet. Launching =
deploying that file.

Think of it like a shop's loyalty points:
- Writing `ComputeCoinToken.sol` = printing the rulebook for the points. ✅ done
- **Deploying it** = actually opening the machine that prints the points. ← the launch
- A **liquidity pool** = putting your points next to real cash so people can swap one
  for the other. That's what makes it "tradable."

## What you need first (one-time setup, free)
1. **A crypto wallet** — install **MetaMask** (a browser extension). This is your
   "account" on the blockchain. It gives you an **address** (like an account number)
   and a **private key** (like the password — NEVER share it, never put it in code).
2. **A blockchain to deploy on.** For practice use a **testnet** (fake-money copy of
   a real chain). Good ones: **BSC Testnet**, **Base Sepolia**, or **Sepolia**.
3. **Some test coins for gas.** Deploying costs a tiny "gas" fee. On a testnet you
   get it **free** from a **faucet** (a website that gives you test coins). On
   mainnet later, gas is real money (cents on Base/BSC).

## PART 1 — Launch the coin on TESTNET (do this now, free, safe)
This gives you a **real, live, showable coin** with a public link — using fake
money, so zero risk. It's exactly how real projects demo before going live.

1. **Make the wallet** (MetaMask) and copy your address.
2. **Switch MetaMask to a testnet** (e.g. BSC Testnet or Base Sepolia).
3. **Get free test coins** from that network's **faucet** website.
4. **Deploy `ComputeCoinToken.sol`.** (This is the "launch." I can give you a
   one-command deploy script — you paste your private key into an `.env` file that
   is never committed, run the script, done.) The whole supply appears in your
   wallet.
5. **See it live** on the chain's **block explorer** (like BscScan/Basescan) — paste
   your contract address and there's your coin, public, with a page anyone can open.
6. **Make it "tradable"** — create a **liquidity pool** on a test DEX
   (PancakeSwap/Uniswap testnet): you pair some of your COMPUTE with some test
   BNB/ETH. Now people can "buy/sell" it — with test money.
7. **Share the explorer + pool link.** You now have a coin people can look at and
   play with. **This costs ₹0 and risks nothing.** ✅

## PART 2 — Launch the coin for REAL (later, only when ready)
Same steps, but on a **real chain with real money** — so first you need the grown-up
stuff:

1. **Have real product traction** (users, a paying client) — this is what gives the
   coin any value.
2. **Get a security audit** of the contract (an outside firm) — ₹8L+.
3. **Short legal consult** — is the token OK to sell in your country? (securities /
   KYC rules).
4. **Fund a real wallet** with real crypto for gas + **real liquidity**
   (realistically **₹25,000–₹1,00,000+** — a tiny pool just gets drained by bots).
5. **Deploy to mainnet** (Base/BSC) behind a **multisig** (so one leaked key can't
   steal everything).
6. **Create the real liquidity pool** on Uniswap/PancakeSwap → genuinely tradable.
7. **(Optional, much later) Pursue a big exchange (Binance)** — they do their own
   checks, want volume + legal + often fees, and *they* decide.

## The one rule that saves you
> **Do PART 1 (testnet) now. Do PART 2 (mainnet) only after real revenue pays for
> audit + legal + real liquidity.** A real coin launch is permanent — you can't undo
> it — so practice safely on testnet first, and go live only when it's worth doing
> right.

**Where does the coin come from?** From you deploying `ComputeCoinToken.sol`. That's
it. Everything else (wallet, faucet, pool) is just the setup around that one action.

---

_See [LAUNCH_PLAN.md](LAUNCH_PLAN.md) for the exact technical steps (Workstreams
D + E) and [AUDIT_REPORT.md](AUDIT_REPORT.md) for the security status._
