#!/usr/bin/env python3
"""Limpia menciones de ropa / vestimenta / accesorios cosméticos de las
descripciones existentes en clip_curated_segments. Más barato que re-correr
el LLM (segundos vs horas).

Aplica regex sobre los campos `characters`, `what_action`, `what_stands`,
`objects` y `full_text`. Después reconstruye `full_text` desde los campos
limpios.

Solo toca `curated_by='llm'` (preserva 'claude' y 'auto').
"""

from __future__ import annotations

import argparse
import glob
import re
import sqlite3
import sys
import time
from pathlib import Path
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # noqa: E402
from lib import manifest  # noqa: E402


# Patrones a remover (mantener el contexto de la oración limpio):
#   "vistiendo una chaqueta verde y pantalones negros"
#   "con casco rojo, ropa de escalada"
#   "(camiseta amarilla, chaqueta gris, shorts cortos, zapatillas blancas)"
#   "ropa casual"
#   "accesorios visibles: cuerda, arnés"
CLOTHING_PATTERNS = [
    # Frases entre paréntesis que enumeran ropa
    r"\((?:[^)]*?(?:camiseta|playera|chaqueta|pantalones?|shorts?|zapatos?|zapatillas?|gorra|bandana|sombrero|botas|jersey|sudadera|chamarra|polera|chaleco)[^)]*?)\)",
    # "viste/vistiendo/vestido/llevando + prenda ... hasta . , o |"
    r",?\s*(?:vist[eaiéí]ndo|vestid[oa]s?|llevando|usa|usando)\s+(?:una|unos|unas|un|el|los|las|de)?\s*(?:camiseta|playera|chaqueta|pantalones?|shorts?|zapatos?|zapatillas?|gorra|bandana|sombrero|botas|ropa|sudadera|chamarra|chamarra|chaleco|polera)[^.,|]*?(?=[.,|]|$)",
    # "con camiseta verde / con casco rojo / con shorts negros / con ropa de"
    r",?\s*con\s+(?:una|unos|unas|un)?\s*(?:camiseta|playera|chaqueta|pantalones?|shorts?|zapatos?|zapatillas?|gorra|bandana|sombrero|botas|sudadera|chamarra|chaleco)\s+[^.,|]*?(?=[.,|]|$)",
    # "ropa: ..." — listado completo después de los dos puntos hasta . , o |
    r",?\s*ropa\s*:\s*[^.,|]*?(?=[.,|]|$)",
    # "ropa de escalada / ropa casual / ropa deportiva / ropa cómoda"
    r",?\s*ropa\s+(?:de\s+escalada|casual|deportiva|liviana|abrigada|cómoda|holgada|abrigad[oa])\s*",
    # "accesorios visibles: ..." o "accesorios: ..."
    r",?\s*accesorios\s*(?:visibles)?\s*:?\s*[^.,|]*?(?=[.,|]|$)",
    # "ropa de color X" / "ropa color X"
    r",?\s*ropa\s+(?:de\s+)?color\s+\w+",
    # Frases descriptivas "con/de chaqueta+color+y+pantalón+color" sin verbo
    r",?\s*(?:una|unos|unas|un)?\s*(?:camiseta|playera|chaqueta|sudadera|chamarra|chaleco|polera)\s+(?:negr[oa]|blanc[oa]|roj[oa]|azul|verde|amarilla?|naranja|gris|beige|caf[éé]|marr[óo]n|morada?|rosa|p[úu]rpura|celeste)(?:\s+y\s+(?:pantalones?|shorts?|zapatos?|zapatillas?|gorra|botas|chaqueta)\s+\w+)?(?=[.,|]|$)",
]

# Tags compilados
CLOTHING_RE = [re.compile(p, re.IGNORECASE) for p in CLOTHING_PATTERNS]

# Limpieza de fragmentos sueltos que queden colgando
CLEANUP_RES = [
    # Palabras sueltas que quedaron sin objeto
    (re.compile(r"\b(viste|vistiendo|llevando|usa|usando|tiene|lleva)\s*\.", re.IGNORECASE), "."),
    (re.compile(r"\b(viste|vistiendo|llevando|usa|usando|tiene|lleva)\s*,", re.IGNORECASE), ","),
    (re.compile(r"\b(viste|vistiendo|llevando|usa|usando|tiene|lleva)\s*$", re.IGNORECASE), ""),
    # Comas/espacios duplicados
    (re.compile(r"\s{2,}"), " "),
    (re.compile(r",\s*,+"), ","),
    (re.compile(r"\s+([.,;])"), r"\1"),
    (re.compile(r"\(\s*\)"), ""),
    (re.compile(r"[.,;]\s*[.,;]+"), "."),
    (re.compile(r"^\s*[.,;]+\s*"), ""),
    # "con," → "con" colgando, lo quitamos
    (re.compile(r"\bcon\s*,", re.IGNORECASE), ","),
    (re.compile(r"\bcon\s*\.", re.IGNORECASE), "."),
    (re.compile(r",\s*y\s*,"), ","),
    (re.compile(r"^\s*y\s+", re.IGNORECASE), ""),
    (re.compile(r"\s+\.$"), "."),
    (re.compile(r"\.\s*$"), "."),
]


def clean_clothing(text: str) -> str:
    if not text:
        return text
    out = text
    for pat in CLOTHING_RE:
        out = pat.sub("", out)
    for pat, repl in CLEANUP_RES:
        out = pat.sub(repl, out)
    return out.strip()


def resolve_root(root_arg: str) -> Path:
    p = Path(root_arg)
    if p.exists():
        return p
    m = glob.glob(root_arg + "*")
    if len(m) == 1:
        return Path(m[0])
    sys.exit(f"Root not resolvable: {root_arg}")


def compose_full_text(characters, shot, angle, what_action, where_at, objects, dialogue_idea):
    plano = f"{shot} + {angle}" if shot else (angle or "Normal")
    parts = [characters or "Escena", plano]
    accion_bits = []
    if what_action:
        accion_bits.append(what_action)
    if where_at:
        accion_bits.append(where_at)
    if objects:
        accion_bits.append(f"Objetos: {objects}")
    if dialogue_idea:
        accion_bits.append(f'Idea: "{dialogue_idea}"')
    parts.append(". ".join(b for b in accion_bits if b))
    return " | ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="Muestra cambios sin escribir")
    args = ap.parse_args()

    root = resolve_root(args.root)
    db = root / ".cinema_assistant" / "manifest.sqlite"
    if not db.exists():
        sys.exit(f"No manifest at {db}")

    conn = manifest.conectar(str(db))
    rows = conn.execute("""
        SELECT id, characters, shot_value, angle, what_action, what_stands,
               where_at, objects, dialogue_idea, full_text
        FROM clip_curated_segments
        WHERE curated_by = 'llm'
    """).fetchall()

    n_changed = 0
    samples = []
    for (rid, chars, shot, angle, action, stands, where_at,
         objects, dialog, full) in rows:
        new_chars = clean_clothing(chars)
        new_action = clean_clothing(action)
        new_stands = clean_clothing(stands)
        new_where = clean_clothing(where_at)
        new_objects = clean_clothing(objects)
        new_full = compose_full_text(new_chars, shot, angle, new_action,
                                     new_where, new_objects, dialog)
        changed = (new_chars != chars or new_action != action or
                   new_stands != stands or new_where != where_at or
                   new_objects != objects or new_full != full)
        if changed:
            n_changed += 1
            if len(samples) < 5:
                samples.append((rid, full[:120], new_full[:120]))
            if not args.dry_run:
                conn.execute(
                    "UPDATE clip_curated_segments SET characters=?, what_action=?, "
                    "what_stands=?, where_at=?, objects=?, full_text=? WHERE id=?",
                    (new_chars, new_action, new_stands, new_where, new_objects,
                     new_full, rid)
                )

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"Tramos modificados: {n_changed} / {len(rows)}")
    print()
    print("Muestras (antes → después):")
    for rid, before, after in samples:
        print(f"  [{rid}]")
        print(f"   antes: {before}")
        print(f"   despu: {after}")
        print()


if __name__ == "__main__":
    main()
