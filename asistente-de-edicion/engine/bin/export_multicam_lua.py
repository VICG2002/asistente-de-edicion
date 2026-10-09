#!/usr/bin/env python3
"""Deriva pares multicámara (misma escena, dos cámaras) y los exporta a Lua.

v2 (MAB 2026-06-12): candidatos por RELOJ CORREGIDO + verificación directa
A1↔A1 por envelope.

  1. Candidatos: skew por grupo de cámara (parent_folder) medido contra los
     audios continuos (mismo método que derive_chrono_sync). Dos videos de
     grupos distintos cuyos intervalos de tiempo real corregido se traslapan
     son cobertura simultánea — INCLUYE tramos sin audio externo (tarde).
  2. Seed del delta: diferencia de offsets si ambos comparten WAV (más
     preciso); si no, diferencia de relojes corregidos.
  3. Refinamiento: cross-correlación de envelope (banda voz) ENTRE LOS DOS
     A1 (lib.sync_refiner.refine_offset video↔video). Clave cuando una
     cámara lleva el RX del lavalier directo al A1 (caso Ayan en MAB):
     su A1 es señal limpia y el match es fortísimo. Si prominence pasa el
     umbral, el delta queda verificado (verified=true).

Output Lua: return { {a=, b=, delta=, overlap=, verified=}, ... }
`a` = clip base (V1, empieza primero); `b` va en V2; delta = segundos que
b empieza después de a.

Uso:
    bin/export_multicam_lua.py --root <disk> --out resolve/<p>_multicam.lua \
        [--audio-like '%wireless%'] [--utc-offset -6] [--min-overlap 5]
        [--min-prominence 0.30] [--search-window 2.5] [--no-refine]
"""
from __future__ import annotations

import argparse
import glob
import json
import posixpath
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib.sync_refiner import refine_offset
from lib import manifest  # noqa: E402
from lib import proyecto  # noqa: E402


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root no resolvible: {root_arg}")


def lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def video_epoch(creation_time: str, utc_offset_hours: float):
    if not creation_time:
        return None
    try:
        dt = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
        local = dt + timedelta(hours=utc_offset_hours)
        return time.mktime(local.replace(tzinfo=None).timetuple())
    except (ValueError, OverflowError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--audio-like", default="%wireless%",
                    help="filename LIKE de los audios continuos para medir skew.")
    ap.add_argument("--utc-offset", type=float, default=-6.0)
    ap.add_argument("--min-overlap", type=float, default=5.0)
    ap.add_argument("--min-prominence", type=float, default=0.30,
                    help="Prominence mínima del peak A1↔A1 para marcar verified.")
    ap.add_argument("--search-window", type=float, default=2.5)
    ap.add_argument("--max-refine-delta", type=float, default=3.0,
                    help="Si el refinamiento mueve el delta más que esto (s), "
                         "se descarta el refinamiento (peak sospechoso).")
    ap.add_argument("--no-refine", action="store_true")
    ap.add_argument("--trust-clock-in-windows", action="store_true",
                    help="Acepta la alineación por RELOJ (sin confirmar por "
                         "contenido) para los pares cuyo traslape cae dentro de "
                         "las `show_windows` del project_config.json. Ahí todas "
                         "las cámaras apuntan a lo mismo, así que no puede "
                         "colocarse 'otra escena' encima. Fuera de esas ventanas "
                         "sigue exigiendo confirmación. Correr antes "
                         "bin/derive_show_windows.py.")
    ap.add_argument("--base-group", default=None,
                    help="Grupo(s) de la cámara BASE (V1), CSV. Matchea el "
                         "grupo exacto o su basename (con --group-by dirname, "
                         "'A' matchea 'Jueves/VIDEO/A' y 'Sabado/VIDEO/A'). "
                         "Los pares se orientan a=base, b=compañera (V2). "
                         "En MAB: 'Ayan' (su A1 es el lavalier).")
    ap.add_argument("--group-by", choices=("parent", "dirname"),
                    default="parent",
                    help="Grupo de cámara: parent_folder (default, MAB) o el "
                         "directorio completo del rel_path (layouts anidados "
                         "tipo Jueves/VIDEO/A, Film Club Café 2026-07-09).")
    args = ap.parse_args()

    def vgroup(parent_folder: str, rel_path: str) -> str:
        return proyecto.grupo_de_camara(parent_folder, rel_path, cfg,
                                        por_dirname=args.group_by == "dirname")

    # Se recalcula despues de deducir el base_group por cantidad de material.
    base_tokens: list[str] = []

    def recalcular_base_tokens() -> None:
        base_tokens[:] = ([t.strip() for t in args.base_group.split(",") if t.strip()]
                          if args.base_group else [])

    recalcular_base_tokens()

    def is_base(grp: str) -> bool:
        return grp in base_tokens or posixpath.basename(grp) in base_tokens

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    conn = manifest.conectar(str(db))
    cfg = proyecto.leer_config(root)

    # --- skew por grupo (mismo método que derive_chrono_sync) ---------------
    wavs = {aid: (mt - dur) for aid, mt, dur in conn.execute(
        "SELECT id, mtime, duration_sec FROM clips WHERE file_kind='audio' "
        "AND index_status='ok' AND lower(filename) LIKE ? AND duration_sec > 0",
        (args.audio_like,))}
    skews: dict[str, list[float]] = {}
    try:
        pares_sync = conn.execute(
            "SELECT sp.audio_clip_id, sp.offset_sec, v.parent_folder, v.rel_path, "
            "v.creation_time "
            "FROM audio_sync_pairs sp JOIN clips v ON v.id=sp.video_clip_id"
        ).fetchall()
    except sqlite3.OperationalError:
        # Proyecto sin audio externo: no hay `audio_sync_pairs` y nunca la habra.
        # No es un error — este bake sigue haciendo falta porque es el vehiculo
        # de los `camera_skews_manual` hasta el Lua, y un rodaje a varias camaras
        # con solo audio de camara los necesita igual (IMODAE 2026-08-07, donde
        # esto reventaba con un traceback de SQLite).
        pares_sync = []
        print("  · sin tabla audio_sync_pairs — proyecto sin audio externo. "
              "Los skews saldran de camera_skews_manual.")
    for aid, off, pf, rel, ct in pares_sync:
        if aid not in wavs:
            continue
        v_ep = video_epoch(ct, args.utc_offset)
        if v_ep is None:
            continue
        skews.setdefault(vgroup(pf, rel), []).append(off - (wavs[aid] - v_ep))
    skew_med = {}
    for grp, vals in skews.items():
        med = statistics.median(vals)
        clean = [v for v in vals if abs(v - med) <= 10.0]
        if len(clean) >= 3:
            skew_med[grp] = statistics.median(clean)
            print(f"Grupo '{grp}': skew={skew_med[grp]:.2f}s (n={len(clean)}, medido)")

    # --- skews declarados a mano por el editor --------------------------------
    #
    # Hay camaras cuyo reloj el motor NO puede medir: si su A1 esta tapado por
    # la musica del evento no hay contenido que emparejar, y la correlacion de
    # forma de onda sobre musica devuelve picos falsos con buena pinta. Morsa
    # 2026-08-03: la a6700 de Iban resistio seis metodos distintos.
    #
    # Lo que SI funciona en ese caso es el ojo del editor: Victor vio que el
    # material de Iban traia una cancion del concierto mezclada con tomas del
    # presentador previo, y dedujo que su camara iba una hora atrasada. Eso es
    # una MEDICION, hecha por contenido, y vale mas que seis correlaciones que
    # no convergen.
    #
    # Se declara en project_config.json y queda escrito de donde salio:
    #     "camera_skews_manual": {"Iban": -3600.0}
    #     "camera_skews_manual_nota": "quien lo midio y como"
    #
    # Un skew manual NO da precision de frame: sirve para ORDENAR la cronologia
    # y para generar candidatos de multicam, que despues el refinamiento por
    # contenido confirma o descarta uno por uno.
    cfg_p = root / ".cinema_assistant" / "project_config.json"
    cfg_all = json.loads(cfg_p.read_text(encoding="utf-8")) if cfg_p.exists() else {}
    for grp, val in (cfg_all.get("camera_skews_manual") or {}).items():
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if grp in skew_med:
            print(f"Grupo '{grp}': skew MANUAL {val:+.2f}s sustituye al medido "
                  f"({skew_med[grp]:+.2f}s)")
        else:
            print(f"Grupo '{grp}': skew MANUAL {val:+.2f}s (el motor no pudo medirlo)")
        skew_med[grp] = val

    # --- ORDEN DE PISTAS: la del LAVALIER en V1, el resto por material --------
    #
    # Regla vigente desde 2026-08-13 (peticion del usuario): la espina dorsal de
    # la timeline es el audio del lavalier, asi que V1 es la camara que RECIBIO
    # esa señal —por enlace inalambrico o cable—, no la que mas rodo. Las
    # secundarias se siguen ordenando por cantidad de material, que es la regla
    # v0.3.0 (Morsa) y sigue siendo la mejor para ellas: evita que el mismo
    # angulo caiga en pista distinta segun el clip.
    #
    # QUE CAMARA LLEVABA EL LAVALIER SE DECLARA, NO SE DEDUCE.
    # El plan original media "cobertura de pares contra cadena de lavalier".
    # Medido contra los manifests reales el 2026-08-13, eso no discrimina:
    #
    #   MAB    Ayan 0.08 cobertura / VICG 001 0.08   -> empate perfecto
    #   MORSA  las tres camaras entre -16.6 y -17.3 dB RMS -> 0.7 dB de rango
    #   FCC    mismo numero de palabras utiles en las dos (121 y 121)
    #
    # La razon de fondo es que casi ningun par esta MEDIDO: en Morsa 9 de 534,
    # en MAB 20 de 196. El resto son derivaciones por reloj o al hermano, que se
    # propagan a todas las camaras por igual y borran la señal.
    #
    # El nivel de audio a veces separa (MAB: -17.0 vs -35.7 dB) pero eso puede
    # ser ganancia distinta, no un receptor de lavalier — y en Morsa no separa
    # nada. Elegir la base con esa pista seria adivinar sobre un proyecto vivo.
    # Vale la regla del usuario del 2026-08-07: el MODELO de una camara se
    # comprueba en la metadata, pero EL PAPEL QUE JUGO se pregunta.
    #
    # Orden de autoridad: --base-group  >  project_config.json  >  metraje.
    dur_por_grupo: dict[str, float] = {}
    clips_por_grupo: dict[str, int] = {}
    for pf, rel, dur in conn.execute(
        "SELECT parent_folder, rel_path, duration_sec FROM clips "
        "WHERE file_kind='video' AND index_status='ok'"):
        g = vgroup(pf, rel)
        clips_por_grupo[g] = clips_por_grupo.get(g, 0) + 1
        if dur:
            dur_por_grupo[g] = dur_por_grupo.get(g, 0.0) + dur

    por_material = [g for g, _ in sorted(dur_por_grupo.items(),
                                         key=lambda kv: (-kv[1], kv[0]))]

    # Evidencia: solo se IMPRIME, nunca decide. Cuenta los pares realmente
    # medidos contra contenido; las derivaciones no prueban que esta camara
    # tuviera el lavalier, solo que alguien se lo presto por reloj.
    medidos: dict[str, int] = {}
    conf_sum: dict[str, float] = {}
    try:
        for pf, rel, cf in conn.execute(
            "SELECT v.parent_folder, v.rel_path, sp.confidence "
            "FROM audio_sync_pairs sp JOIN clips v ON v.id=sp.video_clip_id "
            "WHERE sp.method LIKE 'transcript%' OR sp.method LIKE 'waveform%' "
            "   OR sp.method LIKE 'resolve%'"):
            g = vgroup(pf, rel)
            medidos[g] = medidos.get(g, 0) + 1
            conf_sum[g] = conf_sum.get(g, 0.0) + (cf or 0.0)
    except sqlite3.OperationalError:
        pass                      # proyecto sin audio externo

    # El promedio se hace AQUI y no en SQL: el grupo lo define vgroup(), que
    # depende de --group-by, asi que un GROUP BY de SQLite agruparia por otra
    # cosa. Agrupar mal daba -56.1 dB donde el grupo promedia -35.7.
    rms_sum: dict[str, float] = {}
    rms_n: dict[str, int] = {}
    try:
        for pf, rel, r in conn.execute(
            "SELECT c.parent_folder, c.rel_path, a.audio_rms_db "
            "FROM clip_analysis a JOIN clips c ON c.id=a.clip_id "
            "WHERE c.file_kind='video' AND a.audio_rms_db IS NOT NULL"):
            g = vgroup(pf, rel)
            rms_sum[g] = rms_sum.get(g, 0.0) + r
            rms_n[g] = rms_n.get(g, 0) + 1
    except sqlite3.OperationalError:
        pass
    rms = {g: rms_sum[g] / rms_n[g] for g in rms_sum if rms_n[g]}

    def grupos_que_casan(nombre: str) -> list[str]:
        """Grupos cuyo nombre de camara case con `nombre`.

        No exige igualdad exacta porque el nombre de la carpeta y el de la
        camara no coinciden: el editor dice "VICG" y la carpeta es "VICG 001".
        Y la camara no siempre esta al mismo nivel — el layout habitual es
        `<dia>/<camara>/`, pero en un rodaje de un solo dia la camara queda
        arriba del todo:

            MAB     Ayan/            VICG 001/
            MORSA   Ayan/            Vic/Video 001/      Iban/Video/
            FCC     Jueves/VIDEO/V/  Sabado/VIDEO/A/

        Asi que se compara contra CADA componente de la ruta del grupo, sin
        distinguir mayusculas, aceptando que uno sea prefijo del otro.
        """
        objetivo = nombre.strip().lower()
        if not objetivo:
            return []

        def casa(parte: str) -> bool:
            p = parte.strip().lower()
            if p == objetivo:
                return True
            # Prefijo, pero solo si termina en frontera: "VICG" casa con
            # "VICG 001" y con "VICG001", y NO con "VIDEO". Sin esta condicion
            # una camara llamada "V" casaba con la carpeta "VIDEO", que fue
            # justo lo que caza tests/test_camara_base.py.
            largo, corto = (p, objetivo) if len(p) > len(objetivo) else (objetivo, p)
            return largo.startswith(corto) and not largo[len(corto)].isalpha()

        fuera = []
        for g in dur_por_grupo:
            partes = [g] + [p for p in g.replace("\\", "/").split("/") if p]
            if any(casa(p) for p in partes):
                fuera.append(g)
        return fuera

    declarada = (cfg_all.get("camara_lavalier") or "").strip()
    origen = None
    if args.base_group:
        origen = "--base-group"
    elif declarada:
        casan = grupos_que_casan(declarada)
        if len(casan) == 1:
            args.base_group = casan[0]
            origen = f"project_config.json: camara_lavalier='{declarada}'"
            recalcular_base_tokens()
        elif len(casan) > 1:
            # Pasa cuando la misma camara rodo varios dias y el agrupado los
            # separa (`Jueves/VIDEO/V` y `Sabado/VIDEO/V`). Elegir uno seria
            # inventarse cual de los dos dias manda.
            print(f"  ⚠ camara_lavalier='{declarada}' casa con MAS DE UN grupo: "
                  f"{', '.join(sorted(casan))}.")
            print("    Afina el nombre en project_config.json, o fija uno con "
                  "--base-group. Mientras tanto, base por metraje.")
        else:
            print(f"  ⚠ project_config.json declara camara_lavalier="
                  f"'{declarada}', que no casa con ningun grupo "
                  f"({', '.join(sorted(dur_por_grupo))}). Se ignora.")
    if not args.base_group and por_material:
        args.base_group = por_material[0]
        origen = "metraje (NO se declaro la camara del lavalier)"
        recalcular_base_tokens()

    base = args.base_group if args.base_group in dur_por_grupo else (
        por_material[0] if por_material else None)
    track_order = ([base] + [g for g in por_material if g != base]) if base else []

    if track_order:
        print("Camara base y orden de pistas:")
        print(f"  {'grupo':<20} {'min':>7} {'clips':>6} {'medidos':>8} "
              f"{'conf':>6} {'A1 dB':>7}")
        for g in track_order:
            n = clips_por_grupo.get(g, 0)
            m = medidos.get(g, 0)
            cf = (conf_sum[g] / m) if m else 0.0
            r = rms.get(g)
            print(f"  {g[:20]:<20} {dur_por_grupo.get(g, 0)/60:>7.1f} {n:>6} "
                  f"{m:>8} {cf:>6.2f} {('%.1f' % r) if r is not None else '   —':>7}")
        for i, g in enumerate(track_order, 1):
            marca = "  <- base" if g == base else ""
            print(f"  V{i}/A{i} = {g}{marca}")
        print(f"  base por: {origen}")

    if origen and origen.startswith("metraje") and len(track_order) > 1:
        print("  · V1 salio por METRAJE. Si la camara que llevaba el lavalier es")
        print("    otra, declarala en <disco>/.cinema_assistant/project_config.json:")
        print('        { "camara_lavalier": "<grupo>" }')
        print(f"    Grupos disponibles: {', '.join(track_order)}")
        print("    No se sugiere ninguno a proposito: el motor NO puede deducirlo.")
        print("    Los pares medidos son demasiado pocos y el nivel del A1 no")
        print("    distingue un receptor de lavalier de una ganancia mas alta.")

    # --- videos con tiempo real corregido ------------------------------------
    vids = []
    for vid, path, fn, pf, rel, ct, dur in conn.execute(
        "SELECT id, path, filename, parent_folder, rel_path, creation_time, "
        "duration_sec "
        "FROM clips WHERE file_kind='video' AND index_status='ok'"):
        grp = vgroup(pf, rel)
        if grp not in skew_med or not dur:
            continue
        v_ep = video_epoch(ct, args.utc_offset)
        if v_ep is None:
            continue
        vids.append({"vid": vid, "path": path, "fn": fn, "grp": grp,
                     "t0": v_ep - skew_med[grp], "dur": dur})

    # offsets por (video, audio) para seeds más precisos vía WAV compartido
    offs = {}
    try:
        for vid, aid, off in conn.execute(
                "SELECT video_clip_id, audio_clip_id, offset_sec "
                "FROM audio_sync_pairs"):
            offs[(vid, aid)] = off
    except sqlite3.OperationalError:
        pass   # proyecto sin audio externo (ya avisado arriba)
    wav_ids = set(wavs)

    # --- candidatos por traslape de reloj corregido ---------------------------
    # Ventanas de show medidas por bin/derive_show_windows.py. Dentro de ellas
    # todas las cámaras apuntan a lo mismo (el escenario), así que alinear por
    # reloj no puede poner "otra escena" encima — que es el riesgo que motivó
    # exigir confirmación por contenido (caso MAB "luchador cortado por el
    # ciclista", 2026-06-12). Fuera, la regla estricta sigue.
    show_windows: list[tuple[float, float]] = []
    if args.trust_clock_in_windows:
        cfg_p = root / ".cinema_assistant" / "project_config.json"
        cfg = json.loads(cfg_p.read_text(encoding="utf-8")) if cfg_p.exists() else {}
        for w in cfg.get("show_windows") or []:
            try:
                show_windows.append((float(w["inicio_epoch"]), float(w["fin_epoch"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not show_windows:
            sys.exit("--trust-clock-in-windows pero project_config.json no trae "
                     "show_windows. Corre antes: bin/derive_show_windows.py --root "
                     f"{root}")
        tot = sum(b - a for a, b in show_windows) / 60.0
        print(f"Ventanas de show: {len(show_windows)} ({tot:.0f} min) — dentro de "
              "ellas se acepta alineación por reloj.")

    vids.sort(key=lambda v: v["t0"])
    pairs = []
    n_refined = n_unverified = n_dropped = n_reloj_show = 0
    for i in range(len(vids)):
        for j in range(i + 1, len(vids)):
            a, b = vids[i], vids[j]
            if b["t0"] >= a["t0"] + a["dur"]:
                break  # ordenados por t0: ya no hay traslape posible
            if a["grp"] == b["grp"]:
                continue
            overlap = min(a["t0"] + a["dur"], b["t0"] + b["dur"]) - b["t0"]
            if overlap < args.min_overlap:
                continue
            # seed: WAV compartido si existe (offset_A - offset_B); sino reloj
            delta = b["t0"] - a["t0"]
            for aid in wav_ids:
                ka, kb = (a["vid"], aid), (b["vid"], aid)
                if ka in offs and kb in offs:
                    delta = offs[ka] - offs[kb]
                    break
            verified = False
            if not args.no_refine:
                # VOTO MULTI-VENTANA (3 posiciones del clip): el A1 scratch
                # ruidoso engaña a una sola ventana; dos ventanas que
                # coinciden ±0.15s son evidencia fuerte.
                #
                # SIGNO (2026-08-13). Esto pasaba `-delta` y negaba el
                # resultado. No era un capricho: `refine_offset` centraba la
                # busqueda en `v_start + offset` cuando la convencion pide
                # `v_start - offset`, y la doble negacion de aqui CANCELABA ese
                # error. O sea que este sitio media bien por compensacion.
                #
                # Al corregir el refinador, la compensacion paso a ser el error:
                # los pares verificados de Morsa cayeron de 25 a 10. Ahora se
                # pasa `delta` tal cual, que es lo que la convencion dice —
                # aqui `video`=a y `audio`=b, asi que
                # offset = b_start - a_start = delta.
                cands = []
                for cf in (0.30, 0.50, 0.70):
                    r = refine_offset(Path(a["path"]), Path(b["path"]),
                                      delta, a["dur"], b["dur"],
                                      window=args.search_window, center_frac=cf)
                    if r is not None:
                        prom = (r.confidence - 0.5) * 2
                        cands.append((r.new_offset, prom))
                best = None
                for ci in range(len(cands)):
                    for cj in range(ci + 1, len(cands)):
                        if abs(cands[ci][0] - cands[cj][0]) <= 0.15:
                            top = max(cands[ci], cands[cj], key=lambda c: c[1])
                            if top[1] >= args.min_prominence and                                (best is None or top[1] > best[1]):
                                best = top
                if best is None:
                    solo = max(cands, key=lambda c: c[1]) if cands else None
                    if solo and solo[1] >= 0.50:
                        best = solo
                if best is not None and abs(best[0] - delta) <= args.max_refine_delta:
                    delta, verified = best[0], True
                    n_refined += 1
                else:
                    n_unverified += 1
            # Orientar: a = grupo base si está definido (delta puede quedar
            # negativo — la compañera empezó antes; el Lua lo maneja).
            # Ventana REAL del traslape, para saber si cae dentro del show.
            ov_ini = max(a["t0"], b["t0"])
            ov_fin = min(a["t0"] + a["dur"], b["t0"] + b["dur"])
            en_show = any(w0 < ov_fin and ov_ini < w1 for w0, w1 in show_windows)
            if verified:
                basis = "contenido"
            elif args.trust_clock_in_windows and en_show:
                basis = "reloj-show"
                n_reloj_show += 1
            else:
                basis = ""
            if base_tokens:
                if is_base(b["grp"]) and not is_base(a["grp"]):
                    a, b, delta = b, a, -delta
            elif delta < -0.5:
                a, b, delta = b, a, -delta
            pairs.append({"a": a, "b": b, "delta": delta, "overlap": overlap,
                          "verified": verified, "basis": basis,
                          "place": bool(basis)})

    out = Path(args.out).expanduser()
    with out.open("w") as f:
        f.write("-- Pares multicámara v3 (export_multicam_lua.py)\n")
        f.write("-- delta    = s que b empieza después de a.\n")
        f.write("-- verified = delta corroborado por envelope A1<->A1 (contenido).\n")
        f.write("-- basis    = 'contenido' | 'reloj-show' | '' (no colocar).\n")
        f.write("-- place    = true si el Lua debe colocarlo. Los 'reloj-show'\n")
        f.write("--            llevan marcador Yellow: alineados por reloj dentro\n")
        f.write("--            del show, no confirmados por contenido.\n")
        f.write("return {\n")
        f.write("  skews = {\n")
        for grp, sk in sorted(skew_med.items()):
            f.write(f"    [{lua_str(grp)}] = {sk:.3f},\n")
        f.write("  },\n")
        f.write(f"  base_group = {lua_str(args.base_group or '')},\n")
        f.write("  -- track_order: V1/A1, V2/A2, V3/A3... por CANTIDAD DE MATERIAL\n")
        f.write("  -- (regla del usuario 2026-08-03). El Lua asigna las pistas con esto.\n")
        f.write("  track_order = {"
                + ", ".join(lua_str(g) for g in track_order) + "},\n")
        f.write("  pairs = {\n")
        for p in pairs:
            f.write(f"    {{a={lua_str(p['a']['path'])}, b={lua_str(p['b']['path'])}, "
                    f"delta={p['delta']:.3f}, overlap={p['overlap']:.1f}, "
                    f"verified={'true' if p['verified'] else 'false'}, "
                    f"place={'true' if p['place'] else 'false'}, "
                    f"basis={lua_str(p['basis'])}}},\n")
        f.write("  },\n}\n")

    n_place = sum(1 for p in pairs if p["place"])
    print(f"\nPares multicámara: {len(pairs)} -> {out}")
    print(f"  verificados A1<->A1: {n_refined} | sin verificar (peak débil): "
          f"{n_unverified} | refinamiento descartado (salto>max): {n_dropped}")
    print(f"  se colocan: {n_place}  (contenido {n_refined} + reloj-show "
          f"{n_reloj_show}) | no se colocan: {len(pairs) - n_place}")
    for p in pairs[:12]:
        v = "✓" if p["verified"] else ("~" if p["place"] else "?")
        print(f"  {v} {p['a']['fn']} + {p['b']['fn']} delta={p['delta']:+.2f}s "
              f"ov={p['overlap']:.0f}s")
    if len(pairs) > 12:
        print(f"  ... y {len(pairs)-12} más")


if __name__ == "__main__":
    main()
