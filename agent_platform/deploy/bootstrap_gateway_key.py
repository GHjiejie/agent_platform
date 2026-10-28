"""Run inside the LiteLLM container; never print the master or generated key."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ALLOWED_ROUTES = ["/responses", "/v1/responses"]


def control_request(
    base: str, master: str, path: str, body: dict | None = None
) -> dict:
    request = Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + master,
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise TypeError("Expected an object")
        return result
    except HTTPError as exc:
        # Never include the upstream body, credentials or URL query in errors.
        raise RuntimeError(
            f"Gateway control request {path.split('?')[0]} failed with HTTP {exc.code}; no automatic retry."
        ) from None
    except (URLError, TimeoutError):
        raise RuntimeError("Gateway unavailable; no automatic retry.") from None
    except (TypeError, ValueError, UnicodeError):
        raise RuntimeError("Gateway returned an invalid control response.") from None


def key_limits() -> dict:
    limits = {
        "max_budget": float(os.getenv("GATEWAY_KEY_MAX_BUDGET", "100")),
        "rpm_limit": int(os.getenv("GATEWAY_KEY_RPM", "60")),
        "tpm_limit": int(os.getenv("GATEWAY_KEY_TPM", "120000")),
        "max_parallel_requests": int(os.getenv("GATEWAY_KEY_CONCURRENT", "4")),
    }
    if any(not math.isfinite(value) or value <= 0 for value in limits.values()):
        raise ValueError(
            "Gateway budget and rate limits must be finite positive values."
        )
    return limits


def valid_key(key: object, master: str) -> bool:
    return (
        isinstance(key, str)
        and key.startswith("sk-")
        and key != master
        and len(key) > 3
        and not any(character.isspace() for character in key)
    )


def key_models(alias: str) -> list[str]:
    """Require explicit model names and keep the default alias in scope."""
    if re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,200}", alias) is None:
        raise ValueError("An explicit permitted model alias is required.")
    configured = os.getenv("GATEWAY_KEY_MODELS")
    if configured is None:
        return [alias]
    models = [item.strip() for item in configured.split(",")]
    if (
        any(ord(character) < 32 or ord(character) == 127 for character in configured)
        or any(
            re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,200}", item) is None for item in models
        )
        or len(models) != len(set(models))
        or alias not in models
    ):
        raise ValueError(
            "GATEWAY_KEY_MODELS must contain unique explicit model aliases, "
            "including LITELLM_MODEL_ALIAS, with no empty entries or wildcards."
        )
    return models


def same_models(actual: object, expected: list[str]) -> bool:
    return (
        isinstance(actual, list)
        and all(isinstance(item, str) for item in actual)
        and len(actual) == len(set(actual))
        and set(actual) == set(expected)
    )


def validate_existing(
    output: Path, base: str, master: str, models: list[str], limits: dict
) -> None:
    lines = [
        line
        for line in output.read_text().splitlines()
        if line and not line.startswith("#")
    ]
    if len(lines) != 1 or not lines[0].startswith("PLATFORM_LITELLM_KEY="):
        raise RuntimeError(
            "Existing key file is invalid; explicit key rotation is required."
        )
    key = lines[0].split("=", 1)[1]
    if not valid_key(key, master):
        raise RuntimeError(
            "Existing key is invalid or is the master key; explicit rotation is required."
        )

    # POST keeps the stored credential out of access-log URLs. This admin-only
    # endpoint is available in pinned LiteLLM 1.83.0.
    result = control_request(base, master, "/v2/key/info", {"keys": [key]})
    records = result.get("info")
    if (
        not isinstance(records, list)
        or len(records) != 1
        or not isinstance(records[0], dict)
    ):
        raise RuntimeError(
            "Existing key was not found; explicit key rotation is required."
        )
    info = records[0]
    try:
        expires = datetime.fromisoformat(info["expires"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise RuntimeError(
            "Existing key has no valid expiry; explicit key rotation is required."
        ) from None
    if expires <= datetime.now(UTC):
        raise RuntimeError(
            "Existing key has expired; explicitly rotate it before restarting. No new key was created."
        )
    if not same_models(info.get("models"), models) or set(
        info.get("allowed_routes") or []
    ) != set(ALLOWED_ROUTES):
        raise RuntimeError(
            "Existing key model or route permissions do not match; update or rotate it explicitly."
        )
    if info.get("blocked") or any(
        info.get(field) for field in ("aliases", "config", "permissions")
    ):
        raise RuntimeError(
            "Existing key is blocked or has custom permissions; review it before reuse."
        )
    for field, configured in limits.items():
        try:
            actual = float(info[field])
        except (KeyError, ValueError, TypeError):
            raise RuntimeError(
                f"Existing key has no valid {field}; update it before reuse."
            ) from None
        if not math.isfinite(actual) or not 0 < actual <= configured:
            raise RuntimeError(
                f"Existing key {field} exceeds configured limits; update it before reuse."
            )
    if float(info.get("spend") or 0) >= float(info["max_budget"]):
        raise RuntimeError(
            "Existing key budget is exhausted; adjust its budget explicitly before reuse."
        )
    user_id = info.get("user_id")
    if not isinstance(user_id, str) or not user_id.startswith(
        "agent-platform-runtime-"
    ):
        raise RuntimeError("Existing key is not owned by the platform runtime user.")
    owner = control_request(
        base, master, "/user/info?" + urlencode({"user_id": user_id})
    )
    user = owner.get("user_info") or {}
    if user.get("user_role") != "internal_user" or not same_models(
        user.get("models"), models
    ):
        raise RuntimeError(
            "Existing key owner has unexpected permissions; review it before reuse."
        )


def bootstrap(*, reuse_existing: bool = False) -> Path:
    output = Path(os.getenv("GATEWAY_KEY_OUTPUT", "/bootstrap/.env.gateway"))
    if output.exists() and not reuse_existing:
        raise RuntimeError(
            "Key file already exists; use --reuse-existing to validate it, or rotate explicitly."
        )
    master = os.environ["LITELLM_MASTER_KEY"]
    alias = os.environ.get("LITELLM_MODEL_ALIAS", "platform-chat")
    models = key_models(alias)
    base = os.getenv("GATEWAY_BOOTSTRAP_URL", "http://127.0.0.1:4000").rstrip("/")
    limits = key_limits()
    if output.exists():
        validate_existing(output, base, master, models, limits)
        return output

    def post(path: str, body: dict) -> dict:
        return control_request(base, master, path, body)

    user_id = "agent-platform-runtime-" + uuid.uuid4().hex
    post(
        "/user/new",
        {
            "user_id": user_id,
            "user_alias": "Agent platform runtime",
            "user_role": "internal_user",
            "auto_create_key": False,
            "models": models,
        },
    )
    result = post(
        "/key/generate",
        {
            "user_id": user_id,
            "key_alias": "Agent platform runtime",
            "models": models,
            # In 1.83.0, llm_api overwrites allowed_routes with all LLM routes.
            "key_type": "default",
            "allowed_routes": ALLOWED_ROUTES,
            **limits,
            "duration": "30d",
        },
    )
    key = result.get("key")
    if not valid_key(key, master):
        raise RuntimeError("Gateway did not return a distinct valid virtual key.")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as target:
        target.write(f"PLATFORM_LITELLM_KEY={key}\n")
    if "GATEWAY_KEY_UID" in os.environ and "GATEWAY_KEY_GID" in os.environ:
        os.chown(
            output,
            int(os.environ["GATEWAY_KEY_UID"]),
            int(os.environ["GATEWAY_KEY_GID"]),
        )
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Validate and reuse an existing restricted key without creating or rotating keys.",
    )
    args = parser.parse_args()
    try:
        destination = bootstrap(reuse_existing=args.reuse_existing)
    except (KeyError, ValueError, RuntimeError, OSError) as error:
        print(f"Key bootstrap failed: {error}", file=sys.stderr)
        sys.exit(1)
    print(
        f"Restricted runtime key ready at {destination}; existing keys retain their original expiry."
    )
