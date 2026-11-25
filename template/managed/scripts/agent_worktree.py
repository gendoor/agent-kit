#!/usr/bin/env python3
"""Управление изолированными git worktree для параллельных субагентов.

Каждый write-субагент работает в собственном worktree (.worktrees/<slug>) на
собственной ветке (agent/<slug>), чтобы исключить гонки записей в общей
кодовой базе. Главный агент после завершения делегаций выполняет
контролируемый merge каждой ветки в основную.

Команды:
  create <slug> [--from <ref>]  создать worktree и ветку agent/<slug>
  list                          показать активные agent-worktree
  diff <slug>                   показать diff ветки agent/<slug> к базе
  merge <slug>                  слить agent/<slug> в текущую ветку (--no-ff)
  remove <slug> [--force]       удалить worktree и ветку

Все команды выполняются из основного worktree. Запуск:
scripts/agent-worktree (Linux, macOS, WSL) или scripts\\agent-worktree.cmd (Windows).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

WORKTREES_DIR = ".worktrees"
BRANCH_PREFIX = "agent"


class WorktreeError(Exception):
    """Ошибка с сообщением для пользователя."""


def git(*args: str, check: bool = True, capture: bool = True) -> str:
    result = subprocess.run(["git", *args], text=True, encoding="utf-8", errors="replace",
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if check and result.returncode != 0:
        detail = (result.stderr or "").strip() if capture else ""
        raise WorktreeError(f"git {' '.join(args)}: {detail or f'код {result.returncode}'}")
    return (result.stdout or "").strip() if capture else ""


def git_ok(*args: str) -> bool:
    return subprocess.run(["git", *args], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def repo_root() -> Path:
    if not git_ok("rev-parse", "--show-toplevel"):
        raise WorktreeError("не git-репозиторий")
    return Path(git("rev-parse", "--show-toplevel"))


def require_main_worktree() -> Path:
    root = repo_root()
    # Пути git-dir и git-common-dir могут быть относительными к текущему каталогу.
    git_dir = Path(git("rev-parse", "--git-dir")).resolve()
    common = Path(git("rev-parse", "--git-common-dir")).resolve()
    if git_dir != common:
        raise WorktreeError(f"команду нужно выполнять из основного worktree, а не из {root}")
    return root


def validate_slug(slug: str | None) -> str:
    if not slug:
        raise WorktreeError("не указан slug")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,63}", slug):
        raise WorktreeError(f"недопустимый slug '{slug}' (разрешено: [a-z0-9._-], 2-64 символа, "
                            "начинается с буквы/цифры)")
    return slug


def branch_exists(branch: str) -> bool:
    return git_ok("show-ref", "--quiet", f"refs/heads/{branch}")


def cmd_create(args: list) -> None:
    slug = validate_slug(args[0] if args else None)
    from_ref = "HEAD"
    rest = args[1:]
    while rest:
        if rest[0] == "--from" and len(rest) > 1:
            from_ref, rest = rest[1], rest[2:]
        else:
            raise WorktreeError(f"неизвестный аргумент: {rest[0]}")
    root = require_main_worktree()
    branch = f"{BRANCH_PREFIX}/{slug}"
    path = root / WORKTREES_DIR / slug
    if path.exists():
        raise WorktreeError(f"worktree уже существует: {path}")
    if branch_exists(branch):
        raise WorktreeError(f"ветка уже существует: {branch}")
    path.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-b", branch, str(path), from_ref)
    base = git("rev-parse", "--short", from_ref)
    print(f"создан worktree: {path} (ветка {branch}, база {base})")
    print(f"передайте субагенту рабочий каталог: {path}")
    print(f"основной checkout (возможное место установки CLI): {root}")
    print("перед делегацией проверьте запуск инструментов из worktree по навыку agent-kit-worktrees")


def cmd_list(_args: list) -> None:
    worktree = None
    for line in git("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            worktree = line[len("worktree "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):].removeprefix("refs/heads/")
            if branch.startswith(f"{BRANCH_PREFIX}/"):
                print(f"{worktree:<50} {branch}")


def cmd_diff(args: list) -> None:
    slug = validate_slug(args[0] if args else None)
    branch = f"{BRANCH_PREFIX}/{slug}"
    if not branch_exists(branch):
        raise WorktreeError(f"ветка не найдена: {branch}")
    base = git("merge-base", "HEAD", branch)
    git("diff", f"{base}..{branch}", capture=False)


def cmd_merge(args: list) -> None:
    slug = validate_slug(args[0] if args else None)
    require_main_worktree()
    branch = f"{BRANCH_PREFIX}/{slug}"
    if not branch_exists(branch):
        raise WorktreeError(f"ветка не найдена: {branch}")
    # Контролируемый merge: основная копия должна быть чистой, чтобы конфликт
    # можно было разобрать или откатить без потерь.
    if git("status", "--porcelain"):
        raise WorktreeError("рабочее дерево не чистое: зафиксируйте или отложите изменения перед merge")
    result = subprocess.run(["git", "merge", "--no-ff", "--no-edit", branch])
    if result.returncode != 0:
        print("конфликт merge: разрешите конфликты и завершите 'git merge --continue',", file=sys.stderr)
        print("либо откатите 'git merge --abort'", file=sys.stderr)
        raise SystemExit(1)
    current = git("rev-parse", "--abbrev-ref", "HEAD")
    print(f"ветка {branch} слита в {current}")
    print(f"после проверки удалите worktree: scripts/agent-worktree remove {slug}")


def cmd_remove(args: list) -> None:
    slug = validate_slug(args[0] if args else None)
    force = False
    for arg in args[1:]:
        if arg == "--force":
            force = True
        else:
            raise WorktreeError(f"неизвестный аргумент: {arg}")
    root = require_main_worktree()
    branch = f"{BRANCH_PREFIX}/{slug}"
    path = root / WORKTREES_DIR / slug
    if path.exists():
        cmd = ["worktree", "remove"] + (["--force"] if force else []) + [str(path)]
        if not git_ok(*cmd):
            raise WorktreeError("в worktree есть несохраненные изменения; используйте --force "
                                "для принудительного удаления")
    if branch_exists(branch):
        if not git_ok("branch", "-D" if force else "-d", branch):
            raise WorktreeError(f"ветка {branch} не слита; выполните merge или используйте --force")
    git("worktree", "prune")
    print(f"удален worktree {WORKTREES_DIR}/{slug} и ветка {branch}")


COMMANDS = {"create": cmd_create, "list": cmd_list, "diff": cmd_diff,
            "merge": cmd_merge, "remove": cmd_remove}


def main(argv: list) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0 if argv else 1
    command = COMMANDS.get(argv[0])
    if command is None:
        print(f"ошибка: неизвестная команда: {argv[0]} (см. --help)", file=sys.stderr)
        return 1
    try:
        command(argv[1:])
    except WorktreeError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main(sys.argv[1:]))
