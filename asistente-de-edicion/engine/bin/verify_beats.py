#!/usr/bin/env python3
"""Verificador de los beats de entrevista. Garantias, no promesas (playbook §12b).

Comprueba invariantes que, si se rompen, producen markers que enganan al editor:

  1. Toda entrevista con transcript limpio y preguntas tiene al menos una
     PREGUNTA y una RESPUESTA. Si hay preguntas pero cero beats, el derivador
     se salto el clip en silencio.
  2. Cada pregunta tiene su respuesta pareja (mismo q_index) y la respuesta
     empieza DESPUES de la pregunta.
  3. Ningun beat cae fuera del clip.
  4. Ninguna pausa cae dentro de una PREGUNTA — una pausa mientras se formula
     la pregunta no es un punto de corte y confunde.
  5. Los tramos pregunta/respuesta del mismo q_index no se traslapan.
  6. Ningun beat con duracion negativa o cero.

Salida: exit 0 si todo pasa, 1 si hay fallos. Reporta SIEMPRE los numeros,
tambien cuando todo esta bien.

Uso:
    python3 bin/verify_beats.py --root <disco>
    python3 bin/verify_beats.py --root <disco> --verbose
"""

from __future__ import annotations

import argparse
import glob
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import interview_beats as ib  # noqa: E402
from lib import manifest  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--verbose", action="store_true",
                    help="Listar cada fallo, no solo el resumen.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}")

    conn = manifest.conectar(str(db))
    try:
        conn.execute("SELECT 1 FROM interview_beats LIMIT 1")
    except sqlite3.OperationalError:
        print("No existe la tabla interview_beats. Correr antes:")
        print("  python3 bin/derive_interview_beats.py --root " + str(root))
        return 1

    duraciones = {r[0]: float(r[1] or 0) for r in conn.execute(
        "SELECT id, duration_sec FROM clips")}

    beats = defaultdict(list)
    for cid, idx, kind, s, e, conf, src, qi in conn.execute(
            "SELECT clip_id, beat_index, kind, start_sec, end_sec, confidence,"
            " source, q_index FROM interview_beats ORDER BY clip_id, beat_index"):
        beats[cid].append({"idx": idx, "kind": kind, "s": s, "e": e,
                           "conf": conf, "src": src, "qi": qi})

    preguntas_por_clip = Counter()
    try:
        for cid, n in conn.execute(
                "SELECT clip_id, COUNT(*) FROM question_segments GROUP BY clip_id"):
            preguntas_por_clip[cid] = n
    except sqlite3.OperationalError:
        pass

    alucinados = set()
    try:
        alucinados = {r[0] for r in conn.execute(
            "SELECT clip_id FROM transcript_quality WHERE is_hallucinated=1")}
    except sqlite3.OperationalError:
        pass
    tr_dir = root / ".cinema_assistant" / "transcripts"
    con_master = ({int(p.stem.replace("master_", "")) for p in tr_dir.glob("master_*.json")}
                  if tr_dir.exists() else set())
    conn.close()

    fallos = defaultdict(list)

    # 1. clips con preguntas pero sin beats
    for cid, n in preguntas_por_clip.items():
        if cid in alucinados and cid not in con_master:
            continue  # saltado a proposito por el derivador
        if n > 0 and not beats.get(cid):
            fallos["sin_beats"].append(f"clip {cid}: {n} preguntas y 0 beats")

    for cid, bs in beats.items():
        dur = duraciones.get(cid, 0.0)
        preg = {b["qi"]: b for b in bs if b["kind"] == "pregunta" and b["qi"] is not None}
        resp = {b["qi"]: b for b in bs if b["kind"] == "respuesta" and b["qi"] is not None}

        # 2. pregunta sin respuesta pareja y orden correcto
        for qi, p in preg.items():
            r = resp.get(qi)
            if r is None:
                fallos["pregunta_huerfana"].append(f"clip {cid} q{qi}: sin respuesta")
                continue
            if r["s"] < p["s"]:
                fallos["orden_invertido"].append(
                    f"clip {cid} q{qi}: respuesta en {r['s']:.1f}s antes que la "
                    f"pregunta en {p['s']:.1f}s")
            # 5. no se traslapan
            if p["e"] > r["s"] + 0.01:
                fallos["traslape"].append(
                    f"clip {cid} q{qi}: la pregunta acaba en {p['e']:.1f}s pero la "
                    f"respuesta empieza en {r['s']:.1f}s")
        for qi in resp:
            if qi not in preg:
                fallos["respuesta_huerfana"].append(f"clip {cid} q{qi}: sin pregunta")

        for b in bs:
            # 3. dentro del clip
            if dur > 0 and (b["s"] < -0.01 or b["e"] > dur + 0.01):
                fallos["fuera_de_rango"].append(
                    f"clip {cid} beat {b['idx']} ({b['kind']}): "
                    f"{b['s']:.1f}-{b['e']:.1f}s en un clip de {dur:.1f}s")
            # 6. duracion valida
            if b["e"] <= b["s"]:
                fallos["duracion_invalida"].append(
                    f"clip {cid} beat {b['idx']} ({b['kind']}): "
                    f"{b['s']:.1f}-{b['e']:.1f}s")
            if b["kind"] not in ib.KINDS:
                fallos["kind_desconocido"].append(
                    f"clip {cid} beat {b['idx']}: kind='{b['kind']}'")

        # 4. pausas dentro de una pregunta
        for b in bs:
            if b["kind"] != "pausa":
                continue
            for p in preg.values():
                if b["s"] >= p["s"] - 0.01 and b["e"] <= p["e"] + 0.01:
                    fallos["pausa_en_pregunta"].append(
                        f"clip {cid} beat {b['idx']}: pausa {b['s']:.1f}-{b['e']:.1f}s "
                        f"dentro de la pregunta q{p['qi']}")
                    break

    # ---- reporte -----------------------------------------------------
    por_kind = Counter()
    por_metodo = Counter()
    for bs in beats.values():
        for b in bs:
            por_kind[b["kind"]] += 1
            if b["kind"] == "pregunta":
                por_metodo[b["src"]] += 1

    print(f"\nVerificacion de beats — {root.name}")
    print("=" * 62)
    print(f"clips con beats : {len(beats)}")
    print(f"beats totales   : {sum(len(v) for v in beats.values())}")
    for k in ib.KINDS:
        if por_kind[k]:
            print(f"  {k:<10} {por_kind[k]:>6}")
    if por_metodo:
        print("\nSeparacion pregunta/respuesta por metodo:")
        total = sum(por_metodo.values())
        for m, n in por_metodo.most_common():
            print(f"  {m:<12} {n:>5}  ({100*n/total:4.0f}%)")
        estimadas = por_metodo.get("estimado", 0)
        if estimadas:
            print(f"\n  · {estimadas} preguntas con limite ESTIMADO: el marker de "
                  f"respuesta cae\n    en un punto aproximado. Correr "
                  f"build_master_transcripts.py y detect_pauses.py.")

    total_fallos = sum(len(v) for v in fallos.values())
    print("")
    if not total_fallos:
        print("✓ Todas las invariantes se cumplen.")
        return 0

    print(f"✗ {total_fallos} fallo(s):")
    etiquetas = {
        "sin_beats": "clips con preguntas pero sin ningun beat",
        "pregunta_huerfana": "preguntas sin respuesta pareja",
        "respuesta_huerfana": "respuestas sin pregunta",
        "orden_invertido": "respuesta antes que su pregunta",
        "traslape": "pregunta y respuesta traslapadas",
        "fuera_de_rango": "beats fuera del rango del clip",
        "duracion_invalida": "beats de duracion cero o negativa",
        "kind_desconocido": "beats con un tipo que el Lua no sabe pintar",
        "pausa_en_pregunta": "pausas dentro de una pregunta (no son punto de corte)",
    }
    for k, v in sorted(fallos.items(), key=lambda x: -len(x[1])):
        print(f"  {len(v):>5}  {etiquetas.get(k, k)}")
        if args.verbose:
            for linea in v[:20]:
                print(f"         {linea}")
            if len(v) > 20:
                print(f"         ... y {len(v)-20} mas")
    if not args.verbose:
        print("\n  Correr con --verbose para ver cada caso.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
