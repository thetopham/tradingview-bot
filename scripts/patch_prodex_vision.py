"""Patch the installed ProDex 0.5.1 agent node to pass n8n binary images to Codex.

The ProDex Chat Model + Basic LLM Chain drops image_url blocks before Codex. This
patch adds an optional binary-image input to ProDex's standalone Run Agent node.
It leaves the existing Chat Model unchanged. Back up the installed JS first.
"""

import argparse
from pathlib import Path


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(f"Expected one ProDex 0.5.1 patch anchor; found {count}")
    return text.replace(old, new, 1)


def patch(text: str) -> str:
    if "name: 'useInputImage'" in text:
        return text
    text = replace_once(
        text,
        'const n8n_workflow_1 = require("n8n-workflow");',
        'const n8n_workflow_1 = require("n8n-workflow");\n'
        'const fsPromises = require("node:fs/promises");\n'
        'const nodeOs = require("node:os");\n'
        'const nodePath = require("node:path");',
    )
    text = replace_once(
        text,
        "            {\n                displayName: 'Model',\n                name: 'model',",
        """            {
                displayName: 'Use Input Image',
                name: 'useInputImage',
                type: 'boolean',
                default: false,
                description: 'Send a binary image on the incoming item to Codex as a local image',
                displayOptions: { show: { operation: AGENT_OPERATIONS } },
            },
            {
                displayName: 'Image Binary Property',
                name: 'imageBinaryProperty',
                type: 'string',
                default: 'data',
                displayOptions: { show: { operation: AGENT_OPERATIONS, useInputImage: [true] } },
            },
            {
                displayName: 'Model',
                name: 'model',""",
    )
    old = """                    const result = await (0, runAgent_1.runCodexAgent)({
                        prompt: builtPrompt.prompt,"""
    new = """                    let result;
                    let imageIncluded = false;
                    let imageDirectory;
                    try {
                        let agentPrompt = builtPrompt.prompt;
                        const useInputImage = this.getNodeParameter('useInputImage', itemIndex, false);
                        if (useInputImage) {
                            const imageProperty = this.getNodeParameter('imageBinaryProperty', itemIndex, 'data');
                            const binary = items[itemIndex].binary?.[imageProperty];
                            if (binary) {
                                const mimeType = binary.mimeType;
                                const extensions = {
                                    'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
                                };
                                const extension = extensions[mimeType];
                                if (!extension) {
                                    throw new Error(`Unsupported ProDex image MIME type: ${mimeType}`);
                                }
                                const bytes = await this.helpers.getBinaryDataBuffer(itemIndex, imageProperty);
                                if (bytes.length > 10 * 1024 * 1024) {
                                    throw new Error('ProDex input image exceeds 10 MiB');
                                }
                                imageDirectory = await fsPromises.mkdtemp(nodePath.join(nodeOs.tmpdir(), 'prodex-image-'));
                                const imagePath = nodePath.join(imageDirectory, `chart${extension}`);
                                await fsPromises.writeFile(imagePath, bytes, { mode: 0o600 });
                                agentPrompt = [
                                    { type: 'text', text: builtPrompt.prompt },
                                    { type: 'local_image', path: imagePath },
                                ];
                                imageIncluded = true;
                            }
                            else {
                                this.logger.warn(`ProDex image property '${imageProperty}' is missing; using numeric prompt only`);
                            }
                        }
                        result = await (0, runAgent_1.runCodexAgent)({
                        prompt: agentPrompt,"""
    text = replace_once(text, old, new)
    text = replace_once(
        text,
        """                        additionalDirectories: builtPrompt.additionalDirectories,
                    });
                    if (threadMode === 'continue'""",
        """                        additionalDirectories: builtPrompt.additionalDirectories,
                        });
                    }
                    finally {
                        if (imageDirectory) {
                            await fsPromises.rm(imageDirectory, { recursive: true, force: true });
                        }
                    }
                    if (threadMode === 'continue'""",
    )
    text = replace_once(
        text,
        """                            output: result.output,
                            threadId: result.threadId,""",
        """                            output: result.output,
                            imageIncluded,
                            threadId: result.threadId,""",
    )
    return text


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    before = args.source.read_text(encoding='utf-8')
    after = patch(before)
    args.output.write_text(after, encoding='utf-8')
    print('ProDex image support:', 'already present' if before == after else 'patched')
