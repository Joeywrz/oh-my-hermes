"""Shared constants and the borrowed `live-model-tools/v1` primitives.

This lane is a sibling of `benchmarks/live-model-tools/v1`, not a fork of it.
The artifact-safety rules and the pairing statistics are *imported* from that
lane so the two cannot drift; nothing here writes to it.

`live-model-tools/v1/lib/statistics.py` shadows the standard library module
name, so it is loaded inside a scope that pops and restores every colliding
`sys.modules` entry. That is the same loader the repository already uses in
`tests/test_live_model_benchmark_framework.py`, and
`tests/test_ci_offline_benchmark.py` asserts the standard library module
survives it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sys
from types import ModuleType

LANE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = LANE_ROOT.parents[2]
SHARED_LIB = REPO_ROOT / "benchmarks" / "live-model-tools" / "v1" / "lib"

MANIFEST_SCHEMA = "omh_product_ab_benchmark/v1"
CORPUS_SCHEMA = "omh_product_ab_corpus/v1"
RUN_SCHEMA = "omh_product_ab_run/v1"
RECEIPT_SCHEMA = "omh_product_ab_run_receipt/v1"
DOCTOR_SCHEMA = "omh_product_ab_doctor/v1"
REPORT_SCHEMA = "omh_product_ab_report/v1"

ARMS = ("hermes", "omh", "omh_mixture")

#: The hash seed every graded and probed unittest run is given. Any fixed value
#: works; what matters is that it is fixed, recorded, and the same on both
#: sides of every comparison the lane makes.
HASH_SEED = 0

#: The file a candidate writes to claim the goal is finished. A run that never
#: writes it made no completion claim, which is a different outcome from a
#: claim the validator contradicts.
COMPLETION_FILE = ".omh-product-ab-completion.json"

#: Grades. `pass` is the PR's own tests green plus the pre-existing regression
#: modules still green; everything else names why not.
GRADE_REASONS = (
    "passed",
    "target_tests_failed",
    "regression_tests_failed",
    "target_tests_errored",
    "regression_tests_errored",
    "validator_not_applicable",
    "run_failed",
)


@contextmanager
def shared_import_scope() -> Iterator[None]:
    """Prefer the sibling lane's bare imports without leaking them."""

    names = tuple(sorted(path.stem for path in SHARED_LIB.glob("*.py")))
    saved = {name: sys.modules.get(name) for name in names}
    original_path = list(sys.path)
    for name in names:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(SHARED_LIB))
    try:
        yield
    finally:
        sys.path[:] = original_path
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _load_shared() -> tuple[ModuleType, ModuleType]:
    with shared_import_scope():
        import common as shared_common  # noqa: PLC0415
        import statistics as shared_statistics  # noqa: PLC0415

        return shared_common, shared_statistics


common, statistics = _load_shared()

# Re-exported so the rest of the lane never re-implements an artifact rule.
artifact_is_safe = common.artifact_is_safe
append_jsonl = common.append_jsonl
canonical = common.canonical
digest = common.digest
file_digest = common.file_digest
load_object = common.load_object
safe_relative = common.safe_relative
tree_digest = common.tree_digest
write_json = common.write_json

exact_mcnemar = statistics.exact_mcnemar
holm = statistics.holm
percentile = statistics.percentile


def unittest_environment(workspace: Path, scratch: Path) -> dict[str, str]:
    """A bounded environment for a graded run or a verification check.

    `OMH_HOME` and `HERMES_HOME` point into the run's own scratch directory.
    Without that, a graded test module that never passes an explicit home
    writes into the real `~/.omh`, which is how this repository once
    accumulated thousands of journal events from test runs.
    """

    import os  # noqa: PLC0415

    for name in ("home", "tmp", "omh", "hermes"):
        (scratch / name).mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": "tests",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        # Pinned so that two runs of one validator can differ only for a reason
        # the harness caused. Unpinned, set and dict iteration order changes per
        # process, so a test carrying an ordering assumption is red in one run
        # and green in the next. The probe compares two runs and attributes any
        # difference to the workspace path, so an unpinned seed delivers a
        # flake under a name that asserts a cause the check cannot establish --
        # which is how PR-746 came to be recorded as path-dependent.
        "PYTHONHASHSEED": str(HASH_SEED),
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch / "tmp"),
        "OMH_HOME": str(scratch / "omh"),
        "HERMES_HOME": str(scratch / "hermes"),
        "TERMINAL_CWD": str(workspace),
    }


def text_digest(value: str) -> str:
    """sha256 of one whitespace-normalized text block."""

    import hashlib  # noqa: PLC0415

    return hashlib.sha256(" ".join(value.split()).encode("utf-8")).hexdigest()
