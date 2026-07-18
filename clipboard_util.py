import os
import subprocess
import sys


def _clipboard_command(*, clear: bool) -> list[str] | None:
    if os.environ.get("WAYLAND_DISPLAY"):
        return ["wl-copy", "--clear"] if clear else ["wl-copy"]
    if os.environ.get("DISPLAY"):
        return ["xclip", "-selection", "c"]
    return None


def _update_clipboard(text: str, *, clear: bool) -> bool:
    cmd = _clipboard_command(clear=clear)
    if cmd is None:
        print(
            "Error: Unable to detect display server. Clipboard not updated.",
            file=sys.stderr,
        )
        return False

    kwargs: dict[str, object] = {
        "check": True,
        "start_new_session": True,
    }
    if not (clear and cmd[0] == "wl-copy"):
        kwargs.update(input=text, text=True)

    try:
        subprocess.run(cmd, **kwargs)
    except (OSError, subprocess.CalledProcessError):
        print("Error: Clipboard command failed.", file=sys.stderr)
        return False
    return True


def clear_clipboard() -> bool:
    """Clear the active clipboard, returning whether it succeeded."""
    return _update_clipboard("", clear=True)


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the active clipboard, returning whether it succeeded."""
    return _update_clipboard(text, clear=False)
