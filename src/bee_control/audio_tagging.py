"""Background/media audio detection via PANNs (a CNN14 model pretrained on
AudioSet's 527 sound event classes), to flag segments that are likely TV,
radio, music, or other non-foreground-speech audio rather than someone
actually talking to the device.

This is a coarse, imperfect signal -- AudioSet-trained taggers are good at
"is there music/TV-like audio present" but were not designed to distinguish
"person talking in the room" from "person talking in a TV show playing in
the room" with high confidence. Treat its output as a filtering heuristic to
review, not a ground-truth label.

Fully local/offline after the model files are fetched once (see
scripts/fetch_panns_model.sh or the manual curl commands in that file's
comments -- the panns_inference package's own auto-downloader uses wget,
which isn't installed on macOS by default).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

# AudioSet label substrings worth flagging as "probably not direct foreground
# speech to the device." Not exhaustive; tune based on what MEDIA_LABEL_HINTS
# actually fires on in practice.
MEDIA_LABEL_HINTS = (
    "Television",
    "Radio",
    "Music",
    "Theme music",
    "Background music",
    "Film",
    "Broadcast",
)


@dataclass
class AudioTag:
    label: str
    confidence: float


@lru_cache(maxsize=1)
def _load_tagger():
    from panns_inference import AudioTagging

    return AudioTagging(checkpoint_path=None, device="cpu")


def tag_clip(waveform: np.ndarray, sample_rate: int = 32000, top_k: int = 5) -> list[AudioTag]:
    """Tag one mono waveform (float32, -1..1 range) sampled at 32000Hz (PANNs'
    expected rate -- resample before calling if your audio is a different rate).
    Returns the top_k highest-confidence AudioSet labels.
    """
    tagger = _load_tagger()
    clipwise_output, _embedding = tagger.inference(waveform[None, :])
    scores = clipwise_output[0]
    top_indices = np.argsort(scores)[::-1][:top_k]
    from panns_inference.config import labels as audioset_labels

    return [AudioTag(label=audioset_labels[i], confidence=float(scores[i])) for i in top_indices]


def looks_like_media(tags: list[AudioTag], confidence_threshold: float = 0.15) -> bool:
    """Heuristic: True if any high-confidence tag matches a media-related label."""
    for tag in tags:
        if tag.confidence < confidence_threshold:
            continue
        if any(hint.lower() in tag.label.lower() for hint in MEDIA_LABEL_HINTS):
            return True
    return False


def tag_file_segment(path: str, start: float, end: float, top_k: int = 5) -> list[AudioTag]:
    """Load one time range of an audio file at PANNs' expected 32kHz mono and tag it."""
    from pyannote.audio import Audio
    from pyannote.core import Segment

    audio = Audio(sample_rate=32000, mono="downmix")
    waveform, _sample_rate = audio.crop(path, Segment(start, end))
    return tag_clip(np.asarray(waveform).reshape(-1), sample_rate=32000, top_k=top_k)


def tag_file_windows(
    path: str, window: float = 2.0, hop: float = 1.0, top_k: int = 5
) -> list[tuple[float, float, list[AudioTag]]]:
    """Tag a whole file on a fixed sliding window, independent of diarization
    turns -- catches background media (e.g. a TV playing continuously) that a
    per-speaker-turn tagging pass would miss if it never gets its own turn.
    Returns (window_start, window_end, tags) tuples covering the whole file.
    """
    from pyannote.audio import Audio

    audio = Audio(sample_rate=32000, mono="downmix")
    duration = audio.get_duration(path)

    results = []
    start = 0.0
    while start < duration:
        end = min(start + window, duration)
        if end - start >= 0.5:  # PANNs needs a non-trivial clip to say anything useful
            results.append((start, end, tag_file_segment(path, start, end, top_k=top_k)))
        start += hop
    return results
