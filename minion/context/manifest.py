"""Project manifest detection — identifies language, framework, and key metadata.

Purely static analysis: reads well-known fingerprint files using stdlib only
(tomllib, json, re). No LLM calls, no network, no extra dependencies.

Adding a new language/ecosystem = adding one _detect_* function and registering
it in DETECTORS. The first detector that finds its fingerprint file wins.
"""

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    try:
        import tomllib
    except ImportError:
        tomllib = None  # type: ignore


# ─── Framework signal tables ──────────────────────────────────────────────────

_PYTHON_FRAMEWORKS: dict[str, str] = {
    "django": "Django",
    "flask": "Flask",
    "fastapi": "FastAPI",
    "tornado": "Tornado",
    "starlette": "Starlette",
    "aiohttp": "aiohttp",
    "litestar": "Litestar",
    "sanic": "Sanic",
}

_NODE_FRAMEWORKS: dict[str, str] = {
    "next": "Next.js",
    "nuxt": "Nuxt",
    "gatsby": "Gatsby",
    "remix": "@remix-run",
    "express": "Express",
    "fastify": "Fastify",
    "koa": "Koa",
    "hapi": "@hapi",
    "nestjs": "NestJS",
    "vue": "Vue",
    "react": "React",
    "angular": "Angular",
    "svelte": "Svelte",
}

_RUST_FRAMEWORKS: dict[str, str] = {
    "axum": "Axum",
    "actix-web": "Actix Web",
    "warp": "Warp",
    "rocket": "Rocket",
    "tide": "Tide",
}


# ─── Result type ──────────────────────────────────────────────────────────────

@dataclass
class ProjectManifest:
    """Structured metadata extracted from a project's fingerprint file."""
    language: str
    framework: Optional[str] = None
    entry_point: Optional[str] = None
    key_deps: list[str] = field(default_factory=list)

    def to_text(self) -> str:
        """Compact single-line summary for the system prompt header."""
        parts = [f"Language: {self.language}"]
        if self.framework:
            parts[0] += f" · Framework: {self.framework}"
        if self.entry_point:
            parts.append(f"Entry: {self.entry_point}")
        if self.key_deps:
            parts.append(f"Key deps: {', '.join(self.key_deps[:6])}")
        return "\n".join(parts)


# ─── Language-specific detectors ─────────────────────────────────────────────

def _detect_python(cwd: Path) -> Optional[ProjectManifest]:
    """Detect Python projects via pyproject.toml, setup.cfg, or setup.py."""
    pyproject = cwd / "pyproject.toml"
    if pyproject.exists() and tomllib is not None:
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            # Support both [project] (PEP 517) and [tool.poetry]
            project = data.get("project") or data.get("tool", {}).get("poetry", {})
            deps_raw = (
                project.get("dependencies", [])
                or list((project.get("dependencies") or {}).keys())
            )
            # PEP 517: deps is a list of "pkg>=version" strings
            # Poetry: deps is a dict {pkg: version}
            if isinstance(deps_raw, dict):
                dep_names = [k.lower() for k in deps_raw if k.lower() != "python"]
            else:
                dep_names = [
                    re.split(r"[>=<!;\[]", d)[0].strip().lower()
                    for d in deps_raw
                    if d and not d.startswith("#")
                ]

            framework = next(
                (v for k, v in _PYTHON_FRAMEWORKS.items() if k in dep_names), None
            )
            python_ver = (
                project.get("requires-python", "")
                .lstrip(">=~^")
                .split(",")[0]
                .strip()
            )
            language = f"Python {python_ver}" if python_ver else "Python"
            entry_point = _find_entry(cwd, ["src/main.py", "src/app.py", "main.py", "app.py"])
            key_deps = [d for d in dep_names if d not in _PYTHON_FRAMEWORKS][:8]
            return ProjectManifest(language, framework, entry_point, key_deps)
        except Exception:
            pass

    # Fallback: bare setup.py/setup.cfg presence means Python
    if (cwd / "setup.py").exists() or (cwd / "setup.cfg").exists():
        entry_point = _find_entry(cwd, ["main.py", "app.py", "src/main.py"])
        return ProjectManifest("Python", None, entry_point)

    return None


def _detect_node(cwd: Path) -> Optional[ProjectManifest]:
    """Detect Node.js / JS / TS projects via package.json."""
    pkg_file = cwd / "package.json"
    if not pkg_file.exists():
        return None
    try:
        data = json.loads(pkg_file.read_text(encoding="utf-8"))
        all_deps: dict[str, str] = {}
        all_deps.update(data.get("dependencies", {}))
        all_deps.update(data.get("devDependencies", {}))
        dep_names = [k.lstrip("@").split("/")[0].lower() for k in all_deps]

        framework = next(
            (v for k, v in _NODE_FRAMEWORKS.items() if k in dep_names), None
        )
        is_ts = "typescript" in dep_names or (cwd / "tsconfig.json").exists()
        language = "TypeScript" if is_ts else "JavaScript"

        # Entry point: package.json "main" field, then common conventions
        main = data.get("main") or data.get("module")
        if not main:
            main = _find_entry(cwd, ["src/index.ts", "src/index.js", "index.ts", "index.js"])
        entry_point = str(main) if main else None

        key_deps = list(dict.fromkeys(
            k for k in all_deps if k.lower() not in _NODE_FRAMEWORKS
        ))[:8]
        return ProjectManifest(language, framework, entry_point, key_deps)
    except Exception:
        return None


def _detect_go(cwd: Path) -> Optional[ProjectManifest]:
    """Detect Go projects via go.mod."""
    go_mod = cwd / "go.mod"
    if not go_mod.exists():
        return None
    try:
        text = go_mod.read_text(encoding="utf-8")
        ver_match = re.search(r"^go\s+(\S+)", text, re.MULTILINE)
        language = f"Go {ver_match.group(1)}" if ver_match else "Go"
        # Extract require block deps
        deps = re.findall(r"^\s+(\S+)\s+v", text, re.MULTILINE)
        key_deps = [d.split("/")[-1] for d in deps[:8]]
        entry_point = _find_entry(cwd, ["main.go", "cmd/main.go"])
        return ProjectManifest(language, None, entry_point, key_deps)
    except Exception:
        return None


def _detect_rust(cwd: Path) -> Optional[ProjectManifest]:
    """Detect Rust projects via Cargo.toml."""
    cargo = cwd / "Cargo.toml"
    if not cargo.exists() or tomllib is None:
        return None
    try:
        data = tomllib.loads(cargo.read_text(encoding="utf-8"))
        edition = data.get("package", {}).get("edition", "")
        language = f"Rust (edition {edition})" if edition else "Rust"
        dep_names = [k.lower() for k in data.get("dependencies", {})]
        framework = next(
            (v for k, v in _RUST_FRAMEWORKS.items() if k in dep_names), None
        )
        entry_point = _find_entry(cwd, ["src/main.rs", "src/lib.rs"])
        key_deps = [d for d in dep_names if d not in _RUST_FRAMEWORKS][:8]
        return ProjectManifest(language, framework, entry_point, key_deps)
    except Exception:
        return None


def _detect_java(cwd: Path) -> Optional[ProjectManifest]:
    """Detect Java/Kotlin projects via pom.xml or build.gradle."""
    if (cwd / "pom.xml").exists():
        try:
            text = (cwd / "pom.xml").read_text(encoding="utf-8")
            art = re.search(r"<artifactId>([^<]+)</artifactId>", text)
            lang = "Java (Maven)"
            entry_point = art.group(1) if art else None
            return ProjectManifest(lang, None, entry_point)
        except Exception:
            return ProjectManifest("Java (Maven)")

    for gradle in ("build.gradle.kts", "build.gradle"):
        if (cwd / gradle).exists():
            is_kotlin = gradle.endswith(".kts")
            lang = "Kotlin (Gradle)" if is_kotlin else "Java (Gradle)"
            return ProjectManifest(lang)

    return None


# ─── Public API ───────────────────────────────────────────────────────────────

# Ordered list — first match wins. Most common ecosystems first.
DETECTORS = [_detect_python, _detect_node, _detect_go, _detect_rust, _detect_java]


def detect_project(cwd: Path) -> Optional[ProjectManifest]:
    """Return a ProjectManifest for the project rooted at cwd, or None if unrecognised."""
    for detector in DETECTORS:
        result = detector(cwd)
        if result is not None:
            return result
    return None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _find_entry(cwd: Path, candidates: list[str]) -> Optional[str]:
    """Return the first candidate path that exists relative to cwd."""
    for candidate in candidates:
        if (cwd / candidate).exists():
            return candidate
    return None
