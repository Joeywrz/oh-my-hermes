from __future__ import annotations

import argparse
from pathlib import Path

from ..installer import OmhError
from ..workflows.plugin_risk_audit import audit_plugin_risk
from .common import _print_json


def cmd_ops_plugin_risk_audit(args: argparse.Namespace) -> int:
    try:
        payload = audit_plugin_risk(Path(args.path))
    except (OSError, ValueError) as exc:
        raise OmhError(str(exc)) from exc
    _print_json(payload)
    return 0


def add_ops_plugin_risk_audit_command(ops_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    audit = ops_sub.add_parser(
        "plugin-risk-audit",
        help=(
            "Statically audit one explicit local plugin directory without importing or registering it, and classify "
            "the execution semantics of the hooks its manifest declares."
        ),
        description=(
            "Reads bounded text from one local plugin directory and reports aggregate risk categories plus, for every "
            "hook declared in a root plugin.yaml, whether that hook would gate, transform, contribute context to, or "
            "only observe an action on the supported Hermes contract. The result is an advisory static risk contract, "
            "not proof that the plugin is safe: nothing is imported, registered, installed or executed, so it is "
            "never evidence that a declared hook registered, ran, stayed inside its budget, or handled a failure "
            "correctly."
        ),
    )
    audit.add_argument("--path", required=True, help="Explicit local plugin root directory to audit.")
    audit.set_defaults(func=cmd_ops_plugin_risk_audit)
