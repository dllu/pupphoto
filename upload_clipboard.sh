#!/bin/bash

# uploads a photo and copies the url to clipboard

source "$(dirname "$0")/upload_photo.sh"

# Check for the presence of at least one argument
if [ -z "$1" ]; then
    echo "Error: No file path provided."
    exit 1
fi

echo "" | xclip -selection c

# Call the upload function with the provided arguments
if [ -n "$2" ]; then
    # If a resize argument is provided
    output=$(upload_photo "$1" "$2")
else
    # If only the file path is provided
    output=$(upload_photo "$1")
fi

# Check if a file path is provided
if [ -z "$1" ]; then
    echo "No file path provided"
    exit 1
fi

echo $output | xclip -selection c
