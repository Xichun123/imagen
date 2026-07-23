# CLI reference (`scripts/imagen.py`)

Use this bundled CLI for every image generation or editing request handled by the skill.

All operations are subcommands of the same CLI.

## What this CLI does
- `generate`: generate a new image from a prompt
- `edit`: edit one or more existing images
- `generate-batch`: run many generation jobs from a JSONL file
- `config-check`: validate every provider and referenced key status
- `providers` / `list-providers`: list providers, models, defaults, adapters, and key status

Real API calls require a provider config, a selected-provider credential (`api_key_env` or direct `api_key`), and network access. `--dry-run` allows a missing environment-backed key.

## Locate the CLI (works across Agents)

Resolve paths relative to the installed skill directory rather than assuming an Agent-specific home:

```bash
cd /path/to/installed/imagen
export IMAGEN_ROOT="$PWD"
export IMAGEN_CLI="$IMAGEN_ROOT/scripts/imagen.py"
```

Typical roots include a Pi/agent skills directory, a Codex skills directory, or this repository's `skills/imagen/` path. Use the path supplied by the active Agent/runtime. After approval, install dependencies from the Skill directory with `cd "$IMAGEN_ROOT" && python -m pip install '.[downscale]'`.

## Provider configuration

The complete and only human-maintained provider configuration reference is `references/provider-config.md`. Start from `providers.example.json`, validate editor input with `providers.schema.json`, and run:

```bash
python "$IMAGEN_CLI" config-check
python "$IMAGEN_CLI" providers
```

Never paste an API key into chat, commit it, or print it in logs. Cross-provider failover is intentionally not automatic.

## Quick start

Dry-run with the default provider (no `--provider` needed):

```bash
python "$IMAGEN_CLI" generate \
  --prompt "Test" \
  --out output/imagen/test.png \
  --dry-run
```

Notes:
- One-off dry-runs print the API payload and the computed output path(s).
- Repo-local finals should live under `output/imagen/`.

Generate with the default provider:

```bash
python "$IMAGEN_CLI" generate \
  --prompt "A cozy alpine cabin at dawn" \
  --size 1024x1024 \
  --out output/imagen/alpine-cabin.png
```

Switch to the backup provider:

```bash
python "$IMAGEN_CLI" generate \
  --provider backup \
  --prompt "A cozy alpine cabin at dawn" \
  --out output/imagen/alpine-cabin-backup.png
```

Select a real API model ID configured under that provider:

```bash
python "$IMAGEN_CLI" generate \
  --provider backup \
  --model backup-image-model-pro \
  --prompt "A cozy alpine cabin at dawn" \
  --out output/imagen/alpine-cabin-quality.png
```

Edit with the default provider:

```bash
python "$IMAGEN_CLI" edit \
  --image input.png \
  --prompt "Replace only the background with a warm sunset" \
  --out output/imagen/sunset-edit.png
```

## Guardrails
- Use the bundled CLI directly (`python "$IMAGEN_CLI" ...`) after activating the correct environment.
- Do **not** create one-off runners (for example `gen_images.py`) unless the user explicitly asks for a custom wrapper.
- **Never modify** `scripts/imagen.py`. If something is missing, ask the user before doing anything else.

## Defaults
- Provider and model follow the selection precedence in `references/provider-config.md`.
- Default local output path: `output/imagen/output.png`.
- Local filename planning falls back to PNG when no output format is sent.
- Optional API parameters such as size, quality, background, and output format are sent only when a CLI flag, provider default, or batch override enables them.
- Provider capability restrictions are enforced through `supported_params` when configured.

## Quality, input fidelity, and masks
These are CLI controls available where noted.

- `--quality` works with providers that expose the OpenAI Images quality parameter: `low|medium|high|auto`
- `--input-fidelity` is **edit-only** for compatible OpenAI Images providers and validated as `low|high`
- `--mask` is **edit-only** and is not supported by `google_generate_content`
- `--aspect-ratio` and `--image-size` configure Google Gemini image output; see `references/google-generate-content.md`

Example:

```bash
python "$IMAGEN_CLI" edit \
  --image input.png \
  --prompt "Change only the background" \
  --quality high \
  --input-fidelity high \
  --out output/imagen/background-edit.png
```

Mask notes:
- For multi-image edits, pass repeated `--image` flags. Their order is meaningful, so describe each image by index and role in the prompt.
- The CLI accepts a single `--mask`.
- Use a PNG mask with an alpha channel. Inputs and masks must be readable regular files no larger than 50MB.
- In the edit prompt, repeat invariants (`change only the background; keep the subject unchanged`) to reduce drift.

## Output handling
- Use `tmp/imagen/` for temporary JSONL inputs or scratch files.
- Use `output/imagen/` for final outputs.
- Reruns fail if a target file already exists unless you pass `--force`; duplicate targets are rejected even with `--force`.
- The complete batch is preflighted before any API call, and files are staged then atomically published.
- `--out-dir` changes one-off naming to `image_1.<ext>`, `image_2.<ext>`, and so on.
- Downscaled copies use the default suffix `-web` unless you override it.

## Common recipes

Generate with augmentation fields:

```bash
python "$IMAGEN_CLI" generate \
  --prompt "A minimal hero image of a ceramic coffee mug" \
  --use-case "product-mockup" \
  --style "clean product photography" \
  --composition "wide product shot with usable negative space for page copy" \
  --constraints "no logos, no text" \
  --out output/imagen/mug-hero.png
```

Generate + also write a downscaled copy for fast web loading:

```bash
python "$IMAGEN_CLI" generate \
  --prompt "A cozy alpine cabin at dawn" \
  --size 1024x1024 \
  --downscale-max-dim 1024 \
  --out output/imagen/alpine-cabin.png
```

Generate multiple prompts concurrently (async batch):

```bash
mkdir -p tmp/imagen output/imagen/batch
cat > tmp/imagen/prompts.jsonl << 'EOF'
{"prompt":"Cavernous hangar interior with a compact shuttle parked near the center","use_case":"stylized-concept","composition":"wide-angle, low-angle","lighting":"volumetric light rays through drifting fog","constraints":"no logos or trademarks; no watermark","size":"1536x1024"}
{"prompt":"Gray wolf in profile in a snowy forest","use_case":"photorealistic-natural","composition":"eye-level","constraints":"no logos or trademarks; no watermark","size":"1024x1024"}
EOF

python "$IMAGEN_CLI" generate-batch \
  --input tmp/imagen/prompts.jsonl \
  --out-dir output/imagen/batch \
  --concurrency 5

rm -f tmp/imagen/prompts.jsonl
```

Notes:
- `generate-batch` requires `--out-dir`.
- Use `--concurrency` to control parallelism (default `5`).
- Per-job overrides are supported in JSONL (for example `size`, `quality`, `background`, `output_format`, `output_compression`, `moderation`, `n`, `model`, `out`, and prompt-augmentation fields). A per-job `model` must be a real model ID listed under the command's selected provider.
- `--n` generates multiple variants for a single prompt; `generate-batch` is for many different prompts.
- In batch runs, per-job `out` is treated as a filename under `--out-dir`.

## CLI notes
- OpenAI Images `--size` values: `1024x1024`, `1536x1024`, `1024x1536`, or `auto`.
- Google uses `--aspect-ratio` and `--image-size` instead of `--size`.
- Transparent backgrounds require `output_format` to be `png` or `webp`.
- `--prompt-file`, `--output-compression`, `--moderation`, `--max-attempts`, `--fail-fast`, `--force`, and `--no-augment` are supported.
- Supported parameters and model behavior depend on the configured provider; use `--model` to select one of that provider's listed real model IDs.

## See also
- API parameter quick reference: `references/image-api.md`
- Prompt examples: `references/sample-prompts.md`
- Network/sandbox notes: `references/codex-network.md`
