"""What someone works on, so advice can fit the kind of work.

A full-stack web app and a large C++ system call for different habits with Claude Code,
so advice needs more than usage numbers. Per repository this collects the languages of
the files sessions read, the build and Claude Code setup the repo has (whether each is
there, never what it says), and the titles of recent sessions. No conversation text.
Plugins count as setup too: which marketplace each enabled one comes from, often a
team's or a company's own, on which events its hooks run, and what its skills,
subagents and commands are for, as their descriptions say (never their instructions).
With that, advice can point at what a team already provides instead of suggesting it
again, and say when a change belongs in the team's plugin.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath

import yaml

from agentlens.config import repo_name, repo_root
from agentlens.model import ACTIVE_KINDS, Session
from agentlens.terminal import claude_env

LANGUAGES = {
    ".py": "Python", ".ipynb": "Jupyter",
    ".ts": "TypeScript", ".tsx": "TypeScript/React", ".js": "JavaScript", ".jsx": "JavaScript/React",
    ".mjs": "JavaScript", ".vue": "Vue", ".svelte": "Svelte", ".html": "HTML", ".css": "CSS", ".scss": "CSS",
    ".c": "C", ".h": "C/C++ headers", ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".hh": "C++",
    ".inl": "C++", ".cu": "CUDA", ".hlsl": "shaders", ".glsl": "shaders", ".shader": "shaders",
    ".cs": "C#", ".rs": "Rust", ".go": "Go", ".java": "Java", ".kt": "Kotlin", ".swift": "Swift",
    ".m": "Objective-C", ".mm": "Objective-C++", ".rb": "Ruby", ".php": "PHP", ".dart": "Dart",
    ".scala": "Scala", ".lua": "Lua", ".sql": "SQL", ".sh": "shell", ".ps1": "PowerShell",
    ".cmake": "CMake", ".tf": "Terraform", ".proto": "Protobuf",
    ".md": "Markdown", ".yaml": "YAML", ".yml": "YAML", ".json": "JSON", ".toml": "TOML",
}
NAMED_FILES = {"cmakelists.txt": "CMake", "dockerfile": "Docker", "makefile": "Make"}  # paths may be lowercased

# Files at a repository's root that say how it's built and set up.
MARKERS = [
    ("CMakeLists.txt", "CMake"), ("meson.build", "Meson"), ("Makefile", "Make"), ("*.sln", "Visual Studio"),
    ("vcpkg.json", "vcpkg"), ("conanfile.*", "Conan"),
    ("package.json", "npm"), ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("tsconfig.json", "TypeScript"),
    ("next.config.*", "Next.js"), ("vite.config.*", "Vite"),
    ("pyproject.toml", "pyproject"), ("requirements*.txt", "pip requirements"),
    ("Cargo.toml", "Cargo"), ("go.mod", "Go modules"), ("pom.xml", "Maven"), ("build.gradle*", "Gradle"),
    ("Dockerfile", "Docker"), ("docker-compose.y*ml", "Docker Compose"), (".github/workflows", "GitHub Actions"),
    ("CLAUDE.md", "CLAUDE.md"), (".claude/settings.json", "project Claude Code settings"),
    (".mcp.json", "project MCP servers"),
]
# What a .claude folder, a repository's or your own, sets up for Claude Code. Only whether each
# is there goes into the advice, never what it says.
CLAUDE_SETUP = [
    ("CLAUDE.md", "CLAUDE.md"), ("agents/*.md", "custom subagents"), ("skills/**/SKILL.md", "skills"),
    ("commands/**/*.md", "custom slash commands"),
]
SETTINGS_FILES = ("settings.json", "settings.local.json")
TITLES_SHOWN = 6
LANGUAGES_SHOWN = 6
SUBFOLDERS_SHOWN = 3  # folders below a repo's root with setup of their own, nearest to where sessions ran first
# What a plugin provides, by where its files are: (kind, glob). A skill is named by its folder.
PLUGIN_PARTS = [("skill", "skills/*/SKILL.md"), ("subagent", "agents/*.md"), ("command", "commands/*.md")]
PROVIDES_SHOWN = 15  # per plugin
DESCRIPTION_CHARS = 160


@dataclass
class Plugin:
    name: str
    marketplace: str  # where it was installed from: "acme" for acmedev@acme
    hooks: list[str] = field(default_factory=list)  # when its hooks run: "PostToolUse (Write|Edit)"
    provides: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, name, description)


@dataclass
class ProjectWork:
    name: str
    sessions: int = 0
    active_min: float = 0.0
    languages: Counter = field(default_factory=Counter)  # language -> files read
    setup: list[str] = field(default_factory=list)  # marker files at the repo root, and in the folders sessions ran in
    titles: list[str] = field(default_factory=list)  # most recent first
    home: bool = False  # sessions started in the home folder, not in a repository


def language(path: str) -> str | None:
    name = PurePath(path.replace("\\", "/")).name
    return NAMED_FILES.get(name.lower()) or LANGUAGES.get(PurePath(name).suffix.lower())


def repo_setup(root: str) -> list[str]:
    base = Path(root)
    try:
        if not base.is_dir():
            return []
        labels = [label for pattern, label in MARKERS if _exists(base, pattern)] + claude_setup(base / ".claude")
    except OSError:
        return []
    return list(dict.fromkeys(labels))  # CLAUDE.md can be at the root and in .claude


def subfolder_setup(root: str, cwds: list[str]) -> list[str]:
    """Setup in the folders between where sessions ran and the repo root, the root left out.
    Claude Code loads the CLAUDE.md files above its working directory at launch, so a
    subfolder's own CLAUDE.md is the project's setup as much as one at the root."""
    root_parts = PurePath(root.replace("\\", "/")).parts
    folders: dict[tuple[str, ...], tuple[str, ...]] = {}  # lowercased -> as written
    for cwd in cwds:
        parts = PurePath(cwd.replace("\\", "/")).parts
        if [p.lower() for p in parts[:len(root_parts)]] != [p.lower() for p in root_parts]:
            continue
        rel = parts[len(root_parts):]
        if [p.lower() for p in rel[:2]] == [".claude", "worktrees"]:
            rel = rel[3:]  # a worktree is a copy of the repo, so its folders are the repo's own
        for depth in range(len(rel), 0, -1):  # from where it ran up to just below the root
            folders.setdefault(tuple(p.lower() for p in rel[:depth]), rel[:depth])
    labels: list[str] = []
    shown = 0
    for folder in folders.values():
        found = repo_setup(str(Path(root, *folder)))
        if found and shown < SUBFOLDERS_SHOWN:
            shown += 1
            labels += [f"{label} in {'/'.join(folder)}" for label in found]
    return labels


def user_setup(home: Path | None = None) -> list[str]:
    """What your user folder's .claude sets up for every project."""
    try:
        return claude_setup((home or Path.home()) / ".claude")
    except OSError:
        return []


def list_plugins() -> list[dict]:
    """Claude Code's own list of installed plugins, with where each is installed."""
    claude = shutil.which("claude")
    if not claude:
        return []
    try:
        result = subprocess.run([claude, "plugin", "list", "--json"], capture_output=True, text=True,
                                encoding="utf-8", timeout=60, stdin=subprocess.DEVNULL, env=claude_env())
        data = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []


def plugins(listed: Callable[[], list[dict]] | None = None) -> list[Plugin]:
    """The enabled plugins: where each came from, when its hooks run and what it provides."""
    out = []
    for plugin in (listed or list_plugins)():
        if not plugin.get("enabled") or not plugin.get("installPath"):
            continue
        root = Path(plugin["installPath"])
        name, _, marketplace = str(plugin.get("id", root.name)).partition("@")
        try:
            provides = [(kind, *_named(path, kind)) for kind, pattern in PLUGIN_PARTS
                        for path in sorted(root.glob(pattern))]
        except OSError:
            continue
        out.append(Plugin(name, marketplace, _hook_events(root / "hooks" / "hooks.json"), provides))
    return out


def describe_plugin(plugin: Plugin) -> list[str]:
    """A line for the plugin, then one per thing it provides, for the advice prompt."""
    origin = f", from the {plugin.marketplace} marketplace" if plugin.marketplace else ""
    counts = Counter(kind for kind, _, _ in plugin.provides)
    parts = [_counted_kinds(counts)] if counts else []
    if plugin.hooks:
        parts.append("hooks on " + ", ".join(plugin.hooks))
    lines = [f"- {plugin.name}{origin}: {'; '.join(parts) or 'nothing agentlens can see'}"]
    for kind, name, description in plugin.provides[:PROVIDES_SHOWN]:
        lines.append(f"  - {kind} {name}" + (f": {description}" if description else ""))
    if len(plugin.provides) > PROVIDES_SHOWN:
        lines.append(f"  - ...and {len(plugin.provides) - PROVIDES_SHOWN} more")
    return lines


def _counted_kinds(counts: Counter) -> str:
    """How many of each kind, in PLUGIN_PARTS order: "2 skills, 1 subagent"."""
    return ", ".join(f"{n} {kind}{'s' if n != 1 else ''}" for kind, _ in PLUGIN_PARTS if (n := counts[kind]))


def _named(path: Path, kind: str) -> tuple[str, str]:
    """A skill's, subagent's or command's name and its description, cut to a line."""
    front = _frontmatter(path)
    fallback = path.parent.name if kind == "skill" else path.stem
    name = str(front.get("name") or fallback)
    description = " ".join(str(front.get("description") or "").split())
    if len(description) > DESCRIPTION_CHARS:
        description = description[:DESCRIPTION_CHARS].rsplit(" ", 1)[0] + " ..."
    return name, description


def _frontmatter(path: Path) -> dict:
    """The YAML between a markdown file's opening `---` lines, where its name and description are."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    try:
        data = yaml.safe_load(text[3:end]) if end > 0 else None
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _hook_events(path: Path) -> list[str]:
    """`PostToolUse (Write|Edit)` per configured hook: when it runs, never what it runs."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    out = []
    for event, groups in (hooks or {}).items():
        for group in groups if isinstance(groups, list) else []:
            matcher = group.get("matcher") if isinstance(group, dict) else None
            out.append(f"{event} ({matcher})" if matcher else event)
    return out


def claude_setup(folder: Path) -> list[str]:
    labels = [label for pattern, label in CLAUDE_SETUP if _exists(folder, pattern)]
    if any(_has_hooks(folder / name) for name in SETTINGS_FILES):
        labels.append("hooks")
    return labels


def _exists(base: Path, pattern: str) -> bool:
    return next(iter(base.glob(pattern)), None) is not None


def _has_hooks(settings: Path) -> bool:
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("hooks"))


def build(sessions: list[Session]) -> list[ProjectWork]:
    """Projects with the most agent time first."""
    projects: dict[str, ProjectWork] = {}
    roots: dict[str, str] = {}
    cwds: dict[str, Counter] = {}
    for s in sorted(sessions, key=lambda s: s.end, reverse=True):  # most recently active first
        name = repo_name(s.cwd) if s.cwd else s.project
        work = projects.setdefault(name, ProjectWork(name))
        if s.cwd:
            roots.setdefault(name, repo_root(s.cwd))
            cwds.setdefault(name, Counter())[s.cwd] += 1
        work.sessions += 1
        work.active_min += sum(run.minutes(*ACTIVE_KINDS) for run in (s.main, *s.subagents))
        for run in (s.main, *s.subagents):
            work.languages.update(lang for path in run.files_read if (lang := language(path)))
        if s.title and s.title != "(untitled)" and s.title not in work.titles and len(work.titles) < TITLES_SHOWN:
            work.titles.append(s.title)
    for name, work in projects.items():
        root = roots.get(name)
        # The home folder's .claude/ holds your user settings, not a project's setup.
        work.home = root is not None and _same_path(root, Path.home())
        if root and not work.home:
            ran_in = [cwd for cwd, _ in cwds[name].most_common()]
            work.setup = repo_setup(root) + subfolder_setup(root, ran_in)
    return sorted(projects.values(), key=lambda w: w.active_min, reverse=True)


def _same_path(a: str | Path, b: str | Path) -> bool:
    return str(a).replace("\\", "/").rstrip("/").lower() == str(b).replace("\\", "/").rstrip("/").lower()


def describe(work: ProjectWork) -> str:
    """One line for the advice prompt."""
    parts = [f"{work.sessions} session{'s' if work.sessions != 1 else ''}, {work.active_min / 60:.1f} h of agent work"]
    if work.languages:
        parts.append("files read: " + ", ".join(f"{lang} {n}" for lang, n in work.languages.most_common(LANGUAGES_SHOWN)))
    if work.home:
        parts.append("started in the home folder, not a repository")
    else:
        parts.append("repo: " + (", ".join(work.setup) if work.setup else "no build or Claude Code setup files found"))
    if work.titles:
        parts.append("recent sessions: " + "; ".join(f'"{t}"' for t in work.titles))
    return f"- {work.name}: " + " · ".join(parts)
