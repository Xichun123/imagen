# Image API quick reference

These parameters describe the Image API surface used by `scripts/imagen.py`.

## Scope
- Live requests use the provider selected from the providers JSON configuration.
- Each provider supplies `url`, exactly one credential field (`api_key_env` or `api_key`), and a `models` array; details are documented only in `references/provider-config.md`.
- Provider IDs are arbitrary JSON object keys. Models are selected directly by their real API IDs.
- The provider can be any vendor, gateway, proxy, or self-hosted service implementing the endpoints and response structure below.
- Provider-specific APIs with unrelated paths, payloads, authentication, or response formats require an `ImageProviderAdapter` implementation and cannot be made compatible by changing the URL alone. The bundled adapter is `openai_images`.

## Endpoints
- Generate: `POST /v1/images/generations` (`client.images.generate(...)`)
- Edit: `POST /v1/images/edits` (`client.images.edit(...)`)

## Core parameters
- `prompt`: text prompt
- `model`: real provider-specific API model ID selected from the provider's `models` array
- `n`: number of images (1-10)
- `size`: `1024x1024`, `1536x1024`, `1024x1536`, or `auto`
- `quality`: `low`, `medium`, `high`, or `auto`
- `background`: output transparency behavior (`transparent`, `opaque`, or `auto`) for generated output; this is not the same thing as the prompt's visual scene/backdrop
- `output_format`: `png`, `jpeg`/`jpg`, or `webp`
- `output_compression`: 0-100 (jpeg/webp only)
- `moderation`: `auto` (default) or `low`

## Edit-specific parameters
- `image`: one or more input images; provider-specific count and size limits apply.
- `mask`: optional mask image
- `input_fidelity`: `low` (default) or `high`

Provider note for `input_fidelity`:
- Exact preservation behavior depends on the selected model and provider.
- Providers may reject parameters they do not implement.

Optional parameters are not sent by default merely because the CLI has local fallback behavior. They are included only through a CLI flag, provider `defaults`, or a batch override, and may be restricted by provider `supported_params`.

## Output
- `data[]` list with `b64_json` per image
- The number of returned items must exactly equal `n`; base64 is strictly validated before anything is written.
- The CLI stages complete files beside each target and atomically publishes them.

## Limits and notes
- Input images and masks must be under 50MB.
- Use the edits endpoint when the user requests changes to an existing image.
- Masking is prompt-guided; exact shapes are not guaranteed.
- Large sizes and high quality increase latency and cost.
- High `input_fidelity` can materially increase input token usage.
- If a request fails because the provider or selected model does not support an option, retry manually without that option.

## CLI controls
- `quality`, `input_fidelity`, explicit masks, `background`, `output_format`, and related parameters are exposed by `scripts/imagen.py`.
- See `references/cli.md` for command examples.
