#!/usr/bin/env python3
"""
Interactive Key & Subscription Configurator for Code Conductor.
Easily view, configure, and save API keys and subscription tokens into .env.
"""
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
ENV_EXAMPLE = ROOT_DIR / ".env.example"

# Add backend to sys.path so we can import config helpers
sys.path.insert(0, str(ROOT_DIR / "backend"))

try:
    from app.core.config import get_unified_api_config, load_environment_files
except ImportError:
    get_unified_api_config = None


def mask_key(k: str) -> str:
    if not k:
        return "<not set>"
    if len(k) <= 8:
        return "***"
    return k[:4] + "..." + k[-4:]


def read_env_file() -> dict[str, str]:
    data = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def save_env_file(updates: dict[str, str]) -> None:
    # If .env does not exist, initialize from .env.example
    content = ""
    if ENV_PATH.exists():
        content = ENV_PATH.read_text(encoding="utf-8")
    elif ENV_EXAMPLE.exists():
        content = ENV_EXAMPLE.read_text(encoding="utf-8")

    lines = content.splitlines()
    new_lines = []
    seen = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in updates:
                new_val = updates[k]
                new_lines.append(f"{k}={new_val}")
                seen.add(k)
                continue
        new_lines.append(line)

    # Append any remaining keys
    for k, v in updates.items():
        if k not in seen and v:
            new_lines.append(f"{k}={v}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"\n[OK] Configuration successfully saved to: {ENV_PATH}")


def main():
    print("=" * 65)
    print("      CODE CONDUCTOR - Key & Subscription Setup Assistant     ")
    print("=" * 65)
    print(f"Target file: {ENV_PATH}")

    current_env = read_env_file()

    # Discover existing keys from .env or global config
    detected = {}
    if get_unified_api_config:
        try:
            detected = get_unified_api_config()
        except Exception:
            pass

    def get_existing(env_name: str, prov: str, key_type: str = "key") -> str:
        if env_name in current_env and current_env[env_name]:
            return current_env[env_name]
        if prov in detected:
            if key_type == "keys" and detected[prov].get("keys"):
                return detected[prov]["keys"][0]
            if key_type == "token" and detected[prov].get("token"):
                return detected[prov]["token"]
            if key_type == "api_key" and detected[prov].get("api_key"):
                return detected[prov]["api_key"]
        return ""

    fields = [
        ("GITHUB_TOKEN", "GitHub Copilot (Personal Access Token or Copilot token)", "copilot", "token"),
        ("OPENAI_API_KEY", "OpenAI Codex (API Key or leave blank to use ~/.codex login)", "openai", "api_key"),
        ("GEMINI_API_KEY", "Google Gemini / Antigravity AI Layer Key", "gemini", "api_key"),
        ("GROQ_API_KEY", "Groq Cloud Key (Ultra-fast inference: Qwen / GPT-OSS)", "groq", "keys"),
        ("CODECRAFT_API_KEY", "CodeCraft Key (Gemma 2 2B)", "codecraft", "keys"),
        ("APMIX_API_KEY", "APMix Gateway Key (Claude Sonnet 4.6 / GPT-6 Luna)", "apmix", "keys"),
        ("EXPERIENTIALLABS_API_KEY", "Experiential Labs Key (AION 3.0 Mini)", "experientiallabs", "keys"),
        ("AIHUB_AUTH_TOKEN", "Custom Hub Bearer Token (or blank to auto-generate)", None, None),
    ]

    print("\nCurrent Detected Keys:")
    for env_name, label, prov, ktype in fields:
        val = get_existing(env_name, prov, ktype)
        print(f" - {env_name:24}: {mask_key(val)} ({label})")

    print("\n" + "-" * 65)
    print("Enter a new value, or press Enter to keep current:")
    print("-" * 65)

    updates = {}
    changed = False

    for env_name, label, prov, ktype in fields:
        curr = get_existing(env_name, prov, ktype)
        curr_disp = mask_key(curr)
        prompt_str = f"[{env_name}] (Current: {curr_disp})\n > "
        try:
            user_input = input(prompt_str).strip()
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled.")
            return

        if user_input:
            updates[env_name] = user_input
            changed = True
        elif curr:
            updates[env_name] = curr

    if changed or not ENV_PATH.exists():
        save_env_file(updates)
        print("\nAll keys are ready! Restart backend to apply new values.")
    else:
        print("\nNo changes made.")


if __name__ == "__main__":
    main()
