// =========================================================================
// AI Design Assistant - Figma Plugin Code
// Supports creating (with refinement) and modifying designs.
// =========================================================================

figma.showUI(__html__, { width: 450, height: 650, title: "AI Design Assistant" }); // Increased height slightly

let lastNotifiedFrameId = null;
let lastNotifiedMode = null;
let originalSelectedNodeId = null;

function findTopLevelFrame(node) { /* ... (same as before) ... */
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
    if (selection.length !== 1) {
        figma.ui.postMessage({ type: 'selection-invalid', reason: 'Please select exactly one item.' });
        lastNotifiedFrameId = null; lastNotifiedMode = null; originalSelectedNodeId = null; // Reset all
        return;
    }
    const selectedNode = selection[0];
    // Store the ID of the specifically selected node for potential replacement
    originalSelectedNodeId = selectedNode.id; // **** Store the selected node's ID ****

    const targetFrame = findTopLevelFrame(selectedNode);
    if (!targetFrame) {
        figma.ui.postMessage({ type: 'selection-invalid', reason: 'Selected item must be within a top-level frame.' });
        lastNotifiedFrameId = null; lastNotifiedMode = null; originalSelectedNodeId = null; // Reset all
        return;
    }

    let currentMode = null; let elementInfo = null;
    const children = targetFrame.children;

    if (selectedNode.id === targetFrame.id && children.length === 0) {
        currentMode = 'create';
        originalSelectedNodeId = null; // Cannot "replace" the frame itself in create mode
    }
    else if (selectedNode.id !== targetFrame.id && selectedNode.parent) {
        currentMode = 'modify';
        // Pass the selected element's details (including its ID)
        elementInfo = { id: selectedNode.id, name: selectedNode.name, type: selectedNode.type };
    }
    else if (selectedNode.id === targetFrame.id && children.length > 0) {
        figma.ui.postMessage({ type: 'selection-invalid', reason: 'Select element *inside* to modify, or an *empty* frame to create.' });
        lastNotifiedFrameId = null; lastNotifiedMode = null; originalSelectedNodeId = null; // Reset all
        return;
    }
    else {
        figma.ui.postMessage({ type: 'selection-invalid', reason: 'Invalid selection.' });
        lastNotifiedFrameId = null; lastNotifiedMode = null; originalSelectedNodeId = null; // Reset all
        return;
    }

    // if (targetFrame.id !== lastNotifiedFrameId || currentMode !== lastNotifiedMode) {
    //     lastNotifiedFrameId = targetFrame.id; lastNotifiedMode = currentMode;
        // Send elementInfo which contains the selected node's ID needed by UI
        figma.ui.postMessage({ type: 'selection-update', mode: currentMode, frameId: targetFrame.id, frameName: targetFrame.name, element: elementInfo });
    // }
});

// --- Message Handling from UI ---
figma.ui.onmessage = async (msg) => {
    console.log("Message received from ui.html:", msg.type, msg.mode);

    // --- Request to Initiate AI Generation (Unified Entry Point) ---
    if (msg.type === 'request-ai-generation') { /* ... (same as before, routes to specific proceed-with message) ... */
        const { mode, frameId, userPrompt, apiKey, elementInfo } = msg;
        const frameNode = await figma.getNodeByIdAsync(frameId);
        if (!frameNode || frameNode.type !== 'FRAME' || frameNode.removed) { /* ... error handling ... */ return; }

        if (mode === 'modify') {
            if (!elementInfo) { /* ... error handling ... */ return; }
            figma.ui.postMessage({ type: 'status-update', text: `Exporting frame "${frameNode.name}" for modification...`, isLoading: true });
            figma.notify(`⏳ Exporting frame "${frameNode.name}"...`);
            try {
                const exportSettings = { format: 'PNG', constraint: { type: 'SCALE', value: 1 } };
                const pngBytes = await frameNode.exportAsync(exportSettings);
                figma.ui.postMessage({ type: 'proceed-with-vision-ai', pngBytes: pngBytes, userPrompt: userPrompt, apiKey: apiKey, frameId: frameId, frameName: frameNode.name, elementInfo: elementInfo});
            } catch (error) { /* ... error handling ... */ }
        }
        else if (mode === 'create') {
             if (frameNode.children.length > 0) { /* ... error handling: frame not empty ... */ return; }
            figma.ui.postMessage({ type: 'status-update', text: `Preparing to generate new design in "${frameNode.name}"...`, isLoading: true });
            figma.ui.postMessage({ type: 'proceed-with-text-ai', userPrompt: userPrompt, apiKey: apiKey, frameId: frameId, frameName: frameNode.name });
        } else { /* ... error handling: unknown mode ... */ }
    }

    // --- Request to Convert SVG to PNG (for Create Preview) ---
    else if (msg.type === 'convert-svg-to-png') {
        const { svg } = msg;
        if (!svg) {
             figma.ui.postMessage({ type: 'png-conversion-result', error: 'No SVG content provided for preview.' });
             return;
        }

        let tempNode = null;
        try {
            console.log("Creating temporary node for PNG preview...");
            tempNode = figma.createNodeFromSvg(svg);
            if (!tempNode) {
                throw new Error("Failed to create temporary node from SVG.");
            }

            // Add node briefly to scene graph (required for export) off-screen? Doesn't seem necessary.
            // figma.currentPage.appendChild(tempNode); // Try without appending first
            // tempNode.visible = false; // Keep invisible if appended

            // Check if node has dimensions, otherwise export might fail/be empty
            if (tempNode.width === 0 || tempNode.height === 0) {
                // Attempt to resize based on SVG viewbox? Complex.
                // Or just default size? Let's try exporting anyway.
                console.warn("Temporary SVG node has zero width or height. Export might be empty.");
            }

            console.log("Exporting temporary node...");
            const exportSettings = { format: 'PNG', constraint: { type: 'SCALE', value: 1 } }; // Use scale 1 for preview
            const pngBytes = await tempNode.exportAsync(exportSettings);
            console.log("Export successful, sending bytes to UI.");

            figma.ui.postMessage({ type: 'png-conversion-result', pngBytes: pngBytes });

        } catch (error) {
            console.error('Error during SVG to PNG conversion:', error);
            figma.ui.postMessage({ type: 'png-conversion-result', error: `Preview generation failed: ${error.message}` });
        } finally {
            // CRUCIAL: Remove the temporary node whether export succeeded or failed
            if (tempNode && !tempNode.removed) {
                try {
                    tempNode.remove();
                    console.log("Temporary node removed.");
                } catch (removeError) {
                    console.error("Failed to remove temporary node:", removeError);
                    // Log error, but don't block UI response if export worked
                }
            }
        }
    }

    // --- Request to Finalize Creation (Insert Accepted SVG) ---
     else if (msg.type === 'finalize-creation') {
         const { svgContent, targetFrameId } = msg;

         if (!svgContent || typeof svgContent !== 'string' || !svgContent.trim().toLowerCase().startsWith('<svg')) {
             figma.ui.postMessage({ type: 'modification-error', error: 'Invalid SVG content received for finalization.' }); return;
         }

         const targetFrame = await figma.getNodeByIdAsync(targetFrameId);
         if (!targetFrame || targetFrame.removed || targetFrame.type !== 'FRAME') {
              figma.ui.postMessage({ type: 'modification-error', error: `Target frame (ID: ${targetFrameId}) not found or invalid for insertion.` }); return;
         }
        // Optional: Re-check if frame is still empty? Could have changed.
        // if (targetFrame.children.length > 0) { /* ... error handling ... */ return; }

         figma.ui.postMessage({ type: 'status-update', text: 'Importing final SVG...', isLoading: true });
         figma.notify('⏳ Importing final SVG...');

         try {
            const newNode = figma.createNodeFromSvg(svgContent);
            if (!newNode) throw new Error("Figma importer created a null node.");
            newNode.name = "AI Generated Design";

            targetFrame.appendChild(newNode); // Add into the frame

             if (newNode.width && newNode.height) { // Center if possible
                 newNode.x = targetFrame.width / 2 - newNode.width / 2;
                 newNode.y = targetFrame.height / 2 - newNode.height / 2;
             } else { newNode.x = 0; newNode.y = 0; }

             console.log(`Successfully added node ${newNode.id} to frame ${targetFrameId}`);
             figma.currentPage.selection = [newNode];
             figma.viewport.scrollAndZoomIntoView([newNode]);
             figma.notify('✅ New design generated successfully!');
             figma.ui.postMessage({ type: 'creation-success' }); // Final success

         } catch (error) { /* ... error handling ... */
              console.error('Error creating node from SVG or inserting into frame:', error);
              const errorMsg = `SVG Import/Insertion Error: ${error.message || 'Unknown error'}`;
              figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
              figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
         }
    }

    // --- Handle SVG Result for MODIFY (Replacement) ---
    else if (msg.type === 'replace-element-with-svg') {
        // **** Expect 'originalElementId' from UI ****
        const { svgContent, originalElementId } = msg;

        // Basic SVG validation
        if (!svgContent || typeof svgContent !== 'string' || !svgContent.trim().toLowerCase().startsWith('<svg')) {
            const errorMsg = 'Received empty or invalid SVG content for element replacement.';
            console.error(errorMsg);
            figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
            figma.notify(errorMsg, { error: true });
            return;
        }

        // **** Get the specific element to replace using the ID passed from UI ****
        const originalElement = await figma.getNodeByIdAsync(originalElementId);

        // Validate the original element
        if (!originalElement || originalElement.removed) {
            const errorMsg = `Original element (ID: ${originalElementId}) not found or was removed. Cannot replace.`;
            console.error(errorMsg);
            figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
            figma.notify(errorMsg, { error: true });
            return;
        }

        // Prevent replacing the page or top-level frames directly this way
        if (!originalElement.parent || originalElement.parent.type === 'PAGE') {
             const errorMsg = `Cannot replace top-level elements directly. Select an element *inside* a frame.`;
             console.error(errorMsg);
             figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
             figma.notify(errorMsg, { error: true });
             return;
        }

        originalElement.visible = false
        figma.ui.postMessage({ type: 'status-update', text: 'Importing modified element SVG...', isLoading: true });
        figma.notify('⏳ Importing modified element SVG...');

        try {
            // Create the new node from the SVG
            const newNode = figma.createNodeFromSvg(svgContent);
            if (!newNode) {
                throw new Error("Figma importer created a null node from the element SVG.");
            }

            newNode.name = `${originalElement.name} (AI Modified)`;

            // --- Replacement Logic for the Specific Element ---
            const parent = originalElement.parent;
            if (!parent || !('children' in parent)) {
                 throw new Error("Original element parent is invalid or inaccessible.");
            }
            const index = parent.children.indexOf(originalElement);
            if (index === -1) {
                 throw new Error("Could not find original element in its parent's children.");
            }
            const originalX = originalElement.x;
            const originalY = originalElement.y;
            const originalHeight = originalElement.height;
            const originalBottom = originalY + originalHeight;

            // Insert the new node at the same position
            parent.insertChild(index, newNode);
            newNode.x = originalX;
            newNode.y = originalY;

            // --- Reposition Subsequent Siblings ---
            // Calculate the vertical difference introduced by the new node
            const newHeight = newNode.height;
            const deltaY = newHeight - originalHeight;
            const tolerance = 0.01; // Tolerance for floating point comparison

            console.log(`Original Element Height: ${originalHeight}, New Element Height: ${newHeight}, DeltaY: ${deltaY}`);

            // Only proceed if there's a significant vertical change AND the parent is NOT using Autolayout
            if (Math.abs(deltaY) > tolerance) {
                // if (parent.layoutMode === "NONE") {
                    console.log(`Repositioning siblings below y=${originalBottom} by ${deltaY}px...`);
                    // Iterate through all children of the parent *after* insertion
                    const siblings = parent.children;
                    for (const sibling of siblings) {
                        // Skip the newly inserted node and the original node (which will be removed)
                        if (sibling.id === newNode.id || sibling.id === originalElement.id) {
                            continue;
                        }

                        // Check if the sibling's top edge is at or below the original element's bottom edge
                        if (sibling.y >= originalBottom - tolerance) { // Use tolerance here too
                             try {
                                 console.log(`  - Shifting ${sibling.name} (${sibling.id}) from y=${sibling.y} to y=${sibling.y + deltaY}`);
                                 sibling.y += deltaY;
                             } catch (repositionError) {
                                  // This might happen if the sibling's position is locked (e.g., constraints)
                                  console.warn(`    - Failed to reposition ${sibling.name}: ${repositionError.message}`);
                             }
                        }
                    }
                    console.log("Sibling repositioning attempt complete.");
                // } else {
                //     // Parent uses Autolayout - manual repositioning will likely fail or be overridden.
                //     console.warn(`Parent frame "${parent.name}" uses Autolayout (${parent.layoutMode}). Skipping manual repositioning of siblings.`);
                //     figma.notify(`Warning: Parent uses Autolayout. Elements below the modified one might not adjust automatically.`);
                //     // In Autolayout, the layout should ideally adjust based on the new node's size automatically,
                //     // assuming the Autolayout properties (spacing, padding) are set up correctly.
                //     // This manual shift is only for non-Autolayout frames.
                // }
            } else {
                 console.log("No significant height difference detected, skipping sibling repositioning.");
            }
            // --- End Repositioning ---
            console.log(`Successfully replaced element ${originalElementId} with new node: ${newNode.id}`);

            // Select the newly created node
            figma.currentPage.selection = [newNode];

            // Zoom the viewport to fit the newly created node
            figma.viewport.scrollAndZoomIntoView([newNode]);

            // Notify the UI and user of success
            figma.notify('✅ Element successfully modified!');
            figma.ui.postMessage({ type: 'modification-success' }); // Signal success to UI

        } catch (error) {
            console.error('Error creating node from SVG or replacing element:', error);
            const errorMsg = `Element SVG Import/Replacement Error: ${error.message || 'Unknown error'}`;
            figma.notify(`❌ ${errorMsg}`, { error: true, timeout: 5000 });
            figma.ui.postMessage({ type: 'modification-error', error: errorMsg });
        }
    }
    else {
        console.warn("Unknown message type received from UI:", msg.type);
    }
};

// Trigger initial selection check
console.log("Figma AI Design Assistant plugin code loaded.");
// figma.trigger('selectionchange');