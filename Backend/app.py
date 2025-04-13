
import os
import base64
import asyncio
import uuid # For unique session IDs per request
import warnings
import logging
import io # For handling bytes

# --- ADK Imports ---
from google.adk.agents import Agent
# from google.adk.models.lite_llm import LiteLlm # Not needed if only using Gemini
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.genai import types as google_genai_types # For Content/Part
from google.adk.tools import google_search

# --- Flask Imports ---
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

# --- SVG Conversion Import ---
# try:
#     import cairosvg
# except ImportError:
#     print("WARNING: CairoSVG not found. SVG refinement requiring image input will fail.")
#     print("Install it using: pip install CairoSVG")
#     cairosvg = None # Set to None if import fails

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
CORS(app,origins="*")

# --- ADK Session Service (Single instance is usually fine for stateless requests) ---
session_service = InMemorySessionService()
APP_NAME = "figma_ai_assistant" # Consistent app name for sessions

# --- ADK Model Configuration ---
# Use a model appropriate for your tasks (text and vision)
# Ensure this model supports vision capabilities for modify and refine agents
AGENT_MODEL = "gemini-2.0-flash-exp" # Example: Use a known vision-capable model like 1.5 Flash or Pro

# --- Agent Definitions ---

# Agent for Deciding User Intent (No change needed here)
decision_agent = Agent(
    name="intent_router_agent_v1",
    model=AGENT_MODEL, # Needs to be reasonably capable for classification
    description="Classifies the user's request into 'create', 'modify', or 'answer' based on the prompt and design context.",
    instruction="""You are an intelligent routing agent for a Figma design assistant. Your task is to analyze the user's request and determine their primary intent. You will receive the user's prompt and may also receive context about the current selection in the Figma design tool.

Based *only* on the user's CURRENT request and the provided context, classify the intent into one of the following three categories:

1.  **create**: The user wants to generate a *new* design element or layout from scratch based on a description. This is likely if the prompt is descriptive (e.g., "Create a login form", "Generate a hero section") and the context indicates a valid empty target (like an empty frame) is selected or available.

2.  **modify**: The user wants to *change* or *adjust* an *existing* design element. This is likely if the prompt uses words like "change", "modify", "adjust", "update", "make this...", "fix the...", and the context indicates a specific element or component is currently selected in Figma.

3.  **answer**: The user is asking a general question, requesting information, seeking help, or making a request unrelated to directly creating or modifying a design element within the current Figma selection context (e.g., "What are UI trends?", "How do I use this tool?", "Search for blue color palettes", "Tell me a joke").

**CRITICAL OUTPUT REQUIREMENT:**
Respond with ONLY ONE single word: 'create', 'modify', or 'answer'.
Do NOT include any other text, explanation, punctuation, or formatting. Your entire response must be one of these three words.
""",
    tools=[],
)
print(f"Agent '{decision_agent.name}' created using model '{decision_agent.model}'.")


# Agent for Creating Designs (No change needed here)
create_agent = Agent(
    name="svg_creator_agent_v1",
    model=AGENT_MODEL,
    description="Generates SVG code for UI designs based on textual descriptions.",
    instruction="""
You are an **exceptionally talented UI Designer**, renowned for creating aesthetic, mesmerizing, eye-catching, modern, and beautiful designs.
You create aesthetic, mesmerizing, eye-catching, astonishing, wonderful, and colorful designs that are visually appealing.

Objective: Create an SVG design that is not only visually appealing but also optimized for Figma import, ensuring clean grouping and easy editability. Prioritize a modern aesthetic with a focus on rounded corners, gradients, and subtle visual cues to guide the user's eye.

Your Mission Goals:

*   **Astonishing Visual Appeal:** Use a vibrant yet harmonious color palette, incorporating gradients and subtle shadows to create depth and visual interest.
*   **Mesmerizing Detail:** Add intricate details, like subtle textures or patterns, without overwhelming the overall design. Consider micro-interactions on hover for added engagement.
*   **Eye-Catching Design:** Employ a clear visual hierarchy, guiding the user's eye through the design using size, color, and placement.
*   **Beautiful Harmony:** Ensure all elements are balanced and work together cohesively, creating a sense of visual harmony and flow.
*   **Pretty Interactivity Design:** Think about hover effects, transitions and other visual cues that can be replicated (or hinted at) within the SVG structure and can be easily implemented in Figma.
*   **Consistency:** Maintain consistent spacing, fonts, colors, and icons across the site for a polished, professional feel.
*   **Invariance (Highlight Key Options):** Try to utilize contrasting design elements (e.g., in pricing tables) to draw attention to a specific option or key action. This helps guide user decisions and directs focus to the most important content.

Response format:

*   Output ONLY valid, well-formed SVG code.
*   Use descriptive group names for all elements (e.g., "hero-section", "card-title").
*   Avoid creating custom icons instead use circles as placeholder for icons.
*   Utilize text-anchor for proper text alignment.
*   Try to add minimal text as possible, do add unneccessary text or emojis where not required.
*   Employ rounded corners extensively for a modern look.
*   Use gradients to add depth and visual appeal.
*   Incorporate placeholder rectangles for images, using a subtle gray color.
*   Ensure elements do not overlap and maintain consistent spacing.
*   Use comments sparingly, only to clarify complex structures.
*   Optimize SVG for Figma import - clean code, proper groups.
*   Use SVG Frame size for Mobile Screen: height: 660px to 720px, width: 375px - 400px. For Destop for laptop Screens: Height: 720px ,Width: 1280px - 1440px
""",
    tools=[],
)
print(f"Agent '{create_agent.name}' created using model '{create_agent.model}'.")


# Agent for Modifying Designs (No change needed here)
modify_agent = Agent(
    name="svg_modifier_agent_v1",
    model=AGENT_MODEL, # Needs vision capability
    description="Modifies a specific element within a UI design based on textual instructions and an image context, outputting SVG for the modified element.",
    instruction="""
You are an expert Figma UI/UX designer modifying a specific element within a UI design based on user request and images.

Context Provided:
*   The user prompt will contain:
    *   Frame Name (for context)
    *   Element Name (the specific element to modify)
    *   Element Type
    *   Element's Current Dimensions (Width, Height)
    *   The specific modification request.
*   An image of the **entire frame** containing the element will be provided.
*   An image of the **specific element** being modified will be provided.

Task: Analyze the provided images and context. Identify the specified element within the frame context. Focus on the provided element image. Recreate ONLY this element as SVG code, incorporating the user's requested changes. Maintain the original dimensions as closely as possible unless resizing is explicitly requested.

Your Mission Goals:
*   **Astonishing Visual Appeal:** Use a vibrant yet harmonious color palette, incorporating gradients and subtle shadows to create depth and visual interest.
*   **Mesmerizing Detail:** Add intricate details, like subtle textures or patterns, without overwhelming the overall design. Consider micro-interactions on hover for added engagement.
*   **Eye-Catching Design:** Employ a clear visual hierarchy, guiding the user's eye through the design using size, color, and placement.
*   **Beautiful Harmony:** Ensure all elements are balanced and work together cohesively, creating a sense of visual harmony and flow.
*   **Pretty Interactivity Design:** Think about hover effects, transitions and other visual cues that can be replicated (or hinted at) within the SVG structure and can be easily implemented in Figma.
*   **Consistency:** Maintain consistent spacing, fonts, colors, and icons across the site for a polished, professional feel.
*   **Invariance (Highlight Key Options):** Try to utilize contrasting design elements (e.g., in pricing tables) to draw attention to a specific option or key action. This helps guide user decisions and directs focus to the most important content.

Response Format:
*   Output ONLY the raw, valid SVG code for the **MODIFIED element** (starting with <svg> and ending with </svg>).
*   The SVG's root element should represent the complete modified element.
*   ABSOLUTELY NO introductory text, explanations, analysis, commentary, or markdown formatting (like ```svg or backticks). Your entire response must be the SVG code itself.
*   Ensure the SVG is well-structured, uses Figma-compatible features, and is ready for direct replacement.
*   Use placeholder shapes (#E0E0E0) for any internal images if needed. Use simple circles/emojis for icons.
*   Set an appropriate viewBox, width, and height on the root <svg> tag, ideally matching the original element's dimensions provided in the context.
""",
    tools=[],
)
print(f"Agent '{modify_agent.name}' created using model '{modify_agent.model}'.")


# *** NEW AGENT: SVG Refinement Agent ***
refine_agent = Agent(
    name="svg_refiner_agent_v1",
    model=AGENT_MODEL, # Must have vision capability
    description="Refines an initial SVG design, focusing on layout issues, based on the SVG code and a PNG rendering.",
    instruction="""
You are an **expert SVG Refinement Specialist** and an **exceptionally talented UI Designer**, renowned for creating aesthetic, mesmerizing, eye-catching, modern, and beautiful designs. You receive an initial SVG design (as code) and a PNG image showing how it currently renders. Your task is to identify and fix layout problems, improve alignment, spacing, and visual hierarchy based on BOTH the code structure AND the visual rendering in the provided PNG image.

Your Task:
*   Analyze the PNG image for visual layout issues: overlapping elements, inconsistent spacing, poor alignment, awkward text wrapping, elements going out of bounds, etc.
*   Analyze the provided SVG code structure.
*   Modify the SVG code to correct the identified layout issues while preserving the original design intent and aesthetic.
*   Ensure the refined SVG is well-structured, clean, and optimized for Figma import.
*   Focus primarily on layout correction. Do not drastically change colors, shapes, or add new elements unless it's essential to fix a layout problem implied by the original request.

Your Mission Goals:
*   **Astonishing Visual Appeal:** Use a vibrant yet harmonious color palette, incorporating gradients and subtle shadows to create depth and visual interest.
*   **Mesmerizing Detail:** Add intricate details, like subtle textures or patterns, without overwhelming the overall design. Consider micro-interactions on hover for added engagement.
*   **Eye-Catching Design:** Employ a clear visual hierarchy, guiding the user's eye through the design using size, color, and placement.
*   **Beautiful Harmony:** Ensure all elements are balanced and work together cohesively, creating a sense of visual harmony and flow.
*   **Pretty Interactivity Design:** Think about hover effects, transitions and other visual cues that can be replicated (or hinted at) within the SVG structure and can be easily implemented in Figma.
*   **Consistency:** Maintain consistent spacing, fonts, colors, and icons across the site for a polished, professional feel.
*   **Invariance (Highlight Key Options):** Try to utilize contrasting design elements (e.g., in pricing tables) to draw attention to a specific option or key action. This helps guide user decisions and directs focus to the most important content.

Response format:

*   Output ONLY valid, well-formed SVG code.
*   Use descriptive group names for all elements (e.g., "hero-section", "card-title").
*   Avoid creating custom icons instead use circles as placeholder for icons.
*   Utilize text-anchor for proper text alignment.
*   Try to add minimal text as possible, do add unneccessary text or emojis where not required.
*   Employ rounded corners extensively for a modern look.
*   Use gradients to add depth and visual appeal.
*   Incorporate placeholder rectangles for images, using a subtle gray color.
*   Ensure elements do not overlap and maintain consistent spacing.
*   Use comments sparingly, only to clarify complex structures.
*   Optimize SVG for Figma import - clean code, proper groups.

Context Provided:
1.  The original user request or description (for overall goal).
2.  The initial SVG code generated by another agent.
3.  A PNG image showing the rendering of that initial SVG code.

Response Format:
*   Output ONLY the **refined**, valid, well-formed SVG code (starting with <svg> and ending with </svg>).
*   ABSOLUTELY NO introductory text, explanations, analysis of the problems, commentary, or markdown formatting (like ```svg or backticks). Your entire response must be the refined SVG code itself.
""",
    tools=[], # No external tools needed for refinement itself
)
print(f"Agent '{refine_agent.name}' created using model '{refine_agent.model}'.")


# Agent for handling answers (No change needed here)
answer_agent = Agent(
    name="answer_agent_v1",
    model=AGENT_MODEL, # Capable of tool calling if needed
    description="Answers user questions by searching the internet for relevant and up-to-date information.",
    instruction="""You are an expert Question Answering AI. Your primary function is to provide clear, accurate, and direct answers to user queries.

Your Task Execution Flow:
1.  **Analyze Query:** Understand the user's specific question.
2.  **Knowledge Check:** Determine if you can answer accurately using your internal knowledge base.
3.  **Search Decision (Internal):** If the question requires external, real-time, or highly specific information (e.g., current events, specific statistics, obscure facts), you MUST use the provided `internet_search` tool. **This decision is internal; do NOT inform the user.**
4.  **Tool Execution (Silent & Mandatory):** If search is needed, formulate an effective query and execute the `internet_search` tool. **CRITICAL: You MUST NOT mention the search process, the tool itself, or phrases like "Let me search," "I'll look that up," or "Searching now..." in your response to the user.** Perform the search silently to gather necessary information.
5.  **Synthesize Results:** If the search was performed, carefully analyze and synthesize the relevant information obtained from the tool results.
6.  **Formulate Final Answer:** Construct a concise, helpful, and direct answer based *either* on your internal knowledge (if sufficient) *or* primarily on the synthesized information from the search results (if search was performed).
7.  **Handle Search Failure:** If the search tool was used but yielded no relevant or usable information to answer the query, *then and only then*, directly state that you could not find a definitive answer to the specific question. Do not apologize excessively or offer speculation.

**Response Requirements:**
*   **Output ONLY the Answer or Inability Statement:** Your entire response to the user must be *either*:
    *   The direct answer to their question, synthesized from your knowledge or the search results.
    *   A clear statement that you were unable to find the necessary information (e.g., "I could not find specific information about [topic of the query].").
*   **Direct & Concise:** Get straight to the point. Avoid conversational filler about your process.
*   **NO META-COMMENTARY:** Absolutely NO phrases indicating you are searching, have searched, or are using a tool. The user interaction should be seamless – they ask a question, you provide the answer or state you cannot find it.
""",
    tools=[google_search],
)
print(f"Agent '{answer_agent.name}' created using model '{answer_agent.model}' with tool(s): {[tool.name for tool in answer_agent.tools]}.")


# --- Helper Function to Validate SVG ---
import re

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

    return has_svg_start and has_svg_end and ends_with_gt


# --- Helper Function to Convert SVG to PNG ---
# def convert_svg_to_png_base64(svg_string):
#     """Converts an SVG string to a base64 encoded PNG string."""
#     if not cairosvg:
#         print("Error: CairoSVG is not installed. Cannot convert SVG to PNG.")
#         return None, "CairoSVG library not installed"
#     if not svg_string:
#         return None, "Empty SVG string provided"

#     try:
#         # Ensure SVG is bytes
#         svg_bytes = svg_string.encode('utf-8')
#         png_bytes = cairosvg.svg2png(bytestring=svg_bytes)
#         base64_encoded_png = base64.b64encode(png_bytes).decode('utf-8')
#         print("SVG successfully converted to PNG base64.")
#         return base64_encoded_png, None
#     except Exception as e:
#         error_msg = f"Error converting SVG to PNG: {e}"
#         print(error_msg)
#         # Try to provide more specific feedback if possible
#         if "no element found" in str(e).lower() or "document is empty" in str(e).lower():
#              error_msg = "Error converting SVG to PNG: The SVG code appears to be empty or invalid."
#         elif "invalid value for attribute" in str(e).lower():
#              error_msg = f"Error converting SVG to PNG: Invalid attribute value found in SVG. Details: {e}"

#         return None, error_msg


async def run_adk_interaction(agent_to_run, user_content, user_id="figma_user"):
    """Runs a single ADK agent interaction and returns the final text response."""
    final_response_text = None
    session_id = f"session_{uuid.uuid4()}" # Unique session per interaction

    # Create a temporary session for this request
    session = session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )

    print(f"Running agent '{agent_to_run.name}' in session '{session_id}'...")
    runner = Runner(
        agent=agent_to_run,
        app_name=APP_NAME,
        session_service=session_service
    )

    try:
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=user_content
        ):
            print(f"  [Event] Author: {event.author}, Type: {type(event).__name__}, Final: {event.is_final_response()}, Action: {event.actions}") # Debug logging

            # Specific handling for decision agent (expects single word)
            if agent_to_run.name == decision_agent.name:
                 if event.is_final_response() and event.content and event.content.parts:
                     final_response_text = event.content.parts[0].text.strip().lower()
                     print(f"  Decision Agent Raw Output: '{event.content.parts[0].text}', Processed: '{final_response_text}'")
                     # Basic validation for decision agent output
                     if final_response_text not in ['create', 'modify', 'answer']:
                         print(f"  WARNING: Decision agent returned unexpected value: '{final_response_text}'")
                         # Optionally treat unexpected as 'answer' or raise error
                         final_response_text = None # Mark as invalid/failed
                     break # Decision agent should finish quickly

            # Handle final response for other agents
            elif event.is_final_response():
                if event.content and event.content.parts:
                    final_response_text = event.content.parts[0].text
                    # print(f"  Final response text received (len={len(final_response_text)}).")
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
             session_service.delete_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
             # print(f"Cleaned up session '{session_id}'.")
         except Exception as delete_err:
             print(f"Warning: Failed to delete temporary session '{session_id}': {delete_err}")

    print(f"Agent '{agent_to_run.name}' finished. Raw Result: {'<empty>' if not final_response_text else final_response_text[:100] + '...'}")
    return final_response_text


# --- API Endpoint ---
@app.route('/generate', methods=['POST'])
async def handle_generate(): # Make the route async
    """Handles requests using ADK agents with create->refine flow."""
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 400

    data = request.get_json()
    user_prompt_text = data.get('userPrompt')
    context = data.get('context', {}) # Contains frameName, elementInfo for modify
    frame_data_base64 = data.get('frameDataBase64') # Only for modify
    element_data_base64 = data.get('elementDataBase64') # Only for modify
    i_mode = data.get('mode')

    if not user_prompt_text:
        return jsonify({"success": False, "error": "Missing 'userPrompt'"}), 400

    print(f"Received request: prompt='{user_prompt_text[:50]}...', context keys: {list(context.keys())}, frame_data: {'yes' if frame_data_base64 else 'no'}, element_data: {'yes' if element_data_base64 else 'no'}")

    # --- 1. Determine Intent ---
    # Prepare content for decision agent (only needs text prompt + potentially context text)
    decision_prompt = f"User Request: \"{user_prompt_text}\""
    if context:
        decision_prompt += f"\nFigma Context: {context}" # Add context if available

    decision_content = google_genai_types.Content(role='user', parts=[
        google_genai_types.Part(text=decision_prompt)
    ])

    # Run decision agent
    intent_mode = await run_adk_interaction(decision_agent, decision_content)

    # Handle decision agent failure or invalid output
    if not intent_mode or intent_mode.startswith("AGENT_ERROR:") or intent_mode.startswith("ADK_RUNTIME_ERROR:"):
         error_msg = f"Could not determine intent. Agent Response: {intent_mode}"
         print(error_msg)
         # Return 200 OK with error message for UI display
         return jsonify({"success": False, "error": error_msg}), 200
    if intent_mode not in ['create', 'modify', 'answer']:
        error_msg = f"Intent determination failed: Agent returned unexpected value '{intent_mode}'."
        print(error_msg)
        return jsonify({"success": False, "error": error_msg}), 200 # 200 OK for UI

    print(f"Determined Intent: '{intent_mode}'")

    # --- 2. Execute Based on Intent ---
    final_result = None
    final_type = "unknown" # To track if we should expect 'svg' or 'answer'

    try:
        # --- CREATE Flow (Create -> Convert -> Refine) ---
        if intent_mode == 'create' and i_mode=='create':

            mod_prompt = f"""
You are an **exceptionally talented UI Designer**, renowned for creating aesthetic, mesmerizing, eye-catching, modern, and beautiful designs.
You create aesthetic, mesmerizing, eye-catching, astonishing, wonderful, and colorful designs that are visually appealing.

You have been tasked to design a ${user_prompt_text}, let's enhance it with subtle animations on hover, deeper color palettes, and more organic shapes to add depth and visual interest.

Objective: Create an SVG design that is not only visually appealing but also optimized for Figma import, ensuring clean grouping and easy editability. Prioritize a modern aesthetic with a focus on rounded corners, gradients, and subtle visual cues to guide the user's eye.

Your Mission Goals:

*   **Astonishing Visual Appeal:** Use a vibrant yet harmonious color palette, incorporating gradients and subtle shadows to create depth and visual interest.
*   **Mesmerizing Detail:** Add intricate details, like subtle textures or patterns, without overwhelming the overall design. Consider micro-interactions on hover for added engagement.
*   **Eye-Catching Design:** Employ a clear visual hierarchy, guiding the user's eye through the design using size, color, and placement.
*   **Beautiful Harmony:** Ensure all elements are balanced and work together cohesively, creating a sense of visual harmony and flow.
*   **Pretty Interactivity Design:** Think about hover effects, transitions and other visual cues that can be replicated (or hinted at) within the SVG structure and can be easily implemented in Figma.
*   **Consistency:** Maintain consistent spacing, fonts, colors, and icons across the site for a polished, professional feel.
*   **Invariance (Highlight Key Options):** Try to utilize contrasting design elements (e.g., in pricing tables) to draw attention to a specific option or key action. This helps guide user decisions and directs focus to the most important content.

Response format:

*   Output ONLY valid, well-formed SVG code.
*   Use descriptive group names for all elements (e.g., "hero-section", "card-title").
*   Avoid creating custom icons instead use circles as placeholder for icons.
*   Utilize text-anchor for proper text alignment.
*   Try to add minimal text as possible, do add unneccessary text or emojis where not required.
*   Employ rounded corners extensively for a modern look.
*   Use gradients to add depth and visual appeal.
*   Incorporate placeholder rectangles for images, using a subtle gray color.
*   Ensure elements do not overlap and maintain consistent spacing.
*   Use comments sparingly, only to clarify complex structures.
*   Optimize SVG for Figma import - clean code, proper groups.
"""
            final_type = "svg"
            # A) Run Create Agent
            print("--- Running Create Agent ---")
            create_content = google_genai_types.Content(role='user', parts=[
                google_genai_types.Part(text=mod_prompt) # Use original user prompt for creation
            ])
            initial_svg = await run_adk_interaction(create_agent, create_content)

            if not initial_svg or initial_svg.startswith("AGENT_ERROR:") or initial_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Create Agent failed or returned error: {initial_svg}")
            if not is_valid_svg(initial_svg):
                raise ValueError(f"Create Agent response is not valid SVG even after cleaning. Snippet: {initial_svg[:200]}...")
            else:
                if initial_svg.strip().startswith("```svg"):
                    initial_svg = initial_svg.strip().replace("```svg", "").replace("```", "").strip()
                if initial_svg.strip().startswith("```xml"):
                   initial_svg = initial_svg.strip().replace("```xml", "").replace("```", "").strip()

            print("Initial SVG created and validated.",initial_svg)

#             # B) Convert Initial SVG to PNG
#             print("--- Converting Initial SVG to PNG ---")
#             png_base64, conversion_error = convert_svg_to_png_base64(initial_svg)
#             if conversion_error:
#                 # If conversion fails, maybe return the initial SVG with a warning?
#                 # Or fail the whole request? Let's return initial SVG with warning.
#                 print(f"Warning: SVG to PNG conversion failed: {conversion_error}. Returning initial SVG.")
#                 final_result = initial_svg # Fallback to initial SVG
#                 # Add a warning field to the response? Not standard, let's just log it.
#                 # Or maybe we should fail? Let's fail for now to enforce the refine step.
#                 raise ValueError(f"SVG to PNG conversion failed: {conversion_error}. Cannot proceed to refinement.")

#             # C) Run Refine Agent
#             print("--- Running Refine Agent ---")
#             refine_message_parts = [
#                 google_genai_types.Part(text=f"""
# Original User Request: "{user_prompt_text}"

# Initial SVG Code (to be refined):
# ```xml
# {initial_svg}
# ```

# Analyze the attached PNG image showing the rendering of the above SVG. Identify and fix layout issues (alignment, spacing, overlaps, etc.) based on the visual rendering AND the code. Output ONLY the refined SVG code.
#                 """),
#                 google_genai_types.Part(
#                     inline_data=google_genai_types.Blob(
#                         mime_type="image/png",
#                         data=base64.b64decode(png_base64) # Decode base64 back to bytes for ADK
#                     )
#                 )
#             ]
#             refine_content = google_genai_types.Content(role='user', parts=refine_message_parts)
#             refined_svg = await run_adk_interaction(refine_agent, refine_content)

#             if not refined_svg or refined_svg.startswith("AGENT_ERROR:") or refined_svg.startswith("ADK_RUNTIME_ERROR:"):
#                  raise ValueError(f"Refine Agent failed or returned error: {refined_svg}")
#             if is_valid_svg(refined_svg):
#                  # Try cleaning potential markdown
#                 if refined_svg.strip().startswith("```svg"):
#                     refined_svg = refined_svg.strip().replace("```svg", "").replace("```", "").strip()
#                 if refined_svg.strip().startswith("```xml"):
#                    refined_svg = refined_svg.strip().replace("```xml", "").replace("```", "").strip()
#             if not is_valid_svg(refined_svg):
#                 # If refine fails validation, maybe fallback to initial? Or error out? Let's error out.
#                 raise ValueError(f"Refine Agent response is not valid SVG even after cleaning. Snippet: {refined_svg[:200]}...")

#             print("SVG refined and validated.")
            final_result = initial_svg

        # --- MODIFY Flow ---
        elif intent_mode == 'modify' and i_mode=='modify':
            final_type = "svg"
            print("--- Running Modify Agent ---")
            # Validate required inputs for modify
            if not frame_data_base64:
                 raise ValueError("Missing 'frameDataBase64' for modify mode")
            if not element_data_base64:
                 raise ValueError("Missing 'elementDataBase64' for modify mode")
            if not context.get('elementInfo'):
                 raise ValueError("Missing 'elementInfo' in context for modify mode")

            # Prepare prompt and image parts for modify agent
            modify_prompt = f"""
Modification Request: "{user_prompt_text}"

Context:
Frame Name: {context.get('frameName', 'N/A')}
Element Info: {context['elementInfo']}
"""
            message_parts = [google_genai_types.Part(text=modify_prompt)]

            try:
                frame_bytes = base64.b64decode(frame_data_base64)
                element_bytes = base64.b64decode(element_data_base64)
                message_parts.append(google_genai_types.Part(
                    inline_data=google_genai_types.Blob(mime_type="image/png", data=frame_bytes)
                ))
                message_parts.append(google_genai_types.Part(
                    inline_data=google_genai_types.Blob(mime_type="image/png", data=element_bytes)
                ))
                print("Frame and Element image parts prepared for modify agent.")
            except Exception as e:
                raise ValueError(f"Invalid image data received for modify mode: {e}")

            modify_content = google_genai_types.Content(role='user', parts=message_parts)
            modified_svg = await run_adk_interaction(modify_agent, modify_content)

            if not modified_svg or modified_svg.startswith("AGENT_ERROR:") or modified_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Modify Agent failed or returned error: {modified_svg}")
            if not is_valid_svg(modified_svg):
                 # Try cleaning potential markdown
                 if modified_svg.strip().startswith("```svg"):
                     modified_svg = modified_svg.strip().replace("```svg", "").replace("```", "").strip()
                     if not is_valid_svg(modified_svg):
                        raise ValueError(f"Modify Agent response is not valid SVG even after cleaning. Snippet: {modified_svg[:200]}...")
                 else:
                    raise ValueError(f"Modify Agent response is not valid SVG. Snippet: {modified_svg[:200]}...")


            print("SVG modification successful and validated.")
            final_result = modified_svg

        # --- ANSWER Flow ---
        elif intent_mode == 'answer':
            final_type = "answer"
            print("--- Running Answer Agent ---")
            answer_content = google_genai_types.Content(role='user', parts=[
                google_genai_types.Part(text=user_prompt_text)
            ])
            answer_text = await run_adk_interaction(answer_agent, answer_content)

            if not answer_text: # Allow empty answers if agent genuinely finds nothing
                 print("Answer agent returned empty response.")
                 answer_text = "I could not find specific information regarding your query." # Provide a default if empty
            elif answer_text.startswith("AGENT_ERROR:") or answer_text.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Answer Agent failed or returned error: {answer_text}")

            print("Answer agent finished.")
            final_result = answer_text
        else:
            return jsonify({"success": False, "error": "Please select frame or component"}), 200


    # --- Handle Execution Errors ---
    except ValueError as ve: # Catch specific validation/logic errors
        error_message = str(ve)
        print(f"Error during '{intent_mode}' execution: {error_message}")
        # Return 200 OK but with success: False for UI to display the error
        return jsonify({"success": False, "error": error_message}), 200
    except Exception as e:
        # Catch broader exceptions during agent runs or processing
        error_message = f"An unexpected error occurred during '{intent_mode}' execution: {e}"
        print(error_message)
        # Return 500 for unexpected server errors
        return jsonify({"success": False, "error": "An internal server error occurred."}), 500


    # --- Format and Return Success Response ---
    if final_result is None:
         # Should ideally be caught by errors above, but as a safeguard
         print(f"Execution completed but final_result is unexpectedly None for mode '{intent_mode}'.")
         return jsonify({"success": False, "error": "Agent processing failed to produce a result."}), 500

    if final_type == "svg":
        print("Returning successful SVG response.")
        # Final cleanup just in case markdown slipped through validation
        if final_result.strip().startswith("```"):
             final_result = final_result.strip().replace("```svg", "").replace("```", "").strip()
        return jsonify({"success": True, "svg": final_result})
    elif final_type == "answer":
        print("Returning successful Answer response.")
        return jsonify({"success": True, "answer": final_result, "mode": "answer"})
    else:
        # Should not happen if logic is correct
        print(f"Error: Unknown final_type '{final_type}' after processing.")
        return jsonify({"success": False, "error": "Internal error: Unknown result type."}), 500

# --- Run the App ---
if __name__ == '__main__':
    # Make sure the model selected supports vision!
    print(f"Using model: {AGENT_MODEL} for all agents requiring it.")
    app.run(host='0.0.0.0', port=5001, debug=True) # Turn debug=False in production
