# config.py
import os
import warnings
import logging
import json
from dotenv import load_dotenv
from cryptography.fernet import Fernet # Import Fernet

# --- Configuration ---
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.ERROR)

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # Server's default API key
FIREBASE_SERVICE_ACCOUNT_KEY_PATH = os.getenv("FIREBASE_SERVICE_ACCOUNT_KEY_PATH")
FIREBASE_CLIENT_CONFIG_JSON = os.getenv("FIREBASE_CLIENT_CONFIG_JSON")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY") # New env var for the encryption key

# Essential API keys
if not GOOGLE_API_KEY:
    # Allow starting without server key if ENCRYPTION_KEY is set, assuming users bring their own
    if not ENCRYPTION_KEY:
         raise ValueError("Missing GOOGLE_API_KEY or ENCRYPTION_KEY in .env file. At least one is required.")
    else:
         print("WARNING: GOOGLE_API_KEY is not set. Only users providing their own key will be able to use the service.")


if not FIREBASE_SERVICE_ACCOUNT_KEY_PATH:
     raise ValueError("Missing FIREBASE_SERVICE_ACCOUNT_KEY_PATH in .env file")

if not FIREBASE_CLIENT_CONFIG_JSON:
     raise ValueError("Missing FIREBASE_CLIENT_CONFIG_JSON in .env file. UI authentication will fail.")

if not ENCRYPTION_KEY:
    print("WARNING: ENCRYPTION_KEY not set in .env. Generating one for development (DO NOT USE IN PRODUCTION WITHOUT SECURE STORAGE).")
    # Generate a key if missing for development convenience.
    # In production, generate once and store securely.
    ENCRYPTION_KEY = Fernet.generate_key().decode()
    print(f"Generated temporary ENCRYPTION_KEY: {ENCRYPTION_KEY[:8]}...")


# Configure ADK environment variables
if os.getenv("GOOGLE_GENAI_USE_VERTEXAI") is None:
     os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
elif os.getenv("GOOGLE_GENAI_USE_VERTEXAI").lower() == "true":
    if not os.getenv("GOOGLE_CLOUD_PROJECT") or not os.getenv("GOOGLE_CLOUD_LOCATION"):
        raise ValueError("GOOGLE_GENAI_USE_VERTEXAI=True requires GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION in .env file")


print(f"Google API Key set: {'Yes' if GOOGLE_API_KEY else 'No'} (Server Default)")
print(f"Firebase Admin Key Path set: {'Yes' if FIREBASE_SERVICE_ACCOUNT_KEY_PATH else 'No'}")
print(f"Firebase Client Config JSON set: {'Yes' if FIREBASE_CLIENT_CONFIG_JSON else 'No'}")
print(f"Encryption Key set: {'Yes' if ENCRYPTION_KEY else 'No'}")
print("ADK Environment Configured.")

# Parse client config JSON
try:
    FIREBASE_CLIENT_CONFIG = json.loads(FIREBASE_CLIENT_CONFIG_JSON)
    print("Firebase Client Config parsed.")
except json.JSONDecodeError as e:
    print(f"Error decoding FIREBASE_CLIENT_CONFIG_JSON: {e}")
    raise ValueError("Invalid JSON format for FIREBASE_CLIENT_CONFIG_JSON in .env file.") from e


# --- ADK Shared Configuration ---
APP_NAME = "figma_ai_assistant"
AGENT_MODEL = "gemini-2.5-flash-preview-04-17"


# Export configuration variables
__all__ = [
    "GOOGLE_API_KEY",
    "FIREBASE_SERVICE_ACCOUNT_KEY_PATH",
    "FIREBASE_CLIENT_CONFIG",
    "ENCRYPTION_KEY", # Export the encryption key
    "APP_NAME",
    "AGENT_MODEL"
]