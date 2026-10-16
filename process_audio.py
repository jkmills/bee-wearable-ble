"""Full local pipeline on an already-captured audio file: transcribe, diarize
(who spoke when), match speakers against your saved named profiles, and
prompt you to name any new/unrecognized speakers.

ONE-TIME SETUP REQUIRED before this works (diarization models are gated on
Hugging Face -- free, but needs a click-through):
    1. Create a free account at https://huggingface.co if you don't have one.
    2. Visit https://huggingface.co/pyannote/speaker-diarization-3.1 and
       https://huggingface.co/pyannote/segmentation-3.0 and accept each
       model's user conditions.
    3. Run: uv run hf auth login
       and paste a read-access token from
       https://huggingface.co/settings/tokens when prompted. This stores the
       token in your own ~/.cache/huggingface/token -- it's never seen by
       this codebase or sent anywhere by this project.

After that one-time setup, everything below runs fully offline.

Usage:
    cd bee-wearable-ble  # this repo's directory
    uv run python3 process_audio.py [audio_file] [--windowed]

Defaults to captured_audio.aac if no file is given.

--windowed also runs a continuous sliding-window media-tagging pass over the
whole file (independent of diarization turns) and writes it to
captured_audio_windowed_tags.txt -- useful for telling whether background
media is actually constant under a conversation rather than confined to its
own speaker turn. See bee_control/audio_tagging.py and GitHub issue #2.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from bee_control.audio_tagging import looks_like_media, tag_file_segment, tag_file_windows  # noqa: E402
from bee_control.diarize import diarize_file, merge_transcript_and_diarization  # noqa: E402
from bee_control.speakers import enroll_speaker, label_speakers, load_profiles  # noqa: E402
from bee_control.transcribe import transcribe_file  # noqa: E402


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    windowed = "--windowed" in sys.argv[1:]
    audio_path = args[0] if args else "captured_audio.aac"

    print(f"Transcribing {audio_path}...")
    transcript_segments = transcribe_file(audio_path)
    print(f"  {len(transcript_segments)} transcript segments.")

    print("Running speaker diarization (first run downloads models)...")
    diarization = diarize_file(audio_path)
    diarized_segments = diarization.segments
    embeddings = diarization.embeddings
    distinct_speakers = sorted(embeddings.keys())
    print(f"  {len(diarized_segments)} speaker turns, {len(distinct_speakers)} distinct speaker(s): {distinct_speakers}")

    profiles = load_profiles()
    label_to_name = label_speakers(embeddings, profiles)

    for label, embedding in embeddings.items():
        if label_to_name[label] != label:
            print(f"  {label} matched existing profile: {label_to_name[label]}")
            continue
        answer = input(f"  {label} is unrecognized -- name this speaker? (blank to skip): ").strip()
        if answer:
            enroll_speaker(answer, embedding)
            label_to_name[label] = answer
            print(f"  Saved '{answer}' to speaker_profiles.json")

    merged = merge_transcript_and_diarization(transcript_segments, diarized_segments)

    print()
    print("Tagging each segment for likely background media (TV/radio/music)...")
    print("(coarse heuristic, not a reliable ground truth -- see audio_tagging.py)")

    print()
    print("=== Labeled transcript ===")
    lines = []
    for label, start, end, text in merged:
        name = label_to_name.get(label, label)
        media_flag = ""
        tags_str = ""
        if end - start >= 1.0:
            try:
                tags = tag_file_segment(audio_path, start, end)
                tags_str = "  tags: " + ", ".join(f"{t.label} ({t.confidence:.2f})" for t in tags)
                if looks_like_media(tags):
                    media_flag = "  [FLAGGED as possible background media]"
            except Exception as e:
                tags_str = f"  [tagging failed: {e}]"
        line = f"[{start:6.1f}s - {end:6.1f}s] {name}: {text}{media_flag}\n{tags_str}"
        print(line)
        lines.append(line)

    with open("captured_audio_labeled_transcript.txt", "w") as f:
        f.write("\n".join(lines) + "\n")
    print()
    print("Wrote captured_audio_labeled_transcript.txt")

    if windowed:
        print()
        print("Running continuous sliding-window media tagging over the whole file...")
        window_lines = []
        for start, end, tags in tag_file_windows(audio_path):
            tags_str = ", ".join(f"{t.label} ({t.confidence:.2f})" for t in tags)
            flag = "  [FLAGGED]" if looks_like_media(tags) else ""
            line = f"[{start:6.1f}s - {end:6.1f}s]{flag}  {tags_str}"
            print(line)
            window_lines.append(line)
        with open("captured_audio_windowed_tags.txt", "w") as f:
            f.write("\n".join(window_lines) + "\n")
        print()
        print("Wrote captured_audio_windowed_tags.txt")


if __name__ == "__main__":
    main()
