# app.py
import base64
import asyncio
import hypercorn

# --- Flask Imports ---
from flask import Flask, request, jsonify
from flask_cors import CORS

# --- ADK Imports ---
from google.genai import types as google_genai_types # For Content/Part

# --- Local Imports ---
import config # Imports GOOGLE_API_KEY, APP_NAME, AGENT_MODEL
import adk_utils # Imports session_service, is_valid_svg, run_adk_interaction
import agents # Imports all agent instances

# --- Flask App Setup ---
app = Flask(__name__)
# Apply CORS *after* Flask app is created
CORS(app, origins="*") # Be cautious with origins="*" in production

# --- Global State (Manual Chat History) ---
# Note: This is a simple in-memory history. For multiple users or persistence,
# this would need to be stored per-user, potentially using the ADK session_service
# if modified to handle conversation history storage and retrieval across requests,
# or a separate database.
chat_history = []
MAX_CHAT_HISTORY = 10 # Keep last N turns

# --- API Endpoint ---
@app.route('/generate', methods=['POST'])
async def handle_generate(): # Make the route async
    """Handles requests using ADK agents with create->refine flow."""
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 400

    global chat_history # Access the global chat history

    data = request.get_json()
    user_prompt_text = data.get('userPrompt')
    context = data.get('context', {}) # Contains frameName, elementInfo for modify
    frame_data_base64 = data.get('frameDataBase64') # Only for modify
    element_data_base64 = data.get('elementDataBase64') # Only for modify
    i_mode = data.get('mode') # Expected mode from frontend ('create', 'modify') - acts as a hint/override?

    if not user_prompt_text:
        return jsonify({"success": False, "error": "Missing 'userPrompt'"}), 400

    print(f"Received request: prompt='{user_prompt_text[:50]}...', mode='{i_mode}', context keys: {list(context.keys()) if context else 'None'}, frame_data: {'yes' if frame_data_base64 else 'no'}, element_data: {'yes' if element_data_base64 else 'no'}")

    # --- 1. Determine Intent ---
    # Prepare content for decision agent (text prompt + potentially context text + history)
    # Format chat history for inclusion in the prompt
    history_text = ""
    if chat_history:
        history_text = "\n".join([f"{item['user']} -> {item['AI'][:100]}{'...' if len(item['AI']) > 100 else ''}" for item in chat_history])
        history_text = f"Previous Conversation Summary:\n{history_text}\n\n"


    decision_prompt = f"""
{history_text}
**User Request**
{user_prompt_text}
"""
    if context:
        decision_prompt += f"\n**Figma Context**\n{context}" # Add context if available

    decision_content = google_genai_types.Content(role='user', parts=[
        google_genai_types.Part(text=decision_prompt)
    ])

    # Run decision agent using the utility function
    intent_mode = await adk_utils.run_adk_interaction(
        agents.decision_agent,
        decision_content,
        adk_utils.session_service # Pass the shared session service instance
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

    # Further sanitize and validate the expected single word
    intent_mode = intent_mode.strip().lower()
    if intent_mode not in ['create', 'modify', 'answer']:
        print(f"WARNING: Decision agent returned unexpected value: '{intent_mode}'. Falling back to 'answer'.")
        intent_mode = 'answer' # Fallback to answer if classification is odd

    print(f"Determined Intent: '{intent_mode}'")

    # --- 2. Execute Based on Intent ---
    final_result = None
    final_type = "unknown" # To track if we should expect 'svg' or 'answer'
    agent_used_name = "None" # For logging

    try:
        # --- CREATE Flow (Refine -> Create) ---
        if intent_mode == 'create' and i_mode=='create':
            final_type = "svg"
            agent_used_name = agents.create_agent.name
            print("--- Initiating Create Flow (Refine -> Create) ---")

            # A) Run Refine Agent to create the structured brief
            print("--- Running Refine Agent ---")
            refine_content = google_genai_types.Content(role='user', parts=[
                google_genai_types.Part(text=user_prompt_text) # Refine the original user prompt
            ])
            refined_prompt = await adk_utils.run_adk_interaction(
                agents.refine_agent,
                refine_content,
                 adk_utils.session_service
            )

            if not refined_prompt or refined_prompt.startswith("AGENT_ERROR:") or refined_prompt.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Refine Agent failed or returned error: {refined_prompt}")

            # Clean potential markdown block from refined prompt
            refined_prompt_clean = refined_prompt.strip().replace("```markdown", "").replace("```", "").strip()
            if not refined_prompt_clean:
                 print("WARNING: Refine agent returned empty or only markdown block.")
                 refined_prompt_clean = user_prompt_text # Fallback to original prompt


            # B) Run Create Agent using the refined prompt (design brief)
            print("--- Running Create Agent with refined prompt ---")
            # Include the design principles *alongside* the refined prompt/brief
            create_instruction_with_brief = f"""
{refined_prompt_clean}

---
Apply the following advanced design principles during creation:

*   **Astonishing Visual Appeal:** Use vibrant yet harmonious color palettes, incorporating gradients and subtle shadows.
*   **Mesmerizing Detail:** Add intricate details, like subtle textures or patterns, without overwhelming. Consider micro-interactions.
*   **Eye-Catching Design:** Employ clear visual hierarchy (size, color, placement).
*   **Beautiful Harmony:** Ensure elements are balanced and cohesive (alignment, proximity, unity).
*   **Pretty Interactivity Design:** Think about hover effects, transitions, and structure for easy implementation.
*   **Consistency:** Maintain consistent spacing, fonts, colors, and icons.
*   **Invariance (Highlight Key Options):** Use contrast to draw attention to key elements.

**Output Requirements (Mandatory):**

*   Output ONLY valid, well-formed SVG code.
*   Use descriptive group names (kebab-case).
*   Use circles (#E0E0E0) as placeholders for icons.
*   Use rectangles (#F0F0F0) as placeholders for images.
*   Use `<text>` elements for text. Apply `text-anchor` and appropriate `y` offset for positioning.
*   Use rounded corners extensively.
*   Use gradients where aesthetically pleasing.
*   Ensure elements do not overlap and maintain consistent spacing (e.g., multiples of 4px/8px).
*   Set SVG `width` (e.g., 390 for mobile, 1440 for desktop) and `height` to accommodate content (variable height).
*   Optimize SVG for Figma import.
"""

            create_content = google_genai_types.Content(role='user', parts=[
                google_genai_types.Part(text=create_instruction_with_brief)
            ])

            initial_svg = await adk_utils.run_adk_interaction(
                agents.create_agent,
                create_content,
                adk_utils.session_service
            )

            if not initial_svg or initial_svg.startswith("AGENT_ERROR:") or initial_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Create Agent failed or returned error: {initial_svg}")

            # Final SVG validation and cleanup
            cleaned_svg = initial_svg.strip()
            if adk_utils.is_valid_svg(initial_svg):
                 # Attempt robust cleaning before giving up
                 if cleaned_svg.startswith("```svg"):
                      cleaned_svg = cleaned_svg.replace("```svg", "").replace("```", "").strip()
                 elif cleaned_svg.startswith("```xml"):
                      cleaned_svg = cleaned_svg.replace("```xml", "").replace("```", "").strip()
                 elif cleaned_svg.startswith("```"):
                       cleaned_svg = cleaned_svg.replace("```", "").strip()

            else: print("Invalid SVG")

            print("Initial SVG created and validated.")
            final_result = cleaned_svg
            # Add conversation turn to history
            if len(chat_history) >= MAX_CHAT_HISTORY:
                chat_history.pop(0) # Remove oldest turn
            chat_history.append({'user': user_prompt_text, 'AI': "I have created the figma design. Let me know if you require any further changes or assistance with anything else."})


        # --- MODIFY Flow (Refine -> Modify) ---
        elif intent_mode == 'modify' and i_mode=='modify':
            final_type = "svg"
            agent_used_name = agents.modify_agent.name
            print("--- Initiating Modify Flow (Refine -> Modify) ---")

            # Validate required inputs for modify mode
            if not frame_data_base64:
                 return jsonify({"success": False, "error": "Missing 'frameDataBase64' for modify mode"}), 400
            if not element_data_base64:
                 return jsonify({"success": False, "error": "Missing 'elementDataBase64' for modify mode"}), 400
            if not context.get('elementInfo'):
                 return jsonify({"success": False, "error": "Missing 'elementInfo' in context for modify mode"}), 400


            # A) Run Refine Agent to create the structured modification brief
            print("--- Running Refine Agent for Modification ---")
            refine_content = google_genai_types.Content(role='user', parts=[
                google_genai_types.Part(text=user_prompt_text) # Refine the original user prompt for modification
            ])
            refined_prompt = await adk_utils.run_adk_interaction(
                agents.refine_agent,
                refine_content,
                adk_utils.session_service
            )

            if not refined_prompt or refined_prompt.startswith("AGENT_ERROR:") or refined_prompt.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Refine Agent failed or returned error during modify flow: {refined_prompt}")

            # Clean potential markdown block from refined prompt
            refined_prompt_clean = refined_prompt.strip().replace("```markdown", "").replace("```", "").strip()
            if not refined_prompt_clean:
                 print("WARNING: Refine agent returned empty or only markdown block for modify.")
                 refined_prompt_clean = user_prompt_text # Fallback

            # B) Prepare prompt and image parts for modify agent
            modify_prompt = f"""
**Modification Brief**
{refined_prompt_clean}

**Figma Context**
Frame Name: {context.get('frameName', 'N/A')}
Element Info: {context['elementInfo']}
"""
            message_parts = [google_genai_types.Part(text=modify_prompt)]

            try:
                # Decode and add images as parts
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
                # Catch specific decoding errors and return client error
                print(f"Invalid image data received: {e}")
                return jsonify({"success": False, "error": f"Invalid image data provided for modification: {e}"}), 400


            modify_content = google_genai_types.Content(role='user', parts=message_parts)

            # C) Run Modify Agent
            print("--- Running Modify Agent ---")
            modified_svg = await adk_utils.run_adk_interaction(
                agents.modify_agent,
                modify_content,
                adk_utils.session_service
            )

            if not modified_svg or modified_svg.startswith("AGENT_ERROR:") or modified_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Modify Agent failed or returned error: {modified_svg}")

            # Final SVG validation and cleanup
            cleaned_svg = modified_svg.strip()
            if adk_utils.is_valid_svg(modified_svg):
                # Attempt robust cleaning before giving up
                 if cleaned_svg.startswith("```svg"):
                      cleaned_svg = cleaned_svg.replace("```svg", "").replace("```", "").strip()
                 elif cleaned_svg.startswith("```xml"):
                      cleaned_svg = cleaned_svg.replace("```xml", "").replace("```", "").strip()
                 elif cleaned_svg.startswith("```"):
                       cleaned_svg = cleaned_svg.replace("```", "").strip()
            else: print("Invalid SVG")
            

            print("SVG modification successful and validated.")
            final_result = cleaned_svg
            # Add conversation turn to history
            if len(chat_history) >= MAX_CHAT_HISTORY:
                chat_history.pop(0) # Remove oldest turn
            chat_history.append({'user': user_prompt_text, 'AI': "I have modified the component. Let me know if you require any further changes or assistance with anything else."})


        # --- ANSWER Flow ---
        elif intent_mode == 'answer':
            final_type = "answer"
            agent_used_name = agents.answer_agent.name
            print("--- Running Answer Agent ---")

            # Prepare prompt for answer agent including history
            answer_prompt = f"""
{history_text}
**User Query**
{user_prompt_text}

Please provide a helpful design-related answer.
"""
            answer_content = google_genai_types.Content(role='user', parts=[
                google_genai_types.Part(text=answer_prompt)
            ])
            answer_text = await adk_utils.run_adk_interaction(
                agents.answer_agent,
                answer_content,
                adk_utils.session_service
            )

            if not answer_text: # Allow empty answers if agent genuinely finds nothing
                 print("Answer agent returned empty response.")
                 final_result = "I could not find specific information regarding your query." # Provide a default if empty
            elif answer_text.startswith("AGENT_ERROR:") or answer_text.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Answer Agent failed or returned error: {answer_text}")
            else:
                final_result = answer_text # Use the agent's response

            print("Answer agent finished.")
            # Add conversation turn to history
            if len(chat_history) >= MAX_CHAT_HISTORY:
                chat_history.pop(0) # Remove oldest turn
            chat_history.append({'user': user_prompt_text, 'AI': final_result})

        # --- Handle Mismatched Intent and Mode ---
        elif intent_mode in ['create', 'modify'] and i_mode not in ['create', 'modify']:
            # This happens if intent is create/modify but frontend didn't provide mode (e.g., no selection)
             print(f"Intent '{intent_mode}' determined, but frontend 'mode' was '{i_mode}'. Returning clarification.")
             return jsonify({
                 "success": False,
                 "error": f"I detected a '{intent_mode}' request, but I need a Figma selection to proceed. Please select a frame or element and try again.",
                 "mode": "clarification" # Indicate to frontend that clarification is needed
                 }), 200
        elif intent_mode == 'answer' and i_mode in ['create', 'modify']:
             # This happens if intent is answer but frontend provided mode (e.g., user selected but asked a general question)
             print(f"Intent '{intent_mode}' determined despite frontend 'mode' being '{i_mode}'. Proceeding with 'answer'.")
             # The answer agent flow is already handled above, just ensure the response is correct.
             # If we reached here, it means the intent was 'answer' and the answer flow executed successfully.
             pass # Logic is handled above

        else:
            # This case should ideally not be reached if the logic covers all possibilities
            print(f"Unhandled intent '{intent_mode}' with mode '{i_mode}'.")
            return jsonify({"success": False, "error": "Internal error: Unhandled intent."}), 500


    # --- Handle Execution Errors ---
    except ValueError as ve: # Catch specific validation/logic errors raised intentionally
        error_message = str(ve)
        print(f"Error during '{agent_used_name}' execution: {error_message}")
        # Return 200 OK but with success: False for UI to display the error gracefully
        return jsonify({"success": False, "error": error_message}), 200
    except Exception as e:
        # Catch broader unexpected exceptions during agent runs or processing
        import traceback
        print(f"An unexpected error occurred during '{agent_used_name}' execution: {e}")
        traceback.print_exc() # Print traceback for debugging server-side
        # Return 500 for unexpected internal server errors
        return jsonify({"success": False, "error": "An internal server error occurred."}), 500


    # --- Format and Return Success Response ---
    if final_result is None:
         # Should ideally be caught by errors above, but as a safeguard
         print(f"Execution completed for '{agent_used_name}' but final_result is unexpectedly None.")
         return jsonify({"success": False, "error": "Agent processing failed to produce a result."}), 500

    if final_type == "svg":
        print(f"Returning successful SVG response from '{agent_used_name}'.")
        return jsonify({"success": True, "svg": final_result})
    elif final_type == "answer":
        print(f"Returning successful Answer response from '{agent_used_name}'.")
        return jsonify({"success": True, "answer": final_result, "mode": "answer"})
    else:
        # Should not happen if logic is correct
        print(f"Error: Unknown final_type '{final_type}' after processing '{agent_used_name}'.")
        return jsonify({"success": False, "error": "Internal error: Unknown result type."}), 500

# --- Run the App ---
if __name__ == '__main__':
    # You can add more checks or print statements here before running
    print(f"Running Flask app with AGENT_MODEL='{config.AGENT_MODEL}' on http://0.0.0.0:5001")
    # Use asyncio.run to run the Flask app if using async routes
    # Note: For production deployment, a production-ready ASGI server like uvicorn or hypercorn is recommended.
    # Using debug=True should be avoided in production.
    # app.run(host='0.0.0.0', port=5001, debug=True) # Flask's built-in server doesn't handle async well
    import hypercorn.asyncio
    from hypercorn.config import Config

    async def serve_app():
        config = Config()
        config.bind = ["0.0.0.0:5001"]
        # config.worker_class = "asyncio" # Explicitly set worker class if needed
        # config.workers = 1 # Or more for concurrency
        await hypercorn.asyncio.serve(app, config)

    # This block handles running the async Flask app with hypercorn
    try:
        asyncio.run(serve_app())
    except KeyboardInterrupt:
        print("\nServer stopped.")