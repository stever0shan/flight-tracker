#!/usr/bin/env python3
"""
Flight price tracker with email alerts.

Checks a route daily, keeps a price history, and emails when something
worth knowing happens: a drop below your target, a new all-time low,
a sharp rise, or the weekly digest.

Providers:
  serpapi  - Google Flights via SerpApi (recommended; 100 free searches/month)
  amadeus  - Amadeus Self-Service API (free tier, sparser inventory)
  mock     - fake data, for testing the pipeline with no API key

Usage:
  python tracker.py                  # normal run
  python tracker.py --provider mock  # test end to end
  python tracker.py --dry-run        # check prices, print email, don't send
  python tracker.py --force-email    # send digest regardless of triggers
"""

import argparse
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = ROOT / "price_history.json"
STATE_PATH = ROOT / "alert_state.json"

CABIN_SERPAPI = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
CABIN_AMADEUS = {
    "economy": "ECONOMY",
    "premium_economy": "PREMIUM_ECONOMY",
    "business": "BUSINESS",
    "first": "FIRST",
}


def load_json(path, default):
    if Path(path).exists():
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def now_utc():
    return datetime.now(timezone.utc)


def hours_to_departure(date_str):
    dep = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (dep - now_utc()).days


def fetch_serpapi(route, trip, filters, api_key):
    """Google Flights via SerpApi."""
    params = {
        "engine": "google_flights",
        "departure_id": route["origin"],
        "arrival_id": route["destination"],
        "outbound_date": trip["outbound_date"],
        "return_date": trip["return_date"],
        "currency": trip.get("currency", "USD"),
        "adults": trip["adults"],
        "travel_class": CABIN_SERPAPI.get(trip["cabin"], 1),
        "type": "1",
        "hl": "en",
        "api_key": api_key,
    }
    if filters.get("max_stops") is not None:
        params["stops"] = min(filters["max_stops"] + 1, 3)
    if filters.get("airlines"):
        params["include_airlines"] = ",".join(filters["airlines"])

    r = requests.get("https://serpapi.com/search", params=params, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"SerpApi: {data['error']}")

    offers = []
    for bucket in ("best_flights", "other_flights"):
        for item in data.get(bucket, []) or []:
            legs = item.get("flights", []) or []
            if not legs:
                continue
            total_min = item.get("total_duration") or 0
            carriers = sorted({(l.get("flight_number", "") or "")[:2] for l in legs})
            carriers = [c for c in carriers if c]
            offers.append({
                "total_price": float(item["price"]),
                "per_person": round(float(item["price"]) / trip["adults"], 2),
                "duration_hours": round(total_min / 60, 1) if total_min else None,
                "stops": max(len(legs) - 1, 0),
                "airlines": carriers or [l.get("airline", "?") for l in legs],
                "summary": " -> ".join(
                    [legs[0].get("departure_airport", {}).get("id", "?")] +
                    [l.get("arrival_airport", {}).get("id", "?") for l in legs]
                ),
            })
    return offers


def fetch_amadeus(route, trip, filters, client_id, client_secret):
    """Amadeus Self-Service Flight Offers Search."""
    tok = requests.post(
        "https://api.amadeus.com/v1/security/oauth2/token",
        data={"grant_type": "client_credentials",
              "client_id": client_id, "client_secret": client_secret},
        timeout=30,
    )
    tok.raise_for_status()
    access = tok.json()["access_token"]

    params = {
        "originLocationCode": route["origin"],
        "destinationLocationCode": route["destination"],
        "departureDate": trip["outbound_date"],
        "returnDate": trip["return_date"],
        "adults": trip["adults"],
        "travelClass": CABIN_AMADEUS.get(trip["cabin"], "ECONOMY"),
        "currencyCode": trip.get("currency", "USD"),
        "max": 20,
    }
    if filters.get("airlines"):
        params["includedAirlineCodes"] = ",".join(filters["airlines"])
    if filters.get("max_stops") == 0:
        params["nonStop"] = "true"

    r = requests.get(
        "https://api.amadeus.com/v2/shopping/flight-offers",
        params=params, headers={"Authorization": f"Bearer {access}"}, timeout=60,
    )
    r.raise_for_status()

    offers = []
    for o in r.json().get("data", []):
        total = float(o["price"]["grandTotal"])
        its = o.get("itineraries", [])
        segs = [s for it in its for s in it.get("segments", [])]
        stops = max(sum(len(it.get("segments", [])) for it in its) - len(its), 0)
        offers.append({
            "total_price": total,
            "per_person": round(total / trip["adults"], 2),
            "duration_hours": None,
            "stops": stops,
            "airlines": sorted({s["carrierCode"] for s in segs}),
            "summary": " -> ".join(
                [segs[0]["departure"]["iataCode"]] +
                [s["arrival"]["iataCode"] for s in segs]
            ) if segs else "",
        })
    return offers


def fetch_mock(route, trip, filters, seed=0):
    """Deterministic fake data so you can test without any API key."""
    import random
    rnd = random.Random(f"{route['origin']}{datetime.now().date()}{seed}")
    base = 2328.0 if route["origin"] == "COK" else 2240.0
    out = []
    for i in range(4):
        p = base * (1 + rnd.uniform(-0.12, 0.10)) + i * 60
        out.append({
            "total_price": round(p, 2),
            "per_person": round(p / trip["adults"], 2),
            "duration_hours": round(rnd.uniform(19, 26), 1),
            "stops": 1,
            "airlines": ["EK"] if i % 2 else ["QR"],
            "summary": f"{route['origin']} -> DXB -> {route['destination']}",
        })
    return sorted(out, key=lambda x: x["total_price"])


def apply_filters(offers, filters):
    keep = []
    for o in offers:
        if filters.get("max_stops") is not None and o["stops"] > filters["max_stops"]:
            continue
        mx = filters.get("max_total_duration_hours")
        if mx and o.get("duration_hours") and o["duration_hours"] > mx:
            continue
        allowed = filters.get("airlines")
        if allowed and o.get("airlines"):
            if not any(a in allowed for a in o["airlines"]):
                continue
        keep.append(o)
    return sorted(keep, key=lambda x: x["total_price"])


def analyse(route_key, cheapest, history, alerts):
    """Decide whether this reading is worth emailing about."""
    series = [h for h in history if h["route"] == route_key]
    prev = series[-1]["total_price"] if series else None
    lows = [h["total_price"] for h in series]
    prior_low = min(lows) if lows else None

    price = cheapest["total_price"]
    triggers = []

    if price <= alerts.get("alert_below_total_usd", 0):
        triggers.append(("TARGET", f"At or below your ${alerts['alert_below_total_usd']:,.0f} target"))

    if prev:
        pct = (prev - price) / prev * 100
        if pct >= alerts.get("alert_on_pct_drop", 5):
            triggers.append(("DROP", f"Down {pct:.1f}% since last check (${prev:,.0f} -> ${price:,.0f})"))
        if -pct >= alerts.get("alert_on_spike_pct", 12):
            triggers.append(("SPIKE", f"Up {-pct:.1f}% since last check — cheap buckets may be selling out"))

    if alerts.get("alert_on_new_low") and prior_low and price < prior_low:
        triggers.append(("NEW LOW", f"Cheapest seen yet (previous low ${prior_low:,.0f})"))

    return triggers, prev, prior_low


def sparkline(values, width=28):
    if len(values) < 2:
        return ""
    blocks = "_.-~=*#"
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1
    pts = values[-width:]
    return "".join(blocks[int((v - lo) / rng * (len(blocks) - 1))] for v in pts)


def build_email(cfg, results, triggered, digest):
    trip = cfg["trip"]
    days = hours_to_departure(trip["outbound_date"])
    baseline = cfg["alerts"]["baseline_total_usd"]

    if triggered:
        tag = triggered[0][0]
        subject = f"[{tag}] {trip['label']} — ${results[0]['cheapest']['total_price']:,.0f}"
    else:
        label = "Daily" if cfg["alerts"].get("digest_every_run") else "Weekly"
        subject = f"[{label}] {trip['label']} — ${results[0]['cheapest']['total_price']:,.0f}"

    rows = []
    for r in results:
        c = r["cheapest"]
        delta = c["total_price"] - baseline
        arrow = "DOWN" if delta < 0 else ("UP" if delta > 0 else "flat")
        colour = "#15803d" if delta < 0 else "#b91c1c" if delta > 0 else "#6b7280"
        rows.append(
            "<tr>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #e5e7eb'><b>{r['label']}</b><br>"
            f"<span style='color:#6b7280;font-size:12px'>{c['summary']} &middot; {c['stops']} stop "
            f"&middot; {', '.join(c['airlines'][:3])}</span></td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #e5e7eb;text-align:right'>"
            f"<b style='font-size:17px'>${c['total_price']:,.0f}</b><br>"
            f"<span style='color:#6b7280;font-size:12px'>${c['per_person']:,.0f} pp</span></td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #e5e7eb;text-align:right;color:{colour}'>"
            f"{arrow} ${abs(delta):,.0f}<br><span style='font-size:12px'>vs baseline</span></td></tr>"
        )

    trig_html = ""
    if triggered:
        items = "".join(f"<li><b>{t}</b> — {m}</li>" for t, m in triggered)
        trig_html = ("<div style='background:#fef3c7;border-left:4px solid #f59e0b;padding:12px 16px;margin:16px 0'>"
                     f"<b>Why you're getting this</b><ul style='margin:8px 0 0 18px;padding:0'>{items}</ul></div>")

    spark_html = ""
    for r in results:
        if r["series"] and len(r["series"]) > 1:
            spark_html += (
                "<div style='font-family:monospace;font-size:13px;color:#374151'>"
                f"{r['label']}: {sparkline(r['series'])} "
                f"(low ${min(r['series']):,.0f} / high ${max(r['series']):,.0f}, "
                f"{len(r['series'])} checks)</div>"
            )

    window = ""
    if 100 <= days <= 190:
        window = ("<div style='background:#dcfce7;border-left:4px solid #16a34a;padding:12px 16px;margin:16px 0'>"
                  "<b>You are in the target booking window (4–6 months out).</b> "
                  "Historically this is where most of the available savings are. "
                  "Waiting much longer on a peak-summer India–US route usually costs more than it saves.</div>")
    elif days < 100:
        window = ("<div style='background:#fee2e2;border-left:4px solid #dc2626;padding:12px 16px;margin:16px 0'>"
                  "<b>Past the recommended window.</b> Peak-season fares on this route tend to climb from here.</div>")

    html = (
        "<html><body style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "max-width:640px;margin:0 auto;color:#111827\">"
        f"<h2 style='margin-bottom:4px'>{trip['label']}</h2>"
        "<div style='color:#6b7280;font-size:13px;margin-bottom:16px'>"
        f"{trip['outbound_date']} &rarr; {trip['return_date']} &middot; {trip['adults']} passengers &middot; "
        f"{trip['cabin'].replace('_',' ')} &middot; <b>{days} days out</b></div>"
        f"{trig_html}"
        "<table style='width:100%;border-collapse:collapse;border:1px solid #e5e7eb'>"
        "<tr style='background:#f9fafb'>"
        "<th style='padding:10px 14px;text-align:left;font-size:12px;color:#6b7280'>ROUTE</th>"
        "<th style='padding:10px 14px;text-align:right;font-size:12px;color:#6b7280'>CHEAPEST</th>"
        "<th style='padding:10px 14px;text-align:right;font-size:12px;color:#6b7280'>CHANGE</th></tr>"
        f"{''.join(rows)}</table>"
        f"<div style='margin:16px 0'>{spark_html}</div>{window}"
        "<div style='color:#6b7280;font-size:12px;margin-top:20px;border-top:1px solid #e5e7eb;padding-top:12px'>"
        f"Baseline ${baseline:,.0f} (Aug 2026 quote, Rs 2.23 lakh). "
        f"Filters: {', '.join(cfg['filters']['airlines'])}, max {cfg['filters']['max_stops']} stop. "
        f"Checked {now_utc():%Y-%m-%d %H:%M} UTC.</div></body></html>"
    )
    return subject, html


def send_email(subject, html, recipients):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    sender = os.environ.get("SMTP_FROM", user)
    if not (user and pw):
        raise RuntimeError("SMTP_USER / SMTP_PASS not set")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(host, port, timeout=45) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=os.environ.get("PROVIDER", "serpapi"),
                    choices=["serpapi", "amadeus", "mock"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-email", action="store_true")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, {})
    history = load_json(HISTORY_PATH, [])
    state = load_json(STATE_PATH, {})
    trip, filters, alerts = cfg["trip"], cfg["filters"], cfg["alerts"]

    # Recipients come from the RECIPIENTS env var / secret so the repo can be
    # public without exposing an address. Falls back to config for local use.
    recipients = [e.strip() for e in os.environ.get("RECIPIENTS", "").split(",") if e.strip()]
    if not recipients:
        recipients = cfg.get("recipients", [])

    results, all_triggers = [], []

    for route in cfg["routes"]:
        key = f"{route['origin']}-{route['destination']}"
        try:
            if args.provider == "serpapi":
                offers = fetch_serpapi(route, trip, filters, os.environ["SERPAPI_KEY"])
            elif args.provider == "amadeus":
                offers = fetch_amadeus(route, trip, filters,
                                       os.environ["AMADEUS_ID"], os.environ["AMADEUS_SECRET"])
            else:
                offers = fetch_mock(route, trip, filters)
        except Exception as e:
            print(f"  !! {key}: {e}", file=sys.stderr)
            continue

        offers = apply_filters(offers, filters)
        if not offers:
            print(f"  -- {key}: no offers matched filters")
            continue

        cheapest = offers[0]
        triggers, prev, prior_low = analyse(key, cheapest, history, alerts)
        series = [h["total_price"] for h in history if h["route"] == key]
        series.append(cheapest["total_price"])

        history.append({
            "checked_at": now_utc().isoformat(),
            "route": key,
            "total_price": cheapest["total_price"],
            "per_person": cheapest["per_person"],
            "stops": cheapest["stops"],
            "airlines": cheapest["airlines"],
        })

        results.append({"label": route["label"], "key": key,
                        "cheapest": cheapest, "series": series})
        all_triggers.extend(triggers)
        flag = f"  <-- {', '.join(t for t, _ in triggers)}" if triggers else ""
        print(f"  {key}: ${cheapest['total_price']:,.0f} "
              f"(${cheapest['per_person']:,.0f} pp, {cheapest['stops']} stop){flag}")

    if not results:
        print("No results; nothing to do.")
        return 1

    is_digest_day = (alerts.get("digest_every_run", False)
                     or now_utc().weekday() == alerts.get("weekly_digest_weekday", 6))
    last = state.get("last_email_at")
    cooled = True
    if last:
        cooled = (now_utc() - datetime.fromisoformat(last)) > timedelta(
            hours=alerts.get("min_hours_between_alerts", 20))

    should = args.force_email or (all_triggers and cooled) or (is_digest_day and cooled)

    if should:
        subject, html = build_email(cfg, results, all_triggers, is_digest_day)
        if args.dry_run:
            print(f"\n[dry run] subject: {subject}\n[dry run] {len(html)} bytes of HTML")
        else:
            if not recipients:
                raise RuntimeError("No recipients: set the RECIPIENTS secret")
            send_email(subject, html, recipients)
            state["last_email_at"] = now_utc().isoformat()
            print(f"\nEmailed: {subject}")
    else:
        print("\nNo trigger and not digest day — no email sent.")

    if not args.dry_run:
        save_json(HISTORY_PATH, history[-2000:])
        save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
