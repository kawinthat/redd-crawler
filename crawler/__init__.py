"""RE:DD Autonomous Real Estate Crawler — crawler package"""
from __future__ import annotations

import os
from dotenv import load_dotenv
from pathlib import Path


def load_env(env_path: str | None = None) -> None:
    """Load environment variables from .env file."""
    if env_path:
        load_dotenv(dotenv_path=Path(env_path))
    else:
        # Walk up from this file to find .env
        root = Path(__file__).parent.parent
        load_dotenv(dotenv_path=root / ".env")


def get_env(key: str, default: str | None = None, required: bool = False) -> str | None:
    """Get environment variable with optional required check."""
    value = os.getenv(key, default)
    if required and not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"See .env.example for reference."
        )
    return value
