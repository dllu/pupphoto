#!/bin/bash

clipboard_copy() {
    local data="$1"
    if [ "$WAYLAND_DISPLAY" ]; then
        printf '%s' "$data" | wl-copy
    elif [ "$DISPLAY" ]; then
        printf '%s' "$data" | xclip -selection c
    else
        echo "Error: Unable to detect display server. Clipboard not updated." >&2
        return 1
    fi
}

clipboard_clear() {
    clipboard_copy ""
}
