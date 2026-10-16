"""Local speech-to-text transcription of captured Bee audio.

Uses faster-whisper (CTranslate2-based Whisper), fully local/offline after
the model is downloaded once. No audio or text leaves the machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from faster_whisper import WhisperModel


@dataclass
class TranscriptSegment:
    start: float  # seconds
    end: float  # seconds
    text: str


@lru_cache(maxsize=2)
def _load_model(model_size: str, device: str, compute_type: str) -> WhisperModel:
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def transcribe_file(
    path: str,
    model_size: str = "base",
    device: str = "auto",
    compute_type: str = "auto",
    language: str | None = None,
) -> list[TranscriptSegment]:
    """Transcribe an audio file (any format PyAV can decode, including the
    ADTS .aac files this project's capture_audio.py produces).

    model_size: "tiny" | "base" | "small" | "medium" | "large-v3" (bigger =
    more accurate, slower, more memory). "base" is a reasonable default for
    short voice clips on a laptop CPU.
    """
    model = _load_model(model_size, device, compute_type)
    segments_iter, _info = model.transcribe(path, language=language, vad_filter=True)
    return [TranscriptSegment(start=s.start, end=s.end, text=s.text.strip()) for s in segments_iter]


def format_transcript(segments: list[TranscriptSegment]) -> str:
    lines = []
    for seg in segments:
        lines.append(f"[{seg.start:6.1f}s - {seg.end:6.1f}s] {seg.text}")
    return "\n".join(lines)
