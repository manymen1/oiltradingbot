from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


CODEX_CLI_PROVIDERS = {"codex_cli", "codex-cli", "codex"}


def is_codex_cli_provider(provider: str) -> bool:
    return provider.strip().casefold() in CODEX_CLI_PROVIDERS


def codex_cli_available(cli_binary: str = "codex") -> bool:
    return shutil.which(_binary(cli_binary)) is not None


def run_codex_cli(
    prompt: str,
    *,
    model: str,
    output_schema: dict[str, Any],
    cli_binary: str = "codex",
    timeout_seconds: int = 180,
) -> str:
    binary = _binary(cli_binary)
    with tempfile.TemporaryDirectory(
        prefix="polybot-codex-classifier-"
    ) as temporary:
        schema_path = Path(temporary) / "schema.json"
        output_path = Path(temporary) / "last-message.json"
        schema_path.write_text(
            json.dumps(output_schema),
            encoding="utf-8",
        )
        try:
            completed = subprocess.run(
                [
                    binary,
                    "exec",
                    "--ephemeral",
                    "--ignore-rules",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "--model",
                    model,
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "-",
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"codex CLI binary {binary!r} not found; "
                "install Codex CLI and run `codex login`"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"codex CLI timed out after {timeout_seconds}s"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            # Codex emits startup warnings before the actionable terminal
            # error. Keeping the tail prevents plugin warnings from hiding
            # schema, authentication, or model failures.
            if len(detail) > 2_000:
                detail = "..." + detail[-2_000:]
            raise RuntimeError(
                f"codex CLI exited {completed.returncode}: {detail}"
            )
        if output_path.exists():
            return output_path.read_text(encoding="utf-8")
        return completed.stdout


def extract_codex_cli_result(stdout: str) -> str:
    text = stdout.strip()
    if not text:
        raise RuntimeError("codex CLI returned no result text")
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"unexpected codex CLI output shape: {stdout[:300]!r}"
        )
    return json.dumps(parsed)


def _binary(configured: str) -> str:
    override = os.getenv("CODEX_CLI_BINARY")
    if override:
        return override
    if not configured or configured == "claude":
        return "codex"
    return configured


__all__ = [
    "CODEX_CLI_PROVIDERS",
    "codex_cli_available",
    "extract_codex_cli_result",
    "is_codex_cli_provider",
    "run_codex_cli",
]
