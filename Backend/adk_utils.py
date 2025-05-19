# adk_utils.py
import re
import uuid

# --- ADK Imports ---
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.genai import types as google_genai_types # For Content/Part

# --- Local Imports ---
from config import APP_NAME # Import configured app name

# --- ADK Session Service (Single instance for the application) ---
# Note: InMemorySessionService is not persistent. For a production app, use
# a persistent storage solution like Firestore or a database.
session_service = InMemorySessionService()
print("ADK InMemorySessionService initialized.")

# --- Helper Function to Validate SVG ---

def is_valid_svg(svg_string):
    """
    Validates whether the input string is a plausible SVG content.
    Strips optional code block markers and checks for basic SVG structure.
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

    return has_svg_start and has_svg_end and ends_with_gt and starts_with_lt


# --- ADK Interaction Runner ---

async def run_adk_interaction(agent_to_run, user_content, session_service_instance, user_id="figma_user"):
    """
    Runs a single ADK agent interaction using a temporary session and returns the final text response.
    session_service_instance: The InMemorySessionService instance to use.
    user_content: google_genai_types.Content object representing the user's input.
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

    try:
        # Create a temporary session for this specific agent interaction
        session = session_service_instance.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        print(f"Running agent '{agent_to_run.name}' in temporary session '{session_id}'...")

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
                    # Concatenate all text parts
                    final_response_text = "".join(part.text for part in event.content.parts if part.text)
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
         print(f"Exception during ADK run_async for agent '{agent_to_run.name}': {e}")
         final_response_text = f"ADK_RUNTIME_ERROR: {e}" # Propagate exception message
    finally:
         # Clean up the temporary session
         try:
             # It's safer to check if the session exists before deleting
             if session_service_instance.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id):
                 session_service_instance.delete_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
                 # print(f"Cleaned up session '{session_id}'.")
             else:
                  # print(f"Temporary session '{session_id}' not found for cleanup (might have failed early).")
                  pass # Session might not have been created if an error happened before
         except Exception as delete_err:
             print(f"Warning: Failed to delete temporary session '{session_id}': {delete_err}")

    # print(f"Agent '{agent_to_run.name}' finished. Raw Result: {'<empty>' if not final_response_text else final_response_text[:100] + '...'}")
    return final_response_text

# Export necessary items
__all__ = [
    "session_service",
    "is_valid_svg",
    "run_adk_interaction"
]