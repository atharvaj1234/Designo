# Designo - AI Design Assistant

## Have a look at this video demonstration
<video width="640" height="360" controls>
  <source src="./Video and PPT/video.mp4" type="video/mp4">
  Your browser does not support the video tag.
</video>

## Description

Designo is a Figma plugin that acts as an AI-powered design assistant. It leverages Google's Agent Development Kit (ADK) and the Gemini family of models via a Python Flask backend to help users generate new UI elements, modify existing ones based on context and prompts, and answer general design-related questions directly within the Figma environment.

The system intelligently routes user requests to specialized AI agents:
*   One agent determines user intent (create, modify, or answer).
*   One agent generates SVG code for new designs from text prompts.
*   One agent modifies existing design elements using text prompts *and* visual context from the Figma frame.
*   One agent answers general questions, using Google Search via ADK tools when needed.

## Features

*   **Context-Aware Operations:** Understands whether you've selected an empty frame (for creation) or an existing element (for modification).
*   **AI-Powered SVG Generation:** Creates new UI elements as SVG based on natural language descriptions.
*   **Visual Modification:** Modifies selected elements by analyzing the current design (via image export) and following text instructions.
*   **Question Answering:** Responds to general queries, utilizing web search for up-to-date information if necessary.
*   **Specialized Agents:** Uses distinct Google ADK agents optimized for specific tasks (intent routing, SVG creation, SVG modification, Q&A).
*   **Chat Interface:** Provides a simple chat UI within Figma for interaction.
*   **Backend Processing:** Offloads heavy AI processing to a separate Flask backend, keeping the plugin lightweight.

## Architecture Overview

1.  **Figma Plugin (Frontend):**
    *   `ui.html`: Provides the chat interface within the Figma plugin panel. Communicates with `code.js` and the backend API.
    *   `code.js`: The main plugin logic running in Figma's sandbox. It handles selection changes, communication with `ui.html`, exporting frame data (for modification context), and manipulating Figma nodes (inserting/replacing SVG).
    *   `manifest.json`: Defines the plugin's metadata and capabilities for Figma.

2.  **Flask Backend (Python):**
    *   `app.py`: A Flask web server that exposes an API endpoint (`/generate`).
    *   **Google ADK:** Manages interactions with Google's AI models (Gemini).
    *   **Agents:** Defines specialized ADK Agents (`decision_agent`, `create_agent`, `modify_agent`, `answer_agent`) with specific instructions and tools (like Google Search for the `answer_agent`).
    *   **Logic:** Receives requests from the plugin UI, determines intent, selects the appropriate agent, executes the AI task, validates the response (e.g., checking for valid SVG), and sends the result back to the plugin UI.

**Flow:**
`Figma Selection` -> `code.js` -> `ui.html` (Display State) -> `User Input` -> `ui.html` -> `code.js` (Prepare Context/Export Image if needed) -> `ui.html` (Format Request) -> `Flask Backend /generate API` -> `app.py` (ADK Agents Process) -> `Flask Backend Response (SVG/Text)` -> `ui.html` (Process Response) -> `code.js` (Manipulate Figma Document) -> `Figma Canvas Update`.

## Prerequisites

*   **Node.js and npm:** To install plugin dependencies. (Download: [https://nodejs.org/](https://nodejs.org/))
*   **Python:** Version 3.8 or higher recommended. (Download: [https://www.python.org/](https://www.python.org/))
*   **pip:** Python package installer (usually comes with Python).
*   **Figma Desktop App:** The plugin runs within the desktop application. (Download: [https://www.figma.com/downloads/](https://www.figma.com/downloads/))
*   **Google API Key:** An API key for Google Generative AI (Gemini). You can get one from [Google AI Studio](https://aistudio.google.com/app/apikey).

## Setup

1.  **Clone the Repository:**
    ```bash
    git clone https://github.com/atharvaj1234/Designo
    cd Designo
    ```

2.  **Backend Setup:**
    *   Navigate to the backend directory:
        ```bash
        cd Backend
        ```
    *   Create a virtual environment (recommended):
        ```bash
        python -m venv venv
        # Activate it:
        # Windows: venv\Scripts\activate
        # macOS/Linux: source venv/bin/activate
        ```
    *   Create a `.env` file in the `Backend` directory and add your Google API Key:
        ```env
        # Backend/.env
        GOOGLE_API_KEY="YOUR_GOOGLE_API_KEY_HERE"
        ```
        Replace `"YOUR_GOOGLE_API_KEY_HERE"` with your actual key.
    *   Install Python dependencies:
        ```bash
        pip install Flask Flask-Cors google-adk
        ```
3.  **Plugin Setup:**
    *   Navigate to the plugin directory:
        ```bash
        # From the project root
        cd ../Plugin
        # Or if you are still in Backend/
        # cd ../Plugin
        ```
    *   Install Node.js dependencies:
        ```bash
        npm install
        ```
    *   **(Potential Build Step)** Your `manifest.json` specifies `"main": "dist/code.js"`. The provided code file is `Plugin/code.js`. Ensure the `code.js` content is placed inside a `dist` folder within the `Plugin` directory (`Plugin/dist/code.js`). If you were using TypeScript (`code.ts`), you would typically run `npm run build` (defined in `package.json`) which uses `tsc` (from `tsconfig.json`) to compile and place the output in `dist/`. For now, manually ensure `Plugin/dist/code.js` exists with the correct code.

## Running the Application

1.  **Start the Backend Server:**
    *   Open a terminal in the `Backend` directory.
    *   Make sure your virtual environment is activated.
    *   Run the Flask app:
        ```bash
        python app.py
        ```
    *   The server should start, typically listening on `http://127.0.0.1:5001` or `http://0.0.0.0:5001`. Keep this terminal running.

2.  **Load the Plugin in Figma:**
    *   Open the Figma Desktop App.
    *   Go to the main menu (Figma icon) -> Plugins -> Development -> Import plugin from manifest...
    *   Navigate to your project folder and select the `manifest.json` file located inside the `Plugin` directory.
    *   The "Designo" plugin should now appear in your Plugins list (potentially under the "Development" submenu).

## How to Use

1.  **Open the Plugin:** Open a Figma file, then run the "Designo" plugin from the Plugins menu.
2.  **Select a Target:**
    *   **To Create:** Select a single, *empty* top-level Frame on your canvas. The plugin UI should indicate "Ready to generate".
    *   **To Modify:** Select a single element *inside* a top-level Frame. The plugin UI should indicate "Ready to modify".
    *   **To Answer:** Have nothing selected, or an invalid selection (multiple items, item not in a frame). The plugin defaults to allowing general questions.
3.  **Enter Your Prompt:** Type your request into the text area:
    *   *Create:* "Create a modern login form with fields for email, password, and a submit button."
    *   *Modify:* "Change the button color to blue." or "Make the title text larger."
    *   *Answer:* "What are the latest UI design trends for dashboards?"
4.  **Send:** Click the "Send" button.
5.  **Wait for Response:** The plugin will communicate with the backend. You'll see status updates.
    *   If creating/modifying, the backend generates SVG, which the plugin then uses to add or replace the element on your canvas.
    *   If answering, the text response will appear in the chat history.
6.  **View Results:** Check your Figma canvas for the new or modified element, or read the answer in the plugin panel.

## Notes & Potential Improvements

*   The `modify_agent` relies on recreating the element as SVG based on visual context and the prompt. Complex elements might not be recreated perfectly.
*   Error handling can be further improved for edge cases in Figma or AI responses.
*   SVG validation in the backend is basic; more robust XML parsing could be added.
*   Performance depends on the complexity of the request and the AI model's response time.
*   Ensure the `BACKEND_URL` constant in `Plugin/ui.html` (`http://localhost:5001/generate`) correctly points to where your Flask backend is running.
*   Consider adding a proper build process for the plugin frontend if using TypeScript or wanting bundling/minification.
