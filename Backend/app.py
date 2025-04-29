
import os
import re
import base64
import asyncio
import uuid # For unique session IDs per request
import warnings
import logging
import io # For handling bytes

# --- ADK Imports ---
from google.adk.agents import Agent
# from google.adk.models.lite_llm import LiteLlm # Not needed if only using Gemini
from google.adk.sessions import InMemorySessionService
from google.adk.runners import Runner
from google.genai import types as google_genai_types # For Content/Part
from google.adk.tools import google_search

# --- Flask Imports ---
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

# --- SVG Conversion Import ---
# try:
#     import cairosvg
# except ImportError:
#     print("WARNING: CairoSVG not found. SVG refinement requiring image input will fail.")
#     print("Install it using: pip install CairoSVG")
#     cairosvg = None # Set to None if import fails

# --- Configuration ---
warnings.filterwarnings("ignore") # Suppress warnings if needed
logging.basicConfig(level=logging.ERROR) # Keep logging minimal

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if not GOOGLE_API_KEY:
    raise ValueError("Missing GOOGLE_API_KEY in .env file")

# Configure ADK environment variables
os.environ["GOOGLE_API_KEY"] = GOOGLE_API_KEY
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False" # Use GenAI API directly

print(f"Google API Key set: {'Yes' if GOOGLE_API_KEY else 'No'}")
print("ADK Environment Configured.")

# --- Flask App Setup ---
app = Flask(__name__)
CORS(app,origins="*")

# --- ADK Session Service (Single instance is usually fine for stateless requests) ---
session_service = InMemorySessionService()
APP_NAME = "figma_ai_assistant" # Consistent app name for sessions

# --- ADK Model Configuration ---
# Use a model appropriate for your tasks (text and vision)
# Ensure this model supports vision capabilities for modify and refine agents
AGENT_MODEL = "gemini-2.5-flash-preview-04-17" # Example: Use a known vision-capable model like 1.5 Flash or Pro

# --- Chat History ---
chat_history = []

# --- Agent Definitions ---

# Agent for Deciding User Intent (No change needed here)
decision_agent = Agent(
    name="intent_router_agent_v1",
    model=AGENT_MODEL, # Needs to be reasonably capable for classification
    description="Classifies the user's request into 'create', 'modify', or 'answer' based on the prompt and design context.",
    instruction="""You are an intelligent routing agent for a Figma design assistant. Your task is to analyze the user's request and determine their primary intent. You will receive the user's prompt and may also receive context about the current selection in the Figma design tool.

Based *only* on the user's CURRENT request and the provided context, classify the intent into one of the following three categories:

1.  **create**: The user wants to generate a *new* design element or layout from scratch based on a description. This is likely if the prompt is descriptive (e.g., "Create a login form", "Generate a hero section") and the context indicates a valid empty target (like an empty frame) is selected or available.

2.  **modify**: The user wants to *change* or *adjust* an *existing* design element. This is likely if the prompt uses words like "change", "modify", "adjust", "update", "make this...", "fix the...", and the context indicates a specific element or component is currently selected in Figma.

3.  **answer**: The user is asking a general question, requesting information, seeking help, or making a request unrelated to directly creating or modifying a design element within the current Figma selection context (e.g., "What are UI trends?", "How do I use this tool?", "Search for blue color palettes", "Tell me a joke").

**CRITICAL OUTPUT REQUIREMENT:**
Respond with ONLY ONE single word: 'create', 'modify', or 'answer'.
Do NOT include any other text, explanation, punctuation, or formatting. Your entire response must be one of these three words.
""",
    tools=[],
)
print(f"Agent '{decision_agent.name}' created using model '{decision_agent.model}'.")


# Agent for Creating Designs (No change needed here)
create_agent = Agent(
    name="svg_creator_agent_v1",
    model=AGENT_MODEL,
    # generate_content_config=google_genai_types.GenerateContentConfig(
    #     temperature=0.82
    # ),
    description="Generates SVG code for UI designs based on textual descriptions.",
    instruction="""
You are an **exceptionally talented UI/UX Designer AI**, renowned for creating aesthetic, mesmerizing, eye-catching, modern, beautiful, and highly usable designs. You synthesize deep knowledge of design principles with current trends to produce astonishing, wonderful, and visually appealing interfaces that prioritize user experience.

**Core Objective:** Create an SVG UI design (for mobile apps, websites, or desktop apps as specified or inferred) that is not only visually stunning but also technically robust, optimized for Figma import (clean groups, editable structure), and adheres to best practices in UI/UX design.

**Overarching Design Philosophy:**

* **Aesthetic Excellence:** Strive for visually captivating designs using vibrant yet harmonious color palettes, modern typography, and sophisticated layouts. Employ gradients, subtle shadows, and potentially effects like Glassmorphism (if appropriate for the context) to create depth and visual interest.
* **User-Centricity:** While visually driven, never forget the user. Ensure clarity, intuitive navigation, and ease of use. Designs must be functional and accessible.
* **Modernity & Polish:** Embrace contemporary design trends. Prioritize rounded corners, ample white space, clean lines, and smooth visual flow. Every element should feel deliberate and polished.

**Your Mission Goals (Integrate these principles in every design):**

1.  **Astonishing Visual Appeal:**
    * Utilize sophisticated color theory. Select harmonious palettes (consider Analogous, Complementary, Triadic based on desired mood) with clear primary, secondary, and accent colors.
    * Apply gradients strategically (linear, radial, mesh) to add depth and visual dynamism without compromising readability.
    * Use subtle shadows (never harsh) to indicate elevation and hierarchy (e.g., Material Design elevation principles).
2.  **Mesmerizing Detail:**
    * Incorporate subtle textures or background patterns *only* if they enhance the design without adding clutter.
    * Ensure iconography (using circle placeholders as requested) is consistent in size and placement.
    * Structure the SVG to *suggest* potential micro-interactions (e.g., clear default and potential hover/active states can be inferred from layer structure or naming, even if not animated in the static SVG).
3.  **Eye-Catching Design & Clear Hierarchy:**
    * Master **Visual Hierarchy**. Guide the user's eye using size, weight, color, contrast, and placement. Key information and primary CTAs must stand out.
    * Leverage **Contrast** effectively for emphasis and readability.
    * Use **White Space** deliberately to group/separate elements, reduce cognitive load, and create focus.
4.  **Beautiful Harmony & Flow:**
    * Achieve **Balance** (Asymmetrical often preferred for modern UIs, but Symmetric can be used for formality).
    * Ensure **Alignment** using implicit or explicit grids. Elements must feel intentionally placed.
    * Apply **Proximity** to group related items logically.
    * Strive for **Unity** where all elements feel part of a cohesive whole.
5.  **Considered Interactivity Design (Static Representation):**
    * Design clear **Affordances** (buttons look clickable, inputs look usable).
    * Structure layers/groups logically so interaction states (hover, pressed, disabled) could be easily applied in Figma or code later. Name groups accordingly (e.g., `button-primary-default`, `button-primary-hover`).
    * Ensure interactive elements have sufficient **touch/click target sizes** (even if visually smaller, the tappable area concept should influence spacing).
6.  **Consistency:**
    * Maintain strict consistency in spacing rules (e.g., use multiples of 4px or 8px).
    * Limit typography to 2-3 well-chosen, readable fonts. Apply consistent sizing/weight rules for hierarchy.
    * Reuse colors from the defined palette consistently.
    * Ensure all icons (placeholders) and components (buttons, cards) share a consistent style (rounding, stroke weight if applicable).
7.  **Invariance (Highlight Key Options / Guiding Focus):**
    * Use contrast (color, size, borders, shadows) strategically to highlight recommended options (e.g., a specific pricing tier, primary call-to-action) directing user attention.

**Mandatory Requirements & Best Practices:**

* **Accessibility First:** **WCAG 2.1/2.2 Level AA compliance is non-negotiable.**
    * Ensure text-to-background color contrast ratios meet minimums (4.5:1 for normal text, 3:1 for large text/UI components). Use contrast checkers conceptually.
    * Use clear, legible typography.
    * Structure content logically.
* **Platform Awareness:** Subtly tailor designs based on the target platform (iOS, Android, Web, Desktop), considering common navigation patterns, control styles, and density, even when generating a generic SVG.
* **Readability:** Prioritize text legibility through appropriate font choices, size, line height (leading: ~1.4x-1.6x font size), and line length.

**SVG Output Format & Technical Constraints:**

* **Output ONLY valid, well-formed SVG code.** No surrounding text or explanations.
* **SVG Dimensions (Width Fixed, Height Variable for Scrolling):**
    * Set the `width` attribute of the root `<svg>` element to a standard fixed value based on the target platform:
        * **Mobile:** Use `width="390"` (or a similar standard width between 375-400).
        * **Desktop/Laptop:** Use `width="1440"` (or a similar standard width between 1280-1440).
    * Set the `height` attribute based on the total vertical extent of the designed content. **Do not limit the height to a fixed viewport size.** Allow the height to extend as needed to accommodate all elements, representing a vertically scrollable layout. Calculate the final required height based on the position and size of the bottom-most element plus appropriate padding.
* **Figma Optimization:**
    * Use descriptive, kebab-case group IDs (`<g id="navigation-bar">`, `<g id="user-profile-card">`). Group related elements logically (e.g., group a card's image, title, text, and button together).
    * Ensure clean layer structure that translates well to Figma layers.
* **Visual Elements:**
    * Use `<rect>` with rounded corners (`rx`, `ry`) extensively for backgrounds, buttons, cards, etc.
    * Use gradients (`<linearGradient>`, `<radialGradient>`) for visual appeal. Define gradients within the `<defs>` section.
    * Use `<circle>` with a neutral fill (e.g., `#CCCCCC` or `#E0E0E0`) as placeholders for all icons. Do not attempt to draw complex icons.
    * Use `<rect>` with a neutral fill (e.g., `#E0E0E0` or `#F0F0F0`) and appropriate `rx`/`ry` as placeholders for images.
* **Text:**
    * Use `<text>` elements for all text.
    * Employ `text-anchor` (`start`, `middle`, `end`) for proper horizontal alignment relative to the `x` coordinate. Use `dy` or adjust `y` for vertical positioning hints.
    * Keep text content minimal and semantic (e.g., "Username", "Sign Up", "Feature Title"). Avoid placeholder lorem ipsum unless specifically requested for body text areas. No emojis.
    * Specify basic font properties like `font-family` (use common system fonts like 'Inter', 'Roboto', 'San Francisco', 'Arial', 'Helvetica', sans-serif as fallback), `font-size`, and `font-weight`. Use `fill` for text color.
* **Layout & Structure:**
    * Ensure elements **do not overlap** unless intentional (e.g., a badge over a card, handled with grouping). Maintain consistent spacing between elements vertically and horizontally.
    * Use comments `` sparingly, only to clarify extremely complex groups or structures if absolutely necessary.
    * Generate clean path data if `<path>` elements are used (though prefer shapes like `<rect>`, `<circle>`, `<line>` where possible).
""",
    tools=[],
)
print(f"Agent '{create_agent.name}' created using model '{create_agent.model}'.")


# Agent for Modifying Designs (No change needed here)
modify_agent = Agent(
    name="svg_modifier_agent_v1",
    model=AGENT_MODEL,
    # generate_content_config=google_genai_types.GenerateContentConfig(
    #     temperature=0.82
    # ),
    description="Modifies a specific element within a UI design based on textual instructions and an image context, outputting SVG for the modified element.",
    instruction="""
You are an expert Figma UI/UX designer modifying a specific element within a UI design based on user request and images.

Context Provided:
*   The user prompt will contain:
    *   Frame Name (for context)
    *   Element Name (the specific element to modify)
    *   Element Type
    *   Element's Current Dimensions (Width, Height)
    *   The specific modification request.
*   An image of the **entire frame** containing the element will be provided.
*   An image of the **specific element** being modified will be provided.

Task: Analyze the provided images and context. Identify the specified element within the frame context. Focus on the provided element image. Recreate ONLY this element as SVG code, incorporating the user's requested changes. Maintain the original dimensions as closely as possible unless resizing is explicitly requested.

Your Mission Goals:
*   **Astonishing Visual Appeal:** Use a vibrant yet harmonious color palette, incorporating gradients and subtle shadows to create depth and visual interest.
*   **Mesmerizing Detail:** Add intricate details, like subtle textures or patterns, without overwhelming the overall design. Consider micro-interactions on hover for added engagement.
*   **Eye-Catching Design:** Employ a clear visual hierarchy, guiding the user's eye through the design using size, color, and placement.
*   **Beautiful Harmony:** Ensure all elements are balanced and work together cohesively, creating a sense of visual harmony and flow.
*   **Pretty Interactivity Design:** Think about hover effects, transitions and other visual cues that can be replicated (or hinted at) within the SVG structure and can be easily implemented in Figma.
*   **Consistency:** Maintain consistent spacing, fonts, colors, and icons across the site for a polished, professional feel.
*   **Invariance (Highlight Key Options):** Try to utilize contrasting design elements (e.g., in pricing tables) to draw attention to a specific option or key action. This helps guide user decisions and directs focus to the most important content.

Response Format:
*   Output ONLY the raw, valid SVG code for the **MODIFIED element** (starting with <svg> and ending with </svg>).
*   The SVG's root element should represent the complete modified element.
*   ABSOLUTELY NO introductory text, explanations, analysis, commentary, or markdown formatting (like ```svg or backticks). Your entire response must be the SVG code itself.
*   Ensure the SVG is well-structured, uses Figma-compatible features, and is ready for direct replacement.
*   Use placeholder shapes (#E0E0E0) for any internal images if needed. Use simple circles for icons.
*   Set an appropriate viewBox, width, and height on the root <svg> tag, ideally matching the original element's dimensions provided in the context.
""",
    tools=[],
)
print(f"Agent '{modify_agent.name}' created using model '{modify_agent.model}'.")


refine_agent = Agent(
    name="prompt_refiner_v1",
    model=AGENT_MODEL, # Must have vision capability
    description="Refines an initial prompt/design instructions, focusing on layout issues.",
    instruction="""
**System Prompt: UI Prompt Refinement Agent**

**Persona:**

You are an expert **UI/UX Analyst and Design Architect**. Your primary skill is translating high-level user requests and concepts for digital interfaces (mobile apps, websites, desktop apps) into highly detailed, structured, and actionable design specifications. You bridge the gap between a simple idea and a concrete design plan.

**Core Objective:**

Your goal is to take a brief user request for a UI design and transform it into a comprehensive, well-organized Markdown document. This document will serve as a detailed **design brief** for a subsequent AI agent (the "UI Design Agent") tasked with generating the actual visual SVG design. The brief must be clear, unambiguous, and provide enough detail for the Design Agent to create an aesthetically pleasing, modern, and functional UI according to best practices.

**Input:**

You will receive a short, often informal, request from a user describing a UI screen or component they want designed. Examples:
* "create mobile home screen for a food app called foodiez that provides local food delivery"
* "design a settings page for a productivity web app"
* "make a login screen for a crypto wallet desktop app"

**Output Requirements:**

You must output **ONLY** a well-structured Markdown document adhering to the following format and principles:

1.  **Title:** Start with a clear title indicating the App/Website Name, Screen Name, Component Name and target platform context (if inferable or specified, e.g., iOS, Android, Web).
    * Example: `# Foodiez - Home Screen (iOS UI Design)`

2.  **Structure:** Break down the UI into logical sections using Markdown headings (`##`, `###`). Common sections include:
    * Status Bar / Top Bar (for mobile)
    * Header / Navigation Bar
    * Hero Section
    * Main Content Area (can be further subdivided)
    * Sidebars (for desktop/web)
    * Footer / Bottom Navigation (for mobile)

3.  **Components:** Within each section, list the specific UI components using bullet points (`-` or `*`). Detail each component clearly:
    * **Type:** Identify the component (e.g., Button, Search Bar, Image Placeholder, Icon Placeholder, Text Input, Card, Carousel, List Item, Tab Bar).
    * **Content:** Specify placeholder text (e.g., `"Search restaurants..."`, `"Username"`, `"Sign Up"`) or describe the type of content (e.g., "User Profile Image", "Restaurant Thumbnail"). Keep text minimal and semantic.
    * **Styling Hints:** Provide cues for the Design Agent, referencing modern aesthetics. Use terms like:
        * "Rounded corners" (specify degree if important: slight, medium, fully rounded)
        * "Soft shadow" / "Subtle shadow"
        * "Gradient background" (mention general color direction if relevant, e.g., "light blue to darker blue vertical gradient")
        * "Clean layout", "Minimalist style"
        * "Vibrant accent color" (suggest a color type if relevant, e.g., "brand orange")
        * "Standard iOS/Material Design spacing"
    * **Layout & Placement:** Describe alignment (e.g., "Centered", "Left-aligned", "Right-aligned icon"), positioning (e.g., "Below the header", "Fixed to bottom"), and arrangement (e.g., "Horizontal row", "Vertical stack", "Grid layout", "Horizontally scrollable carousel").
    * **Iconography:** Specify where icons are needed but refer to them generically (e.g., "Search icon", "Notification icon", "Settings icon", "Favorite icon (outline/filled)"). The Design Agent will use placeholders.
    * **Interactivity Hints (Optional but helpful):** Mention intended states if crucial (e.g., "Active tab highlighted", "Disabled button style").

4.  **Clarity and Detail:** Be specific enough to avoid ambiguity but avoid overly prescriptive visual details that stifle the Design Agent's creativity (unless the user request was highly specific). Focus on *what* elements are needed and *where* they generally go, along with key style attributes.

5.  **Consistency:** Ensure terminology and structure are consistent throughout the brief.

6.  **Formatting:** Use standard Markdown:
    * Headings (`#`, `##`, `###`) for sections.
    * Bullet points (`-`, `*`) for lists of components or attributes.
    * Bold (`**text**`) for component names or key attributes.
    * Italics (`*text*`) for placeholder text examples or secondary details.
    * Code blocks (`) for specific text like placeholder content is optional but can improve clarity.

**Example Output Structure (Based on User's Example):**

```markdown
# AppName - ScreenName (Platform UI Design) or ComponentName

Design a [brief description, e.g., clean, modern] mobile UI screen for a [platform, e.g., iOS] app titled [App Name] - [Purpose, e.g., Local Food Delivery]. The layout should include the following sections:

---

## 1. Section Name (e.g., Header)
- **Component 1**: [Type, e.g., Centered Logo Text]
  - **Content**: [Placeholder, e.g., "Foodiez"]
  - **Font**: [Hints, e.g., Medium weight, small size]
  - **Color**: [Hints, e.g., Brand orange text]
- **Component 2**: [Type, e.g., Right-aligned Icon Button]
  - **Icon**: [Placeholder, e.g., Notification icon]
  - **Style**: [Hints, e.g., Rounded, 32px bounding box]

---

## 2. Section Name (e.g., Search & Filter Row)
- **Component 1**: [Type, e.g., Search Bar]
  - **Placeholder**: [*Search restaurants or dishes...*]
  - **Style**: [Hints, e.g., Rounded corners, light gray background, subtle border]
  - **Layout**: [Hints, e.g., Search icon aligned left inside bar]
- **Component 2**: [Type, e.g., Filter Button/Dropdown]
  - **Content**: [Placeholder, e.g., "Sort By"]
  - **Icon**: [Placeholder, e.g., Down arrow icon]

---

## 3. Section Name (e.g., Content Area - Featured Items)
- **Layout**: [Hints, e.g., Horizontally scrollable carousel]
- **Item Type**: [Description, e.g., Restaurant Card]
  - **Style**: [Hints, e.g., Rounded corners, soft shadow]
  ### Card Item Details
  - **Component 1**: [Type, e.g., Image Placeholder]
    - **Content**: [Description, e.g., Restaurant photo thumbnail]
    - **Style**: [Hints, e.g., Aspect ratio 16:9]
  - **Component 2**: [Type, e.g., Text - Title]
    - **Content**: [Placeholder, e.g., "Restaurant Name"]
    - **Font**: [Hints, e.g., Bold, medium size]
  - **Component 3**: [Type, e.g., Text - Subtitle]
    - **Content**: [*Cuisine • Delivery Time • Rating*]
    - **Font**: [Hints, e.g., Regular weight, small size]
    - **Color**: [Hints, e.g., Muted gray text]

---

## 4. Section Name (e.g., Bottom Navigation Bar)
- **Style**: [Hints, e.g., Standard iOS tab bar layout, background blur/color]
- **Tabs**: [List the tabs]
  - **Tab 1**: [Name, e.g., Home]
    - **Icon**: [Placeholder, e.g., Home icon]
    - **State**: [e.g., Active]
    - **Style**: [Hints, e.g., Highlighted icon and label (brand color)]
  - **Tab 2**: [Name, e.g., Search]
    - **Icon**: [Placeholder, e.g., Search icon]
    - **State**: [e.g., Inactive]
    - **Style**: [Hints, e.g., Default gray icon and label]
  - ... (other tabs) ...
- **Layout**: [Hints, e.g., Equal horizontal
```
""",
    tools=[], # No external tools needed for refinement itself
)
print(f"Agent '{refine_agent.name}' created using model '{refine_agent.model}'.")


# Agent for handling answers (No change needed here)
answer_agent = Agent(
    name="answer_agent_v1",
    model=AGENT_MODEL, # Capable of tool calling if needed
    description="Answers user questions by searching the internet for relevant and up-to-date information.",
    instruction="""
You are a friendly and helpful AI Design Assistant named "Design Buddy".  Your primary purpose is to assist users with their design-related questions and tasks. You have access to a web search tool and should use it to find up-to-date information, examples, and inspiration for the user. You are designed to be conversational and able to chat casually in any language the user uses.

**Core Capabilities:**

*   **Design Expertise:** You possess knowledge about various design fields, including but not limited to: graphic design, web design, UI/UX design, branding, interior design, architecture, product design, and fashion design.  Be ready to discuss design principles, trends, software, and best practices.
*   **Web Search:** You have access to a web search tool.  Use this tool proactively whenever the user asks for:
    *   Design inspiration (e.g., "Show me examples of minimalist websites," "I need logo design ideas for a coffee shop," "What are the latest trends in packaging design?")
    *   Specific design resources (e.g., "Find me a free icon library," "Where can I download Photoshop brushes?," "What are the best color palette generators?")
    *   Information about design tools or software (e.g., "What are the pros and cons of Figma vs. Adobe XD?," "How do I use the pen tool in Illustrator?").
    *   Information or meaning or definition of design terms.
*   **Website Recommendations:** When providing websites as part of your search results, always include the website name and a direct link to the site.  Briefly explain what the website offers or why it is relevant to the user's request.
*   **Multi-Lingual Support:**  You can communicate fluently in any language the user uses. Respond in the same language.
*   **Chat & Friendly Conversation:** You can engage in casual conversation. Be friendly, approachable, and patient. Use emojis where appropriate to convey tone, but avoid overusing them.
*   **Clarification:** If a user's request is unclear, ask clarifying questions to understand their needs better. For example, ask about the specific design style they are looking for, the target audience, or the intended purpose of the design.
*   **Summarization:** If you are giving a long answer, break it down into small paragraphs, or bullet points for better understanding.
*   **Don't be afraid to say you don't know:** If you are asked a question you do not know the answer to, use your web search tool to find the answer. If you are still unable to find the answer, be honest and say that you don't know, but offer to help them find alternative resources.

**Instructions for Using the Web Search Tool:**

1.  Before responding, analyze the user's request to determine if a web search would be helpful.
2.  Formulate a clear and specific search query that will yield relevant results.
3.  Execute the web search using the available tool.
4.  Review the search results carefully.
5.  Summarize the most relevant findings for the user, providing links to the original sources whenever possible.

**Example Interactions:**

**User:** I need some inspiration for a website design for a yoga studio.

**Design Buddy:**  Namaste!  I can definitely help with that. I'll search the web for some inspiring yoga studio website designs.  One moment...

*(Web Search Conducted)*

**Design Buddy:** Okay, I found some great examples! Here are a few websites that showcase beautiful and effective designs for yoga studios:

*   **YogaGlo (yogado.com):** This website has a clean and modern design with beautiful photography and clear navigation. It's a great example of how to create a calming and inviting online experience.
*   **Gaia (gaia.com):**  Gaia features a more earthy and spiritual aesthetic, with rich imagery and a focus on community.
*   **[Find 2-3 more examples and provide descriptions and links]**

Would you like me to look for anything more specific, like websites that focus on a particular style of yoga (e.g., Vinyasa, Hatha)?

**User:** What is the golden ratio in design?

**Design Buddy:** The golden ratio, often represented by the Greek letter phi (φ), is approximately 1.618. It's a mathematical ratio that appears frequently in nature and is often used in design to create aesthetically pleasing and harmonious compositions. I will search web to see if I can get more details.

*(Web Search Conducted)*

**Design Buddy:** Okay, here is what I found from web. It is often used in design to create aesthetically pleasing and harmonious compositions. [Website link: some_site] It works by... [rest of the summary of what you find on the web]

**User:** Hola! Necesito un logo para mi nueva panadería. (Hi! I need a logo for my new bakery.)

**Design Buddy:** ¡Hola! ¡Qué bueno que te puedo ayudar con eso! Voy a buscar algunas ideas de logos para panaderías. ¿Tienes alguna preferencia de estilo o colores? (Hi! Great that I can help you with that! I'm going to search for some bakery logo ideas. Do you have any style or color preferences?)

**Important Considerations:**

*   **Safety:**  Avoid providing information that is harmful, unethical, or illegal.
*   **Bias:** Strive to provide neutral and unbiased information. Present different perspectives when appropriate.
*   **Creativity:** While you should be helpful and informative, also try to inspire the user and encourage them to think creatively.
*   **Stay Updated:** Design trends and technologies change rapidly.  Use your web search to stay informed about the latest developments in the field.

By following these guidelines, you can be a valuable and engaging AI Design Assistant for users of all skill levels. Good luck!
""",
    tools=[google_search],
)
print(f"Agent '{answer_agent.name}' created using model '{answer_agent.model}' with tool(s): {[tool.name for tool in answer_agent.tools]}.")


# --- Helper Function to Validate SVG ---

def is_valid_svg(svg_string):
    """
    Validates whether the input string is a plausible SVG content.
    Strips optional code block markers and checks for basic SVG structure.
    """

    if not svg_string or not isinstance(svg_string, str):
        return False

    # Remove markdown-style code block indicators like ```svg, ```xml, or backticks
    svg_clean = re.sub(r'^\s*```(?:svg|xml)?\s*', '', svg_string.strip(), flags=re.IGNORECASE)
    svg_clean = re.sub(r'\s*```\s*$', '', svg_clean, flags=re.IGNORECASE)

    # Normalize whitespace and lowercase for tag checks
    svg_clean_lower = svg_clean.lower()

    # Check presence of basic opening and closing SVG tags
    has_svg_start = '<svg' in svg_clean_lower
    has_svg_end = '</svg>' in svg_clean_lower

    # Ensure final tag closes properly
    ends_with_gt = svg_clean.strip().endswith('>')

    return has_svg_start and has_svg_end and ends_with_gt

async def run_adk_interaction(agent_to_run, user_content, user_id="figma_user"):
    """Runs a single ADK agent interaction and returns the final text response."""
    final_response_text = None
    session_id = f"session_{uuid.uuid4()}" # Unique session per interaction

    # Create a temporary session for this request
    session = session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )

    print(f"Running agent '{agent_to_run.name}' in session '{session_id}'...")
    runner = Runner(
        agent=agent_to_run,
        app_name=APP_NAME,
        session_service=session_service
    )

    try:
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=user_content
        ):
            print(f"  [Event] Author: {event.author}, Type: {type(event).__name__}, Final: {event.is_final_response()}, Action: {event.actions}") # Debug logging

            # Specific handling for decision agent (expects single word)
            if agent_to_run.name == decision_agent.name:
                 if event.is_final_response() and event.content and event.content.parts:
                     final_response_text = event.content.parts[0].text.strip().lower()
                     print(f"  Decision Agent Raw Output: '{event.content.parts[0].text}', Processed: '{final_response_text}'")
                     # Basic validation for decision agent output
                     if final_response_text not in ['create', 'modify', 'answer']:
                         print(f"  WARNING: Decision agent returned unexpected value: '{final_response_text}'")
                         # Optionally treat unexpected as 'answer' or raise error
                         final_response_text = None # Mark as invalid/failed
                     break # Decision agent should finish quickly

            # Handle final response for other agents
            elif event.is_final_response():
                if event.content and event.content.parts:
                    final_response_text = event.content.parts[0].text
                    # print(f"  Final response text received (len={len(final_response_text)}).")
                # Check for escalation *even* on final response event
                if event.actions and event.actions.escalate:
                    error_msg = f"Agent escalated: {event.error_message or 'No specific message.'}"
                    print(f"  ERROR: {error_msg}")
                    final_response_text = f"AGENT_ERROR: {error_msg}" # Propagate error
                break # Stop processing events once final response or escalation found

            # Handle explicit escalation before final response
            elif event.actions and event.actions.escalate:
                 error_msg = f"Agent escalated before final response: {event.error_message or 'No specific message.'}"
                 print(f"  ERROR: {error_msg}")
                 final_response_text = f"AGENT_ERROR: {error_msg}" # Propagate error
                 break # Stop processing events

    except Exception as e:
         print(f"Exception during ADK run_async for agent '{agent_to_run.name}': {e}")
         final_response_text = f"ADK_RUNTIME_ERROR: {e}" # Propagate exception message
    finally:
         # Clean up the temporary session
         try:
             session_service.delete_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
             # print(f"Cleaned up session '{session_id}'.")
         except Exception as delete_err:
             print(f"Warning: Failed to delete temporary session '{session_id}': {delete_err}")

    print(f"Agent '{agent_to_run.name}' finished. Raw Result: {'<empty>' if not final_response_text else final_response_text[:100] + '...'}")
    return final_response_text


# --- API Endpoint ---
@app.route('/generate', methods=['POST'])
async def handle_generate(): # Make the route async
    """Handles requests using ADK agents with create->refine flow."""
    if not request.is_json:
        return jsonify({"success": False, "error": "Request must be JSON"}), 400
    
    global chat_history
    data = request.get_json()
    user_prompt_text = data.get('userPrompt')
    context = data.get('context', {}) # Contains frameName, elementInfo for modify
    frame_data_base64 = data.get('frameDataBase64') # Only for modify
    element_data_base64 = data.get('elementDataBase64') # Only for modify
    i_mode = data.get('mode')

    if not user_prompt_text:
        return jsonify({"success": False, "error": "Missing 'userPrompt'"}), 400

    print(f"Received request: prompt='{user_prompt_text[:50]}...', context keys: {list(context.keys())}, frame_data: {'yes' if frame_data_base64 else 'no'}, element_data: {'yes' if element_data_base64 else 'no'}")

    # --- 1. Determine Intent ---
    # Prepare content for decision agent (only needs text prompt + potentially context text)
    decision_prompt = f"""
**User Request**
{user_prompt_text}
            
**Previous Conversations with the Agent**
{chat_history}
"""
    if context:
        decision_prompt += f"\n**Figma Context**\n{context}" # Add context if available

    decision_content = google_genai_types.Content(role='user', parts=[
        google_genai_types.Part(text=decision_prompt)
    ])

    # Run decision agent
    intent_mode = await run_adk_interaction(decision_agent, decision_content)

    # Handle decision agent failure or invalid output
    if not intent_mode or intent_mode.startswith("AGENT_ERROR:") or intent_mode.startswith("ADK_RUNTIME_ERROR:"):
         error_msg = f"Could not determine intent. Agent Response: {intent_mode}"
         print(error_msg)
         # Return 200 OK with error message for UI display
         return jsonify({"success": False, "error": error_msg}), 200
    if intent_mode not in ['create', 'modify', 'answer']:
        error_msg = f"Intent determination failed: Agent returned unexpected value '{intent_mode}'."
        print(error_msg)
        return jsonify({"success": False, "error": error_msg}), 200 # 200 OK for UI

    print(f"Determined Intent: '{intent_mode}'")

    # --- 2. Execute Based on Intent ---
    final_result = None
    final_type = "unknown" # To track if we should expect 'svg' or 'answer'

    try:
        # --- CREATE Flow (Create -> Convert -> Refine) ---
        if intent_mode == 'create' and i_mode=='create':
            final_type = "svg"
            # A) Run Create Agent
            print("--- Running Create Agent ---")
            create_content = google_genai_types.Content(role='user', parts=[
                google_genai_types.Part(text=user_prompt_text) # Use original user prompt for creation
            ])
            refined_prompt = await run_adk_interaction(refine_agent, create_content)
            refined_prompt = refined_prompt.strip().replace("```markdown", "").replace("```", "").strip()
            mod_prompt = f"""
${refined_prompt}

Your Mission Goals:

*   **Astonishing Visual Appeal:** Use a vibrant yet harmonious color palette, incorporating gradients and subtle shadows to create depth and visual interest.
*   **Mesmerizing Detail:** Add intricate details, like subtle textures or patterns, without overwhelming the overall design. Consider micro-interactions on hover for added engagement.
*   **Eye-Catching Design:** Employ a clear visual hierarchy, guiding the user's eye through the design using size, color, and placement.
*   **Beautiful Harmony:** Ensure all elements are balanced and work together cohesively, creating a sense of visual harmony and flow.
*   **Pretty Interactivity Design:** Think about hover effects, transitions and other visual cues that can be replicated (or hinted at) within the SVG structure and can be easily implemented in Figma.
*   **Consistency:** Maintain consistent spacing, fonts, colors, and icons across the site for a polished, professional feel.
*   **Invariance (Highlight Key Options):** Try to utilize contrasting design elements (e.g., in pricing tables) to draw attention to a specific option or key action. This helps guide user decisions and directs focus to the most important content.

Response format:

*   Output ONLY valid, well-formed SVG code.
*   Use descriptive group names for all elements (e.g., "hero-section", "card-title").
*   Avoid creating custom icons instead use circles as placeholder for icons.
*   Utilize text-anchor for proper text alignment.
*   Try to add minimal text as possible, do add unneccessary text or emojis where not required.
*   Employ rounded corners extensively for a modern look.
*   Use gradients to add depth and visual appeal.
*   Incorporate placeholder rectangles for images, using a subtle gray color.
*   Ensure elements do not overlap and maintain consistent spacing.
*   Use comments sparingly, only to clarify complex structures.
*   Optimize SVG for Figma import - clean code, proper groups.
"""

            refined_content = google_genai_types.Content(role='user', parts=[
                google_genai_types.Part(text=mod_prompt) # Use original user prompt for creation
            ])
            initial_svg = await run_adk_interaction(create_agent, refined_content)

            if not initial_svg or initial_svg.startswith("AGENT_ERROR:") or initial_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Create Agent failed or returned error: {initial_svg}")
            if not is_valid_svg(initial_svg):
                raise ValueError(f"Create Agent response is not valid SVG even after cleaning. Snippet: {initial_svg[:200]}...")
            else:
                if initial_svg.strip().startswith("```svg"):
                    initial_svg = initial_svg.strip().replace("```svg", "").replace("```", "").strip()
                if initial_svg.strip().startswith("```xml"):
                   initial_svg = initial_svg.strip().replace("```xml", "").replace("```", "").strip()

            print("Initial SVG created and validated.",initial_svg)
            final_result = initial_svg
            chat_history.append({'user': user_prompt_text, 'AI': "I have created the figma design, let me know if you require any further changes or assistance with anything else."})

        # --- MODIFY Flow ---
        elif intent_mode == 'modify' and i_mode=='modify':
            final_type = "svg"
            print("--- Running Modify Agent ---")
            # Validate required inputs for modify
            if not frame_data_base64:
                 raise ValueError("Missing 'frameDataBase64' for modify mode")
            if not element_data_base64:
                 raise ValueError("Missing 'elementDataBase64' for modify mode")
            if not context.get('elementInfo'):
                 raise ValueError("Missing 'elementInfo' in context for modify mode")
            
            create_content = google_genai_types.Content(role='user', parts=[
                google_genai_types.Part(text=user_prompt_text) # Use original user prompt for creation
            ])
            refined_prompt = await run_adk_interaction(refine_agent, create_content)
            refined_prompt = refined_prompt.strip().replace("```markdown", "").replace("```", "").strip()

            # Prepare prompt and image parts for modify agent
            modify_prompt = f"""
**Modification Request**
{refined_prompt}

**Figma Context**
Frame Name: {context.get('frameName', 'N/A')}
Element Info: {context['elementInfo']}
"""
            message_parts = [google_genai_types.Part(text=modify_prompt)]

            try:
                frame_bytes = base64.b64decode(frame_data_base64)
                element_bytes = base64.b64decode(element_data_base64)
                message_parts.append(google_genai_types.Part(
                    inline_data=google_genai_types.Blob(mime_type="image/png", data=frame_bytes)
                ))
                message_parts.append(google_genai_types.Part(
                    inline_data=google_genai_types.Blob(mime_type="image/png", data=element_bytes)
                ))
                print("Frame and Element image parts prepared for modify agent.")
            except Exception as e:
                raise ValueError(f"Invalid image data received for modify mode: {e}")

            modify_content = google_genai_types.Content(role='user', parts=message_parts)
            modified_svg = await run_adk_interaction(modify_agent, modify_content)

            if not modified_svg or modified_svg.startswith("AGENT_ERROR:") or modified_svg.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Modify Agent failed or returned error: {modified_svg}")
            if not is_valid_svg(modified_svg):
                 # Try cleaning potential markdown
                 if modified_svg.strip().startswith("```svg"):
                     modified_svg = modified_svg.strip().replace("```svg", "").replace("```", "").strip()
                     if not is_valid_svg(modified_svg):
                        raise ValueError(f"Modify Agent response is not valid SVG even after cleaning. Snippet: {modified_svg[:200]}...")
                 else:
                    raise ValueError(f"Modify Agent response is not valid SVG. Snippet: {modified_svg[:200]}...")


            print("SVG modification successful and validated.")
            final_result = modified_svg
            chat_history.append({'user': user_prompt_text, 'AI': "I have modified the component, let me know if you require any further changes or assistance with anything else."})

        # --- ANSWER Flow ---
        elif intent_mode == 'answer':
            final_type = "answer"
            print("--- Running Answer Agent ---")
            mod_prompt = f"""
            {user_prompt_text}
            
            Previous Conversations with the Agent:
            {chat_history}
            """
            answer_content = google_genai_types.Content(role='user', parts=[
                google_genai_types.Part(text=mod_prompt)
            ])
            answer_text = await run_adk_interaction(answer_agent, answer_content)

            if not answer_text: # Allow empty answers if agent genuinely finds nothing
                 print("Answer agent returned empty response.")
                 answer_text = "I could not find specific information regarding your query." # Provide a default if empty
            elif answer_text.startswith("AGENT_ERROR:") or answer_text.startswith("ADK_RUNTIME_ERROR:"):
                raise ValueError(f"Answer Agent failed or returned error: {answer_text}")

            print("Answer agent finished.")
            if len(chat_history) > 10:
                chat_history.pop(0)
            
            # Append the latest user prompt and AI response to chat history
            final_result = answer_text
            chat_history.append({'user': user_prompt_text, 'AI': final_result})
        else:
            return jsonify({"success": False, "error": "Please select frame or component"}), 200


    # --- Handle Execution Errors ---
    except ValueError as ve: # Catch specific validation/logic errors
        error_message = str(ve)
        print(f"Error during '{intent_mode}' execution: {error_message}")
        # Return 200 OK but with success: False for UI to display the error
        return jsonify({"success": False, "error": error_message}), 200
    except Exception as e:
        # Catch broader exceptions during agent runs or processing
        error_message = f"An unexpected error occurred during '{intent_mode}' execution: {e}"
        print(error_message)
        # Return 500 for unexpected server errors
        return jsonify({"success": False, "error": "An internal server error occurred."}), 500


    # --- Format and Return Success Response ---
    if final_result is None:
         # Should ideally be caught by errors above, but as a safeguard
         print(f"Execution completed but final_result is unexpectedly None for mode '{intent_mode}'.")
         return jsonify({"success": False, "error": "Agent processing failed to produce a result."}), 500

    if final_type == "svg":
        print("Returning successful SVG response.")
        # Final cleanup just in case markdown slipped through validation
        if final_result.strip().startswith("```"):
             final_result = final_result.strip().replace("```svg", "").replace("```", "").strip()
        return jsonify({"success": True, "svg": final_result})
    elif final_type == "answer":
        print("Returning successful Answer response.")
        return jsonify({"success": True, "answer": final_result, "mode": "answer"})
    else:
        # Should not happen if logic is correct
        print(f"Error: Unknown final_type '{final_type}' after processing.")
        return jsonify({"success": False, "error": "Internal error: Unknown result type."}), 500

# --- Run the App ---
if __name__ == '__main__':
    # Make sure the model selected supports vision!
    print(f"Using model: {AGENT_MODEL} for all agents requiring it.")
    app.run(host='0.0.0.0', port=5001)