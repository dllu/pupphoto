#!/usr/bin/env python3

from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from config import load_config
from upload_photo import upload_photo


if __name__ == "__main__":
    config = load_config().album

    with config.template_path.open() as f:
        html = f.read()

    lines = []
    tasks = []

    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
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

    config.output_dir.mkdir(parents=True, exist_ok=True)
    with (config.output_dir / output_filename).open("w") as f:
        f.write(html + "\n".join(lines) + "</body></html")

    subprocess.run(
        [
            "rsync",
            "-v",
            str(config.output_dir / output_filename),
            f"{config.rsync_destination}/{output_filename}",
        ]
    )
    print(f"{config.public_base_url}/{output_filename}")
