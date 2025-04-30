from flask import Flask, redirect, url_for, session
from authlib.integrations.flask_client import OAuth
from google.oauth2.credentials import Credentials
import google.generativeai as genai
import os

app = Flask(__name__)
app.secret_key = 'your-secret-key'  # Replace with a secure key

# Replace with your actual client ID and secret from Google Cloud Console
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
os.unsetenv('GOOGLE_API_KEY')


# Initialize OAuth
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    api_base_url='https://www.googleapis.com/oauth2/v1/',
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'https://www.googleapis.com/auth/generative-language.retriever openid profile'}
)

@app.route('/')
def index():
    return '<a href="/login">Login with Google</a>'

@app.route('/login')
def login():
    # redirect_uri = url_for('authorize', _external=True)
    return google.authorize_redirect("https://nr8fxs4v-8080.inc1.devtunnels.ms/oauth2callback")

@app.route('/oauth2callback')
def authorize():
    token = google.authorize_access_token()
    session['token'] = token
    return redirect(url_for('chat'))

@app.route('/chat')
def chat():
    token = session.get('token')
    if not token:
        return redirect(url_for('login'))
    access_token = token['access_token']
    try:
        creds = Credentials(token=access_token)
        genai.configure(credentials=creds)
        model = genai.GenerativeModel('gemini-1.5-pro')
        response = model.generate_content("Hello, how can I assist you?")
        return response.text
    except Exception as e:
        return f"Error: {str(e)}"

if __name__ == '__main__':
    app.run(debug=True, port=8080)