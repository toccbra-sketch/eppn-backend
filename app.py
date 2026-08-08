import os
import json
import time
import random
import re
import secrets
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
CORS(app)  # allows your GitHub Pages site to call this backend

# --- Google Sheets setup ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]

# --- Brevo (email API) setup ---
# We send email via Brevo's HTTPS API instead of raw SMTP because Render's free
# tier blocks outbound traffic on SMTP ports (25/465/587) — HTTPS on port 443
# isn't affected, so this works on the free plan.
BREVO_API_KEY = os.environ["BREVO_API_KEY"]
EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]  # must be a verified sender in Brevo
SENDER_NAME = os.environ.get("SENDER_NAME", "The Portfolio Briefcase")


def get_sheet():
    # Credentials are stored as an environment variable on Render (see deployment steps)
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    sheet_name = os.environ.get("SHEET_NAME", "Subscribers")
    return client.open_by_key(spreadsheet_id).worksheet(sheet_name)


def find_subscriber_row(email):
    """Returns (row_number, row_dict) for a subscriber by email, or (None, None)."""
    sheet = get_sheet()
    records = sheet.get_all_records()
    email_lower = email.strip().lower()
    for i, record in enumerate(records, start=2):  # row 1 is headers
        if str(record.get("Email", "")).strip().lower() == email_lower:
            return i, record
    return None, None


def send_plain_email(to_email, subject, body_text, reply_to=None):
    print(f"[send_plain_email] sending via Brevo API to {to_email}...")
    payload = {
        "sender": {"name": SENDER_NAME, "email": EMAIL_ADDRESS},
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": body_text,
    }
    if reply_to:
        payload["replyTo"] = {"email": reply_to}

    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={
            "api-key": BREVO_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=payload,
        timeout=15,
    )
    if resp.status_code >= 300:
        print(f"[send_plain_email] Brevo API error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
    print(f"[send_plain_email] sent to {to_email} (Brevo message id: {resp.json().get('messageId')})")


# In-memory store of pending login codes: { email: {"code": "123456", "expires": epoch_seconds} }
# NOTE: this resets if the Render service restarts/spins down — acceptable for
# short-lived codes (10 min expiry), just means a user would need to request a
# fresh code in the rare case that happens mid-login.
LOGIN_CODES = {}
CODE_EXPIRY_SECONDS = 10 * 60

# In-memory store of active login sessions: { token: {"email": ..., "expires": epoch_seconds} }
# Issued by /login-verify once a code is confirmed. Every subscriber-specific
# endpoint below requires one of these tokens and derives the email FROM the
# token — it never trusts an email the client just typed into a request body.
# That's the actual fix: knowing someone's email is no longer enough to read
# or change their portfolio; you need a live session for that exact account.
# Same reset-on-restart tradeoff as LOGIN_CODES above — acceptable here since
# it just means an affected user logs in again, nothing is lost or corrupted.
SESSION_TOKENS = {}
SESSION_EXPIRY_SECONDS = 14 * 24 * 60 * 60  # 14 days


def get_authenticated_email():
    """Reads the 'Authorization: Bearer <token>' header, validates it against
    SESSION_TOKENS, and returns the associated email — or None if the header
    is missing, the token is unknown, or it has expired. Endpoints that need
    a logged-in subscriber call this instead of reading email from the
    request body/query string."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):].strip()
    entry = SESSION_TOKENS.get(token)
    if not entry:
        return None
    if time.time() > entry["expires"]:
        del SESSION_TOKENS[token]
        return None
    return entry["email"]


@app.route("/")
def home():
    return "EPPN backend is running."


# Small local fallback so search still works (for common names) if Finnhub's
# search endpoint is temporarily down — this is NOT a replacement for the live
# API, just a safety net so the page doesn't look broken during an outage.
FALLBACK_TICKERS = [
    {"symbol": "AAPL", "name": "Apple Inc"},
    {"symbol": "MSFT", "name": "Microsoft Corp"},
    {"symbol": "GOOGL", "name": "Alphabet Inc"},
    {"symbol": "AMZN", "name": "Amazon.com Inc"},
    {"symbol": "TSLA", "name": "Tesla Inc"},
    {"symbol": "NVDA", "name": "NVIDIA Corp"},
    {"symbol": "META", "name": "Meta Platforms Inc"},
    {"symbol": "NFLX", "name": "Netflix Inc"},
    {"symbol": "DIS", "name": "Walt Disney Co"},
    {"symbol": "IBM", "name": "IBM Corp"},
    {"symbol": "AMD", "name": "Advanced Micro Devices"},
    {"symbol": "VOO", "name": "Vanguard S&P 500 ETF"},
    {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust"},
    {"symbol": "QQQ", "name": "Invesco QQQ Trust"},
    {"symbol": "VTI", "name": "Vanguard Total Stock Market ETF"},
]


# Common cryptocurrencies, mapped to Finnhub's exchange-prefixed quote format.
# Finnhub's general /search endpoint is built for stocks/ETFs and doesn't reliably
# surface real crypto pairs (searching "BTC" there returns things like "Grayscale
# Bitcoin Trust" — a stock, not actual Bitcoin) — so we match crypto separately here.
POPULAR_CRYPTO = [
    {"query_terms": ["btc", "bitcoin"], "symbol": "BINANCE:BTCUSDT", "name": "Bitcoin"},
    {"query_terms": ["eth", "ethereum"], "symbol": "BINANCE:ETHUSDT", "name": "Ethereum"},
    {"query_terms": ["sol", "solana"], "symbol": "BINANCE:SOLUSDT", "name": "Solana"},
    {"query_terms": ["doge", "dogecoin"], "symbol": "BINANCE:DOGEUSDT", "name": "Dogecoin"},
    {"query_terms": ["xrp", "ripple"], "symbol": "BINANCE:XRPUSDT", "name": "XRP"},
    {"query_terms": ["ada", "cardano"], "symbol": "BINANCE:ADAUSDT", "name": "Cardano"},
    {"query_terms": ["bnb", "binance coin"], "symbol": "BINANCE:BNBUSDT", "name": "BNB"},
    {"query_terms": ["ltc", "litecoin"], "symbol": "BINANCE:LTCUSDT", "name": "Litecoin"},
    {"query_terms": ["avax", "avalanche"], "symbol": "BINANCE:AVAXUSDT", "name": "Avalanche"},
    {"query_terms": ["link", "chainlink"], "symbol": "BINANCE:LINKUSDT", "name": "Chainlink"},
]


def get_quote(ticker):
    """Fetches just price + % change for one ticker (no news/AI) — used to show
    live prices on the logged-in dashboard. Returns None on any failure so the
    dashboard can show a ticker as 'price unavailable' rather than break."""
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        current_price = data.get("c")
        prev_close = data.get("pc")
        if not current_price or not prev_close:
            return None
        pct_change = ((current_price - prev_close) / prev_close) * 100
        return {"price": round(current_price, 2), "pct_change": round(pct_change, 2)}
    except Exception as e:
        print(f"Quote fetch failed for {ticker}: {e}")
        return None


@app.route("/portfolio-quotes")
def portfolio_quotes():
    """Returns the logged-in subscriber's name plus a live price + % change
    for each ticker in their portfolio. Used by the home dashboard."""
    email = get_authenticated_email()
    if not email:
        return jsonify({"error": "Not authenticated. Please log in again."}), 401

    try:
        _, record = find_subscriber_row(email)
        if not record or str(record.get("Status", "")).lower() != "active":
            return jsonify({"error": "No active subscriber found for this email"}), 404

        tickers = [t.strip() for t in (record.get("Portfolio", "") or "").split(",") if t.strip()]
        quotes = []
        for ticker in tickers:
            q = get_quote(ticker)
            quotes.append({
                "ticker": ticker,
                "price": q["price"] if q else None,
                "pct_change": q["pct_change"] if q else None,
            })

        return jsonify({"name": record.get("Name", ""), "quotes": quotes})
    except Exception as e:
        print("Portfolio quotes error:", e)
        return jsonify({"error": "Could not retrieve live quotes"}), 500


@app.route("/search-tickers")
def search_tickers():
    """Proxies Finnhub's symbol search so the API key never reaches the browser."""
    query = request.args.get("q", "").strip()
    if not query or len(query) < 1:
        return jsonify({"results": []})

    q_lower = query.lower()
    crypto_matches = [
        {"symbol": c["symbol"], "name": c["name"]}
        for c in POPULAR_CRYPTO
        if any(q_lower in term or term.startswith(q_lower) for term in c["query_terms"])
    ]

    data = None
    last_error = None
    for attempt in range(1, 3):  # try up to 2 times, since 502s/503s are often transient
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/search",
                params={"q": query, "token": FINNHUB_API_KEY},
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_error = e
            print(f"Ticker search attempt {attempt} failed: {e}")
            if attempt < 2:
                time.sleep(1)

    if data is None:
        # Live API is down — fall back to the small local list instead of
        # returning nothing, so the page still works for common tickers.
        print("Ticker search error (all attempts failed), using fallback list:", last_error)
        fallback_matches = [
            t for t in FALLBACK_TICKERS
            if q_lower in t["symbol"].lower() or q_lower in t["name"].lower()
        ]
        combined = crypto_matches + fallback_matches
        return jsonify({"results": combined[:8], "fallback": True})

    try:
        # Exclude a small blocklist of clearly irrelevant types, rather than
        # requiring an exact match — Finnhub's type labels for ETFs/funds
        # aren't consistently "ETF" across all results, so an allowlist was
        # silently dropping valid index fund results.
        blocked_types = {"Forex", "Index"}
        results = list(crypto_matches)  # crypto matches shown first, if any
        for item in data.get("result", []):
            symbol = item.get("symbol", "")
            item_type = item.get("type", "")
            if item_type in blocked_types:
                continue
            if "." in symbol:  # skip foreign-exchange-listed duplicates
                continue
            results.append({
                "symbol": symbol,
                "name": item.get("description"),
            })
            if len(results) >= 8:
                break

        return jsonify({"results": results})
    except Exception as e:
        print("Ticker search error:", e)
        return jsonify({"results": [], "error": "Search failed"}), 500


@app.route("/subscribe", methods=["POST"])
def subscribe():
    data = request.get_json(force=True)

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    portfolio = (data.get("portfolio") or "").strip()
    status = data.get("status", "active")

    if not name or not email or not portfolio:
        return jsonify({"error": "Missing required fields"}), 400

    try:
        sheet = get_sheet()
        sheet.append_row([name, email, portfolio, status])
    except Exception as e:
        print("Error writing to sheet:", e)
        return jsonify({"error": "Could not save subscriber"}), 500

    return jsonify({"message": "Subscribed successfully"}), 200


@app.route("/feedback", methods=["POST"])
def feedback():
    """Sends a feedback submission straight to the site owner's inbox via
    Brevo, with reply-to set to the submitter's email (if given) so replying
    to the notification goes straight back to them."""
    data = request.get_json(force=True)

    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    message = (data.get("message") or "").strip()
    # Honeypot: a hidden field real users never see or fill in. If it has a
    # value, this was almost certainly a bot — pretend success without
    # actually sending, so the bot doesn't learn to try something else.
    honeypot = (data.get("website") or "").strip()

    if honeypot:
        return jsonify({"message": "Thanks for your feedback!"}), 200

    if not message:
        return jsonify({"error": "Message is required"}), 400
    if len(message) > 5000:
        return jsonify({"error": "Message is too long"}), 400
    if email and not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        return jsonify({"error": "That email address doesn't look valid"}), 400

    body_text = (
        f"New feedback submitted via the site.\n\n"
        f"Name: {name or 'Not provided'}\n"
        f"Email: {email or 'Not provided'}\n\n"
        f"Message:\n{message}"
    )

    try:
        send_plain_email(
            EMAIL_ADDRESS,
            "New feedback — The Portfolio Briefcase",
            body_text,
            reply_to=email or None,
        )
    except Exception as e:
        print("Feedback email error:", e)
        return jsonify({"error": "Could not send feedback right now. Please try again."}), 500

    return jsonify({"message": "Thanks for your feedback!"}), 200


@app.route("/login-request", methods=["POST"])
def login_request():
    """Step 1: person enters their email, we email them a 6-digit code (if that
    email belongs to an active subscriber). Always returns a generic success
    message either way, so this endpoint can't be used to check which emails
    are subscribed."""
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()

    if not email:
        return jsonify({"error": "Email is required"}), 400

    generic_response = jsonify({
        "message": "If that email is subscribed, a login code has been sent."
    })

    try:
        print(f"[login_request] looking up {email} in sheet...")
        _, record = find_subscriber_row(email)
        print(f"[login_request] sheet lookup done")
        if not record or str(record.get("Status", "")).lower() != "active":
            return generic_response, 200  # don't reveal whether the email exists

        code = f"{random.randint(0, 999999):06d}"
        LOGIN_CODES[email.lower()] = {
            "code": code,
            "expires": time.time() + CODE_EXPIRY_SECONDS,
        }

        send_plain_email(
            email,
            "Your login code — The Portfolio Briefcase",
            f"Your login code is: {code}\n\nThis code expires in 10 minutes. "
            f"If you didn't request this, you can safely ignore this email."
        )
        print(f"[login_request] done for {email}")

    except Exception as e:
        print("Login request error:", repr(e))
        # Still return the generic message — don't leak internal errors to the client

    return generic_response, 200


@app.route("/login-verify", methods=["POST"])
def login_verify():
    """Step 2: person enters the code they received. If it matches and hasn't
    expired, they're considered logged in (frontend stores the email locally)."""
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()

    entry = LOGIN_CODES.get(email)
    if not entry:
        return jsonify({"error": "No pending code for this email. Request a new one."}), 400

    if time.time() > entry["expires"]:
        del LOGIN_CODES[email]
        return jsonify({"error": "Code expired. Request a new one."}), 400

    if code != entry["code"]:
        return jsonify({"error": "Incorrect code."}), 400

    del LOGIN_CODES[email]  # one-time use

    token = secrets.token_urlsafe(32)
    SESSION_TOKENS[token] = {"email": email, "expires": time.time() + SESSION_EXPIRY_SECONDS}

    return jsonify({"message": "Logged in successfully", "token": token}), 200


@app.route("/logout", methods=["POST"])
def logout():
    """Invalidates the current session token server-side, so a stolen/old
    token can't be reused even if it's still sitting in someone's browser."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()
        SESSION_TOKENS.pop(token, None)
    return jsonify({"message": "Logged out"}), 200


@app.route("/get-portfolio")
def get_portfolio():
    """Returns the logged-in subscriber's current name/portfolio/status."""
    email = get_authenticated_email()
    if not email:
        return jsonify({"error": "Not authenticated. Please log in again."}), 401

    try:
        _, record = find_subscriber_row(email)
        if not record:
            return jsonify({"error": "No subscriber found for this email"}), 404

        return jsonify({
            "name": record.get("Name", ""),
            "email": record.get("Email", ""),
            "portfolio": record.get("Portfolio", ""),
            "status": record.get("Status", ""),
        })
    except Exception as e:
        print("Get portfolio error:", e)
        return jsonify({"error": "Could not retrieve portfolio"}), 500


@app.route("/update-portfolio", methods=["POST"])
def update_portfolio():
    """Overwrites the logged-in subscriber's portfolio column."""
    email = get_authenticated_email()
    if not email:
        return jsonify({"error": "Not authenticated. Please log in again."}), 401

    data = request.get_json(force=True)
    portfolio = (data.get("portfolio") or "").strip()

    if not portfolio:
        return jsonify({"error": "Portfolio is required"}), 400

    try:
        sheet = get_sheet()
        row_num, record = find_subscriber_row(email)
        if not row_num:
            return jsonify({"error": "No subscriber found for this email"}), 404

        headers = sheet.row_values(1)
        portfolio_col = headers.index("Portfolio") + 1  # gspread columns are 1-indexed
        sheet.update_cell(row_num, portfolio_col, portfolio)

        return jsonify({"message": "Portfolio updated successfully"}), 200
    except Exception as e:
        print("Update portfolio error:", e)
        return jsonify({"error": "Could not update portfolio"}), 500


@app.route("/unsubscribe", methods=["POST"])
def unsubscribe():
    """Sets the logged-in subscriber's status to inactive."""
    email = get_authenticated_email()
    if not email:
        return jsonify({"error": "Not authenticated. Please log in again."}), 401

    try:
        sheet = get_sheet()
        row_num, record = find_subscriber_row(email)
        if not row_num:
            return jsonify({"error": "No subscriber found for this email"}), 404

        headers = sheet.row_values(1)
        status_col = headers.index("Status") + 1
        sheet.update_cell(row_num, status_col, "inactive")

        # Invalidate this session too — an unsubscribed account shouldn't
        # keep a live token that could still read/edit anything afterward.
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            SESSION_TOKENS.pop(auth_header[len("Bearer "):].strip(), None)

        return jsonify({"message": "Unsubscribed successfully"}), 200
    except Exception as e:
        print("Unsubscribe error:", e)
        return jsonify({"error": "Could not unsubscribe"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
