"""Local, persistent named speaker profiles.

Stores one averaged voice embedding per named person in speaker_profiles.json
(gitignored -- voice embeddings are personal biometric data). New speakers
heard in a recording are matched against saved profiles by cosine similarity;
unmatched ones are left as generic labels (e.g. "SPEAKER_00") for you to name.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

PROFILES_PATH = Path("speaker_profiles.json")

# Cosine similarity above this is considered the same person. pyannote's
# own embedding model docs suggest ~0.5-0.7 as a reasonable same-speaker
# threshold depending on audio conditions; not independently tuned here.
DEFAULT_MATCH_THRESHOLD = 0.6


def load_profiles(path: Path = PROFILES_PATH) -> dict[str, np.ndarray]:
    if not path.exists():
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {name: np.array(vec, dtype=np.float32) for name, vec in raw.items()}


def save_profiles(profiles: dict[str, np.ndarray], path: Path = PROFILES_PATH) -> None:
    raw = {name: vec.tolist() for name, vec in profiles.items()}
    with open(path, "w") as f:
        json.dump(raw, f, indent=1)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def match_speaker(
    embedding: np.ndarray,
    profiles: dict[str, np.ndarray],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> tuple[str, float] | None:
    """Returns (name, similarity) for the best match above `threshold`, or None."""
    best_name = None
    best_sim = -1.0
    for name, profile_embedding in profiles.items():
        sim = cosine_similarity(embedding, profile_embedding)
        if sim > best_sim:
            best_sim = sim
            best_name = name
    if best_name is not None and best_sim >= threshold:
        return best_name, best_sim
    return None


def enroll_speaker(name: str, embedding: np.ndarray, path: Path = PROFILES_PATH) -> None:
    """Save/overwrite a named profile. If the name already exists, averages
    the new embedding in rather than replacing it outright, so repeated
    enrollment from multiple recordings improves the profile over time."""
    profiles = load_profiles(path)
    if name in profiles:
        profiles[name] = (profiles[name] + embedding) / 2.0
    else:
        profiles[name] = embedding
    save_profiles(profiles, path)


def label_speakers(
    speaker_embeddings: dict[str, np.ndarray],
    profiles: dict[str, np.ndarray] | None = None,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> dict[str, str]:
    """For each generic speaker_label -> embedding, return speaker_label -> best
    guess at a human name (falls back to the original label if no match)."""
    if profiles is None:
        profiles = load_profiles()
    result = {}
    for label, embedding in speaker_embeddings.items():
        match = match_speaker(embedding, profiles, threshold)
        result[label] = match[0] if match else label
    return result
