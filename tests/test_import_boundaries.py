"""Cross-cutting architecture/boundary tests (Section 14.3).

These scan the loom package source with ``ast`` to enforce the non-negotiable
engineering rules independent of runtime behavior.
"""

from __future__ import annotations

import ast
from pathlib import Path

import loom

PACKAGE_DIR = Path(loom.__file__).resolve().parent


def _py_files(*, under: str | None = None) -> list[Path]:
    root = PACKAGE_DIR / under if under else PACKAGE_DIR
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _imports_any(modules: set[str], prefixes: tuple[str, ...]) -> bool:
    for mod in modules:
        for prefix in prefixes:
            if mod == prefix or mod.startswith(prefix + "."):
                return True
    return False


def test_slack_does_not_import_db_or_api():
    forbidden = ("loom.api", "loom.db", "loom.memory.store")
    for path in _py_files(under="slack"):
        modules = _imported_modules(path)
        assert not _imports_any(modules, forbidden), f"{path.name} imports {forbidden}"


def test_mcp_does_not_import_fastapi_or_slack():
    forbidden = ("loom.api", "loom.slack", "fastapi", "slack_bolt", "slack_sdk")
    for path in _py_files(under="mcp"):
        modules = _imported_modules(path)
        assert not _imports_any(modules, forbidden), f"{path.name} imports {forbidden}"


def test_only_config_reads_environment_variables():
    """Only loom/config.py may read environment variables."""
    offenders: list[str] = []
    for path in _py_files():
        if path.name == "config.py" and path.parent == PACKAGE_DIR:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # os.environ / os.getenv / os.environ.get
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                if isinstance(node.value, ast.Name) and node.value.id == "os":
                    offenders.append(f"{path.relative_to(PACKAGE_DIR)}:{node.lineno}")
            # dotenv usage
            if isinstance(node, ast.ImportFrom) and node.module and "dotenv" in node.module:
                offenders.append(f"{path.relative_to(PACKAGE_DIR)}:dotenv")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "dotenv" in alias.name:
                        offenders.append(f"{path.relative_to(PACKAGE_DIR)}:dotenv")
    assert not offenders, f"Env access outside config.py: {offenders}"


def test_no_bare_except_pass_exists():
    offenders: list[str] = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                # A handler whose only statement is `pass` is forbidden.
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    offenders.append(f"{path.relative_to(PACKAGE_DIR)}:{node.lineno}")
    assert not offenders, f"Bare except-pass found: {offenders}"


def test_no_print_logging_outside_cli_and_mcp_protocol():
    """No print() for logging outside CLI and the MCP protocol server."""
    offenders: list[str] = []
    for path in _py_files():
        rel = path.relative_to(PACKAGE_DIR)
        if rel.parts[0] in ("cli", "mcp"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "print":
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"print() used for logging: {offenders}"
