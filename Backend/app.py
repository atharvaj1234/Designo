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
from google.adk.sessions import InMemorySessionService # Still used for per-request ADK state
from google.adk.runners import Runner
from google.genai import types as google_genai_types
from google.adk.tools import google_search
import google.generativeai as genai # Import for model instantiation

# --- Flask Imports ---
from flask import Flask, request, jsonify, redirect, session, url_for, make_response
from flask_cors import CORS
from dotenv import load_dotenv

# --- Google Auth Imports ---
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleAuthRequest # Alias to avoid clash
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials as GoogleCredentials # Alias
import google.auth.exceptions

# --- Configuration ---
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO) # Use INFO for better debugging during dev

load_dotenv()

# --- OAuth 2.0 Configuration ---
CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI") # e.g., http://localhost:5001/oauth2callback
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")

if not all([CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, FLASK_SECRET_KEY]):
    raise ValueError("Missing one or more required environment variables: GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI, FLASK_SECRET_KEY")

SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    # Add 'https://www.googleapis.com/auth/generative-language.tuning' if you plan fine-tuning later
    # Add other scopes if your agents need access to other Google APIs (Drive, Calendar etc.)
]

# Define where your frontend UI is running for CORS and redirects
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5000") # Default if not set, adjust as needed

# --- Flask App Setup ---
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax' # Basic CSRF protection, consider 'Strict'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('FLASK_ENV') == 'production' # Use secure cookies in prod (requires HTTPS)
CORS(app, origins=["*"], supports_credentials=True) # Allow credentials (cookies) from frontend origin

# --- ADK Session Service (For ephemeral ADK agent state within a request) ---
# We are NOT using this for cross-request user chat history.
adk_session_service = InMemorySessionService()
APP_NAME = "figma_ai_assistant"

# --- ADK Model Configuration ---
AGENT_MODEL_NAME = "gemini-1.5-flash-latest" # Use a recent, capable model

# --- Agent Definitions (Keep only those used) ---
# Removed decision_agent as frontend determines mode

# Agent for Creating Designs
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
logging.info(f"Agent '{create_agent.name}' template defined.")

# Agent for Modifying Designs
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
logging.info(f"Agent '{modify_agent.name}' template defined.")

# Agent for handling answers
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
logging.info(f"Agent '{answer_agent.name}' template defined with tools: {[tool.name for tool in answer_agent.tools]}.")

# --- Helper Function to Build Flow ---
def build_flow():
    """Builds Google OAuth Flow object."""
    # Store client config in a dictionary
    client_config = {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
            "javascript_origins": "https://nr8fxs4v-8080.inc1.devtunnels.ms" # Important for CORS/Security
        }
    }
    return Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )

# --- Helper Function to Build Credentials from Session ---
def credentials_from_session(session_data):
    """Builds GoogleCredentials object from stored session data."""
    if not session_data:
        return None
    try:
        # Ensure expiry is timezone-aware
        expiry = session_data.get('expiry')
        if expiry and isinstance(expiry, str):
             expiry = datetime.fromisoformat(expiry)
        elif expiry and isinstance(expiry, datetime) and expiry.tzinfo is None:
             # Assume UTC if timezone is missing (adjust if your storage differs)
             expiry = expiry.replace(tzinfo=timezone.utc)

        return GoogleCredentials(
            token=session_data.get('token'),
            refresh_token=session_data.get('refresh_token'),
            token_uri=session_data.get('token_uri'),
            client_id=session_data.get('client_id'),
            client_secret=session_data.get('client_secret'),
            scopes=session_data.get('scopes'),
            expiry=expiry
        )
    except Exception as e:
        logging.error(f"Error rebuilding credentials from session: {e}")
        return None

# --- Helper to Validate SVG ---
def is_valid_svg(svg_string):
    """Validates if string looks like SVG, stripping common markdown."""
    if not svg_string or not isinstance(svg_string, str): return False
    svg_clean = re.sub(r'^\s*```(?:svg|xml)?\s*', '', svg_string.strip(), flags=re.IGNORECASE)
    svg_clean = re.sub(r'\s*```\s*$', '', svg_clean, flags=re.IGNORECASE)
    return svg_clean.strip().startswith('<svg') and svg_clean.strip().endswith('>')

# --- Helper to Run ADK Interaction with User Credentials ---
async def run_adk_interaction_with_auth(agent_template, user_credentials, user_content, user_id, chat_history_for_agent):
    """Runs a single ADK agent interaction using provided user credentials."""
    final_response_text = None
    adk_run_session_id = f"adk_{uuid.uuid4()}" # Unique ID for ADK's internal session state for this run

    if not user_credentials:
        return "AUTH_ERROR: Missing user credentials."

    # --- Refresh Token if Necessary ---
    try:
        # Check if credentials have expired or are close to expiring
        # The google-auth library often handles this automatically if refresh_token is present
        # but explicit check adds robustness.
        if user_credentials.expired and user_credentials.refresh_token:
            logging.info(f"Credentials expired for user {user_id}. Refreshing...")
            try:
                 auth_request = GoogleAuthRequest() # Use an authorized session/request object
                 user_credentials.refresh(auth_request)
                 logging.info(f"Credentials successfully refreshed for user {user_id}.")
                 # *** IMPORTANT: Update stored credentials after refresh ***
                 # You MUST save the updated user_credentials (especially the new access token and possibly expiry)
                 # back to your persistent store (session/DB). This is crucial.
                 # Example for Flask session (Adapt for DB):
                 session['credentials'] = {
                     'token': user_credentials.token,
                     'refresh_token': user_credentials.refresh_token, # Usually stays the same
                     'token_uri': user_credentials.token_uri,
                     'client_id': user_credentials.client_id,
                     'client_secret': user_credentials.client_secret,
                     'scopes': user_credentials.scopes,
                     'expiry': user_credentials.expiry.isoformat() if user_credentials.expiry else None,
                 }
                 session.modified = True # Ensure session is saved
            except google.auth.exceptions.RefreshError as re:
                 logging.error(f"Failed to refresh token for user {user_id}: {re}")
                 # Log the user out or ask them to re-authenticate
                 session.pop('credentials', None)
                 session.pop('user_info', None)
                 return "AUTH_ERROR: Token refresh failed. Please sign in again."
    except Exception as e:
        logging.error(f"Error during credential refresh check for user {user_id}: {e}")
        # Decide on fallback: proceed with potentially expired token or force re-auth
        return f"AUTH_ERROR: Could not verify credential status: {e}"


    # --- Instantiate Model and Agent with User Credentials ---
    try:
        # Configure GenAI library globally for this request (if needed, or pass creds directly)
        # Passing directly to the model is cleaner:
        user_model = genai.GenerativeModel(
            agent_template.model, # Use model name from template
            credentials=user_credentials
            # Add safety_settings, generation_config if needed globally
        )

        # Create a *new instance* of the agent with the user-specific model
        agent_instance = Agent(
            name=agent_template.name,
            model=user_model, # Pass the model instance with credentials
            description=agent_template.description,
            instruction=agent_template.instruction,
            tools=agent_template.tools,
            # Add generate_content_config if defined in template
        )
        logging.info(f"Instantiated agent '{agent_instance.name}' for user {user_id} with specific credentials.")

    except Exception as e:
        logging.error(f"Failed to initialize model or agent for user {user_id}: {e}")
        return f"AGENT_INIT_ERROR: {e}"

    # Create a temporary ADK session for this specific run
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
    runner = Runner(
        agent=agent_instance, # Use the instance with user credentials
        app_name=APP_NAME,
        session_service=adk_session_service # Use the service for ephemeral state
    )

    logging.info(f"Running agent '{agent_instance.name}' in ADK session '{adk_run_session_id}' for user {user_id}...")
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
        # Clean up the temporary ADK session data for this run
        try:
            adk_session_service.delete_session(app_name=APP_NAME, user_id=user_id, session_id=adk_run_session_id)
        except Exception as delete_err:
            logging.warning(f"Failed to delete temporary ADK session '{adk_run_session_id}': {delete_err}")

    logging.info(f"Agent '{agent_instance.name}' for user {user_id} finished. Result: {'<empty>' if not final_response_text else final_response_text[:100] + '...'}")
    return final_response_text


# --- OAuth Routes ---

@app.route('/authorize')
def authorize():
    """Initiates the Google OAuth 2.0 flow."""
    flow = build_flow()
    authorization_url, state = flow.authorization_url(
        access_type='offline', # Request refresh token
        prompt='consent',      # Force consent screen for refresh token
        include_granted_scopes='true'
    )
    session['oauth_state'] = state
    logging.info(f"Redirecting user to Google for authorization. State: {state}")
    return redirect(authorization_url)

@app.route('/oauth2callback', methods=['GET'])
def oauth2callback():
    """Handles the redirect from Google after user authorization."""
    state = session.get('oauth_state')
    if not state or state != request.args.get('state'):
        logging.error("OAuth callback state mismatch.")
        return jsonify({"success": False, "error": "Invalid state parameter."}), 400
    session.pop('oauth_state', None) # State verified, remove it

    flow = build_flow()
    try:
        # Use the full URL from the request to fetch the token
        flow.fetch_token(authorization_response=request.url)
        logging.info("Successfully fetched token from Google.")

        credentials = flow.credentials

        # --- Store credentials securely ---
        # **PRODUCTION NOTE:** Storing sensitive tokens (esp. refresh_token)
        # in Flask session is NOT recommended for production. Use a secure database
        # encrypted at rest, linking tokens to your internal user ID.
        session['credentials'] = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes,
            'expiry': credentials.expiry.isoformat() if credentials.expiry else None, # Store expiry as ISO string
        }

        # --- Get User Info ---
        try:
            id_info = id_token.verify_oauth2_token(
                credentials.id_token, GoogleAuthRequest(), credentials.client_id
            )
            session['user_info'] = {
                'id': id_info['sub'], # Google's unique user ID
                'email': id_info.get('email'),
                'name': id_info.get('name'),
                'picture': id_info.get('picture'),
            }
            logging.info(f"User {id_info.get('email')} ({id_info['sub']}) authenticated.")
        except Exception as e:
            logging.error(f"Failed to verify ID token or get user info: {e}")
            # Decide if login should fail or proceed without full user info
            session.pop('credentials', None) # Clear credentials if user info fails
            return jsonify({"success": False, "error": f"Failed to verify user identity: {e}"}), 500

        # --- Initialize User Chat History (if not exists) ---
        user_id = session['user_info']['id']
        if f'chat_history_{user_id}' not in session:
            session[f'chat_history_{user_id}'] = []
            logging.info(f"Initialized empty chat history for user {user_id}")

        # Redirect back to the frontend UI
        return redirect(FRONTEND_URL)

    except google.auth.exceptions.FlowError as e:
        logging.error(f"OAuth flow error during token fetch: {e}")
        return jsonify({"success": False, "error": f"Authentication flow failed: {e}"}), 500
    except Exception as e:
        logging.exception("An unexpected error occurred during OAuth callback:")
        return jsonify({"success": False, "error": f"An unexpected error occurred: {e}"}), 500


@app.route('/api/auth/status')
def auth_status():
    """Checks if the user has a valid session and returns user info."""

    print("origin",request.environ.get('REMOTE_PORT'))
    if 'credentials' in session and 'user_info' in session:
        # Optional: Check if token is nearing expiry and flag for frontend if needed
        # For simplicity, just check if credentials exist. Actual validity check happens on API call.
        user_info = session['user_info']
        logging.info(f"Auth status check: User {user_info.get('email')} is logged in.")
        return jsonify({"isLoggedIn": True, "userInfo": user_info})
    else:
        logging.info("Auth status check: User is not logged in.")
        return jsonify({"isLoggedIn": False})

@app.route('/logout', methods=['POST'])
def logout():
    """Clears the user session."""
    user_email = session.get('user_info', {}).get('email', 'Unknown')
    session.pop('credentials', None)
    session.pop('user_info', None)
    # Clear chat history from session as well
    # This requires knowing the user ID before popping user_info, handle carefully
    # Or clear all session keys: session.clear()
    # Let's clear specific keys:
    user_id_to_clear = None
    if 'user_info' in session and 'id' in session['user_info']:
         user_id_to_clear = session['user_info']['id']

    session.clear() # Clears everything including credentials, user_info, history

    logging.info(f"User {user_email} logged out.")
    # Frontend should redirect after receiving this response
    return jsonify({"success": True, "message": "Logged out successfully."})


# --- Main API Endpoint (Requires Authentication) ---
@app.route('/chat', methods=['POST'])
async def handle_chat():
    """Handles user prompts using ADK agents, requires authentication."""
    # --- Authentication Check ---
    if 'credentials' not in session or 'user_info' not in session:
        logging.warning("Access denied to /chat: User not authenticated.")
        return jsonify({"success": False, "error": "Authentication required."}), 401

    user_info = session['user_info']
    user_id = user_info['id']
    user_email = user_info.get('email', user_id) # For logging

    user_credentials = credentials_from_session(session.get('credentials'))
    if not user_credentials:
        logging.error(f"Failed to rebuild credentials for user {user_email}. Forcing logout.")
        session.clear() # Log out user if credentials are bad
        return jsonify({"success": False, "error": "Invalid session credentials. Please log in again."}), 401

    # --- Request Parsing ---
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 400

    data = request.get_json()
    user_prompt_text = data.get('userPrompt')
    mode = data.get('mode') # 'create', 'modify', 'answer' - Sent by frontend
    context = data.get('context', {})
    frame_data_base64 = data.get('frameDataBase64') # For modify
    element_data_base64 = data.get('elementDataBase64') # For modify

    if not user_prompt_text or not mode:
        return jsonify({"success": False, "error": "Missing 'userPrompt' or 'mode'"}), 400
    if mode not in ['create', 'modify', 'answer']:
        return jsonify({"success": False, "error": f"Invalid mode: {mode}"}), 400

    logging.info(f"Received /chat request from user '{user_email}'. Mode: '{mode}'. Prompt: '{user_prompt_text[:50]}...'")

    # --- Get User Chat History ---
    # **PRODUCTION NOTE:** Retrieve from DB here based on user_id
    history_key = f'chat_history_{user_id}'
    user_chat_history = session.get(history_key, [])
    # Limit history length (optional, manage in DB query ideally)
    MAX_HISTORY = 10 # Keep last 5 turns (10 messages)
    if len(user_chat_history) > MAX_HISTORY * 2:
         user_chat_history = user_chat_history[-(MAX_HISTORY * 2):]


    # --- Prepare Agent Input ---
    agent_to_run = None
    agent_input_content = None
    requires_history = False # Flag if agent needs history context

    try:
        if mode == 'create':
            agent_to_run = create_agent # Use the template
            # Create agent doesn't usually need history, just the prompt
            prompt_for_agent = user_prompt_text # Maybe add context text?
            if context.get('frameName'):
                 prompt_for_agent = f"Design for frame '{context['frameName']}'.\nUser Request: {user_prompt_text}"

            agent_input_parts = [google_genai_types.Part(text=prompt_for_agent)]
            agent_input_content = google_genai_types.Content(role='user', parts=agent_input_parts)

        elif mode == 'modify':
            agent_to_run = modify_agent # Use the template
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
            agent_to_run = answer_agent # Use the template
            requires_history = True # Answer agent needs conversational context
            # Simple prompt, history handled by run_adk_interaction_with_auth
            agent_input_parts = [google_genai_types.Part(text=user_prompt_text)]
            agent_input_content = google_genai_types.Content(role='user', parts=agent_input_parts)

    except ValueError as ve:
         logging.warning(f"Input validation error for user {user_email}: {ve}")
         # Return 200 OK with error for UI display
         return jsonify({"success": False, "error": str(ve)}), 200
    except Exception as e:
         logging.exception(f"Error preparing agent input for user {user_email}:")
         return jsonify({"success": False, "error": "Internal server error preparing request."}), 500


    # --- Execute Agent ---
    history_for_agent_run = user_chat_history if requires_history else []
    final_result = await run_adk_interaction_with_auth(
        agent_template=agent_to_run,
        user_credentials=user_credentials,
        user_content=agent_input_content,
        user_id=user_id,
        chat_history_for_agent=history_for_agent_run # Pass relevant history
    )

    # --- Process Result ---
    if not final_result or final_result.startswith("AGENT_ERROR:") or final_result.startswith("ADK_RUNTIME_ERROR:") or final_result.startswith("AUTH_ERROR:") or final_result.startswith("AGENT_INIT_ERROR:") :
        error_msg = f"Agent execution failed for user {user_email}. Details: {final_result}"
        logging.error(error_msg)
        # Return 200 OK for UI display, even on agent errors
        return jsonify({"success": False, "error": final_result or "Agent failed to produce a result."}), 200

    # --- Update Chat History ---
    # **PRODUCTION NOTE:** Update history in DB here
    # Append user prompt and AI response using standardized format
    user_turn = {'role': 'user', 'parts': [{'text': user_prompt_text}]} # Use ADK format
    model_turn = {'role': 'model', 'parts': [{'text': final_result}]}
    user_chat_history.extend([user_turn, model_turn])
    session[history_key] = user_chat_history # Save updated history back to session
    session.modified = True # Mark session as modified
    logging.info(f"Updated chat history for user {user_email}. New length: {len(user_chat_history)}")


    # --- Format and Return Success Response ---
    if mode == 'create' or mode == 'modify':
        # Validate SVG result
        if not is_valid_svg(final_result):
             error_msg = f"Agent returned invalid SVG for mode '{mode}' (user {user_email}). Snippet: {final_result[:200]}..."
             logging.error(error_msg)
             # Even though agent succeeded, the output is bad - return error
             return jsonify({"success": False, "error": f"Agent returned invalid SVG output. Please try again or rephrase."}), 200
        else:
            # Clean potential markdown just in case validation missed it
            cleaned_svg = re.sub(r'^\s*```(?:svg|xml)?\s*', '', final_result.strip(), flags=re.IGNORECASE)
            cleaned_svg = re.sub(r'\s*```\s*$', '', cleaned_svg, flags=re.IGNORECASE)
            logging.info(f"Returning successful SVG response for mode '{mode}' (user {user_email}).")
            return jsonify({"success": True, "svg": cleaned_svg, "mode": mode}) # Include mode for clarity

    elif mode == 'answer':
        logging.info(f"Returning successful Answer response for user {user_email}.")
        return jsonify({"success": True, "answer": final_result, "mode": "answer"})

    else: # Should not be reachable
        logging.error(f"Internal logic error: Reached end of /chat with unhandled mode '{mode}' (user {user_email}).")
        return jsonify({"success": False, "error": "Internal server error: Unknown result type."}), 500


# --- Run the App ---
if __name__ == '__main__':
    # Ensure HTTPS is used in production for secure cookies
    # Use a production WSGI server like Gunicorn:
    # gunicorn --bind 0.0.0.0:5001 app:app --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker --log-level info
    # For development:
    logging.info(f"Starting Flask app in development mode. Frontend expected at: {FRONTEND_URL}")
    logging.info(f"OAuth Redirect URI configured as: {REDIRECT_URI}")
    # Set FLASK_ENV=development or remove debug=True for production builds
    app.run(host='0.0.0.0', port=8080, debug=True)