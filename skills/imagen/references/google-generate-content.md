# Google Gemini `generateContent` image adapter

Official reference: [Gemini Generate Content API image generation](https://ai.google.dev/gemini-api/docs/generate-content/image-generation).
Google currently labels `generateContent` as a legacy API and recommends Interactions API for the newest features. This adapter intentionally implements `generateContent` because it is broadly documented and requested for this Skill.

## Provider configuration

```json
{
  "adapter": "google_generate_content",
  "url": "https://generativelanguage.googleapis.com/v1",
  "api_key_env": "GEMINI_API_KEY",
  "default_model": "gemini-3.1-flash-image",
  "models": [
    "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-image",
    "gemini-3-pro-image",
    "gemini-2.5-flash-image"
  ],
  "timeout": 180,
  "defaults": {
    "aspect_ratio": "1:1",
    "image_size": "1K"
  },
  "supported_params": ["aspect_ratio", "image_size"]
}
```

A direct `api_key` may replace `api_key_env`, following `references/provider-config.md`.

## REST mapping

The adapter sends:

```http
POST {url}/models/{model}:generateContent
x-goog-api-key: <key>
Content-Type: application/json
```

Text-to-image body:

```json
{
  "contents": [{
    "role": "user",
    "parts": [{"text": "Create an image..."}]
  }],
  "generationConfig": {
    "responseModalities": ["IMAGE"],
    "imageConfig": {
      "aspectRatio": "16:9",
      "imageSize": "2K"
    }
  }
}
```

For `edit`, every repeated `--image` becomes an `inline_data` part:

```json
{
  "inline_data": {
    "mime_type": "image/png",
    "data": "<base64>"
  }
}
```

Final, non-thought images are read from:

```text
candidates[].content.parts[].inlineData.data
```

The adapter ignores parts marked `thought: true`, validates `mimeType`, and never writes output unless the response contains exactly the expected number of final images.

## CLI options and constraints

```bash
python scripts/imagen.py generate \
  --provider google \
  --prompt "A clean product photograph" \
  --aspect-ratio 16:9 \
  --image-size 2K \
  --out output/imagen/product.png
```

- `--aspect-ratio`: `1:1`, `1:4`, `1:8`, `2:3`, `3:2`, `3:4`, `4:1`, `4:3`, `4:5`, `5:4`, `8:1`, `9:16`, `16:9`, or `21:9`. Exact model support varies.
- `--image-size`: `512`, `1K`, `2K`, or `4K`; uppercase `K` is required. `gemini-2.5-flash-image` does not accept this field.
- If no aspect ratio is supplied, Google matches an edit input image or otherwise defaults to a square image.
- If no image size is supplied, the model chooses its default. Gemini 3 models commonly default to 1K.
- This adapter requests image-only responses and supports `n=1`. Gemini does not provide the OpenAI Images API `n` parameter.
- Generated output is currently restricted to PNG in this CLI adapter.
- Text-and-image editing and multiple input images are supported. `--mask` is not supported.
- OpenAI-specific `size`, `quality`, `background`, `output_compression`, `moderation`, and `input_fidelity` options are rejected before a request.
- All generated Gemini images include a SynthID watermark according to Google documentation.

Batch generation uses the same endpoint once per job and retains the CLI's full-batch preflight, concurrency, retry, and atomic-output behavior.
