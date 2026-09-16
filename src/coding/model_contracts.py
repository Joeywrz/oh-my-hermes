"""Exact-model contracts and bounded declared inheritance.

A contract is prepared metadata — the vendor's published effort vocabulary,
limits, tool-calling surface, pricing, and runtime mechanisms for one exact
model id, each with the page it was read from. It is consulted by the route
resolver (to keep a documented-unsupported effort from reaching a provider
silently) and printed by `omh coding model-contract`. It never proves that a
provider serves the model to this account, that a runtime implements any of
the mechanisms named here, or that a route ran.

Contracts are keyed by exact model id after the provider prefix is stripped.
Provider catalogs may expose a bounded set of declared mode/service-tier aliases
for one contract. Those aliases resolve only through the explicit projection
table below; arbitrary suffix stripping is forbidden. A model without an exact
or declared contract gets `None`, never a guess.
"""

from __future__ import annotations

import re
from typing import Final, Mapping

MODEL_CONTRACT_SCHEMA_VERSION: Final[str] = "model_contract/v1"
MODEL_CONTRACT_PROJECTION_SCHEMA_VERSION: Final[str] = "model_contract_projection/v1"

MODEL_CONTRACT_CLAIM_BOUNDARY: Final[str] = (
    "A model contract is the vendor's documented interface for one model id, read from the cited "
    "pages on the cited date. It is not evidence that any provider serves the model to this "
    "account, that a runtime implements the named mechanisms, or that a route ran; observed "
    "behavior comes only from the runtime that actually ran the model."
)

MODEL_CONTRACT_PROJECTION_CLAIM_BOUNDARY: Final[str] = (
    "A model contract projection records how OMH interprets one explicitly declared catalog id. "
    "It is not evidence that a provider advertises or serves the requested id, that an account is "
    "entitled to it, or that execution occurred. The host/runtime owns any wire translation."
)

# Compatibility outcomes a contract can hand the route resolver.
EFFORT_FLOOR_KIND: Final[str] = "floor_raised"

# The data-handling axis (issue #1560): two closed vocabularies plus prose,
# because a policy value outside a fixed set cannot be gated on.
#
# Every value here describes the VENDOR'S DOCUMENTED DEFAULT for the surface
# the contract was read from, on `sources_read`. It is never an account-level
# fact, for the same reason `rollout` is not: tier, region, and a negotiated
# agreement all move it, in both directions. An enterprise agreement can
# exclude data the default page includes, and an opted-in consumer tier can
# include data the default excludes. So the axis carries what the vendor
# publishes, the gate repeats that in its claim boundary, and neither ever
# reports what THIS account agreed to.
#
# `not_recorded` is the honest value when the contract carries no reading of
# a vendor data-usage page. It is neither permitting nor conflicting; it is
# the case the gate exists to exclude, and it says so by name rather than by
# an absent key.
TRAINING_USE_EXCLUDED: Final[str] = "excluded_by_default"
TRAINING_USE_INCLUDED: Final[str] = "included_by_default"
TRAINING_USE_NOT_RECORDED: Final[str] = "not_recorded"
DATA_HANDLING_TRAINING_USE_VALUES: Final[tuple[str, ...]] = (
    TRAINING_USE_EXCLUDED,
    TRAINING_USE_INCLUDED,
    TRAINING_USE_NOT_RECORDED,
)

RETENTION_NONE: Final[str] = "none"
RETENTION_BOUNDED: Final[str] = "bounded"
RETENTION_INDEFINITE: Final[str] = "indefinite"
RETENTION_NOT_RECORDED: Final[str] = "not_recorded"
DATA_HANDLING_RETENTION_VALUES: Final[tuple[str, ...]] = (
    RETENTION_NONE,
    RETENTION_BOUNDED,
    RETENTION_INDEFINITE,
    RETENTION_NOT_RECORDED,
)

DATA_HANDLING_ACCOUNT_SCOPE: Final[str] = (
    "vendor-documented default for the surface this contract was read from; account tier, "
    "region, and a negotiated agreement all change it in both directions, so this is not "
    "account-level evidence and never states what this account agreed to"
)

# The reading both shipped contracts carry today. Their `sources` are vendor
# model, pricing, and capability pages; none is a data-usage or retention
# page, so no data-handling value was read and none is invented here.
# Replacing this on a contract means reading that vendor's data-usage page,
# adding it to `sources`, and moving `sources_read`.
_DATA_HANDLING_NOT_READ: Final[dict[str, object]] = {
    "training_use": TRAINING_USE_NOT_RECORDED,
    "retention": RETENTION_NOT_RECORDED,
    "note": (
        "no data-usage or retention page is among this contract's sources, so neither value "
        "was read; add the vendor's data-usage page to `sources` before declaring one"
    ),
    "account_scope": DATA_HANDLING_ACCOUNT_SCOPE,
}

_GPT_6_ASTRA: Final[dict[str, object]] = {
    "schema_version": MODEL_CONTRACT_SCHEMA_VERSION,
    "model_id": "gpt-6-astra",
    "reasoning_mode": "standard",
    "service_tier": "standard",
    "family": "gpt",
    "generation": "gpt-6",
    "released": "2026-09-03",
    "rollout": "staged; a released id is not account-level readiness evidence",
    "knowledge_cutoff": "2026-04-30",
    "context_window_tokens": 1_050_000,
    "max_input_tokens": 922_000,
    "max_output_tokens": 128_000,
    # The documented ladder. `none` returns HTTP 400 and the migration guide
    # sends `none`/`minimal` callers to `low`, so `low` is the documented
    # floor OMH raises a lower request to — explicitly, in the route record.
    "reasoning_efforts": ("low", "medium", "high", "xhigh", "max"),
    "effort_floor": "low",
    "effort_default": "",
    "unsupported_efforts": {
        "off": "`none` returns HTTP 400; migrate to `low`",
        "minimal": "not in the documented ladder; migrate to `low`",
    },
    "tool_calling": {
        "api": "responses",
        "note": "tool calling requires the Responses API; Chat Completions serves text only",
    },
    "unsupported_parameters": ("temperature", "top_p", "top_logprobs"),
    "dynamic_effort": {
        "mechanism": "configuration_update",
        "scope": "standard single-agent mode only",
        "constraints": (
            "not combinable with automatic compaction or automatic truncation",
            "two adjacent configuration_update items are rejected",
            "the original prompt prefix is preserved for prompt caching",
        ),
        # No executor profile OMH prepares for has been observed to expose
        # this mechanism; the guidance that depends on it is emitted only for
        # a profile named here, so today it is emitted nowhere.
        "compatible_profiles": (),
        "status": "documented_not_observed",
    },
    "runtime_mechanisms": {
        "async_tool_calling": "documented_not_observed",
        "mid_turn_steering": "documented_not_observed",
    },
    "documented_traits": (
        "asks a clarifying question more readily when more input could materially change the result",
        "follows instructions more strictly and may pause on unclear or conflicting skill-file guidance",
        "may delegate less often than a harness expects",
        "may write broader tests than the change requires",
    ),
    # OpenAI list price (developers.openai.com model reference, 2026-09).
    # The approximation table carries input/output only; cache writes and
    # the >272K long-context multiplier are documented here, not flattened.
    "pricing_usd_per_mtok": {
        "input": 10.0,
        "cached_input": 1.0,
        "cache_write": 12.5,
        "output": 50.0,
        "long_context_over_272k_input": "2x input and cache rates, 1.5x output",
    },
    "data_handling": _DATA_HANDLING_NOT_READ,
    "sources": (
        "https://openai.com/index/gpt-6-astra/",
        "https://developers.openai.com/api/docs/models/gpt-6-astra",
        "https://developers.openai.com/api/docs/guides/latest-model",
        "https://developers.openai.com/api/docs/guides/reasoning",
        "https://developers.openai.com/api/docs/guides/async-tool-calling",
        "https://developers.openai.com/api/docs/guides/steering",
    ),
    "sources_read": "2026-09-04",
    "claim_boundary": MODEL_CONTRACT_CLAIM_BOUNDARY,
}

# DeepSeek V4.1 Flash. OMH's alias is the versioned gateway spelling; the
# first-party API names the current Flash generation `deepseek-flash` and
# that pointer is a declared projection below, not a second contract.
_DEEPSEEK_V41_FLASH: Final[dict[str, object]] = {
    "schema_version": MODEL_CONTRACT_SCHEMA_VERSION,
    "model_id": "deepseek-v4.1-flash",
    "reasoning_mode": "thinking",
    "service_tier": "standard",
    "family": "deepseek",
    "generation": "deepseek-v4.1",
    "released": "2026-09-10",
    "rollout": (
        "generally available on the first-party API as `deepseek-flash`; `deepseek-v4-flash` and "
        "`deepseek-v4-flash-vision-exp` are routed to it, and `deepseek-v4-pro` routes to it from "
        "2026-09-14 12:00 Beijing time pending a V4.1 Pro release; a released id is not "
        "account-level readiness evidence"
    ),
    "served_ids": {
        "first_party": "deepseek-flash",
        "openrouter": "deepseek/deepseek-v4.1-flash",
        "huggingface": "deepseek-ai/DeepSeek-V4.1-Flash",
    },
    "knowledge_cutoff": "",
    # The vendor documents the window and the max output literally (1M
    # context, 384K max output) and publishes no separate max-input figure;
    # the output is produced inside the window, so input plus output is not
    # additive here the way it is in the Astra record.
    "context_window_tokens": 1_000_000,
    "max_input_tokens": 1_000_000,
    "max_output_tokens": 384_000,
    "limits_note": "1M context and 384K max output are documented literally; no separate max-input figure is published",
    # The documented ladder is three rungs with thinking on by default at
    # `high`. Nothing returns an error: the thinking-mode guide publishes a
    # mapping table for every other rung (below), so there is no floor to
    # raise to and `unsupported_efforts` stays empty — OMH keeps the
    # requested rung in the route record and the table says what it bought.
    "reasoning_efforts": ("low", "high", "max"),
    "effort_floor": "low",
    "effort_default": "high",
    "unsupported_efforts": {},
    "effort_mapping": {
        "minimal": "low",
        "medium": "high",
        "xhigh": "high",
        "ultra": "max",
        "note": (
            "documented in the thinking-mode guide; no value returns an error. Thinking is "
            "disabled through `thinking.type=disabled` (OpenAI format) or `reasoning.effort=none` "
            "(Anthropic format), not through the effort ladder"
        ),
    },
    "tool_calling": {
        "api": "chat_completions",
        "note": (
            "tool calls are served on Chat Completions, the Responses API, and the "
            "Anthropic-compatible endpoint; on every request that carries the `tools` parameter "
            "the `reasoning_content` of every earlier turn must be sent back — including turns "
            "where the model made no tool call — and is concatenated into the context (HTTP 400 "
            "otherwise); on a request without `tools` it is ignored"
        ),
    },
    # Accepted without error but without effect in thinking mode; `top_p`
    # has a documented floor of 0.95 there.
    "unsupported_parameters": ("temperature", "presence_penalty", "frequency_penalty"),
    "runtime_mechanisms": {
        "reasoning_content_passback": "documented_not_observed",
        "prefix_cache_hit_pricing": "documented_not_observed",
        "native_image_input": "documented_not_observed",
        "json_output": "documented_not_observed",
    },
    "documented_traits": (
        "thinking is on by default at `high`; the ladder is `low`, `high`, `max`, and the guide "
        "maps every other rung (`minimal` to `low`, `medium` and `xhigh` to `high`, `ultra` to "
        "`max`) without an error",
        "on every request carrying `tools` the reasoning_content of every earlier turn is sent "
        "back, so earlier reasoning is already in the context of every later tool step",
        "post-trained on large-scale synthesized agent tasks and evaluated by the vendor at its "
        "maximum reasoning setting with a 1M-token context and max_tokens of at least 256K",
        "cache-hit input is priced at a fiftieth of cache-miss input, so a changed prompt prefix "
        "is billing-visible on every later turn",
        "temperature, presence_penalty, and frequency_penalty have no effect in thinking mode",
    ),
    # DeepSeek list price (api-docs.deepseek.com/quick_start/pricing, 2026-09):
    # peak-hour rates; every rate halves off-peak. The approximation table
    # carries peak input/output; the cache-hit rate is its 0.02 ratio row.
    "pricing_usd_per_mtok": {
        "input": 0.30,
        "cached_input": 0.006,
        "output": 1.20,
        "off_peak": (
            "half of every peak rate (cache hit 0.003, cache miss 0.15, output 0.6) outside "
            "01:00-04:00 and 06:00-10:00 UTC, Monday through Friday"
        ),
    },
    "data_handling": _DATA_HANDLING_NOT_READ,
    "sources": (
        "https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash",
        "https://api-docs.deepseek.com/updates/",
        "https://api-docs.deepseek.com/quick_start/pricing",
        "https://api-docs.deepseek.com/guides/thinking_mode",
    ),
    "sources_read": "2026-09-11",
    "claim_boundary": MODEL_CONTRACT_CLAIM_BOUNDARY,
}

MODEL_CONTRACTS: Final[dict[str, Mapping[str, object]]] = {
    "gpt-6-astra": _GPT_6_ASTRA,
    "deepseek-v4.1-flash": _DEEPSEEK_V41_FLASH,
}

# Catalog aliases whose relationship to an exact contract is explicitly
# declared. The spelling and composition order are part of the contract: a
# future suffix does not inherit until it gets its own row and evidence.
DECLARED_MODEL_CONTRACT_PROJECTIONS: Final[dict[str, Mapping[str, str]]] = {
    "gpt-6-astra-fast": {
        "contract_model_id": "gpt-6-astra",
        "reasoning_mode": "standard",
        "service_tier": "fast",
    },
    "gpt-6-astra-flex": {
        "contract_model_id": "gpt-6-astra",
        "reasoning_mode": "standard",
        "service_tier": "flex",
    },
    "gpt-6-astra-pro": {
        "contract_model_id": "gpt-6-astra",
        "reasoning_mode": "pro",
        "service_tier": "standard",
    },
    "gpt-6-astra-pro-fast": {
        "contract_model_id": "gpt-6-astra",
        "reasoning_mode": "pro",
        "service_tier": "fast",
    },
    "gpt-6-astra-pro-flex": {
        "contract_model_id": "gpt-6-astra",
        "reasoning_mode": "pro",
        "service_tier": "flex",
    },
    # The first-party API's moving pointer to the current Flash generation
    # (api-docs.deepseek.com, read 2026-09-11: DeepSeek-V4.1-Flash). The next
    # Flash release moves it, which is exactly why it is a declared row with
    # a read date and not a second exact contract.
    "deepseek-flash": {
        "contract_model_id": "deepseek-v4.1-flash",
        "reasoning_mode": "thinking",
        "service_tier": "standard",
    },
}

# Declared aliases that are the SAME model at the contract's own reasoning
# mode and service tier — a vendor's second spelling, nothing more. A shipped
# chain may name the pointer instead of the exact id (DeepSeek serves
# `deepseek-flash` and rejects `deepseek-v4.1-flash`), so a child observed
# under the exact id must still label the category that names its pointer.
# Mode and tier variants (`gpt-6-astra-pro`, `-fast`, `-flex`) are NOT
# pointers: the forward projection is honest for them, the reverse is not.
# Mirrored in the plugin bundle; the parity test pins both tables and that
# every entry here is a declared row at the contract's own mode and tier.
EXACT_CONTRACT_POINTER_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "deepseek-v4.1-flash": ("deepseek-flash",),
}


def _unqualified_model_id(model_id: str) -> str:
    normalized = str(model_id or "").strip().casefold()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[1]
    return normalized


# A vendor's dated snapshot id (OpenAI's `<model>-YYYY-MM-DD`, e.g.
# `gpt-5.6-terra-2026-07-09`) is the base model pinned to a release date: the
# same contract at the same reasoning mode and service tier. Some providers
# serve only the dated form, so a user who confirms it must still be
# recognized as running the base. This is the one suffix rule the catalog
# applies without a declared row, and it is bounded twice: only this exact
# trailing shape matches, and it projects only onto a base the caller already
# knows (a contract, a declared row, a chain alias). An unknown base with a
# date stays unknown. Mirrored in the plugin bundle; the parity test pins the
# pattern.
_DATED_SNAPSHOT_SUFFIX: Final = re.compile(
    r"^(?P<base>.+)-(?P<date>\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))$"
)


def dated_snapshot_base(model_id: str) -> str:
    """Return the base alias of a ``<base>-YYYY-MM-DD`` snapshot id, else ''.

    Shape only: the caller decides whether the base is one it knows.
    """
    match = _DATED_SNAPSHOT_SUFFIX.match(_unqualified_model_id(model_id))
    return match.group("base") if match else ""


def _dated_snapshot_projection(canonical: str) -> tuple[str, str, str] | None:
    """Project a dated snapshot onto the contract its base already resolves to."""
    base = dated_snapshot_base(canonical)
    # One date only: a snapshot of a snapshot is not a shape the vendor ships.
    if not base or dated_snapshot_base(base):
        return None
    projection = model_contract_projection(base)
    if projection is None:
        return None
    return (
        projection["contract_model_id"],
        projection["reasoning_mode"],
        projection["service_tier"],
    )


def model_contract_projection(model_id: str) -> dict[str, str] | None:
    """Resolve an exact, explicitly inherited, or dated-snapshot contract without guessing."""
    requested = str(model_id or "").strip()
    canonical = _unqualified_model_id(requested)
    if not canonical:
        return None
    contract = MODEL_CONTRACTS.get(canonical)
    declared = DECLARED_MODEL_CONTRACT_PROJECTIONS.get(canonical)
    if contract is not None:
        contract_id = canonical
        reasoning_mode = str(contract.get("reasoning_mode", "standard"))
        service_tier = str(contract.get("service_tier", "standard"))
        provenance = "exact"
    elif declared is not None:
        contract_id = str(declared["contract_model_id"])
        if contract_id not in MODEL_CONTRACTS:
            return None
        reasoning_mode = str(declared["reasoning_mode"])
        service_tier = str(declared["service_tier"])
        provenance = "declared_inheritance"
    else:
        snapshot = _dated_snapshot_projection(canonical)
        if snapshot is None:
            return None
        contract_id, reasoning_mode, service_tier = snapshot
        provenance = "dated_snapshot"
    return {
        "schema_version": MODEL_CONTRACT_PROJECTION_SCHEMA_VERSION,
        "requested_model": requested,
        "canonical_model_id": canonical,
        "contract_model_id": contract_id,
        "reasoning_mode": reasoning_mode,
        "service_tier": service_tier,
        "provenance": provenance,
        "claim_boundary": MODEL_CONTRACT_PROJECTION_CLAIM_BOUNDARY,
    }


def contract_model_id(model_id: str) -> str:
    """Return the exact/declared contract key, or the normalized unknown id."""
    projection = model_contract_projection(model_id)
    return str(projection["contract_model_id"]) if projection is not None else _unqualified_model_id(model_id)


def model_contract(model_id: str) -> Mapping[str, object] | None:
    """Return the exact or explicitly inherited documented contract, or None."""
    projection = model_contract_projection(model_id)
    return MODEL_CONTRACTS.get(str(projection["contract_model_id"])) if projection is not None else None


def contract_effort_floor(model_id: str, effort: str) -> tuple[str, str] | None:
    """Return (floor, reason) when ``effort`` sits below the model's documented ladder.

    ``None`` means the contract has nothing to say: no contract, no floor, an
    effort the ladder supports, or a value that is not a documented-unsupported
    rung. The caller records the raise as an explicit effort change; nothing is
    changed silently.
    """
    contract = model_contract(model_id)
    if contract is None:
        return None
    floor = str(contract.get("effort_floor", "") or "")
    supported = tuple(str(value) for value in contract.get("reasoning_efforts", ()))
    normalized = str(effort or "").strip().casefold()
    if not floor or not normalized or normalized in supported:
        return None
    unsupported = contract.get("unsupported_efforts", {})
    if not isinstance(unsupported, Mapping) or normalized not in unsupported:
        return None
    detail = str(unsupported[normalized])
    return floor, (
        f"`{normalized}` is below `{contract['model_id']}`'s documented effort ladder "
        f"({detail}); raised to the documented floor `{floor}`"
    )


def dynamic_effort_guidance(model_id: str, executor_profile: str) -> dict[str, object] | None:
    """Return the effort-policy record for a model with a documented dynamic-effort mechanism.

    The mid-conversation mechanism is described only when ``executor_profile``
    is one the contract names as compatible; every other profile gets the
    per-turn policy, so no prepared text implies a runtime mutation that the
    runtime cannot perform.
    """
    contract = model_contract(model_id)
    if contract is None:
        return None
    dynamic = contract.get("dynamic_effort")
    if not isinstance(dynamic, Mapping):
        return None
    profile = str(executor_profile or "").strip().casefold()
    compatible = tuple(str(value) for value in dynamic.get("compatible_profiles", ()))
    floor = str(contract.get("effort_floor", "") or "")
    policy: dict[str, object] = {
        "model_id": str(contract["model_id"]),
        "effort_floor": floor,
        "escalate_while": (
            "an active criterion still holds unresolved hard reasoning, a new failure, or "
            "contradictory evidence"
        ),
        "reduce_when": "the decisive evidence is in hand and the remaining work is routine follow-up",
        "stop_when": (
            "every predeclared criterion is done, the single verification pass succeeded, no "
            "contradictory result remains, and the TODO is reconciled; a passed criterion is not "
            "reopened for reassurance"
        ),
        "status": str(dynamic.get("status", "documented_not_observed")),
    }
    if profile and profile in compatible:
        policy["mode"] = "mid_conversation"
        policy["mechanism"] = str(dynamic.get("mechanism", ""))
        policy["scope"] = str(dynamic.get("scope", ""))
        policy["constraints"] = list(dynamic.get("constraints", ()))
    else:
        policy["mode"] = "per_turn"
        policy["mechanism"] = "prepare the next unit or turn at the new effort"
        policy["note"] = (
            f"`{profile or 'this'}` executor profile is not documented as exposing "
            f"{dynamic.get('mechanism', 'a mid-conversation effort update')}; effort is set "
            "explicitly per prepared turn and no mid-conversation change is claimed"
        )
    return policy


__all__ = [
    "DATA_HANDLING_ACCOUNT_SCOPE",
    "DATA_HANDLING_RETENTION_VALUES",
    "DATA_HANDLING_TRAINING_USE_VALUES",
    "DECLARED_MODEL_CONTRACT_PROJECTIONS",
    "EFFORT_FLOOR_KIND",
    "EXACT_CONTRACT_POINTER_ALIASES",
    "MODEL_CONTRACTS",
    "RETENTION_BOUNDED",
    "RETENTION_INDEFINITE",
    "RETENTION_NONE",
    "RETENTION_NOT_RECORDED",
    "TRAINING_USE_EXCLUDED",
    "TRAINING_USE_INCLUDED",
    "TRAINING_USE_NOT_RECORDED",
    "MODEL_CONTRACT_CLAIM_BOUNDARY",
    "MODEL_CONTRACT_PROJECTION_CLAIM_BOUNDARY",
    "MODEL_CONTRACT_PROJECTION_SCHEMA_VERSION",
    "MODEL_CONTRACT_SCHEMA_VERSION",
    "contract_effort_floor",
    "contract_model_id",
    "dynamic_effort_guidance",
    "model_contract",
    "model_contract_projection",
]
