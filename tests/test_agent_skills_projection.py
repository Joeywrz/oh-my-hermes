"""Portable projection contracts; Hermes digests captured before target support."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest


class AgentSkillsProjectionTests(unittest.TestCase):
    def test_ulw_work_projection_has_no_hermes_framing(self):
        from omh.skills.render import agent_skill_templates
        templates = {t.name: t.content for t in agent_skill_templates()}
        self.assertIn("ulw-work", templates)
        self.assertIn("omh-frontend", templates)
        for name, content in templates.items():
            for token in ("Hermes-native", "omh hermes", "Wrapper Backend", "skills.external_dirs"):
                self.assertNotIn(token.lower(), content.lower(), name)

    def test_requires_omh_cli_skills_carry_compatibility_frontmatter(self):
        from omh.skills.catalog_portable import PORTABILITY_REQUIRES_OMH_CLI, skill_portability
        from omh.skills.render import agent_skill_templates
        required = []
        for template in agent_skill_templates():
            frontmatter = template.content.split("---", 2)[1]
            if skill_portability(template.name) == PORTABILITY_REQUIRES_OMH_CLI:
                required.append(template.name)
                self.assertIn("\ncompatibility:", frontmatter)
            else:
                self.assertNotIn("\ncompatibility:", frontmatter)
        self.assertIn("ulw-loop", required)

    def test_hermes_only_skills_absent_from_projection(self):
        from omh.skills.catalog_portable import PORTABILITY_HERMES_ONLY, portable_skill_names, skill_portability
        from omh.skills.render import agent_skill_templates
        names = tuple(t.name for t in agent_skill_templates())
        self.assertEqual(names, portable_skill_names())
        for name in ("ulw-maestro", "omh-routing", "omh-doctor", "unknown-future-skill"):
            self.assertNotIn(name, names)
            self.assertEqual(skill_portability(name), PORTABILITY_HERMES_ONLY)

    def test_hermes_projection_byte_stable(self):
        from omh.skills.render import builtin_skill_templates
        expected = json.loads((Path(__file__).parent / "fixtures/agent_skills_hermes_digests.json").read_text())
        actual = {t.name: hashlib.sha256(t.content.encode()).hexdigest() for t in builtin_skill_templates()}
        self.assertEqual(actual, expected)

    def test_user_scope_mirror_has_shared_manifest_and_drift(self):
        from omh.install.agent_skills_projection import install_agent_skills, agent_skills_status, MANIFEST_NAME
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".agents/skills"
            mirror = Path(tmp) / ".claude/skills"
            install_agent_skills(target, mirror=mirror)
            manifest = json.loads((target / MANIFEST_NAME).read_text())
            self.assertEqual(manifest["target_dirs"], [str(target), str(mirror)])
            self.assertEqual((target / MANIFEST_NAME).read_bytes(), (mirror / MANIFEST_NAME).read_bytes())
            self.assertFalse((mirror / "ulw-work/SKILL.md").is_symlink())
            self.assertEqual((target / "ulw-work/SKILL.md").read_bytes(), (mirror / "ulw-work/SKILL.md").read_bytes())
            (mirror / "ulw-work/SKILL.md").write_bytes(b"local mirror edit")
            status = agent_skills_status(target, mirror=mirror)
            self.assertEqual(status["drift"], "locally_modified")
            self.assertTrue(status["locally_modified"])
            self.assertEqual(install_agent_skills(target, mirror=mirror)["drift"], "clean")

    def test_agent_descriptions_are_bounded_without_changing_hermes(self):
        from dataclasses import replace
        from omh.skills.catalog import installable_skill_definitions
        from omh.skills.render import agent_skill_templates, agent_frontmatter_description, frontmatter_description
        for template in agent_skill_templates():
            line = next(line for line in template.content.splitlines() if line.startswith("description: "))
            description = json.loads(line.removeprefix("description: "))
            self.assertGreaterEqual(len(description), 1)
            self.assertLessEqual(len(description), 1024)
        # The router deliberately emits no trigger tail; exercise an ordinary skill.
        definition = replace(installable_skill_definitions()[1], description="x" * 990, triggers=("long trigger phrase",))
        self.assertLessEqual(len(agent_frontmatter_description(definition)), 1024)
        self.assertGreater(len(frontmatter_description(definition)), 1024)
        with self.assertRaises(ValueError):
            agent_frontmatter_description(replace(definition, description="x" * 1025))

    def test_install_manifest_detects_local_modification(self):
        from omh.install.agent_skills_projection import install_agent_skills, agent_skills_status, MANIFEST_NAME
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".agents/skills"
            status = install_agent_skills(target)
            self.assertEqual(status["projection"], "fresh")
            manifest = json.loads((target / MANIFEST_NAME).read_text())
            self.assertEqual(manifest["schema_version"], "omh_agent_skills_projection/v1")
            path = target / "ulw-work/SKILL.md"
            path.write_bytes(path.read_bytes() + b"\nlocal edit\n")
            status = agent_skills_status(target)
            self.assertEqual(status["drift"], "locally_modified")
            self.assertIn("ulw-work/SKILL.md", status["locally_modified"])
            self.assertTrue(status["next_action"])
            status = install_agent_skills(target)
            self.assertEqual(status["projection"], "fresh")
            self.assertEqual(status["drift"], "clean")

    def test_hermes_source_discovery_does_not_mix_agent_projections(self):
        from omh.converter import discover_skill_files, convert_references_from_dir
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            for directory in ("skills", "agent-skills", ".agents/skills"):
                skill = source / directory / "omh-frontend/SKILL.md"
                reference = skill.parent / "references/example.md"
                reference.parent.mkdir(parents=True)
                skill.write_text("---\nname: omh-frontend\n---\n" + directory)
                reference.write_text(directory)
            self.assertEqual(discover_skill_files(source), [source / "skills/omh-frontend/SKILL.md"])
            self.assertEqual([ref.content for ref in convert_references_from_dir(source)], ["skills"])
            # An explicit import root still means exactly what the operator named.
            self.assertEqual(discover_skill_files(source / "agent-skills"), [source / "agent-skills/omh-frontend/SKILL.md"])

    def test_repo_scope_ignores_ambient_git_target_and_bounds_the_probe(self):
        from contextlib import chdir
        import os
        import subprocess
        from unittest.mock import patch
        from omh.install.agent_skills_projection import agent_skills_targets
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo, foreign = root / "repo", root / "foreign"
            for path in (repo, foreign):
                subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
            with chdir(repo), patch.dict(os.environ, {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign)}):
                self.assertEqual(agent_skills_targets("repo"), (repo / ".agents/skills", None))
            with patch("omh.install.agent_skills_projection.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)) as runner:
                with self.assertRaisesRegex(ValueError, "timed out"):
                    agent_skills_targets("repo")
                self.assertEqual(runner.call_args.kwargs["timeout"], 10)

    def test_projection_references_resolve_and_shipped_bytes_match(self):
        import re
        from omh.install.agent_skills_projection import agent_skill_files
        root = Path(__file__).resolve().parents[1] / "agent-skills"
        files = agent_skill_files()
        self.assertEqual({p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}, set(files))
        for relative, content in files.items():
            self.assertEqual((root / relative).read_bytes(), content.encode("utf-8"), relative)
            for ref in re.findall(r"`((?:omh-[\w-]+/|ulw-[\w-]+/)?references/[\w./-]+)`", content):
                key = ref if ref.startswith(("omh-", "ulw-")) else relative.split("/")[0] + "/" + ref
                self.assertIn(key, files, relative)
            for token in ("Hermes-native", "omh hermes", "Wrapper Backend", "skills.external_dirs"):
                self.assertNotIn(token.lower(), content.lower(), relative)

    def test_manifest_revision_and_mirror_loss_are_not_fresh(self):
        from omh.install.agent_skills_projection import install_agent_skills, agent_skills_status, MANIFEST_NAME
        with tempfile.TemporaryDirectory() as tmp:
            target, mirror = Path(tmp) / ".agents/skills", Path(tmp) / ".claude/skills"
            install_agent_skills(target, mirror=mirror)
            path = target / MANIFEST_NAME
            manifest = json.loads(path.read_text())
            manifest["catalog_revision"] = "old-revision"
            path.write_text(json.dumps(manifest))
            status = agent_skills_status(target, mirror=mirror)
            self.assertEqual(status["projection"], "stale")
            self.assertEqual(status["drift"], "clean")
            (mirror / MANIFEST_NAME).unlink()
            self.assertEqual(agent_skills_status(target, mirror=mirror)["projection"], "missing")

    def test_manifest_traversal_and_symlink_destinations_fail_before_writes(self):
        from omh.install.agent_skills_projection import install_agent_skills, MANIFEST_NAME
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, mirror = root / ".agents/skills", root / ".claude/skills"
            install_agent_skills(target)
            manifest_path = target / MANIFEST_NAME
            original = manifest_path.read_bytes()
            manifest = json.loads(original)
            manifest["files"]["../../outside"] = "a" * 64
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                install_agent_skills(target, mirror=mirror)
            self.assertFalse(mirror.exists())
            manifest_path.write_bytes(original)
            outside = root / "outside"
            outside.mkdir()
            mirror.parent.mkdir()
            mirror.symlink_to(outside, target_is_directory=True)
            before = {p: p.read_bytes() for p in target.rglob("*") if p.is_file()}
            with self.assertRaisesRegex(ValueError, "symlink"):
                install_agent_skills(target, mirror=mirror)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual(before, {p: p.read_bytes() for p in target.rglob("*") if p.is_file()})

    def test_unowned_collisions_refused_and_unrelated_skills_preserved(self):
        from omh.install.agent_skills_projection import install_agent_skills
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".agents/skills"
            collision = target / "ulw-work/SKILL.md"
            collision.parent.mkdir(parents=True)
            collision.write_bytes(b"not installed by OMH")
            with self.assertRaisesRegex(ValueError, "unowned"):
                install_agent_skills(target)
            self.assertEqual(collision.read_bytes(), b"not installed by OMH")
            collision.unlink()
            foreign = target / "foreign/SKILL.md"
            foreign.parent.mkdir()
            foreign.write_bytes(b"foreign skill")
            install_agent_skills(target)
            self.assertEqual(foreign.read_bytes(), b"foreign skill")

    def test_retired_manifest_files_removed_without_removing_foreign_files(self):
        from omh.install.agent_skills_projection import install_agent_skills, MANIFEST_NAME
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".agents/skills"
            install_agent_skills(target)
            retired = target / "retired/SKILL.md"
            retired.parent.mkdir()
            retired.write_bytes(b"old generated skill")
            foreign = retired.parent / "notes.txt"
            foreign.write_bytes(b"user notes")
            path = target / MANIFEST_NAME
            manifest = json.loads(path.read_text())
            manifest["files"]["retired/SKILL.md"] = hashlib.sha256(retired.read_bytes()).hexdigest()
            path.write_text(json.dumps(manifest))
            self.assertEqual(install_agent_skills(target)["projection"], "fresh")
            self.assertFalse(retired.exists())
            self.assertEqual(foreign.read_bytes(), b"user notes")

    def test_byte_gate_detects_missing_extra_stale_and_crlf(self):
        from _cli_harness import run_cli
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pack"
            command = ["docs", "agent-skills", "--output", str(root)]
            self.assertEqual(run_cli(command)[0], 0)
            self.assertEqual(run_cli(command + ["--check"])[0], 0)
            (root / "ulw-work/SKILL.md").unlink()
            (root / "extra.txt").write_bytes(b"extra")
            changed = root / "omh-frontend/SKILL.md"
            changed.write_bytes(changed.read_bytes().replace(b"\n", b"\r\n"))
            status, output, _ = run_cli(command + ["--check"])
            payload = json.loads(output)
            self.assertEqual(status, 1)
            self.assertEqual(payload["missing"], ["ulw-work/SKILL.md"])
            self.assertEqual(payload["extra"], ["extra.txt"])
            self.assertEqual(payload["stale"], ["omh-frontend/SKILL.md"])

    def test_cli_scopes_use_git_root_and_home_and_status_is_read_only(self):
        from contextlib import chdir
        import os
        import subprocess
        from unittest.mock import patch
        from _cli_harness import run_cli
        from omh.skills.catalog_portable import portable_skill_names
        from omh.install.agent_skills_projection import MANIFEST_NAME
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home, repo = root / "home", root / "repo"
            home.mkdir()
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
            nested = repo / "nested"
            nested.mkdir()
            with chdir(nested), patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
                command = ["install", "--target", "agents", "--scope", "repo", "--json"]
                status, output, error = run_cli(command + ["--status"])
                self.assertEqual(status, 0, error)
                self.assertEqual(json.loads(output)["projection"], "missing")
                self.assertFalse((repo / ".agents").exists())
                self.assertEqual(run_cli(command + ["--dry-run"])[0], 0)
                self.assertFalse((repo / ".agents").exists())
                self.assertEqual(run_cli(command)[0], 0)
                self.assertEqual({p.name for p in (repo / ".agents/skills").iterdir() if p.is_dir()}, set(portable_skill_names()))
                self.assertFalse((nested / ".agents").exists())
                self.assertFalse((repo / ".claude").exists())
                user = ["install", "--target", "agents", "--scope", "user", "--json"]
                self.assertEqual(run_cli(user)[0], 0)
                self.assertEqual((home / ".agents/skills" / MANIFEST_NAME).read_bytes(), (home / ".claude/skills" / MANIFEST_NAME).read_bytes())
                self.assertFalse((home / ".hermes").exists())
                self.assertFalse((home / ".omh").exists())
            with chdir(home):
                self.assertNotEqual(run_cli(command)[0], 0)
                self.assertNotEqual(run_cli(["install", "--target", "agents", "--json"])[0], 0)


if __name__ == "__main__":
    unittest.main()
