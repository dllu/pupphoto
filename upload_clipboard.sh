#!/bin/bash

script_dir=$(cd "$(dirname "$0")" && pwd)
source "$script_dir/clipboard.sh" || exit 1

# uploads a photo and copies the url to clipboard

# Check for the presence of at least one argument
if [ -z "$1" ]; then
    echo "Error: No file path provided."
    exit 1
fi

clipboard_clear || exit 1

# Call the upload function with the provided arguments
if [ -n "$2" ]; then
    # If a resize argument is provided
    output=$(python "$script_dir/upload_photo.py" "$1" "$2")
else
    # If only the file path is provided
    output=$(python "$script_dir/upload_photo.py" "$1")
fi

clipboard_copy "$output" || exit 1
