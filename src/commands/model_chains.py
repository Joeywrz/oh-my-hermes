"""View and edit the per-category mixture model chains.

The chains live as user config in `<omh-home>/routing/model-chains.json`
(`mixture_chain_overrides/v1`): the document replaces only the categories it
names and the shipped defaults keep serving the rest. This command is the
supported editing surface over that file:

* `omh model-chains` (bare, on a terminal) — the arrow-key picker: one row
  per category, left/right steps the head model, -/+ its effort, Enter saves.
  `omh model` is the same command under a shorter name. Off a terminal the
  bare form prints `show`.
* `omh model-chains show` — the current effective chain per category, with
  its origin (shipped default vs override) and the document status.
* `omh model-chains set <category> "model[:effort], model[:effort]"` — write
  one category's replacement chain; `--clear` returns it to the default.
* `omh model-chains interview` — walk every category with numbered choices
  (keep / shipped default / Ultrafast tier / custom entry) on a terminal.

Editing the JSON file directly stays equally supported; every path converges
on the same validated document.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from ..catalogs.model_chain_table import CHAIN_SURFACE_PURPOSES, MODEL_DISPLAY_LABELS
from ..local_store import atomic_write_text
from ..plugin_bundle.omh.hermes_delegation import (
    APPROX_PRICE_PER_MTOK,
    HERMES_MIXTURE_CATEGORY_CHAINS,
    alias_is_served,
    entitlement_shaped_chain,
    load_mixture_chain_overrides,
    load_model_provider_routes,
    effective_provider_entitlements,
    mixture_chain_overrides_path,
    parse_mixture_chain_overrides,
    provider_entitlements_path,
)
from ..plugin_bundle.omh.model_chain_picker import (
    chain_text,
    compose_override_document,
    picker_rows,
    read_override_document,
)
from ..install.config_adapter import display_skin_selection, read_config
from ..skin_pack import skin_colors, theme_for_skin_name
from .common import _paths
from .model_chain_picker import default_palette, pick_chains_interactively

# From `quickstart`, not `setup`, for the reason `theme` gives: importing the
# parser module back here would close an import cycle.
from .quickstart import _use_color
from .theme_picker import picker_available

_ENTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")

# The Ultrafast-tier interview option: a chain member is offered its
# `<model>-ultrafast` speed variant when that token is a known model (shipped
# chains or the price table), so no variant is invented. Editorial offer, not
# a capability claim; the option is offered only when it changes the chain.
_KNOWN_MODEL_TOKENS = frozenset(APPROX_PRICE_PER_MTOK) | {
    model for chain in HERMES_MIXTURE_CATEGORY_CHAINS.values() for model, _ in chain
}
_ULTRAFAST_SWAPS = {
    model: f"{model}-ultrafast"
    for model in _KNOWN_MODEL_TOKENS
    if f"{model}-ultrafast" in _KNOWN_MODEL_TOKENS
}


def _parsechain_text(text: str) -> tuple[tuple[str, str], ...]:
    """Parse `model[:effort], model[:effort]` into chain entries or raise ValueError."""
    entries: list[tuple[str, str]] = []
    for piece in text.split(","):
        piece = piece.strip()
        if not piece:
            continue
        model, _, effort = piece.partition(":")
        model = model.strip()
        effort = effort.strip()
        if not _ENTRY_RE.match(model):
            raise ValueError(f"{model!r} is not a plain model identifier")
        if effort and not _ENTRY_RE.match(effort):
            raise ValueError(f"{effort!r} is not a plain reasoning-effort token")
        entries.append((model, effort))
    if not entries:
        raise ValueError("a chain needs at least one `model[:effort]` entry")
    return tuple(entries)


def _write_document(omh_home, document: dict[str, object]) -> str:
    _, status = parse_mixture_chain_overrides(document)
    if status.startswith("invalid"):
        raise ValueError(status)
    path = mixture_chain_overrides_path(omh_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return str(path)


def _state(omh_home, hermes_home=None) -> dict[str, object]:
    overrides, status = load_mixture_chain_overrides(omh_home)
    entitlements, entitlement_status, providers = effective_provider_entitlements(omh_home, hermes_home)
    routes, _ = load_model_provider_routes(omh_home)
    categories = []
    for name, default_chain in HERMES_MIXTURE_CATEGORY_CHAINS.items():
        chain = overrides.get(name, default_chain)
        shaped = chain
        if entitlements is not None:
            shaped = entitlement_shaped_chain(chain, entitlements, routes)
        categories.append(
            {
                "category": name,
                "chain": [
                    {
                        "model": model,
                        "reasoning_effort": effort,
                        "served": (
                            alias_is_served(model, entitlements, routes)
                            if entitlements is not None
                            else True
                        ),
                    }
                    for model, effort in shaped
                ],
                "chain_text": chain_text(shaped),
                "origin": "override" if name in overrides else "default",
                "entitlement_shaped": shaped != chain,
            }
        )
    return {
        "schema_version": "model_chain_state/v1",
        "path": str(mixture_chain_overrides_path(omh_home)),
        "document_status": status,
        "entitlements_path": str(provider_entitlements_path(omh_home)),
        "entitlements_status": entitlement_status,
        "providers": [dict(row) for row in providers],
        "categories": categories,
    }


def _print_state(state: dict[str, object]) -> None:
    print("Current model chains (category -> effective order):")
    for row in state["categories"]:
        marker = " (override)" if row["origin"] == "override" else ""
        if row.get("entitlement_shaped"):
            marker += " (reordered by this machine's providers)"
        print(f"  {row['category']}: {row['chain_text']}{marker}")
    print(f"Overrides file: {state['path']} [{state['document_status']}]")
    print(f"Provider entitlements: {state['entitlements_path']} [{state['entitlements_status']}]")
    linked = [row for row in state.get("providers", []) if row["source"] != "recorded"]
    if linked:
        print("Linked Hermes providers: " + ", ".join(f"{row['id']} ({row['source']})" for row in linked))
    else:
        print("Linked Hermes providers: none found (a `hermes auth` login, a config provider, or a key name counts)")
    print("Edit a category with `omh model-chains set <category> \"model[:effort], ...\"`,")
    print("walk all of them with `omh model-chains interview`, or edit the JSON directly.")


def cmd_model_chains_show(args: argparse.Namespace) -> int:
    paths = _paths(args)
    state = _state(paths.omh_home, paths.hermes_home)
    if getattr(args, "json", False):
        print(json.dumps(state, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    else:
        _print_state(state)
    return 0


def cmd_model_chains_set(args: argparse.Namespace) -> int:
    paths = _paths(args)
    omh_home = paths.omh_home
    category = str(args.category)
    if category not in HERMES_MIXTURE_CATEGORY_CHAINS:
        print(
            f"omh: unknown category {category!r}; choose one of "
            + ", ".join(HERMES_MIXTURE_CATEGORY_CHAINS),
            file=sys.stderr,
        )
        return 2
    document = read_override_document(omh_home)
    categories = document["categories"]
    assert isinstance(categories, dict)
    if args.clear:
        categories.pop(category, None)
    else:
        if not args.chain:
            print("omh: set needs a chain (`model[:effort], ...`) or --clear", file=sys.stderr)
            return 2
        try:
            entries = _parsechain_text(" ".join(args.chain))
        except ValueError as exc:
            print(f"omh: {exc}", file=sys.stderr)
            return 2
        categories[category] = [
            {"model": model, "reasoning_effort": effort} for model, effort in entries
        ]
    try:
        path = _write_document(omh_home, document)
    except ValueError as exc:
        print(f"omh: refused to write an invalid document: {exc}", file=sys.stderr)
        return 2
    state = _state(omh_home, paths.hermes_home)
    if getattr(args, "json", False):
        print(json.dumps(state, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    else:
        row = next(r for r in state["categories"] if r["category"] == category)
        print(f"{category}: {row['chain_text']} ({row['origin']})")
        print(f"Written to {path}. New delegations use this order; running children keep theirs.")
    return 0


def _active_palette(paths) -> dict[str, str]:
    """The active OMH skin's colours, so the picker matches the TUI; the
    default skin when `display.skin` is unset or not an OMH theme."""
    theme = theme_for_skin_name(display_skin_selection(read_config(paths.hermes_config_path)))
    return skin_colors(theme.skin_name) if theme else default_palette()


def cmd_model_chains_pick(args: argparse.Namespace) -> int:
    """Bare `omh model-chains` picks interactively; anything else prints `show`.

    The split is the theme command's: `show` is the scriptable surface and
    stays byte-predictable, the bare form is what a person types to SEE the
    chains and move them. `--json` and any non-terminal end degrade the bare
    form to the same listing.
    """
    if getattr(args, "json", False) or not picker_available():
        return cmd_model_chains_show(args)
    paths = _paths(args)
    omh_home = paths.omh_home
    payload = picker_rows(
        omh_home, hermes_home=paths.hermes_home, labels=MODEL_DISPLAY_LABELS, purposes=CHAIN_SURFACE_PURPOSES
    )
    changes = pick_chains_interactively(payload, use_color=_use_color(), palette=_active_palette(paths))
    if changes is None:
        print(f"Cancelled; {payload['path']} was not changed.")
        return 0
    if not changes:
        print("No changes.")
        return 0
    try:
        document = compose_override_document(read_override_document(omh_home), changes)
        path = _write_document(omh_home, document)
    except ValueError as exc:
        print(f"omh: refused to write an invalid document: {exc}", file=sys.stderr)
        return 2
    print(f"Saved {len(changes)} categor{'y' if len(changes) == 1 else 'ies'} to {path}.")
    _print_state(_state(omh_home, paths.hermes_home))
    return 0


def _ultrafast_variant(
    chain: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...] | None:
    swapped = tuple((_ULTRAFAST_SWAPS.get(model, model), effort) for model, effort in chain)
    return swapped if swapped != chain else None


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def model_chains_interview(paths) -> int:
    """Numbered per-category interview on a terminal.

    Non-interactive callers (agents, pipes) get a refusal that names the
    scriptable path instead of a hanging prompt.

    Takes resolved paths rather than the parsed args so the interactive
    `omh setup` wizard can offer the same walk inline, exactly the way
    `category_maestro_interview` serves the Maestro lane's category dial.
    """
    if not _stdin_is_tty():
        print(
            "omh: interview needs a terminal; use `omh model-chains show` and "
            "`omh model-chains set <category> \"model[:effort], ...\"` instead.",
            file=sys.stderr,
        )
        return 2
    omh_home = paths.omh_home
    overrides, _ = load_mixture_chain_overrides(omh_home)
    document = read_override_document(omh_home)
    categories = document["categories"]
    assert isinstance(categories, dict)
    print("Model chain interview — Enter keeps the current order.")
    changed = 0
    for name, default_chain in HERMES_MIXTURE_CATEGORY_CHAINS.items():
        current = overrides.get(name, default_chain)
        options: list[tuple[str, tuple[tuple[str, str], ...] | None]] = [
            (f"keep current: {chain_text(current)}", current),
        ]
        if current != default_chain:
            options.append((f"shipped default: {chain_text(default_chain)}", default_chain))
        ultrafast = _ultrafast_variant(current)
        if ultrafast is not None:
            options.append((f"Ultrafast tier: {chain_text(ultrafast)}", ultrafast))
        options.append(("custom entry (`model[:effort], ...`)", None))
        print(f"\n[{name}]")
        for index, (label, _chain) in enumerate(options, start=1):
            print(f"  {index}) {label}")
        raw = input(f"choose 1-{len(options)} [1]: ").strip() or "1"
        try:
            pick = int(raw)
        except ValueError:
            pick = 0
        if not 1 <= pick <= len(options):
            print("  unrecognized choice; keeping current")
            continue
        label, selected = options[pick - 1]
        if selected is None:
            custom = input("  chain: ").strip()
            try:
                selected = _parsechain_text(custom)
            except ValueError as exc:
                print(f"  {exc}; keeping current")
                continue
        if selected == current:
            continue
        if selected == default_chain:
            categories.pop(name, None)
        else:
            categories[name] = [
                {"model": model, "reasoning_effort": effort} for model, effort in selected
            ]
        changed += 1
    if not changed:
        print("\nNo changes.")
        return 0
    try:
        path = _write_document(omh_home, document)
    except ValueError as exc:
        print(f"omh: refused to write an invalid document: {exc}", file=sys.stderr)
        return 2
    print(f"\nSaved {changed} categor{'y' if changed == 1 else 'ies'} to {path}.")
    _print_state(_state(omh_home, paths.hermes_home))
    return 0


def cmd_model_chains_interview(args: argparse.Namespace) -> int:
    return model_chains_interview(_paths(args))


def _add_model_chains_commands(sub) -> None:
    chains = sub.add_parser(
        "model-chains",
        aliases=["model"],
        help="View and edit the per-category mixture model chains (routing, fallback, HUD labels).",
    )
    chains.add_argument("--json", action="store_true", help="Print the machine-readable state payload.")
    chains.set_defaults(func=cmd_model_chains_pick, model_chains_command=None)
    chains_sub = chains.add_subparsers(dest="model_chains_command")

    show = chains_sub.add_parser("show", help="Show the effective chain per category and its origin.")
    show.add_argument("--json", action="store_true", help="Print the machine-readable state payload.")
    show.set_defaults(func=cmd_model_chains_show)

    set_cmd = chains_sub.add_parser("set", help="Replace one category's chain (or --clear it back to the default).")
    set_cmd.add_argument("category", help="Mixture category name (e.g. quick, architect).")
    set_cmd.add_argument("chain", nargs="*", help='Replacement chain: "model[:effort], model[:effort], ..."')
    set_cmd.add_argument("--clear", action="store_true", help="Remove the override so the shipped default applies again.")
    set_cmd.add_argument("--json", action="store_true", help="Print the machine-readable state payload.")
    set_cmd.set_defaults(func=cmd_model_chains_set)

    interview = chains_sub.add_parser(
        "interview",
        help="Walk every category with numbered choices (keep / default / Ultrafast / custom).",
    )
    interview.set_defaults(func=cmd_model_chains_interview)
