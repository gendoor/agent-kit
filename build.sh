#!/bin/sh
# Сборка установочных архивов agent-kit: dist/agent-kit-<версия>.tar.gz и .zip
# (содержимое одинаковое; zip удобнее распаковывать в Windows).
#
#   ./build.sh                          обычные архивы
#   ./build.sh --with-wheels <платф.>   архив с wheels/ для офлайн-установки,
#                                       платформа: linux | windows

set -eu

KIT_DIR=$(cd "$(dirname "$0")" && pwd)
VERSION=$(sed -n 's/^KIT_VERSION = "\(.*\)"/\1/p' "$KIT_DIR/lib/kit.py")
NAME="agent-kit-$VERSION"
PLATFORM=""
if [ "${1:-}" = "--with-wheels" ]; then
  PLATFORM="${2:?укажите платформу: linux или windows}"
fi

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME"
cp -r "$KIT_DIR/setup.sh" "$KIT_DIR/setup.cmd" "$KIT_DIR/install.sh" "$KIT_DIR/install.cmd" "$KIT_DIR/README.md" \
      "$KIT_DIR/lib" "$KIT_DIR/mempalace" "$KIT_DIR/template" "$STAGE/$NAME/"
find "$STAGE/$NAME" -name '__pycache__' -prune -exec rm -rf {} +
find "$STAGE/$NAME" -name '*.bak-*' -delete

OUT="$NAME"
if [ -n "$PLATFORM" ]; then
  case "$PLATFORM" in
    linux) PIP_PLATFORM="manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --platform linux_x86_64" ;;
    windows) PIP_PLATFORM="win_amd64" ;;
    *) echo "ошибка: платформа linux или windows" >&2; exit 1 ;;
  esac
  python3 -m venv "$STAGE/venv"
  "$STAGE/venv/bin/python" -m pip install --quiet --disable-pip-version-check packaging
  # pip download с --platform не вычисляет маркеры под целевую ОС:
  # отбираем строки lock-файла по маркерам платформы заранее.
  "$STAGE/venv/bin/python" - "$PLATFORM" "$KIT_DIR/mempalace/requirements.lock" "$STAGE/req.txt" <<'PY'
import sys
from packaging.requirements import Requirement
platform, src, dst = sys.argv[1:]
env = {"python_version": "3.12", "python_full_version": "3.12.0",
       "platform_python_implementation": "CPython", "implementation_name": "cpython",
       "platform_machine": "AMD64" if platform == "windows" else "x86_64",
       "sys_platform": "win32" if platform == "windows" else "linux",
       "os_name": "nt" if platform == "windows" else "posix",
       "platform_system": "Windows" if platform == "windows" else "Linux"}
out = []
for line in open(src, encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    req = Requirement(line)
    if req.marker is None or req.marker.evaluate(env):
        out.append(f"{req.name}{req.specifier}")
open(dst, "w", encoding="utf-8").write("\n".join(out) + "\n")
print(f"пакетов для {platform}: {len(out)}")
PY
  # shellcheck disable=SC2086
  "$STAGE/venv/bin/python" -m pip download --quiet --disable-pip-version-check \
    --only-binary=:all: --no-deps --python-version 3.12 --implementation cp \
    --platform $PIP_PLATFORM -d "$STAGE/$NAME/wheels" -r "$STAGE/req.txt"
  OUT="$NAME-offline-$PLATFORM-py312"
fi

mkdir -p "$KIT_DIR/dist"
rm -f "$KIT_DIR/dist/$OUT.tar.gz" "$KIT_DIR/dist/$OUT.zip"
COPYFILE_DISABLE=1 tar -C "$STAGE" -czf "$KIT_DIR/dist/$OUT.tar.gz" "$NAME"
python3 - "$STAGE" "$NAME" "$KIT_DIR/dist/$OUT.zip" <<'PY'
import os, sys, zipfile
stage, name, out = sys.argv[1:]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(os.path.join(stage, name)):
        dirs.sort()
        for file in sorted(files):
            path = os.path.join(root, file)
            info = zipfile.ZipInfo.from_file(path, os.path.relpath(path, stage))
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as fh:
                zf.writestr(info, fh.read())
PY
for f in "$KIT_DIR/dist/$OUT.tar.gz" "$KIT_DIR/dist/$OUT.zip"; do
  echo "$f ($(du -h "$f" | cut -f1))"
done
