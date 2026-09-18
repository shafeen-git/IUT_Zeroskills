import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    load_dotenv()

# Configuration is kept as an ordered list of [key, value] pairs (project data-structure rule).
APP_CONFIG_LIST = [
    ["PORT", int(os.getenv("PORT", "8000"))],
    ["HOST", os.getenv("HOST", "0.0.0.0")],
    ["ENVIRONMENT", os.getenv("ENVIRONMENT", "production")],
    ["APP_NAME", "GridWise Smart Campus Energy Optimizer"],
    ["VERSION", "2.0.0"],
    ["GROQ_MODEL", os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")],
    ["LLM_TIMEOUT_SECONDS", float(os.getenv("LLM_TIMEOUT_SECONDS", "10"))],
    ["SOLVER_TIME_LIMIT_SECONDS", int(os.getenv("SOLVER_TIME_LIMIT_SECONDS", "10"))],
]

RUNTIME_SECRET_KEYS = [
    "GROQ_API_KEY",
]


def get_config(key: str, default=None):
    for entry in APP_CONFIG_LIST:
        if entry[0] == key:
            return entry[1]
    return default


def get_runtime_secret(key_name: str) -> str:
    """Read an approved secret from the environment at call time; secrets are never stored in code."""
    if key_name not in RUNTIME_SECRET_KEYS:
        return ""
    return os.getenv(key_name, "")
