#!/bin/bash

# resizes a photo and uploads uploads it to backblaze b2
upload_photo () {
    local src_file=$1
    local resize=$2
    local dir_name=$(dirname "$src_file")
    local filename=$(basename "$src_file")
    local ext="${filename##*.}"
    local src="./$filename"

    cd "$dir_name"

    if [ -n "$resize" ]; then
        filename="${filename%.*}_${resize}.$ext"
        src="/tmp/$filename"
        gm convert "$src_file" -resize "${resize}x${resize}" -quality 95 "$src"
    fi

    local sha1=$(sha1sum "$src" | awk '{ print $1 }')
    local dst="${filename%.*}_${sha1:0:16}.$ext"

    if [ "$(rclone lsf b2:dllu-pics/"$dst")" = "$dst" ]; then
        echo 'https://i.dllu.net/'"$dst"
    else
        rclone copyto "$src" b2:dllu-pics/"$dst"
        notify-send "$filename"
        echo 'https://i.dllu.net/'"$dst"
    fi
}
