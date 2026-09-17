from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


_STANDALONE_BUNDLE_MODULE_NAME = "_test_omh_standalone_bundle"


def _bundle_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "plugin_bundle" / "omh"


def standalone_bundle_module_names() -> tuple[str, ...]:
    """Every module in the bundle, read off the directory rather than listed.

    The subject has to be derived. A written list is a list of the modules
    someone thought of, and the module this exists to catch -- a new one whose
    `omh.*` import is not guarded -- is the one nobody thought of. Until
    #1623 the standalone load covered `awareness` alone, so its real subject
    was whatever `awareness` happened to pull in, and three modules that could
    not import at all sat outside it.

    `__init__.py` is left out because it is the entry the loader starts from;
    each subpackage's `__init__` appears here as the package itself.
    """
    bundle = _bundle_dir()
    names: list[str] = []
    for path in sorted(bundle.rglob("*.py")):
        parts = path.relative_to(bundle).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            names.append(".".join(parts))
    return tuple(names)


def _blocked_import() -> Callable[..., ModuleType]:
    """An import hook that answers as a host with no ``omh`` package would.

    It raises plain `ImportError`, not `ModuleNotFoundError`: both shapes
    reach a plugin host -- the installed package really missing, and Hermes'
    loader handing back a module it failed to exec -- and a guard written for
    only the narrower one is what #1623 was.
    """
    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> ModuleType:
        if name == "omh" or name.startswith("omh."):
            raise ImportError(
                f"standalone plugin host has no installed omh package (blocked import of {name!r})",
                name=name,
            )
        return real_import(name, *args, **kwargs)

    return blocked


def _bundle_package() -> tuple[ModuleType, importlib.machinery.ModuleSpec]:
    for name in list(sys.modules):
        if name == _STANDALONE_BUNDLE_MODULE_NAME or name.startswith(
            f"{_STANDALONE_BUNDLE_MODULE_NAME}."
        ):
            sys.modules.pop(name, None)

    bundle_dir = _bundle_dir()
    spec = importlib.util.spec_from_file_location(
        _STANDALONE_BUNDLE_MODULE_NAME,
        bundle_dir / "__init__.py",
        submodule_search_locations=[str(bundle_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load the vendored plugin bundle standalone")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_STANDALONE_BUNDLE_MODULE_NAME] = module
    return module, spec


def load_standalone_bundle(names: Iterable[str]) -> dict[str, ModuleType]:
    """Import the named bundle modules with no access to ``omh``.

    The bundle is copied into ``$HERMES_HOME/plugins/omh`` and loaded by
    Hermes' interpreter, which has no reason to have the ``omh`` package on
    its path, so every module in it has to import from inside itself.
    """
    module, spec = _bundle_package()
    loader = spec.loader
    assert loader is not None
    with patch("builtins.__import__", side_effect=_blocked_import()):
        loader.exec_module(module)
        return {
            name: importlib.import_module(f"{_STANDALONE_BUNDLE_MODULE_NAME}.{name}")
            for name in names
        }


def standalone_bundle_import_failures() -> dict[str, str]:
    """Import every bundle module standalone; report the ones that cannot."""
    module, spec = _bundle_package()
    loader = spec.loader
    assert loader is not None
    failures: dict[str, str] = {}
    with patch("builtins.__import__", side_effect=_blocked_import()):
        loader.exec_module(module)
        for name in standalone_bundle_module_names():
            try:
                importlib.import_module(f"{_STANDALONE_BUNDLE_MODULE_NAME}.{name}")
            except Exception as error:  # noqa: BLE001 - any failure to import is the finding
                failures[name] = f"{type(error).__name__}: {error}"
    return failures


def _load_standalone_bundle_awareness():
    """Load bundled awareness without access to the installed ``omh`` package."""
    return load_standalone_bundle(("awareness",))["awareness"]
