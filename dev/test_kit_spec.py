"""Проверки установки latest, сохранности проекта и штатного CLI focused-spec."""
from __future__ import annotations

import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_kit_omo import KIT_ROOT, kit

SKILL, COMMAND = kit.SPEC_ASSETS
ASSET = "Штатный загрузчик\n"
VERSION = "9.1.0"
CONTRACTS = {"guidance": 1, "configuration": 27, "format": 3, "runner": 2}
WORKFLOW = {"protocolVersion": 1, "package": {"name": "focused-spec", "version": VERSION},
            "topic": "workflow", "content": "# Порядок работы\n\nИнструкции пакета.\n"}


def put(project: Path, relative: str, content: str) -> Path:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    return path


def snapshot(project: Path) -> dict:
    return {str(path.relative_to(project)): path.read_bytes()
            for path in project.rglob("*") if path.is_file()}


def without_backups(files: dict) -> dict:
    return {name: content for name, content in files.items() if ".bak-" not in name}


def project_install(command: list) -> bool:
    return (len(command) > 1 and Path(command[0]).name in ("npm", "npm.cmd")
            and command[1] == "install" and "--prefix" not in command)


def fake_package(project: Path, version: str = VERSION, content: str = ASSET) -> Path:
    put(project, "node_modules/focused-spec/package.json", json.dumps({"name": "focused-spec", "version": version}))
    cli = put(project, "node_modules/focused-spec/dist/cli.js", "")
    for relative in kit.SPEC_ASSETS.values():
        put(project, "node_modules/focused-spec/" + relative, content)
    return cli


class SpecInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.latest_version = VERSION
        self.latest_asset = ASSET
        self.contracts = dict(CONTRACTS)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.addCleanup(setattr, kit, "DRY_RUN", False)

    def fake_run(self, command, **kwargs):
        if command[0] == "npm":
            if command[1] == "view":
                return subprocess.CompletedProcess(command, 0, json.dumps(self.latest_version), "")
            destination = Path(command[command.index("--prefix") + 1]) if "--prefix" in command else self.project
            requested = next(value.split("@", 1)[1] for value in command if value.startswith("focused-spec@"))
            fake_package(destination, requested, self.latest_asset)
            if project_install(command):
                package = kit.load_state(destination / "package.json")
                for section in ("devDependencies", "dependencies"):
                    if "focused-spec" in package.get(section, {}):
                        package[section]["focused-spec"] = requested
                put(destination, "package.json", json.dumps(package))
                lock = {"packages": {"": package, "node_modules/focused-spec": {"version": requested}}}
                put(destination, "package-lock.json", json.dumps(lock))
            return subprocess.CompletedProcess(command, 0, "", "")
        version = kit.spec_package(Path(command[1]))["version"]
        if "instructions" in command:
            report = {**WORKFLOW, "package": {"name": "focused-spec", "version": version}}
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")
        if "version" in command:
            report = {"package": {"name": "focused-spec", "version": version},
                      "executable": str(Path(command[1]).resolve()), "contracts": self.contracts}
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")
        files, errors = [], []
        preview = "--dry-run" in command
        assets = kit.bundled_spec_assets(Path(command[1]))
        for relative, content in assets.items():
            path = self.project / relative
            action = "install" if not path.exists() else "unchanged" if path.read_bytes() == content else "conflict"
            files.append({"path": relative, "action": action, "applied": not preview and action == "install"})
            if action == "conflict":
                errors.append({"path": relative, "message": "custom integration differs; preserve it or explicitly use --replace"})
        if not preview and not errors:
            for relative, content in assets.items():
                put(self.project, relative, content.decode("utf-8"))
        report = {"root": str(self.project.resolve()), "tools": ["opencode"], "dryRun": preview,
                  "files": files, "errors": errors}
        return subprocess.CompletedProcess(command, int(bool(errors)), json.dumps(report), "")

    def install(self):
        with patch.object(kit, "node_tools", return_value=("node", "npm")), \
             patch.object(kit.subprocess, "run", side_effect=self.fake_run) as run:
            kit.install_spec(self.project, check=True)
        return [call.args[0] for call in run.call_args_list]

    def test_fresh_install_only_initializes_opencode_and_is_idempotent(self):
        root = "# Проект\n\nЗагрузи focused-spec при задаче по спецификации.\n"
        put(self.project, "AGENTS.md", root)
        commands = self.install()
        init = [command for command in commands if "init" in command]
        self.assertTrue(all("--tools" in command and "opencode" in command for command in init))
        self.assertIn("--dry-run", init[0])
        self.assertTrue(any("version" in command and command[0] == "node" for command in commands))
        self.assertFalse(any("--replace" in command or "validate" in command for command in commands))
        self.assertFalse((self.project / ".focused-spec").exists())
        self.assertFalse((self.project / ".omo").exists())
        self.assertEqual(kit.read_text(self.project / "AGENTS.md"), root)
        self.assert_latest_manifest_and_lock()
        before = snapshot(self.project)
        self.install()
        self.assertEqual(without_backups(snapshot(self.project)), without_backups(before))

    def assert_latest_manifest_and_lock(self, section="devDependencies"):
        self.assertEqual(kit.load_state(self.project / "package.json")[section]["focused-spec"], "latest")
        lock = kit.load_state(self.project / "package-lock.json")["packages"]
        self.assertEqual(lock[""][section]["focused-spec"], "latest")
        self.assertEqual(lock["node_modules/focused-spec"]["version"], self.latest_version)

    def test_latest_refreshes_package_owned_bundles_with_backups(self):
        self.install()
        self.latest_version = "9.2.0"
        self.latest_asset = "Новая штатная интеграция\n"
        original_run = self.fake_run

        def check_backups_before_update(command, **kwargs):
            if project_install(command):
                for relative in (SKILL, COMMAND):
                    path = self.project / relative
                    self.assertEqual(kit.read_text(path), ASSET)
                    self.assertEqual([kit.read_text(item) for item in path.parent.glob(path.name + ".bak-*")], [ASSET])
            return original_run(command, **kwargs)

        self.fake_run = check_backups_before_update
        commands = self.install()
        self.assertEqual(commands[0], ["npm", "view", "focused-spec@latest", "version", "--json"])
        self.assertTrue(any(project_install(command) and "focused-spec@9.2.0" in command for command in commands))
        self.assertFalse(any("--replace" in command for command in commands))
        for relative in (SKILL, COMMAND):
            self.assertEqual(kit.read_text(self.project / relative), self.latest_asset)
        self.assert_latest_manifest_and_lock()

    def test_custom_loader_conflict_preserves_entire_project(self):
        self.install()
        put(self.project, SKILL, "Свой загрузчик\n")
        before = snapshot(self.project)
        with self.assertRaises(kit.KitError):
            self.install()
        self.assertEqual(snapshot(self.project), before)

    def test_custom_config_runner_documents_root_and_omo_are_byte_identical(self):
        files = {
            ".focused-spec/config.yaml": f"\ufeff# Мой конфиг\r\nversion: {self.contracts['configuration']}\r\nrunners: {{}}\r\n",
            ".focused-spec/runners/custom.ts": "// Мой адаптер\n",
            ".opencode/skills/focused-spec/references/custom.md": "Мой справочник\n",
            "docs/orders-spec.md": "# Спецификация проекта\n",
            "AGENTS.md": "\ufeff# Мои инструкции\r\n",
            ".omo/omo.jsonc": '\ufeff// Мои настройки\r\n{"[opencode]":{"agents":{"my-agent":{"prompt_append":"Свои инструкции"}}}}\r\n',
        }
        for relative, content in files.items():
            put(self.project, relative, content)
        self.install()
        for relative, content in files.items():
            path = self.project / relative
            self.assertEqual(path.read_bytes(), content.encode("utf-8"))
            self.assertEqual(list(path.parent.glob(path.name + ".bak-*")), [])

    def test_script_config_paths_are_never_evaluated_as_shell(self):
        for command in ("focused-spec run --config $SPEC_CONFIG", "focused-spec run --config=$(touch unsafe)",
                        "focused-spec run --config %SPEC_CONFIG%", "focused-spec run --config ../outside.yaml",
                        "cd other && focused-spec run --config config.yaml", "focused-spec run --config"):
            with self.subTest(command=command):
                put(self.project, "package.json", json.dumps({"scripts": {"spec:run": command}}))
                before = snapshot(self.project)
                with patch.object(kit.subprocess, "run") as run, self.assertRaises(kit.KitError):
                    kit.install_spec(self.project, check=True)
                run.assert_not_called()
                self.assertEqual(snapshot(self.project), before)

    def test_custom_script_configs_use_latest_configuration_contract(self):
        path = put(self.project, "custom configs/current.yaml", f"\ufeffversion: {self.contracts['configuration']}\r\nrunners: {{}}\r\n")
        scripts = {"spec:run": 'focused-spec run --config "custom configs/current.yaml"',
                   "spec:validate": 'focused-spec validate --config="custom configs/current.yaml"'}
        put(self.project, "package.json", json.dumps({"scripts": scripts}))
        before = path.read_bytes()
        self.install()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(kit.load_state(self.project / "package.json")["scripts"], scripts)
        self.contracts["configuration"] += 1
        before = snapshot(self.project)
        with self.assertRaisesRegex(kit.KitError, "custom configs/current.yaml"):
            self.install()
        self.assertEqual(snapshot(self.project), before)

    def test_active_config_and_installed_cli_contracts_are_checked_before_npm_update(self):
        fake_package(self.project)
        config = put(self.project, ".focused-spec/config.yaml", f"version: {self.contracts['configuration']}\nrunners: {{}}\n")
        original_run = self.fake_run
        for key in ("configuration", "format", "runner"):
            def incompatible(command, **kwargs):
                result = original_run(command, **kwargs)
                if "version" in command and command[0] != "npm" and str(self.project) not in command[1]:
                    report = json.loads(result.stdout)
                    report["contracts"][key] += 1
                    return subprocess.CompletedProcess(command, 0, json.dumps(report), "")
                if project_install(command):
                    self.fail("Нельзя обновлять зависимости с несовместимыми контрактами")
                return result

            with self.subTest(contract=key):
                self.fake_run = incompatible
                before = snapshot(self.project)
                with self.assertRaises(kit.KitError):
                    self.install()
                self.assertEqual(snapshot(self.project), before)
                self.assertTrue(config.exists())

    def test_state_records_actual_version_and_backs_up_previous_state(self):
        state = {"kitVersion": kit.KIT_VERSION, "components": ["base"], "name": "Мой проект"}
        path = put(self.project, ".agent-kit.json", json.dumps(state))
        before = path.read_bytes()
        args = kit.build_parser().parse_args(["install", str(self.project), "--only", "spec"])
        with patch.object(kit, "node_tools", return_value=("node", "npm")), \
             patch.object(kit.subprocess, "run", side_effect=self.fake_run):
            self.assertEqual(args.func(args), 0)
            updated = kit.load_state(path)
            self.assertEqual(updated["focusedSpecVersion"], self.latest_version)
            self.assertEqual(updated["components"], ["base", "spec"])
            self.assertEqual([item.read_bytes() for item in self.project.glob(".agent-kit.json.bak-*")], [before])
            before = snapshot(self.project)
            self.assertEqual(args.func(args), 0)
            self.assertEqual(without_backups(snapshot(self.project)), without_backups(before))

    def test_latest_tag_race_installs_checked_version(self):
        original_run = self.fake_run

        def changed_registry(command, **kwargs):
            if project_install(command):
                self.latest_version = "9.2.0"
                self.assertIn(f"focused-spec@{VERSION}", command)
            return original_run(command, **kwargs)

        self.fake_run = changed_registry
        self.install()
        self.assertEqual(kit.spec_package(kit.spec_cli(self.project))["version"], VERSION)
        self.fake_run = original_run
        self.install()
        self.assertEqual(kit.spec_package(kit.spec_cli(self.project))["version"], self.latest_version)
        self.assert_latest_manifest_and_lock()

    def test_production_dependency_stays_in_original_section(self):
        put(self.project, "package.json", json.dumps({"dependencies": {"focused-spec": f"^{VERSION}"}}))
        self.install()
        self.assert_latest_manifest_and_lock(section="dependencies")
        self.assertNotIn("devDependencies", kit.load_state(self.project / "package.json"))

    def test_lock_backup_precedes_npm_changes(self):
        for name in ("package-lock.json", "npm-shrinkwrap.json"):
            put(self.project, name, '{"исходник":true}\n')
        self.install()
        for name in ("package-lock.json", "npm-shrinkwrap.json"):
            self.assertEqual([kit.read_text(path) for path in self.project.glob(name + ".bak-*")], ['{"исходник":true}\n'])

    def test_failed_native_init_restores_package_owned_files(self):
        self.install()
        self.latest_asset = "Новая интеграция\n"
        original_run = self.fake_run

        def fail(command, **kwargs):
            if "init" in command and "--dry-run" not in command:
                return subprocess.CompletedProcess(command, 1, '{"files":[],"errors":[{"message":"Ошибка записи"}]}', "")
            return original_run(command, **kwargs)

        self.fake_run = fail
        with self.assertRaises(kit.KitError):
            self.install()
        for relative in (SKILL, COMMAND):
            path = self.project / relative
            self.assertEqual(kit.read_text(path), ASSET)
            self.assertEqual([kit.read_text(item) for item in path.parent.glob(path.name + ".bak-*")], [ASSET])

    def test_npm_hook_cannot_authorize_changed_loader(self):
        self.install()
        self.latest_asset = "Новая интеграция\n"
        original_run = self.fake_run

        def change_loader(command, **kwargs):
            result = original_run(command, **kwargs)
            if project_install(command):
                put(self.project, SKILL, "Изменение проектного хука\n")
            return result

        self.fake_run = change_loader
        with self.assertRaisesRegex(kit.KitError, "файл изменился"):
            self.install()
        self.assertEqual(kit.read_text(self.project / SKILL), "Изменение проектного хука\n")

    def test_resolution_failure_preserves_project(self):
        put(self.project, "AGENTS.md", "# Проект\n")
        before = snapshot(self.project)
        with patch.object(kit, "node_tools", return_value=("node", "npm")), \
             patch.object(kit.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "{}", "Ошибка реестра")):
            with self.assertRaisesRegex(kit.KitError, "npm view"):
                kit.install_spec(self.project, check=True)
        self.assertEqual(snapshot(self.project), before)

    def test_dry_run_preserves_project_and_missing_target_without_downloads(self):
        fake_package(self.project)
        before = snapshot(self.project)
        kit.DRY_RUN = True
        commands = self.install()
        self.assertFalse(any(command[0] == "npm" for command in commands))
        self.assertEqual(snapshot(self.project), before)
        target = self.project / "new"
        args = kit.build_parser().parse_args(["install", str(target), "--only", "spec", "--dry-run"])
        with patch.object(kit, "node_tools", return_value=None):
            self.assertEqual(args.func(args), 0)
        self.assertFalse(target.exists())

    def test_competing_managers_block_before_base_commands_or_writes(self):
        for package, lock in (({"packageManager": "yarn@4"}, None), ({"packageManager": "pnpm@10"}, None),
                              ({"packageManager": "bun@1"}, None), *[({}, name) for name in
                              ("yarn.lock", "pnpm-lock.yaml", "pnpm-lock.yml", "bun.lock", "bun.lockb")]):
            with self.subTest(package=package, lock=lock):
                put(self.project, "package.json", json.dumps(package))
                if lock:
                    put(self.project, lock, "Настройки менеджера проекта\n")
                before = snapshot(self.project)
                args = kit.build_parser().parse_args(["install", str(self.project), "--no-mempalace"])
                with patch.object(kit.subprocess, "run") as run, patch.object(kit, "install_base") as base, self.assertRaises(kit.KitError):
                    args.func(args)
                run.assert_not_called()
                base.assert_not_called()
                self.assertEqual(snapshot(self.project), before)
                if lock:
                    (self.project / lock).unlink()

    def test_init_rejects_incomplete_or_forged_success_reports(self):
        cli = fake_package(self.project)
        good = {"root": str(self.project.resolve()), "tools": ["opencode"], "dryRun": True, "errors": [],
                "files": [{"path": path, "action": "install", "applied": False} for path in (SKILL, COMMAND)]}
        mutations = [
            lambda report: report.update(root="/другой-проект"),
            lambda report: report.update(tools=["claude"]),
            lambda report: report.update(dryRun=False),
            lambda report: report.update(files=[]),
            lambda report: report["files"].pop(),
            lambda report: report["files"][1].update(path=SKILL),
            lambda report: report["files"][0].update(path="../SKILL.md"),
            lambda report: report["files"][0].update(action="replace"),
            lambda report: report["files"][0].update(applied=0),
            lambda report: report.update(errors=[{"message": "Скрытая ошибка"}]),
        ]
        before = snapshot(self.project)
        for mutation in mutations:
            report = copy.deepcopy(good)
            mutation(report)
            with self.subTest(report=report), patch.object(kit.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(report), "")):
                with self.assertRaises(kit.KitError):
                    kit.init_spec(self.project, "node", cli, preview=True)
                self.assertEqual(snapshot(self.project), before)

    def test_init_checks_actual_bundled_bytes_and_dry_run_has_no_writes(self):
        cli = fake_package(self.project)
        original_run = self.fake_run
        for preview in (True, False):
            def corrupt(command, **kwargs):
                result = original_run(command, **kwargs)
                put(self.project, SKILL, "Поврежденный загрузчик\n")
                return result

            with self.subTest(preview=preview), patch.object(kit.subprocess, "run", side_effect=corrupt), self.assertRaises(kit.KitError):
                kit.init_spec(self.project, "node", cli, preview=preview)
            (self.project / SKILL).unlink()

    def test_workflow_checks_protocol_package_topic_and_content(self):
        cli = fake_package(self.project)
        invalid = [{}, {**WORKFLOW, "protocolVersion": True}, {**WORKFLOW, "protocolVersion": 2},
                   {**WORKFLOW, "package": {"name": "чужой-пакет", "version": VERSION}},
                   {**WORKFLOW, "package": {"name": "focused-spec", "version": "99.0.0"}},
                   {**WORKFLOW, "topic": "runners"}, {**WORKFLOW, "content": " \n"}, {**WORKFLOW, "content": []}]
        for report in invalid:
            with self.subTest(report=report), patch.object(kit.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(report), "")):
                with self.assertRaises(kit.KitError):
                    kit.check_spec_workflow(self.project, "node", cli)

    def test_concurrent_backups_never_overwrite(self):
        path = put(self.project, "AGENTS.md", "Исходник\n")
        barrier = threading.Barrier(8)

        def save(_):
            barrier.wait(timeout=10)
            return kit.backup(path)

        with ThreadPoolExecutor(max_workers=8) as executor:
            backups = list(executor.map(save, range(8)))
        self.assertEqual(len(set(backups)), 8)
        self.assertTrue(all(item.read_bytes() == path.read_bytes() for item in backups))

    @unittest.skipIf(os.name == "nt", "Для ссылки в Windows могут потребоваться права")
    def test_symlink_ancestor_blocks_without_writing_external_files(self):
        external = self.project / "external"
        external.mkdir()
        (self.project / ".opencode").symlink_to(external, target_is_directory=True)
        with self.assertRaises(kit.KitError):
            self.install()
        self.assertEqual(list(external.iterdir()), [])


@unittest.skipUnless(os.environ.get("AGENT_KIT_SPEC_INTEGRATION") == "1", "Реальный CLI проверяется через dev/check.sh")
class NativeInitTests(unittest.TestCase):
    def test_real_fresh_latest_second_update_custom_files_and_short_root(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            project = Path(tmp)
            project_text = "# Проектные инструкции\n" + "".join(f"Правило проекта {index}.\n" for index in range(1, 74))
            put(project, "AGENTS.md", project_text)
            kit.upsert_block(project / "AGENTS.md", KIT_ROOT / "template/blocks/agents-kit.md", "agent-kit", "md",
                             {"KIT_VERSION": kit.KIT_VERSION, "WING": "test"})
            root = (project / "AGENTS.md").read_bytes()
            self.assertTrue(root.decode("utf-8").startswith(project_text))
            self.assertLessEqual(len(root.decode("utf-8").splitlines()), 150)
            put(project, ".omo/omo.jsonc", '\ufeff// Мои настройки\r\n{"custom":true}\r\n')
            put(project, "docs/custom.md", "# Документ проекта\n")
            args = kit.build_parser().parse_args(["install", str(project), "--only", "spec"])
            real_run = subprocess.run
            with patch.object(kit.subprocess, "run", wraps=real_run) as run:
                self.assertEqual(args.func(args), 0)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertFalse(any("--replace" in command for command in commands))
            cli = kit.spec_cli(project)
            installed = kit.spec_package(cli)["version"]
            self.assertEqual(installed, kit.resolve_spec_latest(shutil.which("npm")))
            self.assertEqual(kit.load_state(project / ".agent-kit.json")["focusedSpecVersion"], installed)
            self.assertFalse((project / ".focused-spec").exists())
            self.assertEqual((project / "AGENTS.md").read_bytes(), root)
            for relative, content in kit.bundled_spec_assets(cli).items():
                self.assertEqual((project / relative).read_bytes(), content)

            # Имитация предыдущего package-owned bundle без исторических выпусков.
            previous = "Прежняя штатная интеграция\n"
            for relative, asset in kit.SPEC_ASSETS.items():
                put(project, "node_modules/focused-spec/" + asset, previous)
                put(project, relative, previous)
            contracts = kit.spec_contracts(project, shutil.which("node"), cli)
            put(project, ".focused-spec/config.yaml", f"\ufeff# Конфиг проекта\r\nversion: {contracts['configuration']}\r\nrunners: {{}}\r\n")
            put(project, ".focused-spec/runners/custom.ts", "// Мой адаптер\n")
            custom = {relative: (project / relative).read_bytes() for relative in
                      ("AGENTS.md", ".omo/omo.jsonc", "docs/custom.md", ".focused-spec/config.yaml", ".focused-spec/runners/custom.ts")}
            package_before = (project / "package.json").read_bytes()
            lock_before = (project / "package-lock.json").read_bytes()

            def reinstall_bundle(command, **kwargs):
                if project_install(command):
                    # npm заново получает пакет: версия та же, bundled-байты меняются.
                    shutil.rmtree(project / "node_modules/focused-spec")
                return real_run(command, **kwargs)

            with patch.object(kit.subprocess, "run", side_effect=reinstall_bundle):
                self.assertEqual(args.func(args), 0)
            for relative, content in kit.bundled_spec_assets(kit.spec_cli(project)).items():
                path = project / relative
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual([kit.read_text(item) for item in path.parent.glob(path.name + ".bak-*")], [previous])
            self.assertIn(package_before, [item.read_bytes() for item in project.glob("package.json.bak-*")])
            self.assertEqual([item.read_bytes() for item in project.glob("package-lock.json.bak-*")], [lock_before])
            for relative, content in custom.items():
                self.assertEqual((project / relative).read_bytes(), content)
            self.assertEqual(kit.load_state(project / "package.json")["devDependencies"]["focused-spec"], "latest")
            lock = kit.load_state(project / "package-lock.json")["packages"]
            self.assertEqual(lock[""]["devDependencies"]["focused-spec"], "latest")
            self.assertEqual(lock["node_modules/focused-spec"]["version"], installed)

            before = snapshot(project)
            dry_args = kit.build_parser().parse_args(["install", str(project), "--only", "spec", "--dry-run"])
            try:
                with patch.object(kit.subprocess, "run", wraps=real_run) as run:
                    self.assertEqual(dry_args.func(dry_args), 0)
                self.assertFalse(any(Path(call.args[0][0]).name in ("npm", "npm.cmd") for call in run.call_args_list))
            finally:
                kit.DRY_RUN = False
            self.assertEqual(snapshot(project), before)
            self.assertEqual(args.func(args), 0)
            self.assertEqual(without_backups(snapshot(project)), without_backups(before))

    def test_real_cli_dry_run_on_missing_target_has_no_writes(self):
        cli = KIT_ROOT / "dev/node_modules/focused-spec/dist/cli.js"
        self.assertTrue(cli.is_file())
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            target = Path(tmp) / "new-project"
            kit.DRY_RUN = True
            try:
                with patch.object(kit, "spec_cli", return_value=cli):
                    kit.install_spec(target, check=True)
            finally:
                kit.DRY_RUN = False
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
