# Affiliate Engine — Sports Betting Affiliate Marketing (Prototype)

A fully automated sports betting affiliate marketing engine: S2S postback
tracking, tiered CPA / NGR commission calculation, anti-fraud, and weekly
Bitcoin payouts — with an audit-ready SQLite database throughout.

> **Prototype, not production.** No real money moves here. The payout
> gateway is a **mock** that simulates submission and returns deterministic
> fake TXIDs. A real gateway integration is a clearly marked adapter stub
> (see [Connecting a real payout gateway](#connecting-a-real-payout-gateway)).
> No real API keys are used, stored, or requested anywhere in this project.

Related research (which affiliate programs to join first, and Ontario
compliance): [`research/affiliate-programs.md`](research/affiliate-programs.md).

---

## Stack choice

**Python 3.12 + FastAPI + SQLite (stdlib) + pytest.**

- Python's stdlib `sqlite3` means the audit database needs no extra service
  or native binding (a Node build would need a native SQLite module).
- FastAPI gives typed request handling and a built-in `TestClient`, so the
  postback security tests run against the real app with no server process.
- The commission math, BTC validation (Base58Check + Bech32/Bech32m) and
  fraud checks are pure Python with zero extra dependencies — easy to audit.

## Project layout

```
affiliate-engine/
├── affiliate_engine/
│   ├── config.py       # tiers, limits, secrets (env), fraud windows
│   ├── db.py           # SQLite schema + connections
│   ├── security.py     # HMAC verification + idempotency keys
│   ├── btc.py          # BTC address validation (P2PKH/P2SH/SegWit/Taproot)
│   ├── fraud.py        # duplicate IP, self-referral, bonus churn
│   ├── commissions.py  # tiered CPA + NGR revenue-share engine
│   ├── payouts.py      # gateway adapter (mock + real stub), weekly aggregation
│   └── server.py       # FastAPI: postbacks, dashboard, admin endpoints
├── scripts/
│   ├── seed_demo.py        # demo data (incl. fraud cases + invalid BTC address)
│   ├── run_commissions.py  # monthly commission run (dry-run or --approve)
│   └── weekly_payout.py    # weekly BTC payout job (mock gateway)
├── tests/              # pytest: commissions, BTC, idempotency, fraud, postbacks, payouts
├── research/           # sibling research: which affiliate programs to join
└── README.md
```

## Quickstart

```bash
cd ~/workspace/affiliate-engine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1. Seed demo data (affiliates, clicks, FTDs, wagers, fraud cases)
.venv/bin/python -m scripts.seed_demo

# 2. Start the postback server
./run_server.sh            # http://127.0.0.1:8000

# 3. Run the weekly payout job (mock gateway, fake TXIDs)
.venv/bin/python -m scripts.weekly_payout --confirm

# 4. Run the tests
./run_tests.sh
```

Configuration lives in environment variables (see `.env.example`):
`POSTBACK_SECRET`, `ADMIN_TOKEN`, `AFFILIATE_DB`.

## 1. S2S postback integration

Operators send events to:

| Endpoint | Event |
|---|---|
| `POST /postback/click` | tracking click |
| `POST /postback/registration` | new player registration |
| `POST /postback/ftd` | first-time deposit |
| `POST /postback/wager` | wagering activity (feeds NGR + churn checks) |

**Security on every postback:**

- **HMAC-SHA256 signature** over the raw request body, in the
  `X-Postback-Signature` header (`hex`, or `sha256=<hex>`). Constant-time
  comparison. A bad signature returns `401` and the payload goes to the
  `dead_letter` table — it is never processed.
- **Idempotency key** in the `X-Idempotency-Key` header (or the JSON body).
  A replayed key returns `200 {"status": "duplicate"}` and is never
  processed twice. Keys are stored in the `idempotency` table.
- **Malformed JSON** returns `400` and is also dead-lettered.

**Sending a signed test postback:**

```bash
python3 - <<'EOF'
import json, hmac, hashlib, urllib.request
secret = "dev-secret-change-me"   # must match POSTBACK_SECRET
body = json.dumps({
    "click_id": "demo-1", "affiliate_code": "AFF-001", "operator": "bet105",
    "ip": "203.0.113.42", "landing_url": "https://example.com/bet105",
    "idempotency_key": "demo-1",
}).encode()
sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
req = urllib.request.Request(
    "http://127.0.0.1:8000/postback/click", data=body,
    headers={"Content-Type": "application/json", "X-Postback-Signature": sig},
    method="POST")
print(urllib.request.urlopen(req).read().decode())
EOF
```

## 2. Commission calculation engine

Both schemes use **marginal tiers**: each unit of volume is paid at the rate
of the band it falls in. Higher volume never re-prices earlier volume.

**CPA tiers** (per FTD, by monthly FTD count):

| FTDs in month | $ per FTD |
|---|---|
| 1–9 | $100 |
| 10–24 | $125 |
| 25–49 | $150 |
| 50+ | $200 |

**NGR revenue-share tiers** (by monthly NGR, where NGR = wagered − won − bonus):

| Monthly NGR | Share |
|---|---|
| $0–$10,000 | 25% |
| $10,000–$50,000 | 30% |
| $50,000–$100,000 | 35% |
| $100,000+ | 40% |

**Worked example** (the seed data reproduces this exactly):

> Affiliate AFF-001, November 2026: **12 FTDs** and **$60,000 NGR**.
>
> - CPA: 9 × $100 + 3 × $125 = **$1,275**
> - NGR share: $10,000 × 25% + $40,000 × 30% + $10,000 × 35% = **$18,000**
> - **Total: $19,275**

Fraud-flagged conversions are excluded *before* the tier math: a flagged
FTD does not count toward the FTD total, and a flagged player's wagering
does not count toward NGR.

Run a month (dry run, then approved):

```bash
.venv/bin/python -m scripts.run_commissions 2026-11            # pending
.venv/bin/python -m scripts.run_commissions 2026-11 --approve  # payable
```

## 3. Automated BTC payout (weekly)

`scripts/weekly_payout.py` runs the weekly cycle:

1. Sum each affiliate's **approved, unpaid** earnings.
2. Balances below **$25** roll over to next week.
3. Validate the affiliate's BTC address — **P2PKH (`1…`), P2SH (`3…`),
   Native SegWit (`bc1q…`), Taproot (`bc1p…`)**. Real validation: Base58Check
   checksum + version byte, and Bech32/Bech32m checksum + witness program
   checks (BIP-173/350). Invalid address → payout request recorded as
   `invalid_address`, **no money moves**, earnings stay approved.
4. Convert USD → BTC at the configured rate (demo: fixed `95,000` constant —
   a real deployment must use a live rate and record it).
5. Submit the batch through the gateway adapter; every payout is recorded
   with its **TXID, submission timestamp, and confirmation timestamp**.

```bash
.venv/bin/python -m scripts.weekly_payout --week 2026-W46 --confirm
```

## 4. Anti-fraud checks

| Check | Trigger | Effect |
|---|---|---|
| Duplicate IP | ≥2 different players register from one IP within 24h | registration flagged, held out of commissions |
| Self-referral | player email == affiliate's own email | registration flagged, held out |
| Zero-wagering bonus churn | FTD, then $0 wagered (with bonus claimed) within 7 days | FTD flagged, held out |
| Low-wagering churn | FTD, then <10% of deposit wagered within 7 days | FTD flagged, held out |

Flags live in `fraud_flags` and are cleared only by human review
(`reviewed = 1`). Flagged conversions never enter commission math.

## 5. Audit-ready database (SQLite)

`data/affiliate.db` — every money movement is traceable from source events:

`affiliates`, `clicks`, `registrations`, `ftds`, `wagers`, `earnings`,
`payout_requests`, `payouts` (TXID + `submitted_at`/`confirmed_at`),
`fraud_flags`, `dead_letter`, `idempotency`.

Dashboard (read-only):

- `GET /dashboard/affiliates` — balances per affiliate
- `GET /dashboard/affiliate/{code}` — earnings, payouts, fraud flags
- `GET /dashboard/payouts` — all payouts with TXIDs

Admin endpoints (require `X-Admin-Token`):

- `POST /admin/commissions/{period}?approve=true`
- `POST /admin/payouts/weekly?week=2026-W46`

## Tests

```bash
./run_tests.sh
```

| File | Covers |
|---|---|
| `tests/test_commissions.py` | CPA/NGR tier math, worked example, fraud exclusion, idempotent runs |
| `tests/test_btc.py` | valid P2PKH/P2SH/SegWit/Taproot + invalid (bad checksum, testnet, bad length) |
| `tests/test_postback.py` | HMAC accept/reject, dead-letter, idempotency, fraud via postback |
| `tests/test_fraud.py` | duplicate IP, self-referral, zero/low-wagering churn |
| `tests/test_payouts.py` | aggregation, TXID recording, invalid address, below-minimum, confirmation |

## Connecting a real payout gateway

1. Subclass `affiliate_engine.payouts.PayoutGateway` and implement
   `submit_batch()` against the provider's API (e.g. a crypto payout
   gateway's REST endpoint). `RealPayoutGateway` in `payouts.py` is the
   explicit stub — it raises until wired in.
2. Pass credentials **only** via environment variables or a secrets manager.
   Never hardcode keys, never log them.
3. Replace the demo BTC/USD rate with a live rate source and record the
   rate used on each `payout_requests` row.
4. Add a confirmation poller: query the provider (or a block explorer) for
   each TXID and fill `confirmed_at` — `confirm_payouts()` shows the shape.
5. Before going live: per-operator HMAC secrets (today one shared secret),
   dashboard auth, rate limiting, and a real scheduler (cron/systemd) for
   the weekly job instead of the manual CLI.

## Known limitations (prototype)

- One shared postback secret for all operators (production: per-operator).
- Dashboard endpoints have no auth in this prototype.
- Fixed demo BTC/USD rate; no live price feed.
- Negative monthly NGR is clamped to 0 (no negative carryover — a business
  decision to confirm with each operator).
- The weekly job is a CLI; schedule it with cron in production.
