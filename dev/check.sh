#!/bin/sh
# Проверка установщика и штатного init во временных проектах.
# Нужны Node 22.16+, npm и Python 3; пакет хранится только в dev/node_modules.
set -eu
DEV=$(cd "$(dirname "$0")" && pwd)
cd "$DEV"
npm install --silent --no-audit --no-fund --no-save focused-spec@latest
AGENT_KIT_SPEC_INTEGRATION=1 python3 -B -m unittest discover -s "$DEV" -p 'test_kit_*.py'
