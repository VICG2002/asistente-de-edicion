"""Fusión multi-señal de sync — convergencia eleva confianza.

Caso fundador (Zezzions 2026-05-26): el sync de 2645 (ENTREVISTADO_13 y ENTREVISTADO_10) tenía:
  envelope:    off=-19.4  conf=0.24
  envelope:    off=-19.3  conf=0.19
  chromaprint: off=-19.9  conf=0.12
  chromaprint: off=-19.9  conf=0.09
Cada método individualmente quedaba debajo del umbral 0.30. Pero las 4
medidas convergen en offset≈-19.5s ±0.6s. Esa convergencia entre métodos
INDEPENDIENTES (envelope=energía RMS; chromaprint=espectro perceptual; +
phrases=texto; + questions=preguntas) es señal de alta confianza incluso
si cada uno por separado se rechazaría.

Doctrina: si N métodos coinciden en offset ±2s con conf>=0.10 cada uno,
subir score combinado a min(N*0.25, 0.85). Esto reduce falsos negativos
sin abrir la puerta a falsos positivos (un solo método con conf=0.20
queda rechazado, pero dos coincidiendo se promueven).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable


CLUSTER_TOLERANCE_SEC = 2.0       # offsets dentro de ±2s son "el mismo"
MIN_PER_METHOD_CONF = 0.08         # señal mínima para contar en convergencia
CONVERGENCE_BASE = 0.25            # cada voto añade 0.25
CONVERGENCE_CEILING = 0.95         # tope para no inventar certeza absoluta

# Métodos "fuertes" (semánticos / contenido) — un solo voto puede bastar
# si su conf >= STRONG_SINGLE_FLOOR. Resemblan el video literalmente.
STRONG_METHODS = frozenset({"transcript", "phrase-match", "phrases",
                             "question-anchor", "questions", "lip-sync"})
STRONG_SINGLE_FLOOR = 0.50

# Métodos "débiles" (acústicos genéricos) — propensos a falsos positivos.
# Un solo voto NO basta sin importar el conf — exige convergencia con otro.
WEAK_METHODS = frozenset({"envelope", "chromaprint", "waveform-envelope",
                          "diarization", "acoustic"})
WEAK_SINGLE_FLOOR = 0.95  # casi imposible — efectivamente exige convergencia


@dataclass
class SyncSignal:
    method: str          # "transcript" | "questions" | "phrases" | "envelope" | "chromaprint" | "diarization" | "face" | ...
    offset: float        # segundos, offset = audio_start - video_start
    conf: float          # [0..1] confianza individual
    notes: str = ""


@dataclass
class FusedSync:
    offset: float
    conf: float
    votes: int              # cuántas señales caen en el cluster ganador
    methods: list[str]      # qué métodos componen el cluster ganador
    diverged: bool          # True si cluster ganador < 50% de todas las señales
    rejected_signals: list[SyncSignal] = field(default_factory=list)
    notes: str = ""


def cluster_signals(signals: Iterable[SyncSignal], tol: float = CLUSTER_TOLERANCE_SEC) -> list[list[SyncSignal]]:
    """Agrupa señales por offsets cercanos. Greedy en orden de conf desc."""
    ordered = sorted(signals, key=lambda s: -s.conf)
    clusters: list[list[SyncSignal]] = []
    for s in ordered:
        if s.conf < MIN_PER_METHOD_CONF:
            continue
        placed = False
        for cl in clusters:
            ref_offset = sum(x.offset for x in cl) / len(cl)
            if abs(s.offset - ref_offset) <= tol:
                cl.append(s)
                placed = True
                break
        if not placed:
            clusters.append([s])
    return clusters


def fuse(signals: list[SyncSignal]) -> FusedSync | None:
    """Combina señales en una decisión única.

    Estrategia:
      1. Cluster por offset (tol ±2s).
      2. Cluster ganador = el con más votos; empate → mayor suma de conf.
      3. Confianza combinada:
         - 1 voto: respeta SINGLE_METHOD_FLOOR (rechaza si < 0.30).
         - >= 2 votos: min(N * CONVERGENCE_BASE, CONVERGENCE_CEILING),
                       pero NUNCA menos que el max conf individual.
      4. diverged = True si cluster ganador < 50% del total de votos
         (sospechoso — métodos contradicen).
    """
    if not signals:
        return None
    clusters = cluster_signals(signals)
    if not clusters:
        return None
    clusters.sort(key=lambda cl: (-len(cl), -sum(s.conf for s in cl)))
    winner = clusters[0]
    n_votes = len(winner)
    total_votes = sum(len(cl) for cl in clusters)

    avg_offset = sum(s.offset for s in winner) / n_votes
    max_conf_in_winner = max(s.conf for s in winner)
    methods = sorted(set(s.method for s in winner))

    if n_votes == 1:
        m = winner[0].method
        c = winner[0].conf
        # Política por tipo de método:
        #  - Métodos fuertes (transcript/phrases/questions/lip-sync):
        #    aceptar si conf >= 0.50.
        #  - Métodos débiles (envelope/chromaprint/diarization): rechazar
        #    casi siempre — exigen convergencia con otra señal.
        if m in STRONG_METHODS:
            if c < STRONG_SINGLE_FLOOR:
                return None
        elif m in WEAK_METHODS:
            if c < WEAK_SINGLE_FLOOR:
                return None
        else:
            # Método desconocido: actuar como fuerte (compat con scripts viejos)
            if c < STRONG_SINGLE_FLOOR:
                return None
        fused_conf = c
    else:
        method_diversity = len(methods)
        # Bono por diversidad de método (envelope+chromaprint vale más que envelope+envelope)
        diversity_bonus = max(0, (method_diversity - 1) * 0.05)
        fused_conf = min(
            CONVERGENCE_CEILING,
            max(max_conf_in_winner, n_votes * CONVERGENCE_BASE + diversity_bonus),
        )

    # divergencia: hay otro cluster con al menos tantos votos como el ganador.
    # Empate 2-2 cuenta como divergente (señales no se ponen de acuerdo).
    diverged = total_votes > n_votes and (n_votes / total_votes) <= 0.5

    rejected = [s for cl in clusters[1:] for s in cl]
    notes_parts = [
        f"votos={n_votes}/{total_votes}",
        f"métodos={','.join(methods)}",
    ]
    if diverged:
        notes_parts.append("DIVERGENTE: cluster ganador < 50%")

    return FusedSync(
        offset=avg_offset,
        conf=fused_conf,
        votes=n_votes,
        methods=methods,
        diverged=diverged,
        rejected_signals=rejected,
        notes="; ".join(notes_parts),
    )


# --- Self-test cuando se ejecuta directamente ---
if __name__ == "__main__":
    # Caso Zezzions 2645 (ENTREVISTADO_13 y ENTREVISTADO_10): 4 señales convergen en -19.5s
    sigs_2645 = [
        SyncSignal("envelope", -19.4, 0.24),
        SyncSignal("envelope", -19.3, 0.19),
        SyncSignal("chromaprint", -19.9, 0.12),
        SyncSignal("chromaprint", -19.9, 0.09),
    ]
    r = fuse(sigs_2645)
    assert r and r.votes == 4 and abs(r.offset + 19.625) < 0.1
    assert r.conf >= 0.85, f"esperaba >=0.85, got {r.conf}"
    print(f"  ✓ 2645 (ENTREVISTADO_13 y ENTREVISTADO_10): offset={r.offset:.2f}, conf={r.conf:.2f}, votos={r.votes}, métodos={r.methods}")

    # Caso 1 señal débil (envelope conf 0.50): rechazada — métodos acústicos
    # exigen convergencia para no propagar falsos positivos
    r = fuse([SyncSignal("envelope", -10.0, 0.50)])
    assert r is None, f"esperaba None, got conf={r.conf if r else None}"
    print(f"  ✓ 1 señal débil (envelope conf=0.50): rechazada (exige convergencia)")

    # Caso 1 señal fuerte semántica (transcript conf 0.85): aceptada
    r = fuse([SyncSignal("transcript", -10.0, 0.85)])
    assert r and r.conf == 0.85
    print(f"  ✓ 1 señal fuerte semántica (transcript conf=0.85): aceptada")

    # Caso 1 señal fuerte pero baja (transcript conf 0.40): rechazada
    r = fuse([SyncSignal("transcript", -10.0, 0.40)])
    assert r is None
    print(f"  ✓ 1 señal fuerte pero baja (transcript conf=0.40): rechazada")

    # Caso señales divergentes: cluster ganador pero diverged=True
    sigs_div = [
        SyncSignal("envelope", -10.0, 0.30),
        SyncSignal("envelope", -10.5, 0.25),
        SyncSignal("chromaprint", +50.0, 0.40),
        SyncSignal("chromaprint", +51.0, 0.35),
    ]
    r = fuse(sigs_div)
    assert r and r.diverged
    print(f"  ✓ divergente: ganador offset={r.offset:.2f}, diverged={r.diverged}")

    # Caso diversidad de método: bonus
    sigs_diverse = [
        SyncSignal("envelope", -19.4, 0.20),
        SyncSignal("chromaprint", -19.9, 0.15),
        SyncSignal("phrases", -19.5, 0.18),
    ]
    r = fuse(sigs_diverse)
    assert r and r.conf >= 0.80
    print(f"  ✓ diversidad (3 métodos distintos): conf={r.conf:.2f}")

    print("\nlib/sync_fusion: tests OK")
