"""One bounded completion recap for one stored runtime run.

The recap answers a question the learning trace could not: which delivery,
verification, review, pull-request, CI, merge-readiness and merge facts were
*observed* for this run, as opposed to which ones an operator reported. Those
two authorities are kept in separate blocks and never write to each other.
`operator_outcome` is a human judgement and may not set or upgrade a single
evidence cell; `observed_completion` is derived from validated
`runtime_observation/v1` records and nothing else.

Every function here is pure with respect to the world outside the local store:
no network, no model, no subprocess, no executor. Building or reading a recap
changes no skill, no memory, no candidate decision and no pull request.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from ..local_store import atomic_write_json, read_json_object
from ..paths import OmhPaths
from ..runtime.artifacts import show_run
from ..runtime.records import (
    RUNTIME_OBSERVABLE_EVENTS,
    RUNTIME_OBSERVATION_EVENTS,
    RUNTIME_OBSERVATION_SCHEMA_VERSION,
    validate_runtime_observation_record,
)
from ..system.append_only_store import redacted_ref
from ..system.metadata_safety import is_sensitive_metadata_text, require_opaque_metadata_ref
from .workflow_learning_errors import WorkflowLearningError


RUNTIME_LEARNING_RECAP_SCHEMA_VERSION = "runtime_learning_recap/v1"
RUNTIME_LEARNING_RECAP_RECORD_TYPE = "runtime_learning_recap"
RUNTIME_LEARNING_RECAP_REF_PREFIX = "omh-runtime-learning-recap"

# The seven completion facts the issue keeps apart. They are separate cells and
# never collapse into one another: a pull request is not passing CI, passing CI
# is not a completed review, and a completed review is not a merge.
RUNTIME_LEARNING_RECAP_CELLS = (
    "delivery",
    "verification",
    "review",
    "pull_request",
    "ci",
    "merge_readiness",
    "merge",
)
# What a cell may say. Three values, because a cell reports evidence and
# absence of evidence is `unavailable` -- never an empty success.
RECAP_CELL_STATES = ("observed", "failed", "unavailable")
# What the run as a whole may say.
OBSERVED_COMPLETION_STATES = ("partial", "completed", "failed", "unknown")
OPERATOR_OUTCOMES = ("unknown", "useful", "not_useful", "blocked", "failed")

# Six cells are a milestone on the runtime observation ladder. `pull_request`
# is deliberately absent: no ladder event records "a pull request exists", so
# that cell is carried by a typed `pr:` evidence reference on whichever ladder
# observation names one. An executor that never names one leaves the cell
# unavailable, which is the executor-neutral answer, not a failure.
_CELL_EVENT_TYPES: dict[str, str] = {
    "delivery": "worker_result",
    "verification": "verification",
    "review": "review",
    "ci": "ci",
    "merge_readiness": "merge_readiness",
    "merge": "merge",
}
# A runtime observation status maps onto a cell state. `blocked`, `cancelled`
# and `not_observed` all become `unavailable` rather than `failed`: a block is
# recoverable by definition and a cancellation is not a fault of the stage, so
# neither may be reported as a stage that failed. The raw status is kept on the
# cell as `observation_status`, so nothing is lost by the narrowing.
_CELL_STATE_BY_OBSERVATION_STATUS: dict[str, str] = {
    "observed": "observed",
    "failed": "failed",
    "blocked": "unavailable",
    "cancelled": "unavailable",
    "not_observed": "unavailable",
}

# The closed typed-evidence vocabulary. An identity field is filled only when
# the observation that owns its cell carries the matching prefix, so a recap can
# never name a commit the evidence did not name.
_COMMIT_PREFIX = "commit"
_CHANGED_PATH_PREFIX = "changed_path"
_PULL_REQUEST_PREFIX = "pr"
_MERGE_COMMIT_PREFIX = "merge_commit"

_MAX_EVIDENCE_REFS_PER_CELL = 8
_MAX_CHANGED_PATHS = 20
_MAX_OPERATOR_FEEDBACK = 200
_MAX_SUMMARY = 600
_MAX_TIMESTAMP = 40
_MAX_DIAGNOSTICS = 20
# What a recap string may never be. Deliberately narrower than the
# metadata-line screen the append-only stores use: that one rejects any "/",
# which would reject this module's own schema names and the repository-relative
# changed paths the recap exists to report. These four are the shapes the issue
# names -- a link, an absolute filesystem path, a body (a prompt, a transcript,
# a log slice, a command with its output), and a credential.
_UNSAFE_URL = re.compile(r"(?i)[a-z][a-z0-9+.-]*://")
_UNSAFE_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'=(,])(?:/[A-Za-z0-9._~-]|~/|[A-Za-z]:[\\/]|\\\\)")
_UNSAFE_BODY = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|```|[\r\n\t]")
_MAX_RECAP_TEXT = 600
_RECAP_ID_PATTERN = re.compile(r"^wlr-[0-9a-f]{20}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_PATTERN = re.compile(r"[^A-Za-z0-9_.:-]+")
_REDACTED = "[redacted]"

# Keys a recap may never carry, whatever produced it.
FORBIDDEN_RECAP_KEYS = {
    "message",
    "raw_message",
    "prompt",
    "raw_prompt",
    "prompt_body",
    "transcript",
    "raw_transcript",
    "command",
    "commands",
    "argv",
    "tool_input",
    "tool_result",
    "tool_output",
    "stdout",
    "stderr",
    "log",
    "logs",
    "credential",
    "credentials",
    "token",
    "secret",
}

_CLAIM_BOUNDARY = (
    "A runtime learning recap is bounded review material derived from stored "
    "runtime_observation/v1 records. It does not prove any stage it reports as "
    "unavailable, does not execute anything, and does not change a skill, "
    "memory, candidate decision, or pull request."
)
_OPERATOR_CLAIM_BOUNDARY = (
    "Operator outcome and feedback are supplied human assessment. They never "
    "set or upgrade an evidence cell or the observed completion state."
)
_OBSERVED_CLAIM_BOUNDARY = (
    "Observed completion is derived from validated runtime observation records "
    "only. A prepared handoff, a wrapper summary, or a process exit is not "
    "terminal success."
)


# ---------------------------------------------------------------------------
# Build


def build_runtime_learning_recap(
    paths: OmhPaths,
    run_id: str,
    *,
    operator_outcome: str = "unknown",
    operator_feedback_summary: str = "",
) -> dict[str, Any]:
    """Project one stored runtime run into a recap for its current revision."""
    # Completion is a question about the whole history, so the projection reads
    # every observation rather than the bounded status tail.
    shown = show_run(paths, run_id, history_limit=None)
    return build_runtime_learning_recap_from_shown(
        shown,
        run_id,
        operator_outcome=operator_outcome,
        operator_feedback_summary=operator_feedback_summary,
    )


def build_runtime_learning_recap_from_shown(
    shown: Mapping[str, Any],
    run_id: str,
    *,
    operator_outcome: str = "unknown",
    operator_feedback_summary: str = "",
) -> dict[str, Any]:
    """The pure half of the build, for callers that already read the run.

    Deterministic by construction: the recap carries no wall clock, so
    re-recording one run at one runtime history revision produces byte-identical
    content and the same `recap_id`.
    """
    if operator_outcome not in OPERATOR_OUTCOMES:
        raise WorkflowLearningError(f"unsupported operator outcome: {operator_outcome}")
    identity = _run_identity(run_id)
    eligible, rejected = _eligible_observations(shown, identity)
    digest = _runtime_history_digest(identity, eligible)
    latest_by_event = _latest_by_event(eligible)
    cells = _evidence_cells(latest_by_event)
    completion = _observed_completion(cells)
    recap = {
        "schema_version": RUNTIME_LEARNING_RECAP_SCHEMA_VERSION,
        "record_type": RUNTIME_LEARNING_RECAP_RECORD_TYPE,
        "recap_id": _recap_id(identity, digest),
        "run_id": identity,
        "revision": {
            "runtime_history_digest": digest,
            "observation_count": len(eligible),
            "rejected_observation_count": rejected,
            "latest_observation_at": _latest_observation_at(eligible),
            "basis": (
                f"validated {RUNTIME_OBSERVATION_SCHEMA_VERSION} records whose target_type is "
                "run and whose target_id is this run id"
            ),
        },
        "operator_assessment": {
            "authority": "operator_supplied",
            "operator_outcome": operator_outcome,
            "operator_feedback_summary": _bounded_operator_text(operator_feedback_summary),
            "operator_feedback_redacted": _is_redacted_operator_text(operator_feedback_summary),
            "claim_boundary": _OPERATOR_CLAIM_BOUNDARY,
        },
        "observed_completion": completion,
        "evidence_cells": cells,
        "identity": _identity_fields(cells, latest_by_event),
        "privacy": {
            "mode": "metadata_only",
            "raw_prompt_stored": False,
            "raw_platform_event_stored": False,
            "stored_fields": [
                "run id",
                "runtime history digest",
                "closed evidence states",
                "observation event types",
                "bounded opaque evidence references",
            ],
        },
        "not_evidence_yet": _not_evidence_yet(cells),
        "overclaim_guard": [
            "An operator outcome is not completion evidence.",
            "A prepared handoff, a wrapper summary, or a process exit is not terminal success.",
            "A cell reported unavailable is unproven, not absent work.",
            "Recaps are review material; they patch no skill and write no memory.",
        ],
        "claim_boundary": _CLAIM_BOUNDARY,
    }
    recap["summary"] = _recap_summary(recap)
    validate_runtime_learning_recap(recap)
    return recap


def _run_identity(run_id: str) -> str:
    text = str(run_id or "").strip()
    if not text:
        raise WorkflowLearningError("runtime learning recap run_id is required")
    try:
        return require_opaque_metadata_ref(text, field="runtime learning recap run_id")
    except ValueError as exc:
        raise WorkflowLearningError(str(exc)) from exc


def _eligible_observations(shown: Mapping[str, Any], run_id: str) -> tuple[list[dict[str, Any]], int]:
    """Every observation that may speak for this run, plus how many may not.

    A record is eligible only when it validates as `runtime_observation/v1`,
    targets a run rather than a wrapper session, and names this run id. A record
    that names a different run is a foreign observation and is counted, never
    used: the whole point of binding the recap to a run is that another run's
    evidence cannot leak into it.
    """
    eligible: list[dict[str, Any]] = []
    rejected = 0
    for record in shown.get("runtime_observations") or []:
        if not isinstance(record, dict):
            rejected += 1
            continue
        if validate_runtime_observation_record(record):
            rejected += 1
            continue
        if str(record.get("target_type", "")) != "run" or str(record.get("target_id", "")) != run_id:
            rejected += 1
            continue
        eligible.append(record)
    return eligible, rejected


def _sort_key(record: Mapping[str, Any]) -> tuple[str, str]:
    """Which of two records for one milestone is the later one.

    `status` is deliberately not a member. `updated_at` has second resolution --
    `local_store.utc_now` drops microseconds -- so two records for one event
    type written in one second tie on both fields here, and any third field
    would decide the winner by its own ordering rather than by arrival. With
    `status` in the tuple the comparison ran
    `blocked < cancelled < failed < not_observed < observed`, so `observed` won
    every same-second tie in both directions: a corrective `failed` appended
    after an `observed` was discarded, and a stale `observed` appended after a
    `failed` also won. The key was not picking a different record from the
    runtime projection, it was not picking at all.
    """
    return (
        str(record.get("updated_at", "")),
        str(record.get("event_type", "")),
    )


def _latest_by_event(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The record that speaks for each event type: the last one appended.

    Stored order is append order, so a tie on the key falls through `>=` to the
    later record. That is this repository's existing rule, stated at
    `append_only_store.latest_record_in`: a later append always wins the tie.

    It agrees with the runtime status projection, whose key is

        (updated_at, target_type, target_id, event_type)

    in `runtime.artifacts._runtime_observation_sort_key`, and which breaks a
    full tie the same way. The agreement is conditional, not structural: the two
    fields that key carries and this one does not are constant across the
    eligible set only because `_eligible_observations` keeps one run id and
    `target_type == "run"`. Widen that filter and the two keys stop being
    equivalent, so widen this key in the same commit.
    """
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        event_type = str(record.get("event_type", ""))
        if event_type not in RUNTIME_OBSERVATION_EVENTS:
            continue
        current = latest.get(event_type)
        if current is None or _sort_key(record) >= _sort_key(current):
            latest[event_type] = record
    return latest


def _runtime_history_digest(run_id: str, records: list[dict[str, Any]]) -> str:
    """Which revision of the history this recap was built from.

    Identity fields only, in stored order. A later observation changes the
    digest, which mints a new recap id, which is how a new revision is created
    without rewriting the evidence basis of an earlier reviewed one.
    """
    basis = [
        {
            "updated_at": str(record.get("updated_at", "")),
            "event_type": str(record.get("event_type", "")),
            "status": str(record.get("status", "")),
            "worker_ref": str(record.get("worker_ref", "")),
            "worktree_ref": str(record.get("worktree_ref", "")),
            "evidence_refs": [str(ref) for ref in record.get("evidence_refs") or []],
        }
        for record in records
    ]
    payload = json.dumps({"run_id": run_id, "observations": basis}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _recap_id(run_id: str, digest: str) -> str:
    seed = json.dumps({"run_id": run_id, "digest": digest}, sort_keys=True)
    return f"wlr-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _latest_observation_at(records: list[dict[str, Any]]) -> str:
    stamps = [str(record.get("updated_at", "")) for record in records if record.get("updated_at")]
    return max(stamps)[:_MAX_TIMESTAMP] if stamps else ""


def _evidence_cells(latest_by_event: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cells: dict[str, dict[str, Any]] = {}
    for cell in RUNTIME_LEARNING_RECAP_CELLS:
        if cell == "pull_request":
            continue
        cells[cell] = _cell_from_record(latest_by_event.get(_CELL_EVENT_TYPES[cell]))
    cells["pull_request"] = _pull_request_cell(latest_by_event)
    return {cell: cells[cell] for cell in RUNTIME_LEARNING_RECAP_CELLS}


def _cell_from_record(record: Mapping[str, Any] | None) -> dict[str, Any]:
    if not record:
        return _unavailable_cell("no eligible runtime observation")
    status = str(record.get("status", ""))
    state = _CELL_STATE_BY_OBSERVATION_STATUS.get(status, "unavailable")
    refs, truncated = _bounded_evidence_refs(record.get("evidence_refs"))
    return {
        "state": state,
        "source_observation_schema": RUNTIME_OBSERVATION_SCHEMA_VERSION,
        "source_observation_type": str(record.get("event_type", "")),
        "observation_status": status,
        "observed_at": str(record.get("updated_at", ""))[:_MAX_TIMESTAMP],
        "evidence_refs": refs,
        "evidence_ref_count": len([ref for ref in record.get("evidence_refs") or [] if str(ref).strip()]),
        "evidence_refs_truncated": truncated,
        "reason": "" if state != "unavailable" else f"observation status {status} is not terminal evidence",
    }


def _unavailable_cell(reason: str) -> dict[str, Any]:
    return {
        "state": "unavailable",
        "source_observation_schema": "",
        "source_observation_type": "",
        "observation_status": "",
        "observed_at": "",
        "evidence_refs": [],
        "evidence_ref_count": 0,
        "evidence_refs_truncated": False,
        "reason": reason,
    }


def _pull_request_cell(latest_by_event: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The most advanced ladder observation that names a pull request.

    Most advanced rather than newest: a merge observation naming the pull
    request says more about it than a worker result naming the same one, and
    ladder order is the only ordering both agree on.
    """
    carrier: Mapping[str, Any] | None = None
    for event_type in RUNTIME_OBSERVATION_EVENTS:
        record = latest_by_event.get(event_type)
        if record and _typed_evidence_values(record, _PULL_REQUEST_PREFIX):
            carrier = record
    if carrier is None:
        return _unavailable_cell("no eligible runtime observation carries a pr: evidence reference")
    return _cell_from_record(carrier)


def _typed_evidence_values(record: Mapping[str, Any], prefix: str) -> list[str]:
    values: list[str] = []
    for ref in record.get("evidence_refs") or []:
        text = str(ref or "").strip()
        head, separator, tail = text.partition(":")
        if separator and head == prefix and tail.strip():
            values.append(tail.strip())
    return values


def _observed_completion(cells: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The one closed state for the run, from the cells and nothing else.

    `completed` requires an observed merge, because merge is the only cell whose
    evidence is terminal for the whole run. Every other observed cell moves the
    run to `partial`, which is the honest answer for work that reached a
    milestone and stopped there.
    """
    observed = [cell for cell in RUNTIME_LEARNING_RECAP_CELLS if cells[cell]["state"] == "observed"]
    failed = [cell for cell in RUNTIME_LEARNING_RECAP_CELLS if cells[cell]["state"] == "failed"]
    unavailable = [cell for cell in RUNTIME_LEARNING_RECAP_CELLS if cells[cell]["state"] == "unavailable"]
    if failed:
        state = "failed"
        reason = f"observed failure in: {', '.join(failed)}"
    elif "merge" in observed:
        state = "completed"
        reason = "merge observed and no cell reports a failure"
    elif observed:
        state = "partial"
        reason = f"observed evidence in: {', '.join(observed)}; merge not observed"
    else:
        state = "unknown"
        reason = "no eligible runtime observation reports a terminal evidence cell"
    return {
        "authority": "observed_evidence",
        "state": state,
        "reason": reason,
        "observed_cells": observed,
        "failed_cells": failed,
        "unavailable_cells": unavailable,
        "claim_boundary": _OBSERVED_CLAIM_BOUNDARY,
    }


def _identity_fields(
    cells: Mapping[str, Mapping[str, Any]],
    latest_by_event: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Commit, changed paths, pull request and merge identity.

    Each one is filled only from the observation that owns its own cell, and
    only through the closed typed-evidence prefix for that field. A field with
    no such reference stays unavailable rather than becoming an empty success.
    """
    delivery = latest_by_event.get(_CELL_EVENT_TYPES["delivery"])
    merge = latest_by_event.get(_CELL_EVENT_TYPES["merge"])
    pull_request_source = _pull_request_source(latest_by_event)
    return {
        "commit": _identity_value(cells["delivery"], delivery, _COMMIT_PREFIX),
        "changed_paths": _changed_paths(cells["delivery"], delivery),
        "pull_request": _identity_value(cells["pull_request"], pull_request_source, _PULL_REQUEST_PREFIX),
        "merge_commit": _identity_value(cells["merge"], merge, _MERGE_COMMIT_PREFIX),
    }


def _pull_request_source(latest_by_event: dict[str, dict[str, Any]]) -> Mapping[str, Any] | None:
    carrier: Mapping[str, Any] | None = None
    for event_type in RUNTIME_OBSERVATION_EVENTS:
        record = latest_by_event.get(event_type)
        if record and _typed_evidence_values(record, _PULL_REQUEST_PREFIX):
            carrier = record
    return carrier


def _identity_value(
    cell: Mapping[str, Any],
    record: Mapping[str, Any] | None,
    prefix: str,
) -> dict[str, Any]:
    if record is None or cell.get("state") == "unavailable":
        return {"state": "unavailable", "source_observation_type": "", "value": "", "reason": f"no eligible {prefix}: evidence reference"}
    values = _typed_evidence_values(record, prefix)
    if not values:
        return {
            "state": "unavailable",
            "source_observation_type": str(record.get("event_type", "")),
            "value": "",
            "reason": f"supporting observation carries no {prefix}: evidence reference",
        }
    return {
        "state": "observed",
        "source_observation_type": str(record.get("event_type", "")),
        "value": redacted_ref(values[0], field=f"{prefix} identity"),
        "reason": "",
    }


def _changed_paths(cell: Mapping[str, Any], record: Mapping[str, Any] | None) -> dict[str, Any]:
    """A bounded count of repository-relative paths, never a filesystem path.

    A value that is not a bounded relative reference -- an absolute path, a URL,
    anything secret-shaped -- is dropped and counted rather than digested: a
    digest in a path list reads like a path and is not one.
    """
    if record is None or cell.get("state") == "unavailable":
        return {"state": "unavailable", "source_observation_type": "", "count": 0, "paths": [], "dropped_count": 0, "truncated": False}
    values = _typed_evidence_values(record, _CHANGED_PATH_PREFIX)
    if not values:
        return {
            "state": "unavailable",
            "source_observation_type": str(record.get("event_type", "")),
            "count": 0,
            "paths": [],
            "dropped_count": 0,
            "truncated": False,
        }
    safe: list[str] = []
    dropped = 0
    for value in values:
        if _is_safe_relative_path(value):
            safe.append(value)
        else:
            dropped += 1
    truncated = len(safe) > _MAX_CHANGED_PATHS
    return {
        "state": "observed" if safe else "unavailable",
        "source_observation_type": str(record.get("event_type", "")),
        "count": len(safe),
        "paths": safe[:_MAX_CHANGED_PATHS],
        "dropped_count": dropped,
        "truncated": truncated,
    }


def is_unsafe_recap_text(value: str) -> bool:
    """Whether one recap string carries something a recap must not.

    A link, an absolute filesystem path, a body, a credential, or more text than
    a bounded field holds. Public because the store, the builder and the tests
    must all screen by the same rule.
    """
    text = str(value or "")
    if not text:
        return False
    return bool(
        len(text) > _MAX_RECAP_TEXT
        or _UNSAFE_BODY.search(text)
        or _UNSAFE_URL.search(text)
        or _UNSAFE_ABSOLUTE_PATH.search(text)
        or is_sensitive_metadata_text(text)
    )


def _is_safe_relative_path(value: str) -> bool:
    if ".." in value or value.startswith("/") or "\\" in value:
        return False
    if is_unsafe_recap_text(value):
        return False
    try:
        require_opaque_metadata_ref(value, field="changed path")
    except ValueError:
        return False
    return True


def _bounded_evidence_refs(values: Any) -> tuple[list[str], bool]:
    """Fold every reference into a bounded, non-navigable handle.

    A safe opaque identifier passes through as itself; a URL, a credential
    shape, an absolute path, or anything longer than a bounded identifier
    becomes a stable `ref-` digest. That is what makes the recap citable
    without republishing whatever the executor happened to write.
    """
    refs: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        folded = redacted_ref(text, field="evidence_ref")
        if folded and folded not in refs:
            refs.append(folded)
    return refs[:_MAX_EVIDENCE_REFS_PER_CELL], len(refs) > _MAX_EVIDENCE_REFS_PER_CELL


def _bounded_operator_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if is_unsafe_recap_text(text):
        return _REDACTED
    return text[:_MAX_OPERATOR_FEEDBACK]


def _is_redacted_operator_text(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text) and is_unsafe_recap_text(text)


def _not_evidence_yet(cells: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return [cell for cell in RUNTIME_LEARNING_RECAP_CELLS if cells[cell]["state"] == "unavailable"]


def _recap_summary(recap: Mapping[str, Any]) -> str:
    """One line assembled from closed states and the run id, nothing else.

    No observation summary is copied in. An executor writes that field freely,
    so quoting it would put arbitrary prose in a record whose whole contract is
    that it carries none.
    """
    completion = recap["observed_completion"]
    operator = recap["operator_assessment"]
    cells = recap["evidence_cells"]
    states = "; ".join(f"{cell} {cells[cell]['state']}" for cell in RUNTIME_LEARNING_RECAP_CELLS)
    return (
        f"run {recap['run_id']}: observed completion {completion['state']}. "
        f"{states}. "
        f"operator outcome {operator['operator_outcome']} (supplied assessment, not evidence)."
    )[:_MAX_SUMMARY]


# ---------------------------------------------------------------------------
# Validate


def validate_runtime_learning_recap(recap: Any) -> None:
    """Raise on the first fault, naming it. See `runtime_learning_recap_errors`."""
    errors = runtime_learning_recap_errors(recap)
    if errors:
        raise WorkflowLearningError(errors[0])


def runtime_learning_recap_errors(recap: Any) -> list[str]:
    """Every reason a payload is not a valid recap, bounded.

    Bounded because a report of faults is itself a surface: a hand-edited store
    must not be able to turn a validation failure into an unbounded dump.
    """
    if not isinstance(recap, dict):
        return ["runtime learning recap must be an object"]
    errors: list[str] = []
    if recap.get("schema_version") != RUNTIME_LEARNING_RECAP_SCHEMA_VERSION:
        errors.append(
            f"unsupported runtime learning recap schema version: {str(recap.get('schema_version', ''))[:80]}"
        )
        return errors[:_MAX_DIAGNOSTICS]
    if recap.get("record_type") != RUNTIME_LEARNING_RECAP_RECORD_TYPE:
        errors.append("runtime learning recap record_type is invalid")
    errors.extend(_identity_errors(recap))
    errors.extend(_cell_errors(recap))
    errors.extend(_completion_errors(recap))
    errors.extend(_identity_field_errors(recap))
    errors.extend(_operator_errors(recap))
    errors.extend(_privacy_errors(recap))
    errors.extend(_content_safety_errors(recap))
    return errors[:_MAX_DIAGNOSTICS]


def _identity_errors(recap: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    run_id = recap.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return ["runtime learning recap run_id is required"]
    try:
        require_opaque_metadata_ref(run_id, field="runtime learning recap run_id")
    except ValueError as exc:
        errors.append(str(exc))
        return errors
    revision = recap.get("revision")
    if not isinstance(revision, dict):
        return errors + ["runtime learning recap revision must be an object"]
    digest = revision.get("runtime_history_digest")
    if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
        return errors + ["runtime learning recap runtime_history_digest must be a sha256 hex digest"]
    for key in ("observation_count", "rejected_observation_count"):
        value = revision.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"runtime learning recap revision.{key} must be a non-negative integer")
    latest = revision.get("latest_observation_at")
    if not isinstance(latest, str) or len(latest) > _MAX_TIMESTAMP:
        errors.append("runtime learning recap revision.latest_observation_at must be a bounded string")
    recap_id = recap.get("recap_id")
    if not isinstance(recap_id, str) or not _RECAP_ID_PATTERN.fullmatch(recap_id):
        errors.append("runtime learning recap recap_id is malformed")
    elif recap_id != _recap_id(run_id, digest):
        # The binding is the whole contract: a recap id that does not follow
        # from this run id and this revision is evidence from somewhere else.
        errors.append("runtime learning recap recap_id does not bind this run id and runtime history revision")
    return errors


def _cell_errors(recap: Mapping[str, Any]) -> list[str]:
    cells = recap.get("evidence_cells")
    if not isinstance(cells, dict):
        return ["runtime learning recap evidence_cells must be an object"]
    extra = sorted(set(cells) - set(RUNTIME_LEARNING_RECAP_CELLS))
    if extra:
        return [f"runtime learning recap has unsupported evidence cells: {extra[:5]}"]
    errors: list[str] = []
    for cell in RUNTIME_LEARNING_RECAP_CELLS:
        entry = cells.get(cell)
        if not isinstance(entry, dict):
            errors.append(f"runtime learning recap evidence cell {cell} must be an object")
            continue
        state = entry.get("state")
        if state not in RECAP_CELL_STATES:
            errors.append(f"runtime learning recap evidence cell {cell} state is invalid")
            continue
        source = entry.get("source_observation_type")
        if not isinstance(source, str):
            errors.append(f"runtime learning recap evidence cell {cell} source_observation_type must be a string")
            continue
        if state == "unavailable":
            # An unavailable cell may still name its source: a blocked or
            # cancelled observation is why the cell is unavailable, and saying
            # which one is the difference between "unproven" and "unexplained".
            # What it may not do is name an observation type that does not exist.
            if source and source not in RUNTIME_OBSERVABLE_EVENTS:
                errors.append(f"runtime learning recap evidence cell {cell} names an unknown observation type")
        else:
            if source not in RUNTIME_OBSERVABLE_EVENTS:
                errors.append(f"runtime learning recap evidence cell {cell} must name a supporting observation type")
            if entry.get("source_observation_schema") != RUNTIME_OBSERVATION_SCHEMA_VERSION:
                errors.append(f"runtime learning recap evidence cell {cell} must cite {RUNTIME_OBSERVATION_SCHEMA_VERSION}")
        errors.extend(_evidence_ref_errors(cell, entry))
    return errors


def _evidence_ref_errors(cell: str, entry: Mapping[str, Any]) -> list[str]:
    refs = entry.get("evidence_refs")
    if not isinstance(refs, list):
        return [f"runtime learning recap evidence cell {cell} evidence_refs must be a list"]
    if len(refs) > _MAX_EVIDENCE_REFS_PER_CELL:
        return [f"runtime learning recap evidence cell {cell} carries too many evidence references"]
    errors: list[str] = []
    for ref in refs:
        if not isinstance(ref, str) or not ref:
            errors.append(f"runtime learning recap evidence cell {cell} evidence reference must be a non-empty string")
            continue
        try:
            require_opaque_metadata_ref(ref, field=f"{cell} evidence reference")
        except ValueError as exc:
            errors.append(str(exc))
    count = entry.get("evidence_ref_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        errors.append(f"runtime learning recap evidence cell {cell} evidence_ref_count must be a non-negative integer")
    if not isinstance(entry.get("evidence_refs_truncated"), bool):
        errors.append(f"runtime learning recap evidence cell {cell} evidence_refs_truncated must be boolean")
    return errors


def _completion_errors(recap: Mapping[str, Any]) -> list[str]:
    completion = recap.get("observed_completion")
    if not isinstance(completion, dict):
        return ["runtime learning recap observed_completion must be an object"]
    errors: list[str] = []
    if completion.get("authority") != "observed_evidence":
        errors.append("runtime learning recap observed_completion authority must be observed_evidence")
    if completion.get("state") not in OBSERVED_COMPLETION_STATES:
        errors.append("runtime learning recap observed_completion state is invalid")
    cells = recap.get("evidence_cells")
    if not isinstance(cells, dict):
        return errors
    for key, state in (("observed_cells", "observed"), ("failed_cells", "failed"), ("unavailable_cells", "unavailable")):
        listed = completion.get(key)
        if not isinstance(listed, list) or any(not isinstance(item, str) for item in listed):
            errors.append(f"runtime learning recap observed_completion.{key} must be a list of strings")
            continue
        expected = [
            cell
            for cell in RUNTIME_LEARNING_RECAP_CELLS
            if isinstance(cells.get(cell), dict) and cells[cell].get("state") == state
        ]
        if list(listed) != expected:
            # A summary that disagrees with the cells is a recap whose headline
            # was edited away from its evidence.
            errors.append(f"runtime learning recap observed_completion.{key} does not match the evidence cells")
    state = completion.get("state")
    observed = [cell for cell in RUNTIME_LEARNING_RECAP_CELLS if isinstance(cells.get(cell), dict) and cells[cell].get("state") == "observed"]
    failed = [cell for cell in RUNTIME_LEARNING_RECAP_CELLS if isinstance(cells.get(cell), dict) and cells[cell].get("state") == "failed"]
    if state == "completed" and ("merge" not in observed or failed):
        errors.append("runtime learning recap may report completed only with an observed merge and no failed cell")
    if state == "partial" and not observed:
        errors.append("runtime learning recap may report partial only with at least one observed cell")
    if state == "unknown" and (observed or failed):
        errors.append("runtime learning recap may report unknown only with no observed or failed cell")
    if state == "failed" and not failed:
        errors.append("runtime learning recap may report failed only with a failed cell")
    return errors


def _identity_field_errors(recap: Mapping[str, Any]) -> list[str]:
    identity = recap.get("identity")
    if not isinstance(identity, dict):
        return ["runtime learning recap identity must be an object"]
    cells = recap.get("evidence_cells") if isinstance(recap.get("evidence_cells"), dict) else {}
    supporting = {"commit": "delivery", "changed_paths": "delivery", "pull_request": "pull_request", "merge_commit": "merge"}
    errors: list[str] = []
    for field, cell in supporting.items():
        entry = identity.get(field)
        if not isinstance(entry, dict):
            errors.append(f"runtime learning recap identity.{field} must be an object")
            continue
        if entry.get("state") not in ("observed", "unavailable"):
            errors.append(f"runtime learning recap identity.{field} state is invalid")
            continue
        if entry.get("state") != "observed":
            continue
        cell_entry = cells.get(cell)
        if not isinstance(cell_entry, dict) or cell_entry.get("state") == "unavailable":
            errors.append(f"runtime learning recap identity.{field} has no supporting observed evidence cell")
        if field == "changed_paths":
            errors.extend(_changed_path_errors(entry))
        elif not isinstance(entry.get("value"), str) or not entry.get("value"):
            errors.append(f"runtime learning recap identity.{field} must carry a bounded value")
    return errors


def _changed_path_errors(entry: Mapping[str, Any]) -> list[str]:
    paths = entry.get("paths")
    if not isinstance(paths, list):
        return ["runtime learning recap identity.changed_paths paths must be a list"]
    if len(paths) > _MAX_CHANGED_PATHS:
        return ["runtime learning recap identity.changed_paths carries too many paths"]
    errors: list[str] = []
    for value in paths:
        if not isinstance(value, str) or not _is_safe_relative_path(value):
            errors.append("runtime learning recap identity.changed_paths must carry bounded repository-relative paths")
            break
    return errors


def _operator_errors(recap: Mapping[str, Any]) -> list[str]:
    operator = recap.get("operator_assessment")
    if not isinstance(operator, dict):
        return ["runtime learning recap operator_assessment must be an object"]
    errors: list[str] = []
    if operator.get("authority") != "operator_supplied":
        errors.append("runtime learning recap operator_assessment authority must be operator_supplied")
    if operator.get("operator_outcome") not in OPERATOR_OUTCOMES:
        errors.append("runtime learning recap operator_outcome is invalid")
    feedback = operator.get("operator_feedback_summary")
    if not isinstance(feedback, str) or len(feedback) > _MAX_OPERATOR_FEEDBACK:
        errors.append("runtime learning recap operator_feedback_summary must be a bounded string")
    elif feedback and feedback != _REDACTED and is_unsafe_recap_text(feedback):
        errors.append("runtime learning recap operator_feedback_summary must not carry unbounded or sensitive text")
    if not isinstance(operator.get("operator_feedback_redacted"), bool):
        errors.append("runtime learning recap operator_feedback_redacted must be boolean")
    return errors


def _privacy_errors(recap: Mapping[str, Any]) -> list[str]:
    privacy = recap.get("privacy")
    if not isinstance(privacy, dict):
        return ["runtime learning recap privacy must be an object"]
    errors: list[str] = []
    if privacy.get("mode") != "metadata_only":
        errors.append("runtime learning recap privacy mode must be metadata_only")
    if privacy.get("raw_prompt_stored") is not False or privacy.get("raw_platform_event_stored") is not False:
        errors.append("runtime learning recaps must not store raw prompts or platform events")
    summary = recap.get("summary")
    if not isinstance(summary, str) or not summary or len(summary) > _MAX_SUMMARY:
        errors.append("runtime learning recap summary must be a bounded non-empty string")
    elif is_unsafe_recap_text(summary):
        errors.append("runtime learning recap summary must not carry sensitive or body-shaped text")
    if not isinstance(recap.get("claim_boundary"), str) or not recap.get("claim_boundary"):
        errors.append("runtime learning recap claim_boundary is required")
    guard = recap.get("overclaim_guard")
    if not isinstance(guard, list) or not guard or any(not isinstance(item, str) or not item for item in guard):
        errors.append("runtime learning recap overclaim_guard must be non-empty strings")
    return errors


def _content_safety_errors(value: Any, *, path: str = "", errors: list[str] | None = None) -> list[str]:
    """No forbidden key and no unsafe leaf anywhere in the record.

    The walk is the backstop for every producer, including a hand-edited store:
    the bounds above describe the fields this module writes, and this one
    describes the fields a recap may contain at all.
    """
    found = [] if errors is None else errors
    if len(found) >= _MAX_DIAGNOSTICS:
        return found
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text.casefold() in FORBIDDEN_RECAP_KEYS:
                found.append(f"runtime learning recap contains forbidden field: {path}{key_text}")
                continue
            _content_safety_errors(child, path=f"{path}{key_text}.", errors=found)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _content_safety_errors(child, path=f"{path}{index}.", errors=found)
    elif isinstance(value, str) and value and is_unsafe_recap_text(value):
        found.append(f"runtime learning recap field carries unsafe text: {path.rstrip('.')}")
    return found


# ---------------------------------------------------------------------------
# Store


def runtime_learning_recap_ref(recap_id: str) -> str:
    return f"{RUNTIME_LEARNING_RECAP_REF_PREFIX}:{_safe_id(str(recap_id))}"


def runtime_learning_recap_path(paths: OmhPaths, recap_id: str) -> Path:
    return paths.learning_recaps_dir / f"{_safe_id(str(recap_id))}.json"


def write_runtime_learning_recap(paths: OmhPaths, recap: dict[str, Any]) -> dict[str, Any]:
    """Persist one revision. Re-writing the same revision is idempotent.

    Idempotent because the recap carries no wall clock: the same run at the
    same runtime history revision produces the same id and the same bytes. A
    later observation mints a different id and lands beside the earlier file,
    so an already-reviewed revision is never rewritten under a reviewer.
    """
    validate_runtime_learning_recap(recap)
    atomic_write_json(runtime_learning_recap_path(paths, str(recap["recap_id"])), recap, private=True)
    return recap


def show_runtime_learning_recap(paths: OmhPaths, recap_id: str) -> dict[str, Any]:
    recap = read_json_object(runtime_learning_recap_path(paths, recap_id))
    if not recap:
        raise FileNotFoundError(recap_id)
    validate_runtime_learning_recap(recap)
    return recap


def list_runtime_learning_recaps(
    paths: OmhPaths,
    *,
    run_id: str = "",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Every stored revision, oldest revision first.

    Ordered by the newest observation each revision was built from, so the
    revisions of one run read as the order in which its evidence arrived.
    """
    directory = paths.learning_recaps_dir
    if not directory.exists():
        return []
    summaries: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        record = read_json_object(path)
        if not record:
            continue
        if runtime_learning_recap_errors(record):
            continue
        if run_id and str(record.get("run_id", "")) != run_id:
            continue
        summaries.append(_recap_summary_record(record))
    summaries.sort(key=lambda item: (str(item["run_id"]), str(item["latest_observation_at"]), str(item["recap_id"])))
    if limit is None:
        return summaries
    return summaries[-max(limit, 0):] if limit > 0 else []


def _recap_summary_record(recap: Mapping[str, Any]) -> dict[str, Any]:
    revision = recap.get("revision") if isinstance(recap.get("revision"), dict) else {}
    completion = recap.get("observed_completion") if isinstance(recap.get("observed_completion"), dict) else {}
    operator = recap.get("operator_assessment") if isinstance(recap.get("operator_assessment"), dict) else {}
    return {
        "schema_version": "runtime_learning_recap_summary/v1",
        "recap_id": str(recap.get("recap_id", "")),
        "recap_ref": runtime_learning_recap_ref(str(recap.get("recap_id", ""))),
        "run_id": str(recap.get("run_id", "")),
        "runtime_history_digest": str(revision.get("runtime_history_digest", "")),
        "observation_count": revision.get("observation_count", 0),
        "latest_observation_at": str(revision.get("latest_observation_at", "")),
        "observed_completion": str(completion.get("state", "")),
        "observed_completion_authority": "observed_evidence",
        "operator_outcome": str(operator.get("operator_outcome", "")),
        "operator_outcome_authority": "operator_supplied",
    }


def _safe_id(value: str) -> str:
    return _SAFE_ID_PATTERN.sub("-", value).strip("-")[:120] or "recap"
