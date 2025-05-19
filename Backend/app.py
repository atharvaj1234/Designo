# app.py
import base64
import asyncio
import json
from flask import Flask, request, jsonify, abort
from flask_cors import CORS
from google.genai import types as google_genai_types
import config
import adk_utils
import agents
# Imports firebase_auth, db, process_daily_trial, verify_firebase_id_token, create_user_doc_if_not_exists
import firebase_admin_init
import datetime
import pytz
import traceback
import re # Keep re for string cleaning outside of adk_utils


# --- Flask App Setup ---
app = Flask(__name__)
CORS(app, origins="*") # Be cautious with origins="*" in production

# --- Global State (Manual Chat History) ---
# NOTE: For a production multi-user app, chat history should be stored per-user
# in a persistent database (like Firestore) using their UID.
chat_history = {}
MAX_CHAT_HISTORY = 10 # Keep last N turns


# --- Utility to extract and verify UID from request (for AI requests) ---
def get_user_uid_from_request(request):
    """Extracts and verifies the Firebase ID token from the Authorization header."""
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        return None, "Authorization header missing"

    try:
        scheme, id_token = auth_header.split()
        if scheme.lower() != 'bearer':
            return None, "Authorization scheme must be Bearer"
    except ValueError:
        return None, "Invalid Authorization header format"

    # Verify the ID token using the Firebase Admin SDK
    # This token should be the one obtained *after* signInWithCustomToken on the client
    # It implicitly checks for expiration, revocation, and disabled status if enabled in verify_id_token
    uid = firebase_admin_init.verify_firebase_id_token(id_token)

    if not uid:
        # verify_firebase_id_token returns None if verification fails for any reason
        return None, "Invalid or expired authentication token. Please sign in again."

    return uid, None # Return uid and no error


# --- AUTHENTICATION ENDPOINTS (for Custom Auth Flow) ---

@app.route('/auth/exchange-id-token-for-custom-token', methods=['POST'])
# This endpoint doesn't perform long-running ADK calls, so it doesn't strictly need to be async.
def exchange_id_token_for_custom_token():
    """
    Exchanges a standard Firebase ID token (from initial client auth like Email/Password) for a Custom Token.
    Ensures user document exists in Firestore upon successful exchange.
    """
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 415

    data = request.get_json()
    client_id_token = data.get('idToken')

    if not client_id_token:
        return jsonify({"success": False, "error": "Missing 'idToken' in request body"}), 400

    try:
        # Verify the ID token sent from the client's *initial* sign-in (Email/Password or Google)
        # This verifies the user's identity without relying on popup/redirect flows for session management.
        # Use check_revoked=True to ensure the token hasn't been revoked
        # If email/password sign-in worked client-side, this token should be valid.
        decoded_token = firebase_admin_init.firebase_auth.verify_id_token(client_id_token, check_revoked=True)
        uid = decoded_token['uid']
        email = decoded_token.get('email') # Get email if available in the token

        print(f"Client ID Token verified. User UID: {uid}")

        # --- Ensure User Document Exists in Firestore ---
        # Call the function to create the doc if it doesn't exist.
        # It handles its own transaction and potential errors.
        # We don't strictly need to check its return value here unless we wanted
        # to report back to the UI if the doc was newly created vs already existed.
        firebase_admin_init.create_user_doc_if_not_exists(uid, email=email)
        # If create_user_doc_if_not_exists failed internally (e.g., Firestore error),
        # a warning is printed. We still proceed to mint the token.
        # A more robust approach might check for Firestore errors specifically and abort.


        # --- Mint Custom Token ---
        custom_token = firebase_admin_init.firebase_auth.create_custom_token(uid)
        print(f"Custom token minted for UID: {uid}")

        # Return the custom token to the client
        # custom_token is bytes, decode to utf-8 string for JSON
        return jsonify({"success": True, "customToken": custom_token.decode('utf-8')}), 200

    except firebase_admin_init.auth.ExpiredIdTokenError:
        print("Client ID Token is expired.")
        return jsonify({"success": False, "error": "Authentication failed: Token expired. Please sign in again."}), 401
    except firebase_admin_init.auth.InvalidIdTokenError:
        print("Client ID Token is invalid.")
        return jsonify({"success": False, "error": "Authentication failed: Invalid token. Please sign in again."}), 401
    except firebase_admin_init.auth.UserDisabledError:
         print(f"User account is disabled.") # UID not available in except block easily, rely on token content or re-fetch user
         return jsonify({"success": False, "error": "Your account is disabled. Please contact support."}), 401
    except Exception as e:
        print(f"Error exchanging client ID token for custom token: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "error": "An internal error occurred during authentication."}), 500


# --- AI GENERATION ENDPOINT ---

@app.route('/generate', methods=['POST'])
async def handle_generate():
    """Handles requests using ADK agents, requiring authentication and trial check."""
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 415

    # --- Authentication ---
    # Get and verify the user's UID from the Custom Auth ID token
    uid, auth_error = get_user_uid_from_request(request)

    if auth_error:
        print(f"Authentication/Authorization failed for /generate: {auth_error}")
        # Return 401 Unauthorized for authentication failures
        return jsonify({"success": False, "error": f"Authentication failed: {auth_error}"}), 401

    print(f"/generate request from authenticated user UID: {uid}")

    # --- Daily Trial Check ---
    # Use the synchronous version from firebase_admin_init
    # This function assumes the user doc exists and performs the check/increment within a transaction
    can_proceed, trial_message = firebase_admin_init.process_daily_trial(uid)

    if not can_proceed:
        print(f"Trial limit reached for user {uid}: {trial_message}")
        # Return 200 OK but with success: False and message for UI display
        # This allows the UI to display the specific trial message.
        return jsonify({"success": False, "error": trial_message}), 200


    # --- Proceed with Request Processing (only if authenticated and within trial limit) ---
    # Note: For a production multi-user system, chat_history should be stored per-user
    # in a persistent database (like Firestore) using their UID.
    user_history = chat_history.get(uid)
    if user_history is None:
        user_history = []
        chat_history[uid] = user_history
        print(f"Initialized chat history for UID: {uid}")

    data = request.get_json()
    user_prompt_text = data.get('userPrompt')
    context = data.get('context', {})
    frame_data_base64 = data.get('frameDataBase64')
    element_data_base64 = data.get('elementDataBase64')
    i_mode = data.get('mode') # Frontend mode hint ('create', 'modify', 'answer')

    if not user_prompt_text:
        return jsonify({"success": False, "error": "Missing 'userPrompt'"}), 400

    # Prepare content for decision agent (text prompt + potentially context text + history)
    history_text = ""
    # Using global chat_history - need to filter by user UID in a real app
    if user_history:
        # Example: Filter by user UID (assuming history items have a 'uid' field)
        # Limit history sent to the model
        user_history_summary = [f"User: {item['user'][:100]}{'...' if len(item['user']) > 100 else ''}\nAI: {item['AI'][:100]}{'...' if len(item['AI']) > 100 else ''}" for item in user_history[-5:]] # Limit to last 5 turns
        if user_history_summary:
            history_text = "Previous Conversation Summary:\n" + "\n---\n".join(user_history_summary) + "\n\n"


    decision_prompt = f"""
{history_text}
**User Request**
{user_prompt_text}
"""
    if context:
        decision_prompt += f"\n**Figma Context**\n{context}"

    decision_content = google_genai_types.Content(role='user', parts=[
        google_genai_types.Part(text=decision_prompt)
    ])

    # Run decision agent
    intent_mode = await adk_utils.run_adk_interaction(
        agents.decision_agent,
        decision_content,
        adk_utils.session_service, # Pass the shared session service instance
        user_id=uid # Pass the authenticated UID to ADK runner for session separation
    )

    # Clean and validate decision agent output
    if not intent_mode:
         error_msg = f"Could not determine intent: Agent returned empty response."
         print(error_msg)
         return jsonify({"success": False, "error": error_msg}), 200
    if intent_mode.startswith("AGENT_ERROR:") or intent_mode.startswith("ADK_RUNTIME_ERROR:"):
         error_msg = f"Could not determine intent. Agent Error: {intent_mode}"
         print(error_msg)
         return jsonify({"success": False, "error": error_msg}), 200

    # Sanitize and validate the expected single word output
    intent_mode = intent_mode.strip().lower()
    if intent_mode not in ['create', 'modify', 'answer']:
        print(f"WARNING: Decision agent returned unexpected value: '{intent_mode}'. Falling back to 'answer'.")
        intent_mode = 'answer' # Fallback to answer if classification is odd

    print(f"Determined Intent: '{intent_mode}'")

    # Frontend mode hint helps ensure the user *intended* a design task if selection was made
    # If agent determines create/modify but frontend mode doesn't match, it indicates a missing selection state
    if intent_mode in ['create', 'modify'] and i_mode != intent_mode:
         print(f"Agent intent '{intent_mode}' determined, but frontend mode hint was '{i_mode}'. Requiring matching mode for design tasks.")
         if intent_mode == 'create':
              return jsonify({"success": False, "error": "I detected a creation request, but I need an empty frame selection to create a new design."}), 200
         elif intent_mode == 'modify':
              return jsonify({"success": False, "error": "I detected a modification request, but I need an element selection to proceed."}), 200
         # If intent was 'answer' but frontend mode was create/modify, we proceed with answer.
         # This fallback is handled by the elif intent_mode == 'answer' block below.


    # --- 2. Execute Based on Intent ---
    final_result = None
    final_type = "unknown"
    agent_used_name = "None" # Track agent name for logging/error reporting

    try:
        if intent_mode == 'create':
            final_type = "svg"
            agent_used_name = agents.create_agent.name
            print("--- Initiating Create Flow (Refine -> Create) ---")

            # Validation already done above based on i_mode check and context presence


            # A) Run Refine Agent
            print(f"Running Refine Agent for UID {uid}...")
            refine_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=user_prompt_text)])
            refined_prompt = await adk_utils.run_adk_interaction(agents.refine_agent, refine_content, adk_utils.session_service, user_id=uid)

            if not refined_prompt or refined_prompt.startswith("AGENT_ERROR:") or refined_prompt.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Refine Agent failed: {refined_prompt}")

            refined_prompt_clean = refined_prompt.strip()
            # Robustly remove potential markdown code block wrappers
            refined_prompt_clean = re.sub(r'^\s*```(?:markdown)?\s*', '', refined_prompt_clean, flags=re.IGNORECASE)
            refined_prompt_clean = re.sub(r'\s*```\s*$', '', refined_prompt_clean, flags=re.IGNORECASE)

            if not refined_prompt_clean:
                 print("WARNING: Refine agent returned empty brief, falling back to original prompt.")
                 refined_prompt_clean = user_prompt_text


            # B) Run Create Agent
            print(f"Running Create Agent for UID {uid} with refined prompt...")
            # Include the design principles *alongside* the refined prompt/brief
            # Keep the instruction string consistent with the agent definition, just prefix the prompt
            # The agent instruction is already defined with the mission goals and output requirements.
            # We just need to pass the *input* which is the refined brief.
            # The agent's *instruction* tells it how to use the input.
            # So the input should just be the refined brief text.
            create_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=refined_prompt_clean)])


            initial_svg = await adk_utils.run_adk_interaction(agents.create_agent, create_content, adk_utils.session_service, user_id=uid)

            if not initial_svg or initial_svg.startswith("AGENT_ERROR:") or initial_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Create Agent failed: {initial_svg}")

            # Validate and clean SVG output from the agent
            if adk_utils.is_valid_svg(initial_svg):
                 cleaned_svg = re.sub(r'^\s*```(?:svg|xml)?\s*', '', initial_svg.strip(), flags=re.IGNORECASE)
                 cleaned_svg = re.sub(r'\s*```\s*$', '', cleaned_svg, flags=re.IGNORECASE)

            if not cleaned_svg: # If cleaning didn't make it valid (returns False)
                 raise ValueError(f"Create Agent response is not valid SVG even after cleaning. Snippet: {initial_svg[:200]}...")
            initial_svg = cleaned_svg # Use the cleaned, validated SVG


            print("Initial SVG created and validated.")
            final_result = initial_svg
            # Add conversation turn to history (global history for this example)
            if len(user_history) >= MAX_CHAT_HISTORY: user_history.pop(0)
            # In a real app, store history with UID in Firestore
            user_history.append({'uid': uid, 'user': user_prompt_text, 'AI': "Created design."}) # Short history entry


        elif intent_mode == 'modify':
            final_type = "svg"
            agent_used_name = agents.modify_agent.name
            print("--- Initiating Modify Flow (Refine -> Modify) ---")

            # Validation already done above based on i_mode check and data presence

            # A) Run Refine Agent for Modification
            print(f"Running Refine Agent for Modification for UID {uid}...")
            refine_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=user_prompt_text)])
            refined_prompt = await adk_utils.run_adk_interaction(agents.refine_agent, refine_content, adk_utils.session_service, user_id=uid)

            if not refined_prompt or refined_prompt.startswith("AGENT_ERROR:") or refined_prompt.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Refine Agent failed during modify flow: {refined_prompt}")

            refined_prompt_clean = refined_prompt.strip()
            # Robustly remove potential markdown code block wrappers
            refined_prompt_clean = re.sub(r'^\s*```(?:markdown)?\s*', '', refined_prompt_clean, flags=re.IGNORECASE)
            refined_prompt_clean = re.sub(r'\s*```\s*$', '', refined_prompt_clean, flags=re.IGNORECASE)


            if not refined_prompt_clean:
                 print("WARNING: Refine agent returned empty brief for modify, falling back.")
                 refined_prompt_clean = user_prompt_text

            # B) Prepare prompt and image parts for modify agent
            modify_prompt_text = f"""**Modification Brief**\n{refined_prompt_clean}\n\n**Figma Context**\nFrame Name: {context.get('frameName', 'N/A')}\nElement Info: {context['elementInfo']}"""
            message_parts = [google_genai_types.Part(text=modify_prompt_text)]

            try:
                frame_bytes = base64.b64decode(frame_data_base64)
                element_bytes = base64.b64decode(element_data_base64)
                message_parts.append(google_genai_types.Part(inline_data=google_genai_types.Blob(mime_type="image/png", data=frame_bytes)))
                message_parts.append(google_genai_types.Part(inline_data=google_genai_types.Blob(mime_type="image/png", data=element_bytes)))
                print("Frame and Element image parts prepared for modify agent.")
            except Exception as e:
                print(f"Invalid image data received for UID {uid}: {e}")
                # Return 400 for invalid data
                return jsonify({"success": False, "error": f"Invalid image data provided: {e}"}), 400

            modify_content = google_genai_types.Content(role='user', parts=message_parts)

            # C) Run Modify Agent
            print(f"Running Modify Agent for UID {uid}...")
            modified_svg = await adk_utils.run_adk_interaction(agents.modify_agent, modify_content, adk_utils.session_service, user_id=uid)

            if not modified_svg or modified_svg.startswith("AGENT_ERROR:") or modified_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Modify Agent failed: {modified_svg}")

            # Validate and clean SVG output from the agent
            if adk_utils.is_valid_svg(modified_svg):
                 cleaned_svg = re.sub(r'^\s*```(?:svg|xml)?\s*', '', modified_svg.strip(), flags=re.IGNORECASE)
                 cleaned_svg = re.sub(r'\s*```\s*$', '', cleaned_svg, flags=re.IGNORECASE)

            if not cleaned_svg: # If cleaning didn't make it valid (returns False)
                 raise ValueError(f"Modify Agent response is not valid SVG even after cleaning. Snippet: {modified_svg[:200]}...")
            modified_svg = cleaned_svg # Use the cleaned, validated SVG

            print("SVG modification successful and validated.")
            final_result = modified_svg
            # Add conversation turn to history (global history for this example)
            if len(user_history) >= MAX_CHAT_HISTORY: user_history.pop(0)
            # In a real app, store history with UID in Firestore
            user_history.append({'uid': uid, 'user': user_prompt_text, 'AI': "Modified component."}) # Short history entry


        elif intent_mode == 'answer':
            final_type = "answer"
            agent_used_name = agents.answer_agent.name
            print(f"--- Running Answer Agent for UID {uid} ---")

            answer_prompt = f"""{history_text}**User Query**\n{user_prompt_text}\n\nPlease provide a helpful design-related answer."""
            answer_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=answer_prompt)])
            answer_text = await adk_utils.run_adk_interaction(agents.answer_agent, answer_content, adk_utils.session_service, user_id=uid)

            if not answer_text:
                 print("Answer agent returned empty response.")
                 final_result = "I could not find specific information regarding your query."
            elif answer_text.startswith("AGENT_ERROR:") or answer_text.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Answer Agent failed: {answer_text}")
            else:
                final_result = answer_text

            print("Answer agent finished.")
            if len(user_history) >= MAX_CHAT_HISTORY: user_history.pop(0)
             # In a real app, store history with UID in Firestore
            user_history.append({'uid': uid, 'user': user_prompt_text, 'AI': final_result}) # Store full answer


        else:
            # This branch should not be reached if intent_mode is correctly classified
            print(f"Internal error: Unhandled intent '{intent_mode}' for UID {uid}.")
            return jsonify({"success": False, "error": f"Internal error: Unhandled intent type '{intent_mode}'."}), 500

    except ValueError as ve:
        # Catch specific validation/logic errors raised intentionally
        error_message = str(ve)
        print(f"Error during '{agent_used_name}' execution for UID {uid}: {error_message}")
        # Return 200 OK but with success: False for UI to display the error gracefully
        # This includes errors like agent failures encapsulated in ValueErrors.
        return jsonify({"success": False, "error": error_message}), 200
    except Exception as e:
        # Catch broader unexpected exceptions during agent runs or processing
        error_message = f"An unexpected error occurred during '{agent_used_name}' execution for UID {uid}: {e}"
        print(error_message)
        traceback.print_exc() # Print traceback for debugging server-side
        # Return 500 for unexpected internal server errors
        return jsonify({"success": False, "error": "An internal server error occurred."}), 500

    # --- Format and Return Success Response ---
    if final_result is None:
         # Should ideally be caught by errors above, but as a safeguard
         print(f"Execution completed for '{agent_used_name}' but final_result is unexpectedly None for UID {uid}.")
         return jsonify({"success": False, "error": "Agent processing failed to produce a result."}), 500

    # Return the determined mode from the backend for UI clarity
    response_payload = {"success": True, "mode": final_type}
    if final_type == "svg":
        response_payload["svg"] = final_result
    elif final_type == "answer":
        response_payload["answer"] = final_result

    print(f"Request for UID {uid} completed successfully ({final_type}).")
    return jsonify(response_payload), 200


# --- Provide Firebase Client Config to UI ---
@app.route('/firebase-config', methods=['GET'])
def firebase_config():
     # config.FIREBASE_CLIENT_CONFIG is already a Python dict, jsonify will handle it
     if not config.FIREBASE_CLIENT_CONFIG:
         print("Firebase client config is not loaded.")
         return jsonify({"error": "Firebase client configuration is not available on the backend."}), 500
     return jsonify(config.FIREBASE_CLIENT_CONFIG), 200


# --- Run the App ---
if __name__ == '__main__':
    print(f"Running Flask app with AGENT_MODEL='{config.AGENT_MODEL}'")
    print("Ensure Firebase Admin SDK is initialized (via import of firebase_admin_init).")
    print("Ensure Firebase Client Config JSON is set in .env and parsed.")

    import hypercorn.asyncio
    from hypercorn.config import Config

    async def serve_app():
        config = Config()
        config.bind = ["0.0.0.0:5001"]
        # Set a reasonable number of workers for concurrency, or use 1 for simplicity
        # config.workers = 1 # For debugging, keep at 1
        await hypercorn.asyncio.serve(app, config)

    try:
        # Firebase Admin SDK is initialized on import of firebase_admin_init
        asyncio.run(serve_app())
    except KeyboardInterrupt:
        print("\nServer stopped.")
    except Exception as e:
         print(f"Server failed to start: {e}")
         traceback.print_exc()