"""Real CLI QA; all commands run outside the worktree, with isolated HOME."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path(os.environ.get("OMH_AGENT_SKILLS_QA_EVIDENCE", ROOT / ".omc/agent-skills"))
receipt = {"commands": [], "checks": {}}
parent = Path(tempfile.mkdtemp(prefix="omh-agent-skills-qa-")).resolve()
home = parent / "home"
repo = parent / "repo"
home.mkdir()
repo.mkdir()
env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
env.pop("OMH_HOME", None)
env.pop("HERMES_HOME", None)
# Keep package downloads in this invocation's temp tree for a cleanup receipt.
env["npm_config_cache"] = str(parent / "npm-cache")


def run(argv, cwd=parent, *, check=True, environment=env):
    result = subprocess.run([str(a) for a in argv], cwd=cwd, env=environment, capture_output=True, text=True, timeout=120)
    row = {"argv": [str(a) for a in argv], "cwd": str(cwd), "exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    receipt["commands"].append(row)
    if check and result.returncode:
        raise RuntimeError(json.dumps(row))
    return result


try:
    # Install the wheel, not an editable source tree, into an invocation-owned venv.
    run(["uv", "venv", "--python", str(ROOT / ".venv/bin/python"), str(parent / "venv")])
    wheel = next((EVIDENCE / "dist").glob("*.whl"))
    python = parent / "venv/bin/python"
    run(["uv", "pip", "install", "--python", python, wheel])
    omh = parent / "venv/bin/omh"
    run(["git", "init", "-q", repo])
    nested = repo / "nested"
    nested.mkdir()
    command = [omh, "install", "--target", "agents"]
    run(command + ["--scope", "repo"], cwd=nested)
    pack = repo / ".agents/skills"
    names = json.loads(run([python, "-P", "-c", "import json; from omh.skills.catalog_portable import portable_skill_names; print(json.dumps(portable_skill_names()))"]).stdout)
    assert sorted(p.name for p in pack.iterdir() if p.is_dir()) == sorted(names)
    assert not (nested / ".agents").exists() and not (nested / ".claude").exists()
    repo_mirror = repo / ".claude/skills"
    manifest_name = ".omh-agent-skills-manifest.json"
    repo_manifest = json.loads((pack / manifest_name).read_text())
    assert repo_manifest["target_dirs"] == [str(pack), str(repo_mirror)]
    assert (pack / manifest_name).read_bytes() == (repo_mirror / manifest_name).read_bytes()
    assert sorted(p.name for p in repo_mirror.iterdir() if p.is_dir()) == sorted(names)
    for relative in repo_manifest["files"]:
        assert (repo_mirror / relative).read_bytes() == (pack / relative).read_bytes()
        assert not (repo_mirror / relative).is_symlink()
    for name in ("ulw-work", "omh-frontend"):
        content = (pack / name / "SKILL.md").read_text()
        (EVIDENCE / f"qa-{name}.md").write_text(content)
        assert content.startswith("---\nname:")
    grep = run(["grep", "-riE", "Hermes-native|omh hermes|Wrapper Backend|skills.external_dirs", pack], check=False)
    assert grep.returncode == 1 and not grep.stdout
    receipt["checks"]["Q1"] = {"passed": True, "skill_count": len(names), "forbidden_token_hits": 0, "readbacks": ["qa-ulw-work.md", "qa-omh-frontend.md"]}

    run(command + ["--scope", "user"])
    user_pack, mirror = home / ".agents/skills", home / ".claude/skills"
    manifest = json.loads((user_pack / manifest_name).read_text())
    assert manifest["target_dirs"] == [str(user_pack), str(mirror)]
    assert (user_pack / manifest_name).read_bytes() == (mirror / manifest_name).read_bytes()
    for destination in (user_pack, mirror):
        assert sorted(p.name for p in destination.iterdir() if p.is_dir()) == sorted(names)
        for relative in manifest["files"]:
            assert (destination / relative).read_bytes() == (pack / relative).read_bytes()
            assert not (destination / relative).is_symlink()
    assert not (home / ".hermes").exists() and not (home / ".omh").exists()
    receipt["checks"]["Q2"] = {"passed": True, "manifest": manifest, "mirror_is_copy": True}

    # skills-ref validates one skill, not a collection root. Show that boundary,
    # then validate EVERY skill in both scopes and both Claude copies.
    root_attempt = run(["npx", "skills-ref", "validate", pack], check=False)
    receipt["validator_collection_boundary"] = {"exit": root_attempt.returncode, "output": root_attempt.stdout + root_attempt.stderr}
    paths = [destination / name for destination in (pack, repo_mirror, user_pack, mirror) for name in names]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda path: run(["npx", "skills-ref", "validate", path]), paths))
    receipt["checks"]["Q3"] = {"passed": all(r.returncode == 0 for r in results), "validated_skills": len(results), "destinations": 4}

    modified = repo_mirror / "ulw-work/SKILL.md"
    modified.write_bytes(modified.read_bytes() + b"\nlocal QA edit\n")
    status = json.loads(run(command + ["--scope", "repo", "--status", "--json"], cwd=repo).stdout)
    assert status["drift"] == "locally_modified"
    assert "mirror:ulw-work/SKILL.md" in status["locally_modified"]
    run(command + ["--scope", "repo"], cwd=repo)
    fresh = json.loads(run(command + ["--scope", "repo", "--status", "--json"], cwd=repo).stdout)
    assert fresh["projection"] == "fresh" and fresh["drift"] == "clean"
    (mirror / "ulw-work/SKILL.md").write_bytes(b"modified mirror")
    mirror_status = json.loads(run(command + ["--scope", "user", "--status", "--json"]).stdout)
    assert mirror_status["drift"] == "locally_modified"
    run(command + ["--scope", "user"])
    user_fresh = json.loads(run(command + ["--scope", "user", "--status", "--json"]).stdout)
    assert user_fresh["projection"] == "fresh" and user_fresh["drift"] == "clean"
    receipt["checks"]["Q4"] = {"passed": True, "modified_status": status, "fresh_status": fresh, "mirror_status": mirror_status, "user_fresh_status": user_fresh}
    outside = run(command + ["--scope", "repo"], check=False)
    assert outside.returncode != 0
    receipt["outside_repo_fail_closed"] = True
finally:
    shutil.rmtree(parent)
    receipt["cleanup"] = {"root": str(parent), "removed": not parent.exists(), "includes": ["git repo", "temporary HOME", "both user skill copies", "wheel venv", "npm cache"]}
    (EVIDENCE / "qa.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"checks": list(receipt["checks"]), "cleanup": receipt["cleanup"]}))
