import secrets
import time
from datetime import datetime, timedelta, timezone
from google.oauth2.credentials import Credentials
from google.oauth2 import id_token
from google.auth.transport.requests import Request
import logging

CUSTOM_TOKEN_EXPIRY = 3600 * 24
temp_token_storage = {}
chat_history_storage = {}

async def initiate_oauth(google_oauth, redirect_uri):
    return google_oauth.authorize_redirect(redirect_uri)

async def handle_oauth_callback(google_oauth):
    token_data = google_oauth.authorize_access_token()
    creds = Credentials(
        token=token_data.get('access_token'),
        refresh_token=token_data.get('refresh_token'),
        token_uri=google_oauth.server_metadata.get('token_endpoint'),
        client_id=google_oauth.client_id,
        client_secret=google_oauth.client_secret,
        scopes=['https://www.googleapis.com/auth/generative-language.retriever', 'openid', 'profile'],
        expiry=datetime.fromtimestamp(token_data.get('expires_at'), tz=timezone.utc) if token_data.get('expires_at') else None
    )
    id_info = id_token.verify_oauth2_token(token_data.get('id_token'), Request(), google_oauth.client_id)
    user_info = {'id': id_info['sub'], 'email': id_info.get('email'), 'name': id_info.get('name')}

    custom_token = secrets.token_urlsafe(32)
    temp_token_storage[custom_token] = {
        'google_creds': {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': creds.token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': creds.scopes,
            'expiry': creds.expiry.isoformat() if creds.expiry else None
        },
        'user_info': user_info,
        'expiry': time.time() + CUSTOM_TOKEN_EXPIRY
    }
    return custom_token

async def verify_token(token):
    creds, user_info, error = await get_credentials_from_token(token)
    return user_info, error

async def get_credentials_from_token(token):
    if not token or token not in temp_token_storage:
        return None, None, "Invalid or expired token."
    data = temp_token_storage[token]
    if time.time() > data.get('expiry', 0):
        temp_token_storage.pop(token, None)
        return None, None, "Token expired."

    creds_dict = data['google_creds']
    user_info = data['user_info']
    creds = Credentials(
        token=creds_dict.get('token'),
        refresh_token=creds_dict.get('refresh_token'),
        token_uri=creds_dict.get('token_uri'),
        client_id=creds_dict.get('client_id'),
        client_secret=creds_dict.get('client_secret'),
        scopes=creds_dict.get('scopes'),
        expiry=datetime.fromisoformat(creds_dict['expiry']) if creds_dict.get('expiry') else None
    )

    if creds.refresh_token:
        creds.refresh(Request())
        data['google_creds'] = {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': creds.token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': creds.scopes,
            'expiry': creds.expiry.isoformat() if creds.expiry else None
        }
    return creds, user_info, None

async def logout(token):
    if token:
        data = temp_token_storage.pop(token, None)
        if data:
            chat_history_storage.pop(data['user_info']['id'], None)

def update_chat_history(user_id, user_content, response):
    history = chat_history_storage.get(user_id, [])
    history.extend([user_content.to_dict(), {'role': 'model', 'parts': [{'text': response}]}])
    chat_history_storage[user_id] = history[-20:]  # Limit to 20 entries