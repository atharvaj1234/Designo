# app.py
import base64
import asyncio
import json
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from google.genai import types as google_genai_types
import config
import adk_utils
import agents
import firebase_admin_init
import api_handler # We will use acquire_project and release_project from here
# import datetime # Not directly used in snippet
# import pytz # Not directly used in snippet
# import traceback # Not directly used in snippet, Flask handles top-level
import re

# --- Flask App Setup ---
app = Flask(__name__)
CORS(app, origins="*")
logging.basicConfig(level=logging.INFO)

# --- Global State (Manual Chat History per user) ---
chat_history = {}
MAX_CHAT_HISTORY = 10

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
    uid = firebase_admin_init.verify_firebase_id_token(id_token)
    if not uid:
        return None, "Authentication failed: Invalid or expired token. Please sign in again."
    return uid, None

# --- AUTHENTICATION & KEY MANAGEMENT ENDPOINTS (Unchanged) ---
@app.route('/auth/exchange-id-token-for-custom-token', methods=['POST'])
def exchange_id_token_for_custom_token():
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 415
    data = request.get_json()
    client_id_token = data.get('idToken')
    if not client_id_token:
        return jsonify({"success": False, "error": "Missing 'idToken' in request body"}), 400
    try:
        decoded_token = firebase_admin_init.firebase_auth.verify_id_token(client_id_token, check_revoked=True)
        uid = decoded_token['uid']
        email = decoded_token.get('email')
        logging.info(f"Client ID Token verified. User UID: {uid}")
        firebase_admin_init.create_user_doc_if_not_exists(uid, email=email)
        custom_token_bytes = firebase_admin_init.firebase_auth.create_custom_token(uid)
        logging.info(f"Custom token minted for UID: {uid}")
        has_api_key = firebase_admin_init.has_api_key_stored(uid)
        logging.info(f"User {uid} has API key stored: {has_api_key}")
        return jsonify({
            "success": True,
            "customToken": custom_token_bytes.decode('utf-8'),
            "hasApiKey": has_api_key
            }), 200
    except firebase_admin_init.auth.ExpiredIdTokenError:
        logging.warning("Client ID Token is expired.")
        return jsonify({"success": False, "error": "Authentication failed: Token expired. Please sign in again."}), 401
    except firebase_admin_init.auth.InvalidIdTokenError:
        logging.warning("Client ID Token is invalid.")
        return jsonify({"success": False, "error": "Authentication failed: Invalid token. Please sign in again."}), 401
    except firebase_admin_init.auth.UserDisabledError:
         logging.warning(f"User account is disabled.")
         return jsonify({"success": False, "error": "Your account is disabled. Please contact support."}), 401
    except Exception as e:
        logging.error(f"Error exchanging client ID token for custom token: {e}", exc_info=True)
        return jsonify({"success": False, "error": "An internal error occurred during authentication."}), 500

@app.route('/auth/set-api-key', methods=['POST'])
def set_user_api_key():
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 415
    uid, auth_error = get_user_uid_from_request(request)
    if auth_error:
        logging.warning(f"Authentication failed for /auth/set-api-key: {auth_error}")
        return jsonify({"success": False, "error": f"Authentication failed: {auth_error}"}), 401
    data = request.get_json()
    api_key_from_user = data.get('apiKey')
    if not api_key_from_user or not isinstance(api_key_from_user, str):
        return jsonify({"success": False, "error": "Missing or invalid 'apiKey' in request body"}), 400
    if not re.match(r'^AIza[0-9A-Za-z_-]{35}$', api_key_from_user):
         logging.warning(f"User {uid} provided an API key that doesn't match typical Gemini format.")
    success = firebase_admin_init.store_encrypted_api_key(uid, api_key_from_user)
    if success:
        return jsonify({"success": True, "message": "API key saved successfully. You now have unlimited access!"}), 200
    else:
        return jsonify({"success": False, "error": "Failed to save API key. Please try again."}), 500

# --- AI GENERATION ENDPOINT (Modified for pooled key handling) ---
@app.route('/generate', methods=['POST'])
async def handle_generate():
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 415

    uid, auth_error = get_user_uid_from_request(request)
    if auth_error:
        logging.warning(f"Authentication/Authorization failed for /generate: {auth_error}")
        return jsonify({"success": False, "error": f"Authentication failed: {auth_error}"}), 401
    logging.info(f"/generate request from authenticated user UID: {uid}")

    can_proceed_trial, trial_message, decrypted_user_api_key, requests_today = firebase_admin_init.process_daily_trial(uid)

    run_interaction_method = None
    # api_key_for_adk_utils will be set either to user's key or a specific pooled key
    api_key_for_this_entire_request = None
    # project_in_use_for_this_request will hold the dict from api_handler if a pooled key is used
    project_in_use_for_this_request = None

    if decrypted_user_api_key:
        logging.info(f"User {uid} has a stored (BYOK) API key. Using it for all steps.")
        run_interaction_method = 'user_key'
        api_key_for_this_entire_request = decrypted_user_api_key
    elif can_proceed_trial:
        logging.info(f"User {uid} is eligible for a trial. Acquiring a pooled API key for all steps.")
        run_interaction_method = 'pooled_key'
        # Key will be acquired later, inside the try/except/finally block for pooled_key path
    else:
        logging.info(f"User {uid} has no API key and trial is not available. Message: {trial_message}")
        return jsonify({"success": False, "error": trial_message, "mode": "trial_expired"}), 200

    user_history = chat_history.get(uid, [])
    data = request.get_json()
    user_prompt_text = data.get('userPrompt')
    context = data.get('context', {})
    frame_data_base64 = data.get('frameDataBase64')
    element_data_base64 = data.get('elementDataBase64')
    i_mode = data.get('mode')

    if not user_prompt_text:
        return jsonify({"success": False, "error": "Missing 'userPrompt'"}), 400

    history_text = ""
    if user_history:
        user_history_summary = [
            f"User: {item.get('user', '')[:100]}{'...' if len(item.get('user', '')) > 100 else ''}\nAI: {item.get('AI', '')[:100]}{'...' if len(item.get('AI', '')) > 100 else ''}"
            for item in user_history[-MAX_CHAT_HISTORY:]
        ]
        if user_history_summary:
            history_text = "Previous Conversation Summary:\n" + "\n---\n".join(user_history_summary) + "\n\n"

    decision_prompt_text = f"{history_text}**User Request**\n{user_prompt_text}"
    if context:
        decision_prompt_text += f"\n**Figma Context**\n{json.dumps(context)}"
    decision_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=decision_prompt_text)])

    final_result = None
    final_type = "unknown"
    agent_used_name_log = "None" # For logging overall flow

    try:
        # --- Acquire pooled key if needed, ONCE for the entire request ---
        if run_interaction_method == 'pooled_key':
            try:
                project_in_use_for_this_request = await api_handler.acquire_project()
                api_key_for_this_entire_request = project_in_use_for_this_request["api_key"]
                logging.info(f"UID {uid}: Acquired pooled project '{project_in_use_for_this_request['id']}' (key ...{api_key_for_this_entire_request[-4:]}) for this entire request.")
            except Exception as acquire_err:
                logging.error(f"UID {uid}: Failed to acquire a pooled project: {acquire_err}", exc_info=True)
                # If acquire fails, it might raise, or we might want to return a specific "busy" error.
                # For now, let it propagate or return a generic error.
                return jsonify({"success": False, "error": f"Server busy, could not acquire an API resource: {acquire_err}"}), 503 # Service Unavailable

        if not api_key_for_this_entire_request: # Should only happen if pooled_key path failed to set it
             logging.error(f"UID {uid}: Logical error - API key for the request was not set.")
             return jsonify({"success": False, "error": "Internal server error: API key not available for processing."}), 500

        # --- 1. Determine Intent (using the single chosen API key) ---
        agent_used_name_log = agents.decision_agent.name
        intent_mode_raw = await adk_utils.run_adk_interaction(
            agents.decision_agent, decision_content, adk_utils.session_service,
            user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
        )

        if not intent_mode_raw or intent_mode_raw.startswith("AGENT_ERROR:") or intent_mode_raw.startswith("ADK_RUNTIME_ERROR:"):
            error_msg = f"Could not determine intent. Agent Response: {intent_mode_raw}"
            logging.error(f"UID {uid}: {error_msg}")
            # Note: If an AGENT_ERROR or ADK_RUNTIME_ERROR occurs, the key is still held until 'finally'
            return jsonify({"success": False, "error": error_msg}), 200

        intent_mode = intent_mode_raw.strip().lower()
        if intent_mode not in ['create', 'modify', 'answer']:
            logging.warning(f"UID {uid}: Decision agent returned unexpected value: '{intent_mode}'. Falling back to 'answer'.")
            intent_mode = 'answer'
        logging.info(f"UID {uid}: Determined Intent: '{intent_mode}'")

        if intent_mode in ['create', 'modify'] and i_mode != intent_mode:
            logging.warning(f"UID {uid}: Agent intent '{intent_mode}', frontend mode '{i_mode}'. Mismatch.")
            error_message_for_mismatch = ("I detected a creation request, but I need an empty frame selection to create a new design."
                                          if intent_mode == 'create' else
                                          "I detected a modification request, but I need an element selection to proceed.")
            return jsonify({"success": False, "error": error_message_for_mismatch}), 200

        # --- 2. Execute Based on Intent (using the SAME API key) ---
        if intent_mode == 'create':
            final_type = "svg"
            agent_used_name_log = f"{agents.refine_agent.name} -> {agents.create_agent.name}"
            logging.info(f"UID {uid}: --- Initiating Create Flow (using key ...{api_key_for_this_entire_request[-4:]}) ---")
            refine_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=user_prompt_text)])
            
            refined_prompt_md = await adk_utils.run_adk_interaction(
                agents.refine_agent, refine_content, adk_utils.session_service,
                user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
            )
            if not refined_prompt_md or refined_prompt_md.startswith("AGENT_ERROR:") or refined_prompt_md.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Refine Agent failed or returned error for create: {refined_prompt_md}")
            
            refined_prompt_clean = refined_prompt_md.strip()
            refined_prompt_clean = re.sub(r'^\s*```(?:markdown)?\s*', '', refined_prompt_clean, flags=re.IGNORECASE)
            refined_prompt_clean = re.sub(r'\s*```\s*$', '', refined_prompt_clean, flags=re.IGNORECASE)
            if not refined_prompt_clean:
                 logging.warning(f"UID {uid}: Refine agent returned empty brief for create, falling back to original prompt.")
                 refined_prompt_clean = user_prompt_text
            
            create_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=refined_prompt_clean)])
            initial_svg = await adk_utils.run_adk_interaction(
                agents.create_agent, create_content, adk_utils.session_service,
                user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
            )
            if not initial_svg or initial_svg.startswith("AGENT_ERROR:") or initial_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Create Agent failed or returned error: {initial_svg}")
            
            cleaned_svg = adk_utils.is_valid_svg(initial_svg)
            if not cleaned_svg:
                 raise ValueError(f"Create Agent response is not valid SVG. Snippet: {str(initial_svg)[:200]}...")
            final_result = cleaned_svg
            logging.info(f"UID {uid}: Create flow successful.")

        elif intent_mode == 'modify':
            final_type = "svg"
            agent_used_name_log = f"{agents.refine_agent.name} -> {agents.modify_agent.name}"
            logging.info(f"UID {uid}: --- Initiating Modify Flow (using key ...{api_key_for_this_entire_request[-4:]}) ---")
            if not frame_data_base64 or not element_data_base64 or not context.get('elementInfo'):
                 raise ValueError("Missing 'frameDataBase64', 'elementDataBase64', or 'elementInfo' for modify mode")

            refine_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=user_prompt_text)])
            refined_prompt_md = await adk_utils.run_adk_interaction(
                agents.refine_agent, refine_content, adk_utils.session_service,
                user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
            )
            if not refined_prompt_md or refined_prompt_md.startswith("AGENT_ERROR:") or refined_prompt_md.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Refine Agent failed or returned error for modify: {refined_prompt_md}")

            refined_prompt_clean = refined_prompt_md.strip()
            refined_prompt_clean = re.sub(r'^\s*```(?:markdown)?\s*', '', refined_prompt_clean, flags=re.IGNORECASE)
            refined_prompt_clean = re.sub(r'\s*```\s*$', '', refined_prompt_clean, flags=re.IGNORECASE)
            if not refined_prompt_clean:
                 logging.warning(f"UID {uid}: Refine agent returned empty brief for modify, falling back to original prompt.")
                 refined_prompt_clean = user_prompt_text

            modify_agent_prompt_text = f"""**Modification Brief**\n{refined_prompt_clean}\n\n**Original User Prompt for context:**\n{user_prompt_text}\n\n**Figma Context:**\nFrame Name: {context.get('frameName', 'N/A')}\nElement Info: {context.get('elementInfo','N/A')}"""
            message_parts = [google_genai_types.Part(text=modify_agent_prompt_text)]
            try:
                frame_bytes = base64.b64decode(frame_data_base64)
                element_bytes = base64.b64decode(element_data_base64)
                message_parts.append(google_genai_types.Part(inline_data=google_genai_types.Blob(mime_type="image/png", data=frame_bytes)))
                message_parts.append(google_genai_types.Part(inline_data=google_genai_types.Blob(mime_type="image/png", data=element_bytes)))
            except Exception as e:
                raise ValueError(f"Invalid image data received for modify mode: {e}")
            
            modify_content = google_genai_types.Content(role='user', parts=message_parts)
            modified_svg = await adk_utils.run_adk_interaction(
                agents.modify_agent, modify_content, adk_utils.session_service,
                user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
            )
            if not modified_svg or modified_svg.startswith("AGENT_ERROR:") or modified_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Modify Agent failed or returned error: {modified_svg}")
            
            cleaned_svg = adk_utils.is_valid_svg(modified_svg)
            if not cleaned_svg:
                 raise ValueError(f"Modify Agent response is not valid SVG. Snippet: {str(modified_svg)[:200]}...")
            final_result = cleaned_svg
            logging.info(f"UID {uid}: Modify flow successful.")

        elif intent_mode == 'answer':
            final_type = "answer"
            agent_used_name_log = agents.answer_agent.name
            logging.info(f"UID {uid}: --- Running Answer Agent (using key ...{api_key_for_this_entire_request[-4:]}) ---")
            answer_prompt_text = f"{history_text}**User Query**\n{user_prompt_text}\n\nPlease provide a helpful design-related answer."
            answer_content = google_genai_types.Content(role='user', parts=[google_genai_types.Part(text=answer_prompt_text)])
            
            answer_text = await adk_utils.run_adk_interaction(
                agents.answer_agent, answer_content, adk_utils.session_service,
                user_id=uid, api_key=api_key_for_this_entire_request # Use the held key
            )
            if not answer_text :
                 logging.info(f"UID {uid}: Answer agent returned empty response. Providing default.")
                 final_result = "I could not find specific information regarding your query at the moment."
            elif answer_text.startswith("AGENT_ERROR:") or answer_text.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Answer Agent failed or returned error: {answer_text}")
            else:
                final_result = answer_text
            logging.info(f"UID {uid}: Answer flow successful.")
        
        else:
            logging.error(f"UID {uid}: Internal error - Unhandled intent '{intent_mode}'.")
            return jsonify({"success": False, "error": f"Internal error: Unhandled intent type '{intent_mode}'."}), 500

    except ValueError as ve:
        error_message = str(ve)
        logging.error(f"UID {uid}: ValueError during '{agent_used_name_log}' execution: {error_message}", exc_info=False) # Set exc_info based on verbosity preference
        return jsonify({"success": False, "error": error_message}), 200
    except Exception as e:
        error_message = f"An unexpected error occurred during '{agent_used_name_log}' execution."
        logging.error(f"UID {uid}: {error_message} Details: {e}", exc_info=True)
        return jsonify({"success": False, "error": "An internal server error occurred."}), 500
    finally:
        # --- Release the pooled project IF it was acquired for this request ---
        if project_in_use_for_this_request: # This implies run_interaction_method was 'pooled_key'
            await api_handler.release_project(project_in_use_for_this_request)
            logging.info(f"UID {uid}: Released pooled project '{project_in_use_for_this_request['id']}' after request completion/failure.")

    # --- Format and Return Success Response ---
    if final_result is None and not (intent_mode_raw.startswith("AGENT_ERROR:") or intent_mode_raw.startswith("ADK_RUNTIME_ERROR:")) : # Check if error already handled
         logging.error(f"UID {uid}: Execution completed for '{agent_used_name_log}' but final_result is unexpectedly None for mode '{intent_mode}'.")
         return jsonify({"success": False, "error": "Agent processing failed to produce a result."}), 500

    user_history = chat_history.get(uid, [])
    user_history.append({'uid': uid, 'user': user_prompt_text, 'AI': final_result if isinstance(final_result, str) else "SVG content generated."})
    chat_history[uid] = user_history[-MAX_CHAT_HISTORY:]
    
    response_payload = {
        "success": True,
        "mode": final_type,
        "requests_today": requests_today,
        "using_own_key": run_interaction_method == 'user_key'
    }
    if final_type == "svg":
        response_payload["svg"] = final_result
    elif final_type == "answer":
        response_payload["answer"] = final_result
    
    key_info = f"(User's key)" if run_interaction_method == 'user_key' else f"(Pooled project: {project_in_use_for_this_request['id'] if project_in_use_for_this_request else 'N/A'})"
    logging.info(f"UID {uid}: Request completed successfully (type: {final_type}) {key_info}. Trial count: {requests_today}.")
    return jsonify(response_payload), 200


# --- Provide Firebase Client Config to UI (Unchanged) ---
@app.route('/firebase-config', methods=['GET'])
def firebase_config():
     if not config.FIREBASE_CLIENT_CONFIG:
         logging.error("Firebase client config is not loaded.")
         return jsonify({"error": "Firebase client configuration is not available on the backend."}), 500
     return jsonify(config.FIREBASE_CLIENT_CONFIG), 200


# --- Run the App (Unchanged) ---
if __name__ == '__main__':
    logging.info(f"Running Flask app with AGENT_MODEL='{config.AGENT_MODEL}'")
    logging.info("Ensure Firebase Admin SDK is initialized (via import of firebase_admin_init).")
    logging.info("Ensure Firebase Client Config JSON and ENCRYPTION_KEY are set in .env and parsed.")

    from hypercorn.config import Config as HypercornConfig
    import hypercorn.asyncio

    async def serve_app():
        await api_handler.initialize_project_pool() # Initialize pool
        
        hypercorn_config_obj = HypercornConfig()
        hypercorn_config_obj.bind = ["0.0.0.0:5001"]
        # hypercorn_config_obj.accesslog = "-"

        await hypercorn.asyncio.serve(app, hypercorn_config_obj)

    try:
        asyncio.run(serve_app())
    except KeyboardInterrupt:
        logging.info("\nServer stopped by user.")
    except Exception as e:
         logging.error(f"Server failed to start or run: {e}", exc_info=True)