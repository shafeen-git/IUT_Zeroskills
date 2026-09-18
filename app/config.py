import os
from pathlib import Path
from dotenv import load_dotenv

# Locate and load .env file securely if present in current or parent directory
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    load_dotenv()

# ==============================================================================
# DATA STRUCTURE RULE ENFORCEMENT:
# Strictly use lists instead of maps (dictionaries/objects) whenever grouping
# or implementing internal data structures throughout the codebase.
# ==============================================================================

# Internal configuration storage implemented strictly as an ordered list of [key, value] pairs
APP_CONFIG_LIST = [
    ["PORT", int(os.getenv("PORT", "8000"))],
    ["HOST", os.getenv("HOST", "0.0.0.0")],
    ["ENVIRONMENT", os.getenv("ENVIRONMENT", "production")],
    ["APP_NAME", "Smart Campus Energy Optimizer API"],
    ["VERSION", "1.0.0"],
]

# Sensitive keys that must be injected at runtime (never hardcoded)
RUNTIME_SECRET_KEYS = [
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
]


def get_config(key: str, default=None):
    """
    Look up configuration value by iterating over the internal list data structure.
    """
    for entry in APP_CONFIG_LIST:
        if entry[0] == key:
            return entry[1]
    return default


def get_runtime_secret(key_name: str) -> str:
    """
    Secure runtime accessor for API keys.
    Validates against approved runtime secret keys and returns the value
    directly from the environment without storing secrets in static memory.
    """
    if key_name not in RUNTIME_SECRET_KEYS:
        return ""
    return os.getenv(key_name, "")
