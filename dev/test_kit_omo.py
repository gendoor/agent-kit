"""Проверки компонентов установщика и безопасного обновления профиля OMO."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

KIT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kit", KIT_ROOT / "lib" / "kit.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Не найден модуль установщика")
kit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kit)


class OmoProfileTests(unittest.TestCase):
    def test_jsonc_preserves_string_literals(self):
        raw = '''// комментарий
        {"url": "https://example.org/a", "prompt": "сохранить ,} и ,]", /* пояснение */
         "escaped": "кавычка \\\" // строка", "items": [1, 2,],}
        '''
        parsed = json.loads(kit.strip_jsonc(raw))
        self.assertEqual(parsed["prompt"], "сохранить ,} и ,]")
        self.assertEqual(parsed["url"], "https://example.org/a")
        self.assertEqual(parsed["escaped"], 'кавычка " // строка')
        self.assertEqual(parsed["items"], [1, 2])

    def test_jsonc_rejects_unclosed_comment(self):
        with self.assertRaises(kit.KitError):
            kit.strip_jsonc('{"agents": {}} /* незакрытый комментарий')

    def test_existing_values_and_arrays_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            path = project / ".omo" / "omo.jsonc"
            path.parent.mkdir()
            original = {
                "[opencode]": {
                    "agents": {"sisyphus": {
                        "model": "local/custom",
                        "fallback_models": [],
                        "prompt_append": "Свои правила",
                    }},
                    "categories": {"quick": {"models": [{"model": "local/quick"}]}},
                },
                "_migrations": ["local-migration"],
            }
            raw = "\ufeff// мои настройки\r\n" + json.dumps(original, ensure_ascii=False) + "\r\n"
            path.write_bytes(raw.encode("utf-8"))
            kit.merge_omo(project, [KIT_ROOT / "template/omo/profile.json",
                                    KIT_ROOT / "template/spec/omo-roles.json"])
            updated = json.loads(kit.strip_jsonc(kit.read_text(path).lstrip("\ufeff")))
            agents = updated["[opencode]"]["agents"]
            self.assertEqual(agents["sisyphus"]["model"], "local/custom")
            self.assertEqual(agents["sisyphus"]["fallback_models"], [])
            self.assertEqual(agents["sisyphus"]["prompt_append"], "Свои правила")
            self.assertEqual(updated["[opencode]"]["categories"]["quick"]["models"],
                             [{"model": "local/quick"}])
            self.assertEqual(updated["_migrations"], ["local-migration"])
            self.assertEqual(len(agents), 11)
            self.assertIn("prompt_append", agents["prometheus"])
            self.assertEqual(path.with_name(f"omo.jsonc.bak-{kit.STAMP}").read_bytes(), raw.encode("utf-8"))
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", path.read_bytes())

    def test_repeated_merge_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            layers = [KIT_ROOT / "template/omo/profile.json", KIT_ROOT / "template/spec/omo-roles.json"]
            kit.merge_omo(project, layers)
            path = project / ".omo" / "omo.jsonc"
            before = path.read_bytes()
            kit.merge_omo(project, layers)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob("*.bak-*")), [])

    def test_dry_run_does_not_create_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "new"
            kit.DRY_RUN = True
            try:
                kit.merge_omo(project, [KIT_ROOT / "template/omo/profile.json"])
            finally:
                kit.DRY_RUN = False
            self.assertFalse(project.exists())


class InstallerComponentTests(unittest.TestCase):
    def test_install_spec_preserves_conflicting_custom_skill(self):
        variables = {
            "KIT_VERSION": kit.KIT_VERSION,
            "FOCUSED_SPEC_VERSION": kit.FOCUSED_SPEC_VERSION,
            "PROJECT_NAME": "Проверка установки", "WING": "install_check",
        }
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            skill = project / ".opencode/skills/focused-spec/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("Custom instructions\n", encoding="utf-8")

            def run_command(command, **kwargs):
                if "init" in command:
                    report = {"root": str(project.resolve()), "tools": ["opencode"],
                              "files": [{"path": ".opencode/skills/focused-spec/SKILL.md",
                                         "action": "conflict", "applied": False}],
                              "errors": [{"path": ".opencode/skills/focused-spec/SKILL.md",
                                          "message": "existing file differs"}]}
                    return subprocess.CompletedProcess(command, 1, json.dumps(report), "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(kit, "node_tools", return_value=("node", "npm")), \
                 patch.object(kit.subprocess, "run", side_effect=run_command), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaisesRegex(kit.KitError, "existing file differs"):
                kit.install_spec(project, variables, check=True)
            self.assertEqual(skill.read_text(encoding="utf-8"), "Custom instructions\n")
            self.assertFalse((project / ".opencode/commands/focused-spec.md").exists())

    def test_valid_yaml_version_variants_allow_repeat_install(self):
        template = (KIT_ROOT / "template/spec/project/.focused-spec/config.yaml").read_text(encoding="utf-8")
        for version in ("version: 5 # Версия конфигурации", '"version": 5', "\ufeffversion: 5"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                config = project / ".focused-spec/config.yaml"
                config.parent.mkdir(parents=True)
                content = template.replace("version: 5", version.lstrip("\ufeff"))
                if version.startswith("\ufeff"):
                    content = "\ufeff" + content
                config.write_text(content, encoding="utf-8")
                args = kit.build_parser().parse_args([
                    "install", str(project), "--only", "spec", "--dry-run", "--skip-probe"])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(kit.cmd_install(args), 0)
                self.assertEqual(config.read_text(encoding="utf-8"), content)
                self.assertFalse((project / ".agent-kit.json").exists())

    def test_nested_version_does_not_mask_legacy_root_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = project / ".focused-spec/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text("version: 2\nspecifications:\n  version: 5\n", encoding="utf-8")
            args = kit.build_parser().parse_args([
                "install", str(project), "--only", "base,spec", "--dry-run", "--no-mempalace"])
            with patch.object(kit, "install_base") as base, \
                 patch.object(kit, "install_spec") as spec, \
                 self.assertRaisesRegex(kit.KitError, "не версии 5"):
                kit.cmd_install(args)
            base.assert_not_called()
            spec.assert_not_called()
            self.assertFalse((project / ".agent-kit.json").exists())

    def test_legacy_spec_project_refused_before_any_component_changes(self):
        for old_config, old_package in ((True, False), (False, True)):
            with self.subTest(old_config=old_config), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                config = project / ".focused-spec/config.yaml"
                config.parent.mkdir(parents=True)
                config.write_text("version: 2\nrunners: {}\n" if old_config else "version: 5\nrunners: {}\n",
                                  encoding="utf-8")
                package = project / "package.json"
                package.write_text(json.dumps({"devDependencies": {"focused-spec": "0.6.0" if old_package else "0.9.2"}}), encoding="utf-8")
                args = kit.build_parser().parse_args([
                    "install", str(project), "--only", "base,spec", "--no-mempalace", "--skip-probe"])
                with patch.object(kit, "node_tools", return_value=("node", "npm")), \
                     patch.object(kit, "install_base") as base, \
                     patch.object(kit, "install_spec") as spec, \
                     self.assertRaises(kit.KitError):
                    kit.cmd_install(args)
                base.assert_not_called()
                spec.assert_not_called()
                self.assertFalse((project / ".agent-kit.json").exists())
                self.assertFalse((project / "AGENTS.md").exists())

    def test_components_and_default_installation(self):
        self.assertEqual(kit.COMPONENTS, ("base", "spec"))
        self.assertEqual(kit.parse_components("spec,base,spec"), ("spec", "base"))
        with self.assertRaises(kit.KitError):
            kit.parse_components("unsupported")
        for options, expected in (([], ["base", "spec"]),
                                  (["--no-spec"], ["base"]),
                                  (["--no-spec", "--only", "spec"], ["spec"])):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                args = kit.build_parser().parse_args([
                    "install", str(project), "--no-mempalace", "--skip-probe", *options,
                ])
                with patch.object(kit, "install_base") as base, \
                     patch.object(kit, "install_spec") as spec, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(args.func(args), 0)
                self.assertEqual(base.call_count, int("base" in expected))
                self.assertEqual(spec.call_count, int("spec" in expected))
                state = json.loads((project / ".agent-kit.json").read_text(encoding="utf-8"))
                self.assertEqual(state["components"], expected)

    def test_wizard_dispatches_supported_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            for use_spec in (True, False):
                answers = iter((tmp, "Проверка мастера", "wizard_check", "none"))
                with self.subTest(use_spec=use_spec), \
                     patch.object(kit, "ask", side_effect=lambda *args: next(answers)), \
                     patch.object(kit, "ask_yes", side_effect=[False, use_spec, True]) as ask_yes, \
                     patch.object(kit, "cmd_install", return_value=0) as install, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(kit.cmd_wizard(None), 0)
                self.assertEqual(ask_yes.call_count, 3)
                args = install.call_args.args[0]
                self.assertEqual(args.no_spec, not use_spec)
                self.assertEqual(args.omo, "none")
                self.assertEqual(args.venv, "shared")


if __name__ == "__main__":
    unittest.main()
