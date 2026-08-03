import os
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import gspread
from google.oauth2.service_account import Credentials
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


def get_stock_snapshot(ticker):
    """Pull current price + % change + a recent headline for one ticker via Finnhub."""
    try:
        quote_resp = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=10
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
            timeout=10
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
        print(f"Could not fetch data for {ticker}: {e}")
        return None


FORBIDDEN_PHRASES = [
    "buy", "sell", "should invest", "you should", "i recommend", "recommendation",
    "good time to", "bad time to", "consider buying", "consider selling",
    "strong buy", "strong sell", "hold", "invest now", "worth investing",
]


def generate_blurb(snapshot):
    """Ask Claude for a short, educational, trend-based note. Never buy/sell language."""
    prompt = f"""You are writing one short paragraph (2-3 sentences) for an educational stock newsletter.

Stock: {snapshot['ticker']}
Current price: ${snapshot['price']}
Change since last close: {snapshot['pct_change']}%
Recent headline: {snapshot['headline'] or 'No major headline today'}

Write a brief, neutral, educational note about what historically tends to follow this kind of move or news,
in plain, beginner-friendly language.

STRICT RULES — these are non-negotiable:
- Do NOT use the words "buy," "sell," "hold," or any variation telling the reader what to do with the stock.
- Do NOT recommend, suggest, or imply any action the reader should take.
- Do NOT say things like "good time to," "bad time to," "worth considering," or similar action-nudging phrases.
- Only describe historical patterns and context — never advice, opinions, or predictions framed as guidance.
- Do not add a disclaimer sentence — one is added separately in the email template.

This is strictly educational content. If you cannot describe this news/movement without implying
a course of action, focus purely on factual historical context instead."""

    response = gemini_client.models.generate_content(
        model="gemini-flash-latest",
        contents=prompt
    )
    blurb = response.text.strip()

    # Second layer of protection: flag (don't silently trust the model)
    lowered = blurb.lower()
    if any(phrase in lowered for phrase in FORBIDDEN_PHRASES):
        print(f"WARNING: blurb for {snapshot['ticker']} contained flagged language, using fallback text")
        return (f"{snapshot['ticker']} moved {snapshot['pct_change']}% following recent news. "
                f"No further trend detail available for this update.")

    return blurb


def build_email_html(name, stock_sections):
    sections_html = ""
    for s in stock_sections:
        direction = "up" if s["pct_change"] >= 0 else "down"
        sections_html += f"""
        <div style="margin-bottom:20px; padding:14px; border:1px solid #ddd; border-radius:6px;">
          <h3 style="margin:0 0 6px;">{s['ticker']} — ${s['price']} ({direction} {abs(s['pct_change'])}%)</h3>
          <p style="margin:0; color:#333;">{s['blurb']}</p>
        </div>
        """

    return f"""
    <div style="font-family:Arial, sans-serif; max-width:600px; margin:auto;">
      <h2>Your daily portfolio update</h2>
      <p>Hi {name}, here's what's happening with your stocks today.</p>
      {sections_html}
      <p style="font-size:12px; color:#777; margin-top:24px;">
        This newsletter is for educational purposes only and is not financial advice.
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
                        snapshot["blurb"] = generate_blurb(snapshot)
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
