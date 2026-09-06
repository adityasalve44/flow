"""
tests/test_layering.py — Architectural import boundary tests (FLOW-041).

Invariants (§8, FLOW-041 of REVIEW_AND_PLAN.md):
- Provider-specific code stops at app/channel/whatsapp.
- InboundEvent DTO is the strict system boundary.
- Nothing under app/domain/, app/services/, or app/agents/ may import from app/channel/whatsapp.
"""

import ast
from pathlib import Path


def _get_imports_from_file(file_path: Path) -> list[str]:
    """Parse a python file into an AST and return all imported module names."""
    imports = []
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
    except Exception as err:
        raise RuntimeError(f"Failed to parse AST for {file_path}: {err}") from err

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    return imports


def test_whatsapp_adapter_layering_boundary():
    """
    Ensure zero leaks across the architectural boundary:
    No module under app/domain/, app/services/, or app/agents/ may import from app/channel/whatsapp.
    """
    root_dir = Path(__file__).resolve().parent.parent / "app"
    forbidden_package = "app.channel.whatsapp"

    restricted_dirs = [
        root_dir / "domain",
        root_dir / "services",
        root_dir / "agents",
    ]

    violations: list[str] = []

    for restricted_dir in restricted_dirs:
        assert restricted_dir.exists(), f"Directory {restricted_dir} must exist"
        for py_file in restricted_dir.rglob("*.py"):
            imported_modules = _get_imports_from_file(py_file)
            for mod in imported_modules:
                if mod == forbidden_package or mod.startswith(f"{forbidden_package}."):
                    violations.append(
                        f"Layering violation in {py_file.relative_to(root_dir.parent)}: "
                        f"imports forbidden '{mod}'"
                    )

    assert not violations, "\n".join(violations)
