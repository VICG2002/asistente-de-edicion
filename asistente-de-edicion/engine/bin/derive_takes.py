#!/usr/bin/env python3
"""Agrupa las tomas que dicen el MISMO texto y marca las que se cortaron.

En un rodaje con guion —un comercial, una capsula, cualquier pieza con texto a
camara— la misma frase se graba muchas veces. El editor no necesita 122 clips:
necesita saber que diez de ellos son la misma toma repetida y cuales de esas
diez sirven. Sin eso, la primera hora de trabajo se va en abrir clips para
descubrir que ya los habia visto.

Salio del rodaje de una joyeria (2026-08-06): 47 clips con dialogo que resultaron
ser 24 textos distintos, con un bloque de 10 tomas y otro de 9. Cuatro de esos
clips eran arranques fallidos que se cantan solos en el transcript —"No,
esperate. Yo eso no tenia que leerlo"— y el motor no tenia forma de verlos.

QUE HACE Y QUE NO HACE. Agrupa, mide y marca. NO elige la mejor toma: eso mira
interpretacion, foco, luz y continuidad, y lo firma el editor. Lo que hace es
dejar el trabajo mecanico hecho y las descartables señaladas con su motivo
citando la frase que lo delata.

Uso:
    python3 bin/derive_takes.py --root <disco>
    python3 bin/derive_takes.py --root <disco> --dry-run
    python3 bin/derive_takes.py --root <disco> --umbral 0.85
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from lib import manifest  # noqa: E402
from lib.guards import assert_selected, report_done  # noqa: E402

# Palabras del INICIO que se comparan para decidir si dos clips son la misma
# toma. Poco: una toma se identifica por como empieza. Comparar el texto entero
# separaria un arranque fallido de 8 palabras de la toma buena de 200, que es
# justo lo que hay que emparejar.
PALABRAS_CABEZA = 12
UMBRAL_DEFECTO = 0.80

# Una toma con menos de este porcentaje del texto canonico del grupo se quedo
# corta. No es un juicio de calidad: es que no llego al final.
RATIO_COMPLETA = 0.70

# Lo que dice alguien cuando una toma se cae. Conservador a proposito: cada
# entrada tiene que ser inequivoca en un set. "A ver" o "espera" sueltos son
# demasiado comunes en conversacion normal para usarlos.
MARCADORES_FALLO = [
    (r"\bno,?\s+esp[eé]rate\b",        "alguien corta la toma"),
    (r"\bva de nuevo\b",               "se reinicia la toma"),
    (r"\botra vez,?\s+(?:por favor|va)\b", "se pide repetir"),
    (r"\bdesde el (?:principio|inicio)\b", "se reinicia desde el principio"),
    (r"\bperd[oó]n[.,]?\s*(?:bueno|va|otra|de nuevo)\b", "se disculpa y corta"),
    (r"\beso no (?:lo )?ten[ií]a que (?:leer|decir)\b", "lectura equivocada"),
    (r"\bme equivoqu[eé]\b",           "se equivoca"),
    (r"\bcorte\b",                     "se canta corte"),
    (r"\bempezamos de nuevo\b",        "se reinicia"),
]

ESQUEMA = """
CREATE TABLE IF NOT EXISTS take_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    texto_canonico TEXT,       -- la version mas completa del grupo
    n_tomas INTEGER,
    dur_total_sec REAL,
    primera_toma_at TEXT,      -- creation_time de la primera
    created_at REAL
);
CREATE TABLE IF NOT EXISTS clip_takes (
    clip_id INTEGER PRIMARY KEY,
    group_id INTEGER,
    take_num INTEGER,          -- orden cronologico dentro del grupo
    similitud REAL,            -- contra el texto canonico
    ratio_longitud REAL,       -- cuanto del canonico alcanzo a decir
    completa INTEGER,          -- llego al final
    marcador TEXT,             -- motivo si se detecto un fallo, en español
    cita TEXT,                 -- la frase literal que lo delata
    entrar_desde_sec REAL,     -- si el corte esta al inicio, desde donde sirve
    FOREIGN KEY (clip_id) REFERENCES clips(id),
    FOREIGN KEY (group_id) REFERENCES take_groups(id)
);
CREATE INDEX IF NOT EXISTS idx_clip_takes_group ON clip_takes(group_id);
"""


def normalizar(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]", " ", s).split()


def cabeza(texto: str, n: int = PALABRAS_CABEZA) -> str:
    return " ".join(normalizar(texto)[:n])


def detectar_fallo(texto: str, palabras: list | None = None) -> tuple[str, str, float]:
    """(motivo, cita, posicion 0..1) del ULTIMO marcador de toma cortada.

    Se busca el ULTIMO, no el primero: si en un clip se corta dos veces y luego
    sale bien, lo que importa es donde empieza lo aprovechable.

    La POSICION es lo que vuelve util el dato. Un marcador al principio no
    invalida el clip: significa que el equipo charlo, se reinicio, y la toma
    buena viene despues. Caso real de C1178 —"Va, entonces, otra vez.
    ¿Empezamos desde el inicio?"— que trae ademas la toma completa detras.
    Marcarlo como descarte habria tirado la mejor toma del grupo.
    """
    plano = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    mejor = None
    for patron, motivo in MARCADORES_FALLO:
        for m in re.finditer(patron, plano, re.IGNORECASE):
            if mejor is None or m.start() > mejor[0].start():
                mejor = (m, motivo)
    if mejor is None:
        return "", "", 0.0
    m, motivo = mejor
    ini = max(0, m.start() - 40)
    cita = texto[ini:m.end() + 40].strip().replace("\n", " ")
    return motivo, f"...{cita}...", m.end() / max(1, len(plano))


def segundo_de_posicion(palabras: list, pos: float) -> float | None:
    """El segundo del clip que corresponde a una posicion 0..1 del texto.

    Whisper ya da el timestamp de cada palabra (`-ml 1 -sow`); esto solo lo
    aprovecha. Sirve para decirle al editor "entra en el segundo 23" en vez de
    "este clip tiene un reinicio en alguna parte"."""
    if not palabras:
        return None
    idx = min(len(palabras) - 1, max(0, int(round(pos * (len(palabras) - 1)))))
    w = palabras[idx]
    # El motor guarda [palabra, t]; se acepta dict por si el formato cambia.
    if isinstance(w, (list, tuple)) and len(w) >= 2:
        return float(w[1]) if isinstance(w[1], (int, float)) else None
    if isinstance(w, dict):
        for k in ("s", "start", "t", "time"):
            if isinstance(w.get(k), (int, float)):
                return float(w[k])
    return None


def agrupar(clips: list[dict], umbral: float) -> list[list[dict]]:
    """Agrupa por parecido del arranque.

    Se compara contra el miembro MAS LARGO del grupo, no contra el primero: el
    primero suele ser un arranque fallido y corto, y usarlo de referencia parte
    el grupo en dos. Con el mas largo, la toma buena atrae a las fallidas.
    """
    grupos: list[list[dict]] = []
    for c in clips:
        mejor_g, mejor_r = None, 0.0
        for g in grupos:
            ref = max(g, key=lambda x: len(x["texto"]))
            r = difflib.SequenceMatcher(None, cabeza(ref["texto"]),
                                        cabeza(c["texto"])).ratio()
            if r > mejor_r:
                mejor_g, mejor_r = g, r
        if mejor_g is not None and mejor_r >= umbral:
            c["_sim"] = mejor_r
            mejor_g.append(c)
        else:
            c["_sim"] = 1.0
            grupos.append([c])
    return grupos


def aplicar_overrides(grupos: list[list[dict]], overrides: dict) -> list[str]:
    """Mueve clips de grupo segun lo declarado por el editor. Devuelve el log.

    POR QUE EXISTE (2026-08-18, cobertura de agosto dia 2). El agrupador compara las
    12 primeras palabras, y eso falla en los dos sentidos justo donde mas duele:

      · PARTE una pieza cuando la toma arranca con ruido de set. C1297 dice el
        texto entero del reel de la marca, pero empieza con "Esperame, esperame,
        porque lo tengo que subir. Ahi voy. Ahi va." — la cabeza no se parece a
        la de sus seis hermanas y quedo en un grupo de una sola toma. Igual el
        reel de Pedro Friedeberg, partido en tres porque un intento empieza por
        la mitad del texto ("Y ahora esa historia se cruza con otra").
      · Y podria UNIR dos piezas que se abren parecido.

    Bajar el umbral no es la respuesta: arregla un caso y rompe otro. Esto es
    conocimiento del editor, que ES el que sabe que dos clips son la misma
    pieza, y se declara igual que `reel_overrides`:

        "take_overrides": {"C1297.MP4": "C1295.MP4", "C1234.MP4": null}

    Valor = el clip ANCLA con cuyo grupo se junta. `null` lo saca a un grupo
    propio. Un nombre que no existe en el material NO se ignora: se avisa, que
    es como se caza un override que dejo de aplicar tras un recorte.
    """
    log = []
    por_fn = {}
    for g in grupos:
        for c in g:
            por_fn[c["fn"]] = g

    for fn, ancla_fn in overrides.items():
        g = por_fn.get(fn)
        if g is None:
            log.append(f"⚠ take_override '{fn}': ese clip no esta en el material")
            continue
        if ancla_fn is None:
            if len(g) == 1:
                log.append(f"· {fn}: ya estaba solo, nada que separar")
                continue
            g.remove(fn_c := next(c for c in g if c["fn"] == fn))
            grupos.append([fn_c]); por_fn[fn] = grupos[-1]
            log.append(f"· {fn}: separado a grupo propio")
            continue
        destino = por_fn.get(ancla_fn)
        if destino is None:
            log.append(f"⚠ take_override '{fn}' -> '{ancla_fn}': el ancla no "
                       f"esta en el material")
            continue
        if destino is g:
            log.append(f"· {fn}: ya estaba con {ancla_fn}")
            continue
        c = next(x for x in g if x["fn"] == fn)
        g.remove(c); destino.append(c); por_fn[fn] = destino
        log.append(f"· {fn} -> al grupo de {ancla_fn}")

    grupos[:] = [g for g in grupos if g]
    return log


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--umbral", type=float, default=UMBRAL_DEFECTO,
                    help=f"similitud minima del arranque (default {UMBRAL_DEFECTO})")
    ap.add_argument("--min-chars", type=int, default=60,
                    help="ignora transcripts mas cortos que esto (default 60)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"Disco no encontrado: {root}")
    ca = root / ".cinema_assistant"
    db = ca / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"Manifest no encontrado: {db}. Corre antes index_project.py.")

    conn = manifest.conectar(str(db))
    conn.executescript(ESQUEMA)
    # `CREATE TABLE IF NOT EXISTS` no añade columnas a una tabla que ya existe:
    # un manifest de una corrida anterior se queda sin las nuevas y el INSERT
    # revienta. Mismo patron de migracion que lib/manifest.py.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(clip_takes)")}
    for col, tipo in (("entrar_desde_sec", "REAL"),):
        if col not in cols:
            conn.execute(f"ALTER TABLE clip_takes ADD COLUMN {col} {tipo}")
    conn.commit()

    # RELOJ CORREGIDO PARA ORDENAR LAS TOMAS (2026-08-18).
    #
    # El numero de toma es una posicion cronologica, y hasta hoy salia del
    # creation_time CRUDO. Con dos camaras cuyos relojes no coinciden, eso
    # numera mal: en el dia 2 de esa cobertura la Sony va 11 h 50 min por
    # delante de la Osmo, asi que las tres tomas de la Sony salian como T4, T5
    # y T6 detras de las tres de la Osmo, cuando en realidad son los MISMOS tres
    # intentos vistos desde la otra camara. El marcador decia "T4/6" de algo que
    # es el intento 1.
    #
    # El mismo fallo estaba latente en IMODAE, donde la A74 iba +12 h.
    skews = {}
    cfg_p = ca / "project_config.json"
    if cfg_p.exists():
        try:
            skews = json.loads(cfg_p.read_text(encoding="utf-8")).get(
                "camera_skews_manual") or {}
        except (json.JSONDecodeError, OSError):
            skews = {}

    def reloj_real(ct: str, rel: str, pf: str) -> str:
        """creation_time menos el skew declarado de su camara. ISO, ordenable."""
        if not ct or not skews:
            return ct or ""
        seg = 0.0
        for cam, v in skews.items():
            if cam and (f"/{cam}/" in f"/{rel or ''}" or (rel or "").startswith(cam + "/")
                        or (pf or "") == cam):
                seg = float(v or 0.0)
                break
        if not seg:
            return ct
        try:
            t = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except ValueError:
            return ct
        return (t - timedelta(seconds=seg)).isoformat()

    tr_dir = ca / "transcripts"
    clips = []
    for cid, fn, pf, dur, ct, rel in conn.execute(
            "SELECT id, filename, parent_folder, duration_sec, creation_time, "
            "rel_path FROM clips WHERE file_kind='video' AND index_status='ok' "
            "ORDER BY COALESCE(creation_time,''), filename"):
        ct = reloj_real(ct, rel, pf)
        p = tr_dir / f"{cid}.json"
        if not p.exists():
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        texto = (d.get("text") or "").strip()
        if len(texto) >= args.min_chars:
            clips.append({"id": cid, "fn": fn, "pf": pf, "dur": dur or 0.0,
                          "ct": ct or "", "texto": texto,
                          "words": d.get("words") or []})

    assert_selected(clips, "clips con dialogo transcrito", filters={
        "--root": str(root), "--min-chars": args.min_chars,
    }, hint="¿Corriste transcribe_clips.py? Sin transcripts no hay nada que agrupar.")

    # Re-ordenar con el reloj YA corregido: el ORDER BY del SQL usaba el crudo.
    clips.sort(key=lambda c: (c["ct"], c["fn"]))

    grupos = agrupar(clips, args.umbral)

    # Lo que el editor sabe y la cabeza de 12 palabras no puede ver.
    cfg_path = ca / "project_config.json"
    overrides = {}
    if cfg_path.exists():
        try:
            overrides = json.loads(cfg_path.read_text(encoding="utf-8")).get(
                "take_overrides") or {}
        except (json.JSONDecodeError, OSError):
            overrides = {}
    if overrides:
        print(f"take_overrides declarados: {len(overrides)}")
        for linea in aplicar_overrides(grupos, overrides):
            print("  " + linea)

    grupos.sort(key=lambda g: (-len(g), g[0]["ct"]))

    n_fallidas = n_incompletas = 0
    filas_g, filas_c = [], []
    for g in grupos:
        canon = max(g, key=lambda x: len(x["texto"]))
        g.sort(key=lambda x: (x["ct"], x["fn"]))
        filas_g.append((canon["texto"], len(g), sum(x["dur"] for x in g),
                        g[0]["ct"], time.time()))
        for i, c in enumerate(g, 1):
            ratio = len(c["texto"]) / max(1, len(canon["texto"]))
            completa = 1 if ratio >= RATIO_COMPLETA else 0
            motivo, cita, pos = detectar_fallo(c["texto"])
            entrar = salir = None
            if motivo and completa:
                # Un clip que dice el texto ENTERO y ademas trae una charla de
                # rodaje no es un descarte: es una toma buena con ruido en un
                # extremo. Segun donde este el marcador, el editor entra despues
                # o sale antes. Los dos casos aparecieron en el mismo grupo:
                # C1177 arranca con "No, esperate" y C1178 termina con
                # "¿Empezamos desde el inicio?" DESPUES de la toma completa.
                t = segundo_de_posicion(c["words"], pos)
                if t is not None:
                    if pos <= 0.5:
                        entrar = t
                    else:
                        salir = t
            if motivo:
                n_fallidas += 1
            if not completa:
                n_incompletas += 1
            c["_nota"] = (motivo, cita, entrar, salir, ratio)
            filas_c.append((c["id"], i, round(c.get("_sim", 1.0), 3),
                            round(ratio, 3), completa, motivo, cita, entrar))

    if args.dry_run:
        print(f"[dry-run] {len(clips)} clips -> {len(grupos)} textos distintos; "
              f"{n_fallidas} con marcador de fallo, {n_incompletas} incompletas.")
        return 0

    # Reemplazo completo: es un derivado del transcript, se recalcula entero.
    conn.execute("DELETE FROM clip_takes")   # lint:ok delete-global
    conn.execute("DELETE FROM take_groups")  # lint:ok delete-global
    for i, fila in enumerate(filas_g):
        cur = conn.execute(
            "INSERT INTO take_groups (texto_canonico, n_tomas, dur_total_sec, "
            "primera_toma_at, created_at) VALUES (?,?,?,?,?)", fila)
        gid = cur.lastrowid
        # las filas de clip_takes de este grupo son las len(grupos[i]) siguientes
        for c in grupos[i]:
            f = next(x for x in filas_c if x[0] == c["id"])
            conn.execute(
                "INSERT OR REPLACE INTO clip_takes (clip_id, group_id, take_num, "
                "similitud, ratio_longitud, completa, marcador, cita, "
                "entrar_desde_sec) VALUES (?,?,?,?,?,?,?,?,?)",
                (f[0], gid, f[1], f[2], f[3], f[4], f[5], f[6], f[7]))
    conn.commit()

    # Informe legible. El editor decide con esto delante; el motor no elige.
    rep_dir = ca / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    rep = rep_dir / f"tomas-{time.strftime('%Y%m%d')}.md"
    with rep.open("w", encoding="utf-8") as f:
        f.write("# Tomas agrupadas por texto\n\n")
        f.write(f"{len(clips)} clips con diálogo -> **{len(grupos)} textos "
                f"distintos**. {n_fallidas} con marcador de toma cortada.\n\n")
        f.write("El motor agrupa y señala; **la mejor toma la eliges tú**: "
                "interpretación, foco y continuidad no están aquí.\n\n")
        for gi, g in enumerate(grupos, 1):
            canon = max(g, key=lambda x: len(x["texto"]))
            f.write(f"## Grupo {gi} — {len(g)} toma(s), "
                    f"{sum(x['dur'] for x in g)/60:.1f} min\n\n")
            f.write(f"> {canon['texto'][:300]}\n\n")
            f.write("| Toma | Clip | Dur | Dice | Nota |\n|---|---|---|---|---|\n")
            for i, c in enumerate(g, 1):
                motivo, cita, entrar, salir, ratio = c.get(
                    "_nota", ("", "", None, None, 1.0))
                if entrar is not None:
                    nota = (f"**sirve desde {entrar:.0f}s** — antes hay charla "
                            f"({motivo})")
                elif salir is not None:
                    nota = (f"**sirve hasta {salir:.0f}s** — después hay charla "
                            f"({motivo})")
                elif motivo:
                    nota = f"**{motivo}** — {cita}"
                elif ratio < RATIO_COMPLETA:
                    nota = "se queda corta"
                else:
                    nota = "**completa**"
                f.write(f"| {i} | `{c['fn']}` | {c['dur']:.0f}s | "
                        f"{ratio*100:.0f}% | {nota} |\n")
            f.write("\n")

    report_done("derive_takes", grupos=len(grupos), clips=len(clips),
                con_marcador=n_fallidas, incompletas=n_incompletas)
    print(f"  informe: {rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
