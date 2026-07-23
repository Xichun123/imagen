#!/usr/bin/env python3
"""CLI for image generation or editing through preconfigured API providers.

Loads provider URL, model ID, and an environment-backed or direct API key from JSON config.
Uses a structured prompt augmentation workflow and accepts provider-specific model identifiers.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from dataclasses import dataclass
from io import BytesIO
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import Request, urlopen

DEFAULT_MODEL = None
DEFAULT_SIZE = "1024x1024"
DEFAULT_QUALITY = "auto"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_CONCURRENCY = 5
DEFAULT_DOWNSCALE_SUFFIX = "-web"
DEFAULT_OUTPUT_PATH = "output/imagen/output.png"
DEFAULT_CONFIG_PATH = "~/.config/imagen/providers.json"
CONFIG_PATH_ENV = "IMAGEN_CONFIG_PATH"

ALLOWED_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
ALLOWED_QUALITIES = {"low", "medium", "high", "auto"}
ALLOWED_BACKGROUNDS = {"transparent", "opaque", "auto", None}
ALLOWED_INPUT_FIDELITIES = {"low", "high", None}
OPTIONAL_API_PARAMS = {
    "size",
    "quality",
    "background",
    "output_format",
    "output_compression",
    "moderation",
    "input_fidelity",
    "aspect_ratio",
    "image_size",
}
SUPPORTED_ADAPTERS = {"openai_images", "google_generate_content"}
GOOGLE_ASPECT_RATIOS = {
    "1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3",
    "4:5", "5:4", "8:1", "9:16", "16:9", "21:9",
}
GOOGLE_IMAGE_SIZES = {"512", "1K", "2K", "4K"}

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_BATCH_JOBS = 500
EXIT_RUNTIME_ERROR = 1
EXIT_VALIDATION_ERROR = 2


class ImagenError(Exception):
    """A user-facing CLI error that does not abort internal control flow."""

    def __init__(self, message: str, exit_code: int = EXIT_RUNTIME_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


class BatchJobError(ImagenError):
    """A consistently formatted validation or runtime error for one batch job."""

    def __init__(self, job_index: int, phase: str, message: str, exit_code: int):
        self.job_index = job_index
        self.phase = phase
        super().__init__(f"Job {job_index} {phase} failed: {message}", exit_code)


def _die(message: str, code: int = EXIT_RUNTIME_ERROR) -> None:
    raise ImagenError(message, code)


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def _dependency_hint(package: str, *, upgrade: bool = False) -> str:
    command = f"uv pip install {'-U ' if upgrade else ''}{package}"
    return (
        "Activate the repo-selected environment first, then install it with "
        f"`{command}`. If this repo uses a local virtualenv, start with "
        "`source .venv/bin/activate`; otherwise use this repo's configured shared "
        "environment. If your project declares dependencies, prefer that project's normal "
        "`uv sync` flow."
    )


def _resolve_config_path(explicit_path: Optional[str]) -> Path:
    raw_path = explicit_path or os.getenv(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH
    return Path(raw_path).expanduser()


def _read_provider_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        _die(
            f"Provider config not found: {path}. Create it or pass --config / set {CONFIG_PATH_ENV}."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _die(f"Invalid provider config JSON in {path}: {exc}")
    except OSError as exc:
        _die(f"Could not read provider config {path}: {exc}")
    if not isinstance(data, dict):
        _die(f"Provider config must be a JSON object: {path}")
    return data


def _safe_url(url: str) -> str:
    """Strip credentials, query parameters, and fragments from displayed URLs."""
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    return urlunparse((parsed.scheme, hostname, parsed.path, "", "", ""))


def _read_provider_models(provider_name: str, provider: Dict[str, Any]) -> List[str]:
    raw_models = provider.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        _die(f"Provider '{provider_name}' must define a non-empty 'models' array.")

    models: List[str] = []
    for model_id in raw_models:
        if not isinstance(model_id, str) or not model_id.strip():
            _die(f"Provider '{provider_name}' contains an invalid API model ID.")
        model_id = model_id.strip()
        if model_id in models:
            _die(f"Provider '{provider_name}' contains duplicate model ID '{model_id}'.")
        models.append(model_id)
    return models


def _resolve_provider_model(
    provider_name: str,
    models: List[str],
    model_id: Any,
) -> str:
    if not isinstance(model_id, str) or not model_id.strip():
        _die(f"Choose a model with --model or set default_model for provider '{provider_name}'.")
    model_id = model_id.strip()
    if model_id not in models:
        available = ", ".join(sorted(models))
        _die(
            f"Unknown model '{model_id}' for provider '{provider_name}'. Available models: {available}"
        )
    return model_id


def _validate_provider_config(data: Dict[str, Any], config_path: Path) -> Dict[str, Any]:
    providers = data.get("providers")
    if not isinstance(providers, dict) or not providers:
        _die(f"Provider config must contain a non-empty 'providers' object: {config_path}")

    default_provider = data.get("default_provider")
    if default_provider is not None:
        if not isinstance(default_provider, str) or default_provider not in providers:
            _die("default_provider must reference a provider defined in 'providers'.")

    for provider_name, provider in providers.items():
        if not isinstance(provider_name, str) or not provider_name.strip():
            _die("Every provider ID must be a non-empty string.")
        if not isinstance(provider, dict):
            _die(f"Provider '{provider_name}' must be an object.")
        api_url = provider.get("url")
        if not isinstance(api_url, str) or not api_url.strip():
            _die(f"Provider '{provider_name}' must define a non-empty 'url'.")
        parsed_url = urlparse(api_url.strip())
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            _die(f"Provider '{provider_name}' has an invalid HTTP(S) URL: {_safe_url(api_url)}")
        api_key_env = provider.get("api_key_env")
        direct_api_key = provider.get("api_key")
        has_key_env = api_key_env is not None
        has_direct_key = direct_api_key is not None
        if has_key_env == has_direct_key:
            _die(
                f"Provider '{provider_name}' must define exactly one of 'api_key_env' or 'api_key'."
            )
        if has_key_env and (
            not isinstance(api_key_env, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env)
        ):
            _die(
                f"Provider '{provider_name}' must define a valid environment variable name in 'api_key_env'."
            )
        if has_direct_key and (
            not isinstance(direct_api_key, str) or not direct_api_key.strip()
        ):
            _die(f"Provider '{provider_name}' must define a non-empty string in 'api_key'.")
        models = _read_provider_models(provider_name, provider)
        default_model = provider.get("default_model")
        if default_model is not None and default_model not in models:
            _die(
                f"default_model '{default_model}' for provider '{provider_name}' "
                "must appear in its models array."
            )
        timeout = provider.get("timeout")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
        ):
            _die(f"Provider '{provider_name}' timeout must be a positive number.")
        adapter = provider.get("adapter", "openai_images")
        if adapter not in SUPPORTED_ADAPTERS:
            available = ", ".join(sorted(SUPPORTED_ADAPTERS))
            _die(f"Provider '{provider_name}' uses unsupported adapter '{adapter}'. Available: {available}")
        defaults = provider.get("defaults", {})
        if not isinstance(defaults, dict) or any(k not in OPTIONAL_API_PARAMS for k in defaults):
            _die(
                f"Provider '{provider_name}' defaults may only contain: "
                + ", ".join(sorted(OPTIONAL_API_PARAMS))
            )
        supported = provider.get("supported_params")
        if supported is not None and (
            not isinstance(supported, list)
            or any(not isinstance(k, str) or k not in OPTIONAL_API_PARAMS for k in supported)
            or len(set(supported)) != len(supported)
        ):
            _die(
                f"Provider '{provider_name}' supported_params must be a unique array drawn from: "
                + ", ".join(sorted(OPTIONAL_API_PARAMS))
            )
        if supported is not None and any(k not in supported for k in defaults):
            _die(f"Provider '{provider_name}' defaults contains an unsupported parameter.")
        try:
            default_payload = {"model": default_model or models[0], "n": 1, **defaults}
            _validate_generate_payload(default_payload)
            _get_adapter(adapter).validate_generate_payload(default_payload)
            default_format = _normalize_output_format(defaults.get("output_format"))
            _validate_transparency(defaults.get("background"), default_format)
            _validate_input_fidelity(defaults.get("input_fidelity"))
        except ImagenError as exc:
            _die(f"Provider '{provider_name}' has invalid defaults: {exc}")
        headers = provider.get("extra_headers", {})
        if not isinstance(headers, dict) or any(
            not isinstance(k, str) or not k.strip() or not isinstance(v, str)
            for k, v in headers.items()
        ):
            _die(f"Provider '{provider_name}' extra_headers must be a string-to-string object.")
    return providers


def _load_validated_config(path: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    data = _read_provider_config(path)
    return data, _validate_provider_config(data, path)


def _configure_provider(args: argparse.Namespace) -> None:
    config_path = _resolve_config_path(args.config)
    data, providers = _load_validated_config(config_path)
    provider_name = args.provider if args.provider is not None else data.get("default_provider")
    if provider_name is None and len(providers) == 1:
        provider_name = next(iter(providers))
    if not isinstance(provider_name, str) or not provider_name.strip():
        _die("Choose a provider with --provider or set default_provider in the config.")
    provider_name = provider_name.strip()

    provider = providers.get(provider_name)
    if not isinstance(provider, dict):
        available = ", ".join(sorted(str(name) for name in providers))
        _die(f"Unknown provider '{provider_name}'. Available providers: {available}")

    api_url = provider["url"].strip()
    models = _read_provider_models(provider_name, provider)
    model_id = args.model if args.model is not None else provider.get("default_model")
    if model_id is None and len(models) == 1:
        model_id = models[0]
    model_id = _resolve_provider_model(provider_name, models, model_id)
    api_key_env = provider.get("api_key_env")
    if api_key_env is not None:
        api_key = os.getenv(api_key_env, "").strip()
        credential_source = "environment"
        if not api_key:
            message = f"API key environment variable {api_key_env} for provider '{provider_name}' is not set."
            if args.dry_run:
                _warn(f"{message} Continuing because --dry-run does not call the API.")
            else:
                _die(message)
    else:
        api_key = provider["api_key"].strip()
        credential_source = "config"

    args.config_path = config_path
    args.provider = provider_name
    args.api_url = api_url
    args.safe_api_url = _safe_url(api_url)
    args.api_key = api_key
    args.api_key_env = api_key_env
    args.credential_source = credential_source
    args.provider_models = models
    args.provider_defaults = dict(provider.get("defaults", {}))
    args.supported_params = provider.get("supported_params")
    args.extra_headers = dict(provider.get("extra_headers", {}))
    args.timeout = provider.get("timeout")
    args.adapter = provider.get("adapter", "openai_images")
    args.model = model_id
    print(
        f"Using provider '{provider_name}' with model '{model_id}' from {config_path}.",
        file=sys.stderr,
    )


def _prepare_provider_params(args: argparse.Namespace) -> None:
    explicit = {
        "size": args.size,
        "quality": args.quality,
        "background": args.background,
        "output_format": args.output_format,
        "output_compression": args.output_compression,
        "moderation": args.moderation,
        "input_fidelity": getattr(args, "input_fidelity", None),
        "aspect_ratio": args.aspect_ratio,
        "image_size": args.image_size,
    }
    supported = set(args.supported_params) if args.supported_params is not None else None
    if supported is not None:
        rejected = sorted(k for k, v in explicit.items() if v is not None and k not in supported)
        if rejected:
            _die(
                f"Provider '{args.provider}' does not support parameter(s): {', '.join(rejected)}",
                EXIT_VALIDATION_ERROR,
            )
    params = dict(args.provider_defaults)
    params.update({k: v for k, v in explicit.items() if v is not None})
    if args.command != "edit":
        params.pop("input_fidelity", None)
    args.api_params = params


def _require_readable_file(path: Path, label: str) -> Path:
    if not path.exists():
        _die(f"{label} not found: {path}", EXIT_VALIDATION_ERROR)
    if not path.is_file():
        _die(f"{label} must be a regular file: {path}", EXIT_VALIDATION_ERROR)
    if not os.access(path, os.R_OK):
        _die(f"{label} is not readable: {path}", EXIT_VALIDATION_ERROR)
    return path


def _read_prompt(prompt: Optional[str], prompt_file: Optional[str]) -> str:
    if prompt and prompt_file:
        _die("Use --prompt or --prompt-file, not both.")
    if prompt_file:
        path = _require_readable_file(Path(prompt_file), "Prompt file")
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            _die(f"Could not read prompt file {path}: {exc}")
    if prompt:
        return prompt.strip()
    _die("Missing prompt. Use --prompt or --prompt-file.")
    return ""  # unreachable


def _check_image_paths(paths: Iterable[str]) -> List[Path]:
    resolved: List[Path] = []
    for raw in paths:
        path = _require_readable_file(Path(raw), "Image file")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            _die(f"Image exceeds 50MB limit: {path}", EXIT_VALIDATION_ERROR)
        resolved.append(path)
    return resolved


def _normalize_output_format(fmt: Optional[str]) -> str:
    if not fmt:
        return DEFAULT_OUTPUT_FORMAT
    fmt = fmt.lower()
    if fmt not in {"png", "jpeg", "jpg", "webp"}:
        _die("output-format must be png, jpeg, jpg, or webp.")
    return "jpeg" if fmt == "jpg" else fmt


def _output_extension_matches(suffix: str, output_format: str) -> bool:
    extension = suffix.lstrip(".").lower()
    if extension == "jpg":
        extension = "jpeg"
    return extension == output_format


def _validate_size(size: str) -> None:
    if size not in ALLOWED_SIZES:
        _die(
            "size must be one of 1024x1024, 1536x1024, 1024x1536, or auto."
        )


def _validate_quality(quality: str) -> None:
    if quality not in ALLOWED_QUALITIES:
        _die("quality must be one of low, medium, high, or auto.")


def _validate_background(background: Optional[str]) -> None:
    if background not in ALLOWED_BACKGROUNDS:
        _die("background must be one of transparent, opaque, or auto.")


def _validate_input_fidelity(input_fidelity: Optional[str]) -> None:
    if input_fidelity not in ALLOWED_INPUT_FIDELITIES:
        _die("input-fidelity must be one of low or high.")


def _validate_model(model: Optional[str]) -> None:
    if model is not None and not model.strip():
        _die("model must be a non-empty identifier accepted by the configured image API.")


def _validate_transparency(background: Optional[str], output_format: str) -> None:
    if background == "transparent" and output_format not in {"png", "webp"}:
        _die("transparent background requires output-format png or webp.")


def _validate_generate_payload(payload: Dict[str, Any]) -> None:
    model = payload.get("model")
    _validate_model(str(model) if model is not None else None)
    n = int(payload.get("n", 1))
    if n < 1 or n > 10:
        _die("n must be between 1 and 10")
    size = str(payload.get("size", DEFAULT_SIZE))
    quality = str(payload.get("quality", DEFAULT_QUALITY))
    background = payload.get("background")
    _validate_size(size)
    _validate_quality(quality)
    _validate_background(background)
    oc = payload.get("output_compression")
    if oc is not None:
        if not (0 <= int(oc) <= 100):
            _die("output_compression must be between 0 and 100")
        output_format = _normalize_output_format(payload.get("output_format"))
        if output_format not in {"jpeg", "webp"}:
            _die("output_compression is only valid with JPEG or WebP output")


def _build_output_paths(
    out: str,
    output_format: str,
    count: int,
    out_dir: Optional[str],
) -> List[Path]:
    ext = "." + output_format

    if out_dir:
        out_base = Path(out_dir)
        return [out_base / f"image_{i}{ext}" for i in range(1, count + 1)]

    out_path = Path(out)
    if out_path.exists() and out_path.is_dir():
        return [out_path / f"image_{i}{ext}" for i in range(1, count + 1)]

    if out_path.suffix == "":
        out_path = out_path.with_suffix(ext)
    elif output_format and not _output_extension_matches(out_path.suffix, output_format):
        _warn(
            f"Output extension {out_path.suffix} does not match output-format {output_format}."
        )

    if count == 1:
        return [out_path]

    return [
        out_path.with_name(f"{out_path.stem}-{i}{out_path.suffix}")
        for i in range(1, count + 1)
    ]


def _augment_prompt(args: argparse.Namespace, prompt: str) -> str:
    fields = _fields_from_args(args)
    return _augment_prompt_fields(args.augment, prompt, fields)


def _augment_prompt_fields(augment: bool, prompt: str, fields: Dict[str, Optional[str]]) -> str:
    if not augment:
        return prompt

    sections: List[str] = []
    if fields.get("use_case"):
        sections.append(f"Use case: {fields['use_case']}")
    sections.append(f"Primary request: {prompt}")
    if fields.get("scene"):
        sections.append(f"Scene/background: {fields['scene']}")
    if fields.get("subject"):
        sections.append(f"Subject: {fields['subject']}")
    if fields.get("style"):
        sections.append(f"Style/medium: {fields['style']}")
    if fields.get("composition"):
        sections.append(f"Composition/framing: {fields['composition']}")
    if fields.get("lighting"):
        sections.append(f"Lighting/mood: {fields['lighting']}")
    if fields.get("palette"):
        sections.append(f"Color palette: {fields['palette']}")
    if fields.get("materials"):
        sections.append(f"Materials/textures: {fields['materials']}")
    if fields.get("text"):
        sections.append(f"Text (verbatim): \"{fields['text']}\"")
    if fields.get("constraints"):
        sections.append(f"Constraints: {fields['constraints']}")
    if fields.get("negative"):
        sections.append(f"Avoid: {fields['negative']}")

    return "\n".join(sections)


def _fields_from_args(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    return {
        "use_case": getattr(args, "use_case", None),
        "scene": getattr(args, "scene", None),
        "subject": getattr(args, "subject", None),
        "style": getattr(args, "style", None),
        "composition": getattr(args, "composition", None),
        "lighting": getattr(args, "lighting", None),
        "palette": getattr(args, "palette", None),
        "materials": getattr(args, "materials", None),
        "text": getattr(args, "text", None),
        "constraints": getattr(args, "constraints", None),
        "negative": getattr(args, "negative", None),
    }


def _print_request(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _derive_downscale_path(path: Path, suffix: str) -> Path:
    if suffix and not suffix.startswith("-") and not suffix.startswith("_"):
        suffix = "-" + suffix
    return path.with_name(f"{path.stem}{suffix}{path.suffix}")


def _downscale_image_bytes(image_bytes: bytes, *, max_dim: int, output_format: str) -> bytes:
    try:
        from PIL import Image
    except Exception:
        _die(f"Downscaling requires Pillow. {_dependency_hint('pillow')}")

    if max_dim < 1:
        _die("--downscale-max-dim must be >= 1")

    with Image.open(BytesIO(image_bytes)) as img:
        img.load()
        w, h = img.size
        scale = min(1.0, float(max_dim) / float(max(w, h)))
        target = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))

        resized = img if target == (w, h) else img.resize(target, Image.Resampling.LANCZOS)

        fmt = output_format.lower()
        if fmt == "jpg":
            fmt = "jpeg"

        if fmt == "jpeg":
            if resized.mode in ("RGBA", "LA") or ("transparency" in getattr(resized, "info", {})):
                bg = Image.new("RGB", resized.size, (255, 255, 255))
                bg.paste(resized.convert("RGBA"), mask=resized.convert("RGBA").split()[-1])
                resized = bg
            else:
                resized = resized.convert("RGB")

        out = BytesIO()
        resized.save(out, format=fmt.upper())
        return out.getvalue()


def _all_output_paths(
    outputs: List[Path], downscale_max_dim: Optional[int], downscale_suffix: str
) -> List[Path]:
    targets = list(outputs)
    if downscale_max_dim is not None:
        targets.extend(_derive_downscale_path(path, downscale_suffix) for path in outputs)
    return targets


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def _validate_output_targets(
    targets: Iterable[Path], *, force: bool, owners: Optional[Iterable[str]] = None
) -> None:
    target_list = list(targets)
    owner_list = list(owners) if owners is not None else ["output"] * len(target_list)
    seen: Dict[str, Tuple[Path, str]] = {}
    for target, owner in zip(target_list, owner_list):
        key = _path_key(target)
        if key in seen:
            previous, previous_owner = seen[key]
            _die(
                f"Duplicate output path {target} ({previous_owner} and {owner}). "
                "Output paths must be unique even with --force.",
                EXIT_VALIDATION_ERROR,
            )
        seen[key] = (target, owner)
        if target.exists():
            if target.is_dir():
                _die(f"Output path is a directory: {target}", EXIT_VALIDATION_ERROR)
            if not force:
                _die(
                    f"Output already exists: {target} (use --force to overwrite)",
                    EXIT_VALIDATION_ERROR,
                )
        ancestor = target.parent
        while ancestor != ancestor.parent and not ancestor.exists():
            ancestor = ancestor.parent
        if ancestor.exists() and not ancestor.is_dir():
            _die(
                f"Output parent is not a directory: {ancestor}",
                EXIT_VALIDATION_ERROR,
            )


def _atomic_write_bundle(entries: List[Tuple[Path, bytes]], *, force: bool) -> None:
    """Stage complete files beside their targets, then atomically publish each file."""
    _validate_output_targets((path for path, _ in entries), force=force)
    staged: List[Tuple[Path, Path]] = []
    try:
        for target, content in entries:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                staged.append((Path(handle.name), target))

        # Recheck the complete target set after staging so a concurrent writer is
        # detected before this process publishes anything.
        _validate_output_targets((target for _, target in staged), force=force)
        for temporary, target in staged:
            if force:
                os.replace(temporary, target)
            else:
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    _die(
                        f"Output appeared while writing: {target} (use --force to overwrite)",
                        EXIT_RUNTIME_ERROR,
                    )
                temporary.unlink()
            print(f"Wrote {target}")
    finally:
        for temporary, _ in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _decode_write_and_downscale(
    images: List[str],
    outputs: List[Path],
    *,
    force: bool,
    downscale_max_dim: Optional[int],
    downscale_suffix: str,
    output_format: str,
) -> None:
    if len(images) != len(outputs):
        _die(
            f"API returned {len(images)} image(s), but {len(outputs)} were requested. "
            "No output files were written."
        )

    entries: List[Tuple[Path, bytes]] = []
    for image_b64, out_path in zip(images, outputs):
        try:
            raw = base64.b64decode(image_b64, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            _die(f"API returned invalid base64 image data: {exc}")
        entries.append((out_path, raw))
        if downscale_max_dim is not None:
            derived = _derive_downscale_path(out_path, downscale_suffix)
            resized = _downscale_image_bytes(
                raw, max_dim=downscale_max_dim, output_format=output_format
            )
            entries.append((derived, resized))

    _atomic_write_bundle(entries, force=force)


class ImageProviderAdapter:
    """Interface for provider-specific client and image operation protocols."""

    name = ""

    def endpoint(self, operation: str, model: str) -> str:
        return "/v1/images/edits" if operation == "edit" else "/v1/images/generations"

    def validate_generate_payload(self, payload: Dict[str, Any]) -> None:
        return None

    def validate_edit_payload(self, payload: Dict[str, Any], *, has_mask: bool) -> None:
        self.validate_generate_payload(payload)

    def create_client(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    def create_async_client(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    def generate(self, client: Any, payload: Dict[str, Any]) -> Any:
        raise NotImplementedError

    def edit(self, client: Any, payload: Dict[str, Any]) -> Any:
        raise NotImplementedError

    async def generate_async(self, client: Any, payload: Dict[str, Any]) -> Any:
        raise NotImplementedError


class OpenAIImagesAdapter(ImageProviderAdapter):
    name = "openai_images"

    def validate_generate_payload(self, payload: Dict[str, Any]) -> None:
        google_only = [
            key for key in ("aspect_ratio", "image_size") if payload.get(key) is not None
        ]
        if google_only:
            _die(
                "Parameter(s) only supported by google_generate_content: "
                + ", ".join(google_only)
            )

    @staticmethod
    def _client_kwargs(
        api_url: str,
        api_key: str,
        timeout: Optional[float],
        extra_headers: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "base_url": api_url,
            # CLI batch retry is authoritative; disable SDK retries to avoid
            # multiplying attempts and delays.
            "max_retries": 0,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        if extra_headers:
            kwargs["default_headers"] = extra_headers
        return kwargs

    def create_client(self, **kwargs: Any) -> Any:
        try:
            from openai import OpenAI
        except ImportError:
            _die(f"openai SDK not installed in the active environment. {_dependency_hint('openai')}")
        return OpenAI(**self._client_kwargs(**kwargs))

    def create_async_client(self, **kwargs: Any) -> Any:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            try:
                import openai as _openai  # noqa: F401
            except ImportError:
                _die(
                    f"openai SDK not installed in the active environment. {_dependency_hint('openai')}"
                )
            _die(
                "AsyncOpenAI not available in this openai SDK version. "
                f"{_dependency_hint('openai', upgrade=True)}"
            )
        return AsyncOpenAI(**self._client_kwargs(**kwargs))

    def generate(self, client: Any, payload: Dict[str, Any]) -> Any:
        return client.images.generate(**payload)

    def edit(self, client: Any, payload: Dict[str, Any]) -> Any:
        return client.images.edit(**payload)

    async def generate_async(self, client: Any, payload: Dict[str, Any]) -> Any:
        return await client.images.generate(**payload)


@dataclass(frozen=True)
class _AdapterImage:
    b64_json: str


@dataclass(frozen=True)
class _AdapterResult:
    data: List[_AdapterImage]


@dataclass(frozen=True)
class _GoogleGenerateContentClient:
    base_url: str
    api_key: str
    timeout: Optional[float]
    extra_headers: Dict[str, str]

    def post(self, model: str, body: Dict[str, Any]) -> Dict[str, Any]:
        endpoint = f"{self.base_url.rstrip('/')}/models/{quote(model, safe='')}:generateContent"
        headers = {
            key: value
            for key, value in self.extra_headers.items()
            if key.lower() not in {"content-type", "x-goog-api-key"}
        }
        headers.update({"Content-Type": "application/json", "x-goog-api-key": self.api_key})
        request = Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise RuntimeError(f"Google generateContent returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Google generateContent request failed: {exc.reason}") from exc
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("Google generateContent returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("Google generateContent response must be a JSON object")
        return parsed


class GoogleGenerateContentAdapter(ImageProviderAdapter):
    """Gemini native image generation via the REST generateContent method."""

    name = "google_generate_content"
    _allowed_payload_keys = {
        "model", "prompt", "n", "aspect_ratio", "image_size", "output_format"
    }

    def endpoint(self, operation: str, model: str) -> str:
        return f"/models/{quote(model, safe='')}:generateContent"

    def validate_generate_payload(self, payload: Dict[str, Any]) -> None:
        if int(payload.get("n", 1)) != 1:
            _die("Google generateContent supports exactly one requested image per call (n=1).")
        unsupported = sorted(
            key for key, value in payload.items()
            if value is not None and key not in self._allowed_payload_keys
        )
        if unsupported:
            _die(
                "Google generateContent does not support CLI parameter(s): "
                + ", ".join(unsupported)
            )
        aspect_ratio = payload.get("aspect_ratio")
        if aspect_ratio is not None and aspect_ratio not in GOOGLE_ASPECT_RATIOS:
            _die(
                "aspect-ratio must be one of: " + ", ".join(sorted(GOOGLE_ASPECT_RATIOS))
            )
        image_size = payload.get("image_size")
        if image_size is not None and image_size not in GOOGLE_IMAGE_SIZES:
            _die("image-size must be one of 512, 1K, 2K, or 4K (uppercase K).")
        if image_size is not None and str(payload.get("model", "")).startswith(
            "gemini-2.5-flash-image"
        ):
            _die("gemini-2.5-flash-image does not support image-size configuration.")
        output_format = payload.get("output_format")
        if output_format is not None and _normalize_output_format(str(output_format)) != "png":
            _die("Google generateContent currently supports PNG output in this CLI adapter.")

    def validate_edit_payload(self, payload: Dict[str, Any], *, has_mask: bool) -> None:
        self.validate_generate_payload(payload)
        if has_mask:
            _die("Google generateContent image editing does not support --mask.")

    def create_client(self, **kwargs: Any) -> _GoogleGenerateContentClient:
        return _GoogleGenerateContentClient(
            base_url=kwargs["api_url"],
            api_key=kwargs["api_key"],
            timeout=kwargs.get("timeout"),
            extra_headers=dict(kwargs.get("extra_headers") or {}),
        )

    def create_async_client(self, **kwargs: Any) -> _GoogleGenerateContentClient:
        return self.create_client(**kwargs)

    @staticmethod
    def _request_body(payload: Dict[str, Any], image_parts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        parts: List[Dict[str, Any]] = [{"text": str(payload["prompt"])}]
        if image_parts:
            parts.extend(image_parts)
        generation_config: Dict[str, Any] = {"responseModalities": ["IMAGE"]}
        image_config: Dict[str, Any] = {}
        if payload.get("aspect_ratio") is not None:
            image_config["aspectRatio"] = payload["aspect_ratio"]
        if payload.get("image_size") is not None:
            image_config["imageSize"] = payload["image_size"]
        if image_config:
            generation_config["imageConfig"] = image_config
        return {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config,
        }

    @staticmethod
    def _result(response: Dict[str, Any]) -> _AdapterResult:
        images: List[_AdapterImage] = []
        candidates = response.get("candidates")
        if not isinstance(candidates, list):
            feedback = response.get("promptFeedback")
            raise RuntimeError(f"Google generateContent returned no candidates: {feedback or 'unknown reason'}")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content", {})
            parts = content.get("parts", []) if isinstance(content, dict) else []
            for part in parts:
                if not isinstance(part, dict) or part.get("thought") is True:
                    continue
                inline = part.get("inlineData") or part.get("inline_data")
                if not isinstance(inline, dict):
                    continue
                mime_type = inline.get("mimeType") or inline.get("mime_type")
                data = inline.get("data")
                if not isinstance(data, str) or not data:
                    raise RuntimeError("Google generateContent returned an image without data")
                if mime_type != "image/png":
                    raise RuntimeError(
                        f"Google generateContent returned unsupported image MIME type: {mime_type}"
                    )
                images.append(_AdapterImage(data))
        if not images:
            finish_reasons = [
                candidate.get("finishReason")
                for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("finishReason")
            ]
            raise RuntimeError(
                "Google generateContent returned no final image"
                + (f" (finish reasons: {', '.join(finish_reasons)})" if finish_reasons else "")
            )
        return _AdapterResult(images)

    def generate(self, client: _GoogleGenerateContentClient, payload: Dict[str, Any]) -> _AdapterResult:
        self.validate_generate_payload(payload)
        return self._result(client.post(str(payload["model"]), self._request_body(payload)))

    def edit(self, client: _GoogleGenerateContentClient, payload: Dict[str, Any]) -> _AdapterResult:
        request_payload = dict(payload)
        image_value = request_payload.pop("image")
        has_mask = request_payload.pop("mask", None) is not None
        self.validate_edit_payload(request_payload, has_mask=has_mask)
        handles = image_value if isinstance(image_value, list) else [image_value]
        image_parts: List[Dict[str, Any]] = []
        for handle in handles:
            filename = str(getattr(handle, "name", ""))
            mime_type = mimetypes.guess_type(filename)[0]
            if not mime_type or not mime_type.startswith("image/"):
                raise RuntimeError(f"Could not determine image MIME type for Google input: {filename}")
            encoded = base64.b64encode(handle.read()).decode("ascii")
            image_parts.append(
                {"inline_data": {"mime_type": mime_type, "data": encoded}}
            )
        return self._result(
            client.post(
                str(request_payload["model"]),
                self._request_body(request_payload, image_parts),
            )
        )

    async def generate_async(
        self, client: _GoogleGenerateContentClient, payload: Dict[str, Any]
    ) -> _AdapterResult:
        return await asyncio.to_thread(self.generate, client, payload)


ADAPTERS: Dict[str, ImageProviderAdapter] = {
    OpenAIImagesAdapter.name: OpenAIImagesAdapter(),
    GoogleGenerateContentAdapter.name: GoogleGenerateContentAdapter(),
}


def _get_adapter(name: str) -> ImageProviderAdapter:
    try:
        return ADAPTERS[name]
    except KeyError:
        _die(f"Unsupported provider adapter: {name}")
    raise AssertionError("unreachable")


def _create_client(
    api_url: str,
    api_key: str,
    *,
    timeout: Optional[float] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    adapter: str = "openai_images",
) -> Any:
    return _get_adapter(adapter).create_client(
        api_url=api_url, api_key=api_key, timeout=timeout, extra_headers=extra_headers
    )


def _create_async_client(
    api_url: str,
    api_key: str,
    *,
    timeout: Optional[float] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    adapter: str = "openai_images",
) -> Any:
    return _get_adapter(adapter).create_async_client(
        api_url=api_url, api_key=api_key, timeout=timeout, extra_headers=extra_headers
    )


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:60] if value else "job"


def _normalize_job(job: Any, idx: int) -> Dict[str, Any]:
    if isinstance(job, str):
        prompt = job.strip()
        if not prompt:
            _die(f"Empty prompt at job {idx}")
        return {"prompt": prompt}
    if isinstance(job, dict):
        if "prompt" not in job or not str(job["prompt"]).strip():
            _die(f"Missing prompt for job {idx}")
        return job
    _die(f"Invalid job at index {idx}: expected string or object.")
    return {}  # unreachable


def _read_jobs_jsonl(path: str) -> List[Dict[str, Any]]:
    p = _require_readable_file(Path(path), "Input file")
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        _die(f"Could not read input file {p}: {exc}")
    jobs: List[Dict[str, Any]] = []
    for line_no, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item: Any
            if line.startswith("{"):
                item = json.loads(line)
            else:
                item = line
            jobs.append(_normalize_job(item, idx=line_no))
        except json.JSONDecodeError as exc:
            _die(f"Invalid JSON on line {line_no}: {exc}")
    if not jobs:
        _die("No jobs found in input file.")
    if len(jobs) > MAX_BATCH_JOBS:
        _die(f"Too many jobs ({len(jobs)}). Max is {MAX_BATCH_JOBS}.")
    return jobs


def _merge_non_null(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(dst)
    for k, v in src.items():
        if v is not None:
            merged[k] = v
    return merged


def _job_output_paths(
    *,
    out_dir: Path,
    output_format: str,
    idx: int,
    prompt: str,
    n: int,
    explicit_out: Optional[str],
) -> List[Path]:
    ext = "." + output_format

    if explicit_out:
        base = Path(explicit_out)
        if base.suffix == "":
            base = base.with_suffix(ext)
        elif not _output_extension_matches(base.suffix, output_format):
            _warn(
                f"Job {idx}: output extension {base.suffix} does not match output-format {output_format}."
            )
        base = out_dir / base.name
    else:
        slug = _slugify(prompt[:80])
        base = out_dir / f"{idx:03d}-{slug}{ext}"

    if n == 1:
        return [base]
    return [
        base.with_name(f"{base.stem}-{i}{base.suffix}")
        for i in range(1, n + 1)
    ]


def _extract_retry_after_seconds(exc: Exception) -> Optional[float]:
    # Best-effort: openai SDK errors vary by version. Prefer a conservative default.
    for attr in ("retry_after", "retry_after_seconds"):
        val = getattr(exc, attr, None)
        if isinstance(val, (int, float)) and val >= 0:
            return float(val)
    msg = str(exc)
    m = re.search(r"retry[- ]after[:= ]+([0-9]+(?:\.[0-9]+)?)", msg, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    if "ratelimit" in name or "rate_limit" in name:
        return True
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "too many requests" in msg


def _is_transient_error(exc: Exception) -> bool:
    if _is_rate_limit_error(exc):
        return True
    name = exc.__class__.__name__.lower()
    if "timeout" in name or "timedout" in name or "tempor" in name:
        return True
    msg = str(exc).lower()
    return (
        "timeout" in msg
        or "timed out" in msg
        or "connection reset" in msg
        or re.search(r"\b(?:500|502|503|504)\b", msg) is not None
    )


async def _generate_one_with_retries(
    client: Any,
    payload: Dict[str, Any],
    *,
    attempts: int,
    job_label: str,
    adapter: ImageProviderAdapter,
) -> Any:
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return await adapter.generate_async(client, payload)
        except Exception as exc:
            last_exc = exc
            if not _is_transient_error(exc):
                raise
            if attempt == attempts:
                raise
            sleep_s = _extract_retry_after_seconds(exc)
            if sleep_s is None:
                sleep_s = min(60.0, 2.0**attempt)
            print(
                f"{job_label} attempt {attempt}/{attempts} failed ({exc.__class__.__name__}); retrying in {sleep_s:.1f}s",
                file=sys.stderr,
            )
            await asyncio.sleep(sleep_s)
    raise last_exc or RuntimeError("unknown error")


@dataclass(frozen=True)
class BatchJobPlan:
    index: int
    payload: Dict[str, Any]
    outputs: List[Path]
    output_format: str


@dataclass(frozen=True)
class BatchPlan:
    jobs: List[BatchJobPlan]
    all_targets: List[Path]


def _preflight_generate_batch(args: argparse.Namespace) -> BatchPlan:
    """Validate every job and every destination before any async task exists."""
    jobs = _read_jobs_jsonl(args.input)
    out_dir = Path(args.out_dir)
    base_fields = _fields_from_args(args)
    base_payload = {"model": args.model, "n": args.n, **args.api_params}

    plans: List[BatchJobPlan] = []
    all_targets: List[Path] = []
    owners: List[str] = []
    for i, job in enumerate(jobs, start=1):
        try:
            prompt = str(job["prompt"]).strip()
            job_fields = job.get("fields", {})
            if not isinstance(job_fields, dict):
                _die("fields must be an object", EXIT_VALIDATION_ERROR)
            fields = _merge_non_null(base_fields, job_fields)
            fields = _merge_non_null(fields, {k: job.get(k) for k in base_fields})
            augmented = _augment_prompt_fields(args.augment, prompt, fields)

            payload = dict(base_payload)
            payload["prompt"] = augmented
            override_keys = {"model", "n", *OPTIONAL_API_PARAMS}
            overrides = {k: job.get(k) for k in override_keys}
            if args.supported_params is not None:
                unsupported = sorted(
                    k
                    for k in OPTIONAL_API_PARAMS
                    if overrides.get(k) is not None and k not in args.supported_params
                )
                if unsupported:
                    _die(
                        f"provider does not support parameter(s): {', '.join(unsupported)}",
                        EXIT_VALIDATION_ERROR,
                    )
            payload = _merge_non_null(payload, overrides)
            if job.get("model") is not None:
                payload["model"] = _resolve_provider_model(
                    args.provider, args.provider_models, job["model"]
                )
            payload = {k: v for k, v in payload.items() if v is not None}
            _validate_generate_payload(payload)
            _get_adapter(args.adapter).validate_generate_payload(payload)
            effective_format = _normalize_output_format(payload.get("output_format"))
            _validate_transparency(payload.get("background"), effective_format)
            n = int(payload.get("n", 1))

            explicit_out = job.get("out")
            if explicit_out is not None and (
                not isinstance(explicit_out, str) or not explicit_out.strip()
            ):
                _die("out must be a non-empty path string", EXIT_VALIDATION_ERROR)
            outputs = _job_output_paths(
                out_dir=out_dir,
                output_format=effective_format,
                idx=i,
                prompt=prompt,
                n=n,
                explicit_out=explicit_out,
            )
            targets = _all_output_paths(
                outputs, args.downscale_max_dim, args.downscale_suffix
            )
            plans.append(BatchJobPlan(i, payload, outputs, effective_format))
            all_targets.extend(targets)
            owners.extend([f"job {i}"] * len(targets))
        except ImagenError as exc:
            raise BatchJobError(
                i, "validation", str(exc), EXIT_VALIDATION_ERROR
            ) from exc
        except (TypeError, ValueError) as exc:
            raise BatchJobError(
                i, "validation", str(exc), EXIT_VALIDATION_ERROR
            ) from exc

    _validate_output_targets(all_targets, force=args.force, owners=owners)
    return BatchPlan(plans, all_targets)


async def _run_generate_batch(args: argparse.Namespace) -> int:
    plan = _preflight_generate_batch(args)

    if args.dry_run:
        for job in plan.jobs:
            downscaled = None
            if args.downscale_max_dim is not None:
                downscaled = [
                    str(_derive_downscale_path(path, args.downscale_suffix))
                    for path in job.outputs
                ]
            _print_request(
                {
                    "provider": args.provider,
                    "api_url": args.safe_api_url,
                    "adapter": args.adapter,
                    "endpoint": _get_adapter(args.adapter).endpoint("generate", str(job.payload["model"])),
                    "job": job.index,
                    "outputs": [str(path) for path in job.outputs],
                    "outputs_downscaled": downscaled,
                    **job.payload,
                }
            )
        return 0

    # Client creation and task creation deliberately happen only after the full
    # batch and its complete output set pass preflight.
    client = _create_async_client(
        args.api_url,
        args.api_key,
        timeout=args.timeout,
        extra_headers=args.extra_headers,
        adapter=args.adapter,
    )
    provider_adapter = _get_adapter(args.adapter)
    sem = asyncio.Semaphore(args.concurrency)
    stop_event = asyncio.Event()
    failures: List[BatchJobError] = []

    async def run_job(job: BatchJobPlan) -> None:
        job_label = f"[job {job.index}/{len(plan.jobs)}]"
        try:
            async with sem:
                if args.fail_fast and stop_event.is_set():
                    return
                print(f"{job_label} starting", file=sys.stderr)
                started = time.time()
                result = await _generate_one_with_retries(
                    client,
                    job.payload,
                    attempts=args.max_attempts,
                    job_label=job_label,
                    adapter=provider_adapter,
                )
                elapsed = time.time() - started
                print(f"{job_label} completed in {elapsed:.1f}s", file=sys.stderr)
            images = [item.b64_json for item in result.data]
            _decode_write_and_downscale(
                images,
                job.outputs,
                force=args.force,
                downscale_max_dim=args.downscale_max_dim,
                downscale_suffix=args.downscale_suffix,
                output_format=job.output_format,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = BatchJobError(job.index, "runtime", str(exc), EXIT_RUNTIME_ERROR)
            failures.append(error)
            print(f"{job_label} failed: {exc}", file=sys.stderr)
            if args.fail_fast:
                stop_event.set()
                raise error from exc

    tasks = [asyncio.create_task(run_job(job)) for job in plan.jobs]
    try:
        await asyncio.gather(*tasks)
    except BatchJobError:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    return EXIT_RUNTIME_ERROR if failures else 0


def _generate_batch(args: argparse.Namespace) -> None:
    exit_code = asyncio.run(_run_generate_batch(args))
    if exit_code:
        raise ImagenError("One or more batch jobs failed.", exit_code)


def _generate(args: argparse.Namespace) -> None:
    prompt = _read_prompt(args.prompt, args.prompt_file)
    prompt = _augment_prompt(args, prompt)

    payload = {"model": args.model, "prompt": prompt, "n": args.n, **args.api_params}
    payload = {k: v for k, v in payload.items() if v is not None}
    _validate_generate_payload(payload)
    _get_adapter(args.adapter).validate_generate_payload(payload)

    output_format = _normalize_output_format(payload.get("output_format"))
    _validate_transparency(payload.get("background"), output_format)
    output_paths = _build_output_paths(args.out, output_format, args.n, args.out_dir)
    downscaled = None
    if args.downscale_max_dim is not None:
        downscaled = [str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths]
    _validate_output_targets(
        _all_output_paths(output_paths, args.downscale_max_dim, args.downscale_suffix),
        force=args.force,
    )

    if args.dry_run:
        _print_request(
            {
                "provider": args.provider,
                "api_url": args.safe_api_url,
                "adapter": args.adapter,
                "endpoint": _get_adapter(args.adapter).endpoint("generate", str(payload["model"])),
                "outputs": [str(p) for p in output_paths],
                "outputs_downscaled": downscaled,
                **payload,
            }
        )
        return

    print(
        f"Calling provider '{args.provider}' for image generation. This can take up to a couple of minutes.",
        file=sys.stderr,
    )
    started = time.time()
    client = _create_client(
        args.api_url,
        args.api_key,
        timeout=args.timeout,
        extra_headers=args.extra_headers,
        adapter=args.adapter,
    )
    try:
        result = _get_adapter(args.adapter).generate(client, payload)
    except Exception as exc:
        raise ImagenError(f"Image generation request failed: {exc}") from exc
    elapsed = time.time() - started
    print(f"Generation completed in {elapsed:.1f}s.", file=sys.stderr)

    images = [item.b64_json for item in result.data]
    _decode_write_and_downscale(
        images,
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        output_format=output_format,
    )


def _edit(args: argparse.Namespace) -> None:
    prompt = _read_prompt(args.prompt, args.prompt_file)
    prompt = _augment_prompt(args, prompt)

    image_paths = _check_image_paths(args.image)
    mask_path = _require_readable_file(Path(args.mask), "Mask file") if args.mask else None
    if mask_path:
        if mask_path.suffix.lower() != ".png":
            _warn(f"Mask should be a PNG with an alpha channel: {mask_path}")
        if mask_path.stat().st_size > MAX_IMAGE_BYTES:
            _die(f"Mask exceeds 50MB limit: {mask_path}", EXIT_VALIDATION_ERROR)

    payload = {"model": args.model, "prompt": prompt, "n": args.n, **args.api_params}
    payload = {k: v for k, v in payload.items() if v is not None}
    _validate_generate_payload(payload)
    _get_adapter(args.adapter).validate_edit_payload(
        payload, has_mask=mask_path is not None
    )

    output_format = _normalize_output_format(payload.get("output_format"))
    _validate_transparency(payload.get("background"), output_format)
    _validate_input_fidelity(payload.get("input_fidelity"))
    output_paths = _build_output_paths(args.out, output_format, args.n, args.out_dir)
    downscaled = None
    if args.downscale_max_dim is not None:
        downscaled = [str(_derive_downscale_path(p, args.downscale_suffix)) for p in output_paths]
    _validate_output_targets(
        _all_output_paths(output_paths, args.downscale_max_dim, args.downscale_suffix),
        force=args.force,
    )

    if args.dry_run:
        payload_preview = dict(payload)
        payload_preview["image"] = [str(p) for p in image_paths]
        if mask_path:
            payload_preview["mask"] = str(mask_path)
        _print_request(
            {
                "provider": args.provider,
                "api_url": args.safe_api_url,
                "adapter": args.adapter,
                "endpoint": _get_adapter(args.adapter).endpoint("edit", str(payload["model"])),
                "outputs": [str(p) for p in output_paths],
                "outputs_downscaled": downscaled,
                **payload_preview,
            }
        )
        return

    print(
        f"Calling provider '{args.provider}' for image editing with {len(image_paths)} image(s).",
        file=sys.stderr,
    )
    started = time.time()
    client = _create_client(
        args.api_url,
        args.api_key,
        timeout=args.timeout,
        extra_headers=args.extra_headers,
        adapter=args.adapter,
    )

    with _open_files(image_paths) as image_files, _open_mask(mask_path) as mask_file:
        request = dict(payload)
        request["image"] = image_files if len(image_files) > 1 else image_files[0]
        if mask_file is not None:
            request["mask"] = mask_file
        try:
            result = _get_adapter(args.adapter).edit(client, request)
        except Exception as exc:
            raise ImagenError(f"Image edit request failed: {exc}") from exc

    elapsed = time.time() - started
    print(f"Edit completed in {elapsed:.1f}s.", file=sys.stderr)
    images = [item.b64_json for item in result.data]
    _decode_write_and_downscale(
        images,
        output_paths,
        force=args.force,
        downscale_max_dim=args.downscale_max_dim,
        downscale_suffix=args.downscale_suffix,
        output_format=output_format,
    )


def _open_files(paths: List[Path]):
    return _FileBundle(paths)


def _open_mask(mask_path: Optional[Path]):
    if mask_path is None:
        return _NullContext()
    return _SingleFile(mask_path)


class _NullContext:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


class _SingleFile:
    def __init__(self, path: Path):
        self._path = path
        self._handle = None

    def __enter__(self):
        self._handle = self._path.open("rb")
        return self._handle

    def __exit__(self, exc_type, exc, tb):
        if self._handle:
            try:
                self._handle.close()
            except Exception:
                pass
        return False


class _FileBundle:
    def __init__(self, paths: List[Path]):
        self._paths = paths
        self._handles: List[object] = []

    def __enter__(self):
        self._handles = [p.open("rb") for p in self._paths]
        return self._handles

    def __exit__(self, exc_type, exc, tb):
        for handle in self._handles:
            try:
                handle.close()
            except Exception:
                pass
        return False


def _provider_rows(config_path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    data, providers = _load_validated_config(config_path)
    rows: List[Dict[str, Any]] = []
    for name in sorted(providers):
        provider = providers[name]
        key_env = provider.get("api_key_env")
        credential_source = "environment" if key_env is not None else "config"
        rows.append(
            {
                "provider": name,
                "default": name == data.get("default_provider")
                or (data.get("default_provider") is None and len(providers) == 1),
                "adapter": provider.get("adapter", "openai_images"),
                "url": _safe_url(provider["url"]),
                "default_model": provider.get("default_model")
                or (provider["models"][0] if len(provider["models"]) == 1 else None),
                "models": provider["models"],
                "credential_source": credential_source,
                "api_key_env": key_env,
                "key_configured": (
                    bool(os.getenv(key_env, "").strip()) if key_env is not None else True
                ),
            }
        )
    return data, rows


def _config_check(args: argparse.Namespace) -> None:
    config_path = _resolve_config_path(args.config)
    _, rows = _provider_rows(config_path)
    _print_request({"config": str(config_path), "providers": rows, "valid": True})
    missing = [
        row["api_key_env"]
        for row in rows
        if row["credential_source"] == "environment" and not row["key_configured"]
    ]
    if missing:
        _die(
            "Provider configuration is structurally valid, but these API key variables are unset: "
            + ", ".join(missing),
            EXIT_VALIDATION_ERROR,
        )


def _list_providers(args: argparse.Namespace) -> None:
    config_path = _resolve_config_path(args.config)
    _, rows = _provider_rows(config_path)
    _print_request({"config": str(config_path), "providers": rows})


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        help=f"Provider config path (default: {DEFAULT_CONFIG_PATH}; env: {CONFIG_PATH_ENV})",
    )


def _add_shared_args(parser: argparse.ArgumentParser) -> None:
    _add_config_arg(parser)
    parser.add_argument("--provider", help="Configured provider selection ID")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Configured API model ID")
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--size")
    parser.add_argument("--quality")
    parser.add_argument("--aspect-ratio")
    parser.add_argument("--image-size")
    parser.add_argument("--background")
    parser.add_argument("--output-format")
    parser.add_argument("--output-compression", type=int)
    parser.add_argument("--moderation")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--out", default=DEFAULT_OUTPUT_PATH)
    output_group.add_argument("--out-dir")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--augment", dest="augment", action="store_true")
    parser.add_argument("--no-augment", dest="augment", action="store_false")
    parser.set_defaults(augment=True)

    # Prompt augmentation hints
    parser.add_argument("--use-case")
    parser.add_argument("--scene")
    parser.add_argument("--subject")
    parser.add_argument("--style")
    parser.add_argument("--composition")
    parser.add_argument("--lighting")
    parser.add_argument("--palette")
    parser.add_argument("--materials")
    parser.add_argument("--text")
    parser.add_argument("--constraints")
    parser.add_argument("--negative")

    # Post-processing (optional): generate an additional downscaled copy for fast web loading.
    parser.add_argument("--downscale-max-dim", type=int)
    parser.add_argument("--downscale-suffix", default=DEFAULT_DOWNSCALE_SUFFIX)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate or edit images through preconfigured image API providers"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser(
        "config-check", help="Validate every configured provider and API key status"
    )
    _add_config_arg(check_parser)
    check_parser.set_defaults(func=_config_check)

    providers_parser = subparsers.add_parser(
        "providers", aliases=["list-providers"], help="List configured providers and models"
    )
    _add_config_arg(providers_parser)
    providers_parser.set_defaults(func=_list_providers)

    gen_parser = subparsers.add_parser("generate", help="Create a new image")
    _add_shared_args(gen_parser)
    gen_parser.set_defaults(func=_generate)

    batch_parser = subparsers.add_parser(
        "generate-batch",
        help="Generate multiple prompts concurrently (JSONL input)",
    )
    _add_shared_args(batch_parser)
    batch_parser.add_argument("--input", required=True, help="Path to JSONL file (one job per line)")
    batch_parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    batch_parser.add_argument("--max-attempts", type=int, default=3)
    batch_parser.add_argument("--fail-fast", action="store_true")
    batch_parser.set_defaults(func=_generate_batch)

    edit_parser = subparsers.add_parser("edit", help="Edit an existing image")
    _add_shared_args(edit_parser)
    edit_parser.add_argument("--image", action="append", required=True)
    edit_parser.add_argument("--mask")
    edit_parser.add_argument("--input-fidelity")
    edit_parser.set_defaults(func=_edit)

    args = parser.parse_args()
    if args.command in {"config-check", "providers", "list-providers"}:
        args.func(args)
        return 0

    if args.n < 1 or args.n > 10:
        _die("--n must be between 1 and 10")
    if getattr(args, "concurrency", 1) < 1 or getattr(args, "concurrency", 1) > 25:
        _die("--concurrency must be between 1 and 25")
    if getattr(args, "max_attempts", 3) < 1 or getattr(args, "max_attempts", 3) > 10:
        _die("--max-attempts must be between 1 and 10")
    if args.output_compression is not None and not (0 <= args.output_compression <= 100):
        _die("--output-compression must be between 0 and 100")
    if args.command == "generate-batch" and not args.out_dir:
        _die("generate-batch requires --out-dir")
    if getattr(args, "downscale_max_dim", None) is not None and args.downscale_max_dim < 1:
        _die("--downscale-max-dim must be >= 1")

    _configure_provider(args)
    _prepare_provider_params(args)
    _validate_size(str(args.api_params.get("size", DEFAULT_SIZE)))
    _validate_quality(str(args.api_params.get("quality", DEFAULT_QUALITY)))
    _validate_background(args.api_params.get("background"))
    _validate_model(args.model)

    args.func(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ImagenError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code)
