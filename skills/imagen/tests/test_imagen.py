import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "imagen.py"
SPEC = importlib.util.spec_from_file_location("imagen_cli", SCRIPT)
assert SPEC and SPEC.loader
imagen = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = imagen
SPEC.loader.exec_module(imagen)


class ImagenProviderConfigTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.work = Path(self.tempdir.name)
        self.config = self.work / "providers.json"
        self.config.write_text(
            json.dumps(
                {
                    "default_provider": "primary",
                    "providers": {
                        "primary": {
                            "url": "https://primary.example/v1",
                            "api_key_env": "PRIMARY_TEST_IMAGE_KEY",
                            "default_model": "primary-image-v1",
                            "models": [
                                "primary-image-v1",
                                "primary-image-pro",
                            ],
                        },
                        "backup": {
                            "url": "https://backup.example/v1",
                            "api_key_env": "BACKUP_TEST_IMAGE_KEY",
                            "default_model": "backup-image-v2",
                            "models": [
                                "backup-image-v2",
                                "backup-image-quality-v3",
                            ],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.env = os.environ.copy()
        self.env.pop("PRIMARY_TEST_IMAGE_KEY", None)
        self.env.pop("BACKUP_TEST_IMAGE_KEY", None)
        self.env.pop("IMAGEN_CONFIG_PATH", None)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_cli(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.work,
            env=env or self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_default_provider_is_selected(self):
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--prompt",
            "Test",
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["provider"], "primary")
        self.assertEqual(payload["api_url"], "https://primary.example/v1")
        self.assertEqual(payload["model"], "primary-image-v1")
        self.assertNotIn("api_key", payload)

    def test_explicit_provider_switches_url_and_model(self):
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--provider",
            "backup",
            "--prompt",
            "Test",
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["provider"], "backup")
        self.assertEqual(payload["api_url"], "https://backup.example/v1")
        self.assertEqual(payload["model"], "backup-image-v2")

    def test_configured_model_can_be_selected_for_one_command(self):
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--provider",
            "backup",
            "--model",
            "backup-image-quality-v3",
            "--prompt",
            "Test",
            "--dry-run",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["model"], "backup-image-quality-v3")

    def test_unknown_model_lists_provider_models(self):
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--provider",
            "backup",
            "--model",
            "missing",
            "--prompt",
            "Test",
            "--dry-run",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown model 'missing' for provider 'backup'", result.stderr)
        self.assertIn("backup-image-quality-v3, backup-image-v2", result.stderr)

    def test_config_path_can_come_from_environment(self):
        env = dict(self.env, IMAGEN_CONFIG_PATH=str(self.config))
        result = self.run_cli("generate", "--prompt", "Test", "--dry-run", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["provider"], "primary")

    def test_live_request_requires_selected_provider_key(self):
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--provider",
            "backup",
            "--prompt",
            "Test",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BACKUP_TEST_IMAGE_KEY", result.stderr)
        self.assertNotIn("PRIMARY_TEST_IMAGE_KEY", result.stderr)

    def test_unknown_provider_lists_available_names(self):
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--provider",
            "missing",
            "--prompt",
            "Test",
            "--dry-run",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown provider 'missing'", result.stderr)
        self.assertIn("backup, primary", result.stderr)

    def test_provider_selection_applies_to_edit_and_batch(self):
        image = self.work / "input.png"
        image.write_bytes(b"placeholder")
        edit = self.run_cli(
            "edit",
            "--config",
            str(self.config),
            "--provider",
            "backup",
            "--image",
            str(image),
            "--prompt",
            "Change the background",
            "--dry-run",
        )
        self.assertEqual(edit.returncode, 0, edit.stderr)
        self.assertEqual(json.loads(edit.stdout)["provider"], "backup")

        jobs = self.work / "jobs.jsonl"
        jobs.write_text(
            '{"prompt":"Create a landscape","model":"backup-image-quality-v3"}\n',
            encoding="utf-8",
        )
        batch = self.run_cli(
            "generate-batch",
            "--config",
            str(self.config),
            "--provider",
            "backup",
            "--input",
            str(jobs),
            "--out-dir",
            str(self.work / "output"),
            "--dry-run",
        )
        self.assertEqual(batch.returncode, 0, batch.stderr)
        batch_payload = json.loads(batch.stdout)
        self.assertEqual(batch_payload["provider"], "backup")
        self.assertEqual(batch_payload["model"], "backup-image-quality-v3")

    def test_providers_lists_defaults_models_and_key_status(self):
        result = self.run_cli("providers", "--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)["providers"]
        self.assertEqual([row["provider"] for row in rows], ["backup", "primary"])
        primary = next(row for row in rows if row["provider"] == "primary")
        self.assertTrue(primary["default"])
        self.assertFalse(primary["key_configured"])
        self.assertEqual(primary["default_model"], "primary-image-v1")

    def test_config_check_validates_every_provider_and_keys(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["providers"]["backup"]["url"] = "not-a-url"
        self.config.write_text(json.dumps(data), encoding="utf-8")
        env = dict(
            self.env,
            PRIMARY_TEST_IMAGE_KEY="one",
            BACKUP_TEST_IMAGE_KEY="two",
        )
        result = self.run_cli("config-check", "--config", str(self.config), env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("backup", result.stderr)
        self.assertIn("invalid HTTP(S) URL", result.stderr)

    def test_config_check_reports_all_missing_keys(self):
        result = self.run_cli("config-check", "--config", str(self.config))
        self.assertEqual(result.returncode, imagen.EXIT_VALIDATION_ERROR)
        self.assertIn("PRIMARY_TEST_IMAGE_KEY", result.stderr)
        self.assertIn("BACKUP_TEST_IMAGE_KEY", result.stderr)

    def test_invalid_default_references_are_rejected(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["default_provider"] = "missing"
        self.config.write_text(json.dumps(data), encoding="utf-8")
        result = self.run_cli("providers", "--config", str(self.config))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("default_provider", result.stderr)

        data["default_provider"] = "primary"
        data["providers"]["backup"]["default_model"] = "missing"
        self.config.write_text(json.dumps(data), encoding="utf-8")
        result = self.run_cli("providers", "--config", str(self.config))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("default_model 'missing'", result.stderr)

    def test_optional_api_params_are_only_sent_when_configured(self):
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--prompt",
            "Test",
            "--dry-run",
        )
        payload = json.loads(result.stdout)
        for name in ("size", "quality", "output_format", "background"):
            self.assertNotIn(name, payload)

        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["providers"]["primary"].update(
            {
                "timeout": 12.5,
                "defaults": {"quality": "high"},
                "supported_params": ["quality"],
                "extra_headers": {"X-Tenant": "images"},
            }
        )
        self.config.write_text(json.dumps(data), encoding="utf-8")
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--prompt",
            "Test",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["quality"], "high")
        self.assertNotIn("size", payload)

    def test_supported_params_rejects_unsupported_cli_option(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["providers"]["primary"]["supported_params"] = ["quality"]
        self.config.write_text(json.dumps(data), encoding="utf-8")
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--prompt",
            "Test",
            "--size",
            "1024x1024",
            "--dry-run",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not support parameter(s): size", result.stderr)

    def test_displayed_provider_url_is_sanitized(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["providers"]["primary"]["url"] = (
            "https://user:secret@primary.example/v1?token=hidden#fragment"
        )
        self.config.write_text(json.dumps(data), encoding="utf-8")
        result = self.run_cli("providers", "--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout + result.stderr
        self.assertIn("https://primary.example/v1", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("hidden", output)

    def test_retry_after_decimal_is_parsed(self):
        self.assertEqual(
            imagen._extract_retry_after_seconds(Exception("retry-after: 2.5")), 2.5
        )

    def test_openai_adapter_disables_sdk_retries_and_applies_provider_options(self):
        captured = {}

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        fake_module = type(sys)("openai")
        fake_module.OpenAI = FakeOpenAI
        with mock.patch.dict(sys.modules, {"openai": fake_module}):
            imagen._create_client(
                "https://example.test/v1",
                "key",
                timeout=9.0,
                extra_headers={"X-Test": "yes"},
            )
        self.assertEqual(captured["max_retries"], 0)
        self.assertEqual(captured["timeout"], 9.0)
        self.assertEqual(captured["default_headers"], {"X-Test": "yes"})

    def test_dry_run_does_not_create_output_directory(self):
        output = self.work / "new" / "nested"
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--prompt",
            "Test",
            "--out-dir",
            str(output),
            "--dry-run",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(output.exists())

    def test_output_compression_requires_jpeg_or_webp(self):
        png = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--prompt",
            "Test",
            "--output-format",
            "png",
            "--output-compression",
            "80",
            "--dry-run",
        )
        self.assertNotEqual(png.returncode, 0)
        self.assertIn("only valid with JPEG or WebP", png.stderr)

        jpeg = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--prompt",
            "Test",
            "--output-format",
            "jpeg",
            "--output-compression",
            "80",
            "--out",
            str(self.work / "ok.jpg"),
            "--dry-run",
        )
        self.assertEqual(jpeg.returncode, 0, jpeg.stderr)
        self.assertNotIn("does not match", jpeg.stderr)

    def test_out_and_out_dir_are_mutually_exclusive(self):
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--prompt",
            "Test",
            "--out",
            "one.png",
            "--out-dir",
            "output",
            "--dry-run",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_input_paths_must_be_regular_files(self):
        result = self.run_cli(
            "generate",
            "--config",
            str(self.config),
            "--prompt-file",
            str(self.work),
            "--dry-run",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a regular file", result.stderr)

        result = self.run_cli(
            "edit",
            "--config",
            str(self.config),
            "--image",
            str(self.work),
            "--prompt",
            "Test",
            "--dry-run",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a regular file", result.stderr)

    def test_images_over_50mb_are_rejected(self):
        image = self.work / "large.png"
        with image.open("wb") as handle:
            handle.truncate(imagen.MAX_IMAGE_BYTES + 1)
        with self.assertRaises(imagen.ImagenError) as raised:
            imagen._check_image_paths([str(image)])
        self.assertIn("exceeds 50MB", str(raised.exception))

    def test_invalid_config_json_url_env_and_duplicate_models(self):
        self.config.write_text("{broken", encoding="utf-8")
        result = self.run_cli("providers", "--config", str(self.config))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid provider config JSON", result.stderr)

        cases = [
            ({"url": "ftp://example.test", "api_key_env": "KEY", "models": ["m"]}, "invalid HTTP(S) URL"),
            ({"url": "https://example.test", "api_key_env": "BAD-NAME", "models": ["m"]}, "valid environment variable"),
            ({"url": "https://example.test", "api_key_env": "KEY", "models": ["m", "m"]}, "duplicate model ID"),
        ]
        for provider, message in cases:
            self.config.write_text(json.dumps({"providers": {"only": provider}}), encoding="utf-8")
            result = self.run_cli("providers", "--config", str(self.config))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(message, result.stderr)

    def test_single_provider_and_model_are_auto_selected(self):
        self.config.write_text(
            json.dumps(
                {
                    "providers": {
                        "only": {
                            "url": "https://only.example/v1",
                            "api_key_env": "ONLY_KEY",
                            "models": ["only-model"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = self.run_cli(
            "generate", "--config", str(self.config), "--prompt", "Test", "--dry-run"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["provider"], "only")
        self.assertEqual(payload["model"], "only-model")

    def test_complete_config_path_precedence(self):
        home = self.work / "home"
        default_config = home / ".config" / "imagen" / "providers.json"
        default_config.parent.mkdir(parents=True)

        def write_config(path, provider_name, model):
            path.write_text(
                json.dumps(
                    {
                        "providers": {
                            provider_name: {
                                "url": f"https://{provider_name}.example/v1",
                                "api_key_env": f"{provider_name.upper()}_KEY",
                                "models": [model],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

        write_config(default_config, "home", "home-model")
        env_config = self.work / "env.json"
        write_config(env_config, "environment", "env-model")
        explicit_config = self.work / "explicit.json"
        write_config(explicit_config, "explicit", "explicit-model")

        env = dict(self.env, HOME=str(home))
        default = self.run_cli("generate", "--prompt", "Test", "--dry-run", env=env)
        self.assertEqual(json.loads(default.stdout)["provider"], "home")

        env["IMAGEN_CONFIG_PATH"] = str(env_config)
        from_env = self.run_cli("generate", "--prompt", "Test", "--dry-run", env=env)
        self.assertEqual(json.loads(from_env.stdout)["provider"], "environment")

        explicit = self.run_cli(
            "generate",
            "--config",
            str(explicit_config),
            "--prompt",
            "Test",
            "--dry-run",
            env=env,
        )
        self.assertEqual(json.loads(explicit.stdout)["provider"], "explicit")

    def test_direct_api_keys_pass_config_check_without_being_printed(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        secrets = {
            "primary": "primary-direct-secret",
            "backup": "backup-direct-secret",
        }
        for name, secret in secrets.items():
            data["providers"][name].pop("api_key_env")
            data["providers"][name]["api_key"] = secret
        self.config.write_text(json.dumps(data), encoding="utf-8")

        result = self.run_cli("config-check", "--config", str(self.config))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout + result.stderr
        self.assertNotIn(secrets["primary"], output)
        self.assertNotIn(secrets["backup"], output)
        rows = json.loads(result.stdout)["providers"]
        self.assertTrue(all(row["credential_source"] == "config" for row in rows))
        self.assertTrue(all(row["key_configured"] for row in rows))
        self.assertTrue(all(row["api_key_env"] is None for row in rows))

    def test_provider_requires_exactly_one_credential_field(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["providers"]["primary"].pop("api_key_env")
        self.config.write_text(json.dumps(data), encoding="utf-8")
        missing = self.run_cli("providers", "--config", str(self.config))
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("exactly one of 'api_key_env' or 'api_key'", missing.stderr)

        data["providers"]["primary"]["api_key_env"] = "PRIMARY_TEST_IMAGE_KEY"
        data["providers"]["primary"]["api_key"] = "direct-secret"
        self.config.write_text(json.dumps(data), encoding="utf-8")
        both = self.run_cli("providers", "--config", str(self.config))
        self.assertNotEqual(both.returncode, 0)
        self.assertIn("exactly one of 'api_key_env' or 'api_key'", both.stderr)
        self.assertNotIn("direct-secret", both.stderr)


class ImagenP0SafetyTests(ImagenProviderConfigTests):
    def _run_main(self, *args):
        argv = [str(SCRIPT), *args]
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(
            os.environ, {"PRIMARY_TEST_IMAGE_KEY": "test-key"}
        ):
            return imagen.main()

    def test_live_request_accepts_direct_api_key(self):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        direct_key = "primary-direct-secret"
        data["providers"]["primary"].pop("api_key_env")
        data["providers"]["primary"]["api_key"] = direct_key
        self.config.write_text(json.dumps(data), encoding="utf-8")
        encoded = base64.b64encode(b"direct-key-image").decode("ascii")

        class FakeImages:
            def generate(self, **payload):
                return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])

        output = self.work / "direct.png"
        client = SimpleNamespace(images=FakeImages())
        with mock.patch.object(imagen, "_create_client", return_value=client) as create_client:
            self._run_main(
                "generate",
                "--config",
                str(self.config),
                "--prompt",
                "Test",
                "--out",
                str(output),
            )
        self.assertEqual(output.read_bytes(), b"direct-key-image")
        self.assertEqual(create_client.call_args.args[1], direct_key)

    def test_entire_batch_is_preflighted_before_client_creation(self):
        jobs = self.work / "jobs.jsonl"
        jobs.write_text(
            '{"prompt":"valid"}\n'
            '{"prompt":"invalid","model":"not-configured"}\n',
            encoding="utf-8",
        )
        with mock.patch.object(imagen, "_create_async_client") as create_client:
            with self.assertRaises(imagen.BatchJobError) as raised:
                self._run_main(
                    "generate-batch",
                    "--config",
                    str(self.config),
                    "--input",
                    str(jobs),
                    "--out-dir",
                    str(self.work / "output"),
                )
        self.assertEqual(raised.exception.job_index, 2)
        self.assertEqual(raised.exception.exit_code, imagen.EXIT_VALIDATION_ERROR)
        create_client.assert_not_called()
        self.assertFalse((self.work / "output").exists())

    def test_duplicate_batch_outputs_are_rejected_even_with_force(self):
        jobs = self.work / "jobs.jsonl"
        jobs.write_text(
            '{"prompt":"one","out":"same.png"}\n'
            '{"prompt":"two","out":"same.png"}\n',
            encoding="utf-8",
        )
        with self.assertRaises(imagen.ImagenError) as raised:
            self._run_main(
                "generate-batch",
                "--config",
                str(self.config),
                "--input",
                str(jobs),
                "--out-dir",
                str(self.work / "output"),
                "--force",
            )
        self.assertIn("Duplicate output path", str(raised.exception))

    def test_empty_downscale_suffix_is_rejected(self):
        with self.assertRaises(imagen.ImagenError) as raised:
            self._run_main(
                "generate",
                "--config",
                str(self.config),
                "--prompt",
                "test",
                "--out",
                str(self.work / "same.png"),
                "--downscale-max-dim",
                "100",
                "--downscale-suffix",
                "",
                "--dry-run",
            )
        self.assertIn("Duplicate output path", str(raised.exception))

    def test_existing_output_is_found_before_batch_client_creation(self):
        output = self.work / "output"
        output.mkdir()
        (output / "001-valid.png").write_bytes(b"existing")
        jobs = self.work / "jobs.jsonl"
        jobs.write_text('{"prompt":"valid"}\n', encoding="utf-8")
        with mock.patch.object(imagen, "_create_async_client") as create_client:
            with self.assertRaises(imagen.ImagenError) as raised:
                self._run_main(
                    "generate-batch",
                    "--config",
                    str(self.config),
                    "--input",
                    str(jobs),
                    "--out-dir",
                    str(output),
                )
        self.assertIn("Output already exists", str(raised.exception))
        create_client.assert_not_called()

    def test_response_count_mismatch_writes_nothing(self):
        outputs = [self.work / "one.png", self.work / "two.png"]
        encoded = base64.b64encode(b"image").decode("ascii")
        with self.assertRaises(imagen.ImagenError) as raised:
            imagen._decode_write_and_downscale(
                [encoded],
                outputs,
                force=False,
                downscale_max_dim=None,
                downscale_suffix="-web",
                output_format="png",
            )
        self.assertIn("1 image(s), but 2 were requested", str(raised.exception))
        self.assertFalse(any(path.exists() for path in outputs))

    def test_invalid_base64_writes_nothing(self):
        output = self.work / "bad.png"
        with self.assertRaises(imagen.ImagenError):
            imagen._decode_write_and_downscale(
                ["not base64!"],
                [output],
                force=False,
                downscale_max_dim=None,
                downscale_suffix="-web",
                output_format="png",
            )
        self.assertFalse(output.exists())

    def test_atomic_writer_replaces_complete_file_with_force(self):
        output = self.work / "atomic.png"
        output.write_bytes(b"old")
        imagen._atomic_write_bundle([(output, b"new-complete")], force=True)
        self.assertEqual(output.read_bytes(), b"new-complete")
        self.assertEqual(list(self.work.glob(".*.tmp")), [])

    def test_fake_client_generates_single_and_multiple_outputs(self):
        encoded = base64.b64encode(b"fake-image").decode("ascii")

        class FakeImages:
            def __init__(self):
                self.requests = []

            def generate(self, **payload):
                self.requests.append(payload)
                return SimpleNamespace(
                    data=[SimpleNamespace(b64_json=encoded) for _ in range(payload["n"])]
                )

        images = FakeImages()
        client = SimpleNamespace(images=images)
        output = self.work / "result.png"
        with mock.patch.object(imagen, "_create_client", return_value=client):
            self._run_main(
                "generate",
                "--config",
                str(self.config),
                "--prompt",
                "Test",
                "--n",
                "2",
                "--out",
                str(output),
            )
        first = self.work / "result-1.png"
        second = self.work / "result-2.png"
        self.assertEqual(first.read_bytes(), b"fake-image")
        self.assertEqual(second.read_bytes(), b"fake-image")
        self.assertEqual(images.requests[0]["n"], 2)
        self.assertNotIn("size", images.requests[0])

    def test_fake_client_failure_becomes_cli_error(self):
        class FakeImages:
            def generate(self, **payload):
                raise TimeoutError("provider timed out")

        client = SimpleNamespace(images=FakeImages())
        with mock.patch.object(imagen, "_create_client", return_value=client):
            with self.assertRaises(imagen.ImagenError) as raised:
                self._run_main(
                    "generate",
                    "--config",
                    str(self.config),
                    "--prompt",
                    "Test",
                    "--out",
                    str(self.work / "never.png"),
                )
        self.assertIn("provider timed out", str(raised.exception))
        self.assertFalse((self.work / "never.png").exists())

    def test_edit_passes_multiple_images_and_closes_all_files(self):
        encoded = base64.b64encode(b"edited").decode("ascii")
        input_one = self.work / "one.png"
        input_two = self.work / "two.png"
        mask = self.work / "mask.png"
        for path in (input_one, input_two, mask):
            path.write_bytes(b"input")

        class FakeImages:
            handles = []

            def edit(self, **payload):
                self.handles = [*payload["image"], payload["mask"]]
                return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])

        images = FakeImages()
        client = SimpleNamespace(images=images)
        output = self.work / "edited.png"
        with mock.patch.object(imagen, "_create_client", return_value=client):
            self._run_main(
                "edit",
                "--config",
                str(self.config),
                "--prompt",
                "Edit",
                "--image",
                str(input_one),
                "--image",
                str(input_two),
                "--mask",
                str(mask),
                "--out",
                str(output),
            )
        self.assertEqual(output.read_bytes(), b"edited")
        self.assertTrue(all(handle.closed for handle in images.handles))

    def test_downscale_creates_resized_image(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        source = self.work / "source.png"
        image = Image.new("RGB", (20, 10), "red")
        image.save(source)
        raw = source.read_bytes()
        output = self.work / "full.png"
        imagen._decode_write_and_downscale(
            [base64.b64encode(raw).decode("ascii")],
            [output],
            force=False,
            downscale_max_dim=5,
            downscale_suffix="-web",
            output_format="png",
        )
        with Image.open(self.work / "full-web.png") as resized:
            self.assertEqual(resized.size, (5, 2))

    def test_retry_loop_handles_rate_limit_timeout_and_max_attempts(self):
        class SequenceAdapter(imagen.ImageProviderAdapter):
            def __init__(self, outcomes):
                self.outcomes = list(outcomes)
                self.calls = 0

            async def generate_async(self, client, payload):
                outcome = self.outcomes[self.calls]
                self.calls += 1
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        success = object()
        adapter = SequenceAdapter([TimeoutError("timeout"), success])
        with mock.patch.object(imagen.asyncio, "sleep", new=mock.AsyncMock()) as sleep:
            result = imagen.asyncio.run(
                imagen._generate_one_with_retries(
                    object(), {}, attempts=3, job_label="job", adapter=adapter
                )
            )
        self.assertIs(result, success)
        self.assertEqual(adapter.calls, 2)
        sleep.assert_awaited_once()

        limited = SequenceAdapter(
            [RuntimeError("429 retry-after: 0.25"), RuntimeError("429"), RuntimeError("429")]
        )
        with mock.patch.object(imagen.asyncio, "sleep", new=mock.AsyncMock()) as sleep:
            with self.assertRaises(RuntimeError):
                imagen.asyncio.run(
                    imagen._generate_one_with_retries(
                        object(), {}, attempts=3, job_label="job", adapter=limited
                    )
                )
        self.assertEqual(limited.calls, 3)
        self.assertEqual(sleep.await_args_list[0].args[0], 0.25)

    def test_fail_fast_cancels_job_waiting_for_semaphore(self):
        plans = [
            imagen.BatchJobPlan(1, {"prompt": "first", "n": 1}, [self.work / "1.png"], "png"),
            imagen.BatchJobPlan(2, {"prompt": "second", "n": 1}, [self.work / "2.png"], "png"),
        ]
        batch_plan = imagen.BatchPlan(plans, [self.work / "1.png", self.work / "2.png"])

        class FailingAdapter(imagen.ImageProviderAdapter):
            def __init__(self):
                self.calls = []

            async def generate_async(self, client, payload):
                self.calls.append(payload["prompt"])
                raise RuntimeError("stop")

        adapter = FailingAdapter()
        args = SimpleNamespace(
            dry_run=False,
            api_url="https://example.test/v1",
            api_key="key",
            timeout=None,
            extra_headers={},
            adapter="fake",
            concurrency=1,
            max_attempts=1,
            fail_fast=True,
            force=False,
            downscale_max_dim=None,
            downscale_suffix="-web",
        )
        with mock.patch.object(imagen, "_preflight_generate_batch", return_value=batch_plan), mock.patch.object(
            imagen, "_create_async_client", return_value=object()
        ), mock.patch.object(imagen, "_get_adapter", return_value=adapter):
            with self.assertRaises(imagen.BatchJobError):
                imagen.asyncio.run(imagen._run_generate_batch(args))
        self.assertEqual(adapter.calls, ["first"])


class ImagenGoogleAdapterTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self.payload

    def setUp(self):
        self.adapter = imagen.GoogleGenerateContentAdapter()
        self.client = imagen._GoogleGenerateContentClient(
            base_url="https://generativelanguage.googleapis.com/v1",
            api_key="google-test-key",
            timeout=30,
            extra_headers={"X-Test": "yes"},
        )
        self.image_b64 = base64.b64encode(b"google-image").decode("ascii")
        self.response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "thought": True,
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": base64.b64encode(b"thought").decode("ascii"),
                                },
                            },
                            {"text": "Generated image"},
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": self.image_b64,
                                }
                            },
                        ]
                    }
                }
            ]
        }

    def test_generate_content_request_and_response(self):
        payload = {
            "model": "gemini-3.1-flash-image",
            "prompt": "Create a landscape",
            "n": 1,
            "aspect_ratio": "16:9",
            "image_size": "2K",
        }
        with mock.patch.object(
            imagen, "urlopen", return_value=self.FakeResponse(self.response)
        ) as urlopen:
            result = self.adapter.generate(self.client, payload)

        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(
            request.full_url,
            "https://generativelanguage.googleapis.com/v1/models/"
            "gemini-3.1-flash-image:generateContent",
        )
        self.assertEqual(request.get_header("X-goog-api-key"), "google-test-key")
        self.assertEqual(request.get_header("X-test"), "yes")
        self.assertEqual(body["contents"][0]["parts"], [{"text": "Create a landscape"}])
        self.assertEqual(body["generationConfig"]["responseModalities"], ["IMAGE"])
        self.assertEqual(
            body["generationConfig"]["responseFormat"]["image"],
            {"aspectRatio": "16:9", "imageSize": "2K"},
        )
        self.assertEqual([item.b64_json for item in result.data], [self.image_b64])

    def test_google_edit_sends_inline_images(self):
        with tempfile.TemporaryDirectory() as tempdir:
            image_path = Path(tempdir) / "input.png"
            image_path.write_bytes(b"input-image")
            with image_path.open("rb") as image_file, mock.patch.object(
                imagen, "urlopen", return_value=self.FakeResponse(self.response)
            ) as urlopen:
                result = self.adapter.edit(
                    self.client,
                    {
                        "model": "gemini-3.1-flash-image",
                        "prompt": "Change only the sky",
                        "n": 1,
                        "image": image_file,
                    },
                )

        body = json.loads(urlopen.call_args.args[0].data)
        parts = body["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": "Change only the sky"})
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/png")
        self.assertEqual(
            base64.b64decode(parts[1]["inline_data"]["data"]), b"input-image"
        )
        self.assertEqual(result.data[0].b64_json, self.image_b64)

    def test_google_adapter_rejects_unsupported_options_before_network(self):
        base_payload = {
            "model": "gemini-3.1-flash-image",
            "prompt": "Test",
            "n": 1,
        }
        invalid_payloads = [
            {**base_payload, "n": 2},
            {**base_payload, "quality": "high"},
            {**base_payload, "aspect_ratio": "7:5"},
            {**base_payload, "image_size": "2k"},
            {**base_payload, "output_format": "jpeg"},
            {
                **base_payload,
                "model": "gemini-2.5-flash-image",
                "image_size": "2K",
            },
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(imagen.ImagenError):
                self.adapter.validate_generate_payload(payload)
        with self.assertRaises(imagen.ImagenError):
            self.adapter.validate_edit_payload(base_payload, has_mask=True)
        self.assertTrue(
            imagen._is_transient_error(
                RuntimeError("Google generateContent returned HTTP 503: unavailable")
            )
        )

    def test_google_provider_dry_run_uses_generate_content_endpoint(self):
        with tempfile.TemporaryDirectory() as tempdir:
            work = Path(tempdir)
            config = work / "providers.json"
            config.write_text(
                json.dumps(
                    {
                        "providers": {
                            "google": {
                                "adapter": "google_generate_content",
                                "url": "https://generativelanguage.googleapis.com/v1",
                                "api_key_env": "UNSET_GEMINI_TEST_KEY",
                                "models": ["gemini-3.1-flash-image"],
                                "defaults": {
                                    "aspect_ratio": "16:9",
                                    "image_size": "2K",
                                },
                                "supported_params": ["aspect_ratio", "image_size"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.pop("UNSET_GEMINI_TEST_KEY", None)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "generate",
                    "--config",
                    str(config),
                    "--prompt",
                    "Test",
                    "--dry-run",
                ],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        preview = json.loads(result.stdout)
        self.assertEqual(preview["adapter"], "google_generate_content")
        self.assertEqual(
            preview["endpoint"],
            "/models/gemini-3.1-flash-image:generateContent",
        )
        self.assertEqual(preview["aspect_ratio"], "16:9")
        self.assertEqual(preview["image_size"], "2K")


if __name__ == "__main__":
    unittest.main()
