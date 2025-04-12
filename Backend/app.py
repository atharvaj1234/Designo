import os
import base64
import google.generativeai as genai
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv()  # Load environment variables from .env file
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if not GOOGLE_API_KEY:
    raise ValueError("Missing GOOGLE_API_KEY in .env file")

genai.configure(api_key=GOOGLE_API_KEY)

# --- Flask App Setup ---
app = Flask(__name__)
CORS(app) # Enable Cross-Origin Resource Sharing for requests from Figma plugin UI

# --- AI Model Configuration ---
# Use a model appropriate for your tasks (text and vision)
# Consider 'gemini-1.5-flash-latest' or specific vision/text models if needed
TEXT_MODEL_NAME = "gemini-2.0-flash-exp"
VISION_MODEL_NAME = "gemini-2.0-flash-exp" # Can often handle both

generation_config = {
    "temperature": 1, # Adjust creativity/determinism
    "top_p": 0.95,
    "top_k": 64, # Generous limit, adjust as needed
    "response_mime_type": "text/plain", # Expecting text (SVG)
}
safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
]

text_model = genai.GenerativeModel(
    model_name=TEXT_MODEL_NAME,
    safety_settings=safety_settings,
    generation_config=generation_config,
)
vision_model = genai.GenerativeModel(
    model_name=VISION_MODEL_NAME,
    safety_settings=safety_settings,
    generation_config=generation_config,
)


# --- Helper Function to Validate SVG ---
def is_valid_svg(svg_string):
    """Basic check if the string looks like SVG."""
    if not svg_string or not isinstance(svg_string, str):
        return False
    # Trim whitespace and check start/end tags (case-insensitive)
    trimmed = svg_string.strip()
    return trimmed.lower().startswith("<svg") and trimmed.lower().endswith("</svg>")

# --- API Endpoint ---
@app.route('/generate', methods=['POST'])
def handle_generate():
    """Handles requests for both creating and modifying designs."""
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 400

    data = request.get_json()
    mode = data.get('mode')
    user_prompt = data.get('userPrompt')
    context = data.get('context', {}) # Optional context like frameName, elementInfo
    image_data_base64 = data.get('imageDataBase64') # Only for modify mode

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

    try:
        svg_result = None
        if mode == 'create':
            # --- Text AI Call (Create) ---
            frame_name = context.get("frameName", "the target frame")
            prompt = f"""
You are an expert UI Designer specializing in creating modern, clean, and visually appealing SVG designs directly from prompts, optimized for Figma import.

Task: Design a UI component or layout based on the user's request: "{user_prompt}".

Targeting frame: "{frame_name}" (This is context, do not include frame name in SVG).

Response Format:
*   Output ONLY the raw, valid SVG code (starting with <svg> and ending with </svg>).
*   NO introductory text, explanations, comments outside SVG tags, or markdown formatting (like ```svg).
*   Use descriptive group IDs (<g id="...">) for logical sections.
*   Use standard SVG features compatible with Figma. Avoid complex filters or scripts.
*   Use rounded corners (rx, ry).
*   Use placeholder shapes (#E0E0E0) for images.
*   Use simple circles or text emojis for icons.
*   Keep text concise. Use font-family="sans-serif".
*   Ensure reasonable spacing and no overlaps.
*   Define colors directly.
*   Set a viewBox and width/height on the root <svg> element.
            """
            print("Generating response using Text model...")
            response = text_model.generate_content(prompt)
            svg_result = response.text.strip()

        elif mode == 'modify':
            # --- Vision AI Call (Modify) ---
            element_info = context.get("elementInfo", {})
            frame_name = context.get("frameName", "the frame")
            element_name = element_info.get("name", "the element")
            element_type = element_info.get("type", "element")
            element_width = element_info.get("width", "N/A")
            element_height = element_info.get("height", "N/A")

            prompt = f"""
You are an expert Figma UI/UX designer modifying a specific element within a Figma frame based on user request and an image.

Context:
*   Frame Name: "{frame_name}"
*   Element to Modify:
    *   Name: "{element_name}"
    *   Type: "{element_type}"
    *   Current Approx. Width: {element_width:.0f}px
    *   Current Approx. Height: {element_height:.0f}px
*   User Request: "{user_prompt}"

Task: Analyze the provided image. Identify element "{element_name}". Recreate ONLY this element as SVG code, incorporating the user's changes ("{user_prompt}"). Maintain original dimensions ({element_width:.0f} x {element_height:.0f}px) closely unless resizing is explicitly requested.

Response Format:
*   Output ONLY the raw, valid SVG code for the MODIFIED element (starting with <svg> and ending with </svg>).
*   The SVG root element represents the complete modified "{element_name}".
*   NO extra text, explanations, or markdown.
*   Ensure Figma-compatible SVG. Use placeholders/emojis for images/icons.
*   Set appropriate viewBox, width, height on the root <svg> tag, ideally matching original dimensions.
            """
            # Decode Base64 image data
            try:
                image_bytes = base64.b64decode(image_data_base64)
                image_part = {"mime_type": "image/png", "data": image_bytes}
            except Exception as e:
                 print(f"Error decoding base64 image: {e}")
                 return jsonify({"success": False, "error": "Invalid image data received"}), 400

            print(f"Generating response using Vision model for element '{element_name}'...")
            response = vision_model.generate_content([prompt, image_part])
            svg_result = response.text.strip()

        # --- Process and Validate Response ---
        print("AI Response received.")
        # Additional cleanup (remove potential markdown backticks)
        svg_result = svg_result.replace("```svg", "").replace("```", "").strip()

        if not is_valid_svg(svg_result):
            print(f"Validation Failed: AI response is not valid SVG. Response:\n{svg_result[:200]}...")
            # Try to extract SVG if wrapped in text (simple case)
            start_tag = "<svg"
            end_tag = "</svg>"
            start_index = svg_result.lower().find(start_tag)
            end_index = svg_result.lower().rfind(end_tag)
            if start_index != -1 and end_index != -1 and start_index < end_index:
                extracted_svg = svg_result[start_index : end_index + len(end_tag)]
                if is_valid_svg(extracted_svg):
                    print("Successfully extracted SVG from wrapped response.")
                    svg_result = extracted_svg
                else:
                     # Fallback to error if extraction fails or is still invalid
                     error_msg = f"AI response was not valid SVG. Please try rephrasing. Response snippet: {svg_result[:150]}..."
                     return jsonify({"success": False, "error": error_msg}), 200 # Return 200 so UI can show error
            else:
                 error_msg = f"AI response was not valid SVG. Please try rephrasing. Response snippet: {svg_result[:150]}..."
                 return jsonify({"success": False, "error": error_msg}), 200 # Return 200 so UI can show error


        print("SVG Validation successful.")
        return jsonify({"success": True, "svg": svg_result})

    except Exception as e:
        # Handle potential API errors (rate limits, auth, etc.) or other exceptions
        error_message = f"An error occurred: {e}"
        print(f"Error during generation: {error_message}")
        # Check for specific Google API errors if possible
        if "API key not valid" in str(e):
            error_message = "Server configuration error: Invalid Google API Key."
            status_code = 500
        elif "quota" in str(e).lower():
             error_message = "API Quota Exceeded. Please check your Google Cloud project."
             status_code = 429 # Too Many Requests
        elif hasattr(e, 'response') and hasattr(e.response, 'prompt_feedback'): # Check for safety blocks
             try:
                 block_reason = e.response.prompt_feedback.block_reason
                 if block_reason:
                     error_message = f"AI response blocked due to safety settings ({block_reason}). Try a different prompt."
                     status_code = 200 # Return 200 so UI can show error message clearly
                 else:
                     status_code = 500 # Other API error
             except AttributeError:
                 status_code = 500
        else:
             status_code = 500 # Internal Server Error for other exceptions

        return jsonify({"success": False, "error": error_message}), status_code


# --- Run the App ---
if __name__ == '__main__':
    # Make sure to set the host and port appropriately
    # Use 0.0.0.0 to make it accessible on your network if needed
    # Default port is 5000, using 5001 to avoid potential conflicts
    app.run(host='0.0.0.0', port=5001, debug=True) # Turn debug=False in production