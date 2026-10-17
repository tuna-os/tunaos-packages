"""Shared module loader for standalone CLI scripts in scripts/.

Scripts in this directory often need to import sibling scripts with hyphens in
their filenames (which cannot be imported using standard syntax) or when invoked
outside normal package context.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_MODULE_CACHE: dict[str, ModuleType] = {}


def load_script(name: str, filename: str | pathlib.Path | None = None) -> ModuleType:
    """Import a sibling script once by module name and optional filename."""
    if name not in _MODULE_CACHE:
        if filename is not None:
            path = pathlib.Path(filename)
            if not path.is_absolute():
                path = SCRIPTS_DIR / path
        else:
            path = SCRIPTS_DIR / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot find script at {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULE_CACHE[name] = module
    return _MODULE_CACHE[name]
