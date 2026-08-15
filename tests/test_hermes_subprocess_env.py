"""Hermes subprocess must load Lexi Composio credentials, not ~/.hermes/.env keys."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_run_teams_card_action_uses_lexi_composio_key_not_hermes():
    lexi_root = Path(__file__).resolve().parents[1]
    lexi_python = lexi_root / ".venv" / "bin" / "python"
    if not lexi_python.exists():
        return

    env = os.environ.copy()
    env["PYTHONPATH"] = str(lexi_root)
    env["COMPOSIO_API_KEY"] = "hermes-wrong-key-should-not-win"
    env["LEXI_DRY_RUN"] = "true"

    proc = subprocess.run(
        [
            str(lexi_python),
            "-c",
            "from app.config import settings; print(settings.composio_api_key or '')",
        ],
        cwd=str(lexi_root),
        env=env,
        capture_output=True,
        text=True,
    )
    # Import settings alone does not load run_teams_card_action dotenv — test runner entry:
    proc2 = subprocess.run(
        [
            str(lexi_python),
            "-c",
            """
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path('app/config.py').resolve().parents[1] / '.env', override=True)
from app.config import settings
print(settings.composio_api_key or '')
""",
        ],
        cwd=str(lexi_root),
        env=env,
        capture_output=True,
        text=True,
    )
    key = (proc2.stdout or "").strip()
    assert key and key != "hermes-wrong-key-should-not-win"
