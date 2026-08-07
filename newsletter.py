import os
import json
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import gspread
from google.oauth2.service_account import Credentials
import time
import requests
import anthropic

# --- Config from environment variables (set as GitHub Actions secrets) ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "Subscribers")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def get_subscribers():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    rows = sheet.get_all_records()  # expects headers: Name, Email, Portfolio, Status
    return [r for r in rows if str(r.get("Status", "")).lower() == "active"]


def get_upcoming_earnings(ticker, days_ahead=7):
    """Checks Finnhub's earnings calendar for a report date in the next
    `days_ahead` days. Returns {"date": "2026-08-07", "timing": "after market
    close"} or None. Silently returns None for ETFs/crypto/funds — Finnhub's
    calendar just comes back empty for those, no special-casing needed."""
    from datetime import date, timedelta
    today = date.today()
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={
                "symbol": ticker,
                "from": today.isoformat(),
                "to": (today + timedelta(days=days_ahead)).isoformat(),
                "token": FINNHUB_API_KEY,
            },
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("earningsCalendar", [])
        if not items:
            return None
        items.sort(key=lambda x: x.get("date", ""))
        soonest = items[0]
        timing_map = {
            "bmo": "before market open",
            "amc": "after market close",
            "dmh": "during market hours",
        }
        return {
            "date": soonest.get("date"),
            "timing": timing_map.get(soonest.get("hour"), ""),
        }
    except Exception as e:
        print(f"Earnings calendar check failed for {ticker}: {e}")
        return None


def get_macro_events(days_ahead=5):
    """Fetches upcoming high-impact US macro events (Fed rate decisions, CPI,
    jobs reports) from Finnhub's economic calendar. Relevant mainly for
    broad-market funds/ETFs, which don't have a single-company earnings date
    but do move on these releases. Fetched once per run and reused across all
    fund tickers, rather than once per ticker, to avoid redundant API calls."""
    from datetime import date, timedelta
    today = date.today()
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={
                "from": today.isoformat(),
                "to": (today + timedelta(days=days_ahead)).isoformat(),
                "token": FINNHUB_API_KEY,
            },
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("economicCalendar", [])
        high_impact = [
            e for e in items
            if str(e.get("country", "")).upper() == "US" and str(e.get("impact", "")).lower() == "high"
        ]
        high_impact.sort(key=lambda x: x.get("time", ""))
        return high_impact[:2]  # keep it to the soonest couple, not a wall of events
    except Exception as e:
        print(f"Macro calendar check failed, proceeding without it: {e}")
        return []


def get_stock_snapshot(ticker, macro_events=None, max_retries=3):
    """Pull current price + % change + a recent headline for one ticker via Finnhub."""
    for attempt in range(1, max_retries + 1):
        try:
            quote_resp = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": ticker, "token": FINNHUB_API_KEY},
                timeout=20
            )
            quote_resp.raise_for_status()
            quote = quote_resp.json()

            current_price = quote.get("c")
            prev_close = quote.get("pc")

            if not current_price or not prev_close:
                print(f"No price data returned for {ticker}")
                return None

            pct_change = ((current_price - prev_close) / prev_close) * 100

            # Recent company news (last 3 days)
            headline = None
            from datetime import date, timedelta
            today = date.today()
            news_resp = requests.get(
                "https://finnhub.io/api/v1/company-news",
                params={
                    "symbol": ticker,
                    "from": (today - timedelta(days=3)).isoformat(),
                    "to": today.isoformat(),
                    "token": FINNHUB_API_KEY,
                },
                timeout=20
            )
            if news_resp.ok:
                news_items = news_resp.json()
                if news_items:
                    headline = news_items[0].get("headline")

            return {
                "ticker": ticker,
                "price": round(current_price, 2),
                "pct_change": round(pct_change, 2),
                "headline": headline,
                "earnings": get_upcoming_earnings(ticker),
                "macro_events": (macro_events or []) if ticker in KNOWN_FUNDS else [],
            }
        except Exception as e:
            print(f"Attempt {attempt}/{max_retries} failed for {ticker}: {e}")
            if attempt < max_retries:
                time.sleep(3 * attempt)  # brief backoff before retrying
            else:
                print(f"Giving up on {ticker} after {max_retries} attempts")
                return None


FORBIDDEN_PHRASES = [
    # Direct second-person directives — the clearest signal of actual advice
    "you should buy", "you should sell", "you should hold",
    "you might buy", "you might sell", "you might want to buy", "you might want to sell",
    "you could buy", "you could sell",
    "consider buying", "consider selling", "consider holding",
    # Explicit recommendation language
    "should invest", "i recommend", "we recommend", "recommendation is",
    "good time to buy", "good time to sell", "bad time to buy", "bad time to sell",
    "strong buy", "strong sell", "buy now", "sell now",
    "invest now", "worth investing", "worth buying", "worth selling",
]
# Note: bare words like "buy," "sell," and "hold" are intentionally NOT in this
# list on their own — they show up constantly in legitimate historical/descriptive
# writing (e.g. "investors continued to buy shares after the earnings beat"),
# which isn't advice. Blocking the bare words caused valid content to get
# discarded inconsistently. The phrases above catch actual directive language
# instead of penalizing normal market vocabulary.

# Common broad-market index funds/ETFs — used to hint the model so it doesn't
# talk about "earnings" or "the company" for something that isn't a single company.
# Add any ETF/fund tickers from your own portfolio here if their write-ups look off.
KNOWN_FUNDS = {
    "VOO", "SPY", "VTI", "QQQ", "IVV", "VXUS", "VEA", "VWO", "BND", "AGG",
    "ARKK", "DIA", "IWM", "SCHD", "VYM", "VUG", "VTV", "XLK", "XLF", "XLE",
    "TQQQ", "UCYB", "SQQQ", "SOXL", "SOXS", "UPRO", "SPXL", "SPXS", "TMF", "TMV",
}

# Rotated through so consecutive blurbs don't all lean on the same opening
# word/structure (e.g. everything starting with "Historically...").
STYLE_HINTS = [
    "Open with the upcoming date/event if there is one, then connect it to today's price move.",
    "Open with the price move itself, then pivot straight to what's coming up next.",
    "Open with the news/headline angle, then connect it to what's ahead.",
    "Open by framing the size of today's move (small/moderate/large relative to typical daily swings), then get to what's next.",
]


def generate_blurb(snapshot, variation_index=0):
    """Ask Claude for a catchy headline + short educational blurb. Never buy/sell language.
    Returns a dict: {"headline": ..., "body": ...}"""
    is_fund = snapshot['ticker'] in KNOWN_FUNDS
    # Crypto/forex symbols on Finnhub use an exchange-prefixed format like
    # "BINANCE:BTCUSDT" — the colon is a reliable signal it's not a stock/fund.
    is_crypto = ":" in snapshot['ticker']

    if is_crypto:
        asset_type_note = (
            "This ticker is a cryptocurrency, not a company or fund. Do NOT reference 'earnings reports,' "
            "'shares,' or talk about it as if it were a company or index fund. Discuss it in terms of "
            "crypto market trends, trading activity, or sentiment instead. Cryptocurrency markets trade "
            "24/7 and tend to be more volatile than stocks — you can note this as general context if relevant."
        )
    elif is_fund:
        asset_type_note = (
            "This ticker is a broad-market index fund or ETF, not a single company — "
            "it holds many underlying stocks. Do NOT reference 'earnings reports' or "
            "talk about it as if it were one company. Instead, discuss it in terms of "
            "overall market/sector movement."
        )
    else:
        asset_type_note = "This ticker is an individual company's stock."

    style_hint = STYLE_HINTS[variation_index % len(STYLE_HINTS)]

    if snapshot.get("earnings"):
        e = snapshot["earnings"]
        timing_str = f" ({e['timing']})" if e["timing"] else ""
        earnings_line = f"Upcoming earnings: reports on {e['date']}{timing_str}."
    else:
        earnings_line = "Upcoming earnings: none scheduled in the next 7 days."

    macro_line = ""
    if snapshot.get("macro_events"):
        events_desc = "; ".join(
            f"{e.get('event', 'Economic release')} on {str(e.get('time', ''))[:10]}"
            for e in snapshot["macro_events"]
        )
        macro_line = f"Upcoming macro events (relevant to broad-market funds): {events_desc}."

    prompt = f"""You are writing content for an educational investing newsletter, covering one ticker.

Ticker: {snapshot['ticker']}
{asset_type_note}
Current price: ${snapshot['price']}
Change since last close: {snapshot['pct_change']}%
Recent headline: {snapshot['headline'] or 'No major headline today'}
{earnings_line}
{macro_line}

Produce TWO things:
1. A short, catchy, punny/playful headline (max 8 words) related to the news, price move, or
   upcoming date above — think newspaper-style wordplay tied to the company/fund or what's happening
   (e.g. for a rocket company having a good day: "SpaceX Shoots for the Moon").
   The headline must NOT imply the reader should buy, sell, or take any action.
2. A brief paragraph (2-3 sentences) that prioritizes FORWARD-LOOKING, factual information —
   readers want to know what's coming up and what could move the price next, not just what already
   happened. Follow this priority order:
   a) If there's an upcoming earnings date listed above, LEAD with it — state the date (and timing,
      if known) plainly. This is the single most useful thing you can tell a reader holding this stock.
   b) If there are upcoming macro events listed above (for funds/ETFs), lead with those instead —
      funds don't have their own earnings, so Fed decisions, inflation data, and jobs reports are
      the equivalent "what's coming up" information for them.
   c) Read the "Recent headline" carefully for any OTHER concrete, forward-looking catalyst it
      mentions or implies — e.g. a pending FDA decision, a scheduled court ruling, a merger vote
      date, a product launch, a regulatory deadline. If one is there, surface it plainly, since
      this is exactly the kind of thing that can move the price and readers want to know about it.
   d) Only if there is genuinely nothing forward-looking to report from (a)-(c), fall back to a
      short neutral note on what historically tends to follow this kind of move — this is the
      last resort, not the default.
   Never predict which way the price will move because of any of the above. State facts and known
   dates, not forecasts.

READABILITY — this is written for everyday personal investors, not finance professionals. Follow these
rules strictly:
- Use short, simple sentences. One idea per sentence.
- Avoid financial jargon (e.g. don't say "volatility," "momentum," "valuation multiples," "market cap
  compression" — say things like "the price swung a lot" or "investors reacted quickly" instead).
- If a technical term is genuinely necessary, briefly explain it in plain words right there.
- Write like you're explaining it to a smart friend who doesn't follow the stock market closely —
  clear and conversational, not textbook or press-release toned.
- Prefer concrete, everyday comparisons over abstract financial concepts.

STYLE: {style_hint} Avoid starting with the word "Historically" — vary sentence openings
and structure so this doesn't read like a template repeated for every stock. Keep the actual information
(price move, upcoming dates, catalysts) the same regardless of phrasing style.

STRICT RULES — these are non-negotiable, for BOTH the headline and paragraph:
- Do NOT use the words "buy," "sell," "hold," or any variation telling the reader what to do.
- Do NOT recommend, suggest, or imply any action the reader should take.
- Do NOT say things like "good time to," "bad time to," "worth considering," or similar action-nudging phrases.
- Do NOT predict future price direction ("will likely rise/fall") — stating a known upcoming date or
  event (like an earnings date or FDA decision date) is fine; guessing what happens to the price
  because of it is not.
- Do not invent a catalyst that isn't actually in the headline/data above — only surface what's
  genuinely there.
- Do not add a disclaimer sentence — one is added separately in the email template.

Respond ONLY with valid JSON in this exact format, nothing else, no markdown code fences:
{{"headline": "your headline here", "body": "your paragraph here"}}"""

    fallback = {
        "headline": f"{snapshot['ticker']} Update",
        "body": (f"{snapshot['ticker']} moved {snapshot['pct_change']}% today. "
                 f"(Trend note unavailable for this update.)")
    }

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if the model added them anyway
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()

        parsed = json.loads(raw)
        headline = str(parsed.get("headline", "")).strip()
        body = str(parsed.get("body", "")).strip()

        if not headline or not body:
            raise ValueError("Missing headline or body in response")

    except Exception as e:
        print(f"Claude call/parse failed for {snapshot['ticker']}: {e}")
        return fallback

    # Second layer of protection: flag (don't silently trust the model).
    # Uses word-boundary matching, not raw substring matching — otherwise "hold"
    # would false-positive on completely innocent words like "holds", "holdings",
    # or "shareholders", which are normal, safe things to say about a fund.
    combined_lowered = (headline + " " + body).lower()
    flagged = any(
        re.search(r'\b' + re.escape(phrase) + r'\b', combined_lowered)
        for phrase in FORBIDDEN_PHRASES
    )
    if flagged:
        print(f"WARNING: content for {snapshot['ticker']} contained flagged language, using fallback text")
        return {
            "headline": f"{snapshot['ticker']} Update",
            "body": (f"{snapshot['ticker']} moved {snapshot['pct_change']}% following recent news. "
                     f"No further trend detail available for this update.")
        }

    return {"headline": headline, "body": body}


def build_email_html(name, stock_sections):
    sections_html = ""
    for s in stock_sections:
        arrow = "▲" if s["pct_change"] >= 0 else "▼"
        headline = s["blurb"]["headline"]
        body = s["blurb"]["body"]
        # Display-friendly ticker: strip exchange prefix for crypto (e.g.
        # "BINANCE:BTCUSDT" shows as "BTCUSDT") so it doesn't look technical.
        display_ticker = s['ticker'].split(":")[-1] if ":" in s['ticker'] else s['ticker']
        sections_html += f"""
        <div style="margin-bottom:20px; padding:14px; border:1px solid #ddd; border-radius:6px;">
          <h2 style="margin:0 0 4px; font-size:17px; color:#1a1a1a;">{headline}</h2>
          <div style="font-size:13px; color:#555; margin-bottom:8px; font-weight:bold;">
            {display_ticker} &nbsp;·&nbsp; ${s['price']} &nbsp;<span style="color:#555;">{arrow}</span> {abs(s['pct_change'])}%
          </div>
          <p style="margin:0; color:#333;">{body}</p>
        </div>
        """

    from datetime import datetime
    import zoneinfo
    timestamp = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%B %-d, %Y — %-I:%M %p ET")

    return f"""
    <div style="font-family:Arial, sans-serif; max-width:600px; margin:auto;">
      <h2 style="margin-bottom:2px;">Your daily portfolio update</h2>
      <p style="font-size:12px; color:#888; margin-top:0;">Prices as of {timestamp}</p>
      <p>Hi {name}, here's what's happening with your stocks today.</p>
      {sections_html}
      <p style="font-size:12px; color:#777; margin-top:24px;">
        <strong>Educational content only.</strong> This newsletter shares general market information
        and historical context — it is not financial advice, and nothing here is a recommendation
        to buy, sell, or hold any investment.
      </p>
      <p style="font-size:12px; color:#777;">
        <strong>AI-generated content.</strong> The write-ups above are created using AI and may contain
        mistakes or inaccuracies. Always verify important details yourself before making any decisions
        about your portfolio.
      </p>
      <p style="font-size:12px; color:#777;">
        <strong>Data accuracy.</strong> Price and market data is provided by third-party sources and may
        be delayed, incomplete, or occasionally inaccurate.
      </p>
      <p style="font-size:12px; color:#777;">
        <strong>Past performance.</strong> Historical patterns referenced in this newsletter are not
        indicative of future results.
      </p>
      <p style="font-size:12px; color:#777;">
        <strong>No advisory relationship.</strong> EPPN is not a registered investment advisor and does
        not act in a fiduciary capacity for subscribers.
      </p>
      <p style="font-size:12px; color:#777;">
        Can't find a ticker, or does something look off — missing news, an unclear price, or
        anything else that doesn't seem right? Email us at
        <a href="mailto:YOUR_EMAIL_HERE@example.com">YOUR_EMAIL_HERE@example.com</a> and we'll look into adding it.
      </p>
      <p style="font-size:12px;">
        <a href="https://yourdomain.com/edit-portfolio">Edit portfolio</a> |
        <a href="https://yourdomain.com/unsubscribe">Unsubscribe</a>
      </p>
      <p style="font-size:11px; color:#aaa;">
        [Your business mailing address here — required by the CAN-SPAM Act for commercial email]
      </p>
    </div>
    """


def send_email(server, to_email, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))
    server.sendmail(EMAIL_ADDRESS, to_email, msg.as_string())


def is_trading_day():
    """Checks Finnhub's market-status endpoint to see if today is a US market
    holiday. We check this instead of just 'is the market open right now',
    since the script runs before market open (8am) — 'open' would always be
    false at that hour even on a normal trading day."""
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/market-status",
            params={"exchange": "US", "token": FINNHUB_API_KEY},
            timeout=10
        )
        resp.raise_for_status()
        status = resp.json()
        holiday_name = status.get("holiday")
        if holiday_name:
            print(f"Market holiday today ({holiday_name}) — skipping send.")
            return False
        return True
    except Exception as e:
        # If the check itself fails, default to sending rather than silently
        # skipping a real trading day due to an unrelated API hiccup.
        print(f"Could not check market holiday status, proceeding anyway: {e}")
        return True


def main():
    if not is_trading_day():
        return

    subscribers = get_subscribers()
    print(f"Found {len(subscribers)} active subscribers")

    # Fetch once per run (not per ticker) — reused for every fund/ETF snapshot.
    macro_events = get_macro_events()

    # Cache stock snapshots/blurbs so we don't re-fetch/re-generate per subscriber
    # if multiple people hold the same stock.
    cache = {}

    # Open ONE SMTP connection and reuse it for every email, instead of
    # reconnecting/logging in from scratch per subscriber (this was the main
    # slowdown as subscriber count grows).
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)

        for sub in subscribers:
            name = sub.get("Name", "there")
            email = sub.get("Email")
            portfolio = [t.strip().upper() for t in sub.get("Portfolio", "").split(",") if t.strip()]

            if not email or not portfolio:
                continue

            stock_sections = []
            for ticker in portfolio:
                if ticker not in cache:
                    snapshot = get_stock_snapshot(ticker, macro_events=macro_events)
                    if snapshot:
                        # Vary style based on how many unique tickers we've already
                        # generated, so back-to-back blurbs in one email don't match.
                        snapshot["blurb"] = generate_blurb(snapshot, variation_index=len(cache))
                        cache[ticker] = snapshot
                    else:
                        continue
                stock_sections.append(cache[ticker])

            if not stock_sections:
                print(f"No valid stock data for {email}, skipping")
                continue

            html = build_email_html(name, stock_sections)
            try:
                send_email(server, email, "Your daily portfolio update — EPPN", html)
                print(f"Sent to {email}")
            except Exception as e:
                print(f"Failed to send to {email}: {e}")


if __name__ == "__main__":
    main()
