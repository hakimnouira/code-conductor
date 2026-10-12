import os
import secrets
from pathlib import Path
from pydantic import BaseModel, Field

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
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = Field(default_factory=get_or_create_token)
    workspace_root: str = Field(default_factory=lambda: os.getcwd())
    log_level: str = "INFO"

_settings: Settings | None = None

def get_settings() -> Settings:
    global _settings
    if _settings is None:
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
