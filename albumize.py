#!/usr/bin/env python3

from upload_photo import upload_photo
import sys
import os
from pathlib import Path
import subprocess
from concurrent.futures import ThreadPoolExecutor


if __name__ == "__main__":
    with open(
        Path(os.path.realpath(__file__)).parent / "static" / "album_template.html"
    ) as f:
        html = f.read()

    lines = []
    tasks = []

    with ThreadPoolExecutor(max_workers=12) as executor:
        # Schedule the upload tasks for each file and each size
        for f in sys.argv[1:]:
            full_upload_future = executor.submit(upload_photo, f)
            thumb_upload_future = executor.submit(upload_photo, f, 600)
            tasks.append((f, full_upload_future, thumb_upload_future))

        # As tasks complete, collect their results
        for f, full_future, thumb_future in tasks:
            full_url = full_future.result()
            thumb_url = thumb_future.result()
            lines.append(f'<a href="{full_url}"><img src="{thumb_url}"></a>')

    output_filename = f"{Path(sys.argv[1]).stem}---{Path(sys.argv[-1]).stem}.html"

    tmp = Path("/tmp")
    with open(tmp / output_filename, "w") as f:
        f.write(html + "\n".join(lines) + "</body></html")

    subprocess.run(
        [
            "rsync",
            "-v",
            str(tmp / output_filename),
            f"purplepuppy.linode:/www/misc/public/{output_filename}",
        ]
    )
    print(f"https://daniel.lawrence.lu/public/{output_filename}")
