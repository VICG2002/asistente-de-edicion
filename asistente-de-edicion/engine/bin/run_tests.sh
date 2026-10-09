#!/bin/bash
# Suite de pruebas del motor. Sin discos externos, sin Whisper, sin instalar nada.
# Uso: bash bin/run_tests.sh [-v] [tests.test_guards ...]
set -e
cd "$(dirname "$0")/.."
exec python3 -m unittest discover -s tests -t tests "$@"
