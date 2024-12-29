#!/bin/bash

# uploads a photo and copies the url to clipboard

# Check for the presence of at least one argument
if [ -z "$1" ]; then
    echo "Error: No file path provided."
    exit 1
fi

echo "" | xclip -selection c

# Call the upload function with the provided arguments
if [ -n "$2" ]; then
    # If a resize argument is provided
    output=$(python $(dirname "$0")/upload_photo.py "$1" "$2")
else
    # If only the file path is provided
    output=$(python $(dirname "$0")/upload_photo.py "$1")
fi

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
