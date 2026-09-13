"""Real clone installers and catalog projection parity; no host execution claims."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from _cli_harness import run_cli

ROOT = Path(__file__).resolve().parents[1]
HOSTS = ("cursor", "claude", "codex", "opencode", "openclaw", "pi")


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class HostAdapterTests(unittest.TestCase):
    def test_committed_adapter_byte_gate(self):
        from contextlib import chdir
        with chdir(ROOT):
            code, output, error = run_cli(["docs", "agent-skills", "--check"])
        self.assertEqual(code, 0, error + output)
        self.assertEqual(json.loads(output).get("adapter_file_count"), 18)
        for host in HOSTS:
            for name in ("install.sh", "install.ps1", "manifest.json"):
                self.assertTrue((ROOT / f".{host}" / name).is_file())

    @unittest.skipUnless(shutil.which("sh"), "POSIX shell unavailable")
    def test_install_equality(self):
        self._install_equality("sh")

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell unavailable")
    def test_powershell_install_equality(self):
        self._install_equality(shutil.which("pwsh") or shutil.which("powershell"))

    def _install_equality(self, shell):
        expected = tree(ROOT / "agent-skills")
        for host in HOSTS:
            for user in (False, True):
                with self.subTest(host=host, user=user), tempfile.TemporaryDirectory(prefix="omh adapter ") as tmp:
                    root = Path(tmp).resolve()
                    home, repo = root / "home", root / "project"
                    home.mkdir()
                    repo.mkdir()
                    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
                    if shell == "sh":
                        command = [shell, str(ROOT / f".{host}/install.sh"), *(["--user"] if user else [])]
                    else:
                        command = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                   "-File", str(ROOT / f".{host}/install.ps1"), *(["-User"] if user else [])]
                    target = (home if user else repo) / (".claude/skills" if host == "claude" else ".agents/skills")
                    for _ in range(2):
                        result = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, timeout=60)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(tree(target), expected)
                        self.assertEqual(set(result.stdout.splitlines()[1:]), {p.split("/")[0] for p in expected})
                    self.assertFalse(((repo if user else home) / target.relative_to(home if user else repo)).exists())

    def test_manifest_source_digest_and_cli_lockstep(self):
        from contextlib import chdir
        import hashlib
        from unittest.mock import patch
        from omh.install.agent_skills_projection import MANIFEST_NAME
        from omh.skills.host_adapters import HOST_ADAPTERS, checksum_inventory
        self.assertEqual(tuple(adapter.host for adapter in HOST_ADAPTERS), HOSTS)
        expected = tree(ROOT / "agent-skills")
        hashes = {path: hashlib.sha256(content).hexdigest() for path, content in expected.items()}
        digest = hashlib.sha256(checksum_inventory(hashes).encode()).hexdigest()
        for host in HOSTS:
            manifest = json.loads((ROOT / f".{host}/manifest.json").read_bytes())
            self.assertEqual(manifest["files"], hashes)
            self.assertEqual(manifest["source_digest"], digest)
            self.assertEqual(manifest["transform"], "copy-only")
            self.assertEqual(manifest["evidence"], "docs/AGENT-SKILLS.md#host-support-matrix")
            for scope in ("repo", "user"):
                with self.subTest(host=host, scope=scope), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve()
                    home, repo = root / "home", root / "project"
                    home.mkdir()
                    repo.mkdir()
                    with chdir(repo), patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
                        command = ["install", "--target", "agents", "--host", host, "--json"]
                        if scope == "user":
                            command += ["--scope", scope]
                        target = (home if scope == "user" else repo) / manifest["targets"][scope]
                        for flag in ("--status", "--dry-run"):
                            code, output, error = run_cli(command + [flag])
                            self.assertEqual(code, 0, error)
                            self.assertEqual(json.loads(output)["projection"], "missing")
                            self.assertFalse(target.exists())
                        for _ in range(2):
                            code, output, error = run_cli(command)
                            self.assertEqual(code, 0, error)
                            payload = json.loads(output)
                            self.assertEqual(payload["target_dirs"], [str(target)])
                            self.assertEqual(payload["host_adapter"], manifest)
                            self.assertEqual(payload["projection"], "fresh")
                            installed = tree(target)
                            installed.pop(MANIFEST_NAME)
                            self.assertEqual(installed, expected)
                        self.assertFalse((home / ".hermes").exists())
                        self.assertFalse((home / ".omh").exists())
        self.assertNotEqual(run_cli(["install", "--host", "cursor"])[0], 0)
        with self.assertRaises(SystemExit) as rejected:
            run_cli(["install", "--target", "agents", "--host", "unknown"])
        self.assertEqual(rejected.exception.code, 2)

    def test_gate_catches_every_adapter_missing_stale_and_crlf_preserves_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            custom = root / ".claude/skills/triage-sweep/SKILL.md"
            custom.parent.mkdir(parents=True)
            custom.write_bytes(b"custom skill\n")
            command = ["docs", "agent-skills", "--output", str(root / "agent-skills")]
            self.assertEqual(run_cli(command)[0], 0)
            self.assertEqual(custom.read_bytes(), b"custom skill\n")
            self.assertEqual(run_cli(command + ["--check"])[0], 0)
            for host in HOSTS:
                for name in ("install.sh", "install.ps1", "manifest.json"):
                    relative = f".{host}/{name}"
                    path = root / relative
                    original = path.read_bytes()
                    with self.subTest(path=relative):
                        for content in (None, b"stale", original.replace(b"\n", b"\r\n")):
                            if content is None:
                                path.unlink()
                            else:
                                path.write_bytes(content)
                            code, output, error = run_cli(command + ["--check"])
                            self.assertEqual(code, 1, error)
                            key = "adapter_missing" if content is None else "adapter_stale"
                            self.assertEqual(json.loads(output)[key], [relative])
                        path.write_bytes(original)
            self.assertEqual(run_cli(command)[0], 0)
            self.assertEqual(run_cli(command + ["--check"])[0], 0)
            self.assertEqual(custom.read_bytes(), b"custom skill\n")

    def test_git_attributes_pin_all_generated_bytes(self):
        paths = [f".{host}/{name}" for host in HOSTS for name in ("install.sh", "install.ps1", "manifest.json")]
        result = subprocess.run(["git", "-c", "core.autocrlf=true", "check-attr", "eol", "--", *paths],
                                cwd=ROOT, text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual(result.stdout.splitlines(), [f"{path}: eol: lf" for path in paths])

    @unittest.skipUnless(shutil.which("sh"), "POSIX shell unavailable")
    def test_source_tampering_fails_before_writes(self):
        for host in HOSTS:
            for mutation in ("modified", "missing", "extra"):
                with self.subTest(host=host, mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp).resolve()
                    clone, repo = root / "clone", root / "project"
                    repo.mkdir()
                    shutil.copytree(ROOT / "agent-skills", clone / "agent-skills")
                    shutil.copytree(ROOT / f".{host}", clone / f".{host}", ignore=shutil.ignore_patterns("skills"))
                    path = clone / "agent-skills/ulw-work/SKILL.md"
                    if mutation == "modified":
                        path.write_bytes(path.read_bytes() + b"tampered")
                    elif mutation == "missing":
                        path.unlink()
                    else:
                        (path.parent / "extra.txt").write_bytes(b"extra")
                    result = subprocess.run(["sh", str(clone / f".{host}/install.sh")], cwd=repo,
                                            capture_output=True, text=True, timeout=60)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(result.stderr)
                    self.assertEqual(list(repo.iterdir()), [])

    @unittest.skipUnless(os.name != "nt" and shutil.which("sh"), "POSIX symlink/hash fallback test")
    def test_no_python_or_git_hash_fallback_and_preservation(self):
        for hasher in ("sha256sum", "shasum"):
            if not shutil.which(hasher):
                continue  # Only the installed platform checksum utilities can be exercised.
            with self.subTest(hasher=hasher), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                binaries, repo = root / "bin", root / "project"
                binaries.mkdir()
                repo.mkdir()
                for name in (hasher, "cat", "dirname", "find", "sort", "mkdir", "cp"):
                    (binaries / name).symlink_to(shutil.which(name))
                custom = repo / ".claude/skills/triage-sweep/SKILL.md"
                custom.parent.mkdir(parents=True)
                custom.write_bytes(b"custom")
                env = {**os.environ, "PATH": str(binaries)}
                result = subprocess.run([shutil.which("sh"), str(ROOT / ".claude/install.sh")], cwd=repo,
                                        env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(custom.read_bytes(), b"custom")
                installed = tree(custom.parents[1])
                installed.pop("triage-sweep/SKILL.md")
                self.assertEqual(installed, tree(ROOT / "agent-skills"))

    @unittest.skipUnless(os.name != "nt" and shutil.which("sh"), "POSIX symlink test")
    def test_symlink_destinations_and_invalid_flags_fail_before_writes(self):
        for relative in (".agents", ".agents/skills/ulw-work", ".agents/skills/ulw-work/SKILL.md"):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                repo, outside = root / "project", root / "outside"
                repo.mkdir()
                outside.mkdir()
                link = repo / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside)
                result = subprocess.run(["sh", str(ROOT / ".cursor/install.sh")], cwd=repo,
                                        capture_output=True, text=True, timeout=60)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertEqual(list(repo.rglob("SKILL.md")), [link] if relative.endswith("SKILL.md") else [])
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(["sh", str(ROOT / ".cursor/install.sh"), "--invalid"], cwd=tmp,
                                    capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
