#!/bin/bash
# Shim de compatibilidad. El orquestador vive ahora en bin/run_pipeline.py.
#
# El de shell se retiro el 2026-08-05 despues de descubrir que llevaba roto
# desde el 31 de julio: llamaba a verify_coverage.py sin el --project-prefix que
# ese dia se hizo obligatorio, asi que salia con exit 1 y, con `set -euo
# pipefail`, moria en su linea 110 ANTES DE INDEXAR UN SOLO AUDIO. Nadie lo
# noto porque ninguna prueba ejecutaba un .sh.
#
# Y no era el unico problema. Tenia cinco hardcodes de ESCALANDO MEXICO
# (invisibles al linter, que solo leia .py), guardas que comparaban contra un
# piso absoluto en vez de medir el crecimiento real, ocho pasos que seguian
# adelante tras fallar —incluido el sync principal— y un solo interprete, por lo
# que ningun paso de identidad podia correr.
#
# Todo eso esta resuelto en run_pipeline.py, que ademas se puede probar:
#   python3 bin/run_pipeline.py --root <disco> --plan-only
#   python3 bin/run_pipeline.py --root <disco> --listar
#
# Este archivo existe solo para no romper un alias o una nota vieja. Se puede
# borrar cuando la doctrina y los apuntes de todos apunten al nuevo.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "run_full_pipeline.sh se retiro: el orquestador es ahora run_pipeline.py." >&2
echo "Reenviando la llamada. Si tienes esto en un alias, actualizalo a:" >&2
echo "  python3 $DIR/run_pipeline.py --root <disco>" >&2
echo "" >&2

if [ $# -eq 0 ]; then
    echo "Uso: run_full_pipeline.sh <disco_root>" >&2
    exit 2
fi

exec python3 "$DIR/run_pipeline.py" --root "$@"
