#!/bin/bash

script_dir=$(cd "$(dirname "$0")" && pwd)

# uploads a photo and copies the url to clipboard

# Check for the presence of at least one argument
if [ -z "$1" ]; then
    echo "Error: No file path provided."
    exit 1
fi

# Call the upload function with the provided arguments
if [ -n "$2" ]; then
    # If a resize argument is provided
    output=$(python "$script_dir/upload_photo.py" --clipboard "$1" "$2") || exit 1
else
    # If only the file path is provided
    output=$(python "$script_dir/upload_photo.py" --clipboard "$1") || exit 1
fi
echo "$output"
