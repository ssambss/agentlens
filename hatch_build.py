"""At build time, agentlens's version, from how many commits it has (agentlens.version), for the
package and for a copy installed away from its checkout (uv tool install .), which has no git to ask.
It goes into the package as agentlens/_build.py, in the sdist too, so a wheel built from that has it.
Without git, or outside a checkout, it's what that file says, else just 0.1 (FALLBACK)."""

from __future__ import annotations

import importlib.util
import re
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.metadata.plugin.interface import MetadataHookInterface

TARGETS = {"wheel": "agentlens/_build.py", "sdist": "src/agentlens/_build.py"}


def _version_module(root: str):
    spec = importlib.util.spec_from_file_location("_agentlens_version", Path(root) / "src" / "agentlens" / "version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # it imports nothing of agentlens's
    return module


def version(root: str) -> str:
    module = _version_module(root)
    found = module.from_git(Path(root))
    if found:
        return found
    built = Path(root) / "src" / "agentlens" / "_build.py"  # in an sdist, from when it was made
    match = re.search(r'VERSION = "([^"]+)"', built.read_text(encoding="utf-8")) if built.exists() else None
    return match[1] if match else module.FALLBACK


class CustomMetadataHook(MetadataHookInterface):
    def update(self, metadata: dict) -> None:
        metadata["version"] = version(self.root)


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version_: str, build_data: dict) -> None:
        if version_ == "editable" or self.target_name not in TARGETS:
            return  # an editable install runs from the checkout, where git says
        if _version_module(self.root).from_git(Path(self.root)) is None:
            return  # not a checkout: a wheel built from an sdist, which has the file already
        built = Path(tempfile.mkdtemp()) / "_build.py"
        built.write_text(f'"""Written when agentlens was built (hatch_build.py)."""\n\n'
                         f'VERSION = "{version(self.root)}"\n', encoding="utf-8")
        build_data["force_include"][str(built)] = TARGETS[self.target_name]
