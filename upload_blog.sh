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

exif_make=$(exiv2 "$1" -Pt -g Exif.Image.Make)
exif_model=$(exiv2 "$1" -Pt -g Exif.Image.Model)
exif_lens=$(exiv2 "$1" -Pt -g Exif.Photo.LensModel)
exif_aperture=$(exiv2 "$1" -Pt -g Exif.Photo.ApertureValue
exif_ss=$(exiv2 "$1" -Pt -g Exif.Photo.ShutterSpeedValue
exif_iso=$(exiv2 "$1" -Pt -g Exif.Photo.ISOSpeedRatings)
exif_fl=$(exiv2 "$1" -Pt -g Exif.Photo.FocalLength | awk '{print $1 "mm"}')

# Assemble the description
desc="$exif_make $exif_model with $exif_lens, $exif_fl. $exif_aperture, $exif_ss, ISO $exif_iso."

# Output the sentence with both links
output="pic $resized_link : $desc (full size: $full_size_link)"

echo $output | xclip -selection c
echo $output
