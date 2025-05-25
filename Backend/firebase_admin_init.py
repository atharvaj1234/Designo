# firebase_admin_init.py
import firebase_admin
from firebase_admin import credentials, auth, firestore
import datetime
import pytz
import os

# --- Local Imports ---
import config
# Import encryption/decryption from adk_utils now
from adk_utils import encrypt_api_key, decrypt_api_key

# --- Firebase Admin SDK Initialization ---
try:
    cred = credentials.Certificate(config.FIREBASE_SERVICE_ACCOUNT_KEY_PATH)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK initialized successfully.")
    else:
        print("Firebase Admin SDK already initialized.")

    # Get Firestore client instance (Synchronous)
    db = firestore.client()
    print("Firestore client initialized.")

    # Get Auth client instance
    firebase_auth = auth
    print("Firebase Auth client initialized.")

except Exception as e:
    print(f"Error initializing Firebase Admin SDK: {e}")
    if not config.FIREBASE_SERVICE_ACCOUNT_KEY_PATH:
        pass # Already handled in config.py
    else:
        raise # Re-raise other initialization errors


# --- Trial Tracking Logic ---

# Define the function that will run inside the transaction
# It accepts the transaction object as its first argument
# firebase_admin_init.py
# ... imports ...

@firestore.transactional
def _update_trial_usage_in_transaction(transaction, uid):
    """
    Internal function to be run within a transaction.
    Checks and updates the user's daily trial usage.
    Returns (can_proceed, message, user_api_key, requests_today).
    """
    users_ref = db.collection('users')
    user_doc_ref = users_ref.document(uid)

    user_doc = user_doc_ref.get(transaction=transaction)

    if not user_doc.exists:
        print(f"Error: User document not found for UID {uid} during trial processing.")
        return False, "Internal error: User data missing.", None, 0 # Return count 0

    data = user_doc.to_dict()
    last_reset_timestamp = data.get('last_reset_date')
    requests_today = data.get('requests_today', 0)
    encrypted_api_key = data.get('encrypted_api_key')

    utc_now = datetime.datetime.now(pytz.utc)
    today_utc = utc_now.date()

    TRIAL_LIMIT = int(os.getenv("MAX_TRIAL"))

    last_reset_date = None
    if last_reset_timestamp:
         try:
            last_reset_date = last_reset_timestamp.astimezone(pytz.utc).date()
         except ValueError:
            last_reset_date = last_reset_timestamp.replace(tzinfo=pytz.utc).astimezone(pytz.utc).date()
         except Exception as e:
            print(f"Warning: Could not convert timestamp {last_reset_timestamp} to date for UID {uid}: {e}")
            last_reset_date = None

    # Check if today is a new day (UTC) or if last_reset_date is missing/invalid
    if last_reset_date is None or last_reset_date < today_utc:
        requests_today = 0
        print(f"Resetting trial count for user {uid}. New day.")
        # Update last_reset_date in Firestore on reset
        transaction.set(user_doc_ref, {'last_reset_date': utc_now, 'requests_today': 0}, merge=True) # Reset count and date


    # --- Determine if request can proceed ---
    can_proceed = False
    message = ""
    decrypted_key = None # Initialize decrypted_key

    # Prioritize user's own API key if available
    if encrypted_api_key:
        decrypted_key = decrypt_api_key(encrypted_api_key)
        if decrypted_key:
            print(f"User {uid} has a valid API key stored. Proceeding with unlimited access.")
            can_proceed = True
            message = "Unlimited access with your key." # Simple message for success
            # No need to update Firestore count/date if using own key - depends on your logging needs
        else:
            print(f"Warning: Could not decrypt API key for user {uid}. Falling back to trials.")
            # Decrypted key is None, will fall through to trial check

    # If not proceeding with user's key (either no key or decryption failed), check trial limit
    if not can_proceed:
        if requests_today < TRIAL_LIMIT:
            # User is within the trial limit, increment and allow
            new_requests_today = requests_today + 1
            update_data = {
                'requests_today': new_requests_today
            }
            # Update only requests_today (last_reset_date was updated on new day reset or needs update on *any* trial use?)
            # Let's update last_reset_date on *any* trial use to be precise.
            update_data['last_reset_date'] = utc_now
            transaction.set(user_doc_ref, update_data, merge=True) # Increment count and update date
            print(f"User {uid} used trial {new_requests_today}/{TRIAL_LIMIT}.")
            can_proceed = True
            message = f"Trial used: {new_requests_today}/{TRIAL_LIMIT} today." # More informative message
            decrypted_key = None # Ensure no decrypted key is returned if using trial


        else:
            # User has exceeded the trial limit AND does not have a usable API key
            print(f"User {uid} exceeded trial limit ({TRIAL_LIMIT}) and no usable API key.")
            can_proceed = False
            message = f"You have used your {TRIAL_LIMIT} free trials for today. Provide your own Gemini API key for unlimited use." # Message for expired trial
            decrypted_key = None # Ensure no decrypted key is returned

    # Return the requests_today count regardless of outcome, so UI can display it
    return can_proceed, message, decrypted_key, requests_today


def process_daily_trial(uid: str) -> tuple[bool, str, str | None, int]:
    """
    Runs the transaction to check and update the user's daily trial usage or use their key.
    Returns (can_proceed, message, user_api_key, requests_today).
    """
    transaction = db.transaction()
    try:
        # Run the decorated function within the transaction
        can_proceed, message, decrypted_key, requests_today = _update_trial_usage_in_transaction(transaction, uid)
        return can_proceed, message, decrypted_key, requests_today
    except Exception as e:
        print(f"Error running trial usage transaction for user {uid}: {e}")
        # Handle potential Firestore errors during transaction execution
        # Return default count 0 on error
        return False, f"An error occurred while processing your trial count: {e}", None, 0


# ... rest of firebase_admin_init.py (create_user_doc_if_not_exists, store_encrypted_api_key, etc.) ...

# Define the function that will run inside the transaction for user doc creation
@firestore.transactional
def _create_user_doc_in_transaction(transaction, uid, email=None):
    """
    Internal function to be run within a transaction.
    Creates the user document in Firestore if it doesn't exist.
    Includes a placeholder for the API key.
    Returns True if created, False if already exists.
    """
    users_ref = db.collection('users')
    user_doc_ref = users_ref.document(uid)

    # Read the document within the transaction
    user_doc = user_doc_ref.get(transaction=transaction)

    if not user_doc.exists:
        print(f"User document not found for {uid}. Creating...")
        utc_now = datetime.datetime.now(pytz.utc)
        initial_data = {
            'requests_today': 0, # Start with 0 trials used for the day
            'last_reset_date': utc_now,
            'created_at': utc_now,
            'email': email, # Store email if available from token
            'encrypted_api_key': None # Add a field for the encrypted API key
        }
        # Use set within the transaction to create the document
        transaction.set(user_doc_ref, initial_data)
        print(f"User document created for {uid}.")
        return True # Indicate creation happened
    else:
        print(f"User document already exists for {uid}.")
        return False # Indicate document already existed

def create_user_doc_if_not_exists(uid: str, email: str | None = None) -> bool:
    """
    Runs the transaction to create the user document if it doesn't exist.
    Returns True if created, False if already exists.
    """
    transaction = db.transaction()
    try:
        # Run the decorated function within the transaction
        doc_created = _create_user_doc_in_transaction(transaction, uid, email)
        return doc_created
    except Exception as e:
        print(f"Error running transaction to create user doc for user {uid}: {e}")
        # Handle potential Firestore errors - Decide how to proceed
        # Returning False here means we can't confirm the doc exists.
        # The trial check transaction in process_daily_trial will handle the missing doc case.
        return False


# Function to store the encrypted API key
@firestore.transactional
def _store_api_key_in_transaction(transaction, uid, encrypted_key):
     """Internal function to store encrypted API key in user doc."""
     users_ref = db.collection('users')
     user_doc_ref = users_ref.document(uid)

     # Check if the document exists before attempting to set
     user_doc = user_doc_ref.get(transaction=transaction)
     if not user_doc.exists:
         print(f"Error: User document not found for UID {uid} when attempting to store API key.")
         # Could create it here, but it *should* have been created during auth exchange.
         # Let's just return False to indicate failure.
         return False

     update_data = {
         'encrypted_api_key': encrypted_key,
         # Optionally reset trials when user provides key? Depends on business logic.
         # 'requests_today': 0,
         # 'last_reset_date': datetime.datetime.now(pytz.utc),
     }
     transaction.set(user_doc_ref, update_data, merge=True)
     print(f"Stored encrypted API key for UID: {uid}")
     return True


def store_encrypted_api_key(uid: str, api_key: str) -> bool:
    """
    Encrypts the API key and stores it in the user's Firestore document.
    Returns True on success, False on failure.
    """
    encrypted_key = encrypt_api_key(api_key)
    if not encrypted_key:
        print("Failed to encrypt API key.")
        return False

    transaction = db.transaction()
    try:
        success = _store_api_key_in_transaction(transaction, uid, encrypted_key)
        return success
    except Exception as e:
        print(f"Error storing API key for user {uid}: {e}")
        return False



def verify_firebase_id_token(id_token: str) -> str | None:
    """
    Verifies the Firebase ID token (from signInWithCustomToken or initial client auth)
    and returns the user's UID if valid and active, None otherwise.
    """
    if not firebase_admin._apps:
         print("Firebase Admin SDK not initialized.")
         return None

    try:
        # verify_id_token is synchronous
        # check_revoked=True adds security against token revocation
        decoded_token = firebase_auth.verify_id_token(id_token, check_revoked=True)
        uid = decoded_token['uid']
        # Optional: Check if the user account is disabled - adds latency but security
        # user = firebase_auth.get_user(uid) # This adds a second call to Auth service
        # if user.disabled:
        #      print(f"User account {uid} is disabled.")
        #      return None
        print(f"Token verified for UID: {uid}")
        return uid
    except Exception as e:
        print(f"Firebase ID token verification failed: {e}")
        return None

def has_api_key_stored(uid: str) -> bool:
    """
    Checks if the user has an encrypted_api_key stored in their Firestore document.
    """
    if not firebase_admin._apps:
         print("Firebase Admin SDK not initialized.")
         return False
    try:
        users_ref = db.collection('users')
        user_doc_ref = users_ref.document(uid)
        user_doc = user_doc_ref.get(["encrypted_api_key"]) # Only fetch this specific field
        if user_doc.exists:
            data = user_doc.to_dict()
            # Check if the field exists and is not None/empty string
            encrypted_key = data.get('encrypted_api_key')
            return encrypted_key is not None and encrypted_key != ''
        else:
            # If the document doesn't exist, they certainly don't have a key stored
            return False
    except Exception as e:
        print(f"Error checking for API key for user {uid}: {e}")
        return False # Assume no key or error

# Export necessary items
__all__ = [
    "firebase_auth",
    "db",
    "process_daily_trial", # Updated return signature
    "verify_firebase_id_token",
    "create_user_doc_if_not_exists",
    "encrypt_api_key",
    "decrypt_api_key",
    "store_encrypted_api_key",
    "has_api_key_stored"
]