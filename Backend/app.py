from flask import Flask, request, jsonify, redirect, url_for, render_template_string
from flask_cors import CORS
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
import os, base64
from auth import initiate_oauth, handle_oauth_callback, verify_token, get_credentials_from_token, logout, update_chat_history
from agent import create_agent_template, modify_agent_template, answer_agent_template, run_adk_interaction
from utils import is_valid_svg

load_dotenv()

# Flask App Setup
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
CORS(app, origins=["*"], supports_credentials=True)

# OAuth Setup
oauth = OAuth(app)
oauth.register(
    name='google',
    client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'https://www.googleapis.com/auth/generative-language.retriever openid profile', 'access_type': 'offline', 'prompt': 'consent'}
)
google_oauth = oauth.create_client('google')

# Routes
@app.route('/authorize')
def authorize():
    redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
    return google_oauth.authorize_redirect(redirect_uri)

@app.route('/oauth2callback')
async def oauth2callback():
    token = await handle_oauth_callback(google_oauth)
    return redirect(url_for('show_token', token=token))

@app.route('/auth/token')
def show_token():
    token = request.args.get('token')
    if not token:
        return "Error: No token provided.", 400
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Authentication Token</title></head>
    <body style="font-family: sans-serif; text-align: center;">
      <h2>Authentication Token</h2>
      <p>Copy this token into the Figma plugin:</p>
      <textarea id="tokenArea" readonly>{{ token }}</textarea>
      <button onclick="copyToken()">Copy Token</button>
      <script>
        function copyToken() {
          const el = document.getElementById('tokenArea');
          el.select();
          navigator.clipboard.writeText(el.value).then(() => alert('Token copied!'));
        }
      </script>
    </body>
    </html>
    """
    return render_template_string(html, token=token)

@app.route('/api/auth/verify_token', methods=['POST'])
async def verify_token_endpoint():
    data = request.get_json()
    token = data.get('token')
    if not token:
        return jsonify({"success": False, "error": "No token provided."}), 400
    user_info, error = await verify_token(token)
    if error:
        return jsonify({"success": False, "error": error}), 401
    return jsonify({"success": True, "userInfo": user_info, "authToken": token})

@app.route('/api/auth/status')
async def auth_status():
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({"isLoggedIn": False})
    token = auth_header.split('Bearer ')[1]
    user_info, error = await verify_token(token)
    return jsonify({"isLoggedIn": bool(not error), "userInfo": user_info or {}})

@app.route('/logout', methods=['POST'])
async def logout_endpoint():
    auth_header = request.headers.get('Authorization')
    token = auth_header.split('Bearer ')[1] if auth_header and auth_header.startswith('Bearer ') else None
    await logout(token)
    return jsonify({"success": True, "message": "Logged out successfully."})

@app.route('/chat', methods=['POST'])
async def handle_chat():
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({"success": False, "error": "Authentication required."}), 401

    token = auth_header.split('Bearer ')[1]
    credentials, user_info, error = await get_credentials_from_token(token)
    if error:
        return jsonify({"success": False, "error": error}), 401

    data = request.get_json()
    prompt = data.get('userPrompt')
    mode = data.get('mode')
    context = data.get('context', {})
    frame_data = data.get('frameDataBase64')
    element_data = data.get('elementDataBase64')

    if not prompt or not mode:
        return jsonify({"success": False, "error": "Missing 'userPrompt' or 'mode'."}), 400
    if mode not in ['create', 'modify', 'answer']:
        return jsonify({"success": False, "error": f"Invalid mode: {mode}."}), 400

    agent = {
        'create': create_agent_template,
        'modify': modify_agent_template,
        'answer': answer_agent_template
    }[mode]

    content = prepare_agent_input(mode, prompt, context, frame_data, element_data)
    if isinstance(content, tuple):  # Error case
        return jsonify({"success": False, "error": content[1]}), 400

    result = await run_adk_interaction(agent, credentials, content, user_info['id'])
    if not result or result.startswith("AGENT_ERROR:"):
        return jsonify({"success": False, "error": result or "Agent failed."}), 200

    update_chat_history(user_info['id'], content, result)

    if mode in ['create', 'modify']:
        if not is_valid_svg(result):
            return jsonify({"success": False, "error": "Invalid SVG output."}), 200
        cleaned_svg = result.strip('```').strip()
        return jsonify({"success": True, "svg": cleaned_svg, "mode": mode})
    return jsonify({"success": True, "answer": result, "mode": "answer"})

def prepare_agent_input(mode, prompt, context, frame_data, element_data):
    from google.genai import types as genai_types
    if mode == 'create':
        text = f"Design for frame '{context.get('frameName', '')}'.\n{prompt}"
        return genai_types.Content(role='user', parts=[genai_types.Part(text=text)])
    elif mode == 'modify':
        if not all([frame_data, element_data, context.get('element')]):
            return (None, "Missing image data or element context.")
        element = context['element']
        text = f"Modify: {prompt}\nFrame: {context.get('frameName', 'N/A')}\nElement: {element.get('name', 'N/A')}, {element.get('type', 'N/A')}"
        parts = [genai_types.Part(text=text)]
        for data in [frame_data, element_data]:
            parts.append(genai_types.Part(inline_data=genai_types.Blob(mime_type="image/png", data=base64.b64decode(data))))
        return genai_types.Content(role='user', parts=parts)
    return genai_types.Content(role='user', parts=[genai_types.Part(text=prompt)])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)