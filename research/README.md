# Research tools

Scripts used to work out the XU protocol. They are not needed for everyday use of the camera.

- `flipcheck.py <frames_dir> <phases_file>`: detects whether any test phase flipped the image (horizontal or vertical mirror, 180° rotation) against the reference frame
- `flip2.sh`: selector sweep with a watchdog that checks after every write that the camera is still on USB

Phases file format: `<frame_number> <unit> <selector> <value>`, with the first line `<number> BASE - -`.

Read the section on dangerous writes in `../NOTES.md` before running any of them.
