# Flight price tracker — Kochi/Bangalore → New York, June 2027

Checks fares daily, keeps a price history, and emails you
when something worth acting on happens.

Runs free on GitHub Actions.

## What triggers an email

| Trigger | Condition |
|---|---|
| **TARGET** | Total for 2 drops to **$2,150** or below |
| **DROP** | Falls **5%+** since the previous check |
| **NEW LOW** | Cheapest price ever recorded |
| **SPIKE** | Jumps **12%+** — cheap buckets may be selling out |
| **Weekly** | Sunday digest even if nothing moved |

Baseline is **$2,328** (₹2.23 lakh quote, Aug 2026). Emails are rate-limited
to one per 20 hours. The email also shows where you are relative to the
**Dec 2026 – Feb 2027** target booking window.

## Remaining setup

### 1. Get a SerpApi key
Sign up at serpapi.com (100 free searches/month). Two routes × daily ≈ 60/month.

Alternative: Amadeus (developers.amadeus.com) — set repo variable
`PROVIDER` to `amadeus`.

### 2. Email sending
Gmail needs an **App Password**:
myaccount.google.com → Security → 2-Step Verification → App passwords.

For Binghamton Office 365, set variable `SMTP_HOST` to `smtp.office365.com`.

### 3. Add secrets
Repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `SERPAPI_KEY` | your SerpApi key |
| `SMTP_USER` | sending address |
| `SMTP_PASS` | app password |
| `RECIPIENTS` | where alerts go (comma-separated) |

### 4. Test
```
pip install -r requirements.txt
python tracker.py --provider mock --dry-run   # no keys needed
python tracker.py --dry-run                   # real prices, no email
python tracker.py --force-email               # real email
```

Then: Actions → Flight price check → Run workflow (tick `force_email`).

## Tuning

Everything is in `config.json`:
- `alert_below_total_usd` — your buy signal; lower it as the trip nears
- `routes` — add Chennai/Hyderabad, or change `JFK` to `EWR`
- `filters.airlines` — currently `EK`, `QR`; clear to see all
- `trip.cabin` — flip to `business` to watch that tier

`price_history.json` accumulates every reading and is committed back here,
so you build a real price curve over the months.

## Backup

Also set a free Google Flights alert on the same route —
google.com/travel/flights → route and dates → **Track prices**.
