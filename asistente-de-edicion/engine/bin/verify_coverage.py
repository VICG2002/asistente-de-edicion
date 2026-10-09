#!/usr/bin/env python3
"""Verificador de cobertura del pipeline por sector.

Para cada sector del proyecto calcula el porcentaje de clips que recibieron cada
tipo de procesamiento (clip_segments, curaduria, descripcion LLM, preguntas,
sync, metadata payload). Falla con exit code 1 si algun sector queda debajo del
umbral.

El orquestador `run_full_pipeline.sh` lo corre al final como gate. Tambien se
puede correr a mano para auditar estado actual.

`--project-prefix` es OBLIGATORIO y no tiene default. Lo tuvo
("ESCALANDO MEXICO/") y ese fue el defecto mas caro del motor: en cualquier otro
proyecto el filtro no casaba con nada, este verificador reportaba cero sectores y
salia con exit 0 dando el proyecto por bueno. Asi quedo FANTASTICO COMICS a
medias sin que nadie se enterara. Un verificador que no verifica nada y dice que
todo esta bien es peor que no tener verificador.

Uso:
    # proyecto plano (las carpetas de primer nivel son los sectores)
    python3 bin/verify_coverage.py --root <disco> --project-prefix ''
    # proyecto dentro de una carpeta raiz
    python3 bin/verify_coverage.py --root <disco> --project-prefix 'MI PROYECTO/'
    python3 bin/verify_coverage.py --root <disco> --project-prefix '' --report-only
"""

from __future__ import annotations

import argparse
import glob
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.guards import assert_selected, exigir_prefix  # noqa: E402
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resoluble: {root_arg}")


# Sectores que se esperan procesados. Sectores con < 10 clips o sin entrevistas
# (ej. "imagenes de soporte", "Drone La Bufa") se omiten via SECTOR_MIN_CLIPS.
SECTOR_MIN_CLIPS = 10

# Tipos de cobertura que verificamos. Cada uno tiene su query SQL.
# Formato: (etiqueta, descripcion_query, min_coverage_pct_si_aplica)
COVERAGE_CHECKS = [
    ("clip_segments", "tramos multi-senal (base de la curaduria)", 0.50),
    ("clip_curated_segments", "tramos curados", 0.10),
    ("clip_curated_segments_with_llm", "tramos curados con descripcion LLM", 0.10),
    ("question_segments_video", "preguntas en entrevistas (video)", 0.0),
    ("audio_sync_pairs", "pares video<->audio externo", 0.0),
    ("clip_metadata_payload", "metadata payload pre-armada", 0.50),
]


def fetch_coverage(conn, sector_rel_like: str) -> dict[str, tuple[int, int]]:
    """Devuelve dict: tabla -> (clips_con_dato, clips_video_totales)."""
    out = {}
    total = conn.execute(
        "SELECT COUNT(*) FROM clips "
        "WHERE file_kind='video' AND index_status='ok' AND rel_path LIKE ?",
        (sector_rel_like,)
    ).fetchone()[0]
    if total == 0:
        return {}

    queries = {
        "clip_segments": (
            "SELECT COUNT(DISTINCT cs.clip_id) FROM clip_segments cs "
            "JOIN clips c ON c.id=cs.clip_id "
            "WHERE c.rel_path LIKE ? AND c.file_kind='video'"
        ),
        "clip_curated_segments": (
            "SELECT COUNT(DISTINCT cs.clip_id) FROM clip_curated_segments cs "
            "JOIN clips c ON c.id=cs.clip_id "
            "WHERE c.rel_path LIKE ? AND c.file_kind='video'"
        ),
        "clip_curated_segments_with_llm": (
            "SELECT COUNT(DISTINCT cs.clip_id) FROM clip_curated_segments cs "
            "JOIN clips c ON c.id=cs.clip_id "
            "WHERE c.rel_path LIKE ? AND c.file_kind='video' "
            "AND cs.full_text IS NOT NULL AND cs.full_text <> ''"
        ),
        "question_segments_video": (
            "SELECT COUNT(DISTINCT q.clip_id) FROM question_segments q "
            "JOIN clips c ON c.id=q.clip_id "
            "WHERE c.rel_path LIKE ? AND c.file_kind='video' AND q.seg_index < 1000"
        ),
        "audio_sync_pairs": (
            "SELECT COUNT(DISTINCT sp.video_clip_id) FROM audio_sync_pairs sp "
            "JOIN clips c ON c.id=sp.video_clip_id "
            "WHERE c.rel_path LIKE ? AND c.file_kind='video'"
        ),
        "clip_metadata_payload": (
            "SELECT COUNT(DISTINCT p.clip_id) FROM clip_metadata_payload p "
            "JOIN clips c ON c.id=p.clip_id "
            "WHERE c.rel_path LIKE ? AND c.file_kind='video'"
        ),
    }
    for k, q in queries.items():
        try:
            v = conn.execute(q, (sector_rel_like,)).fetchone()[0]
        except sqlite3.OperationalError:
            v = 0  # tabla no existe aun
        out[k] = (v, total)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--min-coverage", type=float, default=None,
                    help="Umbral global (0-1). Si se pasa, sobrescribe los umbrales "
                         "individuales de cada tipo de cobertura.")
    ap.add_argument("--report-only", action="store_true",
                    help="No fallar — solo imprimir la tabla.")
    ap.add_argument("--project-prefix", default=None,
                    help="OBLIGATORIO. rel_path prefix del proyecto. Pasar '' "
                         "para proyecto plano (sectores = carpetas de primer "
                         "nivel). No hay default a proposito: un default "
                         "heredado deja el proyecto sin verificar en silencio.")
    args = ap.parse_args()

    if args.project_prefix is None:
        sys.exit(exigir_prefix("verify_coverage"))

    root = resolve_root(args.root)
    db_path = root / ".cinema_assistant" / "manifest.sqlite"
    if not db_path.exists():
        sys.exit(f"Manifest no encontrado: {db_path}")

    conn = manifest.conectar(str(db_path))

    pfx = args.project_prefix
    if pfx:
        n0 = len(pfx) + 1
        sectors = [r[0] for r in conn.execute(
            f"SELECT DISTINCT substr(rel_path, {n0}, "
            f"instr(substr(rel_path,{n0}),'/')-1) "
            "FROM clips WHERE rel_path LIKE ? || '%' AND file_kind='video' "
            "AND index_status='ok' ORDER BY 1", (pfx,)
        )]
        like_for = lambda s: f"{pfx}{s}/%"
    else:
        # Proyecto plano: sectores = carpetas de primer nivel (parent_folder)
        sectors = [r[0] for r in conn.execute(
            "SELECT DISTINCT parent_folder FROM clips WHERE file_kind='video' "
            "AND index_status='ok' AND parent_folder != '' ORDER BY 1"
        )]
        like_for = lambda s: f"{s}/%"

    # Guard: cero sectores es un BUG DE SELECCION, no "no habia nada que
    # verificar". Sin esto el verificador seguia adelante con la lista vacia,
    # imprimia una tabla sin filas y devolvia exit 0 — el fallo que dejo
    # FANTASTICO COMICS a medias.
    assert_selected(sectors, "sectores del proyecto", filters={
        "--root": str(root), "--project-prefix": pfx,
    }, hint="Si el material cuelga directo del disco, el prefix correcto es ''")

    # Filtrar sectores con muy poco material (no son entrevistas/escaladas reales)
    real_sectors = []
    for s in sectors:
        like = like_for(s)
        total = conn.execute(
            "SELECT COUNT(*) FROM clips WHERE file_kind='video' AND index_status='ok' "
            "AND rel_path LIKE ?", (like,)
        ).fetchone()[0]
        if total >= SECTOR_MIN_CLIPS:
            real_sectors.append((s, like, total))

    if not real_sectors:
        # OJO: esto NO es "nada que verificar". Es que hay sectores pero ninguno
        # llega al minimo de clips — en un proyecto con material eso es un bug de
        # seleccion. Antes devolvia 0 con --report-only, y como el arranque del
        # skill corre justo con --report-only, el proyecto se daba por bueno.
        # --report-only silencia los fallos de COBERTURA, no los de SELECCION.
        print(f"\n{'=' * 68}\n"
              f"TRABAJO CERO: {len(sectors)} sector(es) encontrados, ninguno con "
              f">= {SECTOR_MIN_CLIPS} clips.\n"
              f"{'=' * 68}\n"
              f"  --project-prefix = {pfx!r}\n"
              f"  sectores vistos  = {', '.join(map(str, sectors[:8]))}"
              f"{' ...' if len(sectors) > 8 else ''}\n"
              "Si el proyecto tiene material, esto es un BUG DE SELECCION: el\n"
              "prefix o la forma del manifest no coinciden con lo que hay en\n"
              "disco. Revisa con:\n"
              "  sqlite3 <disco>/.cinema_assistant/manifest.sqlite \\\n"
              "    \"SELECT rel_path FROM clips WHERE file_kind='video' LIMIT 5;\"",
              file=sys.stderr)
        return 3

    # Header
    headers = ["sector", "clips"] + [c[0][:14] for c in COVERAGE_CHECKS]
    widths = [max(len(s), len(headers[0])) for s, _, _ in real_sectors]
    sector_w = max(max(widths), len(headers[0])) + 1
    print()
    print(f"{'sector':<{sector_w}} {'clips':>6} | " +
          " ".join(f"{h[:14]:>14}" for h in headers[2:]))
    print("-" * (sector_w + 8 + 16 * len(COVERAGE_CHECKS)))

    fails = []
    for sector, like, total in real_sectors:
        cov = fetch_coverage(conn, like)
        cells = []
        for chk_key, _desc, default_min in COVERAGE_CHECKS:
            min_pct = args.min_coverage if args.min_coverage is not None else default_min
            val, _ = cov.get(chk_key, (0, total))
            pct = val / total if total else 0.0
            mark = "OK " if pct >= min_pct else "FAIL"
            cells.append(f"{val:>4}/{total:<4} {mark}")
            if pct < min_pct:
                fails.append((sector, chk_key, val, total, pct, min_pct))
        print(f"{sector:<{sector_w}} {total:>6} | " +
              " ".join(f"{c:>14}" for c in cells))

    print()
    if fails:
        print(f"FAIL: {len(fails)} sector/check(s) por debajo del umbral:")
        for s, k, v, t, p, m in fails:
            print(f"  - {s} / {k}: {v}/{t} ({p*100:.1f}%) < umbral {m*100:.0f}%")
        if args.report_only:
            print("(--report-only: no fallando)")
            return 0
        return 1
    print("PASS: todos los sectores estan por encima del umbral en todos los chequeos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
