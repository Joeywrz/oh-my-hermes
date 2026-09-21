"""Who could answer a route question on THIS machine, in order of specificity.

A route question is typed so that something other than the deterministic
router can answer it. This module reports what that something could be here,
and nothing else. It is a report, not a recommendation: OMH does not call a
third-party plugin, does not ask the host model to call one, and the presence
of a rung is never a reason to prefer it.

The rungs, most specific first:

* ``jev_plugin`` -- a Jev-class plugin is on this machine. It appears only
  when one was detected, and its ``status`` is the strongest tier OMH can
  prove: ``installed`` (a manifest declares a ``jev_`` tool), ``enabled``
  (Hermes' config also lists the plugin), ``observed`` (a ``jev_``-prefixed
  tool call reached dispatch on this install at least once).
* ``main_model`` -- the host model reading this payload. Always available,
  because a model that can read the question can answer it.
* ``none`` -- leave the question unanswered. Always available, and it is not
  a failure: an unanswered question changes nothing and the deterministic
  route stays in force.

Every tier under-claims by construction. A config shape this module cannot
read reports ``installed`` rather than ``enabled``; an unreadable plugins
directory drops the ``jev_plugin`` rung entirely rather than inventing one.
That direction is deliberate -- a missing rung costs a reader an option it
could have had, while an invented one is a claim about a machine.

Stdlib and intra-bundle imports only: Hermes loads this directory with its own
interpreter, which has no reason to have the `omh` package on its path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .jev_sidekick import JEV_TOOL_PREFIX, classify_plugin
from .runtime_reader import _yaml_list_values, installed_plugin_advertised_tools
from .tool_bursts import jev_tool_observed_at

JEV_PLUGIN_ANSWERER = "jev_plugin"
MAIN_MODEL_ANSWERER = "main_model"
NO_ANSWERER = "none"

STATUS_INSTALLED = "installed"
STATUS_ENABLED = "enabled"
STATUS_OBSERVED = "observed"
STATUS_AVAILABLE = "available"

# How many plugin names and tool names one rung carries. A machine with more
# Jev-class plugins than this has a bigger problem than a truncated list, and
# the rung is a hint surface rather than an inventory -- `omh doctor --json`
# carries the full posture.
MAX_LADDER_ITEMS = 8

_JEV_CLAIM_BOUNDARY = (
    "A plugin name, an enabled name, or one observed tool call is what this "
    "machine declares, not evidence that Jev was served, that the plugin "
    "answered anything, or that OMH called it. OMH never calls it."
)
_MAIN_MODEL_CLAIM_BOUNDARY = (
    "An answer from the model reading this payload is a routing judgment it "
    "declares about itself, not an observation OMH made."
)
_NO_ANSWERER_CLAIM_BOUNDARY = (
    "Leaving the question unanswered changes nothing: the deterministic route "
    "stays in force."
)


def answerer_ladder(hermes_home: object = "", omh_home: object = "") -> list[dict[str, Any]]:
    """The answerers available on this machine, most specific first.

    Both homes are optional strings or paths. Without a Hermes home no plugin
    can be detected, and without an OMH home no tool call can have been
    observed; each absence costs its own tier and never the whole ladder.
    """
    rungs: list[dict[str, Any]] = []
    jev_rung = _jev_plugin_rung(hermes_home, omh_home)
    if jev_rung is not None:
        rungs.append(jev_rung)
    rungs.append(
        {
            "answerer": MAIN_MODEL_ANSWERER,
            "status": STATUS_AVAILABLE,
            "claim_boundary": _MAIN_MODEL_CLAIM_BOUNDARY,
        }
    )
    rungs.append(
        {
            "answerer": NO_ANSWERER,
            "status": STATUS_AVAILABLE,
            "claim_boundary": _NO_ANSWERER_CLAIM_BOUNDARY,
        }
    )
    return rungs


def _jev_plugin_rung(hermes_home: object, omh_home: object) -> dict[str, Any] | None:
    """The `jev_plugin` rung, or None when no Jev-class plugin was detected."""
    detected = _detected_jev_plugins(hermes_home)
    if not detected:
        return None
    names = sorted(detected)
    tools = sorted({tool for name in names for tool in detected[name]})
    status = STATUS_INSTALLED
    if any(_plugin_is_enabled(hermes_home, name) for name in names):
        status = STATUS_ENABLED
    if _jev_tool_observed(omh_home):
        status = STATUS_OBSERVED
    return {
        "answerer": JEV_PLUGIN_ANSWERER,
        "status": status,
        "plugins": names[:MAX_LADDER_ITEMS],
        "tools": tools[:MAX_LADDER_ITEMS],
        "claim_boundary": _JEV_CLAIM_BOUNDARY,
    }


def _detected_jev_plugins(hermes_home: object) -> dict[str, list[str]]:
    """Installed plugin directory name -> the `jev_` tools its manifest declares.

    The classifier decides what counts as Jev-class, so a known plugin under a
    name that declares no tool at all is detected too, and the tool-prefix
    signal keeps a renamed one from disappearing.
    """
    home = _home_path(hermes_home)
    if home is None:
        return {}
    try:
        declarations = installed_plugin_advertised_tools(home)
    except OSError:
        # An unreadable plugins directory is the absence of a reading, not the
        # absence of a plugin. The rung is dropped rather than reported at a
        # tier that was not proved.
        return {}
    detected: dict[str, list[str]] = {}
    for name, tools in declarations.items():
        record = classify_plugin(name, tools)
        if record is None:
            continue
        detected[name] = [tool for tool in tools if tool.startswith(JEV_TOOL_PREFIX)]
    return detected


def _plugin_is_enabled(hermes_home: object, name: str) -> bool:
    """Whether Hermes' own config lists `name` under `plugins.enabled`.

    The bundle cannot import core's `config_adapter`, so this reuses the list
    reader the HUD already has, applied to the `plugins:` block alone rather
    than to the whole file -- an `enabled:` key nested under some other
    plugin's settings is a different key with the same name.

    A shape this reader cannot follow (an inline `plugins: {enabled: [omh]}`,
    an anchor) yields False, which reports the plugin one tier lower than it
    sits. That is the safe direction: a rung claiming `enabled` is a claim
    about the host's config, and under-reading it costs only precision.
    """
    home = _home_path(hermes_home)
    if home is None or not name:
        return False
    try:
        text = (home / "config.yaml").read_text(encoding="utf-8")
    except OSError:
        return False
    block = _plugins_block(text)
    if not block:
        return False
    enabled = _yaml_list_values(block, "enabled")
    disabled = _yaml_list_values(block, "disabled")
    return name in enabled and name not in disabled


def _plugins_block(config_text: str) -> str:
    """The lines under a top-level `plugins:` key, or "".

    Sliced by indentation so the list reader below sees one block. The last
    `plugins:` key wins, the way a YAML loader resolves a duplicate mapping
    key.
    """
    lines = config_text.splitlines()
    start = -1
    for index, line in enumerate(lines):
        if line.strip().split("#", 1)[0].rstrip() == "plugins:" and not line[:1].isspace():
            start = index
    if start < 0:
        return ""
    block: list[str] = []
    for line in lines[start + 1:]:
        if line.strip() and not line[:1].isspace():
            break
        block.append(line)
    return "\n".join(block)


def _jev_tool_observed(omh_home: object) -> bool:
    home = str(omh_home or "")
    try:
        return jev_tool_observed_at(home) > 0
    except OSError:
        return False


def _home_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    return Path(text)


__all__ = [
    "JEV_PLUGIN_ANSWERER",
    "MAIN_MODEL_ANSWERER",
    "MAX_LADDER_ITEMS",
    "NO_ANSWERER",
    "STATUS_AVAILABLE",
    "STATUS_ENABLED",
    "STATUS_INSTALLED",
    "STATUS_OBSERVED",
    "answerer_ladder",
]
