// =========================================================================
// AI Design Assistant - Figma Plugin Code (Backend + Auth Version)
// =========================================================================

figma.showUI(__html__, {
    width: 450,
    height: 600, // Adjusted height for auth section
    title: "AI Design Assistant",
});

// State variables
let lastNotifiedFrameId = null;
let lastNotifiedMode = null;
let originalSelectedNodeId = null; // Still needed for modify replacement
let isProcessing = false; // Use this flag to prevent selection changes during AI process

// findTopLevelFrame function (remains the same)
function findTopLevelFrame(node) {
    let current = node;
    if (!current) return null; // Handle null/undefined input
    // Check if the direct parent is a Page
    if (current.type === "FRAME" && current.parent && current.parent.type === "PAGE") {
        return current;
    }
    // Traverse up the parent chain
    let parent = current.parent;
    while (parent) {
        // Check if the current parent is a top-level frame (parent's parent is Page)
        if (parent.type === "FRAME" && parent.parent && parent.parent.type === "PAGE") {
            return parent;
        }
        // Stop if we reach the page without finding a top-level frame
        if (parent.type === "PAGE") {
            return null;
        }
        parent = parent.parent; // Move up to the next parent
    }
    return null; // Reached the root without finding a suitable frame
}


// --- Selection Change Handler ---
figma.on("selectionchange", async () => {
    if (isProcessing) {
         // console.log("Selection change ignored during processing."); // Keep this quiet unless debugging
         return; // Ignore selection changes while processing is active
    }
    const selection = figma.currentPage.selection;
    let mode = 'answer'; // Default mode is answer
    let frameId = null;
    let frameName = null;
    let elementInfo = null;
    originalSelectedNodeId = null; // Reset stored element ID

    // If no selection or multiple selections, default to 'answer' mode and inform UI
    if (selection.length !== 1) {
        figma.ui.postMessage({
            type: "selection-invalid",
            reason: "Please select exactly one item.",
        });
        lastNotifiedFrameId = null;
        lastNotifiedMode = 'answer';
        return;
    }

    const selectedNode = selection[0];

    // Check if the selected node is the canvas itself (Page)
    if (selectedNode.type === "PAGE") {
         figma.ui.postMessage({
            type: "selection-invalid",
            reason: "Please select a frame or an element, not the page.",
        });
        lastNotifiedFrameId = null;
        lastNotifiedMode = 'answer';
        return;
    }

    const targetFrame = findTopLevelFrame(selectedNode);

    // If no top-level frame found for the selected node
    if (!targetFrame) {
         if (selectedNode.type === "FRAME" && selectedNode.parent && selectedNode.parent.type === "PAGE") {
             // If the selected node *is* a top-level frame, check if it's empty
             if (selectedNode.children.length === 0) {
                 mode = "create";
                 frameId = selectedNode.id;
                 frameName = selectedNode.name;
             } else {
                 // Top-level frame with content -> invalid target for create/modify
                 figma.ui.postMessage({
                    type: "selection-invalid",
                    reason: "Select element *inside* frame to modify, or an *empty* frame to create.",
                });
                lastNotifiedFrameId = null;
                lastNotifiedMode = 'answer';
                return;
             }
         } else {
             // Selected item is not in a top-level frame and is not one itself
             figma.ui.postMessage({
                 type: "selection-invalid",
                 reason: "Selected item must be within a top-level frame.",
             });
             lastNotifiedFrameId = null;
             lastNotifiedMode = 'answer';
             return;
         }

    } else {
        // Item is inside a top-level frame (targetFrame is valid)
        frameId = targetFrame.id;
        frameName = targetFrame.name;

        if (selectedNode.id === targetFrame.id && targetFrame.children.length === 0) {
            // Selected the frame itself, and it's empty -> create mode
            mode = "create";
        } else if (selectedNode.id !== targetFrame.id) {
            // Selected something *inside* the frame -> modify mode
            mode = "modify";
            originalSelectedNodeId = selectedNode.id; // Store ID for potential replacement
            elementInfo = {
                id: selectedNode.id,
                name: selectedNode.name,
                type: selectedNode.type,
                width: selectedNode.width,
                height: selectedNode.height,
            };
        } else if (selectedNode.id === targetFrame.id && targetFrame.children.length > 0) {
             // Selected the frame itself, but it's NOT empty -> invalid target for create/modify
             figma.ui.postMessage({
                 type: "selection-invalid",
                 reason:
                     "Select element *inside* frame to modify, or an *empty* frame to create.",
             });
             lastNotifiedFrameId = null;
             lastNotifiedMode = 'answer';
             return;
         }
         // Any other case for a valid targetFrame where selectedNode is not the frame
         // should fall into the modify check `selectedNode.id !== targetFrame.id`
    }


    // Send update only if relevant state changes
    // This ensures the UI mode/selection info is kept up-to-date.
    // Use a simple check on mode and frameId, and the presence of elementInfo for modify
    const currentElementInfoJson = elementInfo ? JSON.stringify(elementInfo) : null;
    const lastNotifiedElementInfoJson = figma.ui.element ? JSON.stringify(figma.ui.element) : null; // Assuming UI state is accessible for comparison (it's not directly, this is hypothetical or requires storing it in code.js state)
    // Simpler check:
    if (frameId !== lastNotifiedFrameId || mode !== lastNotifiedMode || (mode === 'modify' && currentElementInfoJson !== lastNotifiedElementInfoJson)) {
        lastNotifiedFrameId = frameId;
        lastNotifiedMode = mode;
        // The UI will store the elementInfo state based on the message

        figma.ui.postMessage({
            type: "selection-update",
            mode: mode,
            frameId: frameId,
            frameName: frameName,
            element: elementInfo, // null if mode is 'create'
        });
    }
});

// --- Message Handling from UI ---
figma.ui.onmessage = async (msg) => {
    console.log("Message received from ui.html:", msg.type);

    // --- Request from UI to START AI Generation (Initial trigger after auth/prompt) ---
    if (msg.type === "request-ai-generation") {
        isProcessing = true; // Set processing flag
        const { mode, frameId, userPrompt, elementInfo } = msg;

        // --- Add Try...Catch block around the main processing logic in code.js ---
        try {
            figma.ui.postMessage({
                type: "status-update",
                text: `Preparing "${mode}" request...`,
                isLoading: true,
            });
            // figma.notify(`⏳ Preparing "${mode}" request...`); // Notification handled by status update in UI


            // Prepare context object for the backend
            const context = {
                frameName: null, // Will fetch below if frameId exists
            };

            // Fetch the target frame node (only if frameId is provided)
            let targetFrame = null;
            if (frameId) {
                targetFrame = await figma.getNodeByIdAsync(frameId);
                if (!targetFrame || targetFrame.removed || targetFrame.type !== 'FRAME') {
                    // Handle missing/invalid frame early
                    const errorMsg = `Target frame (ID: ${frameId}) not found or invalid. Please reselect.`;
                    console.error(errorMsg);
                    // Don't throw here, just report error and exit this handler
                    figma.ui.postMessage({ type: 'modification-error', error: errorMsg }); // Send error to UI
                    isProcessing = false; // Reset flag
                    return; // Exit the handler
                }
                context.frameName = targetFrame.name;
            } else if (mode !== 'answer') {
                 // If frameId is null but mode is create/modify, this is an inconsistency from selection logic
                 // Should be caught by selectionchange, but this is a safeguard.
                 const errorMsg = `Internal Error: Frame ID is missing for mode "${mode}". Please reselect.`;
                 console.error(errorMsg);
                 figma.ui.postMessage({ type: 'modification-error', error: errorMsg }); // Send error to UI
                 isProcessing = false; // Reset flag
                 return; // Exit the handler
            }


            if (mode === "modify") {
                // For modify, need to export images
                if (!elementInfo || !elementInfo.id) {
                    const errorMsg = "Internal Error: Missing element information for modification. Please reselect.";
                    console.error(errorMsg);
                    figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                    isProcessing = false; // Reset flag
                    return; // Exit the handler
                }
                const elementToModify = await figma.getNodeByIdAsync(elementInfo.id);
                if (!elementToModify || elementToModify.removed) {
                    const errorMsg = `The selected element (ID: ${elementInfo.id}) seems to have been removed. Please reselect.`;
                    console.error(errorMsg);
                    figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                    isProcessing = false; // Reset flag
                    return; // Exit the handler
                }

                context["elementInfo"] = elementInfo; // Add element context to backend payload context

                figma.ui.postMessage({
                    type: "status-update",
                    text: `Exporting frame "${targetFrame.name}" and element for analysis...`,
                    isLoading: true,
                });
                figma.notify(`⏳ Exporting frame "${targetFrame.name}"...`); // Keep notify for prominent status


                // The try...catch block already existed around exports, keep it for specific export errors
                try {
                    // Export settings - adjust scale/format if needed by backend/AI model
                    const exportSettings = {
                        format: "PNG",
                        constraint: { type: "SCALE", value: 1 }, // Export at 1x resolution
                    };
                    // Use Promise.all for potentially faster parallel export
                    const [framePngBytes, elementPngBytes] = await Promise.all([
                         targetFrame.exportAsync(exportSettings),
                         elementToModify.exportAsync(exportSettings)
                    ]);

                    // Tell UI to proceed by calling the backend with vision data
                    // UI will handle base64 encoding and fetch request
                    figma.ui.postMessage({
                        type: "proceed-to-backend-vision", // New message type
                        framePngBytes: framePngBytes, // Send raw bytes
                        elementPngBytes: elementPngBytes, // Send raw bytes
                        userPrompt: userPrompt,
                        context: context, // Contains frameName and elementInfo
                        originalElement: elementInfo, // Still need originalElement info for replacement later
                        // mode: mode, // Pass mode explicitly to UI (though payload should include it)
                    });
                    // UI's callBackendApi will handle setting isLoading=true for the next step
                    // isProcessing flag will be reset when UI reports final result/error

                } catch (error) {
                    // Catch specific export errors here
                    console.error("Error exporting frame/element:", error);
                    const errorMsg = `Export Error: ${error.message || "Unknown error"}`;
                    // Don't throw here, report error to UI and exit this handler
                    figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
                    figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                    isProcessing = false; // Reset flag on export failure
                    return; // Exit the handler
                }


            } else if (mode === "create") {
                // For create, only need the target frame ID for later insertion
                 if (!targetFrame) { // This check is redundant with the one above, but harmless
                      const errorMsg = "Internal Error: Target frame not available for creation.";
                      console.error(errorMsg);
                      figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                      isProcessing = false; // Reset flag
                      return;
                 }
                 // Check if frame is still empty just before proceeding
                 if (targetFrame.children.length > 0) {
                      const errorMsg = `Target frame "${targetFrame.name}" is no longer empty. Cannot create new design.`;
                      console.error(errorMsg);
                      figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
                      figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                      isProcessing = false; // Reset flag
                      return;
                 }

                figma.ui.postMessage({
                    type: "status-update",
                    text: `Preparing to generate design in "${targetFrame.name}"...`,
                    isLoading: true,
                });
                 // figma.notify(`⏳ Preparing to generate design...`); // Notification handled by status update in UI


                // Tell UI to proceed by calling the backend with text data
                figma.ui.postMessage({
                    type: "proceed-to-backend-text", // New message type
                    userPrompt: userPrompt,
                    context: context, // Contains frameName
                    targetFrameId: frameId, // Pass frame ID for insertion later
                    // mode: mode, // Pass mode explicitly to UI
                });
                 // UI's callBackendApi will set isLoading=true for the next step
                 // isProcessing flag will be reset when UI reports final result/error


            } else if (mode === "answer") {
                 // For answer mode, code.js doesn't need to do any special Figma API calls
                 // other than confirming it got the request. The UI will handle the backend call directly.
                 // Just acknowledge the request and let the UI proceed.
                 // This branch is technically not needed if UI sends 'proceed-to-answer-text' directly
                 // from sendButton.onclick for answer mode, but removing the handler in ui.html
                 // means we should keep this branch here to forward the request. Let's stick to
                 // UI calling backend directly for answer mode.
                 // REMOVED: the logic to post proceed-to-answer-text. UI handles answer flow.

                 // If this branch is reached for some reason, it means the UI asked code.js
                 // to start an 'answer' request. We just need to ensure isProcessing is false
                 // after this step, as no further Figma API calls are initiated BY code.js.
                 // The UI will make the backend call.
                 console.log("Code.js received 'request-ai-generation' for 'answer' mode. Acknowledging.");
                 isProcessing = false; // Reset processing flag here, as code.js task is done.
                 // No need to post a specific 'proceed-to-answer-text' message back if UI calls backend directly.

            } else {
                // This case should technically be handled by selectionchange setting mode to 'answer' or 'selection-invalid'
                // but include as a fallback
                const errorMsg = `Internal Error: Unknown mode "${mode}" in request-ai-generation message.`;
                console.error(errorMsg);
                figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                isProcessing = false; // Reset flag
                return; // Exit the handler
            }

        } catch (error) {
             // --- Catch any unexpected error during the initial Figma API interactions ---
             console.error("Unexpected Error during Figma API preparation:", error);
             const errorMsg = `Preparation Error: ${error.message || "Unknown error"}`;
             // Ensure processing flag is reset on error
             isProcessing = false;
             // Send error message back to the UI
             figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
             figma.ui.postMessage({ type: "modification-error", error: errorMsg });
        } finally {
            // isProcessing flag is reset in the catch block or the subsequent message handlers in UI
            // No need to reset here in the outer finally, as successful paths continue the flow
            // driven by messages.
        }
    }

    // --- Messages FROM UI (after successful backend call) to insert SVG ---
    // These handlers should already correctly set isProcessing = false upon completion/error
    else if (msg.type === "finalize-creation") {
        const { svgContent, targetFrameId } = msg;

        // Basic validation if SVG content is present and looks like SVG start
        if (!svgContent || typeof svgContent !== "string" || !svgContent.trim().toLowerCase().startsWith("<svg")) {
            const errorMsg = "Invalid SVG content received from backend/UI for creation.";
            console.error(errorMsg);
            isProcessing = false; // Ensure reset
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: "modification-error", error: errorMsg }); // Send error to UI
            return; // Exit the handler
        }

        const targetFrame = await figma.getNodeByIdAsync(targetFrameId);
        if (!targetFrame || targetFrame.removed || targetFrame.type !== "FRAME") {
            const errorMsg = `Target frame (ID: ${targetFrameId}) not found or invalid for insertion. Please reselect.`;
             console.error(errorMsg);
            isProcessing = false; // Ensure reset
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: "modification-error", error: errorMsg }); // Send error to UI
            return; // Exit the handler
        }
        // Re-check emptiness just before insertion - this is critical!
        if (targetFrame.children.length > 0) {
            const errorMsg = `Target frame "${targetFrame.name}" is no longer empty. Creation aborted.`;
             console.error(errorMsg);
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });
            figma.notify(`❌ ${errorMsg}`, {
                error: true,
                timeout: 5000,
            });
            isProcessing = false; // Ensure reset
            return; // Exit the handler
        }

        figma.ui.postMessage({
            type: "status-update",
            text: "Importing generated SVG...",
            isLoading: true,
        });
        figma.notify("⏳ Importing generated SVG..."); // Keep notify for prominent status


        try {
            // --- Use figma.createNodeFromSvg directly for import. It will handle parsing and validation. ---
            const newNode = figma.createNodeFromSvg(svgContent);

            if (!newNode) {
                 // figma.createNodeFromSvg can return null or throw an error depending on input/Figma version
                 // Explicitly handle null return
                throw new Error("Figma importer failed to create a node from the SVG content. The SVG might be invalid.");
            }

            newNode.name = "AI Generated Design";
            targetFrame.appendChild(newNode);

            console.log(
                `Successfully added node ${newNode.id} to frame ${targetFrameId}`
            );
            figma.currentPage.selection = [newNode];
            figma.viewport.scrollAndZoomIntoView([newNode]);
            figma.notify("✅ New design generated successfully!");
            isProcessing = false; // Reset flag after successful action
            figma.ui.postMessage({ type: "creation-success" }); // Final success to UI

        } catch (error) {
            console.error("Error creating node from SVG or inserting:", error);
            const errorMsg = `SVG Import/Insertion Error: ${error.message || "Unknown error"}`;
            isProcessing = false; // Ensure reset
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: "modification-error", error: errorMsg }); // Send error to UI
        }
    }

    // --- Messages FROM UI (after successful backend call) to replace element ---
    // This handler should already correctly set isProcessing = false upon completion/error
    else if (msg.type === "replace-element-with-svg") {
        const { svgContent, originalElementId } = msg; // Get original ID from UI message

        // Basic validation if SVG content is present and looks like SVG start
        if (!svgContent || typeof svgContent !== "string" || !svgContent.trim().toLowerCase().startsWith("<svg")) {
             const errorMsg = "Invalid SVG content received from backend/UI for replacement.";
            console.error(errorMsg);
            isProcessing = false; // Ensure reset
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: "modification-error", error: errorMsg }); // Send error to UI
            return; // Exit the handler
        }
        if (!originalElementId) {
             const errorMsg = "Internal Error: Missing original element ID for replacement.";
             console.error(errorMsg);
            isProcessing = false; // Ensure reset
            figma.ui.postMessage({ type: "modification-error", error: errorMsg }); // Send error to UI
            return; // Exit the handler
        }

        const originalElement = await figma.getNodeByIdAsync(originalElementId);
        if (!originalElement || originalElement.removed) {
            const errorMsg = `Original element (ID: ${originalElementId}) not found or was removed. Cannot replace. Please reselect.`;
            console.error(errorMsg);
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });
            figma.notify(errorMsg, { error: true });
            isProcessing = false; // Ensure reset
            return; // Exit the handler
        }
        if (!originalElement.parent || originalElement.parent.type === "PAGE") {
            const errorMsg = `Cannot replace top-level elements directly.`;
            console.error(errorMsg);
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });
            figma.notify(errorMsg, { error: true });
            isProcessing = false; // Ensure reset
            return; // Exit the handler
        }

        figma.ui.postMessage({
            type: "status-update",
            text: "Importing modified element SVG...",
            isLoading: true,
        });
        figma.notify("⏳ Importing modified element SVG..."); // Keep notify for prominent status


        let newNode = null;
        try {
             // --- Use figma.createNodeFromSvg directly for import ---
            newNode = figma.createNodeFromSvg(svgContent);

            if (!newNode) {
                 // Explicitly handle null return
                 throw new Error(
                     "Figma importer failed to create a node from the element SVG content. The SVG might be invalid."
                 );
            }
            newNode.name = `${originalElement.name} (AI Modified)`;

            // --- Replacement Logic ---
            const parent = originalElement.parent;
            const index = parent.children.indexOf(originalElement);
            if (index === -1) {
                // This should not happen if originalElement.parent is valid
                throw new Error(
                    "Internal Error: Could not find original element in its parent's children list."
                );
            }
            const originalX = originalElement.x;
            const originalY = originalElement.y;
            const originalWidth = originalElement.width;
            const originalHeight = originalElement.height;
            const originalConstraints = originalElement.constraints;

            // Insert the new node at the same position as the old one
            parent.insertChild(index + 1, newNode);
            newNode.x = originalX;
            newNode.y = originalY;

            // Attempt to apply original constraints
            try {
                if (originalConstraints) {
                    newNode.constraints = originalConstraints;
                }
            } catch (constraintError) {
                console.warn(`Could not apply constraints: ${constraintError.message}`);
            }

            // Attempt to resize to original dimensions (might distort if aspect ratio changed significantly)
            if (newNode.resize) {
                 if (newNode.width > 0 && newNode.height > 0) {
                    newNode.resize(originalWidth, originalHeight);
                 } else {
                     console.warn("New SVG node has zero dimensions, cannot resize to original.");
                 }
            } else {
                 console.warn("New SVG node does not support resize operation.");
            }

            // Remove original AFTER successful insert/position
            originalElement.remove();
            // --- End Replacement Logic ---

            console.log(
                `Successfully replaced element ${originalElementId} with new node: ${newNode.id}`
            );
            figma.currentPage.selection = [newNode];
            figma.viewport.scrollAndZoomIntoView([newNode]);
            isProcessing = false; // Reset flag after successful action
            figma.notify("✅ Element successfully modified!");
            figma.ui.postMessage({ type: "modification-success" }); // Signal success to UI

        } catch (error) {
            console.error(
                "Error creating node from SVG or replacing element:",
                error
            );
            const errorMsg = `Element SVG Import/Replacement Error: ${error.message || "Unknown error"}`;
            isProcessing = false; // Ensure reset
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: "modification-error", error: errorMsg }); // Send error to UI

            // Cleanup partially added node if necessary
            if (
                newNode &&
                !newNode.removed &&
                newNode.parent !== originalElement?.parent // Check against original parent in case original was removed
            ) {
                try {
                    newNode.remove();
                } catch (cleanupError) {
                     console.warn("Cleanup failed for partially added node:", cleanupError);
                }
            }
        }
    }

     // --- Message from UI reporting errors during backend communication ---
    else if(msg.type === "backend-error" || msg.type === "modification-error"){
        // UI sends this if fetch fails or backend returns success:false
        // The message contains the error string
        console.error("Error reported from UI/Backend:", msg.error);
        figma.notify(`❌ Error: ${msg.error}`, { error: true, timeout: 5000 });
        isProcessing = false; // Ensure processing flag is reset
        // No need to repost to UI, it already displayed the error
    }
    // Handle messages from UI reporting Answer results (UI displays this directly)
    else if (msg.type === 'answer') {
        // This message type is received by UI when backend responds for 'answer' intent
        // Code.js doesn't need a specific handler for the *answer content* result.
        // If this message type *is* sent TO code.js (which shouldn't happen in the fixed flow),
        // code.js should just log it and ensure isProcessing is reset if it was set.
         console.warn("Code.js received unexpected 'answer' message from UI. Ignoring.");
         isProcessing = false; // Ensure processing flag is reset if it was set.
         // No need to send further messages to UI, it's done.
         return; // Exit handler


    }
    else {
        console.warn("Unknown message type received from UI:", msg.type);
        // Even if unknown, ensure processing flag is reset eventually
        isProcessing = false; // Assume unknown message means processing stopped unexpectedly
    }
};

// Trigger initial selection check on load
// This helps set the initial UI state based on Figma selection on plugin open
// Using a small timeout gives the UI a moment to load before receiving the message
setTimeout(() => {
  figma.trigger('selectionchange');
}, 50); // Adjust timeout if needed

console.log("Figma AI Design Assistant plugin code (Backend + Auth Version) loaded.");
// Optionally clear selection on load to *force* the selectionchange handler when user selects something
figma.currentPage.selection = []; // This would clear user's current selection, maybe not desirable
