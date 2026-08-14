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


def get_general_market_news(limit=6):
    """Pulls Finnhub's general market news category (not tied to any single
    ticker) — broad economic/world stories that could move markets overall.
    Returns a list of headline strings, most recent first. Free tier, no
    special access needed. Returns [] on failure rather than raising, since
    the overview blurb is a nice-to-have, not something that should ever
    break the newsletter send."""
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category": "general", "token": FINNHUB_API_KEY},
            timeout=20,
        )
        resp.raise_for_status()
        items = resp.json() or []
        return [i.get("headline", "") for i in items[:limit] if i.get("headline")]
    except Exception as e:
        print(f"General market news fetch failed: {e}")
        return []


MARKET_OVERVIEW_INSTRUCTIONS = """You are writing the opening line(s) of a daily investing newsletter. You'll be given a handful of general market/world news headlines and any upcoming Fed events below. Your job is to write a SHORT overview — no title, no headline, just 1-2 plain sentences — summarizing the one or two biggest stories relevant to markets overall today.

RULES:
- 1-2 sentences total. This sits above everyone's personalized stock section, so keep it tight.
- No title, no header, no bullet points — just the sentence(s) themselves.
- Prioritize FORWARD-LOOKING items: an upcoming Fed decision, a scheduled economic report, a pending policy decision, a world event still unfolding — over stories that already fully played out.
- If there's an upcoming Fed rate decision in the data below, it's fine to mention it plainly (date only, no prediction of the outcome).
- Write in plain, everyday language — no jargon, no textbook tone. Explain any necessary term briefly, right there.
- Do NOT use the words "buy," "sell," "hold," or any action-nudging phrases ("good time to," "worth considering," etc.).
- Do NOT predict which way any market or price will move.
- Do NOT invent a story that isn't reflected in the headlines below — only summarize what's genuinely there.
- If the headlines are mostly noise with nothing genuinely notable, write a short neutral line noting markets are digesting mixed news — don't force drama that isn't there.

Call the submit_overview tool with your result."""


def generate_market_overview(headlines, macro_events=None):
    """One Claude call per newsletter run (not per subscriber) producing a
    short, shared 'what's moving markets today' line. Returns a plain string,
    or None if generation fails — the newsletter should never block on this."""
    if not headlines and not macro_events:
        return None

    macro_line = ""
    if macro_events:
        events_desc = "; ".join(
            f"{e.get('event', 'Economic release')} on {str(e.get('time', ''))[:10]}"
            for e in macro_events
        )
        macro_line = f"Upcoming macro events: {events_desc}."

    headlines_block = "\n".join(f"- {h}" for h in headlines) or "No major headlines available."

    dynamic_data = f"""Today's general market headlines:
{headlines_block}
{macro_line}"""

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            tools=[{
                "name": "submit_overview",
                "description": "Submit the 1-2 sentence market overview.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "overview": {
                            "type": "string",
                            "description": "1-2 plain sentences, no title, following the rules above.",
                        },
                    },
                    "required": ["overview"],
                },
            }],
            tool_choice={"type": "tool", "name": "submit_overview"},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": MARKET_OVERVIEW_INSTRUCTIONS, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": dynamic_data},
                ],
            }]
        )
        tool_use_block = next((b for b in response.content if b.type == "tool_use"), None)
        if not tool_use_block:
            return None
        overview = str(tool_use_block.input.get("overview", "")).strip()
        if not overview:
            return None

        # Same forbidden-phrase safety net as the per-ticker blurbs.
        if any(re.search(r'\b' + re.escape(p) + r'\b', overview.lower()) for p in FORBIDDEN_PHRASES):
            print("WARNING: market overview contained flagged language, omitting it")
            return None

        return overview
    except Exception as e:
        print(f"Market overview generation failed: {e}")
        return None



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


def get_recent_headline(ticker, days=3):
    """Pulls the single most recent company-news headline for a ticker.
    Shared by get_stock_snapshot (for the ticker itself) and the ETF
    top-holdings lookup below (for its biggest constituents)."""
    try:
        from datetime import date, timedelta
        today = date.today()
        resp = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": ticker,
                "from": (today - timedelta(days=days)).isoformat(),
                "to": today.isoformat(),
                "token": FINNHUB_API_KEY,
            },
            timeout=20,
        )
        if resp.ok:
            items = resp.json()
            if items:
                return items[0].get("headline")
    except Exception as e:
        print(f"Headline fetch failed for {ticker}: {e}")
    return None


def _price_performance_from_closes(dated_closes, current_price, today):
    """Shared math for both data sources below — takes a list of (date, close)
    tuples and computes 5d/1m/YTD % change from current_price."""
    from datetime import timedelta, date as date_cls

    def closest_close_on_or_before(target_date):
        candidates = [c for d, c in dated_closes if d <= target_date]
        return candidates[-1] if candidates else None

    def closest_close_on_or_after(target_date):
        candidates = [c for d, c in dated_closes if d >= target_date]
        return candidates[0] if candidates else None

    result = {}
    close_5d = closest_close_on_or_before(today - timedelta(days=7))
    if close_5d:
        result["5d"] = round((current_price - close_5d) / close_5d * 100, 1)

    close_1m = closest_close_on_or_before(today - timedelta(days=30))
    if close_1m:
        result["1m"] = round((current_price - close_1m) / close_1m * 100, 1)

    close_ytd = closest_close_on_or_after(date_cls(today.year, 1, 1))
    if close_ytd:
        result["ytd"] = round((current_price - close_ytd) / close_ytd * 100, 1)

    return result


def get_price_performance(ticker, current_price):
    """Computes % price change over 5-day, 1-month, and year-to-date windows.
    Three independent sources, tried in order, each only used if the previous
    one fails: yfinance (Yahoo) -> Finnhub candles (requires a paid Finnhub
    plan, 403s on free tier) -> Stooq (free, keyless, different infrastructure
    entirely from the first two). This 3-way fallback exists because in
    practice, Yahoo and Finnhub have both failed in the SAME run before —
    having a third, unrelated source matters. 1-day change is handled
    separately via the quote endpoint's previous-close field.

    Returns a dict like {"5d": -2.3, "1m": 6.1, "ytd": 14.8} — any window that
    can't be computed is simply omitted rather than guessed at."""
    from datetime import date, timedelta, datetime, timezone

    today = date.today()
    start = date(today.year - 1, 1, 1)

    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(start=start.isoformat(), end=(today + timedelta(days=1)).isoformat())
        if not hist.empty:
            dated_closes = [(idx.date(), row["Close"]) for idx, row in hist.iterrows()]
            dated_closes.sort(key=lambda x: x[0])
            return _price_performance_from_closes(dated_closes, current_price, today)
    except Exception as e:
        print(f"yfinance price history lookup failed for {ticker}, trying Finnhub: {e}")

    try:
        from_ts = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        to_ts = int(datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc).timestamp()) + 86400

        resp = requests.get(
            "https://finnhub.io/api/v1/stock/candle",
            params={"symbol": ticker, "resolution": "D", "from": from_ts, "to": to_ts, "token": FINNHUB_API_KEY},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("s") != "ok" or not data.get("c"):
            return _try_stooq_price_performance(ticker, current_price, today, start)

        closes = list(zip(data["t"], data["c"]))
        closes.sort(key=lambda x: x[0])
        dated_closes = [(datetime.fromtimestamp(t, tz=timezone.utc).date(), c) for t, c in closes]
        return _price_performance_from_closes(dated_closes, current_price, today)
    except Exception as e:
        print(f"Finnhub price performance lookup failed for {ticker}, trying Stooq: {e}")
        return _try_stooq_price_performance(ticker, current_price, today, start)


def _try_stooq_price_performance(ticker, current_price, today, start):
    """Third and last fallback: Stooq's free, keyless daily-history CSV. Not
    affiliated with Yahoo or Finnhub — different infrastructure entirely, so
    it's unlikely to fail at the same time those two do (as happened in
    practice: Yahoo returned empty history AND Finnhub 403'd in the same run).
    Only works for plain US-listed tickers, not crypto (colon-prefixed)."""
    if ":" in ticker:
        return {}
    try:
        resp = requests.get(
            "https://stooq.com/q/d/l/",
            params={"s": f"{ticker.lower()}.us", "d1": start.strftime("%Y%m%d"), "d2": today.strftime("%Y%m%d"), "i": "d"},
            timeout=20,
        )
        resp.raise_for_status()
        lines = resp.text.strip().splitlines()
        if len(lines) < 2 or not lines[0].startswith("Date"):
            return {}
        from datetime import date as date_cls
        dated_closes = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) >= 5:
                try:
                    dated_closes.append((date_cls.fromisoformat(parts[0]), float(parts[4])))
                except ValueError:
                    continue
        if not dated_closes:
            return {}
        dated_closes.sort(key=lambda x: x[0])
        return _price_performance_from_closes(dated_closes, current_price, today)
    except Exception as e:
        print(f"Stooq price performance lookup also failed for {ticker}: {e}")
        return {}


def get_key_metrics(ticker):
    """Pulls a curated handful of hard fundamental numbers from Finnhub's free
    basic-financials endpoint (P/E, revenue/EPS growth, margins, dividend
    yield). Finnhub's exact field names have shifted over time, so each metric
    tries a couple of known key variants and takes whichever is present.
    Returns a dict of only the metrics that actually came back — never
    fabricated. Only meaningful for individual companies, not funds/crypto."""
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/metric",
            params={"symbol": ticker, "metric": "all", "token": FINNHUB_API_KEY},
            timeout=20,
        )
        resp.raise_for_status()
        m = resp.json().get("metric", {}) or {}

        def pick(*keys):
            for k in keys:
                v = m.get(k)
                if v is not None:
                    return v
            return None

        raw = {
            "P/E (trailing)": pick("peTTM", "peBasicExclExtraTTM", "peExclExtraTTM"),
            "P/E (forward)": pick("peForward", "forwardPE", "peNormalizedAnnual"),
            "PEG ratio": pick("pegRatio", "pegRatioTTM"),
            "Price/Sales": pick("psTTM", "psAnnual", "priceToSalesTTM"),
            "Revenue growth YoY": pick("revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy"),
            "EPS growth YoY": pick("epsGrowthTTMYoy", "epsGrowthQuarterlyYoy"),
            "Gross margin": pick("grossMarginTTM", "grossMarginAnnual"),
            "Net margin": pick("netProfitMarginTTM", "netProfitMarginAnnual"),
            "Debt/Equity": pick("totalDebt/totalEquityAnnual", "totalDebt/totalEquityQuarterly", "longTermDebt/equityAnnual"),
            "Dividend yield": pick("dividendYieldIndicatedAnnual", "currentDividendYieldTTM"),
        }
        return {k: v for k, v in raw.items() if v is not None}
    except Exception as e:
        print(f"Key metrics lookup failed for {ticker}: {e}")
        return {}


def get_peer_comparison(ticker):
    """Real (not fabricated) peer/industry-average context for valuation
    ratios. Uses Finnhub's free /stock/peers endpoint to get same-industry
    tickers, then averages P/E and Price/Sales across up to 2 of them —
    capped deliberately low since each peer costs a full extra metrics call,
    and this already runs on top of a fairly heavy per-ticker call budget.
    Returns {"peers": ["MSFT","GOOGL"], "avg_pe": 28.4, "avg_ps": 7.1} with
    only the ratios that actually came back — never fabricated. Returns {}
    if peers aren't available or none had usable data."""
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/peers",
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=20,
        )
        resp.raise_for_status()
        peers = [p for p in resp.json() if p and p.upper() != ticker.upper()][:2]
        if not peers:
            return {}

        pes, pss, used = [], [], []
        for p in peers:
            m = get_key_metrics(p)
            pe = m.get("P/E (trailing)")
            ps = m.get("Price/Sales")
            if pe is not None or ps is not None:
                used.append(p)
            if pe is not None:
                pes.append(pe)
            if ps is not None:
                pss.append(ps)

        if not used:
            return {}
        result = {"peers": used}
        if pes:
            result["avg_pe"] = round(sum(pes) / len(pes), 1)
        if pss:
            result["avg_ps"] = round(sum(pss) / len(pss), 1)
        return result
    except Exception as e:
        print(f"Peer comparison lookup failed for {ticker}: {e}")
        return {}


def _dividend_estimate_from_history(ticker):
    """FALLBACK ONLY. Finnhub's dividend endpoint returns historical payments,
    not confirmed future ones — this ESTIMATES the next ex-dividend date by
    taking the most recent payment and adding ~1 quarter. Only used if the
    live yfinance lookup below fails. Returns {"estimated_date": "...",
    "based_on": "..."} or None."""
    from datetime import date, timedelta
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/dividend",
            params={
                "symbol": ticker,
                "from": (date.today() - timedelta(days=200)).isoformat(),
                "to": date.today().isoformat(),
                "token": FINNHUB_API_KEY,
            },
            timeout=20,
        )
        if not resp.ok:
            return None
        items = resp.json()
        if not items:
            return None
        items.sort(key=lambda x: x.get("date", ""), reverse=True)
        last_date = items[0].get("date")
        if not last_date:
            return None
        est = (date.fromisoformat(last_date) + timedelta(days=91)).isoformat()
        return {"estimated_date": est, "based_on": last_date}
    except Exception as e:
        print(f"Dividend estimate fallback failed for {ticker}: {e}")
        return None


def get_dividend_info(ticker):
    """Tries to get the REAL, company-declared next ex-dividend date via
    yfinance (reads Yahoo Finance's tracked calendar data — free, no API key,
    but unofficial/scraped so it can occasionally fail or lag). Falls back to
    a heuristic estimate (last payment + ~1 quarter) if that doesn't work.
    Returns {"date": "...", "confirmed": True/False, "based_on": "..." (only
    when not confirmed)} or None if nothing is available either way."""
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar
        if cal:
            ex_div = cal.get("Ex-Dividend Date") or cal.get("Dividend Date")
            if ex_div:
                ex_div_str = ex_div.isoformat() if hasattr(ex_div, "isoformat") else str(ex_div)
                return {"date": ex_div_str, "confirmed": True}
    except Exception as e:
        print(f"yfinance dividend lookup failed for {ticker}, falling back: {e}")

    fallback = _dividend_estimate_from_history(ticker)
    if fallback:
        return {"date": fallback["estimated_date"], "confirmed": False, "based_on": fallback["based_on"]}
    return None


# FALLBACK ONLY — approximate top holdings for common funds, hand-maintained
# and will drift out of date. Only used if the live yfinance lookup below
# fails (e.g. Yahoo rate-limits the request). Revisit every few months.
ETF_TOP_HOLDINGS_FALLBACK = {
    "VOO": ["AAPL", "MSFT", "NVDA"], "SPY": ["AAPL", "MSFT", "NVDA"],
    "IVV": ["AAPL", "MSFT", "NVDA"], "VTI": ["AAPL", "MSFT", "NVDA"],
    "QQQ": ["AAPL", "MSFT", "NVDA"], "TQQQ": ["AAPL", "MSFT", "NVDA"], "SQQQ": ["AAPL", "MSFT", "NVDA"],
    "DIA": ["UNH", "GS", "MSFT"],
    "XLK": ["AAPL", "MSFT", "NVDA"], "XLF": ["JPM", "V", "MA"], "XLE": ["XOM", "CVX", "COP"],
    "ARKK": ["TSLA", "COIN", "ROKU"],
    "SCHD": ["ABBV", "PEP", "HD"], "VYM": ["JPM", "XOM", "JNJ"],
    "VUG": ["AAPL", "MSFT", "NVDA"], "VTV": ["JPM", "XOM", "JNJ"],
    "UPRO": ["AAPL", "MSFT", "NVDA"], "SPXL": ["AAPL", "MSFT", "NVDA"], "SPXS": ["AAPL", "MSFT", "NVDA"],
}


def get_spdr_holdings(ticker, top_n=3):
    """Official, authoritative holdings straight from State Street's own site
    — no API key, no rate limit (it's just a public XLSX file they're required
    to publish daily). Covers SPY/DIA/XLK/XLF/XLE and other SPDR funds. The
    exact header row position isn't hardcoded — this scans for the row
    containing "Ticker" as a column header, since issuer file layouts shift
    occasionally and hardcoding a skiprows count is fragile."""
    try:
        import pandas as pd
        import io
        url = f"https://www.ssga.com/us/en/institutional/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{ticker.lower()}.xlsx"
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()

        raw = pd.read_excel(io.BytesIO(resp.content), header=None)
        header_row = None
        for i, row in raw.iterrows():
            if row.astype(str).str.strip().str.lower().eq("ticker").any():
                header_row = i
                break
        if header_row is None:
            return None

        df = pd.read_excel(io.BytesIO(resp.content), header=header_row)
        df.columns = [str(c).strip() for c in df.columns]
        ticker_col = next((c for c in df.columns if c.lower() == "ticker"), None)
        weight_col = next((c for c in df.columns if "weight" in c.lower()), None)
        if not ticker_col:
            return None
        if weight_col:
            df[weight_col] = pd.to_numeric(df[weight_col], errors="coerce")
            df = df.sort_values(weight_col, ascending=False)
        symbols = [str(t).strip() for t in df[ticker_col].tolist() if isinstance(t, str) and str(t).strip().isalpha()]
        return symbols[:top_n] if symbols else None
    except Exception as e:
        print(f"SPDR official holdings lookup failed for {ticker}: {e}")
        return None


# Confirmed iShares product IDs (the numeric ID in the fund's ishares.com URL
# — visible right in the browser address bar on that fund's page, e.g.
# ishares.com/us/products/{ID}/{fund-name}). Add more here as needed; this
# only covers what's been verified. Not every iShares fund is worth adding —
# only ones you actually hold.
ISHARES_HOLDINGS_URLS = {
    "IVV": "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund",
    "IWM": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
}


def get_ishares_holdings(ticker, top_n=3):
    """Official iShares (BlackRock) holdings CSV — same idea as the SPDR
    fetch above: a public file they're required to publish, no key, no rate
    limit. Only covers tickers in ISHARES_HOLDINGS_URLS above."""
    url = ISHARES_HOLDINGS_URLS.get(ticker)
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        lines = resp.text.splitlines()
        header_idx = next((i for i, l in enumerate(lines) if l.strip().startswith("Ticker,")), None)
        if header_idx is None:
            return None

        import pandas as pd
        import io
        df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
        df.columns = [str(c).strip() for c in df.columns]
        weight_col = next((c for c in df.columns if "weight" in c.lower()), None)
        if weight_col:
            df[weight_col] = pd.to_numeric(df[weight_col], errors="coerce")
            df = df.sort_values(weight_col, ascending=False)
        symbols = [str(t).strip() for t in df["Ticker"].tolist() if isinstance(t, str) and str(t).strip().isalpha()]
        return symbols[:top_n] if symbols else None
    except Exception as e:
        print(f"iShares official holdings lookup failed for {ticker}: {e}")
        return None


# SPDR funds with a confirmed working ssga.com file naming pattern.
SPDR_TICKERS = {"SPY", "DIA", "XLK", "XLF", "XLE"}

# Leveraged/inverse funds don't hold real company stock (they hold swaps and
# futures) — their own issuer filing wouldn't give useful ticker-level news
# anyway. Point these at the plain index fund they track instead, so they get
# real, meaningful top-holdings news rather than a swap counterparty name.
LEVERAGED_FUND_UNDERLYING = {
    "TQQQ": "QQQ", "SQQQ": "QQQ",
    "UPRO": "SPY", "SPXL": "SPY", "SPXS": "SPY",
}

# Broader than the mapping above — includes leveraged funds with no clean
# single-index underlying to redirect to (SOXL/SOXS track a semiconductor
# index, TMF/TMV track long-duration treasuries). Used only to flag these in
# the email as "(leveraged)" so readers don't mistake them for a plain index
# fund — these carry daily-reset compounding risk a normal ETF doesn't.
LEVERAGED_TICKERS = set(LEVERAGED_FUND_UNDERLYING.keys()) | {"SOXL", "SOXS", "TMF", "TMV"}


def get_live_etf_holdings(ticker, top_n=3):
    """Real top holdings via yfinance's funds_data (reads Yahoo Finance's
    tracked fund composition — free, no key, updated regularly, but unofficial
    so treat failures as expected/normal, not bugs). Returns a list of ticker
    symbols sorted by weight, or None if unavailable (caller falls back to
    the hardcoded table)."""
    try:
        import yfinance as yf
        holdings_df = yf.Ticker(ticker).funds_data.top_holdings
        if holdings_df is None or holdings_df.empty:
            return None
        weight_col = next(
            (c for c in holdings_df.columns if "percent" in c.lower() or "weight" in c.lower()),
            None
        )
        if weight_col:
            holdings_df = holdings_df.sort_values(weight_col, ascending=False)
        return list(holdings_df.index[:top_n])
    except Exception as e:
        print(f"yfinance holdings lookup failed for {ticker}, falling back: {e}")
        return None


def get_holdings_news(ticker):
    """Pulls one recent headline per top holding of a fund. Priority order,
    each falling through to the next only on failure:
      1. Leveraged/inverse funds -> redirect to their underlying index fund.
      2. Official issuer CSV/XLSX (SPDR, then iShares) — authoritative, free,
         no rate-limit risk, since it's a public file each issuer must publish.
      3. yfinance (Yahoo Finance's tracked data) — real, free, no key, but
         unofficial and can occasionally be rate-limited.
      4. Hand-maintained hardcoded table — last resort, keeps things working
         even if every live source is down.
    Returns a list of {"ticker", "headline"} for holdings that had news."""
    lookup_ticker = LEVERAGED_FUND_UNDERLYING.get(ticker, ticker)

    holdings = None
    if lookup_ticker in SPDR_TICKERS:
        holdings = get_spdr_holdings(lookup_ticker)
    if not holdings and lookup_ticker in ISHARES_HOLDINGS_URLS:
        holdings = get_ishares_holdings(lookup_ticker)
    if not holdings:
        holdings = get_live_etf_holdings(lookup_ticker)
    if not holdings:
        holdings = ETF_TOP_HOLDINGS_FALLBACK.get(lookup_ticker, [])

    results = []
    for h in holdings:
        headline = get_recent_headline(h, days=3)
        if headline:
            results.append({"ticker": h, "headline": headline})
    return results


def get_fund_info(ticker):
    """Gets the fund's REAL category, full name, and Yahoo's own asset-type
    classification (quoteType) — so the newsletter doesn't wrongly assume
    every ticker in KNOWN_FUNDS is a broad-market index fund, AND so a fund
    that's MISSING from KNOWN_FUNDS (a hand-maintained list — it will miss
    things, as it already did for one ticker) still gets correctly identified
    as a fund rather than treated like an individual company. Tries
    yfinance's category/quoteType fields first; some niche/leveraged tickers
    lack that data on Yahoo entirely (this fails normally, not a bug), so it
    falls back to just the fund's official full name from Finnhub's profile
    endpoint — often the name alone reveals the focus (e.g. "Amplify
    Cybersecurity ETF" makes it obvious even with no formal category field).
    Returns {"category": ..., "name": ..., "quote_type": ...} (any may be
    None) or None if nothing came back from either source."""
    category, name, quote_type = None, None, None
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        category = info.get("category")
        name = info.get("longName") or info.get("shortName")
        quote_type = info.get("quoteType")  # "ETF", "MUTUALFUND", "EQUITY", etc.
    except Exception as e:
        print(f"yfinance fund info lookup failed for {ticker}, trying Finnhub: {e}")

    if not name:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/stock/profile2",
                params={"symbol": ticker, "token": FINNHUB_API_KEY},
                timeout=20,
            )
            if resp.ok:
                name = resp.json().get("name") or name
        except Exception as e:
            print(f"Finnhub profile lookup failed for {ticker}: {e}")

    if not (category or name or quote_type):
        return None
    return {"category": category, "name": name, "quote_type": quote_type}


def is_fund_ticker(ticker, fund_info=None):
    """KNOWN_FUNDS is hand-maintained and WILL occasionally miss a fund (it
    already has, silently, before this check existed) — this treats it as a
    starting guess, not the final word. If Yahoo's own quoteType data says
    "ETF" or "MUTUALFUND", that overrides the guess even if the ticker isn't
    in the hardcoded set at all. Falls back to the set alone if quote_type
    data isn't available for this ticker."""
    if fund_info and fund_info.get("quote_type") in ("ETF", "MUTUALFUND"):
        return True
    return ticker in KNOWN_FUNDS


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

            headline = get_recent_headline(ticker, days=3)
            is_crypto = ":" in ticker
            # Fetched for every non-crypto ticker (not just ones already in
            # KNOWN_FUNDS) so a fund that's missing from that hardcoded set
            # still gets correctly identified rather than treated like a stock.
            fund_info = None if is_crypto else get_fund_info(ticker)
            is_fund = False if is_crypto else is_fund_ticker(ticker, fund_info)

            return {
                "ticker": ticker,
                "price": round(current_price, 2),
                "pct_change": round(pct_change, 2),
                "headline": headline,
                "is_fund": is_fund,
                "is_crypto": is_crypto,
                "earnings": None if is_fund or is_crypto else get_upcoming_earnings(ticker),
                "macro_events": (macro_events or []) if is_fund else [],
                "sec_filing": None if is_fund or is_crypto else get_sec_filings(ticker),
                "price_performance": get_price_performance(ticker, current_price),
                "key_metrics": {} if is_fund or is_crypto else get_key_metrics(ticker),
                "peer_comparison": {} if is_fund or is_crypto else get_peer_comparison(ticker),
                "dividend_info": None if is_crypto else get_dividend_info(ticker),
                "holdings_news": get_holdings_news(ticker) if is_fund else [],
                "fund_info": fund_info if is_fund else None,
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
    "NLR",
}

# Rotated through so consecutive blurbs don't all lean on the same opening
# word/structure (e.g. everything starting with "Historically...").
STYLE_HINTS = [
    "Open with the upcoming date/event if there is one, then build into the hypothesis.",
    "Open with the valuation-vs-growth picture, then connect it to what's coming up.",
    "Open with the news/headline angle, then connect it to the longer-term hypothesis.",
    "Open directly with the hypothesis/question worth watching, then back it with the specifics.",
]

# This block is IDENTICAL on every single call, regardless of ticker, asset
# type, or style variation — that's deliberate, so it can be marked
# cacheable. Ticker-specific data (price, headline, earnings, asset type,
# style hint) all live in the per-call dynamic block below instead, so they
# never invalidate the cache. Anthropic's prompt caching charges a ~25%
# premium on the first call that writes the cache, but only ~10% of normal
# input price on every subsequent call within the cache window (5 min) that
# reuses it — a net win as soon as a run generates more than one blurb.
STATIC_BLURB_INSTRUCTIONS = """You are writing content for an educational investing newsletter aimed at people who already own this holding FOR THE LONG TERM. Assume the reader already knows the basics; they want to understand what actually matters for this holding going forward — not a recap of what the price did yesterday.

Produce TWO things:

1. A short, catchy, punny/playful headline (max 8 words) tied to the specific news/data below (e.g.
   for a rocket company with a pending launch: "SpaceX Counts Down to Launch Day"). Must NOT imply
   the reader should buy, sell, or take any action, and must NOT center on a daily price swing.

2. A set of 3-5 bullet points, built around ONE THROUGHLINE: a forward-looking investment hypothesis
   — a specific thing worth watching over the coming weeks/quarters, and why. This is the single most
   important thing about this newsletter: readers already get today's price everywhere else. What
   they can't get elsewhere is "here's the specific thing that will tell you if the long-term story is
   still intact." Every set of bullets should build toward that, in different ways for different
   tickers so it doesn't read like a template. Bullets can be ONE or TWO sentences each — every
   sentence must contain a concrete, specific fact: a date, a percentage, a dollar figure, a named
   event, a named product, a named executive, or a specific metric. No vague filler.

   PRIORITIZE FUNDAMENTALS OVER DAILY NOISE. Rank what you draw from roughly in this order of
   importance — fundamentals should dominate the bullets, short-term noise should be a light garnish
   at most, never the lead:

   TIER 1 — FUNDAMENTAL SIGNALS (this should be most of the content):
   - Earnings date/timing, and what specifically to watch for at that report if the data suggests
     something (e.g. a metric that's been trending a certain way).
   - Valuation paired with growth — THIS PAIRING MATTERS: whenever you cite a growth number (revenue
     growth, EPS growth), pair it with a valuation ratio given below (P/E, PEG, Price/Sales) so the
     reader sees growth in context, not in isolation. E.g. "Revenue grew 12% YoY, and at a forward P/E
     of 24 the stock isn't pricing in much more than that continuing." If peer/industry comparison
     data is given, you can use it to add context (e.g. "...compared to peers averaging closer to
     19x") — but don't force a peer comparison into every single bullet; use it when it's genuinely
     the most useful framing, not as a rigid formula every time.
   - Debt/balance-sheet changes, if given, and margin trends (gross/net) — tie to what it implies
     going forward (e.g. rising debt/equity worth watching, or margin trend suggesting pricing power).
   - SEC filings — say plainly what type and its date.
   - Management guidance shifts — if the headline mentions a change in outlook, guidance, or strategy,
     this is high-value; get specific about what changed.
   - Dividend date — CONFIRMED dates stated as fact; ESTIMATED ones always hedged ("roughly estimated
     around...").
   - FOR FUNDS/ETFs — news on top individual holdings (given below) is your best fundamental content,
     since "the market was mixed" tells a reader nothing. Use it like you would company news.

   TIER 2 — MACRO/SENTIMENT CONTEXT (use briefly, as supporting color, not the main point):
   - Fed decisions, CPI/inflation prints, short-term rate expectations, sector rotation. These are
     legitimate to mention — especially for funds — but keep them in a supporting role. Do not let a
     macro/sentiment bullet be the most prominent one, and do not frame short-term rate or sentiment
     noise as more decision-relevant than the Tier 1 fundamentals above.

   PRICE MOVEMENT — de-emphasize this deliberately:
   - Do NOT lead with or headline a single day's price move — daily swings create noise/panic/excitement
     that isn't useful for a long-term holder, and this newsletter is explicitly trying not to
     encourage reacting to them.
   - If you mention price performance at all, prefer the longer window (1-month or YTD) over the
     1-day number, and only include it as brief supporting context for a fundamental point — never as
     a standalone bullet about "the stock moved X% today."

   THE HYPOTHESIS ITSELF — at least one bullet must explicitly frame something forward-looking to
   watch, phrased as a question or a specific marker a long-term holder should track — NOT a
   prediction of where the price goes. Vary the phrasing every time so it doesn't become a template
   (do not write "watch whether X holds steady" as a formula on every ticker — find a different way
   to say it each time). If an exact date is available for the event this hypothesis hangs on (the
   earnings date given below, for example), USE THE EXACT DATE in this bullet too, not a vague
   reference to it — e.g.: "The real test on August 14 is whether ad revenue growth can offset
   slowing subscriber additions." / "Keep an eye on whether the next guidance update — expected
   alongside the September 16 Fed decision's market reaction — narrows or widens that margin
   outlook." If no exact date applies to this particular hypothesis, phrase it without inventing a
   timeframe: "The open question here: can pricing power offset the input-cost pressure flagged in
   the headline below" — not "next quarter" or "soon" when you don't actually have that date.

   Only include categories that actually have data — never write a bullet noting the ABSENCE of
   something. Skip silently instead.

LEVERAGED FUNDS — if the data below flags this ticker as leveraged/inverse, you do not need to
explain leverage mechanics (a small warning tag is already shown next to the ticker in the email) —
but do NOT frame it with the same long-term "hold and watch fundamentals" lens as a plain index fund,
since leveraged/inverse products are structurally built for short-term use and decay over time from
daily rebalancing. A brief, neutral acknowledgment of that is fine; don't lecture.

CRITICAL — DO NOT HALLUCINATE NUMBERS:
- Every specific number, percentage, date, or figure you state MUST come directly from the data
  block below. Do NOT invent analyst estimates, consensus expectations, growth targets, industry
  averages, or any number that isn't explicitly provided to you.
- If you want to describe an expectation or target, you may ONLY do this if it's explicitly present
  in the headline text or data below — quote/paraphrase what's reported, don't estimate your own figure.
- If you don't have a specific number for something, describe it qualitatively instead rather than
  making one up. Getting a number wrong is worse than being general — when in doubt, omit it.

READABILITY — still written for everyday personal investors, not finance professionals:
- Explain any jargon in plain words right where you use it (e.g. "PEG ratio — P/E divided by growth
  rate, a way of checking whether a high P/E is justified by how fast the company is actually growing").
- Write like a sharp, knowledgeable friend explaining what actually matters, not a press release.

STRICT RULES — non-negotiable, for BOTH the headline and bullets:
- Do NOT use the words "buy," "sell," "hold," or any variation telling the reader what to do.
- Do NOT recommend, suggest, or imply any action the reader should take.
- Do NOT say things like "good time to," "bad time to," "worth considering," or similar action-nudging phrases.
- Do NOT predict future price direction ("will likely rise/fall") — a hypothesis bullet raises what
  to watch, never what will happen to the price as a result.
- Do not invent a catalyst, filing, event, ratio, or number that isn't actually in the data below.
- Do not add a disclaimer sentence — one is added separately in the email template.
- USE EXACT DATES, EVERY TIME, wherever one is available in the data below — never substitute vague
  phrases like "next quarter," "next earnings call," "later this year," "this fall," or "soon" once
  you already have the actual date. This applies everywhere in the bullets, not just the first time
  an event is mentioned — if the hypothesis bullet references the same earnings report or Fed
  decision as an earlier bullet, restate the exact date there too rather than referring back to it
  vaguely. If NO exact date exists for something, don't invent a timeframe — phrase it without one.

Vary structure and phrasing across different tickers so this doesn't read like a repeated template —
this applies especially to the hypothesis bullet, which is the easiest place to fall into a formula.
Avoid starting with the word "Historically."

Call the submit_blurb tool with your headline and bullets."""


def generate_blurb(snapshot, variation_index=0):
    """Ask Claude for a catchy headline + short educational blurb. Never buy/sell language.
    Returns a dict: {"headline": ..., "body": ...}"""
    # Read the already-corrected flags from the snapshot (set in get_stock_snapshot,
    # which self-corrects KNOWN_FUNDS misses using real quoteType data) rather than
    # re-deriving from the raw hardcoded set here, which would just repeat the bug.
    is_fund = snapshot.get('is_fund', snapshot['ticker'] in KNOWN_FUNDS)
    is_crypto = snapshot.get('is_crypto', ":" in snapshot['ticker'])

    if is_crypto:
        asset_type_note = (
            "This ticker is a cryptocurrency, not a company or fund. Do NOT reference 'earnings reports,' "
            "'shares,' or talk about it as if it were a company or index fund. Discuss it in terms of "
            "crypto market trends, trading activity, or sentiment instead. Cryptocurrency markets trade "
            "24/7 and tend to be more volatile than stocks — you can note this as general context if relevant."
        )
    elif is_fund:
        fund_info = snapshot.get("fund_info")
        if fund_info and (fund_info.get("category") or fund_info.get("name")):
            known_bits = []
            if fund_info.get("name"):
                known_bits.append(f'full name "{fund_info["name"]}"')
            if fund_info.get("category"):
                known_bits.append(f'category "{fund_info["category"]}"')
            known_desc = " and ".join(known_bits)
            asset_type_note = (
                f"This ticker is a fund/ETF, not a single company — it holds many underlying "
                f"positions. Its {known_desc} — USE THIS to correctly identify what kind of fund it "
                f"actually is (e.g. broad-market, sector-specific like cybersecurity/energy/"
                f"semiconductors, thematic, leveraged, bond fund, etc.). Do NOT default to describing "
                f"it as a 'broad market index fund' unless its name/category genuinely says so — a "
                f"cybersecurity fund is not a broad-market fund, for example. Do NOT reference "
                f"'earnings reports' or talk about it as if it were one company. If news on its top "
                f"individual holdings is given below, lean on that heavily — specific company-level "
                f"news is far more useful to a reader than generic sector talk."
            )
        else:
            asset_type_note = (
                "This ticker is a fund/ETF, not a single company. Its specific category/focus could NOT "
                "be determined automatically — do NOT guess or assume it's a broad-market index fund, "
                "since it may well be a niche sector, thematic, or leveraged fund instead. Speak about it "
                "generically as 'this fund' without asserting a specific focus area, unless the ticker "
                "name or headline below makes the focus obvious on its own. Do NOT reference 'earnings "
                "reports' or talk about it as if it were one company."
            )
    else:
        asset_type_note = (
            "This ticker is an individual company's stock. If key financial metrics are given below, use "
            "the one most relevant to what's currently happening — don't just list all of them."
        )

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

    perf_line = ""
    perf = snapshot.get("price_performance") or {}
    # 1-day is included last and de-emphasized in the label itself — the prompt
    # is instructed to prefer the longer windows and avoid leading with this one.
    perf_parts = []
    if "ytd" in perf:
        perf_parts.append(f"YTD: {perf['ytd']:+.1f}%")
    if "1m" in perf:
        perf_parts.append(f"1-month: {perf['1m']:+.1f}%")
    if "5d" in perf:
        perf_parts.append(f"5-day: {perf['5d']:+.1f}%")
    perf_parts.append(f"1-day (de-emphasize this one, it's noise): {snapshot['pct_change']:+.1f}%")
    perf_line = f"Price performance — {', '.join(perf_parts)}."

    metrics_line = ""
    if snapshot.get("key_metrics"):
        metrics_desc = "; ".join(f"{k}: {v}" for k, v in snapshot["key_metrics"].items())
        metrics_line = f"Key financial metrics: {metrics_desc}."

    peer_line = ""
    peer = snapshot.get("peer_comparison") or {}
    if peer.get("peers"):
        peer_bits = []
        if "avg_pe" in peer:
            peer_bits.append(f"avg P/E {peer['avg_pe']}")
        if "avg_ps" in peer:
            peer_bits.append(f"avg Price/Sales {peer['avg_ps']}")
        if peer_bits:
            peer_line = f"Peer/industry comparison (based on {', '.join(peer['peers'])}): {', '.join(peer_bits)}."

    dividend_line = ""
    if snapshot.get("dividend_info"):
        d = snapshot["dividend_info"]
        if d.get("confirmed"):
            dividend_line = f"Dividend: next ex-dividend date is confirmed for {d['date']} (this is a real, company-declared date — state it as fact)."
        else:
            dividend_line = (f"Dividend: last paid on {d['based_on']}; next payment is roughly estimated "
                              f"around {d['date']} based on typical quarterly cadence (NOT a confirmed date — "
                              f"you MUST phrase this as an estimate, e.g. 'roughly estimated around').")

    holdings_line = ""
    if snapshot.get("holdings_news"):
        holdings_desc = " | ".join(f"{h['ticker']}: {h['headline']}" for h in snapshot["holdings_news"])
        holdings_line = f"News on this fund's top individual holdings: {holdings_desc}"

    leveraged_line = ""
    if snapshot['ticker'] in LEVERAGED_TICKERS:
        leveraged_line = ("NOTE: this is a LEVERAGED/INVERSE fund — it resets and compounds daily and "
                           "isn't structurally built for a 'watch these fundamentals long-term' framing "
                           "the way a plain index fund is. Keep that in mind for the hypothesis bullet.")

    dynamic_data = f"""Ticker: {snapshot['ticker']}
{asset_type_note}
{leveraged_line}
Recent headline: {snapshot['headline'] or 'No major headline today'}
{perf_line}
{earnings_line}
{macro_line}
{filing_line}
{metrics_line}
{peer_line}
{dividend_line}
{holdings_line}

STYLE for this one: {style_hint}"""

    fallback = {
        "headline": f"{snapshot['ticker']} Update",
        "bullets": [f"No major forward-looking news found for {snapshot['ticker']} today — check back tomorrow for updates."]
    }

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=550,
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
                            "minItems": 3,
                            "maxItems": 5,
                            "description": "3-5 specific, data-grounded bullets (1-2 sentences each) following the rules above.",
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


def build_email_html(name, stock_sections, sponsor=None, referral_count=0, market_overview=None):
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
        leveraged_tag = (
            ' <span style="color:#a13d2e; font-weight:bold;">(leveraged)</span>'
            if s['ticker'] in LEVERAGED_TICKERS else ""
        )
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
                {display_ticker}{leveraged_tag} &nbsp;·&nbsp; ${s['price']}
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

    overview_html = ""
    if market_overview:
        overview_html = f"""
        <div style="font-family:Arial,sans-serif; font-size:14px; color:#333; line-height:1.6; padding:12px 14px; margin-bottom:16px; border-left:3px solid {NAVY_700}; background:#f6f5f1;">
          {market_overview}
        </div>
        """

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

        {overview_html}

        <div style="font-family:Arial,sans-serif; font-size:14px; color:#333; margin-bottom:16px;">
          Hi {name}, here's what's happening with your stocks today.
        </div>

        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:18px;">
          <tr><td style="background:{NAVY_900}; border-radius:6px; padding:14px 16px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
              <td style="vertical-align:middle;">
                <div style="font-family:Arial,sans-serif; font-size:13px; color:#c9d3e0;">Your referrals</div>
                <div style="font-family:Georgia,'Times New Roman',serif; font-size:22px; color:#ffffff;">{referral_count}</div>
              </td>
              <td style="text-align:right; vertical-align:middle;">
                <a href="{SITE_BASE}/referrals.html" style="display:inline-block; background:#ffffff; color:{NAVY_900}; font-family:Arial,sans-serif; font-size:13px; font-weight:bold; text-decoration:none; padding:9px 16px; border-radius:4px;">
                  Share your link
                </a>
              </td>
            </tr></table>
          </td></tr>
        </table>

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
              <a href="{SITE_BASE}/referrals.html" style="color:{MUTED}; text-decoration:underline;">Manage your referral link</a>
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

    # Same overview blurb goes to every subscriber, so it's generated once for
    # the whole run rather than once per person — saves an API call per subscriber.
    market_headlines = get_general_market_news()
    market_overview = generate_market_overview(market_headlines, macro_events=macro_events)

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

        html = build_email_html(name, stock_sections, sponsor=sponsor, referral_count=referral_count, market_overview=market_overview)
        try:
            send_email(email, "Your daily portfolio update — The Portfolio Briefcase", html)
            print(f"Sent to {email}")
        except Exception as e:
            print(f"Failed to send to {email}: {e}")


if __name__ == "__main__":
    main()
