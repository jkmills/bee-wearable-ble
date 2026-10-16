"""Local speaker diarization (who spoke when) using pyannote.audio.

Fully local/offline after the model is downloaded once -- no audio leaves
the machine. Requires a (free) Hugging Face account and accepting the
model's terms before the first download:

    1. Create an account at https://huggingface.co if you don't have one.
    2. Visit https://huggingface.co/pyannote/speaker-diarization-3.1 and
       https://huggingface.co/pyannote/segmentation-3.0 and accept each
       model's user conditions (both are free, just need a click-through).
    3. Run `uv run hf auth login` in this project and paste a read-access
       token from https://huggingface.co/settings/tokens when prompted.
       This stores the token in your own Hugging Face cache directory
       (~/.cache/huggingface/token) -- it never passes through this
       codebase or any chat transcript.

Also requires a system FFmpeg install (`brew install ffmpeg`) -- unlike
faster-whisper's PyAV, pyannote.audio's torchcodec-based audio loading
dynamically links against system FFmpeg rather than bundling it.

After that one-time setup, diarize_file() works fully offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np


@dataclass
class DiarizedSegment:
    start: float  # seconds
    end: float  # seconds
    speaker_label: str  # e.g. "SPEAKER_00" -- local to this one recording


@dataclass
class DiarizationResult:
    segments: list[DiarizedSegment]
    # one averaged embedding per distinct speaker_label seen in `segments`
    embeddings: dict[str, np.ndarray]


@lru_cache(maxsize=1)
def _load_diarization_pipeline():
    from pyannote.audio import Pipeline

    return Pipeline.from_pretrained("pyannote/speaker-diarization-3.1")


def diarize_file(path: str) -> DiarizationResult:
    """Run diarization on an audio file, returning per-segment speaker labels
    and one embedding per distinct speaker. Labels (e.g. "SPEAKER_00") are
    only meaningful within this one call/file -- use speakers.py to persist
    named identity across recordings via the embeddings.
    """
    pipeline = _load_diarization_pipeline()
    output = pipeline(path)

    segments = [
        DiarizedSegment(start=turn.start, end=turn.end, speaker_label=label)
        for turn, _, label in output.speaker_diarization.itertracks(yield_label=True)
    ]

    # Per pyannote.audio's own documented usage: output.speaker_embeddings[s]
    # is the embedding for output.speaker_diarization.labels()[s], in order.
    labels_in_order = output.speaker_diarization.labels()
    embeddings = {
        label: np.asarray(output.speaker_embeddings[i]).reshape(-1)
        for i, label in enumerate(labels_in_order)
    }

    return DiarizationResult(segments=segments, embeddings=embeddings)


def merge_transcript_and_diarization(
    transcript_segments: list, diarized_segments: list[DiarizedSegment]
) -> list[tuple[str, float, float, str]]:
    """For each transcript segment, find the diarized speaker whose turn overlaps
    it the most. Returns (speaker_label, start, end, text) tuples.
    """
    result = []
    for t in transcript_segments:
        best_label = "UNKNOWN"
        best_overlap = 0.0
        for d in diarized_segments:
            overlap = min(t.end, d.end) - max(t.start, d.start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = d.speaker_label
        result.append((best_label, t.start, t.end, t.text))
    return result
