// =========================================================================
// AI Design Assistant - Figma Plugin Code (OAuth Backend Version)
// =========================================================================

// Show the UI
figma.showUI(__html__, {
    width: 450,
    height: 580, // Adjusted height slightly
    title: "Designo AI Assistant",
});

// --- State Variables ---
let lastNotifiedFrameId = null;
let lastNotifiedMode = null; // 'create', 'modify', 'answer' (or null initially)
let isProcessing = false; // Flag to prevent concurrent operations

// --- Helper Functions ---

// Finds the top-level frame containing a node
function findTopLevelFrame(node) {
    // This function remains the same - it correctly finds the parent frame.
    let current = node;
    // If the node itself is a top-level frame, return it
    if (current.type === "FRAME" && current.parent.type === "PAGE") {
        return current;
    }
    // Otherwise, traverse up the tree
    let parent = current.parent;
    while (parent) {
        if (parent.type === "FRAME" && parent.parent.type === "PAGE") {
            return parent; // Found the top-level frame
        }
        if (parent.type === "PAGE") {
            return null; // Reached the page without finding a top-level frame
        }
        parent = parent.parent; // Go up one level
    }
    return null; // Should not happen if node is on the page, but good practice
}

// Function to determine current context and notify UI
// Can be called on selection change or initial request
async function updateAndNotifyUI() {
    if (isProcessing) return; // Don't check selection while processing SVG etc.

    const selection = figma.currentPage.selection;
    let mode = 'answer'; // Default to answer mode if nothing valid selected
    let frameId = null;
    let frameName = null;
    let elementInfo = null;
    let notificationReason = "Ready for questions or instructions."; // Default status text

    if (selection.length === 1) {
        const selectedNode = selection[0];
        const targetFrame = findTopLevelFrame(selectedNode);

        if (targetFrame) {
            frameId = targetFrame.id;
            frameName = targetFrame.name;
            const children = targetFrame.children;

            if (selectedNode.id === targetFrame.id && children.length === 0) {
                mode = "create";
                notificationReason = `Selected empty frame "${frameName}". Ready to generate.`;
            } else if (selectedNode.id !== targetFrame.id && selectedNode.parent && selectedNode.parent.type !== "PAGE") {
                // Selected something *inside* a top-level frame
                mode = "modify";
                elementInfo = {
                    id: selectedNode.id,
                    name: selectedNode.name,
                    type: selectedNode.type,
                    width: selectedNode.width, // Ensure width/height are sent
                    height: selectedNode.height,
                };
                notificationReason = `Selected element "${elementInfo.name || 'unnamed'}" (${elementInfo.type}) in frame "${frameName}". Ready to modify.`;
            } else if (selectedNode.id === targetFrame.id && children.length > 0) {
                mode = 'answer'; // Frame selected, but not empty
                notificationReason = `Selected frame "${frameName}" is not empty. Select an element inside to modify, or ask a question.`;
            } else {
                 mode = 'answer'; // Something else selected within the page but not valid target
                 notificationReason = "Selected item is not in a valid frame or is not a modifiable element. Ready for questions.";
            }
        } else {
             mode = 'answer'; // Selection not inside a top-level frame
             notificationReason = "Selected item must be within a top-level frame. Ready for questions.";
        }
    } else if (selection.length > 1) {
        mode = 'answer'; // Multiple items selected
        notificationReason = "Multiple items selected. Please select only one item or ask a question.";
    } else {
        mode = 'answer'; // Nothing selected
        notificationReason = "Nothing selected. Ready for questions or instructions.";
    }

    // --- Send update to UI ---
    // Check if mode or target frame has actually changed to avoid redundant messages
    if (mode !== lastNotifiedMode || frameId !== lastNotifiedFrameId) {
        lastNotifiedMode = mode;
        lastNotifiedFrameId = frameId;

        console.log(`Selection changed: Mode='${mode}', FrameID='${frameId}', ElementID='${elementInfo?.id}'`);

        // Use a generic type that UI handles for both initial and subsequent updates
        figma.ui.postMessage({
            type: "selection-update", // UI can handle this type for initial state too
            mode: mode,
            frameId: frameId,
            frameName: frameName,
            element: elementInfo, // null if not in modify mode
            reason: notificationReason // Add reason for clarity in UI status message (optional)
        });
    }
}


// --- Figma Event Listeners ---

// Listen for selection changes in Figma
figma.on("selectionchange", () => {
    updateAndNotifyUI();
});

// --- Message Handling from UI ---
figma.ui.onmessage = async (msg) => {
    console.log("Message received from ui.html:", msg.type, msg); // Log incoming messages

    switch (msg.type) {
        // --- UI Requesting Initial State After Login ---
        case "request-initial-selection":
            await updateAndNotifyUI(); // Send current selection state
            break;

        // --- UI Requesting Context Prep (Triggered by Send Button in create/modify mode) ---
        case "request-ai-context":
            if (isProcessing) {
                 console.warn("Processing already in progress. Ignoring new request.");
                 // Optionally notify UI that it's busy
                 // figma.ui.postMessage({ type: 'status-update', text: 'Still processing previous request...', isLoading: true });
                 return;
            }
            isProcessing = true; // Set processing flag

            const { mode, frameId, userPrompt, elementInfo } = msg;

            // Validate frameId exists for create/modify
            if ((mode === 'create' || mode === 'modify') && !frameId) {
                figma.ui.postMessage({ type: 'backend-error', error: "Internal Error: Frame ID missing for create/modify context request." });
                isProcessing = false;
                return;
            }

            let targetFrame;
            if (frameId) {
                targetFrame = await figma.getNodeByIdAsync(frameId);
                if (!targetFrame || targetFrame.removed || targetFrame.type !== 'FRAME') {
                     figma.ui.postMessage({ type: 'backend-error', error: `Target frame (ID: ${frameId}) not found or invalid.` });
                     isProcessing = false;
                     return;
                }
            }

            // Prepare base context
            const context = {
                frameName: targetFrame ? targetFrame.name : null,
            };

            try {
                if (mode === "create") {
                    if (!targetFrame) throw new Error("Target frame not found for creation.");
                    if (targetFrame.children.length > 0) {
                        throw new Error(`Target frame "${targetFrame.name}" is not empty. Cannot create.`);
                    }

                    figma.ui.postMessage({ type: "status-update", text: `Preparing 'create' request...`, isLoading: true });
                    // Tell UI to proceed with backend call (text only)
                    figma.ui.postMessage({
                        type: "proceed-to-backend-create", // Updated type
                        userPrompt: userPrompt,
                        context: context,
                        targetFrameId: frameId, // ID needed for insertion later
                    });
                    // Keep isProcessing = true until creation success/error

                } else if (mode === "modify") {
                    if (!targetFrame) throw new Error("Target frame not found for modification.");
                    if (!elementInfo || !elementInfo.id) {
                        throw new Error("Internal Error: Missing element information for modification.");
                    }

                    const elementToModify = await figma.getNodeByIdAsync(elementInfo.id);
                    if (!elementToModify || elementToModify.removed) {
                        throw new Error(`Selected element (ID: ${elementInfo.id}) not found or removed. Please reselect.`);
                    }

                    // Export images
                    figma.ui.postMessage({ type: "status-update", text: `Exporting images for analysis...`, isLoading: true });
                    figma.notify(`⏳ Exporting images...`);

                    const exportSettings = { format: "PNG", constraint: { type: "SCALE", value: 1 } }; // Adjust if needed
                    const [framePngBytes, elementPngBytes] = await Promise.all([
                         targetFrame.exportAsync(exportSettings),
                         elementToModify.exportAsync(exportSettings)
                    ]);

                    // Tell UI to proceed with backend call (includes image data)
                    figma.ui.postMessage({
                        type: "proceed-to-backend-modify", // Updated type
                        framePngBytes: framePngBytes,
                        elementPngBytes: elementPngBytes,
                        userPrompt: userPrompt,
                        context: { // Send necessary context
                            frameName: context.frameName,
                            // Send original element details needed by backend/UI
                            // Ensure the elementInfo passed from UI is sufficient, or fetch fresh details
                            originalElement: {
                                id: elementToModify.id,
                                name: elementToModify.name,
                                type: elementToModify.type,
                                width: elementToModify.width,
                                height: elementToModify.height
                                // Add other relevant properties if backend needs them
                            }
                         }
                        // Note: We don't pass originalElementId separately here,
                        // it's nested in originalElement. The UI will extract it
                        // when sending the replace-element-with-svg message later.
                    });
                     // Keep isProcessing = true until modification success/error

                } else {
                     // Should not happen if UI sends correct mode with this message type
                     throw new Error(`Invalid mode '${mode}' received with 'request-ai-context'.`);
                }

            } catch (error) {
                 console.error("Error preparing AI context:", error);
                 const errorMsg = `Error preparing request: ${error.message || "Unknown error"}`;
                 isProcessing = false; // Reset flag on error
                 figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
                 figma.ui.postMessage({ type: "backend-error", error: errorMsg }); // Use backend-error type
            }
            break; // End of request-ai-context handler

        // --- UI Instructing to Insert Generated SVG ---
        case "finalize-creation":
            // This logic remains largely the same as it deals with Figma node creation
            const { svgContent: createSvgContent, targetFrameId: createTargetFrameId } = msg;

            if (!createSvgContent || typeof createSvgContent !== 'string' || !createSvgContent.trim().toLowerCase().startsWith('<svg')) {
                figma.ui.postMessage({ type: 'modification-error', error: 'Invalid SVG content received for creation.' });
                isProcessing = false;
                return;
            }

            const createTargetFrame = await figma.getNodeByIdAsync(createTargetFrameId);
            // Re-validate frame state before inserting
            if (!createTargetFrame || createTargetFrame.removed || createTargetFrame.type !== 'FRAME') {
                figma.ui.postMessage({ type: 'modification-error', error: `Target frame (ID: ${createTargetFrameId}) not found or invalid for insertion.` });
                isProcessing = false;
                return;
            }
            if (createTargetFrame.children.length > 0) {
                figma.ui.postMessage({ type: 'modification-error', error: `Target frame "${createTargetFrame.name}" is no longer empty. Creation aborted.` });
                figma.notify(`❌ Frame "${createTargetFrame.name}" is no longer empty.`, { error: true });
                isProcessing = false;
                return;
            }

            figma.ui.postMessage({ type: 'status-update', text: 'Importing generated SVG...', isLoading: true });
            figma.notify('⏳ Importing generated SVG...');

            try {
                const newNode = figma.createNodeFromSvg(createSvgContent);
                if (!newNode) throw new Error("Figma importer created a null node.");

                newNode.name = "AI Generated Design";
                createTargetFrame.appendChild(newNode);

                // Optional: Center the new node (adjust positioning as needed)
                newNode.x = createTargetFrame.width / 2 - newNode.width / 2;
                newNode.y = createTargetFrame.height / 2 - newNode.height / 2;

                console.log(`Successfully added node ${newNode.id} to frame ${createTargetFrameId}`);
                figma.currentPage.selection = [newNode]; // Select the new node
                figma.viewport.scrollAndZoomIntoView([newNode]);
                figma.notify('✅ New design generated successfully!');
                isProcessing = false; // Reset flag on success
                figma.ui.postMessage({ type: 'creation-success' }); // Notify UI

            } catch (error) {
                console.error("Error creating node from SVG or inserting:", error);
                const errorMsg = `SVG Import/Insertion Error: ${error.message || 'Unknown error'}`;
                isProcessing = false; // Reset flag on error
                figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
                figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
            }
            break; // End of finalize-creation handler

        // --- UI Instructing to Replace Element with SVG ---
        case "replace-element-with-svg":
            // This logic also remains largely the same
            const { svgContent: modifySvgContent, originalElementId } = msg;

            if (!modifySvgContent || typeof modifySvgContent !== 'string' || !modifySvgContent.trim().toLowerCase().startsWith('<svg')) {
                figma.ui.postMessage({ type: 'modification-error', error: 'Invalid SVG content received for replacement.' });
                isProcessing = false;
                return;
            }
            if (!originalElementId) {
                figma.ui.postMessage({ type: 'modification-error', error: 'Internal Error: Missing original element ID for replacement.' });
                isProcessing = false;
                return;
            }

            const originalElement = await figma.getNodeByIdAsync(originalElementId);
            // Re-validate element state
            if (!originalElement || originalElement.removed) {
                const errorMsg = `Original element (ID: ${originalElementId}) not found or was removed. Cannot replace.`;
                console.error(errorMsg);
                figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
                figma.notify(errorMsg, { error: true });
                isProcessing = false;
                return;
            }
            const parent = originalElement.parent;
            if (!parent || parent.type === "PAGE") {
                 const errorMsg = `Cannot replace top-level elements or elements without a valid parent.`;
                 console.error(errorMsg);
                 figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
                 figma.notify(errorMsg, { error: true });
                 isProcessing = false;
                 return;
            }

            figma.ui.postMessage({ type: 'status-update', text: 'Importing modified SVG...', isLoading: true });
            figma.notify('⏳ Importing modified SVG...');

            let newNode = null; // Declare here for potential cleanup
            try {
                newNode = figma.createNodeFromSvg(modifySvgContent);
                if (!newNode) throw new Error('Figma importer created a null node from the element SVG.');

                newNode.name = `${originalElement.name} (AI Modified)`;

                // Replacement logic: Insert new node, copy properties, remove old
                const index = parent.children.indexOf(originalElement);
                if (index === -1) throw new Error("Could not find original element in its parent's children list.");

                // Copy essential properties BEFORE removing original
                const originalX = originalElement.x;
                const originalY = originalElement.y;
                const originalWidth = originalElement.width;
                const originalHeight = originalElement.height;
                const originalConstraints = originalElement.constraints; // Copy constraints

                // Insert the new node near the original one
                parent.insertChild(index, newNode); // Insert at the same index

                // Apply position, size, and constraints
                newNode.x = originalX;
                newNode.y = originalY;
                 try {
                     if (newNode.resize) {
                         newNode.resize(originalWidth, originalHeight);
                     } else {
                          console.warn(`Node type ${newNode.type} might not support resize(). Size might not match exactly.`);
                     }
                } catch (resizeError) {
                      console.warn(`Could not resize new node: ${resizeError.message}. Size might not match.`);
                }
                try {
                     if (originalConstraints && newNode.constraints) {
                         newNode.constraints = originalConstraints;
                     }
                } catch (constraintError) {
                     console.warn(`Could not apply constraints: ${constraintError.message}`);
                }

                originalElement.remove(); // Remove original element AFTER new one is set up

                console.log(`Successfully replaced element ${originalElementId} with new node: ${newNode.id}`);
                figma.currentPage.selection = [newNode];
                figma.viewport.scrollAndZoomIntoView([newNode]);
                isProcessing = false; // Reset flag on success
                figma.notify('✅ Element successfully modified!');
                figma.ui.postMessage({ type: 'modification-success' }); // Signal success to UI

            } catch (error) {
                console.error('Error creating node from SVG or replacing element:', error);
                const errorMsg = `Element SVG Import/Replacement Error: ${error.message || 'Unknown error'}`;
                isProcessing = false; // Reset flag on error
                figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
                figma.ui.postMessage({ type: 'modification-error', error: errorMsg });

                // Cleanup: Remove the partially added node if it exists and wasn't placed correctly
                if (newNode && !newNode.removed && newNode.parent !== parent) {
                     try { newNode.remove(); } catch (cleanupError) { /* ignore */ }
                }
            }
            break; // End of replace-element-with-svg handler

        // --- Error reported from UI or Backend (forwarded by UI) ---
        case "modification-error": // Keep this for errors originating in UI or final Figma step
        case "backend-error":      // Use this for errors from the backend fetch/processing
            console.error(`Error reported from UI (${msg.type}):`, msg.error);
            // Only notify if it's a new error, not just confirming a code.js initiated error
             if (!isProcessing) { // Avoid double notification if code.js already notified
                 figma.notify(`❌ Error: ${msg.error}`, { error: true, timeout: 4000 });
             }
            isProcessing = false; // Ensure processing flag is reset
            break;

        // --- Catch Unknown Messages ---
        default:
            console.warn("Unknown message type received from UI:", msg.type);
            isProcessing = false; // Consider if unknown messages should reset the flag
            break;
    }
};

// --- Initial Load ---
console.log("Figma AI Design Assistant plugin code (OAuth Backend Version) loaded.");
// Don't clear selection here, let the UI request initial state after checking auth
// figma.currentPage.selection = [];
// Trigger initial check in case something is already selected when plugin loads
// The UI will request this explicitly after checking auth, so this might be redundant
// updateAndNotifyUI();