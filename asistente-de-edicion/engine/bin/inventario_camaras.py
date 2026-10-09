#!/usr/bin/env python3
"""Inventario de camaras del proyecto contra el registro de perfiles.

Para que existe
---------------
La leccion 50 fue: las a6700 de CAMAROGRAFO_1 y CAMAROGRAFO_2 en THE AVALANCHES quedaron sin
transcribir porque `transcribe_clips.py` no conocia ese modelo. El pipeline
corrio, dijo OK, y ese material simplemente no estaba. Una camara nueva no
debe ser un bug silencioso: debe ser un aviso.

Este script agrupa el material por camara y dice, para cada grupo:
  - que perfil lo reconocio (o si cayo al fallback),
  - si lo reconocio solo un perfil GENERICO (red de seguridad, no catalogo),
  - cuantos clips y cuantas horas hay detras.

Los grupos sin perfil propio se escriben como borrador en
`<disco>/.cinema_assistant/camera_profiles.suggested.json` para que el operador
los revise, complete y mueva al registro (del motor o del proyecto).

Uso:
    python3 bin/inventario_camaras.py --root /Volumes/MI_DISCO/Diez50/<proyecto>
    python3 bin/inventario_camaras.py --root <disco> --comparar-legacy
    python3 bin/inventario_camaras.py --root <disco> --sin-sugerencias
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import cameras                                    # noqa: E402
from lib.guards import assert_selected, report_done        # noqa: E402
from lib import manifest  # noqa: E402

# Perfiles que son red de seguridad, no catalogo: si un grupo cae aqui es que
# la camara no esta descrita y conviene darle perfil propio.
GENERICOS = ("sony-generico", "canon-generico", "panasonic-generico",
             "nikon-generico", "mxf-generico")


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def patron(filename: str) -> str:
    """Firma del nombre con los digitos colapsados: C0044.MP4 -> C####.

    Agrupar por los 4 primeros caracteres partia una misma camara en C000,
    C001, C002... segun el numero de clip. Lo que identifica a la camara es la
    FORMA del nombre, no sus digitos.
    """
    stem = Path(filename or "").stem
    out = []
    for ch in stem[:10]:
        out.append("#" if ch.isdigit() else ch.upper())
    # colapsar corridas de # para que C0044 y C044 caigan juntos
    firma = []
    for ch in out:
        if ch == "#" and firma and firma[-1] == "#":
            continue
        firma.append(ch)
    return "".join(firma)


def prefijo_match(filename: str) -> str:
    """Los 4 primeros caracteres — lo que usan las clausulas filename_prefix."""
    return (filename or "")[:4].upper()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True)
    ap.add_argument("--project-prefix", default="",
                    help="Prefijo de rel_path del proyecto. Vacio = todo el manifest.")
    ap.add_argument("--comparar-legacy", action="store_true",
                    help="Muestra que clips cambian de rol respecto a la "
                         "classify_camera() anterior al registro.")
    ap.add_argument("--sin-sugerencias", action="store_true",
                    help="No escribir camera_profiles.suggested.json.")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}")

    reg = cameras.load_profiles(root)
    conn = manifest.conectar(str(db))
    conn.row_factory = sqlite3.Row

    pfx = args.project_prefix
    rows = conn.execute("""
        SELECT id, filename, ext, camera_make, camera_model, codec_name,
               width, height, fps, duration_sec, rel_path
        FROM clips
        WHERE file_kind = 'video' AND index_status = 'ok'
          AND (? = '' OR rel_path LIKE ? || '%')
    """, (pfx, pfx)).fetchall()
    assert_selected(rows, "clips de video en el manifest", filters={
        "--root": str(root), "--project-prefix": pfx or "(todo)",
    }, hint="Si el proyecto es plano, dejar --project-prefix vacio.")

    # ---- agrupar ---------------------------------------------------------
    grupos: dict = {}
    for r in rows:
        p = cameras.classify_row(r, registry=reg)
        clave = (
            (r["camera_make"] or "").strip(),
            (r["camera_model"] or "").strip(),
            patron(r["filename"]),
            (r["codec_name"] or "").strip(),
            f'{r["width"] or 0}x{r["height"] or 0}',
            round(float(r["fps"] or 0), 3),
        )
        g = grupos.setdefault(clave, {"n": 0, "seg": 0.0, "perfil": p,
                                      "ejemplo": r["filename"]})
        g["n"] += 1
        g["seg"] += float(r["duration_sec"] or 0)

    # ---- reporte ---------------------------------------------------------
    print(f"\nInventario de camaras — {root.name}")
    print(f"Registro: {len(reg)} perfiles + fallback '{reg.fallback.id}'")
    print("=" * 96)
    print(f'{"MARCA":<12}{"MODELO":<18}{"PATRON":<12}{"CODEC":<10}{"RESOLUCION":<12}'
          f'{"FPS":>7}{"CLIPS":>7}{"HORAS":>7}  PERFIL')
    print("-" * 96)

    sin_perfil, genericos = [], []
    for clave in sorted(grupos, key=lambda k: -grupos[k]["n"]):
        mk, mo, pf, cod, res, fps = clave
        g = grupos[clave]
        p = g["perfil"]
        es_fallback = (p.id == reg.fallback.id)
        es_generico = p.id in GENERICOS
        marca = "  <-- SIN PERFIL" if es_fallback else ("  <-- generico" if es_generico else "")
        print(f'{mk[:11]:<12}{mo[:17]:<18}{pf[:11]:<12}{cod[:9]:<10}{res:<12}'
              f'{fps:>7.2f}{g["n"]:>7}{g["seg"]/3600:>7.1f}  '
              f'{p.id} [{p.role}]{marca}')
        if es_fallback:
            sin_perfil.append((clave, g))
        elif es_generico:
            genericos.append((clave, g))

    # ---- resumen por rol -------------------------------------------------
    por_rol: dict = {}
    for clave, g in grupos.items():
        por_rol.setdefault(g["perfil"].role, [0, 0.0])
        por_rol[g["perfil"].role][0] += g["n"]
        por_rol[g["perfil"].role][1] += g["seg"]
    print("-" * 96)
    for rol in cameras.ROLES:
        if rol in por_rol:
            n, seg = por_rol[rol]
            print(f"  {rol:<8} {n:>6} clips  {seg/3600:>6.1f} h")

    # ---- avisos ----------------------------------------------------------
    if sin_perfil:
        print(f"\n⚠  {len(sin_perfil)} grupo(s) SIN PERFIL — caen en '{reg.fallback.id}' "
              f"[{reg.fallback.role}].")
        print("   Ese material queda fuera de transcripcion y sync. Si es camara "
              "principal,\n   hay que darle perfil antes de correr el pipeline.")
    if genericos:
        print(f"\n·  {len(genericos)} grupo(s) reconocido(s) por un perfil GENERICO. "
              f"Funcionan, pero\n   conviene catalogarlos para poder afinarlos "
              f"(sidecar, decodable, audio).")
    if not sin_perfil and not genericos:
        print("\n✓  Todas las camaras del proyecto tienen perfil propio.")

    # ---- borrador de perfiles -------------------------------------------
    escritos = 0
    if (sin_perfil or genericos) and not args.sin_sugerencias:
        sugeridos = []
        for clave, g in sin_perfil + genericos:
            mk, mo, pf, cod, res, fps = clave
            base = (mo or mk or pf or "camara").lower()
            pid = "".join(ch if ch.isalnum() else "-" for ch in base).strip("-")
            match: dict = {}
            if mo:
                match["camera_model"] = [mo]
            elif pf.strip("#"):
                match["filename_prefix"] = [pf.split("#")[0][:4]]
            elif mk:
                match["make"] = [mk]
            sugeridos.append({
                "id": pid or "camara-sin-nombre",
                "label": (mo or mk or f"prefijo {pf}"),
                "role": "REVISAR: main | gopro | drone | other",
                "role_label": "REVISAR",
                "priority": 50,
                "match": match,
                "decodable": True,
                "probe": "ffprobe",
                "audio": {"scratch": True},
                "_visto": {"clips": g["n"], "horas": round(g["seg"] / 3600, 2),
                           "ejemplo": g["ejemplo"], "patron": pf, "codec": cod,
                           "resolucion": res, "fps": fps,
                           "perfil_actual": g["perfil"].id},
            })
        out = root / ".cinema_assistant" / "camera_profiles.suggested.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "_doc": ["Borrador generado por bin/inventario_camaras.py. NO se lee solo.",
                     "Revisar cada 'role', ajustar y mover los perfiles buenos a",
                     "<disco>/.cinema_assistant/camera_profiles.json (solo este proyecto)",
                     "o a config/camera_profiles.json del motor (todos los proyectos)."],
            "generado": time.strftime("%Y-%m-%d %H:%M:%S"),
            "profiles": sugeridos,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        escritos = len(sugeridos)
        print(f"\nBorrador escrito: {out}  ({escritos} perfil(es) por revisar)")

    # ---- comparacion con el comportamiento anterior ----------------------
    cambios = 0
    if args.comparar_legacy:
        print("\n" + "=" * 96)
        print("CAMBIOS respecto a la classify_camera() anterior al registro")
        print("=" * 96)
        deltas: dict = {}
        for r in rows:
            viejo = cameras.legacy_classify(r["filename"], r["camera_model"])
            nuevo = cameras.classify_row(r, registry=reg).role
            if viejo != nuevo:
                k = (viejo, nuevo, (r["camera_model"] or "").strip(),
                     patron(r["filename"]))
                deltas.setdefault(k, 0)
                deltas[k] += 1
                cambios += 1
        if not deltas:
            print("  ninguno — el registro clasifica igual que antes.")
        else:
            for (viejo, nuevo, mo, pf), n in sorted(deltas.items(), key=lambda x: -x[1]):
                flecha = f"{viejo} -> {nuevo}"
                print(f"  {n:>5} clips  {flecha:<18} modelo={mo or '-':<16} patron={pf}")
            print(f"\n  Total: {cambios} clips cambian de rol.")
            print("  Revisar que cada cambio sea el arreglo esperado y no una "
                  "sorpresa\n  antes de re-correr el pipeline sobre un proyecto cerrado.")

    conn.close()
    report_done("inventario_camaras", grupos=len(grupos), sin_perfil=len(sin_perfil),
                genericos=len(genericos), sugeridos=escritos, cambios=cambios)
    return 0


if __name__ == "__main__":
    sys.exit(main())
