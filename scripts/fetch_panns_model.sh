#!/bin/sh
# Fetches the PANNs CNN14 AudioSet-tagging checkpoint and label list that
# panns_inference expects at ~/panns_data/. The package's own built-in
# downloader shells out to `wget`, which isn't installed by default on
# macOS -- this does the same fetch with curl instead. Safe to re-run;
# skips files that already exist.

set -e

DEST="$HOME/panns_data"
mkdir -p "$DEST"

if [ ! -f "$DEST/class_labels_indices.csv" ]; then
  echo "Fetching AudioSet label list..."
  curl -sL -o "$DEST/class_labels_indices.csv" \
    "http://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/class_labels_indices.csv"
fi

CKPT="$DEST/Cnn14_mAP=0.431.pth"
if [ ! -f "$CKPT" ]; then
  echo "Fetching CNN14 AudioTagging checkpoint (~300MB)..."
  curl -sL -o "$CKPT" \
    "https://zenodo.org/record/3987831/files/Cnn14_mAP%3D0.431.pth?download=1"
fi

echo "Done. Files in $DEST:"
ls -la "$DEST"
