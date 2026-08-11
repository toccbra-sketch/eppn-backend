import os
import json
import re

import gspread
from google.oauth2.service_account import Credentials
import time
import requests
import anthropic

# --- Config from environment variables (set as GitHub Actions secrets) ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "Subscribers")
SPONSOR_SHEET_NAME = os.environ.get("SPONSOR_SHEET_NAME", "Sponsors")

# SEC EDGAR requires a descriptive User-Agent identifying the requester (their
# fair-access policy) — no API key needed, it's a free public API, but requests
# without a real User-Agent get rate-limited/blocked.
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT", "The Portfolio Briefcase contact@theportfoliobriefcase.com"
)

# --- Brevo (email API) setup ---
# Same reasoning as app.py: sending via Brevo's HTTPS API instead of raw SMTP
# means we can send from a real domain address (contact@theportfoliobriefcase.com)
# with proper SPF/DKIM authentication once the domain is verified in Brevo —
# something plain Gmail SMTP could never do, and it's the single biggest lever
# for staying out of spam folders.
BREVO_API_KEY = os.environ["BREVO_API_KEY"]
EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]  # must be a verified sender/domain in Brevo
SENDER_NAME = os.environ.get("SENDER_NAME", "The Portfolio Briefcase")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def get_subscribers():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    rows = sheet.get_all_records()  # expects headers: Name, Email, Portfolio, Status
    return [r for r in rows if str(r.get("Status", "")).lower() == "active"]


def get_active_sponsor():
    """Reads the Sponsors sheet tab for a currently-active sponsor slot.
    Expected headers: Active, Name, Blurb, LinkURL, StartDate, EndDate, Clicks.
    StartDate/EndDate are optional (YYYY-MM-DD) — leave blank for no expiry.
    Returns {"name", "blurb", "row"} or None. This fails safe: a missing tab,
    bad date format, or any other error just means no sponsor shows that day —
    it never breaks the newsletter send. Note: no LinkURL is returned here on
    purpose — the email links to a backend redirect that looks up the URL
    itself from this same sheet, so the link never carries the destination in
    a client-visible way (keeps the redirect endpoint from being an open
    redirect anyone could repurpose)."""
    from datetime import date
    try:
        creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SPONSOR_SHEET_NAME)
        records = sheet.get_all_records()
        today = date.today()

        for i, r in enumerate(records, start=2):  # row 1 is headers
            if str(r.get("Active", "")).strip().upper() not in ("TRUE", "YES", "1"):
                continue

            start_str = str(r.get("StartDate", "")).strip()
            end_str = str(r.get("EndDate", "")).strip()
            try:
                if start_str and date.fromisoformat(start_str) > today:
                    continue
                if end_str and date.fromisoformat(end_str) < today:
                    continue
            except ValueError:
                pass  # bad date format in the sheet — ignore date gating rather than crash

            name = str(r.get("Name", "")).strip()
            blurb = str(r.get("Blurb", "")).strip()
            if not name or not blurb:
                continue

            return {"name": name, "blurb": blurb, "row": i}

        return None
    except Exception as e:
        print(f"Sponsor lookup failed, proceeding without a sponsor slot: {e}")
        return None


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


# FOMC meeting decision dates, pulled directly from federalreserve.gov's official
# published schedule (https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm).
# The Fed publishes these roughly a year or more in advance and they essentially
# never move, so a small hardcoded table here is more reliable than an API call —
# and it's free, unlike Finnhub's economic-calendar endpoint (which requires a
# paid plan). Each entry is the SECOND day of the two-day meeting, since that's
# when the rate decision is actually announced, at 2:00 PM ET.
# Maintenance: add next year's dates here once the Fed publishes them (usually
# announced each August/September for the following year).
FOMC_DECISION_DATES = [
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    # 2027
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
]


def get_macro_events(days_ahead=5):
    """Checks the hardcoded FOMC schedule for a rate decision in the next
    `days_ahead` days. Relevant mainly for broad-market funds/ETFs, which
    don't have a single-company earnings date but do move on Fed decisions.
    Returns a list shaped like [{"event": ..., "time": "YYYY-MM-DD"}] to match
    what generate_blurb expects — same shape as the old Finnhub-based version,
    so nothing downstream needed to change."""
    from datetime import date, timedelta
    today = date.today()
    cutoff = today + timedelta(days=days_ahead)

    upcoming = [
        d for d in FOMC_DECISION_DATES
        if today.isoformat() <= d <= cutoff.isoformat()
    ]
    if not upcoming:
        return []

    return [{"event": "Fed interest rate decision", "time": upcoming[0]}]



# SEC EDGAR's ticker-to-CIK mapping is a single free JSON file, refreshed by
# the SEC periodically. We fetch it once per run and cache it in memory —
# every ticker lookup afterward is just a dict access, no extra network calls.
_CIK_MAP_CACHE = None

# Filing types worth surfacing to a reader who isn't a securities lawyer.
# 8-K = "something material just happened" (earnings, executive changes, M&A,
#   bankruptcy, etc.) — the single most useful forward-looking filing type.
# 10-Q/10-K = quarterly/annual report (usually coincides with earnings).
# DEF 14A = proxy statement, usually means a shareholder vote/meeting is coming.
# S-1/S-3/424B = new stock or bond offering being registered.
RELEVANT_FORMS = {"8-K", "10-Q", "10-K", "DEF 14A", "S-1", "S-3", "424B5", "424B4"}


def _get_cik_map():
    global _CIK_MAP_CACHE
    if _CIK_MAP_CACHE is not None:
        return _CIK_MAP_CACHE
    try:
        resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": SEC_USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        # File is shaped like {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        _CIK_MAP_CACHE = {
            row["ticker"].upper(): str(row["cik_str"]).zfill(10)
            for row in data.values()
        }
    except Exception as e:
        print(f"Could not load SEC ticker->CIK map, filings lookup will be skipped: {e}")
        _CIK_MAP_CACHE = {}
    return _CIK_MAP_CACHE


def get_sec_filings(ticker, days_back=10):
    """Looks up the most recent notable SEC filing (8-K, 10-Q, 10-K, DEF 14A,
    or an offering-related form) for a ticker within the last `days_back` days.
    Returns {"form": "8-K", "date": "2026-08-05"} or None. ETFs, crypto, and
    tickers with no CIK match (funds mostly don't file this way) safely
    return None — this only applies to individual companies."""
    from datetime import date, timedelta

    cik = _get_cik_map().get(ticker.upper())
    if not cik:
        return None

    try:
        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": SEC_USER_AGENT},
            timeout=20,
        )
        resp.raise_for_status()
        recent = resp.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])

        cutoff = (date.today() - timedelta(days=days_back)).isoformat()
        for form, filed in zip(forms, dates):
            if form in RELEVANT_FORMS and filed >= cutoff:
                return {"form": form, "date": filed}
        return None
    except Exception as e:
        print(f"SEC filings lookup failed for {ticker}: {e}")
        return None


def get_stock_snapshot(ticker, macro_events=None, max_retries=3):
    """Pull current price + a recent headline for one ticker via Finnhub."""
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
                "pct_change": round(pct_change, 2),  # kept internally for style-hint variation only, not displayed
                "headline": headline,
                "earnings": get_upcoming_earnings(ticker),
                "macro_events": (macro_events or []) if ticker in KNOWN_FUNDS else [],
                "sec_filing": None if ticker in KNOWN_FUNDS or ":" in ticker else get_sec_filings(ticker),
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

# This block is IDENTICAL on every single call, regardless of ticker, asset
# type, or style variation — that's deliberate, so it can be marked
# cacheable. Ticker-specific data (price, headline, earnings, asset type,
# style hint) all live in the per-call dynamic block below instead, so they
# never invalidate the cache. Anthropic's prompt caching charges a ~25%
# premium on the first call that writes the cache, but only ~10% of normal
# input price on every subsequent call within the cache window (5 min) that
# reuses it — a net win as soon as a run generates more than one blurb.
STATIC_BLURB_INSTRUCTIONS = """You are writing content for an educational investing newsletter. You'll be given data about one ticker below, and must produce a headline and a short list of simple, readable bullet points about it.

Produce TWO things:
1. A short, catchy, punny/playful headline (max 8 words) related to the news or upcoming date given
   below — think newspaper-style wordplay tied to the company/fund or what's happening (e.g. for a
   rocket company with a pending launch: "SpaceX Counts Down to Launch Day").
   The headline must NOT imply the reader should buy, sell, or take any action, and must NOT be
   about today's price move — focus it on what's coming up instead.
2. Between 2 and 4 short bullet points, each ONE sentence, covering what's coming up and what could
   matter next for this holding — NOT a recap of today's price action. This reader already sees the
   price elsewhere; they want to know what's ahead. Pull bullets from whatever is available below,
   in this priority order, and SKIP any category that has no data rather than writing a filler bullet:
   a) Upcoming earnings date, if given — state the date (and timing, if known) plainly.
   b) Upcoming macro/Fed events, if given (mainly for funds/ETFs) — state the event and date plainly.
   c) A recent SEC filing, if given — briefly say what it is in plain English (e.g. an "8-K" is
      "a filing disclosing a major company event," a "DEF 14A" is "a notice for an upcoming
      shareholder vote," a "10-Q"/"10-K" is "a quarterly/annual financial report") and its date.
   d) Read the "Recent headline" below for any OTHER concrete, forward-looking angle it mentions or
      implies — a pending FDA decision, a scheduled court ruling, a merger vote, a product launch, a
      world/economic event that could affect this holding (trade policy, geopolitical developments,
      commodity prices, interest rates, etc.). If there's a real forward-looking angle in it, write
      a bullet about that angle — not a recap of the headline as old news.
   e) Only if there is genuinely nothing forward-looking available from (a)-(d), include ONE bullet
      with a brief, neutral factual note about the holding (e.g. its sector or role in a portfolio)
      instead of discussing the price move. Never fall back to describing today's price change.
   Never predict which way the price will move. State facts and known dates, not forecasts.
   IMPORTANT: If a category has no data, just omit it — never write things like "no earnings are
   scheduled" or "no filings were found." Absence of an event is not newsworthy; skip it silently.

READABILITY — this is written for everyday personal investors, not finance professionals. Follow these
rules strictly:
- Each bullet is ONE short, simple sentence. One idea per bullet, no run-ons.
- Avoid financial jargon (e.g. don't say "volatility," "momentum," "valuation multiples," "market cap
  compression" — say things like "the price swung a lot" or "investors reacted quickly" instead).
- If a technical term is genuinely necessary (like a filing type), briefly explain it in plain words
  right there in the same bullet.
- Write like you're explaining it to a smart friend who doesn't follow the stock market closely —
  clear and conversational, not textbook or press-release toned.
- Do NOT mention specific price levels or percentage changes anywhere in the bullets.

STRICT RULES — these are non-negotiable, for BOTH the headline and bullets:
- Do NOT use the words "buy," "sell," "hold," or any variation telling the reader what to do.
- Do NOT recommend, suggest, or imply any action the reader should take.
- Do NOT say things like "good time to," "bad time to," "worth considering," or similar action-nudging phrases.
- Do NOT predict future price direction ("will likely rise/fall") — stating a known upcoming date or
  event (like an earnings date or FDA decision date) is fine; guessing what happens to the price
  because of it is not.
- Do not invent a catalyst, filing, or event that isn't actually in the data below — only surface
  what's genuinely there.
- Do not add a disclaimer sentence — one is added separately in the email template.

Vary sentence openings and structure across different tickers so this doesn't read like a template
repeated for every stock. Avoid starting with the word "Historically."

Call the submit_blurb tool with your headline and bullets."""


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
        earnings_line = ""

    macro_line = ""
    if snapshot.get("macro_events"):
        events_desc = "; ".join(
            f"{e.get('event', 'Economic release')} on {str(e.get('time', ''))[:10]}"
            for e in snapshot["macro_events"]
        )
        macro_line = f"Upcoming macro events (relevant to broad-market funds): {events_desc}."

    filing_line = ""
    if snapshot.get("sec_filing"):
        f = snapshot["sec_filing"]
        filing_line = f"Recent SEC filing: form {f['form']} filed on {f['date']}."

    dynamic_data = f"""Ticker: {snapshot['ticker']}
{asset_type_note}
Recent headline: {snapshot['headline'] or 'No major headline today'}
{earnings_line}
{macro_line}
{filing_line}

STYLE for this one: {style_hint}"""

    fallback = {
        "headline": f"{snapshot['ticker']} Update",
        "bullets": [f"No major forward-looking news found for {snapshot['ticker']} today — check back tomorrow for updates."]
    }

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            tools=[{
                "name": "submit_blurb",
                "description": "Submit the generated headline and bullet points for this ticker.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "headline": {
                            "type": "string",
                            "description": "Short punny headline, max 8 words.",
                        },
                        "bullets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 4,
                            "description": "2-4 short, one-sentence bullet points following the priority order and rules above.",
                        },
                    },
                    "required": ["headline", "bullets"],
                },
            }],
            tool_choice={"type": "tool", "name": "submit_blurb"},
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": STATIC_BLURB_INSTRUCTIONS,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": dynamic_data,
                    },
                ],
            }]
        )

        tool_use_block = next(
            (block for block in response.content if block.type == "tool_use"),
            None
        )
        if not tool_use_block:
            raise ValueError("No tool_use block in response")

        headline = str(tool_use_block.input.get("headline", "")).strip()
        bullets = [str(b).strip() for b in tool_use_block.input.get("bullets", []) if str(b).strip()]

        if not headline or not bullets:
            raise ValueError("Missing headline or bullets in response")

    except Exception as e:
        print(f"Claude call/parse failed for {snapshot['ticker']}: {e}")
        return fallback

    # Second layer of protection: flag (don't silently trust the model).
    # Uses word-boundary matching, not raw substring matching — otherwise "hold"
    # would false-positive on completely innocent words like "holds", "holdings",
    # or "shareholders", which are normal, safe things to say about a fund.
    combined_lowered = (headline + " " + " ".join(bullets)).lower()
    flagged = any(
        re.search(r'\b' + re.escape(phrase) + r'\b', combined_lowered)
        for phrase in FORBIDDEN_PHRASES
    )
    if flagged:
        print(f"WARNING: content for {snapshot['ticker']} contained flagged language, using fallback text")
        return fallback

    return {"headline": headline, "bullets": bullets}


def build_email_html(name, stock_sections, sponsor=None, referral_count=0):
    # Colors matched to eppn-common.css so the email looks like an extension
    # of the site rather than a separate, older-looking product.
    NAVY_900 = "#0a1930"
    NAVY_700 = "#17365f"
    MUTED = "#667085"
    BORDER = "#dcdfe4"
    PAPER = "#f6f5f1"

    SITE_BASE = "https://theportfoliobriefcase.com"
    BACKEND_BASE = "https://eppn-backend.onrender.com"

    sections_html = ""
    for s in stock_sections:
        headline = s["blurb"]["headline"]
        bullets = s["blurb"]["bullets"]
        # Display-friendly ticker: strip exchange prefix for crypto (e.g.
        # "BINANCE:BTCUSDT" shows as "BTCUSDT") so it doesn't look technical.
        display_ticker = s['ticker'].split(":")[-1] if ":" in s['ticker'] else s['ticker']
        bullets_html = "".join(
            f'<li style="font-family:Arial,sans-serif; font-size:14px; color:#333; line-height:1.6; margin-bottom:4px;">{b}</li>'
            for b in bullets
        )
        sections_html += f"""
        <tr><td style="padding:0 0 12px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {BORDER}; border-radius:6px; background:#ffffff;">
            <tr><td style="padding:16px;">
              <div style="font-family:Georgia,'Times New Roman',serif; font-size:17px; color:{NAVY_900}; margin-bottom:6px;">{headline}</div>
              <div style="font-family:Arial,sans-serif; font-size:13px; font-weight:bold; color:{MUTED}; margin-bottom:10px;">
                {display_ticker} &nbsp;·&nbsp; ${s['price']}
              </div>
              <ul style="margin:0; padding-left:18px;">{bullets_html}</ul>
            </td></tr>
          </table>
        </td></tr>
        """

    from datetime import datetime
    import zoneinfo
    timestamp = datetime.now(zoneinfo.ZoneInfo("America/New_York")).strftime("%B %-d, %Y — %-I:%M %p ET")

    disclaimer_style = f"font-family:Arial,sans-serif; font-size:11px; color:#888; line-height:1.5; margin:0 0 10px;"

    sponsor_html = ""
    if sponsor:
        sponsor_link = f"{BACKEND_BASE}/sponsor-click?row={sponsor['row']}"
        sponsor_html = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px; border:1px dashed {BORDER}; border-radius:6px; background:#fafaf8;">
          <tr><td style="padding:14px 16px;">
            <div style="font-family:Arial,sans-serif; font-size:10px; font-weight:bold; letter-spacing:0.6px; text-transform:uppercase; color:{MUTED}; margin-bottom:6px;">Sponsored</div>
            <div style="font-family:Georgia,'Times New Roman',serif; font-size:15px; color:{NAVY_900}; margin-bottom:4px;">{sponsor['name']}</div>
            <div style="font-family:Arial,sans-serif; font-size:13px; color:#333; line-height:1.5; margin-bottom:8px;">{sponsor['blurb']}</div>
            <a href="{sponsor_link}" style="font-family:Arial,sans-serif; font-size:13px; font-weight:bold; color:{NAVY_700};">Learn more &rarr;</a>
          </td></tr>
        </table>
        """

    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER};">
    <tr><td align="center" style="padding:24px 16px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px; background:#ffffff; border-radius:6px; overflow:hidden;">

      <!-- Header -->
      <tr><td style="background:{NAVY_900}; padding:18px 20px;">
        <table role="presentation" cellpadding="0" cellspacing="0"><tr>
          <td style="padding-right:9px; vertical-align:middle;">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <rect x="2.5" y="8" width="19" height="12.5" rx="1.8" stroke="#ffffff" stroke-width="1.6"/>
              <path d="M8.5 8V6.3C8.5 5.1 9.5 4 10.8 4H13.2C14.5 4 15.5 5.1 15.5 6.3V8" stroke="#ffffff" stroke-width="1.6" stroke-linecap="round"/>
              <rect x="10.3" y="12.5" width="3.4" height="2.4" rx="0.5" fill="#ffffff"/>
            </svg>
          </td>
          <td style="font-family:Georgia,'Times New Roman',serif; font-size:19px; color:#ffffff; vertical-align:middle;">
            The Portfolio Briefcase
          </td>
        </tr></table>
      </td></tr>

      <!-- Body -->
      <tr><td style="padding:22px 20px;">
        <div style="font-family:Georgia,'Times New Roman',serif; font-size:20px; color:{NAVY_900}; margin-bottom:2px;">
          Your daily portfolio update
        </div>
        <div style="font-family:Arial,sans-serif; font-size:12px; color:#999; margin-bottom:14px;">
          Prices as of {timestamp}
        </div>
        <div style="font-family:Arial,sans-serif; font-size:14px; color:#333; margin-bottom:16px;">
          Hi {name}, here's what's happening with your stocks today.
        </div>

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          {sections_html}
        </table>

        {sponsor_html}

        <div style="text-align:center; margin:20px 0 4px;">
          <a href="{SITE_BASE}/index.html" style="display:inline-block; background:{NAVY_700}; color:#ffffff; font-family:Arial,sans-serif; font-size:14px; font-weight:bold; text-decoration:none; padding:11px 22px; border-radius:4px;">
            View your dashboard
          </a>
        </div>

        <!-- Disclaimers -->
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px; padding-top:14px; border-top:1px solid {BORDER};">
          <tr><td>
            <p style="{disclaimer_style}"><strong>Educational content only.</strong> This newsletter shares general market information and historical context — it is not financial advice, and nothing here is a recommendation to buy, sell, or hold any investment.</p>
            <p style="{disclaimer_style}"><strong>AI-generated content.</strong> The write-ups above are created using AI and may contain mistakes or inaccuracies. Always verify important details yourself before making any decisions about your portfolio.</p>
            <p style="{disclaimer_style}"><strong>Data accuracy.</strong> Price and market data is provided by third-party sources and may be delayed, incomplete, or occasionally inaccurate.</p>
            <p style="{disclaimer_style}"><strong>Past performance.</strong> Historical patterns referenced in this newsletter are not indicative of future results.</p>
            <p style="{disclaimer_style}"><strong>No advisory relationship.</strong> The Portfolio Briefcase is not a registered investment advisor and does not act in a fiduciary capacity for subscribers.</p>
            <p style="{disclaimer_style}">Can't find a ticker, or does something look off — missing news, an unclear price, or anything else that doesn't seem right? Email us at <a href="mailto:contact@theportfoliobriefcase.com" style="color:{NAVY_700};">contact@theportfoliobriefcase.com</a> and we'll look into adding it.</p>
            <p style="font-family:Arial,sans-serif; font-size:12px; margin:0 0 10px;">
              <a href="{SITE_BASE}/edit-portfolio.html" style="color:{NAVY_700};">Edit portfolio</a> &nbsp;|&nbsp;
              <a href="{SITE_BASE}/unsubscribe.html" style="color:{NAVY_700};">Unsubscribe</a> &nbsp;|&nbsp;
              <a href="{SITE_BASE}/privacy-policy.html" style="color:{NAVY_700};">Privacy Policy</a> &nbsp;|&nbsp;
              <a href="{SITE_BASE}/terms.html" style="color:{NAVY_700};">Terms of Service</a>
            </p>
            <p style="font-family:Arial,sans-serif; font-size:11px; color:#aaa; margin:0 0 10px;">
              [Your business mailing address here — required by the CAN-SPAM Act for commercial email]
            </p>
            <p style="text-align:center; font-family:Arial,sans-serif; font-size:12px; color:{MUTED}; margin:0;">
              <a href="{SITE_BASE}/referrals.html" style="color:{MUTED}; text-decoration:underline;">Your Referrals: <strong style="color:{NAVY_900};">{referral_count}</strong></a>
            </p>
          </td></tr>
        </table>
      </td></tr>

    </table>
    </td></tr>
    </table>
    """


def send_email(to_email, subject, html_body):
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": BREVO_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "sender": {"name": SENDER_NAME, "email": EMAIL_ADDRESS},
            "to": [{"email": to_email}],
            "subject": subject,
            "htmlContent": html_body,
            # Tagged so Brevo's stats can be filtered to newsletter sends only,
            # separate from login-code and feedback emails sent from app.py —
            # otherwise open/click rates would be diluted by unrelated mail.
            "tags": ["newsletter"],
        },
        timeout=15,
    )
    if resp.status_code >= 300:
        raise Exception(f"Brevo API error {resp.status_code}: {resp.text}")


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
    sponsor = get_active_sponsor()

    # Cache stock snapshots/blurbs so we don't re-fetch/re-generate per subscriber
    # if multiple people hold the same stock.
    cache = {}

    # Brevo's API is stateless HTTPS — no persistent connection to open/hold
    # like the old SMTP approach, each send is just its own request.
    for sub in subscribers:
        name = sub.get("Name", "there")
        email = sub.get("Email")
        portfolio = [t.strip().upper() for t in sub.get("Portfolio", "").split(",") if t.strip()]
        referral_count_raw = sub.get("ReferralCount", 0)
        referral_count = int(referral_count_raw) if str(referral_count_raw).strip().isdigit() else 0

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

        html = build_email_html(name, stock_sections, sponsor=sponsor, referral_count=referral_count)
        try:
            send_email(email, "Your daily portfolio update — The Portfolio Briefcase", html)
            print(f"Sent to {email}")
        except Exception as e:
            print(f"Failed to send to {email}: {e}")


if __name__ == "__main__":
    main()
