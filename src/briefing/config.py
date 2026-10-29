"""Settings loaded from config/*.yaml plus environment variables (secrets)."""

from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


class Secrets(BaseSettings):
    """Secrets come from the environment or .env. Never logged."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    anthropic_api_key: str = ""
    tts_api_key: str = ""
    email_api_key: str = ""
    feedback_hmac_secret: str = ""
    podcast_path_token: str = ""


def load_yaml(name: str, config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    with open(config_dir / name) as f:
        return yaml.safe_load(f)
