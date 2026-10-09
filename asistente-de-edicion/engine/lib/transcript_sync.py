"""Sync a video clip to an external recording by transcript content alignment.

The robust, general dual-system sync method: it needs only the video and the
external audio — no timecode, no metadata, no folder names. Both are transcribed;
the words spoken in the clip also occur in the external recording, so matching
word n-grams reveal the time offset. The dominant cluster of per-n-gram offsets
is the answer; agreement across many n-grams is the confidence.
"""

from __future__ import annotations

import unicodedata


def normalize(word: str) -> str:
    """Lowercase, drop accents and non-alphanumerics, so transcripts of the same
    speech from two different-quality sources (camera scratch vs field recorder)
    still match word-for-word."""
    w = unicodedata.normalize("NFKD", word.lower())
    w = "".join(c for c in w if not unicodedata.combining(c))
    return "".join(c for c in w if c.isalnum())


def align(clip_words, rec_words, ngram=4, tol=0.6):
    """Align a clip transcript against a recording transcript.

    clip_words / rec_words: lists of (word, start_time_seconds).
    Returns (offset_sec, confidence, n_anchors) or None.
      offset_sec = audio_start - video_start — same convention as audio_sync_pairs
                   (negative => the recording started before the clip). Lua
                   placeSyncAudio places the recording at clip_start + offset.
      confidence = fraction of clip n-grams that agree on the offset (0..1)
    """
    cw = [(normalize(w), t) for w, t in clip_words]
    cw = [wt for wt in cw if wt[0]]
    rw = [(normalize(w), t) for w, t in rec_words]
    rw = [wt for wt in rw if wt[0]]
    if len(cw) < ngram or len(rw) < ngram:
        return None

    # index every n-gram of the recording -> times it starts at
    rec_index: dict[tuple, list] = {}
    for i in range(len(rw) - ngram + 1):
        key = tuple(w for w, _ in rw[i:i + ngram])
        rec_index.setdefault(key, []).append(rw[i][1])

    # every clip n-gram that also occurs in the recording votes an offset
    offsets = []
    for i in range(len(cw) - ngram + 1):
        key = tuple(w for w, _ in cw[i:i + ngram])
        t_clip = cw[i][1]
        for t_rec in rec_index.get(key, ()):
            offsets.append(t_rec - t_clip)
    if not offsets:
        return None

    # the true offset is the densest cluster of votes within `tol` seconds
    offsets.sort()
    best_count, best_center = 0, 0.0
    for pivot in offsets:
        cluster = [o for o in offsets if abs(o - pivot) <= tol]
        if len(cluster) > best_count:
            best_count = len(cluster)
            best_center = sum(cluster) / len(cluster)

    n_clip_ngrams = len(cw) - ngram + 1
    # best_center is median(t_rec - t_clip); negate -> audio_start - video_start
    return -best_center, best_count / n_clip_ngrams, best_count


def best_match(clip_words, recordings, ngram=4, tol=0.6, min_conf=0.10):
    """Pick the recording a clip belongs to.

    recordings: list of (recording_id, rec_words). Returns the alignment dict for
    the best-scoring recording, or None if none clears `min_conf`.
    """
    matches = top_matches(clip_words, recordings, ngram=ngram, tol=tol,
                          min_conf=min_conf, max_pairs=1)
    return matches[0] if matches else None


def top_matches(clip_words, recordings, ngram=4, tol=0.6, min_conf=0.10,
                max_pairs=1):
    """Devuelve hasta `max_pairs` recordings ordenados por confianza descendente.

    Caso de uso (Zezzions 2026-05-25): dual-lavalier en entrevista de dos
    interlocutores. TX1 y TX2 grabaron en archivos distintos; queremos
    emparejar el mismo video con AMBOS (uno por track en A2/A3 de Resolve).
    Para proyectos single-lavalier (default), `max_pairs=1` reproduce
    el comportamiento histórico de `best_match`.
    """
    scored = []
    for rec_id, rec_words in recordings:
        res = align(clip_words, rec_words, ngram=ngram, tol=tol)
        if res is None:
            continue
        offset, conf, anchors = res
        if conf >= min_conf:
            scored.append({"recording_id": rec_id, "offset_sec": offset,
                           "confidence": conf, "anchors": anchors})
    scored.sort(key=lambda m: m["confidence"], reverse=True)
    return scored[:max_pairs]
