"""Registro de camaras — UNICA fuente de verdad sobre que es cada clip.

Antes de este modulo la clasificacion de camara estaba escrita cinco veces con
contenidos YA divergentes (mas tres variantes menores):

    bin/transcribe_clips.py      MVI_/BLAC/VICG, 6D, FX30, a6700
    bin/sync_transcript.py       MVI_/BLAC/VICG, 6D, FX30          (sin a6700)
    bin/sync_waveform.py         MVI_/BLAC, 6D          (sin VICG, FX30, a6700)
    bin/export_lua_data.py       BLAC/MVI_/VICG, 6D, FX30
    bin/enrich_curated_segments.py  BLAC/MVI_, 6D            (sin VICG)

Consecuencia concreta y verificada: el material de FX30 y a6700 NUNCA entraba a
`sync_waveform.py` porque su SQL no los conocia. Agregar una camara era editar
ocho archivos y acordarse de todos.

Ahora: los perfiles viven en `config/camera_profiles.json` (motor) y se pueden
sobreescribir por proyecto en `<disco>/.cinema_assistant/camera_profiles.json`.
Todo consumidor pasa por aqui.

USO TIPICO

    from lib import cameras
    perfil = cameras.classify(filename, camera_model, camera_make, ext)
    if perfil.role == "main": ...

    # Para los scripts que filtran en SQL:
    sql = cameras.main_cam_sql()            # -> "(upper(camera_model) IN (...) OR ...)"
    conn.execute(f"SELECT id FROM clips WHERE file_kind='video' AND {sql}")
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Iterable, Optional

ENGINE = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = ENGINE / "config" / "camera_profiles.json"

# Roles historicos del motor. NO renombrar sin migrar todos los consumidores:
# `is_interview()`, `derive_camera_angle.heuristic()`, `derive_shot_value.heuristic()`
# y `describe_segments_local_llm` comparan contra estas cadenas.
ROLES = ("main", "gopro", "drone", "other")


class Profile:
    """Un perfil de camara resuelto."""

    __slots__ = ("id", "label", "role", "role_label", "priority", "match",
                 "decodable", "probe", "sidecar", "audio", "note")

    def __init__(self, d: dict):
        self.id = d.get("id") or "?"
        self.label = d.get("label") or self.id
        self.role = d.get("role") or "other"
        self.role_label = d.get("role_label") or self.role
        self.priority = int(d.get("priority", 100))
        self.match = d.get("match") or {}
        self.decodable = bool(d.get("decodable", True))
        self.probe = d.get("probe") or "ffprobe"
        self.sidecar = d.get("sidecar")
        self.audio = d.get("audio") or {}
        self.note = d.get("note") or ""

    @property
    def scratch_audio(self) -> bool:
        """El cuerpo graba audio de referencia utilizable para sincronizar."""
        return bool(self.audio.get("scratch"))

    def __repr__(self) -> str:  # pragma: no cover - solo depuracion
        return f"<Profile {self.id} role={self.role} prio={self.priority}>"


class Registry:
    """Perfiles ordenados por precedencia + el fallback."""

    def __init__(self, profiles: list, fallback: dict):
        # Orden estable: primero por priority, y a igual priority se respeta el
        # orden del archivo. Primer match gana.
        self.profiles = sorted(profiles, key=lambda p: p.priority)
        self.fallback = Profile(fallback)

    def __iter__(self):
        return iter(self.profiles)

    def __len__(self) -> int:
        return len(self.profiles)

    def by_id(self, pid: str) -> Optional[Profile]:
        for p in self.profiles:
            if p.id == pid:
                return p
        return None

    def with_role(self, role: str) -> list:
        return [p for p in self.profiles if p.role == role]


# --------------------------------------------------------------------------
# Carga
# --------------------------------------------------------------------------

_CACHE: dict = {}


def _read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"Registro de camaras ilegible: {path}: {e}") from e


def load_profiles(disk_root=None, *, registry_path=None) -> Registry:
    """Carga el registro del motor y le fusiona el override del proyecto.

    Fusion por `id`: mismo id reemplaza el perfil completo; id nuevo se agrega.
    Asi un proyecto puede corregir una camara sin tocar el motor ni el plugin.
    """
    base_path = Path(registry_path) if registry_path else DEFAULT_REGISTRY
    override_path = None
    if disk_root is not None:
        override_path = Path(disk_root) / ".cinema_assistant" / "camera_profiles.json"

    key = (str(base_path), str(override_path) if override_path else "")
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    base = _read_json(base_path)
    if not base.get("profiles"):
        raise ValueError(f"Registro de camaras sin perfiles: {base_path}")

    by_id = {}
    order = []
    for d in base["profiles"]:
        pid = d.get("id")
        if not pid:
            raise ValueError(f"Perfil sin 'id' en {base_path}")
        if pid in by_id:
            raise ValueError(f"Perfil duplicado '{pid}' en {base_path}")
        by_id[pid] = d
        order.append(pid)

    fallback = base.get("fallback") or {"id": "otro", "role": "other"}

    if override_path is not None and override_path.exists():
        ov = _read_json(override_path)
        for d in ov.get("profiles", []) or []:
            pid = d.get("id")
            if not pid:
                continue
            if pid not in by_id:
                order.append(pid)
            by_id[pid] = d
        if ov.get("fallback"):
            fallback = ov["fallback"]

    reg = Registry([Profile(by_id[p]) for p in order], fallback)
    _CACHE[key] = reg
    return reg


def clear_cache() -> None:
    """Para las pruebas y para releer tras editar un registro."""
    _CACHE.clear()


# --------------------------------------------------------------------------
# Clasificacion
# --------------------------------------------------------------------------

def _norm(s) -> str:
    return (s or "").strip().upper()


def _matches(profile: Profile, filename: str, model: str, make: str, ext: str) -> bool:
    """Clausulas OR: si CUALQUIERA acierta, el perfil aplica.

    Es la semantica del codigo que reemplaza
    (`fn.startswith("BLAC") or ... or camera_model == "Canon EOS 6D"`).
    """
    m = profile.match
    fn, mo, mk, ex = _norm(filename), _norm(model), _norm(make), _norm(ext)

    for want in m.get("camera_model", []) or []:
        if mo and mo == _norm(want):
            return True
    for pat in m.get("camera_model_regex", []) or []:
        if mo and re.search(pat, mo, re.IGNORECASE):
            return True
    for pfx in m.get("filename_prefix", []) or []:
        if fn and fn.startswith(_norm(pfx)):
            return True
    # `filename_glob` usa la MISMA sintaxis que el GLOB de SQLite (incluidas
    # las clases [PHX] y [0-9]), asi que Python y SQL coinciden exactamente.
    # Preferirlo sobre `filename_regex`, que no se puede llevar a SQL.
    for pat in m.get("filename_glob", []) or []:
        if fn and fnmatch.fnmatchcase(fn, _norm(pat)):
            return True
    for pat in m.get("filename_regex", []) or []:
        if fn and re.search(pat, fn, re.IGNORECASE):
            return True
    for want in m.get("make", []) or []:
        if mk and mk == _norm(want):
            return True
    for want in m.get("ext", []) or []:
        w = _norm(want)
        if not w.startswith("."):
            w = "." + w
        if ex and (ex if ex.startswith(".") else "." + ex) == w:
            return True
    return False


def classify(filename=None, camera_model=None, camera_make=None, ext=None,
             *, registry=None, disk_root=None) -> Profile:
    """Devuelve el Profile del clip. Nunca None: cae al fallback."""
    reg = registry or load_profiles(disk_root)
    for p in reg:
        if _matches(p, filename or "", camera_model or "", camera_make or "", ext or ""):
            return p
    return reg.fallback


def classify_row(row, *, registry=None, disk_root=None) -> Profile:
    """Como `classify` pero desde un dict / sqlite3.Row con las columnas del manifest."""
    def g(k):
        try:
            return row[k]
        except (KeyError, IndexError, TypeError):
            return None
    return classify(g("filename"), g("camera_model"), g("camera_make"), g("ext"),
                    registry=registry, disk_root=disk_root)


def role_of(filename=None, camera_model=None, camera_make=None, ext=None,
            *, registry=None, disk_root=None) -> str:
    """main | gopro | drone | other — reemplazo directo de las viejas
    `classify_camera()` / `detect_camera()`."""
    return classify(filename, camera_model, camera_make, ext,
                    registry=registry, disk_root=disk_root).role


# --------------------------------------------------------------------------
# Generacion de SQL
# --------------------------------------------------------------------------

# Claves que SI se pueden expresar en el WHERE de SQLite. `camera_model_regex`
# y `filename_regex` no: SQLite no trae REGEXP por defecto.
SQL_KEYS = ("camera_model", "filename_prefix", "filename_glob", "make", "ext")
SQL_UNSAFE_KEYS = ("camera_model_regex", "filename_regex")


def unrepresentable_in_sql(role: str = "main", *, registry=None, disk_root=None) -> list:
    """Perfiles de ese rol que NO se pueden filtrar en SQL.

    Un perfil solo es problematico si TODAS sus clausulas son regex: si ademas
    tiene modelos o prefijos, el SQL lo captura parcialmente y el resto lo
    resuelve `classify()` en Python.
    """
    reg = registry or load_profiles(disk_root)
    malos = []
    for p in reg.with_role(role):
        tiene_sql = any(p.match.get(k) for k in SQL_KEYS)
        tiene_regex = any(p.match.get(k) for k in SQL_UNSAFE_KEYS)
        if tiene_regex and not tiene_sql:
            malos.append(p.id)
    return malos


def _sql_list(values: Iterable) -> str:
    out = []
    for v in values:
        s = str(v).strip().upper().replace("'", "''")
        out.append(f"'{s}'")
    return ", ".join(out)


def role_sql(role: str = "main", *, registry=None, disk_root=None, prefix: str = "") -> str:
    """WHERE de SQLite que selecciona los clips de ese rol.

    `prefix` es el alias de la tabla con punto, p.ej. "c." — util en JOINs.
    Devuelve "(...)" listo para concatenar con AND, o "(1=0)" si el rol no
    tiene perfiles expresables (mejor no procesar nada que procesar todo).
    """
    reg = registry or load_profiles(disk_root)
    p_ = prefix
    modelos, prefijos, globs, makes, exts = set(), set(), set(), set(), set()

    for p in reg.with_role(role):
        for v in p.match.get("camera_model", []) or []:
            modelos.add(str(v))
        for v in p.match.get("filename_prefix", []) or []:
            prefijos.add(str(v))
        for v in p.match.get("filename_glob", []) or []:
            globs.add(str(v))
        for v in p.match.get("make", []) or []:
            makes.add(str(v))
        for v in p.match.get("ext", []) or []:
            s = str(v)
            exts.add(s if s.startswith(".") else "." + s)

    partes = []
    if modelos:
        partes.append(f"upper({p_}camera_model) IN ({_sql_list(sorted(modelos))})")
    # Los prefijos pueden tener largos distintos: un substr() por largo.
    por_largo = {}
    for pf in prefijos:
        por_largo.setdefault(len(pf), set()).add(pf)
    for largo in sorted(por_largo):
        partes.append(
            f"substr(upper({p_}filename),1,{largo}) IN ({_sql_list(sorted(por_largo[largo]))})"
        )
    for g in sorted(globs):
        esc = str(g).upper().replace("'", "''")
        partes.append(f"upper({p_}filename) GLOB '{esc}'")
    if makes:
        partes.append(f"upper({p_}camera_make) IN ({_sql_list(sorted(makes))})")
    if exts:
        partes.append(f"lower({p_}ext) IN ({_sql_list(sorted(exts)).lower()})")

    if not partes:
        return "(1=0)"
    return "(" + " OR ".join(partes) + ")"


def main_cam_sql(*, registry=None, disk_root=None, prefix: str = "") -> str:
    """Reemplaza los `MAIN_CAM` / `MAIN_CAM_SQL` hardcodeados de los scripts."""
    return role_sql("main", registry=registry, disk_root=disk_root, prefix=prefix)


def excluded_from_sync_sql(*, registry=None, disk_root=None, prefix: str = "") -> str:
    """GoPro y drone: traen audio embebido utilizable y no se sincronizan.
    Doctrina `sync_waveform.py`."""
    reg = registry or load_profiles(disk_root)
    a = role_sql("gopro", registry=reg, prefix=prefix)
    b = role_sql("drone", registry=reg, prefix=prefix)
    return f"({a} OR {b})"


def _sample_rows(registry) -> list:
    """Filas sinteticas: un caso por cada valor declarado en el registro.

    Sirve para comprobar que el SQL (OR plano, sin precedencia) y `classify()`
    (primer match por prioridad) no se contradicen.
    """
    filas = []
    for p in registry:
        m = p.match
        for v in m.get("camera_model", []) or []:
            filas.append({"filename": "X.MOV", "camera_model": v,
                          "camera_make": "", "ext": ".mov", "_from": p.id})
        for v in m.get("filename_prefix", []) or []:
            filas.append({"filename": f"{v}0001.MOV", "camera_model": "",
                          "camera_make": "", "ext": ".mov", "_from": p.id})
        for v in m.get("filename_glob", []) or []:
            # Materializa el glob en un nombre concreto: G[PHX][0-9][0-9]* -> GP01xxx
            concreto, i = [], 0
            while i < len(v):
                ch = v[i]
                if ch == "[":
                    j = v.index("]", i)
                    clase = v[i + 1:j]
                    concreto.append("0" if clase.startswith("0-9") else clase[0])
                    i = j + 1
                elif ch == "*":
                    concreto.append("0001")
                    i += 1
                else:
                    concreto.append(ch)
                    i += 1
            filas.append({"filename": "".join(concreto) + ".MOV", "camera_model": "",
                          "camera_make": "", "ext": ".mov", "_from": p.id})
        for v in m.get("make", []) or []:
            filas.append({"filename": "X.MOV", "camera_model": "",
                          "camera_make": v, "ext": ".mov", "_from": p.id})
        for v in m.get("ext", []) or []:
            e = v if v.startswith(".") else "." + v
            filas.append({"filename": f"X{e}", "camera_model": "",
                          "camera_make": "", "ext": e, "_from": p.id})
    return filas


def _sql_would_match(role: str, fila: dict, registry) -> bool:
    """Semantica del WHERE que emite `role_sql`: OR plano sobre todos los
    perfiles del rol, sin precedencia."""
    modelos, prefijos, globs, makes, exts = set(), set(), set(), set(), set()
    for p in registry.with_role(role):
        for v in p.match.get("camera_model", []) or []:
            modelos.add(_norm(v))
        for v in p.match.get("filename_prefix", []) or []:
            prefijos.add(_norm(v))
        for v in p.match.get("filename_glob", []) or []:
            globs.add(_norm(v))
        for v in p.match.get("make", []) or []:
            makes.add(_norm(v))
        for v in p.match.get("ext", []) or []:
            s = str(v)
            exts.add(_norm(s if s.startswith(".") else "." + s))

    fn = _norm(fila.get("filename"))
    if _norm(fila.get("camera_model")) in modelos and fila.get("camera_model"):
        return True
    for pf in prefijos:
        if fn[:len(pf)] == pf:
            return True
    for g in globs:
        if fnmatch.fnmatchcase(fn, g):
            return True
    if _norm(fila.get("camera_make")) in makes and fila.get("camera_make"):
        return True
    if _norm(fila.get("ext")) in exts and fila.get("ext"):
        return True
    return False


def sql_python_conflicts(role: str = "main", *, registry=None, disk_root=None) -> list:
    """Casos donde el SQL de `role_sql(role)` y `classify()` NO coinciden.

    El SQL es un OR plano; `classify()` respeta la prioridad. Si un perfil de
    otro rol con prioridad mas alta reclama una fila que el SQL de este rol
    tambien selecciona, el pipeline procesaria clips que el resto del motor
    considera de otra camara. La prueba `tests/test_camaras.py` exige que esta
    lista este vacia — es el guard que evita que el registro se desincronice
    de sus consumidores.
    """
    reg = registry or load_profiles(disk_root)
    conflictos = []
    for fila in _sample_rows(reg):
        en_sql = _sql_would_match(role, fila, reg)
        real = classify(fila["filename"], fila["camera_model"],
                        fila["camera_make"], fila["ext"], registry=reg)
        if en_sql != (real.role == role):
            conflictos.append({
                "fila": {k: v for k, v in fila.items() if k != "_from"},
                "declarado_por": fila["_from"],
                "sql_selecciona": en_sql,
                "classify_dice": real.role,
                "perfil_ganador": real.id,
            })
    return conflictos


# --------------------------------------------------------------------------
# Comparacion con el comportamiento anterior (para la migracion)
# --------------------------------------------------------------------------

def legacy_classify(filename, camera_model) -> str:
    """La `classify_camera()` que vivia en `bin/export_lua_data.py` antes del
    registro. Se conserva SOLO para que `inventario_camaras.py --comparar-legacy`
    pueda mostrar exactamente que clips cambian de rol. No usar en el pipeline.
    """
    fn = (filename or "").upper()
    if fn.startswith("DJI"):
        return "drone"
    if fn.startswith("GOPR") or (fn[:2] in ("GP", "GH", "GX") and fn[2:4].isdigit()):
        return "gopro"
    if (fn.startswith("BLAC") or fn.startswith("MVI_") or fn.startswith("VICG")
            or camera_model == "Canon EOS 6D" or camera_model == "ILME-FX30"):
        return "main"
    return "other"
