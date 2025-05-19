# adk_utils.py
import re
import base64
import asyncio
import uuid
import io
import os # Import os to modify environment variable
from cryptography.fernet import Fernet # Import Fernet

# --- ADK Imports ---
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.adk.agents import Agent # Import Agent for type hinting
from google.genai import types as google_genai_types # For Content/Part

# --- Local Imports ---
from config import APP_NAME, ENCRYPTION_KEY # Import configured app name and encryption key

# --- Initialize Fernet ---
# Ensure the encryption key is valid before initializing Fernet
try:
    if ENCRYPTION_KEY:
        fernet = Fernet(ENCRYPTION_KEY)
        print("Fernet encryption initialized.")
    else:
        fernet = None
        print("WARNING: ENCRYPTION_KEY is not set. Encryption/Decryption functions will not work.")
except Exception as e:
     fernet = None
     print(f"ERROR: Failed to initialize Fernet with provided key: {e}. Encryption/Decryption will not work.")


# --- Encryption/Decryption Helpers ---
def encrypt_api_key(api_key: str) -> str | None:
    """Encrypts a string using Fernet."""
    if not fernet:
        print("Encryption key not available or invalid. Cannot encrypt.")
        return None
    try:
        # API key should be bytes for Fernet
        encrypted_bytes = fernet.encrypt(api_key.encode())
        return encrypted_bytes.decode() # Return as string for storage
    except Exception as e:
        print(f"Error during encryption: {e}")
        return None

def decrypt_api_key(encrypted_api_key: str) -> str | None:
    """Decrypts a string using Fernet."""
    if not fernet:
        print("Encryption key not available or invalid. Cannot decrypt.")
        return None
    if not encrypted_api_key:
        return None # Cannot decrypt empty string
    try:
        # Encrypted key is a string, encode back to bytes
        decrypted_bytes = fernet.decrypt(encrypted_api_key.encode())
        return decrypted_bytes.decode() # Return as original string
    except Exception as e:
        print(f"Error during decryption: {e}. The key might be invalid or the decryption key is wrong.")
        return None


# --- ADK Session Service (Single instance for the application) ---
# Note: InMemorySessionService is not persistent. For a production app, use
# a persistent storage solution like Firestore or a database.
# If using a persistent session service, it might handle per-user history
# automatically if you configure it correctly.
session_service = InMemorySessionService()
print("ADK InMemorySessionService initialized.")

# --- Helper Function to Validate SVG (remains the same) ---
def is_valid_svg(svg_string):
    """
    Validates whether the input string is a plausible SVG content.
    Strips optional code block markers and checks for basic SVG structure.
    Returns the cleaned string if valid, False otherwise.
    """
    if not svg_string or not isinstance(svg_string, str):
        return False

    # Remove markdown-style code block indicators like ```svg, ```xml, or backticks
    svg_clean = re.sub(r'^\s*```(?:svg|xml)?\s*', '', svg_string.strip(), flags=re.IGNORECASE)
    svg_clean = re.sub(r'\s*```\s*$', '', svg_clean, flags=re.IGNORECASE)

    # Normalize whitespace and lowercase for tag checks
    svg_clean_lower = svg_clean.lower()

    # Check presence of basic opening and closing SVG tags
    has_svg_start = '<svg' in svg_clean_lower
    has_svg_end = '</svg>' in svg_clean_lower

    # Ensure final tag closes properly
    ends_with_gt = svg_clean.strip().endswith('>')

    # Basic check: ensure the string starts roughly where an SVG should
    starts_with_lt = svg_clean.strip().startswith('<')

    # Return the cleaned SVG string if validation passes
    if has_svg_start and has_svg_end and ends_with_gt and starts_with_lt:
        return svg_clean.strip()
    else:
        # print(f"Validation failed for SVG snippet: {svg_string[:200]}...")
        return False # Return False if validation fails


# --- ADK Interaction Runner ---

# Modify the function to accept an optional API key
async def run_adk_interaction(agent_to_run: Agent, user_content: google_genai_types.Content, session_service_instance: InMemorySessionService, user_id: str = "figma_user", api_key: str | None = None):
    """
    Runs a single ADK agent interaction using a temporary session and returns the final text response.
    Optionally uses a specific API key instead of the default GOOGLE_API_KEY env var.
    """
    final_response_text = None
    # Create a unique session ID per agent call within a single request cycle
    # Note: This means history is NOT preserved *between* different agent calls
    # for the same user within one /generate request, only within the single
    # agent's run. If conversational memory is needed across agent steps
    # (e.g., modify agent remembering something from a previous create call),
    # the session management logic needs to be different (e.g., pass a consistent
    # session ID throughout the /generate request flow).
    session_id = f"session_{uuid.uuid4()}"

    original_api_key_env = os.environ.get("GOOGLE_API_KEY") # Store original env var

    try:
        # Create a temporary session for this specific agent interaction
        # Using session_service_instance ensures we use the single app instance
        session = session_service_instance.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        # print(f"Running agent '{agent_to_run.name}' in temporary session '{session_id}' for user '{user_id}'...")

        # --- Temporarily set the environment variable if a user API key is provided ---
        if api_key:
            # print(f"Using user-provided API key for agent '{agent_to_run.name}'...")
            os.environ["GOOGLE_API_KEY"] = api_key
        # else:
            # print(f"Using server's default API key for agent '{agent_to_run.name}'...")


        runner = Runner(
            agent=agent_to_run,
            app_name=APP_NAME,
            session_service=session_service_instance # Use the passed instance
        )

        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=user_content
        ):
            # print(f"  [Event] Author: {event.author}, Type: {type(event).__name__}, Final: {event.is_final_response()}, Action: {event.actions}") # Debug logging

            # Handle final response
            if event.is_final_response():
                if event.content and event.content.parts:
                    # Concatenate all text parts that are not None
                    final_response_text = "".join(str(part.text) for part in event.content.parts if part.text is not None)
                    # print(f"  Final response text received (len={len(final_response_text or '')}).")

                # Check for escalation *even* on final response event
                if event.actions and event.actions.escalate:
                    error_msg = f"Agent escalated: {event.error_message or 'No specific message.'}"
                    print(f"  ERROR: {error_msg}")
                    final_response_text = f"AGENT_ERROR: {error_msg}" # Propagate error
                break # Stop processing events once final response or escalation found

            # Handle explicit escalation before final response
            elif event.actions and event.actions.escalate:
                 error_msg = f"Agent escalated before final response: {event.error_message or 'No specific message.'}"
                 print(f"  ERROR: {error_msg}")
                 final_response_text = f"AGENT_ERROR: {error_msg}" # Propagate error
                 break # Stop processing events

    except Exception as e:
         print(f"Exception during ADK run_async for agent '{agent_to_run.name}' for user '{user_id}': {e}")
         final_response_text = f"ADK_RUNTIME_ERROR: {e}" # Propagate exception message
    finally:
         # --- Restore the original environment variable ---
         if api_key:
             if original_api_key_env is None:
                 del os.environ["GOOGLE_API_KEY"]
             else:
                 os.environ["GOOGLE_API_KEY"] = original_api_key_env
             # print("Restored original GOOGLE_API_KEY environment variable.")

         # Clean up the temporary session
         try:
             # It's safer to check if the session exists before deleting
             if session_service_instance.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id):
                 session_service_instance.delete_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
                 # print(f"Cleaned up session '{session_id}'.")
             # else:
                  # print(f"Temporary session '{session_id}' not found for cleanup (might have failed early).")
         except Exception as delete_err:
             print(f"Warning: Failed to delete temporary session '{session_id}': {delete_err}")

    # print(f"Agent '{agent_to_run.name}' finished for user '{user_id}'. Result: {'<empty>' if not final_response_text else final_response_text[:100] + '...'}")
    return final_response_text


# Export necessary items
__all__ = [
    "session_service",
    "is_valid_svg",
    "run_adk_interaction", # Export the modified function
    "encrypt_api_key", # Export encryption/decryption helpers
    "decrypt_api_key",
]