#!/bin/bash

# uploads a photo and copies the url to clipboard

# Check if a file path is provided
if [ -z "$1" ]; then
    echo "No file path provided"
    exit 1
fi

echo "" | xclip -selection c

# Upload the resized photo
resized_link=$(python $(dirname "$0")/upload_photo.py "$1" 1200)

# Upload the full-size photo
full_size_link=$(python $(dirname "$0")/upload_photo.py "$1")

exif_make=$(exiv2 -Pt -g Exif.Image.Make "$1")
exif_model=$(exiv2 -Pt -g Exif.Image.Model "$1")
exif_lens=$(exiv2 -Pt -g Exif.Photo.LensModel "$1")
exif_aperture=$(exiv2 -Pt -g Exif.Photo.ApertureValue "$1")
exif_ss=$(exiv2 -Pt -g Exif.Photo.ShutterSpeedValue "$1")
exif_iso=$(exiv2 -Pt -g Exif.Photo.ISOSpeedRatings "$1")
exif_fl=$(exiv2 -Pt -g Exif.Photo.FocalLength "$1" | head -n 1)

# Assemble the description
desc="$exif_make $exif_model with $exif_lens, $exif_fl, $exif_aperture, $exif_ss, ISO $exif_iso."

# Output the sentence with both links
output="pic $resized_link : $desc (full size: $full_size_link)"

echo $output | xclip -selection c
echo $output
