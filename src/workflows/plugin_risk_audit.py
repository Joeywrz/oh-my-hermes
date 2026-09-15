from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Final, Literal, TypeAlias

from .plugin_audit_source_io import PluginAuditSource, read_static_plugin_sources, resolve_plugin_audit_root
from .plugin_hook_contract import (
    CANONICAL_HOOK_FIELD,
    MANIFEST_FILENAME,
    PLUGIN_HOOK_CONTRACT_HOST,
    PLUGIN_HOOK_CONTRACT_RANGE,
    PLUGIN_HOOK_CONTRACT_SOURCE,
    SECONDARY_HOOK_FIELD,
    HookDeclaration,
    PluginHookDeclarationError,
    read_hook_declaration,
)
from .plugin_manifest_yaml import PluginManifestFormatError


PLUGIN_RISK_AUDIT_SCHEMA_VERSION: Final = "plugin_risk_audit/v1"
# Deliberately still three names. A text match says a name occurs in a scanned
# file, which is a weaker thing than a declaration and cannot carry execution
# semantics -- widening it was the alternative #1536 rejected. The manifest's
# declared hooks are the detector now; this stays as the secondary signal that
# catches a plugin whose code names a hook its manifest does not.
_HOOK_CAPABILITY = re.compile(r"\b(?:on_session_end|pre_llm_call|pre_tool_call)\b")
_PROCESS_EXECUTION = re.compile(
    r"\b(?:os\.system|subprocess\.(?:call|check_call|check_output|Popen|run)|child_process\.(?:exec|execFile|fork|spawn))\s*\("
)
_DYNAMIC_EXECUTION = re.compile(r"(?<!\.)\b(?:compile|eval|exec)\s*\(|\bnew\s+Function\s*\(")
_NETWORK_REQUEST = re.compile(
    r"\b(?:axios|httpx|requests|urllib(?:\.request|3)?|https?|http)\.(?:get|post|put|request|urlopen)\s*\(|\bfetch\s*\("
)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(?:api[_-]?key|password|private[_-]?key|secret|token)\b\s*[:=]\s*['\"][A-Za-z0-9_-]{16,}['\"]",
    re.IGNORECASE,
)
_DEPENDENCY_DECLARATION = re.compile(r"(?:^\s*dependencies\s*=|^\s*[A-Za-z0-9_.-]+\s*(?:[<>=!~]|$))", re.MULTILINE)
_PACKAGE_DEPENDENCY_FIELDS: Final = frozenset({"dependencies", "optionalDependencies", "peerDependencies"})

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
ManifestStatus: TypeAlias = Literal["invalid_json", "missing", "present"]
# `manifest_status` above is the closed vocabulary two consumers already read,
# and it answers a question about `plugin.json`. A root `plugin.yaml` -- the
# manifest Hermes itself parses -- gets its own value rather than being folded
# into a JSON-shaped word.
ManifestYamlStatus: TypeAlias = Literal["absent", "present", "unreadable"]
RiskCategory: TypeAlias = Literal[
    "declared_dependency",
    "dynamic_code_execution",
    "hermes_hook_capability",
    "network_request",
    "potential_committed_secret",
    "process_execution",
    # "I could not establish this plugin's hook contract" must not render as
    # "this plugin is fine". Without a category of its own, an unreadable
    # manifest produced a summary byte-identical to a plugin that declares
    # nothing, and a wrapper reading only the summary saw a clean plugin.
    "undetermined_hook_contract",
]


@dataclass(frozen=True, slots=True)
class _Undecodable:
    """A root manifest whose bytes are not UTF-8, distinct from one that is absent."""


_UNDECODABLE_MANIFEST: Final = _Undecodable()


@dataclass(frozen=True, slots=True)
class _StaticSource:
    manifest_status: ManifestStatus | None
    byte_count: int
    risk_categories: frozenset[RiskCategory]


def audit_plugin_risk(plugin_root: Path) -> JsonObject:
    root = resolve_plugin_audit_root(plugin_root)
    raw_sources = read_static_plugin_sources(root)
    sources = tuple(_audit_static_source(source) for source in raw_sources)
    manifest_yaml_status, declared_hooks = _declared_hook_report(_root_manifest_text(raw_sources))
    categories = sorted(
        {category for source in sources for category in source.risk_categories} | _hook_categories(declared_hooks)
    )
    return {
        "schema_version": PLUGIN_RISK_AUDIT_SCHEMA_VERSION,
        "source": {
            "explicit_root": True,
            "manifest_status": _manifest_status(sources),
            "manifest_yaml_status": manifest_yaml_status,
        },
        "summary": {
            "scanned_file_count": len(sources),
            "scanned_byte_count": sum(source.byte_count for source in sources),
            "risk_categories": categories,
            "risk_category_count": len(categories),
        },
        "declared_hooks": declared_hooks,
        "not_observed": {
            "plugin_import": {"status": "not_observed"},
            "plugin_registration": {"status": "not_observed"},
            "plugin_execution": {"status": "not_observed"},
            "plugin_hook_registration": {"status": "not_observed"},
            "plugin_hook_execution": {"status": "not_observed"},
            "plugin_hook_timeout_enforcement": {"status": "not_observed"},
            "plugin_hook_failure_handling": {"status": "not_observed"},
            "dependency_installation": {"status": "not_observed"},
            "network_access": {"status": "not_observed"},
            "ci_annotation_publication": {"status": "not_observed"},
        },
        "claim_boundary": (
            "The audit statically reads bounded text from one explicitly named local plugin directory and returns only "
            "aggregate risk categories and the execution semantics its manifest's declared hooks would have on the "
            "supported host contract. A declared hook is an advisory static risk contract, not evidence that the hook "
            "registered, ran, stayed inside its budget, or handled a failure correctly, and an absent declaration is "
            "not evidence that the plugin registers no hooks. It does not expose plugin source, import or execute "
            "plugin code, register a plugin, install dependencies, access a network, publish CI annotations, or prove "
            "that the plugin is safe."
        ),
    }


def scanned_text_risk_categories(name: str, text: str) -> tuple[RiskCategory, ...]:
    """The static risk categories one named text body matches, sorted.

    Public so a caller scanning bytes it is about to write -- skill promotion
    reads a reviewed draft's own instruction text this way -- reuses these
    detectors instead of growing a second copy that then drifts from them. The
    claim is the audit's own weak one: a text match says a pattern occurs in
    this text, never that anything ran.
    """
    return tuple(sorted(_risk_categories(name, text)))


def _audit_static_source(source: PluginAuditSource) -> _StaticSource:
    text = source.content.decode("utf-8", errors="replace")
    return _StaticSource(
        _source_manifest_status(source.name, source.is_root_file, text),
        len(source.content),
        _risk_categories(source.name, text),
    )


def _hook_categories(declared_hooks: JsonObject) -> set[RiskCategory]:
    """Summary-level categories the hook report contributes.

    The second one is the invariant that keeps a failure visible in the
    aggregate. `classification_status` is `unknown` for every way the contract
    could not be established -- an unreadable manifest, a malformed declaration,
    an unsupported or unreadable host range, a hook name the contract does not
    contain -- and each of those must leave a mark on the summary, because the
    summary is what a wrapper reads. Tying the category to the status rather
    than to any one cause also covers subset gaps nobody has found yet.
    """
    categories: set[RiskCategory] = set()
    if declared_hooks["hook_count"]:
        categories.add("hermes_hook_capability")
    if declared_hooks["classification_status"] != "classified":
        categories.add("undetermined_hook_contract")
    return categories


def _root_manifest_text(sources: tuple[PluginAuditSource, ...]) -> str | None | _Undecodable:
    """Return the root `plugin.yaml` text, None when absent, or an undecodable marker.

    The bytes are decoded strictly here, unlike the scanned-text path above.
    Replacing invalid bytes there costs nothing -- a regex over mangled text
    still finds what it finds -- but doing it to a manifest would classify hooks
    out of a file Hermes' own loader cannot read at all, and present the result
    as an established contract.
    """
    for source in sources:
        if source.is_root_file and source.name == MANIFEST_FILENAME:
            try:
                return source.content.decode("utf-8")
            except UnicodeDecodeError:
                return _UNDECODABLE_MANIFEST
    return None


def _declared_hook_report(manifest_text: str | None | _Undecodable) -> tuple[ManifestYamlStatus, JsonObject]:
    """Classify the manifest's declared hooks, or say why it could not.

    A hostile or broken manifest is a finding about the plugin, not a failure of
    the audit: the reader raises, and the raise becomes a bounded diagnostic
    with no hooks classified rather than an aborted scan. The ways it can fail
    stay apart -- undecodable bytes and a document outside the readable subset
    leave the manifest `unreadable`, while a document that parses with a
    malformed hook declaration is `present` with an invalid declaration.
    """
    if manifest_text is None:
        return "absent", _hook_report("absent", None, (), manifest_present=False)
    if isinstance(manifest_text, _Undecodable):
        return "unreadable", _hook_report("invalid", None, ("plugin manifest is not valid UTF-8",))
    try:
        declaration = read_hook_declaration(manifest_text)
    except PluginManifestFormatError as exc:
        return "unreadable", _hook_report("invalid", None, (str(exc)[:200],))
    except PluginHookDeclarationError as exc:
        return "present", _hook_report("invalid", None, (str(exc)[:200],))
    return "present", _hook_report("declared" if declaration.hooks else "absent", declaration, ())


def _hook_report(
    declaration_status: str,
    declaration: HookDeclaration | None,
    diagnostics: tuple[str, ...],
    *,
    manifest_present: bool = True,
) -> JsonObject:
    hooks = declaration.hooks if declaration is not None else ()
    unknown = sum(1 for hook in hooks if hook.contract.effect == "unknown")
    range_state = declaration.range_status if declaration is not None else "undeclared"
    findings = list(diagnostics) + _hook_diagnostics(declaration, unknown)
    if not manifest_present:
        findings.append(
            "no root plugin.yaml was scanned, so the plugin's declared hook contract was never established"
        )
    # `manifest_present` is the same rule one level down, and the distinction it
    # draws is the whole point: a manifest that is present and declares no hooks
    # was READ and found empty, which is a result; a plugin with no manifest was
    # NEVER READ, which is a gap. Both produce an empty hook list, so a consumer
    # keying on `classification_status` alone must not see the second as a
    # positive verdict.
    classified = (
        manifest_present
        and declaration_status != "invalid"
        and not unknown
        and range_state in ("supported", "undeclared")
    )
    return {
        "declaration_status": declaration_status,
        "classification_status": "classified" if classified else "unknown",
        "host_contract": {
            "host": PLUGIN_HOOK_CONTRACT_HOST,
            "supported_range": PLUGIN_HOOK_CONTRACT_RANGE,
            "contract_source": PLUGIN_HOOK_CONTRACT_SOURCE,
            "declared_range": _declared_range_value(declaration),
            "declared_range_status": range_state,
        },
        "hook_count": len(hooks),
        "unknown_hook_count": unknown,
        "effects": sorted({hook.contract.effect for hook in hooks}),
        "hooks": [
            {
                "hook": hook.name,
                "declared_in": hook.field,
                "effect": hook.contract.effect,
                "host_contract": hook.contract.host_contract,
                "timeout_semantics": hook.contract.timeout_semantics,
            }
            for hook in hooks
        ],
        "diagnostics": findings,
    }


def _declared_range_value(declaration: HookDeclaration | None) -> str:
    """Echo the declared range only once it parsed; otherwise a sentinel."""
    if declaration is None or declaration.declared_range is None:
        return "<absent>"
    if declaration.range_status == "unparsable":
        return "<invalid>"
    return declaration.declared_range


def _hook_diagnostics(declaration: HookDeclaration | None, unknown: int) -> list[str]:
    if declaration is None:
        return []
    findings: list[str] = []
    if declaration.range_status == "unsupported":
        findings.append(
            f"manifest requires a {PLUGIN_HOOK_CONTRACT_HOST} version outside the supported host contract "
            f"{PLUGIN_HOOK_CONTRACT_RANGE}; declared hook semantics are unknown"
        )
    elif declaration.range_status == "unparsable":
        findings.append(
            "manifest declares an unreadable Hermes version range; declared hook semantics are unknown"
        )
    elif declaration.range_status == "undeclared" and declaration.hooks:
        findings.append(
            f"manifest declares no Hermes version range; hooks are classified against {PLUGIN_HOOK_CONTRACT_RANGE}"
        )
    if unknown:
        findings.append(
            f"{unknown} declared hook name(s) are not in the supported host contract and cannot be classified"
        )
    if any(hook.field == SECONDARY_HOOK_FIELD for hook in declaration.hooks):
        findings.append(
            f"hook(s) declared under `{SECONDARY_HOOK_FIELD}`, which the Hermes manifest parser does not read into "
            f"`{CANONICAL_HOOK_FIELD}`"
        )
    return findings


def _source_manifest_status(name: str, is_root_file: bool, text: str) -> ManifestStatus | None:
    if name != "plugin.json" or not is_root_file:
        return None
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return "invalid_json"
    return "present"


def _risk_categories(name: str, text: str) -> frozenset[RiskCategory]:
    categories: set[RiskCategory] = set()
    if _declares_dependencies(name, text):
        categories.add("declared_dependency")
    if _HOOK_CAPABILITY.search(text):
        categories.add("hermes_hook_capability")
    if _PROCESS_EXECUTION.search(text):
        categories.add("process_execution")
    if _DYNAMIC_EXECUTION.search(text):
        categories.add("dynamic_code_execution")
    if _NETWORK_REQUEST.search(text):
        categories.add("network_request")
    if _SECRET_ASSIGNMENT.search(text):
        categories.add("potential_committed_secret")
    return frozenset(categories)


def _declares_dependencies(name: str, text: str) -> bool:
    if name in {"pyproject.toml", "requirements.txt", "setup.cfg", "setup.py"}:
        return _DEPENDENCY_DECLARATION.search(text) is not None
    if name != "package.json":
        return False
    try:
        package = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(package, dict) and any(
        isinstance(package.get(field), dict) and package[field] for field in _PACKAGE_DEPENDENCY_FIELDS
    )


def _manifest_status(sources: tuple[_StaticSource, ...]) -> ManifestStatus:
    statuses = {source.manifest_status for source in sources if source.manifest_status is not None}
    if "invalid_json" in statuses:
        return "invalid_json"
    if "present" in statuses:
        return "present"
    return "missing"
