from __future__ import annotations

import atexit
from importlib.machinery import ModuleSpec
import os
import sys
import tempfile
from types import ModuleType
from pathlib import Path


def _redirect_home_env_to_temp_root() -> None:
    """Point OMH/Hermes home defaults at a temp root for the whole test process.

    `default_omh_home()` and `default_hermes_home()` read OMH_HOME/HERMES_HOME
    and otherwise fall back to ~/.omh and ~/.hermes. Tests that never pass an
    explicit home therefore wrote into the developer's real home: the observed
    damage was ~4.7k `plan_artifact_created` events accumulated in
    ~/.omh/runtime/journal/events.jsonl over a month of test runs.

    The override is unconditional. Honouring a pre-existing OMH_HOME would let a
    developer's own export reintroduce the leak, which is the exact failure this
    guards against.
    """
    holder = tempfile.TemporaryDirectory(prefix="omh-test-home-")
    atexit.register(holder.cleanup)
    root = Path(holder.name)
    os.environ["OMH_HOME"] = str(root / "omh")
    os.environ["HERMES_HOME"] = str(root / "hermes")
    # The Kanban board reader honours these over the redirected home, so a
    # developer's live board would otherwise leak into every temp-home HUD
    # and hook test that does not patch them itself.
    os.environ["HERMES_KANBAN_HOME"] = ""
    os.environ["HERMES_KANBAN_BOARD"] = ""


_redirect_home_env_to_temp_root()


def load_local_package() -> None:
    if "omh" in sys.modules:
        return

    source_root = Path(__file__).resolve().parents[1] / "src"
    package_dir = source_root / "omh"
    module = ModuleType("omh")
    module.__file__ = str(package_dir)
    module.__package__ = "omh"
    module.__path__ = [str(package_dir), str(source_root)]  # type: ignore[attr-defined]
    module.__spec__ = ModuleSpec("omh", loader=None, is_package=True)
    sys.modules["omh"] = module

    from omh.version import __version__

    module.__version__ = __version__
