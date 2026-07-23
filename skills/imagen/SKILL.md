---
name: "imagen"
description: "Generate or edit raster images through configured image API providers. Use for photos, illustrations, textures, sprites, mockups, and transparent bitmap cutouts; do not use for true SVG/vector or code-native assets."
license: "Apache-2.0"
---

# Imagen Skill

Generate and edit bitmap images with the bundled `scripts/imagen.py` CLI.

## Core rules

- Always use `scripts/imagen.py`; do not create one-off SDK runners.
- Use `generate` for new images, `edit` when preserving and changing an existing image, and `generate-batch` for distinct prompts/assets.
- Treat style/composition reference images as references, not edit targets, unless the user asks to modify them.
- Load provider configuration from `--config`, `IMAGEN_CONFIG_PATH`, or `~/.config/imagen/providers.json` in that order.
- Use the configured default provider/model unless the user requests `--provider` or `--model`.
- Prefer `api_key_env` so secrets stay outside provider JSON. Direct `api_key` storage is supported only when explicitly requested; never print or commit direct keys, and keep the local config private.
- Do not automatically retry on another provider; explicit switching avoids duplicate requests and costs.
- `--dry-run` performs validation and request planning without network calls, directories, or file writes.
- Never overwrite existing output unless replacement was requested; use `--force` only in that case.
- This skill outputs raster files. Requests requiring real SVG/vector source are out of scope; “vector-like” describes only a raster visual style.

## Decision flow

1. Confirm that generated raster output is appropriate.
2. Choose `generate`, `edit`, or `generate-batch`.
3. Label each input image as edit target, mask, style reference, or compositing input.
4. Gather the prompt, exact text, constraints, destination, and requested provider/model.
5. Read `references/cli.md` and `references/provider-config.md` for execution details.
6. Normalize the prompt without inventing extra objects, brands, slogans, palettes, or story beats.
7. For edits, state `change only X; keep Y unchanged` and repeat critical invariants.
8. Run the bundled CLI, inspect the result, and iterate with one targeted change at a time.
9. Report the provider, model, saved path, and final prompt.

## Prompt essentials

Use only useful lines:

```text
Use case: <taxonomy slug>
Asset type: <intended use>
Primary request: <request>
Input images: <Image 1: role; Image 2: role>
Scene/backdrop: <environment>
Subject: <main subject>
Style/medium: <photo/illustration/3D/vector-like raster graphic>
Composition/framing: <view and layout>
Lighting/mood: <lighting and mood>
Color palette: <palette>
Materials/textures: <details>
Text (verbatim): "<exact text>"
Constraints: <must preserve/avoid>
Avoid: <negative constraints>
```

If the user's prompt is detailed, structure it without adding creative requirements. If it is generic, add only composition, intended-use, layout, or scene details that materially help.

Taxonomy, prompting principles, and complete recipes live in:

- `references/prompting.md`
- `references/sample-prompts.md`

## Output and dependencies

- Temporary batch JSONL: `tmp/imagen/`
- Default final location: `output/imagen/`
- Keep project-bound final assets inside the workspace.
- From this Skill directory, install dependencies with `python -m pip install '.[downscale]'` (or the repository's normal package workflow) after user approval.
- From this Skill directory, run tests with `python -m unittest discover -s tests -v`.

## Reference map

- `references/cli.md`: commands, flags, batch format, output handling, and cross-Agent invocation.
- `references/provider-config.md`: the single provider configuration reference.
- `references/image-api.md`: adapter protocol and API parameter surface.
- `references/google-generate-content.md`: Google Gemini `generateContent` configuration, mapping, and limits.
- `references/prompting.md`: structure, specificity, invariants, and iteration.
- `references/sample-prompts.md`: copy/paste recipes.
- `references/codex-network.md`: Codex-specific network and sandbox notes.
- `providers.example.json` / `providers.schema.json`: machine-readable example and schema.
