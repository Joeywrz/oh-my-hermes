from __future__ import annotations

from .. import runtime_paths

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

from ..degradation import (
    COMPONENT_DELEGATION_ROUTE_RESTORE,
    degradation_payload,
    runtime_binding_degradation,
)
from ..delegation_route_restore import restore_delegation_baseline
from ..engagement_nudges import record_engagement_observer_failure
from ..host_observation import host_session_id, observe_plugin_hook_call
from .nudge_budget import note_delegated_session


def subagent_start(**kwargs) -> None:
    """Record that ``child_session_id`` names a delegated lane, not an orchestrator.

    The one thing OMH takes from this hook. A delegated child runs under its
    own session id, and the tool-result seam the engagement nudges ride carries
    no agent identity, so without this record those nudges cannot tell a
    subagent from the session that spawned it -- and would ask a subagent to
    declare the parent's checklist.

    Observation only: it writes no file, reads no runtime state, returns
    nothing, and never blocks a spawn. A host that does not call it is a host
    with no children to mistake for orchestrators, because the same
    `tools/delegate_tool.py` that creates a child emits this.

    Nothing here raises. The caller already wraps the invocation in its own
    quiet block, so a raise would be swallowed and this would simply stop
    recording -- invisibly, which is the failure worth avoiding. So the swallow
    writes a line: otherwise a failure here is observable ONLY as an absence
    (the `delegated_session` decline that never happens), and an absence needs a
    reader who already knew to expect it.
    """
    try:
        observe_plugin_hook_call("subagent_start", kwargs)
        note_delegated_session(kwargs.get("child_session_id"))
    except Exception as exc:  # noqa: BLE001 - swallowed upstream either way;
        # failing to record one child must not interrupt that child's spawn.
        # Recorded rather than silent: see `record_engagement_observer_failure`.
        record_engagement_observer_failure(type(exc).__name__)
        return None
    return None


def on_session_start(**kwargs) -> dict[str, object] | None:
    """Put back a delegation route whose writing session is gone.

    `on_session_end` is the ordinary way back, and a TUI that is killed never
    reaches it, so a route written two days ago can still be every later
    session's delegation default (#1724). This is the second path, and the
    reason it is a second path rather than the only one: it may act only on a
    route whose recorded writer is NOT a live session, so it cannot pull a
    baseline out from under a session that is still dispatching.

    OMH did not register `on_session_start` before this. It is the host's own
    first-turn lifecycle callback (`hermes_cli.plugins.VALID_HOOKS`), bounded
    and fail-open, and it carries the `session_id` the decision needs -- which
    is why the restore rides it rather than the first `pre_llm_call`, where the
    same work would have to re-derive "is this the first turn" on a hot path
    that runs every turn.

    Nothing here can stop a session starting. A binding failure returns the
    same bounded degradation block the sibling hooks return, a restore that
    fails returns its own, and the host discards the return either way -- the
    point of returning it is that the failure is named rather than absent.
    """
    try:
        omh_home = runtime_paths.plugin_home(kwargs.get("omh_home"))
        hermes_home = runtime_paths.plugin_home(kwargs.get("hermes_home"), hermes=True)
    except (runtime_paths.RuntimeBindingError, OSError, RuntimeError) as exc:
        return runtime_binding_degradation(exc)
    observe_plugin_hook_call("on_session_start", kwargs)
    restore = _restore_route(hermes_home, omh_home, trigger="session_start", orphaned=True)
    payload: dict[str, object] = {"status": "session_start", "route_restore": restore}
    _attach_restore_degradation(payload, restore)
    return payload


def on_session_end(**kwargs) -> dict[str, object] | None:
    """Restore this session's delegation route, then checkpoint OMH runtime state."""
    try:
        home = runtime_paths.plugin_home(kwargs.get("omh_home"))
        hermes_home = runtime_paths.plugin_home(kwargs.get("hermes_home"), hermes=True)
    except (runtime_paths.RuntimeBindingError, OSError, RuntimeError) as exc:
        return runtime_binding_degradation(exc)
    observe_plugin_hook_call("on_session_end", kwargs)
    # Scoped to the session that wrote the route. A session that ended while a
    # later session's route is in the file must not put the baseline back
    # underneath it, and the recorded writer is what says which one this is.
    restore = _restore_route(
        hermes_home,
        home,
        trigger="session_end",
        writer_session=host_session_id(kwargs),
    )
    runtime_dir = home / "runtime"
    if not runtime_dir.exists():
        payload: dict[str, object] = {"status": "no_runtime_state", "route_restore": restore}
        _attach_restore_degradation(payload, restore)
        return payload
    runs_dir = runtime_dir / "runs"
    run_count = len(list(runs_dir.glob("*/run.json"))) if runs_dir.exists() else 0
    state = _read_json(runtime_dir / "state.json")
    if not state and run_count == 0:
        payload = {"status": "no_runtime_state", "route_restore": restore}
        _attach_restore_degradation(payload, restore)
        return payload
    payload = {
        "schema_version": "omh_plugin_session_end/v1",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_state_present": bool(state),
        "latest_run_id": str(state.get("last_run_id", "")) if isinstance(state, dict) else "",
        "run_count": run_count,
        "privacy": "metadata_only",
        "claim_boundary": "This checkpoint proves only that the local OMH plugin hook ran; it is not execution, review, CI, merge, or Hermes reload evidence.",
    }
    path = runtime_dir / "plugin-session-end.json"
    _atomic_write_json(path, payload)
    checkpoint: dict[str, object] = {
        "status": "checkpoint_written",
        "path": str(path),
        "route_restore": restore,
    }
    _attach_restore_degradation(checkpoint, restore)
    return checkpoint


def _restore_route(
    hermes_home: Path,
    omh_home: Path,
    *,
    trigger: str,
    writer_session: str | None = None,
    orphaned: bool = False,
) -> dict[str, object]:
    """Call the restore and never let its failure reach the host as a raise.

    The restore module already turns its own expected faults into a `status`,
    so the narrow catch here is for the unexpected one. It is narrow on
    purpose: a bare `except Exception` would also hide a contract break in the
    caller, and this hook is fail-open at the host anyway -- the value added
    by catching is the named status, not the survival.
    """
    try:
        return restore_delegation_baseline(
            hermes_home,
            omh_home=omh_home,
            trigger=trigger,
            require_writer_session=writer_session,
            require_writer_not_live=orphaned,
        )
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "error", "trigger": trigger, "error": type(exc).__name__}


def _attach_restore_degradation(payload: dict[str, object], restore: dict[str, object]) -> None:
    """Name a failed restore in the hook payload instead of leaving an absence.

    Only a real failure degrades. `no_baseline_recorded`, `foreign_edit`,
    `not_last_writer` and `writer_live` are the restore working: each is a
    decision not to touch a value, and reporting them as degradation would put
    a permanent warning in front of every healthy session.
    """
    if str(restore.get("status", "")) not in ("error", "lock_unavailable"):
        return
    error_type = str(restore.get("error", "")) or str(restore.get("status", ""))
    payload["omh_degradation"] = degradation_payload(
        [(COMPONENT_DELEGATION_ROUTE_RESTORE, error_type)]
    )


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser().resolve()


def _read_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
