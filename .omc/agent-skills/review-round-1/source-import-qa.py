"""Exercise unowned Claude source import through a wheel-installed CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[3]
evidence = Path(__file__).resolve().parent
scratch = Path(tempfile.mkdtemp(prefix="omh-source-import-qa-")).resolve()
receipt = {"passed": False, "commands": []}
env = dict(os.environ, HOME=str(scratch / "home"), USERPROFILE=str(scratch / "home"))
for key in ("OMH_HOME", "HERMES_HOME"):
    env.pop(key, None)


def run(argv, cwd=scratch):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, env=env, text=True, capture_output=True, timeout=120)
    receipt["commands"].append({"argv": [str(a) for a in argv], "cwd": str(cwd), "exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
    result.check_returncode()
    return result


try:
    (scratch / "home").mkdir()
    run(["uv", "venv", "--python", root / ".venv/bin/python", scratch / "venv"])
    run(["uv", "pip", "install", "--python", scratch / "venv/bin/python", next((evidence / "dist").glob("*.whl"))])
    omh = scratch / "venv/bin/omh"
    repo = scratch / "repo"
    run(["git", "init", "-q", repo])
    run([omh, "install", "--target", "agents", "--scope", "repo", "--json"], cwd=repo)
    custom = repo / ".claude/skills/custom-skill/SKILL.md"
    custom.parent.mkdir()
    custom.write_text("---\nname: custom-skill\ndescription: Custom source fixture.\n---\nuser-owned-sentinel\n")
    reference = custom.parent / "references/custom.md"
    reference.parent.mkdir()
    reference.write_text("custom-reference-sentinel\n")
    omh_home = scratch / "imported"
    run([omh, "--omh-home", omh_home, "--hermes-home", scratch / "hermes", "install", "--from-skills-dir", repo, "--json"])
    imported = list((omh_home / "skills").rglob("SKILL.md"))
    assert len(imported) == 1, imported
    assert imported[0].parent.name == "omh-custom-skill"
    assert "user-owned-sentinel" in imported[0].read_text()
    assert (imported[0].parent / "references/custom.md").read_bytes() == reference.read_bytes()
    assert custom.read_text().endswith("user-owned-sentinel\n")
    receipt["passed"] = True
    receipt["imported_skill_names"] = [path.parent.name for path in imported]
finally:
    shutil.rmtree(scratch)
    receipt["cleanup"] = {"root": str(scratch), "removed": not scratch.exists()}
    (evidence / "source-import-qa.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"passed": receipt["passed"], "cleanup": receipt["cleanup"]}))
