import os
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import gspread
from google.oauth2.service_account import Credentials
import time
import requests
from google import genai

# --- Config from environment variables (set as GitHub Actions secrets) ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_NAME = os.environ.get("SHEET_NAME", "Subscribers")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


def get_subscribers():
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    rows = sheet.get_all_records()  # expects headers: Name, Email, Portfolio, Status
    return [r for r in rows if str(r.get("Status", "")).lower() == "active"]


def get_stock_snapshot(ticker, max_retries=3):
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
            }
        except Exception as e:
            print(f"Attempt {attempt}/{max_retries} failed for {ticker}: {e}")
            if attempt < max_retries:
                time.sleep(3 * attempt)  # brief backoff before retrying
            else:
                print(f"Giving up on {ticker} after {max_retries} attempts")
                return None


FORBIDDEN_PHRASES = [
    "buy", "sell", "should invest", "you should", "i recommend", "recommendation",
    "good time to", "bad time to", "consider buying", "consider selling",
    "strong buy", "strong sell", "hold", "invest now", "worth investing",
]

# Common broad-market index funds/ETFs — used to hint the model so it doesn't
# talk about "earnings" or "the company" for something that isn't a single company.
KNOWN_FUNDS = {
    "VOO", "SPY", "VTI", "QQQ", "IVV", "VXUS", "VEA", "VWO", "BND", "AGG",
    "ARKK", "DIA", "IWM", "SCHD", "VYM", "VUG", "VTV", "XLK", "XLF", "XLE",
}

# Rotated through so consecutive blurbs don't all lean on the same opening
# word/structure (e.g. everything starting with "Historically...").
STYLE_HINTS = [
    "Open with the price move itself, then add historical context.",
    "Open with the news/headline angle, then connect it to the price move.",
    "Open with a brief historical parallel, then tie it back to today's move.",
    "Open by framing the size of the move (small/moderate/large relative to typical daily swings), then add context.",
]


def generate_blurb(snapshot, variation_index=0):
    """Ask Gemini for a catchy headline + short educational blurb. Never buy/sell language.
    Returns a dict: {"headline": ..., "body": ...}"""
    is_fund = snapshot['ticker'] in KNOWN_FUNDS
    asset_type_note = (
        "This ticker is a broad-market index fund or ETF, not a single company — "
        "it holds many underlying stocks. Do NOT reference 'earnings reports' or "
        "talk about it as if it were one company. Instead, discuss it in terms of "
        "overall market/sector movement."
        if is_fund else
        "This ticker is an individual company's stock."
    )
    style_hint = STYLE_HINTS[variation_index % len(STYLE_HINTS)]

    prompt = f"""You are writing content for an educational investing newsletter, covering one ticker.

Ticker: {snapshot['ticker']}
{asset_type_note}
Current price: ${snapshot['price']}
Change since last close: {snapshot['pct_change']}%
Recent headline: {snapshot['headline'] or 'No major headline today'}

Produce TWO things:
1. A short, catchy, punny/playful headline (max 8 words) related to the news or price move —
   think newspaper-style wordplay tied to the company/fund or what's happening
   (e.g. for a rocket company having a good day: "SpaceX Shoots for the Moon").
   The headline must NOT imply the reader should buy, sell, or take any action.
2. A brief, neutral, educational paragraph (2-3 sentences) about what historically tends to follow
   this kind of move or news, in plain, beginner-friendly language.

STYLE for the paragraph: {style_hint} Avoid starting with the word "Historically" — vary sentence openings
and structure so this doesn't read like a template repeated for every stock. Keep the actual information
(price move, context, historical pattern) the same regardless of phrasing style.

STRICT RULES — these are non-negotiable, for BOTH the headline and paragraph:
- Do NOT use the words "buy," "sell," "hold," or any variation telling the reader what to do.
- Do NOT recommend, suggest, or imply any action the reader should take.
- Do NOT say things like "good time to," "bad time to," "worth considering," or similar action-nudging phrases.
- Only describe historical patterns and context — never advice, opinions, or predictions framed as guidance.
- Do not add a disclaimer sentence — one is added separately in the email template.

Respond ONLY with valid JSON in this exact format, nothing else, no markdown code fences:
{{"headline": "your headline here", "body": "your paragraph here"}}"""

    fallback = {
        "headline": f"{snapshot['ticker']} Update",
        "body": (f"{snapshot['ticker']} moved {snapshot['pct_change']}% today. "
                 f"(Trend note unavailable for this update.)")
    }

    try:
        response = gemini_client.models.generate_content(
            model="gemini-flash-latest",
            contents=prompt
        )
        raw = response.text.strip()
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
        print(f"Gemini call/parse failed for {snapshot['ticker']}: {e}")
        return fallback

    # Second layer of protection: flag (don't silently trust the model)
    combined_lowered = (headline + " " + body).lower()
    if any(phrase in combined_lowered for phrase in FORBIDDEN_PHRASES):
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
        sections_html += f"""
        <div style="margin-bottom:20px; padding:14px; border:1px solid #ddd; border-radius:6px;">
          <h2 style="margin:0 0 4px; font-size:17px; color:#1a1a1a;">{headline}</h2>
          <div style="font-size:13px; color:#555; margin-bottom:8px; font-weight:bold;">
            {s['ticker']} &nbsp;·&nbsp; ${s['price']} &nbsp;<span style="color:#555;">{arrow}</span> {abs(s['pct_change'])}%
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
      <p style="font-size:12px;">
        <a href="https://yourdomain.com/edit-portfolio">Edit portfolio</a> |
        <a href="https://yourdomain.com/unsubscribe">Unsubscribe</a>
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


def main():
    subscribers = get_subscribers()
    print(f"Found {len(subscribers)} active subscribers")

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
                    snapshot = get_stock_snapshot(ticker)
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
