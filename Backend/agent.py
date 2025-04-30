from google.adk.agents import Agent
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.genai import types as genai_types
import google.generativeai as genai
from google.adk.tools import google_search
import uuid

MODEL_NAME = "gemini-1.5-flash-latest"
APP_NAME = "figma_ai_assistant"
session_service = InMemorySessionService()

import os
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
# os.environ["GOOGLE_CLOUD_PROJECT"] = "designo-2bf10"
# os.environ["GOOGLE_CLOUD_LOCATION"] = "us-central1"

create_agent_template = Agent(
    name="svg_creator_agent_v1",
    model=MODEL_NAME,
    description="Generates SVG code for UI designs.",
    instruction="Output valid SVG code for UI elements with width='390' for mobile or '1440' for desktop, using modern aesthetics."
)

modify_agent_template = Agent(
    name="svg_modifier_agent_v1",
    model=MODEL_NAME,
    description="Modifies specific UI elements as SVG.",
    instruction="Output valid SVG code for the modified element, matching original dimensions and context."
)

answer_agent_template = Agent(
    name="answer_agent_v1",
    model=MODEL_NAME,
    description="Answers design-related questions with web search.",
    instruction="Answer design questions conversationally, using web search for up-to-date info.",
    tools=[google_search]
)

async def run_adk_interaction(agent_template, credentials, content, user_id):
    genai.configure(credentials=credentials)
    session_id = f"session_{uuid.uuid4()}"
    session = session_service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)

    agent = Agent(
        name=agent_template.name,
        model=agent_template.model,
        description=agent_template.description,
        instruction=agent_template.instruction,
        tools=agent_template.tools
    )
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)

    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=content):
        if event.is_final_response() and event.content and event.content.parts:
            return event.content.parts[0].text
        if event.actions and event.actions.escalate:
            return f"AGENT_ERROR: {event.error_message or 'Escalation occurred.'}"

    session_service.delete_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
    return None