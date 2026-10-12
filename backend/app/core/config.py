import json
import os
import secrets
from pathlib import Path
from pydantic import BaseModel, Field

def load_environment_files() -> None:
    """Discovers and loads .env files from project root, backend dir, or user home."""
    try:
        from dotenv import load_dotenv
        candidates = [
            Path.cwd() / ".env",
            Path.cwd().parent / ".env",
            Path(__file__).resolve().parent.parent.parent.parent / ".env",  # project root
            Path(__file__).resolve().parent.parent.parent / ".env",         # backend root
            Path.home() / ".aihub.env",
            Path.home() / ".env",
        ]
        seen = set()
        for p in candidates:
            resolved = p.resolve()
            if resolved not in seen and resolved.exists() and resolved.is_file():
                seen.add(resolved)
                load_dotenv(dotenv_path=resolved, override=False)
    except Exception:
        pass

# Automatically load environment variables on import
load_environment_files()

def get_or_create_token() -> str:
    token_file = Path.home() / ".aihub_token"
    env_token = os.environ.get("AIHUB_AUTH_TOKEN")
    if env_token and len(env_token.strip()) >= 16:
        token = env_token.strip()
        try:
            token_file.write_text(token, encoding="utf-8")
        except Exception:
            pass
        return token

    if token_file.exists():
        try:
            existing = token_file.read_text(encoding="utf-8").strip()
            if len(existing) >= 16:
                return existing
        except Exception:
            pass

    new_token = secrets.token_hex(32)
    try:
        token_file.write_text(new_token, encoding="utf-8")
    except Exception:
        pass
    return new_token

class Settings(BaseModel):
    app_name: str = "Unified AI Coding Hub"
    version: str = "0.1.0"
    host: str = Field(default_factory=lambda: os.environ.get("AIHUB_HOST", "127.0.0.1"))
    port: int = Field(default_factory=lambda: int(os.environ.get("AIHUB_PORT", 8765)))
    auth_token: str = Field(default_factory=get_or_create_token)
    workspace_root: str = Field(default_factory=lambda: os.getcwd())
    log_level: str = "INFO"

_settings: Settings | None = None

def get_settings() -> Settings:
    global _settings
    if _settings is None:
        load_environment_files()
        _settings = Settings()
    return _settings

def set_auth_token(token: str) -> None:
    settings = get_settings()
    settings.auth_token = token
    token_file = Path.home() / ".aihub_token"
    try:
        token_file.write_text(token, encoding="utf-8")
    except Exception:
        pass

def get_unified_api_config() -> dict:
    """
    Unified configuration provider merging:
    1. Environment variables (.env file)
    2. ~/.gemini/config/api_keys.json (legacy global key store)
    """
    load_environment_files()
    config = {}

    # 1. Baseline from ~/.gemini/config/api_keys.json
    gemini_config_path = Path.home() / ".gemini" / "config" / "api_keys.json"
    if gemini_config_path.exists():
        try:
            config = json.loads(gemini_config_path.read_text(encoding="utf-8"))
        except Exception:
            config = {}

    def _parse_keys(env_single: str, env_multi: str) -> list[str]:
        keys: list[str] = []
        val_multi = os.environ.get(env_multi, "")
        if val_multi:
            keys.extend([k.strip() for k in val_multi.split(",") if k.strip()])
        val_single = os.environ.get(env_single, "")
        if val_single and val_single.strip() not in keys:
            keys.insert(0, val_single.strip())
        return keys

    # 2. Overlay / Inject keys from .env
    groq_keys = _parse_keys("GROQ_API_KEY", "GROQ_API_KEYS")
    if groq_keys:
        config.setdefault("groq", {})["keys"] = groq_keys
        if "base_url" not in config["groq"]:
            config["groq"]["base_url"] = "https://api.groq.com/openai/v1"
        if "default_model" not in config["groq"]:
            config["groq"]["default_model"] = "qwen/qwen3.8-27b"

    codecraft_keys = _parse_keys("CODECRAFT_API_KEY", "CODECRAFT_API_KEYS")
    if codecraft_keys:
        config.setdefault("codecraft", {})["keys"] = codecraft_keys
        if "base_url" not in config["codecraft"]:
            config["codecraft"]["base_url"] = "https://codecraftapi.com/v1"
        if "default_model" not in config["codecraft"]:
            config["codecraft"]["default_model"] = "gemma-2-2b"

    apmix_keys = _parse_keys("APMIX_API_KEY", "APMIX_API_KEYS")
    if apmix_keys:
        config.setdefault("apmix", {})["keys"] = apmix_keys
        if "base_url" not in config["apmix"]:
            config["apmix"]["base_url"] = "https://api.apmix.ai/v1"

    xpl_keys = _parse_keys("EXPERIENTIALLABS_API_KEY", "EXPERIENTIALLABS_API_KEYS")
    if xpl_keys:
        config.setdefault("experientiallabs", {})["keys"] = xpl_keys
        if "base_url" not in config["experientiallabs"]:
            config["experientiallabs"]["base_url"] = "https://api.experientiallabs.ai/v1"

    thk_keys = _parse_keys("TOKENHARBOR_API_KEY", "TOKENHARBOR_API_KEYS")
    if thk_keys:
        config.setdefault("tokenharbor", {})["keys"] = thk_keys
        if "base_url" not in config["tokenharbor"]:
            config["tokenharbor"]["base_url"] = "https://tokenharbor.ai/v1"

    fc_keys = _parse_keys("FIRECRAWL_API_KEY", "FIRECRAWL_API_KEYS")
    if fc_keys:
        config.setdefault("firecrawl", {})["keys"] = fc_keys
        if "base_url" not in config["firecrawl"]:
            config["firecrawl"]["base_url"] = "https://api.firecrawl.dev/v1"

    copilot_token = (
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("COPILOT_TOKEN")
        or os.environ.get("GH_TOKEN")
    )
    if copilot_token:
        config.setdefault("copilot", {})["token"] = copilot_token.strip()
        config.setdefault("github", {})["token"] = copilot_token.strip()

    openai_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")
    if openai_key:
        config.setdefault("openai", {})["api_key"] = openai_key.strip()
        config.setdefault("codex", {})["api_key"] = openai_key.strip()

    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTIGRAVITY_API_KEY")
    if gemini_key:
        config.setdefault("gemini", {})["api_key"] = gemini_key.strip()
        config.setdefault("antigravity", {})["api_key"] = gemini_key.strip()

    return config
