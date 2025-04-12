import os
import base64
import asyncio
import uuid # For unique session IDs per request
import warnings
import logging

# --- ADK Imports ---
from google.adk.agents import Agent
# from google.adk.models.lite_llm import LiteLlm # Not needed if only using Gemini
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.genai import types as google_genai_types # For Content/Part

# --- Flask Imports ---
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

# --- Configuration ---
warnings.filterwarnings("ignore") # Suppress warnings if needed
logging.basicConfig(level=logging.ERROR) # Keep logging minimal

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if not GOOGLE_API_KEY:
    raise ValueError("Missing GOOGLE_API_KEY in .env file")

# Configure ADK environment variables
os.environ["GOOGLE_API_KEY"] = GOOGLE_API_KEY
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False" # Use GenAI API directly

print(f"Google API Key set: {'Yes' if GOOGLE_API_KEY else 'No'}")
print("ADK Environment Configured.")

# --- Flask App Setup ---
app = Flask(__name__)
CORS(app)

# --- ADK Session Service (Single instance is usually fine for stateless requests) ---
session_service = InMemorySessionService()
APP_NAME = "figma_ai_assistant" # Consistent app name for sessions

# --- ADK Model Configuration ---
# Use a model appropriate for your tasks (text and vision)
AGENT_MODEL = "gemini-2.0-flash-exp" # Unified model capable of both

# --- Agent Definitions ---

# Agent for Creating Designs (Handles initial prompt)
create_agent = Agent(
    name="svg_creator_agent_v1",
    model=AGENT_MODEL,
    description="Generates SVG code for UI designs based on textual descriptions.",
    instruction="""You are an expert UI Designer specializing in creating modern, clean, and visually appealing SVG designs directly from prompts, optimized for Figma import.

Task: Generate SVG code for a UI component or layout based *only* on the user's request provided in the input message.

Response Format:
*   Output ONLY the raw, valid SVG code (starting with <svg> and ending with </svg>).
*   ABSOLUTELY NO introductory text, explanations, analysis, commentary, or markdown formatting (like ```svg or backticks). Your entire response must be the SVG code itself.
*   Use descriptive group IDs (<g id="...">) for logical sections (e.g., "button-group", "card-header").
*   Use standard SVG features compatible with Figma (paths, rects, circles, text, linear/radial gradients, groups). Avoid complex filters or scripts.
*   Use rounded corners (rx, ry attributes) for a modern feel.
*   Use placeholder shapes (rectangles with fill like #E0E0E0) for images.
*   Use simple circles or text emojis for icons; avoid complex icon paths.
*   Keep text concise and use appropriate text-anchor/alignment attributes. Use font-family="sans-serif".
*   Ensure elements have reasonable spacing and do not overlap unnecessarily.
*   Define colors directly.
*   Set a viewBox and width/height on the root <svg> element appropriate for the content.
""",
    # No external tools needed for this agent, the LLM is the generator
    tools=[],
)
print(f"Agent '{create_agent.name}' created using model '{AGENT_MODEL}'.")

# Agent for Modifying Designs (Handles prompt + image)
modify_agent = Agent(
    name="svg_modifier_agent_v1",
    model=AGENT_MODEL, # Needs vision capability
    description="Modifies a specific element within a UI design based on textual instructions and an image context, outputting SVG for the modified element.",
    instruction="""You are an expert Figma UI/UX designer modifying a specific element within a UI design based on user request and an image.

Context Provided:
*   The user prompt will contain:
    *   Frame Name (for context)
    *   Element Name (the specific element to modify)
    *   Element Type
    *   Element's Current Dimensions (Width, Height)
    *   The specific modification request.
*   An image of the current frame containing the element will also be provided.

Task: Analyze the provided image and context. Identify the specified element. Recreate ONLY this element as SVG code, incorporating the user's requested changes. Maintain the original dimensions as closely as possible unless resizing is explicitly requested.

Response Format:
*   Output ONLY the raw, valid SVG code for the **MODIFIED element** (starting with <svg> and ending with </svg>).
*   The SVG's root element should represent the complete modified element.
*   ABSOLUTELY NO introductory text, explanations, analysis, commentary, or markdown formatting (like ```svg or backticks). Your entire response must be the SVG code itself.
*   Ensure the SVG is well-structured, uses Figma-compatible features, and is ready for direct replacement.
*   Use placeholder shapes (#E0E0E0) for any internal images if needed. Use simple circles/emojis for icons.
*   Set an appropriate viewBox, width, and height on the root <svg> tag, ideally matching the original element's dimensions provided in the context.
""",
     # No external tools needed, relies on Vision model capability
    tools=[],
)
print(f"Agent '{modify_agent.name}' created using model '{AGENT_MODEL}'.")


# --- Helper Function to Validate SVG ---
def is_valid_svg(svg_string):
    """Basic check if the string looks like SVG."""
    if not svg_string or not isinstance(svg_string, str):
        return False
    trimmed = svg_string.strip()
    # More robust check for start/end tags, ignoring potential XML declaration
    svg_start = trimmed.lower().find("<svg")
    svg_end = trimmed.lower().rfind("</svg>")
    return svg_start != -1 and svg_end != -1 and svg_start < svg_end and trimmed.endswith(">")


# --- API Endpoint ---
@app.route('/generate', methods=['POST'])
def handle_generate():
    """Handles requests using ADK agents."""
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 400

    data = request.get_json()
    mode = data.get('mode')
    user_prompt = data.get('userPrompt')
    context = data.get('context', {}) # Contains frameName, elementInfo
    image_data_base64 = data.get('imageDataBase64') # Only for modify

    # --- Input Validation ---
    if not mode or mode not in ['create', 'modify']:
        return jsonify({"success": False, "error": "Missing or invalid 'mode'"}), 400
    if not user_prompt:
        return jsonify({"success": False, "error": "Missing 'userPrompt'"}), 400
    if mode == 'modify' and not image_data_base64:
         return jsonify({"success": False, "error": "Missing 'imageDataBase64' for modify mode"}), 400
    if mode == 'modify' and not context.get('elementInfo'):
         return jsonify({"success": False, "error": "Missing 'elementInfo' in context for modify mode"}), 400

    print(f"Received request: mode='{mode}', prompt='{user_prompt[:50]}...'")

    # --- Select Agent ---
    agent_to_run = create_agent if mode == 'create' else modify_agent

    # --- Prepare Agent Input ---
    message_parts = [google_genai_types.Part(text=user_prompt)]

    if mode == 'modify':
        try:
            # Decode Base64 image data for the vision agent
            image_bytes = base64.b64decode(image_data_base64)
            # ADK expects image data in a Part
            image_part = google_genai_types.Part(
                inline_data=google_genai_types.Blob(
                    mime_type="image/png", # Assuming PNG from frontend
                    data=image_bytes
                )
            )
            # Prepend image part for vision model processing order
            message_parts.insert(0, image_part)
            print("Image part prepared for modify agent.")
        except Exception as e:
            print(f"Error decoding base64 image: {e}")
            return jsonify({"success": False, "error": "Invalid image data received"}), 400

    # Create the user message content in ADK format
    user_content = google_genai_types.Content(role='user', parts=message_parts)

    # --- Define Async Function to Run ADK Interaction ---
    async def run_adk_interaction():
        final_response_text = None
        # Use a unique session ID for each request to ensure isolation
        session_id = f"session_{uuid.uuid4()}"
        user_id = "figma_user" # Or derive from request if needed

        # Create a temporary session for this request
        session = session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        print(f"Running agent '{agent_to_run.name}' in session '{session_id}'...")

        # Create a runner for this specific agent and session
        runner = Runner(
            agent=agent_to_run,
            app_name=APP_NAME,
            session_service=session_service
        )

        try:
            # Execute the agent asynchronously
            async for event in runner.run_async(
                user_id=user_id, session_id=session_id, new_message=user_content
            ):
                # print(f"  [Event] Author: {event.author}, Type: {type(event).__name__}, Final: {event.is_final_response()}") # Debug logging
                if event.is_final_response():
                    if event.content and event.content.parts:
                        # Assuming the SVG is in the first text part
                        final_response_text = event.content.parts[0].text
                    elif event.actions and event.actions.escalate:
                         # Handle cases where the agent explicitly escalates/fails
                         error_msg = f"Agent escalated: {event.error_message or 'No specific message.'}"
                         print(error_msg)
                         # Propagate the error message back
                         final_response_text = f"AGENT_ERROR: {error_msg}"
                    break # Stop processing events once final response or escalation found
        except Exception as e:
             print(f"Exception during ADK run_async: {e}")
             # Propagate exception message
             final_response_text = f"ADK_RUNTIME_ERROR: {e}"
        finally:
             # Clean up the temporary session (optional but good practice)
             try:
                 session_service.delete_session(user_id=user_id, session_id=session_id)
                 # print(f"Cleaned up session '{session_id}'.")
             except Exception as delete_err:
                 print(f"Warning: Failed to delete temporary session '{session_id}': {delete_err}")


        return final_response_text # Return the extracted text or error string

    # --- Execute ADK Interaction within Flask Request ---
    try:
        # Run the async ADK logic within the synchronous Flask route
        svg_result = asyncio.run(run_adk_interaction())

        # --- Process and Validate Response ---
        if not svg_result:
             print("ADK interaction did not produce a final response.")
             return jsonify({"success": False, "error": "AI agent did not produce a response."}), 500 # Internal server error

        # Check for propagated errors
        if svg_result.startswith("AGENT_ERROR:") or svg_result.startswith("ADK_RUNTIME_ERROR:"):
             error_msg = svg_result.split(":", 1)[1].strip()
             print(f"Error from ADK interaction: {error_msg}")
             # Return 200 OK but with success: False so UI shows the specific error
             return jsonify({"success": False, "error": error_msg}), 200


        print("ADK Response received, validating SVG...")
        # Cleanup just in case (remove potential markdown still)
        svg_result = svg_result.replace("```svg", "").replace("```", "").strip()

        if not is_valid_svg(svg_result):
            print(f"Validation Failed: ADK response is not valid SVG. Response:\n{svg_result[:200]}...")
            error_msg = f"AI response was not valid SVG. Please try rephrasing. Snippet: {svg_result[:150]}..."
            # Return 200 OK but with success: False
            return jsonify({"success": False, "error": error_msg}), 200

        print("SVG Validation successful.")
        return jsonify({"success": True, "svg": svg_result})

    except Exception as e:
        # Catch broader exceptions during asyncio.run or validation
        error_message = f"An unexpected error occurred: {e}"
        print(error_message)
        return jsonify({"success": False, "error": error_message}), 500


# --- Run the App ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True) # Turn debug=False in production