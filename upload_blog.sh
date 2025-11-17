#!/bin/bash

script_dir=$(cd "$(dirname "$0")" && pwd)
source "$script_dir/clipboard.sh" || exit 1

# uploads a photo and copies the url to clipboard

# Check if a file path is provided
if [ -z "$1" ]; then
    echo "No file path provided"
    exit 1
fi

dest_dir=${2:-/home/dllu/proj/daniel.lawrence.lu/img}

clipboard_clear || exit 1

# Upload the full-size photo
full_size_link=$(python "$script_dir/upload_photo.py" "$1")

# Extract hashed filename from the URL
dst_filename=$(echo "$full_size_link" | sed 's#.*/##')

# Locate processed source (GPS-scrubbed) in thumbs directory
pictures_dir=$(xdg-user-dir PICTURES 2>/dev/null)
if [ -z "$pictures_dir" ]; then
    pictures_dir="$HOME/Pictures"
fi
processed_path="$pictures_dir/thumbs/$(basename "$1")"

if [ ! -f "$processed_path" ]; then
    echo "Error: Processed photo not found at $processed_path" >&2
    exit 1
fi

# Copy the processed file (GPS sanitized/resized) to the destination directory
mkdir -p "$dest_dir"
dest_file="$dest_dir/$dst_filename"
rsync -a "$processed_path" "$dest_file"

# Output the sentence with both links
output="pic $full_size_link : "

clipboard_copy "$output" || exit 1
echo "$output"

echo "Copied $processed_path to $dest_file"
