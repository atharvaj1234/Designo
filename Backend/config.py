# config.py
import os
import warnings
import logging
import json # To parse the client config JSON
from dotenv import load_dotenv

# --- Configuration ---
warnings.filterwarnings("ignore") # Suppress warnings if needed
logging.basicConfig(level=logging.ERROR) # Keep logging minimal

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
FIREBASE_SERVICE_ACCOUNT_KEY_PATH = os.getenv("FIREBASE_SERVICE_ACCOUNT_KEY_PATH")
FIREBASE_CLIENT_CONFIG_JSON = os.getenv("FIREBASE_CLIENT_CONFIG_JSON") # New env var for UI

# Essential API keys
if not GOOGLE_API_KEY:
    raise ValueError("Missing GOOGLE_API_KEY in .env file")
if not FIREBASE_SERVICE_ACCOUNT_KEY_PATH:
     raise ValueError("Missing FIREBASE_SERVICE_ACCOUNT_KEY_PATH in .env file")

# Firebase client config is essential for UI auth
if not FIREBASE_CLIENT_CONFIG_JSON:
     raise ValueError("Missing FIREBASE_CLIENT_CONFIG_JSON in .env file. UI authentication will fail.")

# Configure ADK environment variables
if os.getenv("GOOGLE_GENAI_USE_VERTEXAI") is None:
     os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
elif os.getenv("GOOGLE_GENAI_USE_VERTEXAI").lower() == "true":
    if not os.getenv("GOOGLE_CLOUD_PROJECT") or not os.getenv("GOOGLE_CLOUD_LOCATION"):
        raise ValueError("GOOGLE_GENAI_USE_VERTEXAI=True requires GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION in .env file")

print(f"Google API Key set: {'Yes' if GOOGLE_API_KEY else 'No'}")
print(f"Firebase Admin Key Path set: {'Yes' if FIREBASE_SERVICE_ACCOUNT_KEY_PATH else 'No'}")
print(f"Firebase Client Config JSON set: {'Yes' if FIREBASE_CLIENT_CONFIG_JSON else 'No'}")
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
    "FIREBASE_CLIENT_CONFIG", # Pass the parsed config
    "APP_NAME",
    "AGENT_MODEL"
]