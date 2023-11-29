#!/bin/bash

# uploads a photo and copies the url to clipboard

source "$(dirname "$0")/upload_photo.sh"

# Check if a file path is provided
if [ -z "$1" ]; then
    echo "No file path provided"
    exit 1
fi

echo "" | xclip -selection c

# Upload the resized photo
resized_link=$(upload_photo "$1" 1200)

# Upload the full-size photo
full_size_link=$(upload_photo "$1")

# Output the sentence with both links
output="pic $resized_link (full size: $full_size_link)"

echo $output | xclip -selection c
