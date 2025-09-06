# app.py
# Minimal Flask + Adyen demo using SQLite for link state.
# DB helpers live in db.py to keep this file focused on HTTP + payment logic.

import logging
import os
import uuid
import json
import sqlite3  # only used to catch IntegrityError from db.create_payment_record
import hmac
import hashlib
import base64
from datetime import datetime, timedelta

from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import Adyen

from db import (
    init_db,
    create_payment_record,
    get_payment_by_id,
    update_status_by_id,
    update_status_by_reference,
)

# -------------------------------------------------
# App configuration (env-driven)
# -------------------------------------------------
load_dotenv()

BASE_URL = (os.getenv("BASE_URL") or "http://localhost:5000").rstrip("/")
# Optional: if you later add a scheduler to unlock stale "processing" links
PROCESSING_LOCK_MINUTES = int(os.getenv("PROCESSING_LOCK_MINUTES", "8"))

MERCHANT_ACCOUNT = os.getenv("ADYEN_MERCHANT_ACCOUNT")
CLIENT_KEY = os.getenv("ADYEN_CLIENT_KEY")  # used by frontend checkout.html
HMAC_KEY = os.getenv("HMAC_KEY")            # only used if you re-enable HMAC check
SKIP_HMAC_VALIDATION = os.getenv("SKIP_HMAC_VALIDATION", "false").lower() == "true"

# -------------------------------------------------
# Logging
# -------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.FileHandler("app.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

logger.info(f"BASE_URL: {BASE_URL}")
logger.info(f"Merchant Account: {MERCHANT_ACCOUNT}")

# -------------------------------------------------
# Flask app + Adyen client
# -------------------------------------------------
app = Flask(__name__)

logger.info("Initializing Adyen client")
adyen = Adyen.Adyen()
adyen.checkout.client.xapikey = os.getenv("ADYEN_API_KEY")
adyen.checkout.client.platform = "test"  # set to 'live' when you go to production

# -------------------------------------------------
# Database bootstrapping
# -------------------------------------------------
init_db()

# -------------------------------------------------
# Routes
# -------------------------------------------------
@app.route("/admin", methods=["GET", "POST"])
def admin_form():
    """
    Admin creates a link by posting price/currency/reference/country.
    We store it with status 'pending' and return the checkout URL containing the unique paymentId.
    """
    if request.method == "POST":
        logger.info("Processing admin form submission")
        try:
            price_minor = int(float(request.form["price"]) * 100)  # minor units
            currency = request.form["currency"]
            reference = request.form["reference"]
            country = request.form["country"]
            expires_hours = int(request.form.get("expires_hours", 24))

            payment_id = str(uuid.uuid4())  # unique ID for the link
            expires_at = datetime.now() + timedelta(hours=expires_hours)

            logger.info(
                "Creating payment record: ID=%s Amount=%s Currency=%s Reference=%s Country=%s",
                payment_id, price_minor, currency, reference, country
            )

            try:
                create_payment_record(payment_id, price_minor, currency, reference, country, expires_at)
            except sqlite3.IntegrityError:
                # Reference must be unique across links
                return jsonify({"error": "Reference must be unique"}), 400

            checkout_url = f"{BASE_URL}/checkout?paymentId={payment_id}"
            logger.info(f"Generated checkout URL: {checkout_url}")
            return jsonify({"message": "Payment link generated", "url": checkout_url})

        except Exception as e:
            logger.exception("Error in admin form")
            return jsonify({"error": str(e)}), 500

    return render_template("form.html")

@app.route("/checkout")
def checkout_page():
    """
    Shopper lands here using the generated link.
    We validate link state, create an Adyen session, and render the Drop-in page.
    """
    payment_id = request.args.get("paymentId")
    logger.info(f"Accessing checkout page with paymentId={payment_id}")

    if not payment_id:
        return render_template("message.html", message="Invalid payment ID"), 400

    try:
        payment = get_payment_by_id(payment_id)
        if not payment:
            return render_template("message.html", message="Payment not found"), 404

        id_, amount, currency, reference, status, country, expires_at_str = payment
        expires_at = datetime.fromisoformat(expires_at_str)

        # Only allow new sessions while link is 'pending' and not expired
        if status != "pending" or datetime.now() > expires_at:
            msg = (
                "This payment link has expired or already been paid"
                if status != "pending" else "This payment link has expired"
            )
            return render_template("message.html", message=msg), 403

        # Unique merchantReference per attempt to avoid collisions/retries issues
        session_reference = f"{reference}_{str(uuid.uuid4())[:8]}"

        request_data = {
            "amount": {"value": amount, "currency": currency},
            "reference": session_reference,
            "merchantAccount": MERCHANT_ACCOUNT,
            "returnUrl": f"{BASE_URL}/result?paymentId={payment_id}",
            "countryCode": country,
        }

        logger.debug("Creating Adyen session with request: %s", json.dumps(request_data, indent=2))
        try:
            result = adyen.checkout.payments_api.sessions(request_data)
            logger.debug("Adyen session response: %s", json.dumps(result.message, indent=2))
            session_id = result.message["id"]
            session_data = result.message["sessionData"]
        except Exception as e:
            logger.exception("Error creating Adyen session")
            return render_template("message.html", message=f"Error creating session: {str(e)}"), 500

        # Render client with CLIENT_KEY + session info
        return render_template("checkout.html", client_key=CLIENT_KEY, session_id=session_id, session_data=session_data)

    except Exception as e:
        logger.exception("Error in checkout page")
        return render_template("message.html", message=f"Error: {str(e)}"), 500

@app.route("/result")
def result_page():
    """
    Shopper returns from redirect (Adyen returnUrl).
    Immediately lock link (status=processing) to block duplicate attempts
    while we wait for the webhook to confirm success/failure.
    """
    payment_id = request.args.get("paymentId")
    logger.info(f"Redirect to result page for paymentId={payment_id}")

    if not payment_id:
        return render_template("message.html", message="Invalid payment ID"), 400

    try:
        payment = get_payment_by_id(payment_id)
        if not payment:
            return render_template("message.html", message="Payment not found"), 404

        _, _, _, _, status, _, _ = payment

        if status == "pending":
            update_status_by_id(payment_id, "processing")
            logger.info("Set status=processing for paymentId=%s", payment_id)

        # Your template can poll /status to auto-update the UI when webhook lands
        return render_template(
            "message.html",
            message="Thanks! We're confirming your payment. This page will update once it's completed."
        )
    except Exception as e:
        logger.exception("Error in result page")
        return render_template("message.html", message=f"Error: {str(e)}"), 500

@app.route("/status")
def status_api():
    """
    Small JSON status endpoint (useful for front-end polling from /result page).
    """
    payment_id = request.args.get("paymentId")
    if not payment_id:
        return jsonify({"error": "paymentId is required"}), 400

    payment = get_payment_by_id(payment_id)
    if not payment:
        return jsonify({"error": "not found"}), 404

    id_, _, _, reference, status, _, _ = payment
    return jsonify({"paymentId": id_, "reference": reference, "status": status})

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Adyen standard webhook (Checkout): processes notifications.
    We currently handle AUTHORISATION to set link 'paid' or unlock back to 'pending'.
    """
    logger.info("Received webhook request")
    payload = request.get_data(cache=False)  # raw bytes

    # Optional HMAC verification (disabled if SKIP_HMAC_VALIDATION=true)
    if not SKIP_HMAC_VALIDATION:
        signature = (
            request.headers.get("Hmac-Signature")
            or request.headers.get("hmac-signature")
            or request.headers.get("HMAC-Signature")
            or ""
        )
        try:
            key_bytes = base64.b64decode(HMAC_KEY or "")
        except Exception:
            key_bytes = (HMAC_KEY or "").encode("utf-8")
        computed = base64.b64encode(hmac.new(key_bytes, payload, hashlib.sha256).digest()).decode("utf-8")
        if not hmac.compare_digest(computed, signature):
            logger.error("Invalid HMAC signature")
            return jsonify({"error": "Invalid HMAC signature"}), 401
    else:
        logger.warning("SKIPPING HMAC VALIDATION (SKIP_HMAC_VALIDATION=true)")

    # Process notifications
    try:
        data = json.loads(payload.decode("utf-8"))
        logger.debug("Webhook JSON: %s", json.dumps(data, indent=2))

        for notification in data.get("notificationItems", []):
            item = notification.get("NotificationRequestItem", {})
            event_code = item.get("eventCode")
            success = str(item.get("success")).lower() == "true"
            session_reference = item.get("merchantReference", "") or ""
            original_reference = session_reference.split("_")[0] if "_" in session_reference else session_reference

            if event_code == "AUTHORISATION":
                if success:
                    logger.info("AUTHORISATION success → paid (%s)", original_reference)
                    update_status_by_reference(original_reference, "paid")
                else:
                    logger.info("AUTHORISATION failed → pending (%s)", original_reference)
                    update_status_by_reference(original_reference, "pending")

        # Respond 2xx within 10s so Adyen doesn't retry
        return "[accepted]", 200

    except Exception as e:
        logger.exception("Error processing webhook")
        return jsonify({"error": str(e)}), 500

# -------------------------------------------------
# Entrypoint (Heroku runs via Procfile: web: gunicorn app:app)
# -------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting Flask application")
    app.run(debug=True)
