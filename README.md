# pupphoto
some random scripts to organize my photos

* `import.py` imports from a camera SD card mounted at `/mnt/camera`. Images are stored in `/home/dllu/pictures/raw` and are renamed to date, original filename, and sha1sum of the raw file. For example, `DSCF2300.JPG` and `DSCF2300.RAF` get renamed to `2023-10-01-11-36-11_DSCF2300_53e266aac66a4b9cb37380214334d15b58517061.jpg` and `2023-10-01-11-36-11_DSCF2300_53e266aac66a4b9cb37380214334d15b58517061.raf`.
