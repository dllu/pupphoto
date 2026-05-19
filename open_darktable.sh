#!/bin/bash
if [[ -d /opt/darktable/bin ]]; then
    export PATH="/opt/darktable/bin:$PATH"
elif [[ -d /opt/darktable ]]; then
    export PATH="/opt/darktable:$PATH"
fi

file="$1"
base="${file%.jpg}"

for ext in arw raf dng; do
    raw_file="${base}.${ext}"
    if [[ -f "$raw_file" ]]; then
        darktable "$raw_file" &
        break
    fi
done
