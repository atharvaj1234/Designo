// =========================================================================
// AI Design Assistant - Figma Plugin Code (Backend Version)
// =========================================================================

figma.showUI(__html__, { width: 450, height: 550, title: "AI Design Assistant" }); // Adjusted height

// State variables (remain the same)
let lastNotifiedFrameId = null;
let lastNotifiedMode = null;
let originalSelectedNodeId = null; // Still needed for modify

// findTopLevelFrame function (remains the same)
function findTopLevelFrame(node) {
    let current = node;
    if (current.type === 'FRAME' && current.parent.type === 'PAGE') { return current; }
    let parent = current.parent;
    while (parent) {
        if (parent.type === 'FRAME' && parent.parent.type === 'PAGE') { return parent; }
        if (parent.type === 'PAGE') { return null; }
        parent = parent.parent;
    } return null;
}

// --- Selection Change Handler ---
figma.on('selectionchange', async () => {
    const selection = figma.currentPage.selection;
    let mode = null;
    let frameId = null;
    let frameName = null;
    let elementInfo = null;
    originalSelectedNodeId = null; // Reset selected ID

    if (selection.length !== 1) {
        figma.ui.postMessage({ type: 'selection-invalid', reason: 'Please select exactly one item.' });
        lastNotifiedFrameId = null; lastNotifiedMode = null;
        return;
    }

    const selectedNode = selection[0];
    const targetFrame = findTopLevelFrame(selectedNode);

    if (!targetFrame) {
        figma.ui.postMessage({ type: 'selection-invalid', reason: 'Selected item must be within a top-level frame.' });
        lastNotifiedFrameId = null; lastNotifiedMode = null;
        return;
    }

    frameId = targetFrame.id;
    frameName = targetFrame.name;

    const children = targetFrame.children;

    if (selectedNode.id === targetFrame.id && children.length === 0) {
        mode = 'create';
    }
    else if (selectedNode.id !== targetFrame.id && selectedNode.parent && selectedNode.parent.type !== 'PAGE') {
        mode = 'modify';
        originalSelectedNodeId = selectedNode.id; // Store ID for potential replacement
        elementInfo = {
            id: selectedNode.id,
            name: selectedNode.name,
            type: selectedNode.type,
            width: selectedNode.width,
            height: selectedNode.height
        };
    }
    else if (selectedNode.id === targetFrame.id && children.length > 0) {
        figma.ui.postMessage({ type: 'selection-invalid', reason: 'Select element *inside* frame to modify, or an *empty* frame to create.' });
        lastNotifiedFrameId = null; lastNotifiedMode = null;
        return;
    }
    else {
        figma.ui.postMessage({ type: 'selection-invalid', reason: 'Invalid selection. Ensure item is in a top-level frame.' });
        lastNotifiedFrameId = null; lastNotifiedMode = null;
        return;
    }

    // Send update only if relevant state changes (optional optimization)
    // if (frameId !== lastNotifiedFrameId || mode !== lastNotifiedMode) {
        lastNotifiedFrameId = frameId;
        lastNotifiedMode = mode;
        figma.ui.postMessage({
            type: 'selection-update',
            mode: mode,
            frameId: frameId,
            frameName: frameName,
            element: elementInfo // null if mode is 'create'
        });
    // }
});

// --- Message Handling from UI ---
figma.ui.onmessage = async (msg) => {
    console.log("Message received from ui.html:", msg.type); // Removed msg.mode as it's not always present

    // --- Request from UI to START AI Generation ---
    // This now triggers preparing data and sending a message BACK to UI
    // which will then call the backend.
    if (msg.type === 'request-ai-generation') {
        const { mode, frameId, userPrompt, elementInfo } = msg; // API Key removed

        const targetFrame = await figma.getNodeByIdAsync(frameId);
        if (!targetFrame || targetFrame.type !== 'FRAME' || targetFrame.removed) {
            figma.ui.postMessage({ type: 'modification-error', error: `Target frame (ID: ${frameId}) not found or invalid.` });
            return;
        }

        // Prepare context object for the backend
        const context = {
            frameName: targetFrame.name
        };
        
        if (mode === 'modify' && elementInfo) {
            context['elementInfo'] = elementInfo;
        }

        if (mode === 'modify') {
            if (!elementInfo || !elementInfo.id) {
                figma.ui.postMessage({ type: 'modification-error', error: 'Internal Error: Missing element information for modification.' });
                return;
            }
             const elementToModify = await figma.getNodeByIdAsync(elementInfo.id);
             if (!elementToModify || elementToModify.removed) {
                 figma.ui.postMessage({ type: 'modification-error', error: `The selected element (ID: ${elementInfo.id}) seems to have been removed. Please reselect.` });
                 return;
             }

            figma.ui.postMessage({ type: 'status-update', text: `Exporting frame "${targetFrame.name}" for analysis...`, isLoading: true });
            figma.notify(`⏳ Exporting frame "${targetFrame.name}"...`);
            try {
                // Export settings - adjust scale/format if needed by backend/AI model
                const exportSettings = { format: 'PNG', constraint: { type: 'SCALE', value: 1 } };
                const pngBytes = await targetFrame.exportAsync(exportSettings);

                // Tell UI to proceed by calling the backend with vision data
                figma.ui.postMessage({
                    type: 'proceed-to-backend-vision', // New message type
                    pngBytes: pngBytes, // Send raw bytes
                    userPrompt: userPrompt,
                    context: context, // Contains frameName and elementInfo
                    // We still need originalElementId for replacement later
                    originalElementId: elementInfo.id
                });

            } catch (error) {
                console.error('Error exporting frame:', error);
                const errorMsg = `Frame Export Error: ${error.message || 'Unknown error'}`;
                figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
                figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
            }
        }
        else if (mode === 'create') {
            if (targetFrame.children.length > 0) {
                figma.ui.postMessage({ type: 'modification-error', error: `Target frame "${targetFrame.name}" is not empty. Cannot create new design.` });
                return;
            }
            figma.ui.postMessage({ type: 'status-update', text: `Preparing to generate design...`, isLoading: true });
            // Tell UI to proceed by calling the backend with text data
            figma.ui.postMessage({
                type: 'proceed-to-backend-text', // New message type
                userPrompt: userPrompt,
                context: context, // Contains frameName
                targetFrameId: frameId // Pass frame ID for insertion later
            });
        } else {
            console.error("Unknown mode in request-ai-generation:", mode);
            figma.ui.postMessage({ type: 'modification-error', error: `Internal Error: Unknown mode "${mode}".` });
        }
    }

    // --- Request FROM UI (after successful backend call) to insert SVG ---
     else if (msg.type === 'finalize-creation') {
         const { svgContent, targetFrameId } = msg;

         if (!svgContent || typeof svgContent !== 'string' || !svgContent.trim().toLowerCase().startsWith('<svg')) {
             figma.ui.postMessage({ type: 'modification-error', error: 'Invalid SVG content received from backend/UI.' }); return;
         }

         const targetFrame = await figma.getNodeByIdAsync(targetFrameId);
         if (!targetFrame || targetFrame.removed || targetFrame.type !== 'FRAME') {
              figma.ui.postMessage({ type: 'modification-error', error: `Target frame (ID: ${targetFrameId}) not found or invalid for insertion.` }); return;
         }
         if (targetFrame.children.length > 0) { // Re-check emptiness
              figma.ui.postMessage({ type: 'modification-error', error: `Target frame "${targetFrame.name}" is no longer empty. Creation aborted.` });
              figma.notify(`❌ Frame "${targetFrame.name}" is no longer empty.`, { error: true });
              return;
         }

         figma.ui.postMessage({ type: 'status-update', text: 'Importing generated SVG...', isLoading: true });
         figma.notify('⏳ Importing generated SVG...');

         try {
            const newNode = figma.createNodeFromSvg(svgContent);
            if (!newNode) throw new Error("Figma importer created a null node.");

            // --- Scaling/Positioning Logic (same as before) ---
            let scale = 1;
            const framePadding = 20;
            const availableWidth = targetFrame.width - 2 * framePadding;
            const availableHeight = targetFrame.height - 2 * framePadding;

            if (newNode.width > 0 && newNode.height > 0 && (newNode.width > availableWidth || newNode.height > availableHeight)) {
                const scaleX = availableWidth / newNode.width;
                const scaleY = availableHeight / newNode.height;
                scale = Math.min(scaleX, scaleY);
            }

            newNode.name = "AI Generated Design";
            targetFrame.appendChild(newNode);

            if (scale < 1 && newNode.resize) {
                 newNode.resize(newNode.width * scale, newNode.height * scale);
            }

            newNode.x = targetFrame.width / 2 - newNode.width / 2;
            newNode.y = targetFrame.height / 2 - newNode.height / 2;
            // --- End Scaling/Positioning ---

            console.log(`Successfully added node ${newNode.id} to frame ${targetFrameId}`);
            figma.currentPage.selection = [newNode];
            figma.viewport.scrollAndZoomIntoView([newNode]);
            figma.notify('✅ New design generated successfully!');
            figma.ui.postMessage({ type: 'creation-success' }); // Final success to UI

         } catch (error) {
              console.error('Error creating node from SVG or inserting:', error);
              const errorMsg = `SVG Import/Insertion Error: ${error.message || 'Unknown error'}`;
              figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
              figma.ui.postMessage({ type: 'modification-error', error: errorMsg }); // Send error to UI
         }
    }

    // --- Request FROM UI (after successful backend call) to replace element ---
    else if (msg.type === 'replace-element-with-svg') {
        const { svgContent, originalElementId } = msg; // Get original ID from UI message

        if (!svgContent || typeof svgContent !== 'string' || !svgContent.trim().toLowerCase().startsWith('<svg')) {
             figma.ui.postMessage({ type: 'modification-error', error: 'Invalid SVG content received from backend/UI for replacement.' });
             return;
        }
        if (!originalElementId) {
             figma.ui.postMessage({ type: 'modification-error', error: 'Internal Error: Missing original element ID for replacement.' });
             return;
        }

        const originalElement = await figma.getNodeByIdAsync(originalElementId);
        if (!originalElement || originalElement.removed) {
            const errorMsg = `Original element (ID: ${originalElementId}) not found or was removed. Cannot replace.`;
            console.error(errorMsg);
            figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
            figma.notify(errorMsg, { error: true });
            return;
        }
        if (!originalElement.parent || originalElement.parent.type === 'PAGE') {
             const errorMsg = `Cannot replace top-level elements directly.`;
             console.error(errorMsg);
             figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
             figma.notify(errorMsg, { error: true });
             return;
        }

        figma.ui.postMessage({ type: 'status-update', text: 'Importing modified element SVG...', isLoading: true });
        figma.notify('⏳ Importing modified element SVG...');

        let newNode = null;
        try {
            newNode = figma.createNodeFromSvg(svgContent);
            if (!newNode) {
                throw new Error("Figma importer created a null node from the element SVG.");
            }
            newNode.name = `${originalElement.name} (AI Modified)`;

            // --- Replacement Logic (same as before) ---
            const parent = originalElement.parent;
            const index = parent.children.indexOf(originalElement);
            if (index === -1) {
                 throw new Error("Could not find original element in its parent's children list.");
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
                 if (originalConstraints) { newNode.constraints = originalConstraints; }
             } catch (constraintError) { console.warn(`Could not apply constraints: ${constraintError.message}`); }

            if (newNode.resize) {
                 newNode.resize(originalWidth, originalHeight);
            }

            originalElement.remove(); // Remove AFTER successful insert/position
            // --- End Replacement Logic ---

            console.log(`Successfully replaced element ${originalElementId} with new node: ${newNode.id}`);
            figma.currentPage.selection = [newNode];
            figma.viewport.scrollAndZoomIntoView([newNode]);
            figma.notify('✅ Element successfully modified!');
            figma.ui.postMessage({ type: 'modification-success' }); // Signal success to UI

        } catch (error) {
            console.error('Error creating node from SVG or replacing element:', error);
            const errorMsg = `Element SVG Import/Replacement Error: ${error.message || 'Unknown error'}`;
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: 'modification-error', error: errorMsg }); // Send error to UI

            // Cleanup partially added node if necessary
             if (newNode && !newNode.removed && newNode.parent !== originalElement.parent) {
                 try { newNode.remove(); } catch (cleanupError) { /* ignore */ }
             }
        }
    }

    // Handle generic error message from UI (e.g., if fetch fails)
    else if (msg.type === 'modification-error') {
        // Log it and potentially show a notification
        console.error("Error reported from UI:", msg.error);
        figma.notify(`❌ Error: ${msg.error}`, { error: true, timeout: 4000 });
        // No need to repost to UI, it already knows
    }
    else {
        console.warn("Unknown message type received from UI:", msg.type);
    }
};

// Trigger initial selection check on load
// figma.emit('selectionchange');
console.log("Figma AI Design Assistant plugin code (Backend Version) loaded.");