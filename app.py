import logging
from flask import Flask, render_template, request, jsonify
import sqlite3
import uuid
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import Adyen
import hmac
import hashlib
import base64
import json
from urllib.parse import urlencode, urljoin

# -------------------------------------------------
# Base URL config (single source of truth)
# -------------------------------------------------
load_dotenv()
BASE_URL = (os.getenv("BASE_URL") or "http://localhost:5000").rstrip("/")

def abs_url(path: str, **query):
    url = urljoin(f"{BASE_URL}/", path.lstrip("/"))
    if query:
        return f"{url}?{urlencode(query)}"
    return url

# Optional: how long you want to keep the lock if no webhook arrives (minutes)
PROCESSING_LOCK_MINUTES = int(os.getenv("PROCESSING_LOCK_MINUTES", "30"))

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[logging.FileHandler('app.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Adyen configuration
logger.info("Initializing Adyen client")
adyen = Adyen.Adyen()
adyen.checkout.client.xapikey = os.getenv('ADYEN_API_KEY')
adyen.checkout.client.platform = 'test'  # 'live' for production
MERCHANT_ACCOUNT = os.getenv('ADYEN_MERCHANT_ACCOUNT')
CLIENT_KEY = os.getenv('ADYEN_CLIENT_KEY')
HMAC_KEY = os.getenv('HMAC_KEY')  # keep for later when you re-enable
SKIP_HMAC_VALIDATION = (os.getenv("SKIP_HMAC_VALIDATION", "false").lower() == "true")

logger.info(f"Merchant Account: {MERCHANT_ACCOUNT}")
logger.info(f"BASE_URL: {BASE_URL}")

# Database setup
DB_NAME = 'payments.db'

def init_db():
    logger.info("Initializing SQLite database")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            amount INTEGER,
            currency TEXT,
            reference TEXT UNIQUE,
            status TEXT,
            country TEXT,
            expires_at DATETIME
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

init_db()

def get_payment_by_id(payment_id: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT id, amount, currency, reference, status, country, expires_at FROM payments WHERE id = ?', (payment_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def update_status_by_id(payment_id: str, new_status: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE payments SET status = ? WHERE id = ?', (new_status, payment_id))
    conn.commit()
    conn.close()

def update_status_by_reference(reference: str, new_status: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE payments SET status = ? WHERE reference = ?', (new_status, reference))
    conn.commit()
    conn.close()

@app.route('/admin', methods=['GET', 'POST'])
def admin_form():
    if request.method == 'POST':
        logger.info("Processing admin form submission")
        try:
            price = float(request.form['price']) * 100  # minor units
            currency = request.form['currency']
            reference = request.form['reference']
            country = request.form['country']
            expires_hours = int(request.form.get('expires_hours', 24))

            payment_id = str(uuid.uuid4())
            expires_at = datetime.now() + timedelta(hours=expires_hours)

            logger.info(f"Creating payment record: ID={payment_id}, Amount={price}, Currency={currency}, Reference={reference}, Country={country}")
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO payments (id, amount, currency, reference, status, country, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (payment_id, int(price), currency, reference, 'pending', country, expires_at))
                conn.commit()
            except sqlite3.IntegrityError:
                conn.close()
                return jsonify({"error": "Reference must be unique"}), 400
            conn.close()

            checkout_url = abs_url("/checkout", paymentId=payment_id)
            logger.info(f"Generated checkout URL: {checkout_url}")
            return jsonify({"message": "Payment link generated", "url": checkout_url})
        except Exception as e:
            logger.exception("Error in admin form")
            return jsonify({"error": str(e)}), 500

    return render_template('form.html')

@app.route('/checkout')
def checkout_page():
    payment_id = request.args.get('paymentId')
    logger.info(f"Accessing checkout page with paymentId={payment_id}")

    if not payment_id:
        return render_template('message.html', message="Invalid payment ID"), 400

    try:
        payment = get_payment_by_id(payment_id)
        if not payment:
            return render_template('message.html', message="Payment not found"), 404

        id_, amount, currency, reference, status, country, expires_at_str = payment
        expires_at = datetime.fromisoformat(expires_at_str)

        if status != 'pending' or datetime.now() > expires_at:
            status_message = (
                "This payment link has expired or already been paid"
                if status != 'pending' else "This payment link has expired"
            )
            return render_template('message.html', message=status_message), 403

        # Create a new Adyen session for each valid visit
        session_reference = f"{reference}_{str(uuid.uuid4())[:8]}"  # Unique per attempt
        request_data = {
            "amount": {"value": amount, "currency": currency},
            "reference": session_reference,
            "merchantAccount": MERCHANT_ACCOUNT,
            "returnUrl": abs_url("/result", paymentId=payment_id),
            "countryCode": country
        }

        logger.debug(f"Creating Adyen session with request: {json.dumps(request_data, indent=2)}")
        try:
            result = adyen.checkout.payments_api.sessions(request_data)
            logger.debug(f"Adyen session response: {json.dumps(result.message, indent=2)}")
            session_id = result.message['id']
            session_data = result.message['sessionData']
        except Exception as e:
            logger.exception("Error creating Adyen session")
            return render_template('message.html', message=f"Error creating session: {str(e)}"), 500

        return render_template('checkout.html', client_key=CLIENT_KEY, session_id=session_id, session_data=session_data)

    except Exception as e:
        logger.exception("Error in checkout page")
        return render_template('message.html', message=f"Error: {str(e)}"), 500

@app.route('/result')
def result_page():
    """
    Shopper returns here after redirect. Immediately lock the link by setting status=processing
    so they can't start another payment while we wait for the webhook.
    """
    payment_id = request.args.get('paymentId')
    logger.info(f"Redirect to result page for paymentId={payment_id}")

    if not payment_id:
        return render_template('message.html', message="Invalid payment ID"), 400

    try:
        payment = get_payment_by_id(payment_id)
        if not payment:
            return render_template('message.html', message="Payment not found"), 404

        id_, amount, currency, reference, status, country, expires_at_str = payment
        now = datetime.now()

        # Optional: expire the link a bit later while processing to give webhook time
        # (we won't change expires_at in DB; just lock by status)
        if status == 'pending':
            update_status_by_id(payment_id, 'processing')
            logger.info(f"Set status=processing for paymentId={payment_id}")

        # Show a friendly message; your template can poll /status to live-update
        # E.g., add JS in message.html to poll GET /status?paymentId=... every few seconds.
        return render_template(
            'message.html',
            message="Thanks! We're confirming your payment. This page will update once it's completed."
        )
    except Exception as e:
        logger.exception("Error in result page")
        return render_template('message.html', message=f"Error: {str(e)}"), 500

@app.route('/status')
def status_api():
    """
    Small JSON status endpoint (useful for front-end polling from /result page).
    """
    payment_id = request.args.get('paymentId')
    if not payment_id:
        return jsonify({"error": "paymentId is required"}), 400

    payment = get_payment_by_id(payment_id)
    if not payment:
        return jsonify({"error": "not found"}), 404

    id_, amount, currency, reference, status, country, expires_at_str = payment
    return jsonify({
        "paymentId": id_,
        "reference": reference,
        "status": status
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    logger.info("Received webhook request")
    payload = request.get_data(cache=False)  # bytes

    if SKIP_HMAC_VALIDATION:
        logger.warning("SKIPPING HMAC VALIDATION (SKIP_HMAC_VALIDATION=true)")
    else:
        # (left here for when you re-enable)
        signature = (
            request.headers.get('Hmac-Signature')
            or request.headers.get('hmac-signature')
            or request.headers.get('HMAC-Signature')
            or ''
        )
        try:
            key_bytes = base64.b64decode(HMAC_KEY or '')
        except Exception:
            key_bytes = (HMAC_KEY or '').encode('utf-8')
        computed = base64.b64encode(hmac.new(key_bytes, payload, hashlib.sha256).digest()).decode('utf-8')
        if not hmac.compare_digest(computed, signature):
            logger.error("Invalid HMAC signature")
            return jsonify({"error": "Invalid HMAC signature"}), 401

    try:
        data = json.loads(payload.decode('utf-8'))
        logger.debug(f"Webhook JSON: {json.dumps(data, indent=2)}")

        for notification in data.get('notificationItems', []):
            item = notification.get('NotificationRequestItem', {})
            event_code = item.get('eventCode')
            success = str(item.get('success')).lower() == 'true'
            session_reference = item.get('merchantReference', '')
            original_reference = session_reference.split('_')[0] if '_' in session_reference else session_reference

            if event_code == 'AUTHORISATION':
                if success:
                    logger.info(f"AUTHORISATION success for reference={original_reference}")
                    update_status_by_reference(original_reference, 'paid')
                else:
                    # Failed auth -> unlock so shopper can retry
                    logger.info(f"AUTHORISATION failed for reference={original_reference}; resetting to pending")
                    update_status_by_reference(original_reference, 'pending')

            # (Optional) handle OFFER_CLOSED / CANCELLATION / REFUND etc. as needed

        # Respond 200 so Adyen stops retrying
        return '[accepted]', 200

    except Exception as e:
        logger.exception("Error processing webhook")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    logger.info("Starting Flask application")
    app.run(debug=True)
