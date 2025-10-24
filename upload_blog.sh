#!/bin/bash

# uploads a photo and copies the url to clipboard

# Check if a file path is provided
if [ -z "$1" ]; then
    echo "No file path provided"
    exit 1
fi

if [ "$WAYLAND_DISPLAY" ]; then
    # Wayland: Use wl-copy
    echo "" | wl-copy
elif [ "$DISPLAY" ]; then
    # X11: Use xclip
    echo "" | xclip -selection c
else
    echo "Error: Unable to detect display server. Clipboard not updated." >&2
    exit 1
fi

# Upload the full-size photo
full_size_link=$(python $(dirname "$0")/upload_photo.py "$1")

# Output the sentence with both links
output="pic $full_size_link : "

if [ "$WAYLAND_DISPLAY" ]; then
    # Wayland: Use wl-copy
    echo "$output" | wl-copy
elif [ "$DISPLAY" ]; then
    # X11: Use xclip
    echo "$output" | xclip -selection c
else
    echo "Error: Unable to detect display server. Clipboard not updated." >&2
    exit 1
fi
echo $output
