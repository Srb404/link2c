# link2c

![C](https://img.shields.io/badge/C-UVC%20XU-A8B9CC?logo=c&logoColor=white)
![Python](https://img.shields.io/badge/Python-GTK4%20%2F%20libadwaita-3776AB?logo=python&logoColor=white)
![Linux](https://img.shields.io/badge/Linux-V4L2-FCC624?logo=linux&logoColor=black)

Native Linux control for the Insta360 Link 2C webcam, which ships only with a Windows and macOS app. The camera's AI features sit behind UVC extension units; their selectors were mapped by reading the vendor's Windows controller (strings, RTTI vtables) instead of blindly sweeping values.

---

## Features

- **Modes:** off, AI tracking, whiteboard, overhead and DeskView
- **Mirror:** horizontal and vertical, done by the camera itself, so it applies in every app
- **Panel:** GTK4/libadwaita window with a live preview (modes only work while the camera streams); changes are saved to the cameractrls preset restored on replug
- **Recovery:** `link2c-recover` brings the camera back from firmware-update mode without unplugging it

## Requirements

- a C compiler and `make`
- `v4l-utils` (`v4l2-ctl`) and `ffmpeg` for the panel and its preview
- Python 3 with PyGObject, GTK 4 and libadwaita for the panel
- `udisks2`, `usbutils` and `util-linux` for `link2c-recover`
- optional: [cameractrls](https://github.com/soyersoyer/cameractrls), whose `cameractrlsd` re-applies the saved image settings when the camera is plugged in

On Arch: `pacman -S base-devel v4l-utils ffmpeg python-gobject gtk4 libadwaita udisks2 usbutils`.

No root is needed: the tools only open the video device, which systemd-logind grants to the logged-in user. Without logind, add yourself to the `video` group.

## Running

```bash
make install            # link2c, link2c-gui, link2c-recover to ~/.local/bin

link2c info
link2c mode deskview
link2c mirror h on
link2c-gui
```

The device defaults to `/dev/v4l/by-id/usb-Insta360_Insta360_Link_2C-video-index0`; override it with `LINK_DEV`.

## Safety

Three writes switch the camera into Ambarella firmware-update mode: unit 9 selector 17, unit 10 selector 9 and bit 5 of unit 9 selector 27. `link2c` refuses them without `--force`.

## Notes

`research/` keeps the scripts used to work out the protocol.

## Disclaimer

This is an unofficial tool, not affiliated with or endorsed by Insta360. It writes undocumented vendor controls; some of them put the camera into firmware-update mode. Use it at your own risk.

## License

MIT
