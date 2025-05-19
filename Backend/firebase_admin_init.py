# firebase_admin_init.py
import firebase_admin
from firebase_admin import credentials, auth, firestore
import datetime
import pytz

# --- Local Imports ---
import config

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
@firestore.transactional
def _update_trial_usage_in_transaction(transaction, uid):
    """
    Internal function to be run within a transaction.
    Checks and updates the user's daily trial usage.
    Returns (can_proceed, message).
    """
    users_ref = db.collection('users')
    user_doc_ref = users_ref.document(uid)

    # Read the document within the transaction
    user_doc = user_doc_ref.get(transaction=transaction)

    if not user_doc.exists:
        # This indicates an issue in the auth exchange flow or a user deleted their doc.
        # Cannot proceed with trial update if doc doesn't exist.
        print(f"Error: User document not found for UID {uid} during trial processing.")
        return False, "Internal error: User data missing."

    data = user_doc.to_dict()
    last_reset_timestamp = data.get('last_reset_date')
    requests_today = data.get('requests_today', 0) # Default to 0 if field is missing

    utc_now = datetime.datetime.now(pytz.utc)
    today_utc = utc_now.date()

    TRIAL_LIMIT = 3 # Define your daily limit

    last_reset_date = None
    if last_reset_timestamp:
         # Convert Firestore Timestamp to timezone-aware datetime, then to date
         # Use the correct method to get timezone-aware datetime from Timestamp
         # If timestamp is naive (common for older versions or direct sets), treat as UTC
         try:
            last_reset_date = last_reset_timestamp.astimezone(pytz.utc).date()
         except ValueError:
            # If astimezone fails (e.g., naive timestamp), assume UTC
            last_reset_date = last_reset_timestamp.replace(tzinfo=pytz.utc).astimezone(pytz.utc).date()
         except Exception as e:
            print(f"Warning: Could not convert timestamp {last_reset_timestamp} to date for UID {uid}: {e}")
            last_reset_date = None # Treat as if last_reset_date is missing


    # Check if today is a new day (UTC) or if last_reset_date is missing/invalid
    if last_reset_date is None or last_reset_date < today_utc:
        # Reset count for a new day
        requests_today = 0
        print(f"Resetting trial count for user {uid}. New day.")

    if requests_today < TRIAL_LIMIT:
        # User is within the trial limit, increment and allow
        new_requests_today = requests_today + 1
        update_data = {
            'last_reset_date': utc_now, # Store as Timestamp
            'requests_today': new_requests_today
        }
        # Use set with merge=True within the transaction to update the document
        transaction.set(user_doc_ref, update_data, merge=True)
        print(f"User {uid} used trial {new_requests_today}/{TRIAL_LIMIT}.")
        return True, "Trial used successfully."
    else:
        # User has exceeded the trial limit
        print(f"User {uid} exceeded trial limit ({TRIAL_LIMIT}).")
        return False, f"You have used your {TRIAL_LIMIT} free trials for today. Please try again tomorrow or consider subscribing for unlimited access."


def process_daily_trial(uid: str) -> tuple[bool, str]:
    """
    Runs the transaction to check and update the user's daily trial usage.
    Returns (can_proceed, message).
    """
    transaction = db.transaction()
    try:
        # Run the decorated function within the transaction
        can_proceed, message = _update_trial_usage_in_transaction(transaction, uid)
        return can_proceed, message
    except Exception as e:
        print(f"Error running trial usage transaction for user {uid}: {e}")
        # Handle potential Firestore errors during transaction execution
        return False, f"An error occurred while processing your trial count: {e}"


# Define the function that will run inside the transaction for user doc creation
@firestore.transactional
def _create_user_doc_in_transaction(transaction, uid, email=None):
    """
    Internal function to be run within a transaction.
    Creates the user document in Firestore if it doesn't exist.
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
            'email': email # Store email if available from token
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
        # Handle potential Firestore errors
        # Decide how to proceed on error - maybe return False and rely on trial check?
        # Returning False is safer, assumes doc doesn't exist or transaction failed.
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

# Export necessary items
__all__ = [
    "firebase_auth",
    "db",
    "process_daily_trial",
    "verify_firebase_id_token",
    "create_user_doc_if_not_exists", # Export the new function
]