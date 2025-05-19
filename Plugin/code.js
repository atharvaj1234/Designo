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
    if (!current) return null;
    if (current.type === "FRAME" && current.parent && current.parent.type === "PAGE") {
        return current;
    }
    let parent = current.parent;
    while (parent) {
        if (parent.type === "FRAME" && parent.parent && parent.parent.type === "PAGE") {
            return parent;
        }
        if (parent.type === "PAGE") {
            return null;
        }
        parent = parent.parent;
    }
    return null;
}


// --- Selection Change Handler ---
figma.on("selectionchange", async () => {
    if (isProcessing) {
         // console.log("Selection change ignored during processing."); // Keep this quiet unless debugging
         return;
    }
    const selection = figma.currentPage.selection;
    let mode = 'answer';
    let frameId = null;
    let frameName = null;
    let elementInfo = null;
    originalSelectedNodeId = null;

    if (selection.length !== 1) {
        figma.ui.postMessage({ type: "selection-invalid", reason: "Please select exactly one item." });
        lastNotifiedFrameId = null;
        lastNotifiedMode = 'answer';
        return;
    }

    const selectedNode = selection[0];
    if (selectedNode.type === "PAGE") {
         figma.ui.postMessage({ type: "selection-invalid", reason: "Please select a frame or an element, not the page." });
         lastNotifiedFrameId = null;
         lastNotifiedMode = 'answer';
         return;
    }

    const targetFrame = findTopLevelFrame(selectedNode);

    if (!targetFrame) {
         if (selectedNode.type === "FRAME" && selectedNode.parent && selectedNode.parent.type === "PAGE") {
             if (selectedNode.children.length === 0) {
                 mode = "create";
                 frameId = selectedNode.id;
                 frameName = selectedNode.name;
             } else {
                 figma.ui.postMessage({ type: "selection-invalid", reason: "Select element *inside* frame to modify, or an *empty* frame to create." });
                 lastNotifiedFrameId = null;
                 lastNotifiedMode = 'answer';
                 return;
             }
         } else {
             figma.ui.postMessage({ type: "selection-invalid", reason: "Selected item must be within a top-level frame." });
             lastNotifiedFrameId = null;
             lastNotifiedMode = 'answer';
             return;
         }

    } else {
        frameId = targetFrame.id;
        frameName = targetFrame.name;

        if (selectedNode.id === targetFrame.id && targetFrame.children.length === 0) {
            mode = "create";
        } else if (selectedNode.id !== targetFrame.id && selectedNode.parent) {
            mode = "modify";
            originalSelectedNodeId = selectedNode.id;
            elementInfo = {
                id: selectedNode.id,
                name: selectedNode.name,
                type: selectedNode.type,
                width: selectedNode.width,
                height: selectedNode.height,
            };
        } else if (selectedNode.id === targetFrame.id && targetFrame.children.length > 0) {
             figma.ui.postMessage({ type: "selection-invalid", reason: "Select element *inside* frame to modify, or an *empty* frame to create." });
             lastNotifiedFrameId = null;
             lastNotifiedMode = 'answer';
             return;
         } else {
             figma.ui.postMessage({ type: "selection-invalid", reason: "Invalid selection. Ensure item is in a top-level frame." });
             lastNotifiedFrameId = null;
             lastNotifiedMode = 'answer';
             return;
        }
    }

    const currentElementInfoJson = elementInfo ? JSON.stringify(elementInfo) : null;
    // This comparison against figma.ui state is not reliable. Better to just always send the update.
    // if (frameId !== lastNotifiedFrameId || mode !== lastNotifiedMode || (mode === 'modify' && currentElementInfoJson !== lastNotifiedElementInfoJson)) { // Remove this if block for simplicity
        lastNotifiedFrameId = frameId;
        lastNotifiedMode = mode;

        figma.ui.postMessage({
            type: "selection-update",
            mode: mode,
            frameId: frameId,
            frameName: frameName,
            element: elementInfo,
        });
    // } // End if block
});

// --- Message Handling from UI ---
figma.ui.onmessage = async (msg) => {
    console.log("Message received from ui.html:", msg.type);

    // --- Request from UI to START AI Generation (Initial trigger after auth/prompt) ---
    if (msg.type === "request-ai-generation") {
        isProcessing = true; // Set processing flag
        const { mode, frameId, userPrompt, elementInfo } = msg;

        try {
            figma.ui.postMessage({
                type: "status-update",
                text: `Preparing "${mode}" request...`,
                isLoading: true,
            });

            const context = { frameName: null };

            let targetFrame = null;
            if (frameId) {
                targetFrame = await figma.getNodeByIdAsync(frameId);
                if (!targetFrame || targetFrame.removed || targetFrame.type !== 'FRAME') {
                    const errorMsg = `Target frame (ID: ${frameId}) not found or invalid. Please reselect.`;
                    console.error(errorMsg);
                    figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
                    isProcessing = false; return;
                }
                context.frameName = targetFrame.name;
            } else if (mode !== 'answer') {
                 const errorMsg = `Internal Error: Frame ID is missing for mode "${mode}". Please reselect.`;
                 console.error(errorMsg);
                 figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
                 isProcessing = false; return;
            }


            if (mode === "modify") {
                if (!elementInfo || !elementInfo.id) {
                    const errorMsg = "Internal Error: Missing element information for modification. Please reselect.";
                    console.error(errorMsg);
                    figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                    isProcessing = false; return;
                }
                const elementToModify = await figma.getNodeByIdAsync(elementInfo.id);
                if (!elementToModify || elementToModify.removed) {
                    const errorMsg = `The selected element (ID: ${elementInfo.id}) seems to have been removed. Please reselect.`;
                    console.error(errorMsg);
                    figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                    isProcessing = false; return;
                }

                context["elementInfo"] = elementInfo;

                figma.ui.postMessage({
                    type: "status-update",
                    text: `Exporting frame "${targetFrame.name}" and element for analysis...`,
                    isLoading: true,
                });
                figma.notify(`⏳ Exporting frame "${targetFrame.name}"...`);

                try {
                    const exportSettings = { format: "PNG", constraint: { type: "SCALE", value: 1 } };
                    const [framePngBytes, elementPngBytes] = await Promise.all([
                         targetFrame.exportAsync(exportSettings),
                         elementToModify.exportAsync(exportSettings)
                    ]);

                    figma.ui.postMessage({
                        type: "proceed-to-backend-vision",
                        framePngBytes: framePngBytes,
                        elementPngBytes: elementPngBytes,
                        userPrompt: userPrompt,
                        context: context,
                        originalElement: elementInfo,
                    });
                    // isProcessing flag reset happens when UI reports final result/error

                } catch (error) {
                    console.error("Error exporting frame/element:", error);
                    const errorMsg = `Export Error: ${error.message || "Unknown error"}`;
                    figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
                    figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                    isProcessing = false; return;
                }

            } else if (mode === "create") {
                 if (!targetFrame) {
                      const errorMsg = "Internal Error: Target frame not available for creation.";
                      console.error(errorMsg);
                      figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                      isProcessing = false; return;
                 }
                 if (targetFrame.children.length > 0) {
                      const errorMsg = `Target frame "${targetFrame.name}" is not empty. Cannot create new design.`;
                      console.error(errorMsg);
                      figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
                      figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                      isProcessing = false; return;
                 }

                figma.ui.postMessage({
                    type: "status-update",
                    text: `Preparing to generate design in "${targetFrame.name}"...`,
                    isLoading: true,
                });

                figma.ui.postMessage({
                    type: "proceed-to-backend-text",
                    userPrompt: userPrompt,
                    context: context,
                    targetFrameId: frameId,
                });
                 // isProcessing flag reset happens when UI reports final result/error


            } else if (mode === "answer") {
                 // For answer mode, code.js doesn't need to do anything special Figma API calls
                 // Just acknowledge and let the UI make the backend call.
                 console.log("Code.js received 'request-ai-generation' for 'answer' mode.");
                 // Tell UI to proceed by calling the backend with text data
                 figma.ui.postMessage({
                    type: "proceed-to-backend-text", // Use text flow message type
                    userPrompt: userPrompt,
                    context: context, // Pass context
                 });
                 isProcessing = false; // Code.js task is done for 'answer' mode.


            } else {
                const errorMsg = `Internal Error: Unknown mode "${mode}" in request-ai-generation message.`;
                console.error(errorMsg);
                figma.ui.postMessage({ type: "modification-error", error: errorMsg });
                isProcessing = false; return;
            }

        } catch (error) {
             console.error("Unexpected Error during Figma API preparation:", error);
             const errorMsg = `Preparation Error: ${error.message || "Unknown error"}`;
             isProcessing = false;
             figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
             figma.ui.postMessage({ type: "modification-error", error: errorMsg });
        } finally {
            // isProcessing is reset in catch or the subsequent UI message handlers
        }
    }

    else if (msg.type === "finalize-creation") {
        const { svgContent, targetFrameId } = msg;

        if (!svgContent || typeof svgContent !== "string" || !svgContent.trim().toLowerCase().startsWith("<svg")) {
            const errorMsg = "Invalid SVG content received from backend/UI for creation.";
            console.error(errorMsg);
            isProcessing = false;
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });
            return;
        }

        const targetFrame = await figma.getNodeByIdAsync(targetFrameId);
        if (!targetFrame || targetFrame.removed || targetFrame.type !== "FRAME") {
            const errorMsg = `Target frame (ID: ${targetFrameId}) not found or invalid for insertion. Please reselect.`;
             console.error(errorMsg);
            isProcessing = false;
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });
            return;
        }
        if (targetFrame.children.length > 0) {
            const errorMsg = `Target frame "${targetFrame.name}" is no longer empty. Creation aborted.`;
             console.error(errorMsg);
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });
            figma.notify(`❌ ${errorMsg}`, {
                error: true,
                timeout: 5000,
            });
            isProcessing = false;
            return;
        }

        figma.ui.postMessage({
            type: "status-update",
            text: "Importing generated SVG...",
            isLoading: true,
        });
        figma.notify("⏳ Importing generated SVG...");

        try {
            const newNode = figma.createNodeFromSvg(svgContent);

            if (!newNode) {
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
            isProcessing = false;
            figma.ui.postMessage({ type: "creation-success" });

        } catch (error) {
            console.error("Error creating node from SVG or inserting:", error);
            const errorMsg = `SVG Import/Insertion Error: ${error.message || "Unknown error"}`;
            isProcessing = false;
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });
        }
    }

    else if (msg.type === "replace-element-with-svg") {
        const { svgContent, originalElementId } = msg;

        if (!svgContent || typeof svgContent !== "string" || !svgContent.trim().toLowerCase().startsWith("<svg")) {
             const errorMsg = "Invalid SVG content received from backend/UI for replacement.";
            console.error(errorMsg);
            isProcessing = false;
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });
            return;
        }
        if (!originalElementId) {
             const errorMsg = "Internal Error: Missing original element ID for replacement.";
             console.error(errorMsg);
            isProcessing = false;
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });
            return;
        }

        const originalElement = await figma.getNodeByIdAsync(originalElementId);
        if (!originalElement || originalElement.removed) {
            const errorMsg = `Original element (ID: ${originalElementId}) not found or was removed. Cannot replace. Please reselect.`;
            console.error(errorMsg);
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });
            figma.notify(errorMsg, { error: true });
            isProcessing = false;
            return;
        }
        if (!originalElement.parent || originalElement.parent.type === "PAGE") {
            const errorMsg = `Cannot replace top-level elements directly.`;
            console.error(errorMsg);
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });
            figma.notify(errorMsg, { error: true });
            isProcessing = false;
            return;
        }

        figma.ui.postMessage({
            type: "status-update",
            text: "Importing modified element SVG...",
            isLoading: true,
        });
        figma.notify("⏳ Importing modified element SVG...");


        let newNode = null;
        try {
            newNode = figma.createNodeFromSvg(svgContent);

            if (!newNode) {
                 throw new Error(
                     "Figma importer failed to create a node from the element SVG content. The SVG might be invalid."
                 );
            }
            newNode.name = `${originalElement.name} (AI Modified)`;

            const parent = originalElement.parent;
            const index = parent.children.indexOf(originalElement);
            if (index === -1) {
                throw new Error(
                    "Internal Error: Could not find original element in its parent's children list."
                );
            }
            const originalX = originalElement.x;
            const originalY = originalElement.y;
            const originalWidth = originalElement.width;
            const originalHeight = originalElement.height;
            const originalConstraints = originalElement.constraints;

            parent.insertChild(index + 1, newNode);
            newNode.x = originalX;
            newNode.y = originalY;

            try {
                if (originalConstraints) {
                    newNode.constraints = originalConstraints;
                }
            } catch (constraintError) {
                console.warn(`Could not apply constraints: ${constraintError.message}`);
            }

            if (newNode.resize) {
                 if (newNode.width > 0 && newNode.height > 0) {
                    newNode.resize(originalWidth, originalHeight);
                 } else {
                     console.warn("New SVG node has zero dimensions, cannot resize to original.");
                 }
            } else {
                 console.warn("New SVG node does not support resize operation.");
            }

            originalElement.remove();
            console.log(
                `Successfully replaced element ${originalElementId} with new node: ${newNode.id}`
            );
            figma.currentPage.selection = [newNode];
            figma.viewport.scrollAndZoomIntoView([newNode]);
            isProcessing = false;
            figma.notify("✅ Element successfully modified!");
            figma.ui.postMessage({ type: "modification-success" });
        } catch (error) {
            console.error(
                "Error creating node from SVG or replacing element:",
                error
            );
            const errorMsg = `Element SVG Import/Replacement Error: ${error.message || "Unknown error"}`;
            isProcessing = false;
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: "modification-error", error: errorMsg });

            if (
                newNode &&
                !newNode.removed &&
                newNode.parent !== originalElement?.parent
            ) {
                try {
                    newNode.remove();
                } catch (cleanupError) {
                     console.warn("Cleanup failed for partially added node:", cleanupError);
                }
            }
        }
    }

     else if(msg.type === "backend-error" || msg.type === "modification-error"){
        console.error("Error reported from UI/Backend:", msg.error);
        figma.notify(`❌ Error: ${msg.error}`, { error: true, timeout: 5000 });
        isProcessing = false; // Ensure processing flag is reset
    }
     // Removed the redundant 'answer' case handler

    else {
        console.warn("Unknown message type received from UI:", msg.type);
        // Even if unknown, ensure processing flag is reset eventually
        isProcessing = false;
    }
};

// Trigger initial selection check on load
setTimeout(() => {
  figma.trigger('selectionchange');
}, 50);

console.log("Figma AI Design Assistant plugin code (Backend + Auth Version) loaded.");