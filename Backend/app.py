import os
import re
import base64
import asyncio
import uuid
import warnings
import logging
import io
from datetime import datetime, timedelta, timezone

# --- ADK Imports ---
from google.adk.agents import Agent
from google.adk.sessions import InMemorySessionService # For ephemeral ADK state
from google.adk.runners import Runner
from google.genai import types as google_genai_types
from google.adk.tools import google_search
import google.generativeai as genai

# --- Flask Imports ---
from flask import Flask, request, jsonify, redirect, session, url_for, make_response
# Removed CORS import: from flask_cors import CORS
from dotenv import load_dotenv

# --- Google Auth Imports ---
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials as GoogleCredentials
import google.auth.exceptions

# --- Firebase Admin SDK Imports ---
import firebase_admin
from firebase_admin import credentials as firebase_credentials
from firebase_admin import firestore
from google.cloud.firestore_v1.base_query import FieldFilter # For querying

# --- Configuration ---
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO)

load_dotenv()

# --- Firebase/Firestore Initialization ---
try:
    # Production (Cloud Run): Uses the service account associated with the instance
    # GOOGLE_APPLICATION_CREDENTIALS env var is automatically set by Cloud Run
    # or can be set manually for local testing pointing to your downloaded key file.
    # Use try-except to handle both local dev and cloud deployment
    if os.getenv('GOOGLE_APPLICATION_CREDENTIALS'):
        # Use explicit service account key file (for local dev or specific setups)
        cred = firebase_credentials.Certificate(os.getenv('GOOGLE_APPLICATION_CREDENTIALS'))
        logging.info("Initializing Firebase Admin SDK using GOOGLE_APPLICATION_CREDENTIALS.")
    else:
        # Use default credentials (suitable for Cloud Run with attached service account)
        cred = firebase_credentials.ApplicationDefault()
        logging.info("Initializing Firebase Admin SDK using Application Default Credentials.")

    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logging.info("Firebase Admin SDK initialized successfully. Firestore client created.")
    # Firestore collection names
    USERS_COLLECTION = "users"
    HISTORY_SUBCOLLECTION = "chat_history"

except Exception as e:
    logging.exception("CRITICAL: Failed to initialize Firebase Admin SDK!")
    # Depending on your deployment, you might want to exit or handle this differently
    db = None # Ensure db is None if initialization fails

# --- OAuth 2.0 Configuration ---
CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI") # e.g., https://your-firebase-hosting-url.web.app/oauth2callback
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")

if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, FLASK_SECRET_KEY]):
    raise ValueError("Missing one or more required OAuth/Flask environment variables.")
if not db:
     raise RuntimeError("Firestore client failed to initialize. Cannot start application.")

SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
]

# Define where your frontend UI is running (for redirect after OAuth)
# Should be your Firebase Hosting URL or the URL Figma uses for the plugin UI
FRONTEND_URL = os.getenv("FRONTEND_URL", "/") # Default to root of hosting URL

# --- Flask App Setup ---
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
# Configure secure cookies for production (Cloud Run typically runs behind HTTPS)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = True # Assume HTTPS in production
app.config['SESSION_COOKIE_HTTPONLY'] = True

# CORS is removed - Configure at Firebase Hosting or Cloud Run ingress level if needed

# --- ADK Session Service (For ephemeral ADK agent state) ---
adk_session_service = InMemorySessionService()
APP_NAME = "figma_ai_assistant"
AGENT_MODEL_NAME = "gemini-1.5-flash-latest"

# --- Agent Definitions (Keep Templates) ---
# (Paste your create_agent, modify_agent, answer_agent definitions here as before)
# Make sure their 'model' attribute is set to AGENT_MODEL_NAME (string)
create_agent = Agent(
    name="svg_creator_agent_v1",
    model=AGENT_MODEL_NAME, # Model instance will be provided per-request
    description="Generates SVG code for UI designs based on textual descriptions.",
    instruction="""
    # --- PASTE YOUR DETAILED CREATE AGENT INSTRUCTION HERE ---
    # (Example instruction, replace with your full one)
    You are a UI/UX Designer AI. Create SVG code for the described UI element.
    **Mandatory Requirements:**
    *   Output ONLY valid, well-formed SVG code. No surrounding text.
    *   Set width="390" for mobile or width="1440" for desktop/web. Height should fit content.
    *   Use descriptive kebab-case group IDs.
    *   Use <circle> with fill="#CCCCCC" for icons. Use <rect> with fill="#E0E0E0" for images.
    *   Use <text> for text, specify font-family, font-size, font-weight, fill. Use text-anchor.
    *   Ensure WCAG AA contrast. Rounded corners, modern aesthetics. Optimize for Figma.
    """,
    tools=[],
)
modify_agent = Agent(
    name="svg_modifier_agent_v1",
    model=AGENT_MODEL_NAME, # Model instance will be provided per-request
    description="Modifies a specific element within a UI design based on textual instructions and image context, outputting SVG for the modified element.",
    instruction="""
    # --- PASTE YOUR DETAILED MODIFY AGENT INSTRUCTION HERE ---
    # (Example instruction, replace with your full one)
    You are an expert Figma UI/UX designer modifying a specific element.
    Context Provided: User prompt, Frame Name, Element Name/Type/Dimensions, Full Frame Image, Specific Element Image.
    Task: Recreate ONLY the specified element as SVG, incorporating changes. Maintain original dimensions unless requested otherwise.
    Response Format:
    *   Output ONLY the raw, valid SVG code for the MODIFIED element.
    *   Set viewBox, width, height matching the original element context.
    *   NO introductory text or markdown.
    *   Use placeholders for images/icons if needed.
    """,
    tools=[],
)
answer_agent = Agent(
    name="answer_agent_v1",
    model=AGENT_MODEL_NAME, # Model instance will be provided per-request
    description="Answers user questions by searching the internet for relevant and up-to-date information.",
    instruction="""
    # --- PASTE YOUR DETAILED ANSWER AGENT INSTRUCTION HERE ---
    # (Example instruction, replace with your full one)
    You are "Design Buddy", a helpful AI Design Assistant. Answer design-related questions. Use the web search tool for current info, inspiration, resources. Be conversational, friendly, multi-lingual. Provide links when citing sources. Summarize long answers. Ask clarifying questions. If you don't know, search, or admit it.
    """,
    tools=[google_search],
)
logging.info("Agent templates defined.")

# --- Firestore Helper Functions ---

def save_user_data(user_id, credentials_obj=None, user_info_dict=None):
    """Saves/updates user credentials and profile info in Firestore."""
    if not db: return False
    doc_ref = db.collection(USERS_COLLECTION).document(user_id)
    data_to_merge = {}
    if user_info_dict:
        data_to_merge['user_info'] = user_info_dict
        data_to_merge['last_login'] = firestore.SERVER_TIMESTAMP # Track last login

    if credentials_obj:
        # Convert credentials object to Firestore-compatible dictionary
        expiry_ts = None
        if credentials_obj.expiry:
            # Ensure expiry is timezone-aware UTC before storing
            expiry_utc = credentials_obj.expiry.astimezone(timezone.utc) if credentials_obj.expiry.tzinfo else credentials_obj.expiry.replace(tzinfo=timezone.utc)
            expiry_ts = firestore.SERVER_TIMESTAMP if expiry_utc <= datetime.now(timezone.utc) else expiry_utc # Use server timestamp if already expired

        data_to_merge['credentials'] = {
            'token': credentials_obj.token,
            'refresh_token': credentials_obj.refresh_token,
            'token_uri': credentials_obj.token_uri,
            'client_id': credentials_obj.client_id,
            'client_secret': credentials_obj.client_secret, # Store carefully - consider encryption
            'scopes': credentials_obj.scopes,
            # Store expiry as Firestore Timestamp or ISO string
            'expiry': expiry_ts,
        }
        data_to_merge['credentials_updated_at'] = firestore.SERVER_TIMESTAMP

    try:
        doc_ref.set(data_to_merge, merge=True) # Use set with merge=True to update/create
        logging.info(f"Successfully saved data for user {user_id}.")
        return True
    except Exception as e:
        logging.error(f"Error saving data for user {user_id} to Firestore: {e}")
        return False

def get_user_credentials_from_db(user_id):
    """Retrieves user credentials map from Firestore."""
    if not db: return None
    doc_ref = db.collection(USERS_COLLECTION).document(user_id)
    try:
        doc = doc_ref.get()
        if doc.exists:
            creds_map = doc.to_dict().get('credentials')
            if creds_map and 'expiry' in creds_map:
                 # Convert Firestore Timestamp back to datetime if needed by GoogleCredentials
                 # google-auth library often handles this conversion automatically
                 if isinstance(creds_map['expiry'], datetime):
                      # Ensure timezone-aware (Firestore timestamps are UTC)
                      creds_map['expiry'] = creds_map['expiry'].replace(tzinfo=timezone.utc)
                 # Handle potential null expiry
                 elif creds_map['expiry'] is None:
                      creds_map['expiry'] = None
            return creds_map
        else:
            logging.warning(f"Credentials document not found for user {user_id}.")
            return None
    except Exception as e:
        logging.error(f"Error getting credentials for user {user_id} from Firestore: {e}")
        return None

def get_user_info_from_db(user_id):
    """Retrieves user profile info map from Firestore."""
    if not db: return None
    doc_ref = db.collection(USERS_COLLECTION).document(user_id)
    try:
        doc = doc_ref.get()
        if doc.exists:
            return doc.to_dict().get('user_info')
        else:
            logging.warning(f"User info document not found for user {user_id}.")
            return None
    except Exception as e:
        logging.error(f"Error getting user info for user {user_id} from Firestore: {e}")
        return None

def save_chat_turn(user_id, turn_data):
    """Adds a chat turn document to the user's history subcollection."""
    if not db: return False
    if not turn_data or 'role' not in turn_data or 'parts' not in turn_data:
         logging.error(f"Invalid turn data for user {user_id}: {turn_data}")
         return False
    history_ref = db.collection(USERS_COLLECTION).document(user_id).collection(HISTORY_SUBCOLLECTION)
    try:
        # Add timestamp for ordering
        turn_data_with_ts = {**turn_data, 'timestamp': firestore.SERVER_TIMESTAMP}
        history_ref.add(turn_data_with_ts) # add() generates a unique doc ID
        # Optional: Implement history pruning logic here if needed
        logging.debug(f"Saved chat turn for user {user_id}.")
        return True
    except Exception as e:
        logging.error(f"Error saving chat turn for user {user_id} to Firestore: {e}")
        return False

def get_chat_history_from_db(user_id, limit=20):
    """Retrieves the last N chat turns from Firestore, ordered by timestamp."""
    if not db: return []
    history_ref = db.collection(USERS_COLLECTION).document(user_id).collection(HISTORY_SUBCOLLECTION)
    try:
        query = history_ref.order_by('timestamp', direction=firestore.Query.DESCENDING).limit(limit)
        docs = query.stream()
        # Firestore returns newest first, so reverse to get chronological order for the agent
        history = [doc.to_dict() for doc in docs][::-1]
        # Remove timestamp from history sent to agent if not needed
        for turn in history:
            turn.pop('timestamp', None)
        logging.debug(f"Retrieved {len(history)} chat turns for user {user_id}.")
        return history
    except Exception as e:
        logging.error(f"Error getting chat history for user {user_id} from Firestore: {e}")
        return []

# --- Helper to Build Credentials Object ---
def build_credentials_obj(creds_map):
    """Builds GoogleCredentials object from Firestore map."""
    if not creds_map: return None
    try:
        # google-auth library handles expiry conversion (Timestamp -> datetime)
        return GoogleCredentials(**creds_map)
    except Exception as e:
        logging.error(f"Error building Credentials object from map: {e}")
        return None

# --- Helper Function to Build Flow (Unchanged) ---
def build_flow():
     # ... (same as before) ...
     client_config = {
         "web": {
             "client_id": CLIENT_ID,
             "client_secret": CLIENT_SECRET,
             "auth_uri": "https://accounts.google.com/o/oauth2/auth",
             "token_uri": "https://oauth2.googleapis.com/token",
             "redirect_uris": [REDIRECT_URI],
             "javascript_origins": [FRONTEND_URL] # Important for CORS/Security
         }
     }
     return Flow.from_client_config(client_config=client_config, scopes=SCOPES, redirect_uri=REDIRECT_URI)

# --- Helper to Validate SVG (Unchanged) ---
def is_valid_svg(svg_string):
    """Validates if string looks like SVG, stripping common markdown."""
    if not svg_string or not isinstance(svg_string, str): return False
    svg_clean = re.sub(r'^\s*```(?:svg|xml)?\s*', '', svg_string.strip(), flags=re.IGNORECASE)
    svg_clean = re.sub(r'\s*```\s*$', '', svg_clean, flags=re.IGNORECASE)
    return svg_clean.strip().startswith('<svg') and svg_clean.strip().endswith('>')

# --- Helper to Run ADK Interaction (Modified for Firestore credential update) ---
async def run_adk_interaction_with_auth(agent_template, user_credentials_obj, user_content, user_id, chat_history_for_agent):
    """Runs ADK agent, handling credential refresh and updating Firestore."""
    # ... (rest of the function preamble - adk_run_session_id etc.) ...
    final_response_text = None
    adk_run_session_id = f"adk_{uuid.uuid4()}"

    if not user_credentials_obj:
        return "AUTH_ERROR: Missing user credentials object."

    # --- Refresh Token if Necessary ---
    try:
        if user_credentials_obj.expired and user_credentials_obj.refresh_token:
            logging.info(f"Credentials expired for user {user_id}. Refreshing...")
            try:
                auth_request = GoogleAuthRequest()
                user_credentials_obj.refresh(auth_request)
                logging.info(f"Credentials successfully refreshed for user {user_id}.")
                # *** IMPORTANT: Update stored credentials in Firestore ***
                if not save_user_data(user_id, credentials_obj=user_credentials_obj):
                     logging.error(f"CRITICAL: Failed to save refreshed credentials to Firestore for user {user_id}!")
                     # Decide how to proceed - maybe return error, maybe continue with in-memory creds
                     # For safety, let's return an error to alert ops.
                     return "AUTH_ERROR: Token refreshed but failed to save to DB."

            except google.auth.exceptions.RefreshError as re:
                logging.error(f"Failed to refresh token for user {user_id}: {re}. Clearing session.")
                session.clear() # Clear Flask session to force re-login
                # Optionally delete the user doc or mark creds as invalid in Firestore
                return "AUTH_ERROR: Token refresh failed. Please sign in again."
    except Exception as e:
        logging.error(f"Error during credential refresh check for user {user_id}: {e}")
        return f"AUTH_ERROR: Could not verify credential status: {e}"

    # --- Instantiate Model and Agent ---
    # ... (same logic as before using user_credentials_obj) ...
    try:
        user_model = genai.GenerativeModel(
            agent_template.model, # Use model name from template
            credentials=user_credentials_obj
            # Add safety_settings, generation_config if needed globally
        )
        agent_instance = Agent(
            name=agent_template.name,
            model=user_model, # Pass the model instance with credentials
            description=agent_template.description,
            instruction=agent_template.instruction,
            tools=agent_template.tools,
            # Add generate_content_config if defined in template
        )
        logging.info(f"Instantiated agent '{agent_instance.name}' for user {user_id}.")
    except Exception as e:
        logging.error(f"Failed to initialize model or agent for user {user_id}: {e}")
        return f"AGENT_INIT_ERROR: {e}"

    # --- Create ADK Session & Load History ---
    # ... (same logic as before using adk_session_service and chat_history_for_agent) ...
    try:
        adk_internal_session = adk_session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=adk_run_session_id
        )
        # Add chat history to the ADK session *if the agent needs it*
        if chat_history_for_agent:
             for turn in chat_history_for_agent:
                 # Assuming history format is [{'role': 'user'/'model', 'parts': [{'text': ...}]}]
                 if 'role' in turn and 'parts' in turn:
                     adk_internal_session.add_history(google_genai_types.Content(role=turn['role'], parts=turn['parts']))
                 elif 'user' in turn and 'AI' in turn: # Adapt old format if needed
                      adk_internal_session.add_history(google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=turn['user'])]))
                      adk_internal_session.add_history(google_genai_types.Content(role='model', parts=[google_genai_types.Part(text=turn['AI'])]))
    except Exception as e:
         logging.error(f"Failed to create ADK session storage for {adk_run_session_id}: {e}")
         return f"ADK_SESSION_ERROR: {e}"

    # --- Run the ADK Runner ---
    # ... (same logic as before using runner.run) ...
    runner = Runner(
        agent=agent_instance, # Use the instance with user credentials
        app_name=APP_NAME,
        session_service=adk_session_service # Use the service for ephemeral state
    )
    logging.info(f"Running agent '{agent_instance.name}'...")
    try:
        final_event = await runner.run(
            user_id=user_id,
            session_id=adk_run_session_id, # Use the specific ID for this run
            user_input=user_content,
            # History is already loaded into the adk_internal_session if needed
        )

        if final_event and final_event.is_final_response():
             if final_event.content and final_event.content.parts:
                 final_response_text = final_event.content.parts[0].text
             elif final_event.actions and final_event.actions.escalate:
                 error_msg = f"Agent escalated: {final_event.error_message or 'No specific message.'}"
                 logging.error(f"  ERROR for user {user_id}: {error_msg}")
                 final_response_text = f"AGENT_ERROR: {error_msg}"
             else:
                 # Handle cases where final response is empty but not an error
                 final_response_text = ""
        elif final_event and final_event.actions and final_event.actions.escalate:
            # Escalation occurred before a final response message
            error_msg = f"Agent escalated early: {final_event.error_message or 'No specific message.'}"
            logging.error(f"  ERROR for user {user_id}: {error_msg}")
            final_response_text = f"AGENT_ERROR: {error_msg}"
        else:
             logging.warning(f"Agent run for user {user_id} finished without a clear final response or escalation.")
             final_response_text = "AGENT_ERROR: Agent did not produce a final response."
    except Exception as e:
        logging.exception(f"Exception during ADK run for agent '{agent_instance.name}', user '{user_id}':")
        final_response_text = f"ADK_RUNTIME_ERROR: {e}"
    finally:
        # Clean up ephemeral ADK session data
        try:
            adk_session_service.delete_session(app_name=APP_NAME, user_id=user_id, session_id=adk_run_session_id)
        except Exception as delete_err:
            logging.warning(f"Failed to delete temporary ADK session '{adk_run_session_id}': {delete_err}")

    logging.info(f"Agent '{agent_instance.name}' for user {user_id} finished.")
    return final_response_text


# --- OAuth Routes (Modified for Firestore & Flask Session for User ID only) ---

@app.route('/authorize')
def authorize():
    # ... (same as before: build flow, generate URL, store state in session, redirect) ...
    flow = build_flow()
    authorization_url, state = flow.authorization_url(access_type='offline', prompt='consent', include_granted_scopes='true')
    session['oauth_state'] = state # State still needed in Flask session for CSRF check
    logging.info(f"Redirecting user to Google for authorization.")
    return redirect(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    # --- State Validation ---
    state = session.get('oauth_state')
    if not state or state != request.args.get('state'):
        logging.error("OAuth callback state mismatch.")
        return jsonify({"success": False, "error": "Invalid state parameter."}), 400
    session.pop('oauth_state', None) # State verified

    flow = build_flow()
    try:
        # --- Fetch Tokens ---
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials # This is a GoogleCredentials object
        logging.info("Successfully fetched token from Google.")

        # --- Get User Info from ID Token ---
        try:
            id_info = id_token.verify_oauth2_token(
                credentials.id_token, GoogleAuthRequest(), credentials.client_id
            )
            user_id = id_info['sub'] # Google's unique ID - USE THIS AS FIRESTORE DOC ID
            user_info = { # Store basic profile info
                'id': user_id,
                'email': id_info.get('email'),
                'name': id_info.get('name'),
                'picture': id_info.get('picture'),
            }
            logging.info(f"User {user_info.get('email')} ({user_id}) authenticated via token.")
        except Exception as e:
            logging.error(f"Failed to verify ID token or get user info: {e}")
            return jsonify({"success": False, "error": f"Failed to verify user identity: {e}"}), 500

        # --- Save User Data and Credentials to Firestore ---
        if not save_user_data(user_id, credentials_obj=credentials, user_info_dict=user_info):
             # Handle DB save error - critical
             logging.error(f"Failed to save initial data for user {user_id} to Firestore.")
             return jsonify({"success": False, "error": "Failed to save user session to database."}), 500

        # --- Store ONLY user_id in Flask Session ---
        # This cookie links the browser to the Firestore user document
        session.clear() # Clear any old session data first
        session['user_id'] = user_id
        session.permanent = True # Make the session last longer (e.g., 30 days)
        app.permanent_session_lifetime = timedelta(days=30) # Configure lifetime

        logging.info(f"User {user_id} session established.")

        # --- Redirect back to the Frontend ---
        return redirect(FRONTEND_URL) # Redirect to the main UI page

    except google.auth.exceptions.FlowError as e:
        logging.error(f"OAuth flow error during token fetch: {e}")
        return jsonify({"success": False, "error": f"Authentication flow failed: {e}"}), 500
    except Exception as e:
        logging.exception("An unexpected error occurred during OAuth callback:")
        return jsonify({"success": False, "error": f"An unexpected server error occurred: {e}"}), 500


@app.route('/api/auth/status')
def auth_status():
    """Checks if user has a valid session cookie and retrieves info from DB."""
    user_id = session.get('user_id')
    if user_id:
        user_info = get_user_info_from_db(user_id)
        if user_info:
            logging.info(f"Auth status check: User {user_id} is logged in.")
            return jsonify({"isLoggedIn": True, "userInfo": user_info})
        else:
            # Session exists but user not found in DB (edge case, maybe clean up?)
            logging.warning(f"Auth status check: Session found for user {user_id}, but no DB record. Clearing session.")
            session.clear()
            return jsonify({"isLoggedIn": False})
    else:
        logging.info("Auth status check: No user session found.")
        return jsonify({"isLoggedIn": False})


@app.route('/logout', methods=['POST'])
def logout():
    """Clears the Flask session cookie."""
    user_id = session.get('user_id')
    user_email = "Unknown"
    if user_id:
         user_info = get_user_info_from_db(user_id) # Get info for logging before clear
         if user_info: user_email = user_info.get('email', user_id)

    session.clear() # Remove the session cookie
    logging.info(f"User {user_email} ({user_id}) logged out.")
    # Optional: Add logic here to revoke Google token if necessary
    return jsonify({"success": True, "message": "Logged out successfully."})


# --- Main API Endpoint (Modified for Firestore) ---
@app.route('/chat', methods=['POST'])
async def handle_chat():
    """Handles user prompts using ADK agents, requires session cookie, uses Firestore."""
    # --- Authentication Check (using Flask session for user_id) ---
    user_id = session.get('user_id')
    if not user_id:
        logging.warning("Access denied to /chat: No user session.")
        return jsonify({"success": False, "error": "Authentication required."}), 401

    # --- Get User Credentials from Firestore ---
    user_credentials_map = get_user_credentials_from_db(user_id)
    if not user_credentials_map:
        logging.error(f"Credentials not found in DB for user {user_id}. Clearing session.")
        session.clear() # Log out user if DB record is missing
        return jsonify({"success": False, "error": "Session invalid. Please log in again."}), 401

    user_credentials_obj = build_credentials_obj(user_credentials_map)
    if not user_credentials_obj:
        logging.error(f"Failed to build credentials object for user {user_id}. Clearing session.")
        session.clear()
        return jsonify({"success": False, "error": "Invalid session credentials. Please log in again."}), 401

    user_email = get_user_info_from_db(user_id).get('email', user_id) # For logging

    # --- Request Parsing ---
    # ... (same as before: get JSON, extract prompt, mode, context, images) ...
    if not request.is_json: return jsonify({"success": False, "error": "Request must be JSON"}), 400
    data = request.get_json()
    # ... extract data ...
    mode = data.get('mode')
    user_prompt_text = data.get('userPrompt')
    context = data.get('context', {})
    frame_data_base64 = data.get('frameDataBase64') # For modify
    element_data_base64 = data.get('elementDataBase64') # For modify

    if not user_prompt_text or not mode:
        return jsonify({"success": False, "error": "Missing 'userPrompt' or 'mode'"}), 400
    if mode not in ['create', 'modify', 'answer']:
        return jsonify({"success": False, "error": f"Invalid mode: {mode}"}), 400

    logging.info(f"Received /chat request from user '{user_email}'. Mode: '{mode}'. Prompt: '{user_prompt_text[:50]}...'")

    # --- Get Chat History from Firestore ---
    MAX_HISTORY_TURNS = 10 # Number of pairs (user+model)
    user_chat_history = get_chat_history_from_db(user_id, limit=MAX_HISTORY_TURNS * 2)

    # --- Prepare Agent Input ---
    # ... (same logic as before to select agent_template, create agent_input_content based on mode) ...
    agent_template = None
    agent_input_content = None
    requires_history = False
    try:
        if mode == 'create':
            agent_template = create_agent
            # Create agent doesn't usually need history, just the prompt
            prompt_for_agent = user_prompt_text # Maybe add context text?
            if context.get('frameName'):
                 prompt_for_agent = f"Design for frame '{context['frameName']}'.\nUser Request: {user_prompt_text}"

            agent_input_parts = [google_genai_types.Part(text=prompt_for_agent)]
            agent_input_content = google_genai_types.Content(role='user', parts=agent_input_parts)
        elif mode == 'modify':
            agent_template = modify_agent
            # Modify agent needs prompt, context text, and images
            if not frame_data_base64 or not element_data_base64 or not context.get('element'):
               raise ValueError("Missing image data or element context for modify mode")
            element_info = context.get('element', {})
            prompt_for_agent = f"""
Modification Request: {user_prompt_text}

Figma Context:
Frame Name: {context.get('frameName', 'N/A')}
Element Name: {element_info.get('name', 'N/A')}
Element Type: {element_info.get('type', 'N/A')}
Element Dimensions: {element_info.get('width')}x{element_info.get('height')}
"""
            message_parts = [google_genai_types.Part(text=prompt_for_agent)]
            try:
               frame_bytes = base64.b64decode(frame_data_base64)
               element_bytes = base64.b64decode(element_data_base64)
               message_parts.append(google_genai_types.Part(inline_data=google_genai_types.Blob(mime_type="image/png", data=frame_bytes)))
               message_parts.append(google_genai_types.Part(inline_data=google_genai_types.Blob(mime_type="image/png", data=element_bytes)))
               logging.info(f"Image parts prepared for modify agent (user {user_email}).")
            except Exception as e:
               raise ValueError(f"Invalid image data received: {e}")

            agent_input_content = google_genai_types.Content(role='user', parts=message_parts)
        elif mode == 'answer':
            agent_template = answer_agent
            requires_history = True
            # Simple prompt, history handled by run_adk_interaction_with_auth
            agent_input_parts = [google_genai_types.Part(text=user_prompt_text)]
            agent_input_content = google_genai_types.Content(role='user', parts=agent_input_parts)
        else:
            raise ValueError(f"Invalid mode: {mode}")

    except ValueError as ve:
         # ... handle input validation error ...
         logging.warning(f"Input validation error for user {user_email}: {ve}")
         # Return 200 OK with error for UI display
         return jsonify({"success": False, "error": str(ve)}), 200
    except Exception as e:
         # ... handle other preparation errors ...
         logging.exception(f"Error preparing agent input for user {user_email}:")
         return jsonify({"success": False, "error": "Internal server error preparing request."}), 500
    
    # --- Execute Agent ---
    history_for_agent_run = user_chat_history if requires_history else []
    final_result = await run_adk_interaction_with_auth(
        agent_template=agent_template,
        user_credentials_obj=user_credentials_obj, # Pass the Credentials object
        user_content=agent_input_content,
        user_id=user_id,
        chat_history_for_agent=history_for_agent_run
    )
    
    # --- Process Result & Handle Errors ---
    if not final_result or final_result.startswith(("AGENT_ERROR:", "ADK_RUNTIME_ERROR:", "AUTH_ERROR:", "AGENT_INIT_ERROR:")):
        error_msg = f"Agent execution failed for user {user_email}. Details: {final_result}"
        logging.error(error_msg)
        # Don't save this failed turn to history
        # Return 200 OK for UI display, even on agent/auth errors during run
        return jsonify({"success": False, "error": final_result or "Agent failed to produce a result."}), 200
    
    # --- Save Successful Turn to Firestore History ---
    user_turn = {'role': 'user', 'parts': [{'text': user_prompt_text}]}
    model_turn = {'role': 'model', 'parts': [{'text': final_result}]}
    save_chat_turn(user_id, user_turn)
    save_chat_turn(user_id, model_turn)
    # History is now saved persistently
    
    # --- Format and Return Success Response ---
    if mode == 'create' or mode == 'modify':
        if not is_valid_svg(final_result):
            error_msg = f"Agent returned invalid SVG for mode '{mode}' (user {user_email}). Snippet: {final_result[:200]}..."
            logging.error(error_msg)
            # Even though agent succeeded, the output is bad - return error
            return jsonify({"success": False, "error": "Agent returned invalid SVG output. Please try again or rephrase."}), 200
        else:
            # Clean potential markdown just in case validation missed it
            cleaned_svg = re.sub(r'^\s*```(?:svg|xml)?\s*', '', final_result.strip(), flags=re.IGNORECASE)
            cleaned_svg = re.sub(r'\s*```\s*$', '', cleaned_svg, flags=re.IGNORECASE)
            logging.info(f"Returning successful SVG response for mode '{mode}' (user {user_email}).")
            return jsonify({"success": True, "svg": cleaned_svg, "mode": mode})
    elif mode == 'answer':
        logging.info(f"Returning successful Answer response for user {user_email}.")
        return jsonify({"success": True, "answer": final_result, "mode": "answer"})
    else:
        # Should not happen
        logging.error(f"Internal logic error: Reached end of /chat with unhandled mode '{mode}' (user {user_email}).")
        return jsonify({"success": False, "error": "Internal server error: Unknown result type."}), 500
    
#--- Entry Point for Gunicorn (and local dev) ---
if __name__ == '__main__':
    # This block is mainly for local development running python app.py
    # Gunicorn will bypass this block when it imports 'app'
    logging.info("Starting Flask development server...")
    # For local dev, specify host/port. Gunicorn uses command-line args.
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)), debug=False) # Use PORT env var (like Cloud Run) or default 8080, disable debug for prod-like testing