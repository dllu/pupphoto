#!/bin/bash
file="$1"
base="${file%.jpg}"

for ext in arw raf dng; do
    raw_file="${base}.${ext}"
    if [[ -f "$raw_file" ]]; then
        darktable "$raw_file" &
        break
    fi
done
