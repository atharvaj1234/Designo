# api_handler.py
import asyncio
import os
import uuid
import logging
from random import shuffle
from dotenv import load_dotenv
import datetime # Import datetime
import pytz # For timezone-aware datetimes

from google.genai import types as google_genai_types
from google.adk.agents import Agent

import agents
import adk_utils

load_dotenv()

# --- Configuration for the Project Pool ---
num_projects_str = os.getenv('NUM_PROJECTS')
DEFAULT_NUM_PROJECTS = 6 # As you mentioned you have 6 now
CLIENT_IMPOSED_SESSIONS_PER_KEY_PER_MINUTE = 3 # Client's new requirement

if num_projects_str is None:
    logging.warning(f"NUM_PROJECTS environment variable not set. Defaulting to {DEFAULT_NUM_PROJECTS}.")
    NUM_PROJECTS = DEFAULT_NUM_PROJECTS
else:
    try:
        NUM_PROJECTS = int(num_projects_str)
        if NUM_PROJECTS <= 0:
            logging.warning(f"NUM_PROJECTS in .env ('{num_projects_str}') is not positive. Defaulting to {DEFAULT_NUM_PROJECTS}.")
            NUM_PROJECTS = DEFAULT_NUM_PROJECTS
    except ValueError:
        logging.warning(f"NUM_PROJECTS environment variable ('{num_projects_str}') is not a valid integer. Defaulting to {DEFAULT_NUM_PROJECTS}.")
        NUM_PROJECTS = DEFAULT_NUM_PROJECTS

MAX_CONCURRENT_REQUESTS_PER_KEY = 3 # Simultaneous active users per key
API_KEYS = []

for i in range(NUM_PROJECTS):
    key = os.getenv(f"GOOGLE_API_KEY_{i}")
    if not key:
        logging.warning(f"Missing GOOGLE_API_KEY_{i} in .env file. This key will not be part of the pool.")
    else:
        API_KEYS.append(key)

if not API_KEYS:
    logging.error("FATAL: No GOOGLE_API_KEY_i found for the api_handler pool. System will not function for pooled keys.")
    # Consider raising an exception or exiting if no keys are loaded for the pool.

logging.info(f"api_handler: Loaded {len(API_KEYS)} API Keys. Target NUM_PROJECTS: {NUM_PROJECTS}.")
logging.info(f"api_handler: Max concurrent users per key: {MAX_CONCURRENT_REQUESTS_PER_KEY}.")
logging.info(f"api_handler: Max new user sessions initiating per key per minute: {CLIENT_IMPOSED_SESSIONS_PER_KEY_PER_MINUTE}.")


PROJECT_POOL = []
if API_KEYS:
    PROJECT_POOL = [
        {
            "api_key": API_KEYS[i],
            "id": f"pooled_project_{i+1}", # 1-based id
            "semaphore": asyncio.Semaphore(MAX_CONCURRENT_REQUESTS_PER_KEY),
            "session_start_timestamps": [], # Stores datetime objects of when sessions started
            # Storing the limit here for clarity, could be a global constant too
            "rate_limit_new_sessions_per_minute": CLIENT_IMPOSED_SESSIONS_PER_KEY_PER_MINUTE
        }
        for i in range(len(API_KEYS))
    ]
    shuffle(PROJECT_POOL) # Shuffle for initial load distribution

available_projects_queue = asyncio.Queue()

async def initialize_project_pool():
    if not PROJECT_POOL:
        logging.warning("api_handler: Project pool is empty (no API keys loaded). Pooled keys unavailable.")
        return

    for project_token in PROJECT_POOL:
        await available_projects_queue.put(project_token)
    logging.info(f"api_handler: Project pool initialized. {available_projects_queue.qsize()} project tokens available in queue.")

    current_vertex_setting = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "True").lower()
    if current_vertex_setting != "false":
        logging.info(f"api_handler: Setting GOOGLE_GENAI_USE_VERTEXAI to 'False'. Was: '{os.getenv('GOOGLE_GENAI_USE_VERTEXAI')}'")
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
    else:
        logging.info(f"api_handler: GOOGLE_GENAI_USE_VERTEXAI is already 'False'.")


async def acquire_project():
    """
    Acquires an available project from the pool that meets BOTH concurrency
    and the new session rate limit. Waits if no such project is available.
    """
    if not PROJECT_POOL:
        logging.error("api_handler: Project pool is empty. Cannot acquire project.")
        # This indicates a setup issue if initialize_project_pool didn't populate it.
        raise Exception("api_handler: Project pool is not configured or empty.")

    attempt_cycle = 0
    while True: # Loop until a suitable project is acquired
        # logging.debug(f"acquire_project: Attempt cycle {attempt_cycle}. Queue size: {available_projects_queue.qsize()}")
        if available_projects_queue.empty():
            logging.info("acquire_project: Queue is empty, waiting for a project token...")
            # This await will block until a project is put back by release_project
            # Potentially add a timeout here if you want to give up after a certain wait.

        project_token = await available_projects_queue.get()
        project_id_log = project_token['id'] # For logging before full acquisition

        # Ensure we use timezone-aware UTC for comparisons
        now_utc = datetime.datetime.now(pytz.utc)

        # 1. Prune old timestamps (older than 60 seconds) from this specific project_token
        project_token["session_start_timestamps"] = [
            ts for ts in project_token["session_start_timestamps"]
            if (now_utc - ts).total_seconds() < 60
        ]
        current_sessions_in_rate_window = len(project_token["session_start_timestamps"])

        # 2. Check the new session rate limit
        if current_sessions_in_rate_window < project_token["rate_limit_new_sessions_per_minute"]:
            # Rate limit check passed for initiating a *new* session.
            # Now, try to acquire the concurrency semaphore.
            # logging.debug(f"acquire_project: Project {project_id_log} passed rate limit ({current_sessions_in_rate_window}/{project_token['rate_limit_new_sessions_per_minute']}). Trying semaphore.")
            try:
                # Attempt to acquire the semaphore. This will block if the key
                # is already at its MAX_CONCURRENT_REQUESTS_PER_KEY limit.
                # Add a small timeout to prevent indefinite blocking on just one key's semaphore
                # if other keys might be available and not rate-limited.
                # However, the primary waiting should be on available_projects_queue.get()
                # For now, let's allow it to block on the semaphore as per original design.
                await project_token["semaphore"].acquire()

                # If we reach here, semaphore acquired! This key can handle another concurrent user.
                # Add current time to mark the start of this new session for rate-limiting.
                project_token["session_start_timestamps"].append(now_utc)
                logging.info(f"api_handler: Acquired project {project_token['id']}. Concurrency slot taken. New session started at {now_utc.isoformat()}. Sessions in last 60s for this key: {len(project_token['session_start_timestamps'])}.")
                return project_token # Successfully acquired!
            except Exception as e: # Should not happen with standard semaphore acquire unless cancelled
                logging.error(f"api_handler: Unexpected error acquiring semaphore for {project_id_log}: {e}", exc_info=True)
                # If semaphore acquisition fails unexpectedly, put token back and try another.
                await available_projects_queue.put(project_token)
                # Continue loop to try another project or wait on queue.
        else:
            # This project_token is currently rate-limited for *new sessions*.
            # logging.info(f"api_handler: Project {project_id_log} is rate-limited for new sessions ({current_sessions_in_rate_window}/{project_token['rate_limit_new_sessions_per_minute']} in last 60s). Returning to queue.")
            await available_projects_queue.put(project_token) # Put it back at the end of the queue.

        # If we've cycled through all available project tokens once and none were suitable,
        # it means all are either rate-limited or their semaphores are full (and we didn't wait on a specific one).
        # The `await available_projects_queue.get()` at the start of the loop will naturally cause a wait
        # if the queue becomes empty.
        # To prevent extremely fast spinning if all keys are rate-limited but semaphores are free:
        attempt_cycle +=1
        # Heuristic: if we check more than the number of projects without success, pause briefly.
        # This helps if all projects are temporarily rate-limited.
        if available_projects_queue.qsize() < len(PROJECT_POOL) and attempt_cycle > len(PROJECT_POOL) * 2 : # Avoid tight loop when many items are in queue but all fail checks
             logging.debug(f"api_handler: Cycled through projects; all appear busy or rate-limited. Brief pause. Attempt cycle: {attempt_cycle}")
             await asyncio.sleep(0.2) # Short pause to yield control and allow time to pass for rate limits
             attempt_cycle = 0 # Reset cycle count


async def release_project(project_token):
    """Releases a project's concurrency semaphore slot and returns its token to the pool."""
    if project_token:
        try:
            project_token["semaphore"].release()
            # The session_start_timestamps are managed at acquisition and by pruning.
            # No change needed here for timestamps upon release for this model.
            logging.info(f"api_handler: Project {project_token['id']} concurrency slot released.")
        except Exception as e:
            logging.error(f"api_handler: Error releasing semaphore for {project_token.get('id', 'UNKNOWN')}: {e}", exc_info=True)
        finally:
            # Always try to put the token back in the queue, even if semaphore release failed (though it shouldn't)
            await available_projects_queue.put(project_token)
            # logging.debug(f"api_handler: Project {project_token['id']} token returned to queue. Queue size: {available_projects_queue.qsize()}")
    else:
        logging.warning("api_handler: Attempted to release a null project_token.")


# process_request_with_pooled_key remains unchanged as app.py now handles acquire/release
# for the entire multi-step user request. If you were to use this function directly
# for single agent calls, it would still work, but each call would be a "new session"
# from the perspective of the rate limiter defined above.
# For your current app.py structure (where app.py calls acquire_project once),
# process_request_with_pooled_key is not directly used in that flow.

async def process_request_with_pooled_key_single_step( # Renamed for clarity if used elsewhere
    agent_to_run: Agent,
    user_content: google_genai_types.Content,
    user_id: str
):
    """
    Manages acquiring a key from the pool for a SINGLE agent step,
    respecting concurrency and new session rate limits, running the ADK interaction,
    and then releasing the key.
    Each call to this function is treated as a new "session" for rate limiting.
    """
    project_in_use = None
    request_log_id = str(uuid.uuid4())[:8]

    if not PROJECT_POOL:
        logging.error(f"api_handler [Req-{request_log_id}]: Cannot process. Project pool is empty.")
        return f"ADK_RUNTIME_ERROR: api_handler: Project pool is empty or not configured."

    try:
        # This acquire_project now embodies the new rate limiting logic too
        project_in_use = await acquire_project()
        pooled_api_key = project_in_use["api_key"]
        # print("here is the api key", pooled_api_key) # Your debug print
        project_id_log_tag = f"{project_in_use['id']}/Req-{request_log_id}"

        logging.info(f"api_handler [{project_id_log_tag}]: Using pooled key ...{pooled_api_key[-4:]} for agent '{agent_to_run.name}' for user '{user_id}'.")

        response = await adk_utils.run_adk_interaction(
            agent_to_run=agent_to_run,
            user_content=user_content,
            session_service_instance=adk_utils.session_service,
            user_id=user_id,
            api_key=pooled_api_key
        )
        logging.info(f"api_handler [{project_id_log_tag}]: Agent '{agent_to_run.name}' completed.")
        return response
    except Exception as e:
        project_id_for_error = project_in_use['id'] if project_in_use else 'N/A_NO_PROJECT_ACQUIRED'
        logging.error(f"api_handler [{project_id_for_error}/Req-{request_log_id}]: Error in process_request_with_pooled_key_single_step for '{agent_to_run.name}': {e}", exc_info=True)
        return f"ADK_RUNTIME_ERROR: Exception in api_handler processing request for '{agent_to_run.name}': {e}"
    finally:
        if project_in_use:
            # This release_project just releases the semaphore and returns token to queue
            await release_project(project_in_use)
            # logging.info(f"api_handler [{project_in_use['id']}/Req-{request_log_id}]: Released project {project_in_use['id']} after single step.")


__all__ = [
    "initialize_project_pool",
    "acquire_project", # Exporting for app.py
    "release_project", # Exporting for app.py
    # "process_request_with_pooled_key_single_step" # Export if needed elsewhere
]