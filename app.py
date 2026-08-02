import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)
CORS(app)  # allows your GitHub Pages site to call this backend

# --- Google Sheets setup ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

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
