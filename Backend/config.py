# config.py
import os
import warnings
import logging
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
# os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False" # Use GenAI API directly - This is commented out in original, uncomment if needed
# Check if the variable is already set in .env and prefer that, otherwise default
if os.getenv("GOOGLE_GENAI_USE_VERTEXAI") is None:
     os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
elif os.getenv("GOOGLE_GENAI_USE_VERTEXAI").lower() == "true":
    # If set to true in .env, ensure GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION are set
    if not os.getenv("GOOGLE_CLOUD_PROJECT") or not os.getenv("GOOGLE_CLOUD_LOCATION"):
        raise ValueError("GOOGLE_GENAI_USE_VERTEXAI=True requires GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION in .env file")
    # print(f"Using Vertex AI: Project={os.getenv('GOOGLE_CLOUD_PROJECT')}, Location={os.getenv('GOOGLE_CLOUD_LOCATION')}")


print(f"Google API Key set: {'Yes' if GOOGLE_API_KEY else 'No'}")
print("ADK Environment Configured.")

# --- ADK Shared Configuration ---
APP_NAME = "figma_ai_assistant" # Consistent app name for sessions

# Use a model appropriate for your tasks (text and vision)
# Ensure this model supports vision capabilities for modify and refine agents
AGENT_MODEL = "gemini-2.5-flash-preview-04-17" # Example: Use a known vision-capable model
# Note: Update this model if a newer, better one is available and supports vision
# and tool use (for answer_agent). Check model compatibility in ADK docs.

# Export configuration variables
__all__ = [
    "GOOGLE_API_KEY",
    "APP_NAME",
    "AGENT_MODEL"
]