"""Pure, evidence-bounded qualification of a complete caller-supplied inventory.

This report never discovers models, calls providers, edits chains, or promotes a
model merely because a chain names it. Decisions are reviewed metadata, not
measurements. Inventory identity parsing is shared with the contract audit.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Final, Iterable, Mapping

from .model_contract_coverage import (
    MODEL_CONTRACT_COVERAGE_CLAIM_BOUNDARY,
    _digest,
    _inventory_model_refs,
    _inventory_record,
    _model_ref,
    _normalized_refs,
    _price_dimension,
    _validate_inventory_shape,
)
from .model_contracts import (
    EXACT_CONTRACT_POINTER_ALIASES,
    MODEL_CONTRACTS,
    contract_model_id,
    model_contract,
    model_contract_projection,
)
from .model_recommendations import SHIPPED_MODEL_RECOMMENDATIONS
from .model_routing import model_family
from .unit_prompt_protocol import (
    HIGH_EFFORT_CALIBRATIONS,
    MAIN_AGENT_COMPOSITION_CALIBRATIONS,
    MODEL_COMPOSITION_CALIBRATIONS,
    MODEL_HIGH_EFFORT_CALIBRATIONS,
)

MODEL_PORTFOLIO_QUALIFICATION_SCHEMA_VERSION: Final = "model_portfolio_qualification/v1"
PORTFOLIO_DISPOSITIONS: Final = (
    "recommended",
    "eligible_not_selected",
    "excluded_quality_dominated",
    "excluded_efficiency_dominated",
    "excluded_tool_unreliable",
    "excluded_runtime_incompatible",
    "excluded_superseded",
    "unmeasured",
)
_SECTIONS: Final = ("categories", "role_suggestions", "domain_affinities", "last_resort")

# Reviewed decisions, NOT a supported-model allowlist. Anything absent from
# these tables still gets a row, but never gets recommendation standing.
QUALIFICATION_HOLDS: Final[dict[str, dict[str, object]]] = {}
RECOMMENDATION_DECISIONS: Final[dict[str, tuple[str, ...]]] = {}
RETIREMENT_DECISIONS: Final[dict[str, dict[str, object]]] = {}


def _canonical_id(model_id: str) -> str:
    return model_id.rsplit("/", 1)[-1].casefold()


def _served_aliases(canonical: str) -> list[str]:
    # Only declared same-mode/tier pointers are bidirectional identities.
    # Astra modes, tiers, snapshots, and fuzzy spellings must not enter here.
    for exact, pointers in EXACT_CONTRACT_POINTER_ALIASES.items():
        if canonical == exact or canonical in pointers:
            return sorted((exact, *pointers))
    return [canonical]


def _calibration_resolution(model: str) -> dict[str, str]:
    key = contract_model_id(model)
    family = model_family(model)

    def level(exact: Mapping[str, str], families: Mapping[str, str]) -> str:
        if not model:
            return "missing"
        if key in exact:
            return "exact"
        if family not in ("", "unknown", "generic") and family in families:
            return "family"
        return "generic" if "generic" in families else "missing"

    return {
        "high_effort": level(MODEL_HIGH_EFFORT_CALIBRATIONS, HIGH_EFFORT_CALIBRATIONS),
        "composition": level(MODEL_COMPOSITION_CALIBRATIONS, MAIN_AGENT_COMPOSITION_CALIBRATIONS),
    }


def _placements(aliases: list[str], approved: tuple[str, ...]) -> list[dict[str, object]]:
    placements = []
    for section in _SECTIONS:
        for category, chain in SHIPPED_MODEL_RECOMMENDATIONS[section].items():
            scope = f"{section}:{category}"
            if scope not in approved:
                continue
            for candidate in chain:
                if _canonical_id(candidate["model_alias"]) in aliases:
                    placements.append({
                        "surface": section,
                        "category": category,
                        "model_alias": candidate["model_alias"],
                        "reason": candidate["reasoning"],
                        "reasoning_effort": candidate["reasoning_effort"],
                        "evidence_state": "editorial_not_measured",
                        "evidence_pointers": ["src/coding/model_recommendations.py:SHIPPED_MODEL_RECOMMENDATIONS"],
                    })
    return placements


def _stage(state: str, *pointers: str) -> dict[str, object]:
    return {"state": state, "evidence_pointers": list(pointers)}


def _qualification_row(
    model: str,
    identities: Mapping[str, object],
    read_date: str | None,
    exclusions: Mapping[str, str],
) -> dict[str, object]:
    canonical = _canonical_id(model)
    family = model_family(model)
    projection = model_contract_projection(model)
    contract_id = projection["contract_model_id"] if projection else None
    recognition = (
        "exact" if canonical in MODEL_CONTRACTS else
        "family" if family not in ("", "unknown") else
        "generic_fallback" if family == "unknown" else "unknown"
    )
    aliases = _served_aliases(canonical)
    resolution = _calibration_resolution(model)
    # Both halves must have standing; the weaker half describes pair coverage.
    levels = ("missing", "generic", "family", "exact")
    calibration = min(resolution.values(), key=levels.index)
    approval = next((RECOMMENDATION_DECISIONS[a] for a in aliases if a in RECOMMENDATION_DECISIONS), ())
    placements = _placements(aliases, approval)
    retirement = deepcopy(RETIREMENT_DECISIONS.get(canonical, {}))
    decision = deepcopy(QUALIFICATION_HOLDS.get(canonical, {}))
    if retirement and retirement["scope"] == "all_shipped_chains":
        decision = retirement
    exclusion = exclusions.get(model.casefold(), exclusions.get(canonical))
    if exclusion:
        decision = {
            "disposition": "excluded_runtime_incompatible",
            "reason": exclusion,
            "dimension": "runtime_contract",
            "evidence_state": "unmeasured",
            "evidence_pointers": ["caller:--intentional-exclusion"],
            "scope": "caller_inventory",
        }
    if decision:
        disposition = str(decision["disposition"])
        reason = str(decision["reason"])
        evidence_state = str(decision.get("evidence_state", "editorial_not_measured"))
        placements = []
    elif approval:
        disposition = "recommended" if placements else "eligible_not_selected"
        reason = "Reviewed editorial placement; not a role-evaluation measurement."
        evidence_state = "editorial_not_measured"
    else:
        disposition = "unmeasured"
        reason = "No reviewed model-and-role qualification decision; recognition alone cannot promote this id."
        evidence_state = "unmeasured"
    excluded = bool(decision)
    contract_coverage = (
        "intentional_exclusion" if excluded else
        "exact" if projection and projection["provenance"] == "exact" else
        "declared_inheritance" if projection else "missing"
    )
    price = _price_dimension(model_contract(model), projection)
    evidence = {
        dimension: {"state": "unmeasured", "evidence_pointers": []}
        for dimension in ("quality", "tool_reliability", "latency")
    }
    evidence["cost"] = {
        **price,
        "state": "documented_list" if price["status"] == "documented_list" else "unmeasured",
        "evidence_pointers": ["src/coding/model_contracts.py"] if contract_id else [],
    }
    stages = {
        "recognition_probe": _stage("shipped_metadata", "src/coding/model_routing.py:model_family"),
        "research_dossier": _stage("documented_reference" if contract_id else "not_verified",
                                   "docs/MODEL-ONBOARDING.md#2-research-official-first--four-lanes-in-parallel"),
        "calibration_pair": _stage("shipped_metadata" if calibration in ("exact", "family") else "not_verified",
                                   "src/coding/unit_prompt_protocol.py", "MODEL_OPTI.md"),
        "chain_placement": _stage("editorial_not_measured" if placements else "not_verified",
                                  "src/coding/model_recommendations.py"),
        "price_row": _stage("documented_reference" if contract_id else "not_verified",
                            "src/coding/model_contracts.py"),
        "measurement_pair": _stage("not_verified", "docs/MODEL-ONBOARDING.md#8-close-with-measurement"),
    }
    return {
        "requested_model": model,
        "canonical_model_id": canonical,
        "aliases": list(identities["identities"]),
        "served_aliases": aliases,
        "family": family,
        "recognition_mode": recognition,
        "contract_model_id": contract_id,
        "contract_projection": projection,
        "contract_coverage": contract_coverage,
        "calibration_coverage": "intentional_exclusion" if excluded else calibration,
        "calibration_resolution": resolution,
        "optimization": {
            "status": "calibration_present" if calibration in ("exact", "family") else "not_optimized",
            "evidence_state": evidence_state,
            "stages": stages,
        },
        "recommendation_eligibility": placements,
        "evidence": evidence,
        "evidence_state": evidence_state,
        "disposition": disposition,
        "reason": reason,
        "decision": decision or None,
        "retirement_decisions": [retirement] if retirement else [],
        "inventory_evidence": deepcopy(dict(identities)),
        "inventory_read_date": read_date,
        "model_version_boundary": {
            "canonical_model_id": canonical,
            "contract_model_id": contract_id,
            "projection_provenance": projection["provenance"] if projection else "missing",
            "pointer_read_date": "2026-09-11" if len(aliases) > 1 else None,
            "qualification_scope": "explicit_model_and_role_only",
        },
    }


def build_model_portfolio_qualification(
    inventory: Mapping[str, object],
    *,
    required_models: Iterable[str] = (),
    intentional_exclusions: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Qualify every supplied id; absent requirements block without invented rows.

    Exclusions are caller-declared runtime-contract exclusions, not empirical
    quality failures. Read dates are supplied provenance, never wall-clock time.
    """
    if not isinstance(inventory, Mapping):
        raise ValueError("model portfolio inventory must be a JSON object")
    _validate_inventory_shape(inventory)
    refs, identities = _inventory_model_refs(inventory)
    record = _inventory_record(inventory, refs, identities)
    read_date = inventory.get("read_date")
    if read_date is not None:
        if not isinstance(read_date, str) or date.fromisoformat(read_date).isoformat() != read_date:
            raise ValueError("inventory read_date must be an ISO date (YYYY-MM-DD)")
    exclusions: dict[str, str] = {}
    if intentional_exclusions is not None:
        if not isinstance(intentional_exclusions, Mapping):
            raise ValueError("intentional exclusions must map model ids to reasons")
        for model, reason in intentional_exclusions.items():
            key = _model_ref(model).casefold()
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 2048 or any(ord(c) < 32 for c in reason):
                raise ValueError("intentional exclusion reason must be non-empty text without control characters (maximum 2048 characters)")
            if key in exclusions and exclusions[key] != reason.strip():
                raise ValueError("conflicting intentional exclusion reasons")
            exclusions[key] = reason.strip()
    required = _normalized_refs(required_models)
    rows = [_qualification_row(model, identities[model.casefold()], read_date, exclusions) for model in refs]
    by_id = {row["requested_model"].casefold(): row for row in rows}
    required_gaps = []
    for model in required:
        row = by_id.get(model.casefold())
        if row is None or (row["disposition"] == "unmeasured" and not row["decision"]):
            required_gaps.append(model)
    counts = {disposition: 0 for disposition in PORTFOLIO_DISPOSITIONS}
    for row in rows:
        counts[row["disposition"]] += 1
    outcome = (
        "required_gaps" if required_gaps else
        f"{record['status']}_inventory" if record["status"] != "observed" else
        "optional_gaps" if counts["unmeasured"] else "qualified"
    )
    comparison = {
        "inventory": {**record, "read_date": read_date},
        "models": rows,
        "requirements": {"required_models": list(required), "intentional_exclusions": dict(sorted(exclusions.items()))},
        "summary": {"total_models": len(rows), "disposition_counts": counts,
                    "required_gaps": required_gaps, "outcome": outcome},
    }
    return {
        "schema_version": MODEL_PORTFOLIO_QUALIFICATION_SCHEMA_VERSION,
        "claim_boundary": MODEL_CONTRACT_COVERAGE_CLAIM_BOUNDARY,
        "comparison": comparison,
        "comparison_digest": _digest(comparison),
        "blocking": bool(required_gaps),
    }
