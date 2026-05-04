"""conftest.py for opensemantic.base tests.

Loads environment variables from tests/.env if present.
Users can either:
  - Copy tests/.env.example to tests/.env and fill in values
  - Set env vars directly (shell, CI, IDE)
"""

from pathlib import Path

_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env_path)
    except ImportError:
        pass
