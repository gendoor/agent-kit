"""Проверки компонентов установщика и безопасного обновления профиля OMO."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
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
            kit.merge_omo(project, [KIT_ROOT / "template/omo/profile.json"])
            updated = json.loads(kit.strip_jsonc(kit.read_text(path).lstrip("\ufeff")))
            agents = updated["[opencode]"]["agents"]
            self.assertEqual(agents["sisyphus"]["model"], "local/custom")
            self.assertEqual(agents["sisyphus"]["fallback_models"], [])
            self.assertEqual(agents["sisyphus"]["prompt_append"], "Свои правила")
            self.assertEqual(updated["[opencode]"]["categories"]["quick"]["models"],
                             [{"model": "local/quick"}])
            self.assertEqual(updated["_migrations"], ["local-migration"])
            self.assertEqual(len(agents), 11)
            self.assertNotIn("prompt_append", agents["prometheus"])
            self.assertEqual(path.with_name(f"omo.jsonc.bak-{kit.STAMP}").read_bytes(), raw.encode("utf-8"))
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", path.read_bytes())

    def test_repeated_merge_does_not_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            layers = [KIT_ROOT / "template/omo/profile.json"]
            kit.merge_omo(project, layers)
            path = project / ".omo" / "omo.jsonc"
            before = path.read_bytes()
            kit.merge_omo(project, layers)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob("*.bak-*")), [])

    def test_legacy_opus_route_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            path = project / ".omo" / "omo.jsonc"
            path.parent.mkdir()
            original = {
                "[opencode]": {
                    "agents": {
                        "sisyphus": {
                            "model": "externcash/claude-opus-5-5",
                            "fallback_models": [
                                {"model": "externcash/claude-opus-5-5"},
                                {"model": "local/fallback"},
                            ],
                        },
                    },
                    "categories": {
                        "writing": {
                            "models": [
                                {"model": "externcash/claude-opus-5-5", "reasoning": "low"},
                                {"model": "apiclo/claude-opus-5-5", "reasoning": "low"},
                            ],
                        },
                    },
                },
            }
            path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
            kit.merge_omo(project, [KIT_ROOT / "template/omo/profile.json"])
            updated = json.loads(kit.strip_jsonc(kit.read_text(path)))
            agent = updated["[opencode]"]["agents"]["sisyphus"]
            self.assertEqual(agent["model"], "apiclo/claude-opus-5-5")
            self.assertEqual(agent["fallback_models"], [{"model": "local/fallback"}])
            self.assertEqual(updated["[opencode]"]["categories"]["writing"]["models"],
                             [{"model": "apiclo/claude-opus-5-5", "reasoning": "low"}])
            self.assertNotIn("externcash/claude-opus-5-5", path.read_text(encoding="utf-8"))
            self.assertTrue(path.with_name(f"omo.jsonc.bak-{kit.STAMP}").is_file())

    def test_dry_run_does_not_create_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "new"
            kit.DRY_RUN = True
            try:
                kit.merge_omo(project, [KIT_ROOT / "template/omo/profile.json"])
            finally:
                kit.DRY_RUN = False
            self.assertFalse(project.exists())


class OmoPatchTests(unittest.TestCase):
    BASE = b"a\nb\nc\nd\n"
    PATCHED = b"a\nB\nc\nd\ne\n"
    DIFF = "--- a/dist/index.js\n+++ b/dist/index.js\n@@ -1,4 +1,5 @@\n a\n-b\n+B\n c\n d\n+e\n \n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "template/omo/patches").mkdir(parents=True)
        (root / "template/omo/patches/test.diff").write_text(self.DIFF, encoding="utf-8")
        self.target = root / "cache/opencode/packages/omo@1.0.0/node_modules/omo/dist/index.js"
        self.target.parent.mkdir(parents=True)
        self.patches = {"omo@1.0.0": {"title": "проверка", "diff": "test.diff", "file": "dist/index.js",
                                      "base": kit.sha256(self.BASE), "patched": kit.sha256(self.PATCHED)}}
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(kit, "TEMPLATE", root / "template"))
        self.stack.enter_context(patch.object(kit, "OMO_PATCHES", self.patches))
        self.stack.enter_context(patch.dict("os.environ", {"XDG_CACHE_HOME": str(root / "cache")}))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def test_shipped_patch_present(self):
        self.stack.close()
        meta = kit.OMO_PATCHES[kit.DEFAULT_OMO]
        self.assertTrue((KIT_ROOT / "template/omo/patches" / meta["diff"]).is_file())

    def test_applies_once_with_backup(self):
        self.target.write_bytes(self.BASE)
        kit.patch_omo("omo@1.0.0")
        self.assertEqual(self.target.read_bytes(), self.PATCHED)
        backups = list(self.target.parent.glob("index.js.bak-*"))
        self.assertEqual([p.read_bytes() for p in backups], [self.BASE])
        kit.patch_omo("omo@1.0.0")
        self.assertEqual(len(list(self.target.parent.glob("index.js.bak-*"))), 1)

    def test_unknown_content_is_kept(self):
        self.target.write_bytes(b"x\n")
        kit.patch_omo("omo@1.0.0")
        self.assertEqual(self.target.read_bytes(), b"x\n")

    def test_wrong_result_hash_aborts(self):
        self.patches["omo@1.0.0"]["patched"] = "0" * 64
        self.target.write_bytes(self.BASE)
        with self.assertRaises(kit.KitError):
            kit.patch_omo("omo@1.0.0")
        self.assertEqual(self.target.read_bytes(), self.BASE)

    def test_context_mismatch_raises(self):
        with self.assertRaises(kit.KitError):
            kit.apply_unified_diff(b"a\nX\nc\nd\n", self.DIFF)

    def test_dry_run_does_not_write(self):
        self.target.write_bytes(self.BASE)
        kit.DRY_RUN = True
        try:
            kit.patch_omo("omo@1.0.0")
        finally:
            kit.DRY_RUN = False
        self.assertEqual(self.target.read_bytes(), self.BASE)

    def test_project_plugin_spec_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.assertEqual(kit.project_omo_spec(project, "omo@1.0.0"), "omo@1.0.0")
            (project / "opencode.json").write_text(
                json.dumps({"plugin": ["other", "oh-my-openagent@4.18.0"]}), encoding="utf-8")
            self.assertEqual(kit.project_omo_spec(project, "omo@1.0.0"), "oh-my-openagent@4.18.0")


class InstallerComponentTests(unittest.TestCase):
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
                     patch.object(kit, "prepare_spec_install") as prepare, \
                     patch.object(kit, "install_spec", return_value="0.9.2") as spec, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(args.func(args), 0)
                self.assertEqual(base.call_count, int("base" in expected))
                self.assertEqual(prepare.call_count, int("spec" in expected))
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
