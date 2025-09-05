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

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)

# Adyen configuration
logger.info("Initializing Adyen client")
adyen = Adyen.Adyen()
adyen.checkout.client.xapikey = os.getenv('ADYEN_API_KEY')
adyen.checkout.client.platform = 'test'  # Change to 'live' for production
MERCHANT_ACCOUNT = os.getenv('ADYEN_MERCHANT_ACCOUNT')
CLIENT_KEY = os.getenv('ADYEN_CLIENT_KEY')
HMAC_KEY = os.getenv('ADYEN_HMAC_KEY')

logger.info(f"Adyen API Key: {os.getenv('ADYEN_API_KEY')[:4]}**** (masked)")
logger.info(f"Merchant Account: {MERCHANT_ACCOUNT}")
logger.info(f"Client Key: {CLIENT_KEY[:4]}**** (masked)")
logger.info(f"HMAC Key: {HMAC_KEY[:4]}**** (masked)")

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

@app.route('/admin', methods=['GET', 'POST'])
def admin_form():
    if request.method == 'POST':
        logger.info("Processing admin form submission")
        try:
            price = float(request.form['price']) * 100  # Convert to minor units
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
                logger.error(f"Reference {reference} already exists")
                conn.close()
                return jsonify({"error": "Reference must be unique"}), 400
            conn.close()
            
            checkout_url = f"http://127.0.0.1:5000/checkout?paymentId={payment_id}"
            logger.info(f"Generated checkout URL: {checkout_url}")
            
            return jsonify({"message": "Payment link generated", "url": checkout_url})
        except Exception as e:
            logger.error(f"Error in admin form: {str(e)}", exc_info=True)
            return jsonify({"error": str(e)}), 500
    
    return render_template('form.html')

@app.route('/checkout')
def checkout_page():
    payment_id = request.args.get('paymentId')
    logger.info(f"Accessing checkout page with paymentId={payment_id}")
    
    if not payment_id:
        logger.error("No paymentId provided in checkout request")
        return render_template('message.html', message="Invalid payment ID"), 400
    
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM payments WHERE id = ?', (payment_id,))
        payment = cursor.fetchone()
        conn.close()
        
        if not payment:
            logger.error(f"Payment not found for paymentId={payment_id}")
            return render_template('message.html', message="Payment not found"), 404
        
        id, amount, currency, reference, status, country, expires_at_str = payment
        expires_at = datetime.fromisoformat(expires_at_str)
        
        logger.info(f"Payment details: ID={id}, Amount={amount}, Currency={currency}, Reference={reference}, Country={country}, Status={status}, Expires={expires_at}")
        
        if status != 'pending' or datetime.now() > expires_at:
            status_message = "This payment link has expired or already been paid" if status != 'pending' else "This payment link has expired"
            logger.warning(f"Invalid payment state: {status_message}")
            return render_template('message.html', message=status_message), 403
        
        # Create a new Adyen session for each valid visit
        session_reference = f"{reference}_{str(uuid.uuid4())[:8]}"  # Unique per session attempt
        request_data = {
            "amount": {"value": amount, "currency": currency},
            "reference": session_reference,
            "merchantAccount": MERCHANT_ACCOUNT,
            "returnUrl": f"http://127.0.0.1:5000/result?paymentId={payment_id}",
            "countryCode": country
        }
        
        logger.debug(f"Creating Adyen session with request: {json.dumps(request_data, indent=2)}")
        
        try:
            result = adyen.checkout.payments_api.sessions(request_data)
            logger.debug(f"Adyen session response: {json.dumps(result.message, indent=2)}")
            session_id = result.message['id']
            session_data = result.message['sessionData']
        except Exception as e:
            logger.error(f"Error creating Adyen session: {str(e)}", exc_info=True)
            return render_template('message.html', message=f"Error creating session: {str(e)}"), 500
        
        logger.info(f"Session created: session_id={session_id}")
        return render_template('checkout.html', client_key=CLIENT_KEY, session_id=session_id, session_data=session_data)
    
    except Exception as e:
        logger.error(f"Error in checkout page: {str(e)}", exc_info=True)
        return render_template('message.html', message=f"Error: {str(e)}"), 500

@app.route('/result')
def result_page():
    payment_id = request.args.get('paymentId')
    logger.info(f"Redirect to result page for paymentId={payment_id}")
    return render_template('message.html', message="Payment processing... Status will be updated via webhook.")

@app.route('/webhook', methods=['POST'])
def webhook():
    logger.info("Received webhook request")
    payload = request.get_data()
    signature = request.headers.get('hmac-signature', '')
    
    logger.debug(f"Webhook payload: {payload}")
    logger.debug(f"Webhook HMAC signature: {signature}")
    
    # Verify HMAC
    computed_signature = base64.b64encode(
        hmac.new(
            HMAC_KEY.encode('utf-8'),
            payload,
            hashlib.sha256
        ).digest()
    ).decode('utf-8')
    
    if not hmac.compare_digest(computed_signature, signature):
        logger.error("Invalid HMAC signature")
        return jsonify({"error": "Invalid HMAC signature"}), 401
    
    try:
        data = json.loads(payload)
        logger.debug(f"Webhook data: {json.dumps(data, indent=2)}")
        
        # Process notifications
        for notification in data.get('notificationItems', []):
            item = notification['NotificationRequestItem']
            if item['eventCode'] == 'AUTHORISATION' and item['success'] == 'true':
                session_reference = item['merchantReference']
                # Extract original reference (before the UUID suffix)
                original_reference = session_reference.split('_')[0]
                
                logger.info(f"Processing AUTHORISATION webhook for session_reference={session_reference}, original_reference={original_reference}")
                
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()
                cursor.execute('SELECT status FROM payments WHERE reference = ?', (original_reference,))
                payment = cursor.fetchone()
                
                if payment and payment[0] == 'pending':
                    cursor.execute('UPDATE payments SET status = ? WHERE reference = ?', ('paid', original_reference))
                    conn.commit()
                    logger.info(f"Updated payment status to 'paid' for reference={original_reference}")
                else:
                    logger.warning(f"Payment already processed or not found for reference={original_reference}")
                
                conn.close()
        
        return '[accepted]', 202
    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    logger.info("Starting Flask application")
    app.run(debug=True)