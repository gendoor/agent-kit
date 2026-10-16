#!/usr/bin/env python3
"""Установщик agent-kit: OMO, Mempalace, project-memory и правила агентов.

Компоненты: base (OMO, Mempalace, project-memory, правила агентов) и spec
(локальный CLI focused-spec и его интеграция OpenCode).

Работает в Linux, macOS, WSL и Windows. Для запуска самого установщика
достаточно Python 3.9+ со стандартной библиотекой; окружению Mempalace нужен
Python 3.12+ (его установщик находит сам или берет из --python).

Подкоманды:
  wizard    пошаговая установка: спрашивает путь проекта и параметры
  install   установка или обновление обвязки в проекте
  probe     проверка MCP-сервера Mempalace проекта по протоколу stdio
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

KIT_VERSION = "1.4.0"
MEMPALACE_VERSION = "3.9.0"
EMBEDDING_MODEL = "embeddinggemma"
MIN_VENV_PYTHON = (3, 12)
DEFAULT_OMO = "oh-my-openagent@4.19.4"
FOCUSED_SPEC_TAG = "latest"
MIN_NODE = (22, 16)
SPEC_SCRIPTS = {
    "spec:validate": "focused-spec validate",
    "spec:run": "focused-spec run",
}
SPEC_ASSETS = {
    ".opencode/skills/focused-spec/SKILL.md": "skills/focused-spec/SKILL.md",
    ".opencode/commands/focused-spec.md": "skills/commands/opencode/focused-spec.md",
}

KIT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = KIT_DIR / "template"
IS_WINDOWS = os.name == "nt"
STAMP = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")

# Скрипты-обертки для sh: должны оставаться исполняемыми и с LF.
SH_SCRIPTS = ("scripts/agent-worktree", "scripts/mempalace", "scripts/mempalace-mcp")
COMPONENTS = ("base", "spec")
# Запуски MCP из прежних версий комплекта, которые безопасно заменить.
LEGACY_MCP_COMMANDS = (
    [".venv-mempalace/bin/mempalace-mcp", "--palace", ".mempalace/palace", "--backend", "chroma"],
)

DRY_RUN = False


class KitError(Exception):
    """Ошибка установки с понятным пользователю сообщением."""


# ---------------------------------------------------------------- вывод

def log(kind: str, message: str) -> None:
    print(f"  [{kind}] {message}", flush=True)


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


# ---------------------------------------------------------------- файлы

def read_text(path: Path) -> str:
    """Читает UTF-8 без преобразования переводов строк (BOM сохраняется)."""
    return path.read_bytes().decode("utf-8")


def write_text(path: Path, text: str) -> None:
    """Пишет байты как есть: в Windows нет подмены LF на CRLF."""
    if not DRY_RUN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


def newline_of(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def with_newline(text: str, newline: str) -> str:
    text = text.replace("\r\n", "\n")
    return text.replace("\n", newline) if newline != "\n" else text


def backup(path: Path) -> Path:
    """Создает копию эксклюзивно: даже параллельные вызовы не затирают прежнюю."""
    target = path.with_name(f"{path.name}.bak-{STAMP}")
    if not DRY_RUN:
        index = 0
        while True:
            try:
                with target.open("xb") as destination, path.open("rb") as source:
                    shutil.copyfileobj(source, destination)
                shutil.copystat(path, target)
                break
            except FileExistsError:
                index += 1
                target = path.with_name(f"{path.name}.bak-{STAMP}-{index}")
    log("backup", f"{path.name} -> {target.name}")
    return target


def render_text(text: str, variables: dict) -> str:
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    unresolved = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", text)))
    if unresolved:
        raise KitError("не заданы переменные шаблона: " + ", ".join(unresolved))
    return text


def render_tree(src: Path, dst: Path, variables: dict, managed: bool) -> None:
    """Копирует дерево шаблонов с подстановкой {{VAR}}.

    Файлы проекта (managed=False) создаются один раз и дальше не меняются.
    Управляемые файлы обновляются при отличии, с резервной копией.
    """
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if path.is_dir() or "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        if path.name == ".keep-empty":
            if not DRY_RUN:
                (dst / rel.parent).mkdir(parents=True, exist_ok=True)
            continue
        target = dst / rel
        content = render_text(read_text(path), variables).encode("utf-8")
        if target.exists():
            if target.read_bytes() == content:
                log("ok", f"{rel.as_posix()} без изменений")
                continue
            if not managed:
                log("keep", f"{rel.as_posix()} уже существует, не изменяется")
                continue
            backup(target)
            action = "update"
        else:
            action = "create"
        if not DRY_RUN:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if not IS_WINDOWS:
                shutil.copymode(path, target)
        log(action, rel.as_posix())


def upsert_block(path: Path, content: Path, block_id: str, style: str,
                 variables: dict, header: Path | None = None) -> None:
    """Вставляет или обновляет блок между маркерами, остальной текст не трогает."""
    body = render_text(read_text(content), variables).replace("\r\n", "\n").strip("\n")
    if style == "md":
        start, end = f"<!-- {block_id}:start -->", f"<!-- {block_id}:end -->"
    else:
        start, end = f"# >>> {block_id}", f"# <<< {block_id}"
    block = f"{start}\n{body}\n{end}\n"
    if path.exists():
        text = read_text(path)
        nl = newline_of(text)
        block_nl = with_newline(block, nl)
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"(\r?\n)?", re.S)
        if pattern.search(text):
            new_text = pattern.sub(lambda _: block_nl, text, count=1)
            action = "update"
        else:
            stripped = text.rstrip("\r\n")
            new_text = stripped + (nl * 2 if stripped.strip("\ufeff") else "") + block_nl
            action = "append"
        if new_text == text:
            log("ok", f"{path.name}: блок {block_id} актуален")
            return
        backup(path)
    else:
        prefix = ""
        if header is not None:
            prefix = render_text(read_text(header), variables).replace("\r\n", "\n").rstrip("\n") + "\n\n"
        new_text = prefix + block
        action = "create"
    write_text(path, new_text)
    log(action, f"{path.name}: блок {block_id}")


# ---------------------------------------------------------------- opencode.json

def merge_opencode(project: Path, variables: dict, omo: str) -> None:
    template = json.loads(render_text(read_text(TEMPLATE / "opencode.json"), variables))
    if omo == "none":
        template.pop("plugin", None)
    else:
        template["plugin"] = [omo]

    jsonc, path = project / "opencode.jsonc", project / "opencode.json"
    if jsonc.exists():
        log("manual", "найден opencode.jsonc: автоматическое слияние не выполняется, добавьте вручную:")
        print(json.dumps({k: template[k] for k in ("mcp", "plugin") if k in template},
                         ensure_ascii=False, indent=2))
        return
    if not path.exists():
        write_text(path, json.dumps(template, ensure_ascii=False, indent=2) + "\n")
        log("create", "opencode.json")
        return

    raw = read_text(path)
    try:
        config = json.loads(raw.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise KitError(f"opencode.json не разобран как JSON ({exc}); добавьте настройки вручную") from exc

    changed = []
    config.setdefault("$schema", template["$schema"])
    servers = config.setdefault("mcp", {})
    for name, server in template["mcp"].items():
        current = servers.get(name)
        if current is None:
            servers[name] = server
            changed.append(f"mcp.{name}")
        elif current.get("command") in LEGACY_MCP_COMMANDS:
            current["command"] = server["command"]
            env = current.setdefault("environment", {})
            for key, value in server.get("environment", {}).items():
                env.setdefault(key, value)
            current.setdefault("timeout", server.get("timeout"))
            changed.append(f"mcp.{name} (кроссплатформенный запуск)")
        elif current != server:
            log("keep", f"mcp.{name} настроен вручную и отличается от шаблона; не изменяется")
    if "plugin" in template:
        plugins = config.setdefault("plugin", [])
        omo_plugins = [str(p) for p in plugins if re.match(r"^oh-my-(openagent|opencode)(@|$)", str(p))]
        if omo_plugins:
            log("keep", "плагин OMO уже подключен: " + ", ".join(omo_plugins))
        else:
            plugins.append(template["plugin"][0])
            changed.append("plugin")
    if not changed:
        log("ok", "opencode.json актуален")
        return
    backup(path)
    out = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    out = ("\ufeff" if raw.startswith("\ufeff") else "") + with_newline(out, newline_of(raw))
    write_text(path, out)
    log("update", "opencode.json: " + ", ".join(changed))


# ---------------------------------------------------------------- .omo/omo.jsonc

OMO_HEADER = """// Конфигурация OMO проекта: модели агентов и категорий с резервными маршрутами
// (профиль agent-kit) и дополнения ролей. Файл проекта выше по приоритету, чем
// личный ~/.omo/omo.jsonc: при совпадении ключей действуют значения отсюда.
// Провайдеры из маршрутов должны быть подключены в OpenCode (opencode auth login);
// недоступные маршруты замените своими. Повторная установка agent-kit добавляет
// только отсутствующие ключи и не меняет значения проекта.
"""


def strip_jsonc(text: str) -> str:
    """Убирает комментарии // и /* */ вне строк и висячие запятые."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        char = text[i]
        if in_str:
            out.append(char)
            if char == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            in_str = char != '"'
            i += 1
        elif char == '"':
            in_str = True
            out.append(char)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] not in "\r\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise KitError("Незакрытый комментарий в .omo/omo.jsonc")
            out.append(" ")
            i = end + 2
        else:
            out.append(char)
            i += 1
    # Второй проход: убираем висячие запятые только вне строк.
    clean = "".join(out)
    out, i, in_str = [], 0, False
    while i < len(clean):
        char = clean[i]
        if in_str:
            out.append(char)
            if char == "\\" and i + 1 < len(clean):
                out.append(clean[i + 1])
                i += 2
                continue
            in_str = char != '"'
        elif char == '"':
            in_str = True
            out.append(char)
        elif char == ",":
            following = clean[i + 1:].lstrip()
            if not following or following[0] not in "}]":
                out.append(char)
        else:
            out.append(char)
        i += 1
    return "".join(out)


def fill_missing(target: dict, source: dict, path: str = "") -> list:
    """Добавляет в target отсутствующие ключи source; значения target не меняются."""
    added = []
    for key, value in source.items():
        if key not in target:
            target[key] = value
            added.append(path + key)
        elif isinstance(target[key], dict) and isinstance(value, dict):
            added += fill_missing(target[key], value, f"{path}{key}.")
    return added


def merge_omo(project: Path, layers: list) -> None:
    """Сливает слои комплекта в .omo/omo.jsonc с приоритетом существующих значений проекта."""
    path = project / ".omo" / "omo.jsonc"
    if path.exists():
        raw = read_text(path)
        try:
            config = json.loads(strip_jsonc(raw.lstrip("\ufeff")))
        except json.JSONDecodeError as exc:
            raise KitError(f".omo/omo.jsonc не разобран ({exc}); добавьте профиль из template/omo вручную") from exc
        if not isinstance(config, dict):
            raise KitError(".omo/omo.jsonc должен содержать объект JSON")
    else:
        raw, config = "", {}
    added = []
    for layer in layers:
        added += fill_missing(config, json.loads(read_text(layer)))
    if not added:
        log("ok", ".omo/omo.jsonc содержит профиль комплекта")
        return
    ordered = {key: config[key] for key in ("$schema", "[opencode]") if key in config}
    ordered |= {key: value for key, value in config.items() if key not in ordered}
    out = OMO_HEADER + json.dumps(ordered, ensure_ascii=False, indent=2) + "\n"
    if raw:
        backup(path)
        if "//" in raw or "/*" in raw:
            log("warn", ".omo/omo.jsonc: комментарии прежнего файла сохранены только в резервной копии")
        out = ("\ufeff" if raw.startswith("\ufeff") else "") + with_newline(out, newline_of(raw))
    write_text(path, out)
    shown = ", ".join(added[:6]) + (f" и еще {len(added) - 6}" if len(added) > 6 else "")
    log("update" if raw else "create", f".omo/omo.jsonc: {shown}")


# ---------------------------------------------------------------- Mempalace

MEMPALACE_YAML = """wing: {wing}
rooms:
- name: project_memory
  description: Files from project-memory/
  keywords:
  - project_memory
  - project-memory
- name: general
  description: Files that don't fit other rooms
  keywords: []
"""


def write_wing(project: Path, wing: str, name: str) -> None:
    """Файлы крыла без `mempalace init`: он пишет эвристические сущности
    в глобальный ~/.mempalace/known_entities.json."""
    yaml_path = project / "mempalace.yaml"
    if yaml_path.exists():
        text = read_text(yaml_path)
        new_text, count = re.subn(r"(?m)^wing:.*$", f"wing: {wing}", text, count=1)
        if count == 0:
            new_text = f"wing: {wing}\n" + text
        action = "ok" if new_text == text else "update"
    else:
        new_text, action = MEMPALACE_YAML.format(wing=wing), "create"
    if action != "ok":
        write_text(yaml_path, new_text)
    log(action, f"mempalace.yaml: крыло {wing}")
    entities = project / "entities.json"
    if not entities.exists():
        write_text(entities, json.dumps({"people": [], "projects": [name], "topics": []},
                                        ensure_ascii=False, indent=2) + "\n")
        log("create", "entities.json")


def default_venv_dir() -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "agent-kit" / "venvs" / f"mempalace-{MEMPALACE_VERSION}"


def venv_exe(venv: Path, name: str) -> Path:
    return venv / "Scripts" / f"{name}.exe" if IS_WINDOWS else venv / "bin" / name


def venv_version(venv: Path) -> str | None:
    python = venv_exe(venv, "python")
    if not python.exists():
        return None
    result = subprocess.run(
        [str(python), "-c", "import importlib.metadata as m; print(m.version('mempalace'))"],
        capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


PYTHON_CHECK = (
    "import platform, sys; "
    "print(sys.version_info[:2] >= (3, 12)); "
    "print(platform.machine()); "
    "print(sys.executable)"
)


def find_python(explicit: str | None) -> str:
    """Ищет Python 3.12+ для окружения Mempalace."""
    candidates = [[explicit]] if explicit else [[sys.executable]]
    if not explicit:
        if IS_WINDOWS:
            candidates += [["py", f"-3.{minor}"] for minor in (14, 13, 12)]
        else:
            candidates += [[f"python3.{minor}"] for minor in (14, 13, 12)]
    for command in candidates:
        if not os.path.isabs(command[0]) and shutil.which(command[0]) is None:
            continue
        try:
            result = subprocess.run(command + ["-c", PYTHON_CHECK], capture_output=True,
                                    text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        lines = result.stdout.split()
        if result.returncode == 0 and len(lines) >= 3 and lines[0] == "True":
            machine = lines[1].lower()
            if IS_WINDOWS and machine not in ("amd64", "x86_64"):
                raise KitError(f"Windows {lines[1]} не поддерживается: для chromadb есть только сборки x64")
            return lines[2]
    if explicit:
        raise KitError(f"{explicit} не подходит: нужен Python 3.12 или новее")
    hint = "установите Python 3.12+ (python.org, py-launcher)" if IS_WINDOWS else \
        "установите python3.12 и python3.12-venv или укажите --python"
    raise KitError(f"не найден Python 3.12+ для окружения Mempalace: {hint}")


def link_venv(link: Path, target: Path) -> Path | None:
    """Ссылка .venv-mempalace на общее окружение. В Windows без прав на
    символические ссылки создается junction."""
    if link.is_symlink() or link.exists():
        try:
            same = link.resolve() == target.resolve()
        except OSError:
            same = False
        if same:
            log("ok", f".venv-mempalace -> {target}")
        else:
            log("keep", ".venv-mempalace уже существует и не ведет в общее окружение")
        return link
    if DRY_RUN:
        log("dry-run", f".venv-mempalace -> {target}")
        return link
    try:
        os.symlink(target, link, target_is_directory=True)
        log("create", f".venv-mempalace -> {target}")
        return link
    except OSError:
        if not IS_WINDOWS:
            raise
    try:
        import _winapi  # type: ignore[import-not-found]
        _winapi.CreateJunction(str(target), str(link))
        log("create", f".venv-mempalace -> {target} (junction)")
        return link
    except (ImportError, OSError) as exc:
        log("warn", f"ссылка не создана ({exc}); скрипты запуска найдут общее окружение сами")
        return None


def venv_path(project: Path, args: argparse.Namespace) -> Path:
    return project / ".venv-mempalace" if args.venv == "local" else Path(args.venv_dir or default_venv_dir())


def ensure_venv(project: Path, args: argparse.Namespace, python: str | None) -> None:
    venv = venv_path(project, args)
    step(f"Окружение Mempalace {MEMPALACE_VERSION} ({args.venv}): {venv}")
    if python is None:
        log("ok", str(venv))
    elif DRY_RUN:
        log("dry-run", f"создание окружения {venv} ({python})")
    else:
        log("python", python)
        subprocess.run([python, "-m", "venv", str(venv)], check=True)
        pip = [str(venv_exe(venv, "python")), "-m", "pip", "install", "--quiet",
               "--disable-pip-version-check"]
        if args.wheelhouse:
            pip += ["--no-index", "--find-links", str(Path(args.wheelhouse).resolve())]
        pip += ["-r", str(KIT_DIR / "mempalace" / "requirements.lock")]
        if subprocess.run(pip).returncode != 0:
            raise KitError("pip не установил зависимости Mempalace (см. вывод выше)")
        log("create", str(venv))
    if args.venv == "shared":
        link_venv(project / ".venv-mempalace", venv)


# ---------------------------------------------------------------- проверка MCP

def resolve_command(command: list, cwd: Path) -> list:
    """Разрешает команду так же, как cross-spawn в OpenCode: в Windows
    `scripts/x` находит `scripts/x.cmd` по PATHEXT и запускает через cmd."""
    if not IS_WINDOWS:
        return command
    head = cwd / command[0]
    if head.suffix:
        candidates = [head]
    else:
        # Как node-which: имя без точки проверяется только с расширениями PATHEXT,
        # поэтому sh-скрипт без расширения рядом с .cmd не выбирается.
        exts = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
        candidates = [head.with_name(head.name + ext.lower()) for ext in exts if ext]
    for candidate in candidates:
        if candidate.is_file():
            if candidate.suffix.lower() in (".cmd", ".bat"):
                return ["cmd.exe", "/d", "/c", str(candidate)] + command[1:]
            return [str(candidate)] + command[1:]
    return command


def probe(project: Path, wing: str, query: str, seed: str | None, timeout: int = 900) -> None:
    config = json.loads(read_text(project / "opencode.json").lstrip("\ufeff"))
    server = config["mcp"]["mempalace"]
    env = os.environ.copy()
    env.update(server.get("environment", {}))
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    command = resolve_command(server["command"], project)
    proc = subprocess.Popen(command, cwd=project, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def receive(request_id: int) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise KitError("MCP-сервер завершился: " + proc.stderr.read()[-1500:])
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                return message
        raise KitError(f"MCP-сервер не ответил за {timeout} с")

    def call(request_id: int, name: str, arguments: dict) -> dict:
        send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}})
        response = receive(request_id)
        result = response.get("result") or {}
        if "error" in response or result.get("isError"):
            raise KitError(f"{name}: {json.dumps(response, ensure_ascii=False)[:800]}")
        text = "".join(part.get("text", "") for part in result.get("content", []))
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "agent-kit-probe", "version": KIT_VERSION}}})
        info = receive(1)["result"]["serverInfo"]
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {tool["name"] for tool in receive(2)["result"]["tools"]}
        required = {"mempalace_search", "mempalace_add_drawer", "mempalace_checkpoint",
                    "mempalace_diary_write"}
        if required - tools:
            raise KitError("нет инструментов MCP: " + ", ".join(sorted(required - tools)))
        log("ok", f"MCP {info.get('name')} {info.get('version')}: {len(tools)} инструментов")
        if seed:
            result = call(3, "mempalace_add_drawer", {
                "wing": wing, "room": "implementation-overview", "content": seed,
                "added_by": "agent-kit"})
            log("ok", f"первая запись палаты: {result.get('drawer_id', result)}")
        hits = call(4, "mempalace_search", {"query": query, "wing": wing, "limit": 1}).get("results", [])
        if seed and not hits:
            raise KitError("поиск не нашел только что добавленную запись")
        log("ok", f"поиск в крыле {wing}: найдено {len(hits)}")
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------- установка

def git(project: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, check=check)


def load_state(path: Path) -> dict:
    try:
        value = json.loads(read_text(path).lstrip("\ufeff"))
        if not isinstance(value, dict):
            raise KitError(f"{path.name}: требуется объект JSON")
        return value
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise KitError(f"{path.name}: неверный JSON ({exc})") from exc


def slug_wing(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def merge_package_json(project: Path) -> bool:
    """Переводит focused-spec на latest, сохраняя раздел зависимости и проектные скрипты.
    Возвращает True, если package.json изменен."""
    path = project / "package.json"
    if path.exists():
        raw = read_text(path)
        try:
            config = json.loads(raw.lstrip("\ufeff"))
        except json.JSONDecodeError as exc:
            raise KitError(f"package.json не разобран как JSON ({exc}); добавьте focused-spec вручную") from exc
    else:
        raw, config = "", {"private": True}
    changed = []
    sections = [name for name in ("devDependencies", "dependencies")
                if "focused-spec" in config.get(name, {})]
    if not sections:
        config.setdefault("devDependencies", {})["focused-spec"] = FOCUSED_SPEC_TAG
        changed.append(f"focused-spec {FOCUSED_SPEC_TAG}")
    for section in sections:
        current = config[section]["focused-spec"]
        if current != FOCUSED_SPEC_TAG:
            config[section]["focused-spec"] = FOCUSED_SPEC_TAG
            changed.append(f"{section}.focused-spec {current} -> {FOCUSED_SPEC_TAG}")
    scripts = config.setdefault("scripts", {})
    for name, command in SPEC_SCRIPTS.items():
        if name not in scripts:
            scripts[name] = command
            changed.append(f"scripts.{name}")
        elif scripts[name] != command:
            log("keep", f"package.json: scripts.{name} задан проектом, не изменяется")
    if not changed:
        log("ok", "package.json актуален")
        return False
    out = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    if raw:
        backup(path)
        out = ("\ufeff" if raw.startswith("\ufeff") else "") + with_newline(out, newline_of(raw))
    write_text(path, out)
    log("update" if raw else "create", "package.json: " + ", ".join(changed))
    return True


def node_tools() -> tuple[str, str] | None:
    """Пути к node и npm, если Node подходит для focused-spec."""
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        log("warn", "Node.js и npm не найдены: зависимости focused-spec не установлены")
        return None
    result = subprocess.run([node, "-p", "process.versions.node"], capture_output=True, text=True)
    version = tuple(int(part) for part in re.findall(r"\d+", result.stdout)[:2])
    if result.returncode != 0 or version < MIN_NODE:
        log("warn", f"Node.js {result.stdout.strip() or '?'} старее {'.'.join(map(str, MIN_NODE))}: "
                    "focused-spec не запустится")
        return None
    return node, npm


def safe_spec_path(project: Path, relative: str) -> Path:
    """Не разрешает установщику писать через ссылки или вместо каталогов."""
    path = project
    parts = Path(relative).parts
    for index, part in enumerate(parts):
        path = path / part
        if path.is_symlink():
            raise KitError(f"{relative}: символическая ссылка; требуется ручная проверка")
        if path.exists() and (not path.is_file() if index == len(parts) - 1 else not path.is_dir()):
            raise KitError(f"{relative}: неожиданный тип файла; требуется ручная проверка")
    return path


def spec_config_paths(project: Path, package: dict) -> list[Path]:
    """Читает литеральные --config из npm-скриптов, не выполняя shell-код."""
    paths = [safe_spec_path(project, ".focused-spec/config.yaml")]
    for name, command in package.get("scripts", {}).items():
        if not isinstance(command, str) or not (name.startswith("spec") or "focused-spec" in command):
            continue
        if "--config" not in command:
            continue
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError as exc:
            raise KitError(f"scripts.{name}: --config не удалось разобрать; файл сохранен") from exc
        # Изменение cwd и shell-подстановки нельзя разрешать как путь от корня проекта.
        if "\\" in command or any(token in ("cd", "pushd", "--root") or token.startswith("--root=") for token in tokens):
            raise KitError(f"scripts.{name}: --config вместе со сменой корня требует ручной проверки до обновления")
        for index, token in enumerate(tokens):
            if token == "--config":
                value = tokens[index + 1] if index + 1 < len(tokens) else ""
            elif token.startswith("--config="):
                value = token.split("=", 1)[1]
            else:
                continue
            if not value or value.startswith("-") or any(char in value for char in "$`%~;&|()<>\\"):
                raise KitError(f"scripts.{name}: --config должен содержать статический путь внутри проекта")
            path = Path(value)
            if not path.is_absolute():
                path = project / path
            try:
                relative = path.relative_to(project).as_posix()
                path.resolve().relative_to(project.resolve())
            except ValueError as exc:
                raise KitError(f"scripts.{name}: --config вне проекта сохранен; обновление требует ручной проверки") from exc
            path = safe_spec_path(project, relative)
            if not path.is_file():
                raise KitError(f"scripts.{name}: --config {value!r} не найден; обновление остановлено")
            if path not in paths:
                paths.append(path)
    return paths


def spec_preflight(project: Path) -> None:
    """Проверяет пути и менеджер пакетов до любых изменений."""
    for relative in ("package.json", "package-lock.json", "npm-shrinkwrap.json",
                     ".gitignore", ".agent-kit.json",
                     ".opencode/skills/focused-spec/SKILL.md", ".opencode/commands/focused-spec.md"):
        safe_spec_path(project, relative)
    package = load_state(project / "package.json")
    manager = package.get("packageManager", "npm")
    if not isinstance(manager, str) or manager.split("@", 1)[0] != "npm":
        raise KitError(f"package.json: packageManager={manager!r}; spec поддерживает установку только через npm. "
                       "Проект сохранен; согласуйте установку локального focused-spec штатным менеджером проекта")
    competing = [name for name in ("yarn.lock", "pnpm-lock.yaml", "pnpm-lock.yml", "bun.lock", "bun.lockb")
                 if (project / name).exists() or (project / name).is_symlink()]
    if competing:
        raise KitError("найдены lock-файлы другого менеджера: " + ", ".join(competing)
                       + "; npm не запускается, проект сохранен. Согласуйте используемый менеджер пакетов")
    for section in ("devDependencies", "dependencies", "scripts"):
        if section in package and not isinstance(package[section], dict):
            raise KitError(f"package.json: {section} должен содержать объект JSON; файл сохранен")
    spec_config_paths(project, package)


def spec_package(cli: Path, expected_version: str | None = None) -> dict:
    package = load_state(cli.parent.parent / "package.json")
    version = package.get("version")
    if (package.get("name") != "focused-spec" or not isinstance(version, str)
            or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version)
            or expected_version is not None and version != expected_version):
        raise KitError("focused-spec: manifest локального CLI не соответствует ожидаемому пакету и разрешенной версии")
    return package


def spec_cli(project: Path, expected_version: str | None = None) -> Path | None:
    root = project / "node_modules/focused-spec"
    package = load_state(root / "package.json")
    if package.get("name") == "focused-spec" and isinstance(package.get("version"), str):
        cli = root / "dist/cli.js"
        if cli.is_file() and (expected_version is None or package["version"] == expected_version):
            spec_package(cli, expected_version)
            return cli
    return None


def bundled_spec_assets(cli: Path, expected_version: str | None = None) -> dict[str, bytes]:
    root = cli.parent.parent
    spec_package(cli, expected_version)
    assets = {}
    for relative, asset in SPEC_ASSETS.items():
        try:
            content = (root / asset).read_bytes()
            if not content.decode("utf-8").strip():
                raise ValueError("пустой ресурс")
        except (OSError, ValueError) as exc:
            raise KitError(f"focused-spec: отсутствует или поврежден комплектный ресурс {asset}") from exc
        assets[relative] = content
    return assets


def init_spec(project: Path, node: str, cli: Path, preview: bool,
              allowed_conflicts: set[str] | None = None) -> dict:
    assets = bundled_spec_assets(cli)
    before = {}
    for relative in assets:
        path = safe_spec_path(project, relative)
        before[relative] = path.read_bytes() if path.exists() else None
    command = [node, str(cli), "init", "--tools", "opencode", "--json"]
    if preview:
        command.append("--dry-run")
    result = subprocess.run(command, cwd=project, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KitError("focused-spec init: не получен JSON-отчет\n" + (result.stdout + result.stderr)[-2000:]) from exc
    check_init_report(report, result.returncode, project, preview, assets, before, allowed_conflicts or set())
    return report


def check_init_report(report: dict, code: int, project: Path, preview: bool,
                      assets: dict[str, bytes], before: dict, allowed_conflicts: set[str]) -> None:
    """Проверяет весь контракт init и реальные байты, а не только успешный код."""
    def invalid(detail: str) -> None:
        raise KitError("focused-spec init: неподтвержденная интеграция; " + detail)

    if (not isinstance(report, dict) or report.get("root") != str(project.resolve())
            or report.get("tools") != ["opencode"] or report.get("dryRun") is not preview
            or not isinstance(report.get("files"), list) or not isinstance(report.get("errors"), list)):
        invalid("неверные root, tools, dryRun или структура JSON")
    files = report["files"]
    if len(files) != len(assets):
        invalid("ожидаются ровно два выбранных файла OpenCode")
    seen, conflicts = set(), set()
    for file in files:
        if not isinstance(file, dict) or set(file) != {"path", "action", "applied"}:
            invalid("неверная структура записи файла")
        relative = file["path"]
        if not isinstance(relative, str) or relative not in assets or relative in seen:
            invalid("неожиданный, повторный или небезопасный путь файла")
        seen.add(relative)
        expected = "install" if before[relative] is None else "unchanged" if before[relative] == assets[relative] else "conflict"
        if file["action"] != expected:
            invalid(f"{relative}: действие не соответствует состоянию файла до init")
        if expected == "conflict":
            conflicts.add(relative)
        applied = not preview and expected == "install"
        if file["applied"] is not applied:
            invalid(f"{relative}: неверный applied")
        path = safe_spec_path(project, relative)
        actual = path.read_bytes() if path.exists() else None
        if actual != (before[relative] if preview else assets[relative]):
            invalid(f"{relative}: изменен при dry-run или не совпадает с bundled asset")
    if conflicts:
        expected_errors = [{"path": path, "message": "custom integration differs; preserve it or explicitly use --replace"}
                           for path in conflicts]
        errors = report["errors"]
        if (not preview or conflicts - allowed_conflicts or code != 1 or len(errors) != len(expected_errors)
                or any(error not in expected_errors for error in errors)
                or any(error not in errors for error in expected_errors)):
            invalid("конфликт пользовательской интеграции: " + json.dumps(errors, ensure_ascii=False))
    elif code != 0 or report["errors"]:
        invalid("неуспешный отчет CLI: " + json.dumps(report["errors"], ensure_ascii=False))


def resolve_spec_latest(npm: str) -> str:
    result = subprocess.run([npm, "view", "focused-spec@latest", "version", "--json"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        version = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KitError("npm view: не удалось разрешить точную версию focused-spec@latest") from exc
    if result.returncode != 0 or not isinstance(version, str) or not re.fullmatch(
            r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version):
        raise KitError("npm view: не получена точная версия focused-spec@latest")
    return version


def spec_contracts(project: Path, node: str, cli: Path) -> dict:
    package = spec_package(cli)
    result = subprocess.run([node, str(cli), "version", "--json"], cwd=project,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KitError("focused-spec version: не получен JSON контрактов для проверки совместимости") from exc
    if (result.returncode != 0 or not isinstance(report, dict)
            or report.get("package") != {"name": "focused-spec", "version": package["version"]}
            or report.get("executable") != str(cli.resolve()) or not isinstance(report.get("contracts"), dict)):
        raise KitError("focused-spec version: неверная идентификация CLI или его контрактов")
    contracts = report["contracts"]
    if (type(contracts.get("guidance")) is not int or contracts["guidance"] != 1
            or any(type(contracts.get(key)) is not int or contracts[key] < 1
                                            for key in ("configuration", "format", "runner"))):
        raise KitError("focused-spec: локальный CLI не поддерживает требуемый протокол инструкций/контрактов")
    return contracts


def check_spec_compatibility(project: Path, node: str, cli: Path) -> None:
    contracts = spec_contracts(project, node, cli)
    active = False
    for path in spec_config_paths(project, load_state(project / "package.json")):
        if not path.exists():
            continue
        active = True
        # Проверяем только корневой скаляр: вложенный version не скрывает старый контракт.
        lines = [line for line in read_text(path).lstrip("\ufeff").splitlines()
                 if line.strip() and not line.lstrip().startswith("#")
                 and line.strip() not in ("---", "...")]
        indent = min((len(line) - len(line.lstrip(" ")) for line in lines), default=0)
        roots = [line.strip() for line in lines if len(line) - len(line.lstrip(" ")) == indent]
        keys = [line for line in roots
                if re.match(r'''^(?:version|"version"|'version')[ \t]*:''', line)]
        match = re.fullmatch(
            r'''(?:version|"version"|'version')[ \t]*:[ \t]*(\d+)(?:[ \t]+\#.*)?[ \t]*''',
            keys[0]) if len(keys) == 1 else None
        if match is None or int(match.group(1)) != contracts["configuration"]:
            raise KitError(f"focused-spec@latest несовместим с активной конфигурацией {path.relative_to(project)}; "
                           "обновление остановлено до npm install, config и evidence сохранены")
    if not active:
        return
    old_cli = spec_cli(project)
    if old_cli is not None:
        old_contracts = spec_contracts(project, node, old_cli)
        if any(old_contracts[key] != contracts[key] for key in ("configuration", "format", "runner")):
            raise KitError("focused-spec@latest меняет контракты активной конфигурации/адаптеров; "
                           "обновление остановлено, необходима согласованная миграция evidence")


def preview_spec_install(project: Path, node: str, npm: str, allowed: set[str],
                         version: str) -> dict[str, bytes]:
    """Проверяет новый пакет до изменения зависимостей и интеграции проекта."""
    # Точная версия фиксирует результат npm view на время проверки без постоянного пина.
    with tempfile.TemporaryDirectory(prefix="agent-kit-spec-") as directory:
        staging = Path(directory)
        result = subprocess.run([npm, "install", "--prefix", str(staging), "--ignore-scripts",
                                 "--no-save", "--no-package-lock", "--no-audit", "--no-fund",
                                 "--loglevel=error", f"focused-spec@{version}"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        cli = spec_cli(staging, version)
        if result.returncode != 0 or cli is None:
            raise KitError("не удалось подготовить CLI для init --dry-run; проект не изменен\n"
                           + (result.stdout + result.stderr)[-2000:])
        check_spec_compatibility(project, node, cli)
        check_spec_workflow(project, node, cli)
        init_spec(project, node, cli, preview=True, allowed_conflicts=allowed)
        return bundled_spec_assets(cli, version)


def check_spec_workflow(project: Path, node: str, cli: Path) -> None:
    package = spec_package(cli)
    result = subprocess.run([node, str(cli), "instructions", "--protocol", "1", "--topic", "workflow", "--json"],
                            cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise KitError("focused-spec instructions: не получен полный JSON workflow") from exc
    if (result.returncode != 0 or not isinstance(report, dict)
            or set(report) != {"protocolVersion", "package", "topic", "content"}
            or type(report["protocolVersion"]) is not int or report["protocolVersion"] != 1
            or report["package"] != {"name": "focused-spec", "version": package["version"]}
            or report["topic"] != "workflow" or not isinstance(report["content"], str)
            or not report["content"].strip()):
        raise KitError("focused-spec instructions: неверные protocolVersion, package, topic или пустой content")


def owned_spec_integrations(project: Path) -> dict[str, bytes]:
    """Снимает байты штатных файлов до npm, пока прежний пакет еще установлен."""
    owned = {}
    cli = spec_cli(project)
    if cli is not None:
        for relative, asset in SPEC_ASSETS.items():
            source = cli.parent.parent / asset
            target = safe_spec_path(project, relative)
            if source.is_file() and target.is_file():
                content = source.read_bytes()
                if content and target.read_bytes() == content:
                    owned[relative] = content
    return owned


def prepare_spec_install(project: Path) -> tuple[str, str, str, dict[str, bytes], dict[str, bytes]]:
    """Готовит проверенный план latest до записи файлов base или spec."""
    owned = owned_spec_integrations(project)
    tools = node_tools()
    if tools is None:
        raise KitError(f"для focused-spec нужны Node.js {'.'.join(map(str, MIN_NODE))}+ и npm")
    node, npm = tools
    version = resolve_spec_latest(npm)
    log("resolve", f"focused-spec@latest -> {version}")
    assets = preview_spec_install(project, node, npm, set(owned), version)
    return node, npm, version, owned, assets


def install_spec(project: Path, check: bool,
                 prepared: tuple[str, str, str, dict[str, bytes], dict[str, bytes]] | None = None) -> str | None:
    """Устанавливает только локальный CLI и его штатную интеграцию OpenCode."""
    spec_preflight(project)
    step(f"focused-spec {FOCUSED_SPEC_TAG}: локальный CLI и init --tools opencode")
    cli = spec_cli(project)
    if DRY_RUN:
        allowed = set(owned_spec_integrations(project))
        tools = node_tools()
        if cli is not None and tools is not None:
            root = cli.parent.parent
            if all((root / asset).is_file() for asset in SPEC_ASSETS.values()) and project.exists():
                init_spec(project, tools[0], cli, preview=True, allowed_conflicts=allowed)
                log("ok", "focused-spec init --tools opencode --dry-run --json")
            elif all((root / asset).is_file() for asset in SPEC_ASSETS.values()):
                bundled_spec_assets(cli)
                log("dry-run", "цель отсутствует: ресурсы CLI проверены; native init --dry-run возможен после создания каталога")
        else:
            log("dry-run", "локальный CLI недоступен: native init --dry-run будет выполнен при установке")
        log("dry-run", "npm view focused-spec@latest version; проверка пакета; npm install focused-spec@<разрешенная версия>")
        log("dry-run", "точная latest-версия в этом режиме не запрашивается; скачиваний и изменений нет")
        merge_package_json(project)
        return
    node, npm, version, owned, expected_assets = prepared if prepared is not None else prepare_spec_install(project)
    allowed = set(owned)
    for relative, content in owned.items():
        path = safe_spec_path(project, relative)
        if not path.is_file() or path.read_bytes() != content:
            raise KitError(f"{relative}: файл изменился после проверки; автоматическая замена отменена")
        if content != expected_assets[relative]:
            backup(path)
    package_changed = merge_package_json(project)
    manifest = project / "package.json"
    manifest_raw = read_text(manifest)
    if not package_changed:
        backup(manifest)
    for name in ("package-lock.json", "npm-shrinkwrap.json"):
        if (project / name).exists():
            backup(project / name)
    # Не перечитываем плавающий latest между проверкой и установкой. npm сохраняет
    # точный номер в manifest/lock; возвращаем только объявление зависимости к latest.
    try:
        result = subprocess.run([npm, "install", f"focused-spec@{version}", "--save", "--save-exact",
                                 "--no-audit", "--no-fund", "--loglevel=error"], cwd=project)
    finally:
        if read_text(manifest) != manifest_raw:
            write_text(manifest, manifest_raw)
    if result.returncode != 0:
        raise KitError(f"npm install focused-spec завершился с кодом {result.returncode}; прежние интеграции сохранены")
    package = json.loads(manifest_raw.lstrip("\ufeff"))
    for name in ("package-lock.json", "npm-shrinkwrap.json"):
        lock_path = project / name
        if not lock_path.exists():
            continue
        raw = read_text(lock_path)
        lock = load_state(lock_path)
        root = lock.get("packages", {}).get("")
        changed = False
        if isinstance(root, dict):
            for section in ("devDependencies", "dependencies"):
                if "focused-spec" in package.get(section, {}) and isinstance(root.get(section), dict):
                    if root[section].get("focused-spec") != FOCUSED_SPEC_TAG:
                        root[section]["focused-spec"] = FOCUSED_SPEC_TAG
                        changed = True
        if changed:
            out = json.dumps(lock, ensure_ascii=False, indent=2) + "\n"
            write_text(lock_path, ("\ufeff" if raw.startswith("\ufeff") else "") + with_newline(out, newline_of(raw)))
    cli = spec_cli(project, version)
    if cli is None or bundled_spec_assets(cli, version) != expected_assets:
        raise KitError("npm установил другую версию/ресурсы, чем проверенный npm view latest; "
                       "прежние интеграции сохранены, повторите установку после проверки реестра")
    # npm может выполнять проектные хуки: заново проверяем владение файлами после установки.
    spec_preflight(project)
    # Проектные npm-хуки не получают права превратить измененный файл в комплектный.
    for relative, content in owned.items():
        path = safe_spec_path(project, relative)
        if not path.is_file() or path.read_bytes() != content:
            raise KitError(f"{relative}: файл изменился после проверки; автоматическая замена отменена")
    # Первая проверка обязана быть штатным dry-run без --replace.
    init_spec(project, node, cli, preview=True, allowed_conflicts=allowed)
    removed = {}
    for relative, content in owned.items():
        if content != expected_assets[relative]:
            path = project / relative
            removed[relative] = content
            path.unlink()
    try:
        init_spec(project, node, cli, preview=True)
        init_spec(project, node, cli, preview=False)
        if check:
            check_spec_workflow(project, node, cli)
    except (KitError, OSError, KeyboardInterrupt):
        for relative, content in removed.items():
            path = safe_spec_path(project, relative)
            if path.exists():
                backup(path)
            path.write_bytes(content)
        raise
    upsert_block(project / ".gitignore", TEMPLATE / "spec/gitignore-spec.txt", "agent-kit-spec", "hash", {})
    log("ok", "интеграция OpenCode установлена; формат, runner и evidence выбираются при задаче")
    return version


def parse_components(raw: str) -> tuple[str, ...]:
    items = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    unknown = [item for item in items if item not in COMPONENTS]
    if unknown or not items:
        raise KitError(f"неизвестные компоненты: {', '.join(unknown) or raw!r}; допустимо: {', '.join(COMPONENTS)}")
    return items


def cmd_install(args: argparse.Namespace) -> int:
    global DRY_RUN
    DRY_RUN = args.dry_run
    if shutil.which("git") is None:
        raise KitError("нужен git")

    project = Path(args.target).expanduser().resolve()
    if project == KIT_DIR:
        raise KitError("нельзя устанавливать комплект в его собственный каталог")
    if project.exists() and not project.is_dir():
        raise KitError(f"{project} существует и не является каталогом")

    components = parse_components(args.only) if args.only else \
        tuple(c for c in COMPONENTS if not (c == "spec" and args.no_spec))
    base = "base" in components

    state_path = project / ".agent-kit.json"
    state = load_state(state_path)
    if "spec" in components:
        spec_preflight(project)
    name = args.name or state.get("name") or project.name
    wing = args.wing or state.get("wing") or slug_wing(name) or slug_wing(project.name) or "project"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", wing):
        raise KitError(f"недопустимое крыло '{wing}': используйте --wing с [a-z0-9_]")

    today = dt.date.today()
    variables = {
        "PROJECT_NAME": name, "WING": wing, "DATE": today.isoformat(),
        "DATE_HUMAN": f"{today.day} {MONTHS[today.month - 1]} {today.year}",
        "KIT_VERSION": KIT_VERSION, "EMBEDDING_MODEL": EMBEDDING_MODEL,
        "MEMPALACE_VERSION": MEMPALACE_VERSION, "OMO_PLUGIN": args.omo,
    }

    # Python для окружения ищется до любых изменений в проекте.
    venv_python = None
    if base and not args.no_mempalace and venv_version(venv_path(project, args)) != MEMPALACE_VERSION:
        venv_python = find_python(args.python)

    print(f"agent-kit {KIT_VERSION} -> {project}")
    print(f"  проект: {name}, компоненты: {', '.join(components)}"
          + (f", крыло Mempalace: {wing}, OMO: {args.omo}" if base else ""))
    if DRY_RUN:
        print("  режим проверки: изменения не записываются")
    elif not project.exists():
        project.mkdir(parents=True)

    # Native init --dry-run требует существующий cwd, но файлы base еще не созданы.
    prepared_spec = prepare_spec_install(project) if "spec" in components and not DRY_RUN else None
    if base:
        install_base(project, args, variables, venv_python)
    installed_spec_version = None
    if "spec" in components:
        installed_spec_version = install_spec(project, check=not args.skip_probe, prepared=prepared_spec)

    if not DRY_RUN:
        state.setdefault("installedAt", today.isoformat())
        if base:
            state.update(name=name, wing=wing, mempalaceVersion=MEMPALACE_VERSION)
        else:
            state.setdefault("name", name)
        installed = set(state.get("components") or (["base"] if state.get("wing") else []))
        state.update(kitVersion=KIT_VERSION, updatedAt=today.isoformat(),
                      components=[c for c in COMPONENTS if c in installed | set(components)])
        if "spec" in components:
            state["focusedSpecVersion"] = installed_spec_version
        new_state = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        if not state_path.exists() or read_text(state_path) != new_state:
            if state_path.exists():
                backup(state_path)
            write_text(state_path, new_state)

    step("Готово")
    hints = []
    if base:
        if args.omo != "none" and not args.no_omo_profile:
            hints.append("Подключите в OpenCode провайдеров из .omo/omo.jsonc (opencode auth login) "
                         "или замените их маршрутами своих провайдеров.")
        hints += ["Заполните раздел «Проект» в AGENTS.md (вне блоков agent-kit).",
                  f"Запустите opencode в {project}: MCP mempalace и OMO подключатся автоматически.",
                  "Проверка: opencode mcp list"]
    if "spec" in components:
        hints += ["Перезапустите OpenCode и вызовите /focused-spec с задачей по спецификации.",
                   "Формат, runner и конфигурацию агент выбирает по проекту при задаче; "
                   "установка интеграции не доказывает прохождение спецификаций."]
    commit = [".gitignore", ".agent-kit.json"]
    if base:
        commit[:0] = ["AGENTS.md", "opencode.json"]
        commit.append("scripts/")
        commit += [".gitattributes", "project-memory/", ".opencode/commands/", ".omo/omo.jsonc"]
    if "spec" in components:
        commit += ["package.json", "package-lock.json", ".opencode/skills/focused-spec/",
                   ".opencode/commands/focused-spec.md"]
    hints.append("Закоммитьте " + ", ".join(commit) + ".")
    print("  Дальше:\n" + "\n".join(f"  {i}. {hint}" for i, hint in enumerate(hints, 1)))
    return 0


def install_base(project: Path, args: argparse.Namespace, variables: dict, venv_python: str | None) -> None:
    name, wing = variables["PROJECT_NAME"], variables["WING"]
    step("Git-репозиторий")
    inside = project.exists() and git(project, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0
    if inside:
        log("ok", git(project, "rev-parse", "--show-toplevel").stdout.strip())
    else:
        if not DRY_RUN:
            git(project, "init", "-q")
        log("create", "git init (project-memory и agent-worktree требуют Git)")

    step(".gitignore и .gitattributes")
    blocks = TEMPLATE / "blocks"
    upsert_block(project / ".gitignore", blocks / "gitignore.txt", "agent-kit", "hash", variables)
    upsert_block(project / ".gitattributes", blocks / "gitattributes.txt", "agent-kit", "hash", variables)

    step("Файлы проекта (создаются один раз, затем принадлежат проекту)")
    render_tree(TEMPLATE / "project", project, variables, managed=False)

    step("Управляемые файлы комплекта (обновляются с резервной копией)")
    render_tree(TEMPLATE / "managed", project, variables, managed=True)
    if IS_WINDOWS and not DRY_RUN:
        # NTFS не хранит бит исполнения: фиксируем его в индексе Git для Linux.
        result = git(project, "update-index", "--add", "--chmod=+x", *SH_SCRIPTS, check=False)
        if result.returncode == 0:
            log("git", "sh-скрипты добавлены в индекс с правом исполнения")

    step("AGENTS.md")
    upsert_block(project / "AGENTS.md", blocks / "agents-kit.md", "agent-kit", "md", variables,
                 header=blocks / "agents-header.md")

    step("opencode.json (OMO и MCP mempalace)")
    merge_opencode(project, variables, args.omo)

    if args.omo != "none" and not args.no_omo_profile:
        step("Профиль OMO в .omo/omo.jsonc (модели агентов и категорий; значения проекта не меняются)")
        merge_omo(project, [TEMPLATE / "omo" / "profile.json"])

    if not args.no_mempalace:
        ensure_venv(project, args, venv_python)
        step(f"Палата Mempalace (крыло {wing})")
        write_wing(project, wing, name)
        if not args.skip_probe and not DRY_RUN:
            step("Проверка MCP mempalace (первый запуск может скачать модель эмбеддингов)")
            seed = None
            if not (project / ".mempalace" / "palace" / "chroma.sqlite3").exists():
                seed = (f"{variables['DATE_HUMAN']}: проект {name} подключен к Mempalace комплектом "
                        f"agent-kit {KIT_VERSION}. Источник истины — project-memory/ в Git "
                        "(ADR-0001, CHG-0001); палата хранит причины решений, историю и ссылки "
                        "на Git-артефакты.")
            probe(project, wing, "подключение проекта к Mempalace", seed)


def ask(prompt: str, default: str = "") -> str:
    """Вопрос пользователю; пустой ответ дает значение по умолчанию."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError as exc:
        raise KitError("ввод прерван") from exc
    return answer or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask(f"{prompt} ({hint})").lower()
        if not answer:
            return default
        if answer in ("y", "yes", "д", "да"):
            return True
        if answer in ("n", "no", "н", "нет"):
            return False
        print("  ответьте y или n")


def clean_path(raw: str) -> str:
    """Убирает кавычки, которые добавляет перетаскивание папки в терминал."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    return raw


def cmd_wizard(_args: argparse.Namespace) -> int:
    """Пошаговая установка: запрашивает путь проекта и параметры."""
    print(f"agent-kit {KIT_VERSION}: установка агентской обвязки в проект")
    print("OMO, Mempalace, project-memory, focused-spec и правила агентов. Ctrl+C — отмена.\n")
    example = r"C:\work\my-project" if IS_WINDOWS else "~/work/my-project"
    while True:
        raw = clean_path(ask(f"Путь к каталогу проекта (например, {example})"))
        if not raw:
            print("  путь не указан")
            continue
        project = Path(os.path.expandvars(raw)).expanduser().resolve()
        if project == KIT_DIR:
            print("  это каталог самого комплекта; укажите каталог проекта")
            continue
        if project.exists() and not project.is_dir():
            print(f"  {project} существует и не является каталогом")
            continue
        if not project.exists() and not ask_yes(f"  Каталог {project} не существует. Создать?"):
            continue
        break

    state = load_state(project / ".agent-kit.json")
    if state:
        print(f"  В проекте уже установлен agent-kit {state.get('kitVersion', '?')} "
              f"(крыло {state.get('wing')}): будет выполнено обновление.")
        name, wing = state.get("name") or project.name, state.get("wing") or slug_wing(project.name)
    else:
        name = ask("Имя проекта", project.name)
        while True:
            wing = ask("Крыло Mempalace (a-z, 0-9, _)",
                       slug_wing(name) or slug_wing(project.name) or "project")
            if re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", wing):
                break
            print("  допустимы строчные латинские буквы, цифры и _, начиная с буквы или цифры")
    omo = ask("Плагин OMO для opencode.json (none — не подключать)", DEFAULT_OMO)
    local = ask_yes("Отдельное окружение Mempalace внутри проекта (~430 МБ)? "
                    "Нет — одно общее окружение для всех проектов", default=False)

    spec = ask_yes("focused-spec и интеграция OpenCode (нужен Node.js 22.16+)?")
    argv = ["install", str(project), "--name", name, "--wing", wing, "--omo", omo,
            "--venv", "local" if local else "shared"] + ([] if spec else ["--no-spec"])
    wheels = KIT_DIR / "wheels"
    if wheels.is_dir() and any(wheels.glob("*.whl")):
        argv += ["--wheelhouse", str(wheels)]
        print(f"  Найден каталог пакетов {wheels}: установка без обращения к PyPI.")

    print("\nБудет выполнено:")
    print(f"  проект:       {project}")
    print(f"  имя:          {name}")
    print(f"  крыло:        {wing}")
    print(f"  OMO:          {omo}")
    print(f"  окружение:    {'в проекте (.venv-mempalace)' if local else default_venv_dir()}")
    print(f"  focused-spec: {FOCUSED_SPEC_TAG if spec else 'нет'}")
    print("  Существующие файлы проекта не перезаписываются; измененные файлы сохраняются как *.bak-<время>.")
    if not ask_yes("\nПродолжить?"):
        print("Отменено, изменения не вносились.")
        return 0
    print()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def cmd_probe(args: argparse.Namespace) -> int:
    project = Path(args.project).resolve()
    state = load_state(project / ".agent-kit.json")
    wing = args.wing or state.get("wing")
    if not wing:
        raise KitError("не удалось определить крыло: укажите --wing")
    probe(project, wing, args.query, None)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kit.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("wizard", help="пошаговая установка с вопросами")
    p.set_defaults(func=cmd_wizard)

    p = sub.add_parser("install", help="установка обвязки в проект",
                       description="Установка или обновление agent-kit в каталоге проекта. "
                                   "Повторный запуск безопасен: файлы проекта не перезаписываются, "
                                   "управляемые блоки и скрипты обновляются с резервной копией.")
    p.add_argument("target", help="каталог проекта (создается при отсутствии)")
    p.add_argument("--name", help="имя проекта (по умолчанию имя каталога)")
    p.add_argument("--wing", help="крыло Mempalace (по умолчанию имя в snake_case)")
    p.add_argument("--omo", default=DEFAULT_OMO, help=f"плагин OMO (по умолчанию {DEFAULT_OMO}; none — не подключать)")
    p.add_argument("--venv", choices=("shared", "local"), default="shared",
                   help="общее окружение Mempalace для всех проектов или собственное в .venv-mempalace")
    p.add_argument("--venv-dir", help=f"каталог общего окружения (по умолчанию {default_venv_dir()})")
    p.add_argument("--python", help="Python 3.12+ для создания окружения")
    p.add_argument("--wheelhouse", help="офлайн-установка пакетов из каталога wheel-файлов")
    p.add_argument("--no-mempalace", action="store_true", help="не устанавливать Mempalace (только файлы)")
    p.add_argument("--no-omo-profile", action="store_true",
                   help="не добавлять профиль моделей OMO в .omo/omo.jsonc (использовать ~/.omo/omo.jsonc)")
    p.add_argument("--no-spec", action="store_true",
                    help=f"не устанавливать исполняемые спецификации focused-spec {FOCUSED_SPEC_TAG}")
    p.add_argument("--only", metavar="КОМПОНЕНТЫ",
                   help=f"установить только перечисленные компоненты через запятую ({', '.join(COMPONENTS)}); "
                        "например --only spec для проекта с собственной обвязкой")
    p.add_argument("--skip-probe", action="store_true",
                   help="не проверять MCP-сервер и focused-spec после установки")
    p.add_argument("--dry-run", action="store_true", help="показать действия без изменений")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("probe", help="проверка MCP-сервера Mempalace проекта")
    p.add_argument("project")
    p.add_argument("--wing")
    p.add_argument("--query", default="подключение проекта к Mempalace")
    p.set_defaults(func=cmd_probe)
    return parser


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nПрервано пользователем.", file=sys.stderr)
        return 130
    except KitError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"ошибка: команда завершилась с кодом {exc.returncode}: {' '.join(map(str, exc.cmd))}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
