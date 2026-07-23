# Provider configuration

The CLI loads JSON from `--config`, then `IMAGEN_CONFIG_PATH`, then `~/.config/imagen/providers.json`.
Start from `providers.example.json`; editor validation is available through `providers.schema.json`.

## Complete shape

```json
{
  "default_provider": "primary",
  "providers": {
    "primary": {
      "adapter": "openai_images",
      "url": "https://images.example/v1",
      "api_key_env": "IMAGE_API_KEY",
      "default_model": "image-model-v1",
      "models": ["image-model-v1", "image-model-pro"],
      "timeout": 120,
      "defaults": {"quality": "high", "output_format": "png"},
      "supported_params": ["size", "quality", "background", "output_format"],
      "extra_headers": {"X-Client-Name": "imagen-skill"}
    }
  }
}
```

- `default_provider` must name an entry in `providers`. It may be omitted for a single provider.
- `url` must be an HTTP(S) API base URL. Displayed URLs are stripped of userinfo, query, and fragment data.
- Define exactly one credential field: `api_key_env` names an environment variable (recommended), while `api_key` stores the key directly.
- A direct `api_key` is never included in CLI output. Keep that config private, restrict its file permissions, and never commit it. Do not put secrets in `extra_headers`.
- `models` contains real API model IDs. `default_model` must occur in that array and may be omitted when it has one item.
- `timeout` is an optional positive number of seconds.
- `defaults` explicitly enables optional request parameters for that provider.
- `supported_params`, when present, rejects unsupported CLI/job parameters before a request.
- `extra_headers` supplies non-secret static headers.
- `adapter` selects a protocol implementation: `openai_images` uses OpenAI-compatible Images endpoints, while `google_generate_content` uses Gemini's native REST `generateContent` method. Other protocols require another `ImageProviderAdapter` implementation.

Optional parameter names are `size`, `quality`, `background`, `output_format`, `output_compression`, `moderation`, `input_fidelity`, `aspect_ratio`, and `image_size`. The CLI only sends an optional parameter when it appears in provider `defaults`, a CLI flag, or a batch job override. See `references/google-generate-content.md` for Google-specific constraints.

The OpenAI-compatible adapter uses the CLI retry loop and disables SDK retries (`max_retries=0`) to avoid multiplied attempts.

## Selection

Provider: `--provider` > `default_provider` > sole provider.
Model: `--model` > provider `default_model` > sole model.

Environment-backed credential (recommended):

```json
{"api_key_env": "IMAGE_API_KEY"}
```

```bash
export IMAGE_API_KEY="..."
```

Direct credential (supported, less safe):

```json
{"api_key": "sk-..."}
```

Then validate without revealing either credential:

```bash
python scripts/imagen.py config-check
python scripts/imagen.py providers
```

`config-check` validates every provider and fails if any referenced environment variable is unset. `providers` reports `credential_source` and key status without printing secret values.
