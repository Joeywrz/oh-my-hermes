"""Which Jev-class plugins a Hermes home holds, read and never run.

Jev is a non-generative decision model, so OMH cannot route it as an executor
and never calls it. What an operator still needs to know is whether something
on this machine calls it on their behalf, and whether that something acts on a
surface OMH also acts on -- most sharply `pre_llm_call`, where OMH injects its
route hint and a Jev skill router nominates a skill, so the model can receive
two nominations for one message.

This builder answers that from three local reads and nothing else: the plugin
directories under ``$HERMES_HOME/plugins``, Hermes' ``plugins.enabled`` list,
and the NAMES (never the values) in ``$HERMES_HOME/.env``. It imports no
plugin, spawns nothing, opens no socket, and writes nothing anywhere.

What the tiers mean, and what they do not. ``installed`` means a directory
with a manifest exists. ``enabled`` means Hermes' config lists the name.
``credential_name_present`` means a variable name a Jev plugin declares as its
route appears in the env file. The ladder is monotonic -- each tier requires
the one below it -- so a credential name beside a plugin the operator disabled
reports ``installed``, and ``credential_names_present`` carries the names
regardless so nothing is lost by the collapse. None of the three is evidence
that Jev is served, that the plugin ran, or that any request left the machine.

The manifest reader is the bounded subset reader in
``plugin_manifest_yaml``, which refuses a construct it does not model rather
than guessing. That refusal is carried, not swallowed: a declaration this
reader could not understand is named under ``skipped`` when it left the
plugin unclassified, and under the plugin's own
``unreadable_declarations`` when it did not.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from ..install.config_adapter import plugin_is_enabled
from ..plugin_bundle.omh.jev_sidekick import JEV_CREDENTIAL_ENV_NAMES, classify_plugin
from ..plugin_bundle.omh.provider_detection import env_key_names
from .plugin_hook_contract import MANIFEST_FILENAME
from .plugin_manifest_yaml import (
    ManifestEntry,
    PluginManifestFormatError,
    entry_values,
    read_plugin_manifest_entries,
)

JEV_SIDEKICK_POSTURE_SCHEMA_VERSION: Final = "jev_sidekick_posture/v1"

# Monotonic, lowest first. `credential_name_present` sits above `enabled`
# because a credential name matters only for a plugin Hermes will load.
POSTURE_STATUSES: Final = ("absent", "installed", "enabled", "credential_name_present")

NAME_FIELD: Final = "name"
TOOLS_FIELD: Final = "provides_tools"
HOOKS_FIELD: Final = "provides_hooks"

# The audit reads an untrusted local directory, so both dimensions are bounded
# before anything is interpreted. A home past either bound is not a failure --
# the entries past it are simply reported as unread.
MAX_PLUGIN_DIRECTORIES: Final = 512
MAX_MANIFEST_BYTES: Final = 256 * 1024

CLAIM_BOUNDARY: Final = (
    "presence of a plugin directory, an enabled name, or a credential NAME is not evidence that Jev is "
    "served, that the plugin ran, or that any request left the machine; OMH never reads credential values"
)


def build_jev_sidekick_posture(hermes_home: Path) -> dict[str, Any]:
    """Read the Jev-class plugin posture of one Hermes home.

    Read-only by construction: every path below is opened for reading and the
    function returns a plain dict. A home that does not exist is ``absent``,
    not an error, because no Hermes install is the ordinary state for a
    machine that has never run one.
    """
    plugins: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for directory in _plugin_directories(Path(hermes_home), skipped):
        entry = _classified_plugin(directory, skipped)
        if entry is not None:
            plugins.append(entry)
    plugins.sort(key=lambda item: str(item["name"]))
    credential_names: list[str] = []
    if plugins:
        # Only consulted once a Jev-class plugin is detected: OPENROUTER_API_KEY
        # on its own is an OpenRouter account, not a Jev signal, and reporting
        # it as one would make every OpenRouter user look like a Jev user.
        credential_names = env_key_names(Path(hermes_home), allowed=JEV_CREDENTIAL_ENV_NAMES)
    config_text = _read_text(Path(hermes_home) / "config.yaml")
    for entry in plugins:
        entry["enabled"] = plugin_is_enabled(config_text, str(entry["name"]))
    return {
        "schema_version": JEV_SIDEKICK_POSTURE_SCHEMA_VERSION,
        "status": _status(plugins, credential_names),
        "plugins": plugins,
        "credential_names_present": credential_names,
        "skipped": skipped,
        "claim_boundary": CLAIM_BOUNDARY,
    }


def posture_overlaps(posture: dict[str, Any]) -> list[str]:
    """Every plugin in *posture* that acts on a surface OMH also acts on.

    Two independent kinds of overlap, and either is enough: a hook both the
    plugin's installed manifest and the OMH bridge declare, and an OMH surface
    the plugin's catalog entry says it decides for. Returned as plugin names so
    a caller can name them without re-deriving the rule.
    """
    names: list[str] = []
    for entry in posture.get("plugins", []):
        if not isinstance(entry, dict):
            continue
        if entry.get("hook_overlap") or entry.get("overlap"):
            names.append(str(entry.get("name", "")))
    return names


def posture_unestablished_hook_overlap(posture: dict[str, Any]) -> list[str]:
    """Every plugin whose hook overlap with OMH could not be established.

    An empty ``hook_overlap`` means one of two different things, and a caller
    that conflates them states a fact it does not hold. When the manifest's
    ``provides_hooks`` was read, it means the plugin and the OMH bridge share
    no hook. When it was not -- an inline flow sequence the bounded subset
    does not model -- it means nobody looked, and `[pre_llm_call]` and `[]`
    are the same bytes to this reader.
    """
    names: list[str] = []
    for entry in posture.get("plugins", []):
        if not isinstance(entry, dict):
            continue
        if HOOKS_FIELD in entry.get("unreadable_declarations", []):
            names.append(str(entry.get("name", "")))
    return names


def _status(plugins: list[dict[str, Any]], credential_names: list[str]) -> str:
    if not plugins:
        return "absent"
    if not any(bool(entry.get("enabled")) for entry in plugins):
        return "installed"
    return "credential_name_present" if credential_names else "enabled"


def _plugin_directories(hermes_home: Path, skipped: list[dict[str, str]]) -> list[Path]:
    plugins_dir = hermes_home / "plugins"
    try:
        children = sorted(plugins_dir.iterdir(), key=lambda item: item.name)
    except FileNotFoundError:
        # No plugins directory is the ordinary pre-install state, not a read
        # that failed.
        return []
    except OSError as error:
        skipped.append({"plugin": plugins_dir.name, "reason": f"plugins directory is unreadable: {type(error).__name__}"})
        return []
    directories: list[Path] = []
    for child in children[:MAX_PLUGIN_DIRECTORIES]:
        if child.is_symlink():
            # A symlinked plugin directory is reported rather than followed:
            # resolving one reads a path outside the home OMH was asked about.
            skipped.append({"plugin": child.name, "reason": "symlinked plugin directory was not followed"})
            continue
        if child.is_dir():
            directories.append(child)
    if len(children) > MAX_PLUGIN_DIRECTORIES:
        skipped.append(
            {
                "plugin": plugins_dir.name,
                "reason": f"more than {MAX_PLUGIN_DIRECTORIES} entries; the rest were not read",
            }
        )
    return directories


def _classified_plugin(directory: Path, skipped: list[dict[str, str]]) -> dict[str, Any] | None:
    manifest_path = directory / MANIFEST_FILENAME
    try:
        if not manifest_path.is_file():
            return None
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            skipped.append({"plugin": directory.name, "reason": f"manifest exceeds {MAX_MANIFEST_BYTES} bytes"})
            return None
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        skipped.append({"plugin": directory.name, "reason": f"manifest is unreadable: {type(error).__name__}"})
        return None
    try:
        entries = read_plugin_manifest_entries(text)
    except PluginManifestFormatError as error:
        skipped.append({"plugin": directory.name, "reason": f"manifest is outside the readable subset: {error}"})
        return None
    declared_name, name_readable = _scalar(entries, NAME_FIELD)
    tools, tools_readable = _sequence(entries, TOOLS_FIELD)
    hooks, hooks_readable = _sequence(entries, HOOKS_FIELD)
    # Hermes identifies a plugin by its manifest name; the directory name is
    # the fallback, and it is what `plugins.enabled` would have to list for a
    # manifest that declares none.
    name = declared_name or directory.name
    classified = classify_plugin(name, tools, hooks)
    unreadable = [
        field
        for field, readable in ((NAME_FIELD, name_readable), (TOOLS_FIELD, tools_readable), (HOOKS_FIELD, hooks_readable))
        if not readable
    ]
    if classified is None:
        # The bounded reader's total rule: a manifest that was not understood
        # never reports a clean result. Only the two fields that can carry a
        # Jev signal count here. `provides_tools: [jev_evaluate]` is an inline
        # flow sequence the subset does not model and is indistinguishable
        # from the far commoner `provides_tools: []`, and a manifest whose
        # `name` could not be read is classified against its directory name
        # instead, which a known plugin need not match. An unread
        # `provides_hooks` cannot make a plugin Jev-class, so it never puts an
        # otherwise ordinary plugin in this list.
        hiding = [field for field in (NAME_FIELD, TOOLS_FIELD) if field in unreadable]
        if hiding:
            skipped.append(
                {
                    "plugin": directory.name,
                    "reason": "declaration outside the readable subset: " + ", ".join(hiding),
                }
            )
        return None
    classified["unreadable_declarations"] = unreadable
    classified["directory"] = directory.name
    return classified


def _scalar(entries: tuple[ManifestEntry, ...], key: str) -> tuple[str, bool]:
    found = entry_values(entries, key)
    if not found:
        return "", True
    if len(found) > 1 or found[0].kind != "scalar":
        return "", False
    return found[0].scalar, True


def _sequence(entries: tuple[ManifestEntry, ...], key: str) -> tuple[tuple[str, ...], bool]:
    found = entry_values(entries, key)
    if not found:
        return (), True
    if len(found) > 1:
        return (), False
    entry = found[0]
    if entry.kind == "sequence":
        return entry.sequence, True
    # `provides_tools:` with nothing under it is YAML null, which the reader
    # reports as an empty scalar. It declares nothing, and reading it as
    # unreadable would report a finding about a manifest that said what it
    # meant. An inline `[]` is a different shape and does not arrive here as a
    # scalar; the caller decides what an unread declaration costs.
    if entry.kind == "scalar" and not entry.scalar:
        return (), True
    return (), False


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
