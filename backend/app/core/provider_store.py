"""Local provider metadata + OS credential-store integration.

Provider secrets never enter the browser or the metadata JSON. Windows uses
Credential Manager through keyring; Linux/macOS use the configured keyring backend.
"""
import json
import re
from pathlib import Path
from urllib.parse import urlparse

STORE_PATH = Path.home() / ".code-conductor" / "providers.json"
SERVICE_NAME = "code-conductor-provider"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,47}$")


def _load() -> list[dict]:
    try:
        raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")
    try:
        STORE_PATH.chmod(0o600)
    except OSError:
        pass


def validate_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.hostname:
        return value
    # Plain HTTP is permitted only for local inference servers such as LM Studio.
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return value
    raise ValueError("Use an HTTPS endpoint, or HTTP only for localhost/127.0.0.1/::1.")


def list_providers() -> list[dict]:
    return [{**item, "configured": True, "key_configured": _has_key(item["id"])} for item in _load()]


def _has_key(provider_id: str) -> bool:
    try:
        import keyring
        return bool(keyring.get_password(SERVICE_NAME, provider_id))
    except Exception:
        return False


def get_key(provider_id: str) -> str | None:
    try:
        import keyring
        return keyring.get_password(SERVICE_NAME, provider_id)
    except Exception:
        return None


def upsert_provider(*, provider_id: str, name: str, base_url: str, model: str, api_key: str | None) -> dict:
    provider_id = provider_id.strip().lower()
    if not ID_RE.fullmatch(provider_id):
        raise ValueError("Provider ID must be 2–48 lowercase letters, numbers, underscores or hyphens.")
    name = name.strip()
    model = model.strip()
    if not name or not model:
        raise ValueError("Provider name and model are required.")
    base_url = validate_base_url(base_url)
    try:
        import keyring
        # Verify a usable OS credential backend before writing any metadata.
        keyring.get_keyring()
        if api_key and api_key.strip():
            keyring.set_password(SERVICE_NAME, provider_id, api_key.strip())
        elif not keyring.get_password(SERVICE_NAME, provider_id):
            raise ValueError("Provide an API key, or an existing key must already be stored for this provider.")
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError("The OS credential store is unavailable. Install/configure keyring support before adding API keys.") from exc

    items = _load()
    item = {"id": provider_id, "name": name, "base_url": base_url, "model": model}
    items = [existing for existing in items if existing.get("id") != provider_id]
    items.append(item)
    _save(items)
    return {**item, "configured": True, "key_configured": True}


def delete_provider(provider_id: str) -> bool:
    items = _load()
    remaining = [item for item in items if item.get("id") != provider_id]
    if len(remaining) == len(items):
        return False
    _save(remaining)
    try:
        import keyring
        keyring.delete_password(SERVICE_NAME, provider_id)
    except Exception:
        pass
    return True
