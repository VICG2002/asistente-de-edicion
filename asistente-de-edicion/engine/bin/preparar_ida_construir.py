#!/usr/bin/env python3
"""Arma el paquete de la ida y vuelta de `construir` para OTRA Mac (la VM Free).

PARA QUE SIRVE (2026-10-06, plan Free, Fase 1)
`construir` se prueba con el mock y el sandbox en cada corrida de la suite,
pero la aceptacion es en un Resolve Free real (plan, principio 5). Este script
deja en una carpeta todo lo que esa prueba necesita, con las rutas de la otra
Mac ya escritas:

  media/      cinco clips de dos camaras (Vic, la base, y Ayan) y dos WAV de
              lavalier (izq, drc), sinteticos (ffmpeg)
  bake/       el horneado de esa media (kit_data.lua, kit_indice.json y
              kit_multicam.lua), con la forma de export_lua_data.py v11
  motor/      aplicar.lua, construir.lua y asistente_lib.lua, copiados del motor
  Diez50 Aplicar.lua   el stub del menu
  buzon-<sello>.lua    el buzon con el pedido `construir` + `exportar`
  pedido-<sello>.json  la copia del pedido, que va a <recibos>/<sello>/
  instalar.sh / recoger.sh

Se corre otra vez con --sello nuevo para el segundo pedido (el que aparta las
timelines del primero). Queda como prueba del canal tras cada update de Resolve,
igual que `prueba_regreso`.

EN LA OTRA MAC
    bash "<carpeta>/instalar.sh" <sello>     # media, motor, bake, stub y buzon
    (Resolve Free: proyecto DIEZ50-CONSTRUIR, importar ~/Diez50-construir/media,
     Workspace > Scripts > Diez50 Aplicar)
    bash "<carpeta>/recoger.sh" <sello>      # los recibos a la carpeta compartida
AQUI
    python3 bin/verificar_regreso.py --recibos <carpeta>/recibos/<sello>/

    python3 bin/preparar_ida_construir.py \\
        --home-otra /Users/victorisaycorreagutierrez --destino ~/Diez50-VM/compartida/construir
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE))

from lib import pedido as P  # noqa: E402

# Con guion y no guion bajo: UTM no le pasa a la VM el guion bajo (ni texto de
# corrido, solo teclas sueltas), y el nombre del proyecto se teclea en Resolve
# (medido el 2026-10-07, teclado espanol en la VM).
PROYECTO = "DIEZ50-CONSTRUIR"
PFX = "KIT — "
DIA = "2026-10-06"
T0 = datetime(2026, 10, 6, 18, 0, 0, tzinfo=timezone.utc).timestamp()
MOTOR = ("aplicar.lua", "construir.lua", "asistente_lib.lua")

# (carpeta, archivo, roll, segundos desde T0, duracion, tono Hz)
VIDEOS = [
    ("Vic", "VIC_0001.mov", "A", 40, 20, 440),
    ("Vic", "VIC_0002.mov", "B", 90, 15, 550),
    ("Vic", "VIC_0003.mov", "A", 130, 12, 660),
    ("Ayan", "AYA_0001.mov", "A", 42, 18, 330),
    ("Ayan", "AYA_0002.mov", "B", 150, 10, 770),
]
# (tx, carpeta, epoch relativo a T0, duracion)
WAVS = [("izq", "Izq", 0.0, 180), ("drc", "Drc", -2.5, 180)]


def ffmpeg(*args: str) -> None:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg fallo: {r.stderr.strip()[-400:]}")


def generar_media(media: Path) -> None:
    for carpeta, nombre, _roll, _t, dur, tono in VIDEOS:
        destino = media / carpeta / nombre
        if destino.exists():
            continue
        destino.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=24:duration={dur}",
               "-f", "lavfi", "-i", f"sine=frequency={tono}:sample_rate=48000:duration={dur}",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
               "-c:a", "pcm_s16le", "-shortest", str(destino))
    for _tx, carpeta, _e, dur in WAVS:
        destino = media / "Audio" / carpeta / "00001.WAV"
        if destino.exists():
            continue
        destino.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg("-f", "lavfi", "-i", f"sine=frequency=220:sample_rate=48000:duration={dur}",
               "-ac", "1", "-c:a", "pcm_s24le", str(destino))


def horneado(media_otra: str) -> tuple[dict, dict, dict]:
    """El horneado, su indice y su multicam, con las rutas de la otra Mac."""
    def iso(seg: float) -> str:
        return datetime.fromtimestamp(T0 + seg, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    clips, indice = {}, {"formato": 1, "clips": {}, "audios": {}}
    for carpeta, nombre, roll, t, dur, _ in VIDEOS:
        ruta = f"{media_otra}/{carpeta}/{nombre}"
        clips[ruta] = {
            "name": nombre, "folder": carpeta, "status": "keep", "notes": "", "dur": float(dur),
            "created": iso(t), "dia": DIA, "camera": "main", "interview": roll == "A",
            "category": "", "who": "KIT" if roll == "A" else "",
            "meta_description": f"Kit {nombre}", "meta_shot": "", "meta_scene": "",
            "meta_keywords": "kit", "meta_comments": "", "curated_segments": [],
            "questions": [], "roll": roll, "roll_score": 0.5, "roll_reason": "kit",
            "reel": 0, "cortes": [], "highlights": [],
            "beats": ([{"kind": "pregunta", "s": 1.0, "e": 3.0, "text": "Quien eres",
                        "speaker": "video_a1", "conf": 0.9, "src": "master_src", "qi": 0},
                       {"kind": "respuesta", "s": 3.0, "e": 15.0, "text": "El kit.",
                        "speaker": "lavalier_izq", "conf": 0.9, "src": "master_src",
                        "qi": 0}] if nombre == "VIC_0001.mov" else []),
        }
        indice["clips"][ruta] = {"nombre": nombre, "roll": roll, "dia": DIA, "folder": carpeta}
    audios = {}
    for tx, carpeta, e, dur in WAVS:
        ruta = f"{media_otra}/Audio/{carpeta}/00001.WAV"
        audios[ruta] = {"name": "00001.WAV", "folder": carpeta, "tx": tx, "dia": DIA,
                        "dur": float(dur), "created": "", "category": "",
                        "meta_description": "", "meta_shot": "", "meta_scene": "",
                        "meta_keywords": "", "meta_comments": "", "content_segments": [],
                        "chain": f"Audio/{carpeta}#00001.WAV", "chain_pos": 0.0,
                        "epoch_start": T0 + e, "questions": []}
        indice["audios"][ruta] = {"nombre": "00001.WAV", "dia": DIA, "tx": tx}
    izq = f"{media_otra}/Audio/Izq/00001.WAV"
    vic = lambda n: f"{media_otra}/Vic/{n}"          # noqa: E731
    ayan = lambda n: f"{media_otra}/Ayan/{n}"        # noqa: E731
    data = {
        "clips": clips,
        "byname": {c["name"]: r for r, c in clips.items()},
        # offset = audio_start - video_start: el WAV arranco en T0, el video en T0+t.
        "sync": {vic("VIC_0001.mov"): [{"audio": "00001.WAV", "audiopath": izq,
                                        "offset": -40.0, "conf": 0.95,
                                        "speaker": "lavalier_izq"}],
                 vic("VIC_0002.mov"): [{"audio": "00001.WAV", "audiopath": izq,
                                        "offset": -90.0, "conf": 0.95,
                                        "speaker": "lavalier_izq"}]},
        "audios": audios,
        "show_windows": [],
    }
    multicam = {"skews": {"Vic": 0.0, "Ayan": 0.0}, "base_group": "Vic",
                "track_order": ["Vic", "Ayan"],
                "pairs": [{"a": vic("VIC_0001.mov"), "b": ayan("AYA_0001.mov"), "delta": 2.0,
                           "overlap": 18.0, "verified": True, "place": True,
                           "basis": "contenido"}]}
    return data, indice, multicam


INSTALAR = """#!/bin/bash
# Diez50 - instala en la otra Mac la prueba de `construir`. Solo escribe en
# ~/Diez50-construir, el buzon, los errores y el stub Diez50 Aplicar del menu.
#   bash instalar.sh <sello> [--solo-pedido]
set -e
S="${{1:?falta el sello}}"
C="$(cd "$(dirname "$0")" && pwd)"
K="$HOME/Diez50-construir"
AS="$HOME/Library/Application Support/Diez50"
U="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
mkdir -p "$K/recibos/$S" "$AS/errores" "$U"
if [ "$2" != "--solo-pedido" ]; then
  ditto "$C/media" "$K/media"
  ditto "$C/motor" "$K/motor"
  ditto "$C/bake" "$K/bake"
  cp "$C/Diez50 Aplicar.lua" "$U/Diez50 Aplicar.lua"
fi
cp "$C/pedido-$S.json" "$K/recibos/$S/pedido.json"
cp "$C/buzon-$S.lua" "$AS/buzon.lua.tmp" && mv "$AS/buzon.lua.tmp" "$AS/buzon.lua"
[ "{home}" = "$HOME" ] || echo "AVISO: el paquete es para {home} y esto es $HOME"
find "$K/media" -type f | sort
ls -la "$U" "$K/recibos/$S"
"""

RECOGER = """#!/bin/bash
# Diez50 - trae los recibos de un pedido a la carpeta compartida, con sus fechas.
#   bash recoger.sh <sello>
S="${{1:?falta el sello}}"
C="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$C/recibos" "$C/errores"
ditto "$HOME/Diez50-construir/recibos/$S" "$C/recibos/$S" && ls -la "$C/recibos/$S"
ditto "$HOME/Library/Application Support/Diez50/errores" "$C/errores" && ls -la "$C/errores"
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home-otra", required=True,
                    help="El $HOME de la otra Mac (la VM), p. ej. /Users/victorisaycorreagutierrez")
    ap.add_argument("--destino", default="~/Diez50-VM/compartida/construir",
                    help="Carpeta del paquete (la compartida con la otra Mac).")
    ap.add_argument("--sello", help="Sello del pedido (por defecto, uno nuevo).")
    ap.add_argument("--proyecto", default=PROYECTO,
                    help=f"Nombre EXACTO del proyecto de Resolve alla (por defecto {PROYECTO}).")
    ap.add_argument("--reaplicar", action="store_true")
    ap.add_argument("--sin-media", action="store_true",
                    help="No generar la media (las pruebas de la suite, sin ffmpeg).")
    a = ap.parse_args(argv)

    destino = Path(a.destino).expanduser()
    home = a.home_otra.rstrip("/")
    kit = f"{home}/Diez50-construir"
    destino.mkdir(parents=True, exist_ok=True)
    if not a.sin_media:
        generar_media(destino / "media")

    data, indice, multicam = horneado(f"{kit}/media")
    bake = destino / "bake"
    bake.mkdir(exist_ok=True)
    (bake / "kit_data.lua").write_text("-- Kit de construir (preparar_ida_construir.py)\n"
                                       "return " + P.a_lua(data) + "\n", encoding="utf-8")
    (bake / "kit_indice.json").write_text(json.dumps(indice, indent=1) + "\n",
                                          encoding="utf-8")
    (bake / "kit_multicam.lua").write_text("return " + P.a_lua(multicam) + "\n",
                                           encoding="utf-8")
    motor = destino / "motor"
    motor.mkdir(exist_ok=True)
    for f in MOTOR:
        shutil.copy2(ENGINE / "resolve" / f, motor / f)

    sello = a.sello or P.nuevo_sello()
    ped = P.armar_pedido_construir(indice, data=f"{kit}/bake/kit_data.lua",
                                   multicam=f"{kit}/bake/kit_multicam.lua",
                                   recibos_base=f"{kit}/recibos", pfx=PFX, dia=DIA,
                                   sello=sello, reaplicar=a.reaplicar,
                                   orden_tx=[tx for tx, *_ in WAVS])
    buzon = destino / f"buzon-{sello}.lua"
    for viejo in (buzon, buzon.with_suffix(".json")):
        viejo.unlink(missing_ok=True)
    try:
        P.poner_pedido(a.proyecto, ped, ruta=buzon, validar_rutas=False, crear_recibos=False)
    except P.ErrorPedido as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    (destino / f"pedido-{sello}.json").write_text(
        json.dumps(P.copia_pedido(a.proyecto, ped), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    AS = f"{home}/Library/Application Support/Diez50"
    (destino / P.STUB_NOMBRE).write_text(
        P.texto_stub(f"{kit}/motor/aplicar.lua", f"{AS}/buzon.lua", f"{AS}/errores/"),
        encoding="utf-8")
    for nombre, texto in (("instalar.sh", INSTALAR.format(home=home)),
                          ("recoger.sh", RECOGER.format())):
        f = destino / nombre
        f.write_text(texto, encoding="utf-8")
        f.chmod(0o755)

    print(f"Paquete en {destino}")
    print(f"  pedido {sello} para el proyecto {a.proyecto}: "
          + ", ".join(ped["esperado"]["timelines"]))
    print(f"  alla:  bash \"<compartida>/{destino.name}/instalar.sh\" {sello}")
    print(f"  luego: Resolve Free, proyecto {a.proyecto}, importar {kit}/media,")
    print("         Workspace > Scripts > Diez50 Aplicar")
    print(f"  alla:  bash \"<compartida>/{destino.name}/recoger.sh\" {sello}")
    print(f"  aqui:  python3 bin/verificar_regreso.py --recibos {destino}/recibos/{sello}/ "
          f"--errores {destino}/errores")
    return 0


if __name__ == "__main__":
    sys.exit(main())
