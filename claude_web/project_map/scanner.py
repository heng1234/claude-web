from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import ProjectMapEvidence


IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".claude",
    ".codex",
    "uploads",
}
SENSITIVE_NAME_PATTERNS = (
    re.compile(r"^\.env(?:\.|$)", re.IGNORECASE),
    re.compile(r"^\.mcp\.json$", re.IGNORECASE),
    re.compile(r"(?:^|[-_.])(secret|secrets|credential|credentials|token|tokens)(?:[-_.]|$)", re.IGNORECASE),
    re.compile(r"\.(?:pem|key|p12|pfx|jks|keystore)$", re.IGNORECASE),
)
BINARY_SUFFIXES = {
    ".7z", ".a", ".avi", ".bin", ".bmp", ".class", ".db", ".dylib", ".exe",
    ".gif", ".gz", ".ico", ".jar", ".jpeg", ".jpg", ".lockb", ".mov", ".mp3",
    ".mp4", ".o", ".pdf", ".png", ".pyc", ".sqlite", ".sqlite3", ".so", ".tar",
    ".tiff", ".ttf", ".wav", ".webp", ".woff", ".woff2", ".xls", ".xlsx", ".zip",
}
TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".css", ".go", ".h", ".hpp", ".html", ".htm", ".java",
    ".js", ".jsx", ".json", ".kt", ".kts", ".md", ".mjs", ".cjs", ".php", ".py",
    ".rb", ".rs", ".scss", ".sh", ".sql", ".svelte", ".swift", ".toml", ".ts",
    ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml", ".zsh",
}
MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "requirements.txt", "cargo.toml", "go.mod",
    "pom.xml", "build.gradle", "dockerfile", "docker-compose.yml", "makefile",
}
ENTRYPOINT_NAMES = {
    "main.py", "app.py", "server.py", "__main__.py", "index.js", "index.ts",
    "main.js", "main.ts", "main.tsx", "app.tsx", "app.jsx",
}
ROUTE_DECORATOR_RE = re.compile(r"(?:app|router)\.(get|post|put|patch|delete|options|head|websocket)$")
JS_DEFINITION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
)
JS_IMPORT_RE = re.compile(
    r"^\s*import(?:[\s\S]*?\sfrom\s+)?[\"']([^\"']+)[\"']|"
    r"^\s*(?:const|let|var)\s+[\w${},\s]+\s*=\s*require\([\"']([^\"']+)[\"']\)"
)
JS_FETCH_RE = re.compile(r"\bfetch\(\s*[`\"']([^`\"']+)")
HTML_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)


@dataclass
class ScanLimits:
    max_files: int = 5000
    max_depth: int = 24
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_seconds: float = 8.0
    max_evidence: int = 480
    max_excerpt_chars: int = 1400


@dataclass
class ScanResult:
    root: Path
    files: List[dict] = field(default_factory=list)
    evidence: List[ProjectMapEvidence] = field(default_factory=list)
    imports: List[Tuple[str, str, str]] = field(default_factory=list)
    partial: bool = False
    partial_reason: str = ""
    source_root_hash: str = ""
    languages: Dict[str, int] = field(default_factory=dict)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _language(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".html": "html",
        ".htm": "html",
        ".css": "css",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".kt": "kotlin",
        ".rb": "ruby",
        ".php": "php",
        ".c": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
    }.get(suffix, "text")


def _role(path: Path) -> str:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if name in MANIFEST_NAMES:
        return "manifest"
    if name in ENTRYPOINT_NAMES:
        return "entrypoint"
    if "test" in parts or "tests" in parts or name.startswith("test_") or name.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts")):
        return "test"
    if name.startswith("readme") or path.suffix.lower() == ".md":
        return "documentation"
    if path.suffix.lower() in {".toml", ".yaml", ".yml", ".json"}:
        return "config"
    return "source"


def _is_sensitive(path: Path) -> bool:
    return any(
        pattern.search(part)
        for part in path.parts
        for pattern in SENSITIVE_NAME_PATTERNS
    )


def _is_candidate(path: Path) -> bool:
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    if _is_sensitive(path):
        return False
    return path.suffix.lower() in TEXT_SUFFIXES or path.name.lower() in MANIFEST_NAMES


def _git_files(root: Path) -> Optional[List[str]]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return [value.decode("utf-8", errors="surrogateescape") for value in completed.stdout.split(b"\0") if value]


def _walk_files(
    root: Path,
    *,
    deadline: float,
    max_depth: int,
    max_directories: int,
    state: Dict[str, str],
) -> Iterable[str]:
    directories_seen = 0
    for current_root, dirs, files in os.walk(root, followlinks=False):
        if time.monotonic() > deadline:
            state["partial_reason"] = "time_limit"
            return
        directories_seen += 1
        if directories_seen > max_directories:
            state["partial_reason"] = "discovery_limit"
            return
        current = Path(current_root)
        try:
            current_depth = len(current.relative_to(root).parts)
        except ValueError:
            dirs[:] = []
            continue
        if current_depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = sorted(
                name for name in dirs
                if name not in IGNORED_DIRS and not (current / name).is_symlink()
            )
        for name in sorted(files):
            if time.monotonic() > deadline:
                state["partial_reason"] = "time_limit"
                return
            try:
                yield (current / name).relative_to(root).as_posix()
            except ValueError:
                continue


def _safe_target(root: Path, relative: str) -> Optional[Path]:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    target = root.joinpath(relative_path)
    try:
        if target.is_symlink():
            return None
        resolved = target.resolve()
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            return None
    except (OSError, ValueError):
        return None
    return resolved


def _line_excerpt(lines: Sequence[str], start: int, end: int, limit: int) -> str:
    start_index = max(0, start - 1)
    end_index = min(len(lines), max(start, end))
    value = "".join(lines[start_index:end_index]).strip()
    return value[:limit]


def _evidence(
    *,
    path: str,
    file_hash: str,
    start: int,
    end: int,
    symbol_key: str,
    kind: str,
    label: str,
    excerpt: str,
) -> ProjectMapEvidence:
    stable = f"{path}:{symbol_key}:{kind}:{label}"
    snippet_hash = _sha256_bytes(excerpt.encode("utf-8", errors="replace"))
    return ProjectMapEvidence(
        id=f"ev-{hashlib.sha256(stable.encode()).hexdigest()[:16]}",
        path=path,
        file_hash=file_hash,
        start_line=max(1, start),
        end_line=max(max(1, start), end),
        symbol_key=symbol_key,
        kind=kind,
        label=label,
        excerpt=excerpt,
        snippet_hash=snippet_hash,
    )


def _python_evidence(path: str, text: str, file_hash: str, limit: int) -> Tuple[List[ProjectMapEvidence], List[Tuple[str, str, str]], str]:
    lines = text.splitlines(keepends=True)
    evidence: List[ProjectMapEvidence] = []
    imports: List[Tuple[str, str, str]] = []
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return evidence, imports, "parse_failed"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((path, alias.name, "IMPORTS") for alias in node.names if alias.name)
        elif isinstance(node, ast.ImportFrom):
            names = [alias.name for alias in node.names]
            module = node.module or (names[0] if names else "")
            if module:
                imports.append((path, module, "IMPORTS"))

    scoped_nodes: List[Tuple[ast.AST, str]] = []

    def collect_scoped(node: ast.AST, scope: Tuple[str, ...] = ()) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified_name = ".".join((*scope, child.name))
                scoped_nodes.append((child, qualified_name))
                collect_scoped(child, (*scope, child.name))
            else:
                collect_scoped(child, scope)

    collect_scoped(tree)
    for node, qualified_name in scoped_nodes:
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        symbol_key = f"{kind}:{qualified_name}"
        start = int(getattr(node, "lineno", 1))
        end = int(getattr(node, "end_lineno", start))
        label = qualified_name
        for decorator in getattr(node, "decorator_list", []):
            name = ""
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                if isinstance(decorator.func.value, ast.Name):
                    name = f"{decorator.func.value.id}.{decorator.func.attr}"
                if ROUTE_DECORATOR_RE.search(name):
                    route = ""
                    if decorator.args and isinstance(decorator.args[0], ast.Constant):
                        route = str(decorator.args[0].value)
                    kind = "route"
                    label = f"{name.rsplit('.', 1)[-1].upper()} {route or node.name}"
                    symbol_key = f"route:{qualified_name}:{route}"
                    break
        evidence.append(_evidence(
            path=path,
            file_hash=file_hash,
            start=start,
            end=min(end, start + 36),
            symbol_key=symbol_key,
            kind=kind,
            label=label,
            excerpt=_line_excerpt(lines, start, min(end, start + 36), limit),
        ))
    return evidence, imports, "parsed"


def _javascript_evidence(
    path: str,
    text: str,
    file_hash: str,
    limit: int,
    *,
    line_offset: int = 0,
    occurrences: Optional[Dict[str, int]] = None,
) -> Tuple[List[ProjectMapEvidence], List[Tuple[str, str, str]]]:
    lines = text.splitlines(keepends=True)
    evidence: List[ProjectMapEvidence] = []
    imports: List[Tuple[str, str, str]] = []
    occurrences = occurrences if occurrences is not None else {}
    for index, line in enumerate(lines, start=1):
        match = JS_DEFINITION_RE.search(line)
        if match:
            name = match.group(1) or match.group(2)
            occurrences[name] = occurrences.get(name, 0) + 1
            occurrence = occurrences[name]
            start = index + line_offset
            evidence.append(_evidence(
                path=path,
                file_hash=file_hash,
                start=start,
                end=start,
                symbol_key=f"definition:{name}:{occurrence}",
                kind="symbol",
                label=name,
                excerpt=line.strip()[:limit],
            ))
        import_match = JS_IMPORT_RE.search(line)
        if import_match:
            imports.append((path, import_match.group(1) or import_match.group(2), "IMPORTS"))
        fetch_match = JS_FETCH_RE.search(line)
        if fetch_match:
            start = index + line_offset
            target = fetch_match.group(1)
            evidence.append(_evidence(
                path=path,
                file_hash=file_hash,
                start=start,
                end=start,
                symbol_key=f"fetch:{target}",
                kind="fetch",
                label=f"FETCH {target}",
                excerpt=line.strip()[:limit],
            ))
    return evidence, imports


def _html_evidence(path: str, text: str, file_hash: str, limit: int) -> Tuple[List[ProjectMapEvidence], List[Tuple[str, str, str]], str]:
    evidence: List[ProjectMapEvidence] = []
    imports: List[Tuple[str, str, str]] = []
    occurrences: Dict[str, int] = {}
    for match in HTML_SCRIPT_RE.finditer(text):
        line_offset = text.count("\n", 0, match.start(1))
        script_evidence, script_imports = _javascript_evidence(
            path,
            match.group(1),
            file_hash,
            limit,
            line_offset=line_offset,
            occurrences=occurrences,
        )
        evidence.extend(script_evidence)
        imports.extend(script_imports)
    return evidence, imports, "parsed" if evidence or imports else "file_only"


class ProjectScanner:
    def __init__(self, limits: Optional[ScanLimits] = None) -> None:
        self.limits = limits or ScanLimits()

    def scan(self, root: Path) -> ScanResult:
        root = root.expanduser().resolve()
        result = ScanResult(root=root)
        started = time.monotonic()
        raw_paths = _git_files(root)
        using_git_index = raw_paths is not None
        walk_state: Dict[str, str] = {}
        if raw_paths is None:
            path_iterator: Iterable[str] = _walk_files(
                root,
                deadline=started + self.limits.max_seconds,
                max_depth=self.limits.max_depth,
                max_directories=max(self.limits.max_files * 4, 1000),
                state=walk_state,
            )
        else:
            path_iterator = sorted(set(raw_paths))
        total_bytes = 0
        discovered = 0
        root_hash_parts: List[str] = []
        for relative in path_iterator:
            discovered += 1
            if time.monotonic() - started > self.limits.max_seconds:
                result.partial, result.partial_reason = True, "time_limit"
                break
            if not using_git_index and discovered > max(self.limits.max_files * 4, 1000):
                result.partial, result.partial_reason = True, "discovery_limit"
                break
            if len(result.files) >= self.limits.max_files:
                result.partial, result.partial_reason = True, "file_limit"
                break
            relative_path = Path(relative)
            if (
                len(relative_path.parts) > self.limits.max_depth
                or any(part in IGNORED_DIRS for part in relative_path.parts[:-1])
                or _is_sensitive(relative_path)
            ):
                continue
            target = _safe_target(root, relative)
            if target is None or not target.is_file() or not _is_candidate(relative_path):
                continue
            try:
                stat = target.stat()
            except OSError:
                continue
            if stat.st_size > self.limits.max_file_bytes:
                result.partial = True
                result.partial_reason = result.partial_reason or "oversize_file"
                root_hash_parts.append(
                    f"oversize:{relative_path.as_posix()}:{stat.st_size}:{stat.st_mtime_ns}"
                )
                continue
            if total_bytes + stat.st_size > self.limits.max_total_bytes:
                result.partial, result.partial_reason = True, "byte_limit"
                break
            try:
                raw = target.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:8192]:
                continue
            total_bytes += len(raw)
            file_hash = _sha256_bytes(raw)
            path = Path(relative).as_posix()
            language = _language(target)
            role = _role(target.relative_to(root))
            text = raw.decode("utf-8", errors="replace")
            evidence: List[ProjectMapEvidence] = []
            imports: List[Tuple[str, str, str]] = []
            parse_quality = "file_only"
            if language == "python":
                evidence, imports, parse_quality = _python_evidence(
                    path, text, file_hash, self.limits.max_excerpt_chars
                )
            elif language in {"javascript", "typescript"}:
                evidence, imports = _javascript_evidence(
                    path, text, file_hash, self.limits.max_excerpt_chars
                )
                parse_quality = "conservative"
            elif language == "html":
                evidence, imports, parse_quality = _html_evidence(
                    path, text, file_hash, self.limits.max_excerpt_chars
                )
            if role in {"manifest", "entrypoint", "documentation", "config"}:
                evidence.insert(0, _evidence(
                    path=path,
                    file_hash=file_hash,
                    start=1,
                    end=min(40, max(1, text.count("\n") + 1)),
                    symbol_key=f"file:{role}",
                    kind=role,
                    label=path,
                    excerpt="\n".join(text.splitlines()[:40])[:self.limits.max_excerpt_chars],
                ))
            remaining = self.limits.max_evidence - len(result.evidence)
            if remaining > 0:
                result.evidence.extend(evidence[:remaining])
            result.imports.extend(imports)
            result.files.append({
                "path": path,
                "hash": file_hash,
                "size": len(raw),
                "language": language,
                "role": role,
                "parse_quality": parse_quality,
            })
            result.languages[language] = result.languages.get(language, 0) + 1
            root_hash_parts.append(f"{path}:{file_hash}")
        if not result.partial and walk_state.get("partial_reason"):
            result.partial = True
            result.partial_reason = walk_state["partial_reason"]
        result.source_root_hash = hashlib.sha256(
            "\n".join(root_hash_parts).encode("utf-8", errors="replace")
        ).hexdigest()
        return result
