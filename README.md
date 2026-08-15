# link2c

![C](https://img.shields.io/badge/C-UVC%20XU-A8B9CC?logo=c&logoColor=white)
![Linux](https://img.shields.io/badge/Linux-V4L2-FCC624?logo=linux&logoColor=black)

Native Linux control for the Insta360 Link 2C webcam, which ships only with a Windows and macOS app. The camera's AI features sit behind UVC extension units, reached here with `UVCIOC_CTRL_QUERY`.

---

## Features

- **Modes:** off, AI tracking, whiteboard, overhead and DeskView
- **Framing:** head, half body or full body
- **Raw access:** dump every extension unit selector, read and write single ones
- **Recovery:** `link2c-recover` brings the camera back from firmware-update mode without unplugging it

## Requirements

- a C compiler and `make`
- `udisks2`, `usbutils` and `util-linux` for `link2c-recover`

No root is needed: the tools only open the video device, which systemd-logind grants to the logged-in user. Without logind, add yourself to the `video` group.

## Running

```bash
make install            # link2c, link2c-recover to ~/.local/bin

link2c scan
link2c info
link2c mode deskview
```

The device defaults to `/dev/v4l/by-id/usb-Insta360_Insta360_Link_2C-video-index0`; override it with `LINK_DEV`.

## Safety

Two writes switch the camera into Ambarella firmware-update mode: unit 9 selector 17 and unit 10 selector 9. `link2c` refuses them without `--force`.

## Disclaimer

This is an unofficial tool, not affiliated with or endorsed by Insta360. It writes undocumented vendor controls; some of them put the camera into firmware-update mode. Use it at your own risk.

## License

MIT
