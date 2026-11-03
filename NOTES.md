# Insta360 Link 2C on Linux: technical notes

How the camera's proprietary controls work, how they were found, and what the
tools in this repository rely on.

| | |
|---|---|
| USB ID | `2e1a:4c03` |
| SoC | Ambarella A9 |
| Vendor extension units | 9 (main), 10 (auxiliary), 11 |
| Firmware-update mode | `070a:4026 AmbarellaInc A9 Platform`, 90.5 MB mass storage labelled `LINK 2C` |

## Contents

- [Ground rules](#ground-rules)
- [Extension unit map](#extension-unit-map)
- [Dangerous writes](#dangerous-writes)
- [Reverse engineering](#reverse-engineering)
- [AI tracking](#ai-tracking)
- [GUI preview](#gui-preview)

## Ground rules

1. **Modes need an active stream.** Without one every mode, `off` included,
   reads back as `0xff`. This is why the GUI has a live preview; OBS or an
   ongoing call holding the stream works too.
2. **Mirroring applies on stream start.** The camera sets the flip when the
   stream initialises, so a change during continuous capture stays invisible.
   The GUI restarts its preview after toggling it.
3. **Pan and tilt need zoom.** At 1.0× the crop already covers the whole
   sensor, so there is nowhere to pan.

## Extension unit map

The unit GUIDs on the 2C differ from the Link and Link 2, but most of the
selector map carries over from them.

### Unit 9

| Selector | Size | Meaning | Status |
|---:|---|---|---|
| 2 | 61 B | mode, see [Mode struct](#mode-struct) | confirmed |
| 11 | 5 B, GET | tracking state (the vendor app only reads it) | |
| 12 | 32 B | serial number, ASCII | confirmed |
| 13 | 129 B | relative pan/tilt (from Link/Link 2) | untested |
| 14 | 1 B, SET | reset framing | accepted, no visible effect (no gimbal) |
| 17 | | **firmware-update switch** | [never write](#dangerous-writes) |
| 18 | 1 B | tracking speed: `00` fast, `01` normal, `02` slow | confirmed |
| 19 | 1 B | framing: `01` head, `02` half body, `03` full body | confirmed, needs bit 0 of selector 27 |
| 27 | 2 B | feature bitmask, see [below](#feature-bitmask) | confirmed |

### Unit 10

| Selector | Size | Meaning | Status |
|---:|---|---|---|
| 1 | 8 B, GET | tracking target on Link/Link 2 | read-only on the 2C |
| 9 | | **firmware-update switch** (`02`) | [never write](#dangerous-writes) |
| 15 | 2 B | privacy (gimbal down) | no effect, always reads `02 00` |
| 26 | 1 B | tracking: `00` single, `01` group | does not stick without a tracking stream |
| 28 | 25 B | horizontal correction, byte 0 = `0`/`1` | confirmed |

### Feature bitmask

Unit 9 selector 27. Bit numbers come from the `mov edx, N` before each
`setExtendFunc` call in the vendor app.

| Bit | Feature |
|---:|---|
| 0 | Auto Framing (AI zoom); framing does nothing without it |
| 2 | HDR |
| 3 | horizontal mirror |
| 5 | **firmware-update mode, [never set](#dangerous-writes)** |
| 6 | keep settings across power cycles ("Set as default") |
| 7 | one-touch tracking |
| 8 | auto tracking |
| 9 | smart adjustment (keeps the head centred) |
| 10 | forced portrait |
| 11 | full privacy |
| 12 | vertical mirror |

Bits 3 and 12 together are a 180° rotation. The tested unit shipped with
`0x11c8` (bits 3, 6, 7, 8, 12), which is why its image looked upside down out of
the box.

### Mode struct

Unit 9 selector 2 is a 61-byte struct, laid out as the vendor app's
`setVideoMode` builds it:

| Offset | Content |
|---:|---|
| 0 | mode id: `00` off, `01` tracking, `04` whiteboard, `05` overhead, `06` DeskView |
| 1 | mode flag |
| 2–37 | whiteboard corners |
| 38–41 | two words `0xffff` (read back as `ff 7f`) |
| 42–49 | two dwords `0x0e1a` |
| 52–53 | camera pitch in 0.1°, signed |
| 54 | flags |

Pitch is −45.0° by default. For DeskView the app sends the live pitch the camera
itself reports in the same field, and the perspective correction is built on
it. Sending only the first two bytes, as early versions of `link2c` did, zeroes
all of this.

Whiteboard mode is left out of the tools.

## Dangerous writes

> [!CAUTION]
> Three writes knock the camera off USB into firmware-update mode:
>
> - unit 9 selector 17, any non-zero value
> - unit 10 selector 9 = `02`
> - unit 9 selector 27 bit 5, which **persists**: the camera came back reading
>   `e8 11` and kept falling into firmware mode until the bit was cleared
>
> `link2c` refuses all three unless given `--force`.

**Recovery.** `link2c-recover` finds the Ambarella device with `lsblk` and runs
`udisksctl power-off` on it, which power-cycles the USB port. The camera is back
in about 7 s, no unplugging needed. Do not mount or write anything to that
storage.

## Reverse engineering

### Finding the mirror bit

A blind sweep of selectors 1–24 found no mirror: it is not a selector of its own
but a bit of selector 27, just outside the range. Static analysis of the vendor's
Windows controller found it instead, without ever running the app.

1. **Protobuf descriptor.** `strings` on the executable showed a `ParamType`
   enum with `PARAM_MIRROR_HOR` and `PARAM_MIRROR_VER`, proof that the feature
   exists. Its numbers belong to the app's UI↔service WebSocket protocol, not to
   XU selectors.
2. **From string to code.** Method names such as
   `Webcam::CameraInsta::setMirrorChecked` sit in `.rdata`, and code refers to
   them RIP-relative. A numpy scan of `.text` computes `VA(offset + 4) + disp32`
   for every offset and compares it with the string's address.
3. **The constant at the call.** Walking back from the `call` to
   `mov edx, imm32` (opcode `BA`) gives the bit: `setMirrorChecked` → 3,
   `setVerticallyMirrorChecked` → 12, `setPrivacyModeChecked` → 7.
4. **Through RTTI to the implementation.** The call goes through a virtual thunk
   (`mov rcx,[rcx]; mov rax,[rcx]; jmp [rax+0x100]`). From the string
   `.?AVWinUVCCameraExtendController@@`: TypeDescriptor 16 B before it → a dword
   in `.rdata` equal to its RVA, with signature `1` twelve bytes earlier
   (CompleteObjectLocator) → a pointer to the COL → the vtable 8 B after it →
   slot `+0x100`.
5. **The implementation.** It GETs two bytes, sets or clears bit `edx` and SETs
   them back. `KSPROPERTY.Id = 0x1b` is selector 27, and the GUID points at the
   unit 9 entry of the GUID table.

Tools: `strings`, `objdump -D -b binary -m i386:x86-64 -M intel`, `dd`, numpy and
a hand-written protobuf parser. No Ghidra, no IDA.

### Mapping the rest

The remaining features went faster: function bounds from `.pdata`, thunks
resolved through the `WinUVCCameraExtendController` vtable, then the GUID and
`KSPROPERTY.Id` in each implementation. Feature names come from the app's UI
strings.

### Flip detector

`research/flipcheck.py` compares the last frame of each test phase with the
reference frame and its three transforms (horizontal mirror, vertical mirror,
180° rotation). The closest one counts only if it beats the runner-up by 1.5×,
and the scene must be still. On synthetically flipped frames it recognised every
transform with zero error.

> [!NOTE]
> FFT phase correlation detects a pure shift of the frame well, but returns
> garbage once the scale changes. Compare zoomed frames another way.

### Sweeping safely

Check `lsusb -d 2e1a:4c03 && [ -e /dev/video0 ]` before and after every write to
an unknown selector, and stop the moment the device is gone. The first sweep did
not, lost the camera on selector 17 and kept writing blind for ten more
selectors.

## AI tracking

Tracking runs on the camera. The models shipped with the vendor app (RVM, DPT,
ByteEffects) are virtual camera effects: background, bokeh, beautification.

Weak tracking out of the box had two causes:

- **Auto Framing (bit 0) was off.** Framing and following a person only work
  with it on, and the factory value has it cleared. At 1.0× zoom there is also
  no room to move the crop.
- **Group tracking was selected** (unit 10 selector 26).

Still open: single vs. group tracking needs testing with a person in frame.

## GUI preview

```
camera (MJPEG/H264) → ffmpeg → scale → raw rgb24 pipe → Gdk.MemoryTexture
```

The first version decoded a fixed 640×360 at 15 fps, which gave 12.6 fps and an
upscaled image. Now:

- **Resolution follows the widget.** The pipe picks the smallest of
  `PREVIEW_STEPS = (360, 540, 720, 900, 1080)` that covers the widget in physical
  pixels, capped at the source height. Width follows the source aspect, so 4:3
  modes are not stretched.
- **Real scale factor.** `get_scale_factor()` rounds 1.5 up to 2 and would decode
  a third more pixels than shown; `Gdk.Surface.get_scale()` gives the real one.
- **No frame cap.** `-fps_mode passthrough` with `-fflags nobuffer -flags
  low_delay` runs at the camera's own rate.
- **Drop, do not queue.** A frame still waiting for the UI thread is replaced by
  the next one, so latency cannot pile up. `idle_add` runs at
  `PRIORITY_DEFAULT`, above GTK's redraw.

### Pitfalls

1. **Layout feedback loop.** `Gtk.Picture` measures its paintable, so a bigger
   stream made a wider picture, which took space from the panel and asked for a
   bigger stream again; the camera reopened every ~570 ms. `do_measure` now
   returns fixed sizes.
2. **Restart racing a busy device.** `proc.kill()` without `wait()` left the
   camera busy and the next `ffmpeg` failed with `Device or resource busy`.
   `_close()` reaps the process, with a timeout so a wedged USB cannot freeze
   the UI.
3. **Restart storm on startup.** The compositor tiles the window after mapping
   it, and a format change walks three rows. One 400 ms debounce covers all of
   it, and `start()` waits for the first allocation.

### Performance

Measured on a Ryzen 7 5700X:

| Stream | Drawn | Dropped | `ffmpeg` CPU |
|---|---|---|---|
| 1280×720 @ 60 | 59.9 fps | 0 | ~107 % of one core |
| 1920×1080 @ 60 | 60 fps | | 375 MB/s through the pipe |

Drawing a frame (`_show`) costs 0.3 ms.
