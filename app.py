import logging
import os
import uuid
import json
import sqlite3
import hmac
import hashlib
import base64
import threading
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

load_dotenv()

BASE_URL = (os.getenv("BASE_URL") or "http://localhost:5000").rstrip("/")
PROCESSING_LOCK_MINUTES = int(os.getenv("PROCESSING_LOCK_MINUTES", "2"))
PROCESSING_HOLD_SECONDS = PROCESSING_LOCK_MINUTES * 60

MERCHANT_ACCOUNT = os.getenv("ADYEN_MERCHANT_ACCOUNT")
CLIENT_KEY = os.getenv("ADYEN_CLIENT_KEY")
HMAC_KEY = os.getenv("HMAC_KEY")
SKIP_HMAC_VALIDATION = os.getenv("SKIP_HMAC_VALIDATION", "false").lower() == "true"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.FileHandler("app.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)
logger.info(f"BASE_URL: {BASE_URL} | PROCESSING_LOCK_MINUTES: {PROCESSING_LOCK_MINUTES}")

app = Flask(__name__)

adyen = Adyen.Adyen()
adyen.checkout.client.xapikey = os.getenv("ADYEN_API_KEY")
adyen.checkout.client.platform = "test"

init_db()

def schedule_processing_unlock(payment_id: str):
    def _unlock():
        try:
            row = get_payment_by_id(payment_id)
            if not row:
                return
            _, _, _, _, status, _, _ = row
            if status == "processing":
                update_status_by_id(payment_id, "pending")
                logger.info("Auto-unlock → pending (paymentId=%s)", payment_id)
            else:
                logger.debug("Auto-unlock skipped; status=%s (paymentId=%s)", status, payment_id)
        except Exception:
            logger.exception("Auto-unlock failed (paymentId=%s)", payment_id)
    t = threading.Timer(PROCESSING_HOLD_SECONDS, _unlock)
    t.daemon = True
    t.start()

@app.route("/admin", methods=["GET", "POST"])
def admin_form():
    if request.method == "POST":
        try:
            price_minor = int(float(request.form["price"]) * 100)
            currency = request.form["currency"]
            reference = request.form["reference"]
            country = request.form["country"]
            expires_hours = int(request.form.get("expires_hours", 24))

            payment_id = str(uuid.uuid4())
            expires_at = datetime.now() + timedelta(hours=expires_hours)

            try:
                create_payment_record(payment_id, price_minor, currency, reference, country, expires_at)
            except sqlite3.IntegrityError:
                return jsonify({"error": "Reference must be unique"}), 400

            return jsonify({"message": "Payment link generated",
                            "url": f"{BASE_URL}/checkout?paymentId={payment_id}"})
        except Exception as e:
            logger.exception("Error in admin form")
            return jsonify({"error": str(e)}), 500
    return render_template("form.html")

@app.route("/checkout")
def checkout_page():
    """
    Create Adyen session ONLY if status is 'pending'. Immediately set status='processing'
    before returning HTML, and schedule auto-unlock. While 'processing', do NOT create
    more sessions; show waiting message instead. 'paid' is blocked as before.
    """
    payment_id = request.args.get("paymentId")
    if not payment_id:
        return render_template("message.html", message="Invalid payment ID"), 400

    try:
        row = get_payment_by_id(payment_id)
        if not row:
            return render_template("message.html", message="Payment not found"), 404

        id_, amount, currency, reference, status, country, expires_at_str = row
        expires_at = datetime.fromisoformat(expires_at_str)

        # Expired?
        if datetime.now() > expires_at:
            return render_template("message.html", message="This payment link has expired"), 403

        # Already paid?
        if status == "paid":
            return render_template("message.html", message="This payment link has already been paid"), 403

        # In-flight processing? -> don't create a new session
        if status == "processing":
            return render_template(
                "message.html",
                message="Payment in progress. This page will update once it's completed."
            )

        # status == 'pending' -> create session, then lock to 'processing'
        session_reference = f"{reference}_{str(uuid.uuid4())[:8]}"
        req = {
            "amount": {"value": amount, "currency": currency},
            "reference": session_reference,
            "merchantAccount": MERCHANT_ACCOUNT,
            "returnUrl": f"{BASE_URL}/result?paymentId={payment_id}",
            "countryCode": country,
        }

        try:
            result = adyen.checkout.payments_api.sessions(req)
            session_id = result.message["id"]
            session_data = result.message["sessionData"]
        except Exception as e:
            logger.exception("Error creating Adyen session")
            return render_template("message.html", message=f"Error creating session: {str(e)}"), 500

        # Lock AFTER a successful session creation (so we only lock real attempts)
        update_status_by_id(payment_id, "processing")
        schedule_processing_unlock(payment_id)
        logger.info("Locked link → processing (paymentId=%s)", payment_id)

        return render_template("checkout.html",
                               client_key=CLIENT_KEY,
                               session_id=session_id,
                               session_data=session_data)

    except Exception as e:
        logger.exception("Error in checkout page")
        return render_template("message.html", message=f"Error: {str(e)}"), 500

@app.route("/result")
def result_page():
    """
    Shopper returns from redirect. If still pending (edge cases), lock to processing and schedule
    unlock. Usually it is already 'processing' from /checkout.
    """
    payment_id = request.args.get("paymentId")
    if not payment_id:
        return render_template("message.html", message="Invalid payment ID"), 400

    try:
        row = get_payment_by_id(payment_id)
        if not row:
            return render_template("message.html", message="Payment not found"), 404

        _, _, _, _, status, _, _ = row

        if status == "pending":
            update_status_by_id(payment_id, "processing")
            schedule_processing_unlock(payment_id)
            logger.info("Locked in /result → processing (paymentId=%s)", payment_id)

        return render_template(
            "message.html",
            message="Thanks! We're confirming your payment. This page will update once it's completed."
        )
    except Exception as e:
        logger.exception("Error in result page")
        return render_template("message.html", message=f"Error: {str(e)}"), 500

@app.route("/status")
def status_api():
    payment_id = request.args.get("paymentId")
    if not payment_id:
        return jsonify({"error": "paymentId is required"}), 400
    row = get_payment_by_id(payment_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    id_, _, _, reference, status, _, _ = row
    return jsonify({"paymentId": id_, "reference": reference, "status": status})

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    AUTHORISATION success → paid (overrides timer)
    AUTHORISATION failure → pending (overrides timer)
    """
    payload = request.get_data(cache=False)

    if not SKIP_HMAC_VALIDATION:
        sig = (request.headers.get("Hmac-Signature")
               or request.headers.get("hmac-signature")
               or request.headers.get("HMAC-Signature")
               or "")
        try:
            key_bytes = base64.b64decode(HMAC_KEY or "")
        except Exception:
            key_bytes = (HMAC_KEY or "").encode("utf-8")
        computed = base64.b64encode(hmac.new(key_bytes, payload, hashlib.sha256).digest()).decode("utf-8")
        if not hmac.compare_digest(computed, sig):
            return jsonify({"error": "Invalid HMAC signature"}), 401

    try:
        data = json.loads(payload.decode("utf-8"))
        for n in data.get("notificationItems", []):
            item = n.get("NotificationRequestItem", {})
            event_code = item.get("eventCode")
            success = str(item.get("success")).lower() == "true"
            session_reference = item.get("merchantReference", "") or ""
            original_reference = session_reference.split("_")[0] if "_" in session_reference else session_reference

            if event_code == "AUTHORISATION":
                if success:
                    update_status_by_reference(original_reference, "paid")
                    logger.info("Webhook → paid (%s)", original_reference)
                else:
                    update_status_by_reference(original_reference, "pending")
                    logger.info("Webhook → pending (%s)", original_reference)

        return "[accepted]", 200
    except Exception as e:
        logger.exception("Webhook error")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)
