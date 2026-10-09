#!/usr/bin/env python3
"""Input Color Space e Input Gamma de cada camara, para el Color Management.

Peticion del editor (2026-08-18): "necesito que puedas decirme el input color
space e input gamma de cada camara". Es lo primero que se rellena al abrir un
proyecto con Color Management en Resolve, y equivocarse ahi NO da error: da una
imagen plana o quemada que parece un problema de etalonaje y se persigue durante
horas en el sitio equivocado.

QUE HACE
  1. Agrupa el material por camara, como inventario_camaras.
  2. Para cada grupo busca la evidencia, en este orden de autoridad:
       lo DECLARADO en project_config.json  >  el sidecar XAVC  >  el contenedor
  3. Con --medir, corrobora mirando la luma real de un fotograma: una curva log
     levanta el negro y comprime el rango. Corrobora; no decide.
  4. Escribe reports/color-<fecha>.md con los nombres EXACTOS de los
     desplegables de Resolve.

QUE NO HACE
  No afirma un gamut que no puede ver. Sony escribe `CaptureColorPrimaries:
  rec709` en el sidecar aunque la camara este en S-Gamut3.Cine — ese campo
  describe la codificacion del contenedor, no el espacio de captura. La gamma es
  dato; el gamut se propone y se pide confirmar.

Declararlo por proyecto (gana sobre todo lo demas):

    "camera_color": {
      "Video 01": {"input_color_space": "S-Gamut3.Cine",
                   "input_gamma": "S-Log3",
                   "nota": "PP8, confirmado por quien puso la camara"}
    }

Uso:
    python3 bin/color_por_camara.py --root <disco>
    python3 bin/color_por_camara.py --root <disco> --medir
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import cameras, color_pipeline as cp, manifest      # noqa: E402
from lib.guards import assert_selected, report_done          # noqa: E402


def gamma_del_sidecar(path: str) -> tuple[str, str]:
    """(gamma, primaries) del sidecar XAVC <clip>M01.XML. ('','') si no hay."""
    p = Path(path)
    side = p.parent / (p.stem + "M01.XML")
    if not side.exists():
        return "", ""
    try:
        root = ET.parse(side).getroot()
    except (ET.ParseError, OSError):
        return "", ""
    gamma = primaries = ""
    for el in root.iter():
        if el.tag.endswith("Item"):
            if el.get("name") == "CaptureGammaEquation":
                gamma = el.get("value") or ""
            elif el.get("name") == "CaptureColorPrimaries":
                primaries = el.get("value") or ""
    return gamma, primaries


def tags_del_contenedor(raw_json: str) -> dict:
    try:
        d = json.loads(raw_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    for s in d.get("streams", []) or []:
        if s.get("codec_type") == "video":
            return s
    return {}


def medir_luma(path: str, segundo: float = 5.0) -> tuple | None:
    """(veredicto, explicacion) mirando el negro y el blanco reales."""
    cmd = ["ffmpeg", "-hide_banner", "-v", "info", "-ss", str(segundo),
           "-t", "1.0", "-i", path,
           "-vf", "scale=320:-2,signalstats,metadata=print:file=-",
           "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    def med(clave):
        v = [float(l.split("=")[1]) for l in out.splitlines()
             if l.startswith(f"lavfi.signalstats.{clave}=")]
        return sorted(v)[len(v) // 2] if v else None
    ymin, ymax = med("YMIN"), med("YMAX")
    if ymin is None:
        return None
    return cp.leer_luma(ymin, ymax)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--medir", action="store_true",
                    help="corrobora mirando la luma real (cuesta unos segundos "
                         "por camara)")
    ap.add_argument("--segundo", type=float, default=5.0,
                    help="segundo del clip donde medir (default 5)")
    args = ap.parse_args()

    root = Path(args.root)
    ca = root / ".cinema_assistant"
    db = ca / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}. Corre antes index_project.py.")

    cfg = {}
    cfg_p = ca / "project_config.json"
    if cfg_p.exists():
        try:
            cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    declarados = cfg.get("camera_color") or {}

    conn = manifest.conectar(str(db))
    conn.row_factory = __import__("sqlite3").Row
    reg = cameras.load_profiles(str(root))

    filas = conn.execute("""
        SELECT id, path, filename, parent_folder, camera_make, camera_model,
               ext, duration_sec, raw_metadata_json
        FROM clips WHERE file_kind='video' AND index_status='ok'
        ORDER BY parent_folder, filename""").fetchall()
    assert_selected(filas, "clips de video indexados",
                    filters={"--root": str(root)},
                    hint="¿Corriste index_project.py?")

    grupos: dict = {}
    for r in filas:
        perfil = cameras.classify_row(r, registry=reg)
        clave = (r["parent_folder"] or "(raiz)", r["camera_model"] or "", perfil.id)
        g = grupos.setdefault(clave, {"n": 0, "seg": 0.0, "muestra": None,
                                      "perfil": perfil})
        g["n"] += 1
        g["seg"] += float(r["duration_sec"] or 0)
        # La muestra es el clip mas LARGO: el que mas posibilidades tiene de
        # traer imagen representativa y no un fotograma negro de arranque.
        if g["muestra"] is None or (r["duration_sec"] or 0) > (g["muestra"]["duration_sec"] or 0):
            g["muestra"] = r

    print(f"\nColor management por camara — {root.name}")
    print("=" * 92)

    resultados = []
    for (carpeta, modelo, perfil_id), g in sorted(grupos.items()):
        m = g["muestra"]
        gamma_xml, prim_xml = gamma_del_sidecar(m["path"])
        st = tags_del_contenedor(m["raw_metadata_json"])
        luma = medir_luma(m["path"], args.segundo) if args.medir else None
        decl = declarados.get(carpeta) or declarados.get(modelo)

        res = cp.decidir(gamma_xml=gamma_xml,
                         transfer=st.get("color_transfer"),
                         primaries=prim_xml or st.get("color_primaries"),
                         luma=luma, declarado=decl)
        res.update({"carpeta": carpeta, "modelo": modelo or "(sin modelo)",
                    "perfil": perfil_id, "n": g["n"], "seg": g["seg"],
                    "muestra": m["filename"],
                    "rango": st.get("color_range") or "?",
                    "pix": st.get("pix_fmt") or "?"})
        resultados.append(res)

        print(f"\n{carpeta}   ({res['modelo']}, perfil '{perfil_id}')")
        print(f"  {g['n']} clips · {g['seg']/60:.1f} min · muestra {m['filename']}")
        print(f"  Input Color Space : {res['espacio'] or '— sin determinar —'}")
        print(f"  Input Gamma       : {res['gamma'] or '— sin determinar —'}")
        print(f"  confianza         : {res['confianza']}")
        print(f"  evidencia         : {res['evidencia']}")
        if res["rango"] == "pc":
            print("  ⚠ el clip viene marcado como RANGO COMPLETO (color_range=pc): "
                  "en Resolve, Data Levels = Full")
        for a in res["avisos"]:
            print(f"  ⚠ {a}")

    # ---- informe ---------------------------------------------------------
    rep = ca / "reports"
    rep.mkdir(parents=True, exist_ok=True)
    out = rep / f"color-{time.strftime('%Y%m%d')}.md"
    L = []
    L.append("# Color management — Input Color Space e Input Gamma\n")
    L.append("Nombres tal cual salen en los desplegables de Resolve "
             "(Project Settings > Color Management, o clic derecho en el clip "
             "> Input Color Space).\n")
    L.append("| Cámara | Clips | Input Color Space | Input Gamma | Confianza |")
    L.append("|---|---:|---|---|---|")
    for r in resultados:
        L.append(f"| `{r['carpeta']}` ({r['modelo']}) | {r['n']} | "
                 f"**{r['espacio'] or '?'}** | **{r['gamma'] or '?'}** | "
                 f"{r['confianza']} |")
    L.append("")
    for r in resultados:
        L.append(f"## {r['carpeta']} — {r['modelo']}\n")
        L.append(f"- {r['n']} clips, {r['seg']/60:.1f} min. Muestra: `{r['muestra']}`")
        L.append(f"- Evidencia: {r['evidencia']}")
        L.append(f"- Pixel format: `{r['pix']}` · color_range: `{r['rango']}`")
        if r["rango"] == "pc":
            L.append("- **Rango completo**: en Resolve, *Data Levels = Full* "
                     "para este material. Con *Video* se recortan los extremos.")
        for a in r["avisos"]:
            L.append(f"- **Ojo:** {a}")
        L.append("")
    L.append("## Cómo fijarlo\n")
    L.append("Se declara por proyecto y gana sobre cualquier medición, en "
             "`project_config.json`:\n")
    L.append("```json")
    L.append('"camera_color": {')
    L.append(",\n".join(
        f'  "{r["carpeta"]}": {{"input_color_space": "{r["espacio"] or "?"}", '
        f'"input_gamma": "{r["gamma"] or "?"}", "nota": "quién lo confirmó"}}'
        for r in resultados))
    L.append("}")
    L.append("```")
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"\nInforme: {out}")
    report_done("color_por_camara", camaras=len(resultados), informe=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
