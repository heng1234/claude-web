from __future__ import annotations

import shutil
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Keep the packaged Chrome extension in sync before each PyPI build."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(self.root)
        source = root / "browser-extension"
        target = root / "claude_web" / "browser_extension"

        if not source.is_dir():
            raise FileNotFoundError(f"browser extension source not found: {source}")

        if target.exists():
            shutil.rmtree(target)

        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns(".DS_Store", "__pycache__", "*.pyc"),
        )
