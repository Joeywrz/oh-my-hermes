"""Local executable and Git fixtures for paired-run CLI tests."""

from __future__ import annotations

from pathlib import Path
import subprocess


FAKE_HERMES = r"""
#!/usr/bin/env python3
import json
from pathlib import Path
import sys

root = Path(sys.argv[0]).resolve().parent
args = sys.argv[1:]
# Mirrors the installed Hermes CLI: only `chat --query-file -` reads stdin;
# `-z/--oneshot PROMPT` takes its prompt from argv (#1824).
if args[:3] == ["chat", "--query-file", "-"]:
    prompt = sys.stdin.read()
elif "--oneshot" in args:
    prompt = args[args.index("--oneshot") + 1]
else:
    raise SystemExit("no Hermes query transport in argv")
with (root / "calls.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": args, "prompt": prompt}) + "\n")
print("completed")
"""


def git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
