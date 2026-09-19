"""Hermes-side TUI preflight: can the OMH HUD/todo surface render at all?

OMH's TUI extension only exists inside Hermes' modern Ink TUI: the widget
file under ``$HERMES_HOME/tui-widgets/`` is loaded by that TUI's user-widget
SDK, and only when ``display.interface`` boots it. None of that is visible
from OMH's own install state, so ``omh update`` used to succeed while a user
on an old Hermes kept the classic REPL and read the silence as "OMH is
broken". This module inspects the Hermes side read-only — no subprocess, no
network — and reports what can and cannot render, with the repair action.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..paths import OmhPaths
from ..skin_pack import SKIN_NAME, theme_for_skin_name
from ..system.hermes_install import hermes_install_dir, installed_hermes_version, read_bounded_text
from ..tui_widget_pack import MANIFEST_FILENAME, WIDGET_FILENAME

HERMES_TUI_PREFLIGHT_SCHEMA_VERSION = "omh_hermes_tui_preflight/v1"

# The exact SDK names the installed widget destructures from ``register(sdk)``.
# If Hermes drops or renames one, its loader skips the widget with only a log
# line — this preflight is what turns that silent skip into a named finding.
# ``useShimmerPhase`` left this list when the widget dropped its animation
# subscription (shimmer repaints cleared terminal drag-selections over the
# dock); requiring an SDK key the widget never touches would block installs
# on hosts that render it fine.
REQUIRED_WIDGET_SDK_KEYS = (
    "Box",
    "Text",
    "defineWidgetApp",
    "h",
    "openWidget",
    "updateWidget",
)

_WIDGET_LOADER_RELATIVE = Path("ui-tui") / "src" / "sdk" / "userWidgets.ts"
_PREBUILT_BUNDLE_RELATIVE = Path("hermes_cli") / "tui_dist" / "entry.js"


def _widget_sdk_block(loader_text: str) -> str | None:
    """The ``widgetSdk`` export block, or None when the shape is unrecognizable.

    Without the closing ``} as const`` the "block" would be the rest of the
    file, where every key matches some identifier — a fail-open check that can
    never report a stripped SDK. Unparseable is reported as unparseable.
    """
    marker = "export const widgetSdk"
    start = loader_text.find(marker)
    if start < 0:
        return None
    end = loader_text.find("} as const", start)
    return loader_text[start:end] if end > start else None


def _missing_sdk_keys(loader_text: str) -> list[str] | None:
    block = _widget_sdk_block(loader_text)
    if block is None:
        return None
    return [key for key in REQUIRED_WIDGET_SDK_KEYS if not re.search(rf"\b{key}\b", block)]


def _display_interface(config_text: str) -> dict[str, Any]:
    """Classify ``display.interface`` with the canonical reader/writer pair.

    Three states matter, not two: an explicit value; genuinely unset (the
    canonical writer would add the default, so ``omh setup`` can fix it); and
    user-owned-but-noncanonical shapes (inline maps, dotted keys, duplicate
    blocks) that the writer refuses to touch — reporting those as "unset"
    would send the user into a repair loop ``omh setup`` can never close.
    """
    from ..install.config_adapter import display_interface_selection, ensure_tui_interface

    value = display_interface_selection(config_text)
    if value:
        return {"value": value, "explicit": True, "settable": False}
    try:
        settable = ensure_tui_interface(config_text).changed
    except ValueError:
        settable = False
    return {"value": "", "explicit": False, "settable": settable}


def _display_skin(config_text: str) -> dict[str, Any]:
    """The skin whose palette colours the OMH panel. User-owned, never written.

    An unset key means Hermes resolves its built-in ``default`` skin, which is
    a real answer here, not a gap: the widget takes its border colour from
    whichever skin is active either way.
    """
    from ..install.config_adapter import display_skin_selection

    value = display_skin_selection(config_text)
    return {"value": value or "default", "explicit": bool(value)}


def _widget_draws_themed_panel(widget_text: str) -> bool:
    """Does the INSTALLED widget render the current text-line HUD?

    A widget left over from an older OMH loads and runs fine — it just
    renders yesterday's surface beside the prompt, which reads as "the HUD
    still looks like the old version" with nothing else wrong. The current
    design is dense text in the host's own status-line idiom (the bordered
    card that briefly replaced it was retired by owner direction), so the
    markers are the derived state label the text header carries and colours
    resolving through the theme object — never a border, whose presence now
    marks the RETIRED card design.
    """
    return (
        "hudStateLabel" in widget_text
        and "borderStyle:" not in widget_text
        and re.search(r"color:\s*t\.color\.", widget_text) is not None
    )


def _widget_interpreter(widget_text: str) -> str:
    match = re.search(r"execFile\(\s*\n?\s*(\"(?:[^\"\\]|\\.)+\")", widget_text)
    if not match:
        return ""
    try:
        value = json.loads(match.group(1))
    except ValueError:
        return ""
    return value if isinstance(value, str) else ""


def hermes_tui_preflight(paths: OmhPaths) -> dict[str, Any]:
    """Inspect the Hermes side of the OMH TUI surface. Read-only."""
    install_dir = hermes_install_dir(paths.hermes_home)
    install_found = install_dir.is_dir()

    loader_path = install_dir / _WIDGET_LOADER_RELATIVE
    bundle_path = install_dir / _PREBUILT_BUNDLE_RELATIVE
    loader_text = read_bounded_text(loader_path) if install_found else None
    loader_marker = ""
    if loader_text is not None:
        loader_marker = "ui-tui-source"
    elif install_found and bundle_path.is_file():
        loader_marker = "prebuilt-bundle"

    missing_keys: list[str] | None = None
    sdk_parsed = False
    if loader_text is not None:
        missing_keys = _missing_sdk_keys(loader_text)
        sdk_parsed = missing_keys is not None

    config_text = read_bounded_text(paths.hermes_config_path) or ""
    interface = _display_interface(config_text)
    skin = _display_skin(config_text)

    widget_path = paths.hermes_home / "tui-widgets" / WIDGET_FILENAME
    manifest_path = paths.hermes_home / "tui-widgets" / MANIFEST_FILENAME
    widget_text = read_bounded_text(widget_path)
    interpreter = _widget_interpreter(widget_text) if widget_text is not None else ""
    interpreter_ok = bool(interpreter) and Path(interpreter).is_file()

    return {
        "schema_version": HERMES_TUI_PREFLIGHT_SCHEMA_VERSION,
        "install": {
            "found": install_found,
            "path": str(install_dir),
            "version": installed_hermes_version(install_dir) if install_found else "",
        },
        "widget_loader": {
            "present": bool(loader_marker),
            "marker": loader_marker,
        },
        "sdk_surface": {
            "checked": loader_text is not None,
            "parsed": sdk_parsed,
            "missing": missing_keys or [],
        },
        "display_interface": interface,
        "display_skin": skin,
        "widget": {
            "installed": widget_text is not None,
            "managed": manifest_path.is_file(),
            "interpreter": interpreter,
            "interpreter_ok": interpreter_ok,
            "themed_panel": widget_text is not None and _widget_draws_themed_panel(widget_text),
        },
    }


def widget_render_blockers(preflight: dict[str, Any]) -> list[str]:
    """Human-readable reasons the OMH HUD cannot render, empty when it can."""
    blockers: list[str] = []
    install = preflight.get("install", {})
    loader = preflight.get("widget_loader", {})
    sdk = preflight.get("sdk_surface", {})
    interface = preflight.get("display_interface", {})
    widget = preflight.get("widget", {})
    if not install.get("found"):
        blockers.append("Hermes install not found under the Hermes home; HUD state is unknowable from here.")
        return blockers
    if not loader.get("present"):
        version = str(install.get("version") or "unknown version")
        blockers.append(
            f"no TUI widget loader was found in this Hermes ({version}) — an old Hermes predates the modern "
            "TUI (run `hermes update`); if `hermes --tui` renders fine, the Hermes layout changed and this "
            "check needs updating — report it."
        )
    if sdk.get("checked") and not sdk.get("parsed"):
        blockers.append(
            "the Hermes widget SDK export changed shape and cannot be verified — if the HUD stops rendering, "
            "report the incompatibility."
        )
    elif sdk.get("parsed") and sdk.get("missing"):
        missing = ", ".join(sdk["missing"])
        blockers.append(
            f"the Hermes widget SDK no longer exposes: {missing} — the loader will skip the OMH widget; "
            "update OMH (`omh update`) or report the incompatibility."
        )
    if interface.get("explicit") and interface.get("value") not in ("", "tui"):
        blockers.append(
            f"display.interface is set to {interface['value']!r} — the OMH HUD renders only in the modern TUI "
            "(run `omh setup` or interactive `omh update` and accept the branded TUI; "
            "`hermes --tui` still reaches it for one session)."
        )
    elif not interface.get("explicit") and interface.get("settable"):
        blockers.append(
            "display.interface is unset, so bare `omh` and `hermes` open the classic REPL where the HUD cannot render; "
            "run `omh setup` or interactive `omh update` and accept the branded TUI, or use `hermes --tui` for one session."
        )
    if not widget.get("installed"):
        blockers.append("the OMH status widget is not installed; run `omh setup`.")
    elif widget.get("interpreter") and not widget.get("interpreter_ok"):
        blockers.append(
            f"the installed widget points at a Python interpreter that no longer exists "
            f"({widget['interpreter']}); run `omh setup` to reinstall it."
        )
    return blockers


TUI_VERDICT_SCHEMA_VERSION = "omh_tui_verdict/v1"


def tui_identity_verdict(paths: OmhPaths) -> dict[str, Any]:
    """End-of-run answer to "will this terminal actually look like OMH?".

    Setup and update buried render blockers in a mid-stream note; users read
    the success summary, opened Hermes, saw the stock banner, and reported
    the install broken (owner reports, 2026-08-20/21). The verdict is meant
    to print LAST: it names the exact next command per blocker — an old
    Hermes without the widget loader needs `hermes update`, which OMH never
    runs on its own — and states the two facts users trip on: a running
    Hermes session keeps its old chrome until restarted, and a ready install
    opens the styled TUI from either bare `omh` or bare `hermes`.
    """
    preflight = hermes_tui_preflight(paths)
    blockers = widget_render_blockers(preflight)
    install = preflight.get("install", {})
    loader = preflight.get("widget_loader", {})
    skin = preflight.get("display_skin", {})
    if not install.get("found"):
        status = "unknown"
    elif blockers:
        status = "blocked"
    else:
        status = "ready"
    next_commands: list[str] = []
    if install.get("found") and not loader.get("present"):
        next_commands.append("hermes update")
    notes: list[str] = []
    skin_value = str(skin.get("value") or "")
    theme = theme_for_skin_name(skin_value) if skin_value else None
    if theme is not None and theme.skin_name != SKIN_NAME:
        # An OMH theme is the OMH identity, not a foreign skin. Reporting
        # `omh-crimson` as "the user's explicit choice, banner keeps that look"
        # told operators the branding had been declined when it was active.
        notes.append(f"OMH theme {theme.short_name} is active (display.skin: {theme.skin_name}).")
    elif skin_value and theme is None:
        notes.append(
            f"display.skin is the user's explicit choice ({skin_value!r}); the banner keeps that look on purpose."
        )
    return {
        "schema_version": TUI_VERDICT_SCHEMA_VERSION,
        "status": status,
        "hermes_version": str(install.get("version") or ""),
        "blockers": blockers,
        "next_commands": next_commands,
        "notes": notes,
    }
