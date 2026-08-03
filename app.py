import os
import json
import time
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

def get_sheet():
    # Credentials are stored as an environment variable on Render (see deployment steps)
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    sheet_name = os.environ.get("SHEET_NAME", "Subscribers")
    return client.open_by_key(spreadsheet_id).worksheet(sheet_name)


@app.route("/")
def home():
    return "EPPN backend is running."


@app.route("/search-tickers")
def search_tickers():
    """Proxies Finnhub's symbol search so the API key never reaches the browser."""
    query = request.args.get("q", "").strip()
    if not query or len(query) < 1:
        return jsonify({"results": []})

    data = None
    last_error = None
    for attempt in range(1, 3):  # try up to 2 times, since 502s are often transient
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
        print("Ticker search error (all attempts failed):", last_error)
        return jsonify({"results": [], "error": "Search temporarily unavailable"}), 503

    try:
        # Exclude a small blocklist of clearly irrelevant types, rather than
        # requiring an exact match — Finnhub's type labels for ETFs/funds
        # aren't consistently "ETF" across all results, so an allowlist was
        # silently dropping valid index fund results.
        blocked_types = {"Crypto", "Forex", "Index"}
        results = []
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
