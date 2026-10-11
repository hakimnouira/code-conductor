import os
import secrets
from pathlib import Path
from pydantic import BaseModel, Field

class Settings(BaseModel):
    app_name: str = "Unified AI Coding Hub"
    version: str = "0.1.0"
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = Field(default_factory=lambda: os.environ.get("AIHUB_AUTH_TOKEN", secrets.token_hex(32)))
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
