"""At build time, which commit agentlens is built from, for agentlens.version: a copy installed away
from its checkout (uv tool install .) has no git to ask. Written into the package as agentlens/_build.py,
in the sdist too, so a wheel built from that still has it. Without git, or outside a checkout, nothing."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

TARGETS = {"wheel": "agentlens/_build.py", "sdist": "src/agentlens/_build.py"}


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable" or self.target_name not in TARGETS:
            return  # an editable install runs from the checkout, where git says
        try:
            found = subprocess.run(["git", "log", "-1", "--format=%h %cs"], cwd=self.root, capture_output=True,
                                   text=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return
        parts = found.stdout.split()
        if found.returncode != 0 or len(parts) != 2:
            return  # not a checkout: a wheel built from an sdist, which has the file already
        built = Path(tempfile.mkdtemp()) / "_build.py"
        built.write_text(f'"""Written when agentlens was built (hatch_build.py)."""\n\nCOMMIT = "{parts[0]}"\n'
                         f'DATE = "{parts[1]}"\n', encoding="utf-8")
        build_data["force_include"][str(built)] = TARGETS[self.target_name]
