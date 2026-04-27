from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_home_isolation_sets_windows_home_environment(tmp_path: Path):
    assert os.environ["HOME"] == str(tmp_path)
    assert os.environ["USERPROFILE"] == str(tmp_path)
    assert "HOMEDRIVE" in os.environ
    assert "HOMEPATH" in os.environ


def test_fresh_default_config_import_resolves_under_isolated_home(tmp_path: Path):
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "from link_project_to_chat.config import DEFAULT_CONFIG; print(DEFAULT_CONFIG)",
        ],
        text=True,
    ).strip()

    assert Path(output).is_relative_to(tmp_path)
