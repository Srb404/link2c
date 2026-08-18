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
| 2 | 61 B | mode: byte 0 id, byte 1 flag, see [Modes](#modes) | confirmed |
| 11 | 5 B, GET | tracking state (the vendor app only reads it) | |
| 12 | 32 B | serial number, ASCII | confirmed |
| 13 | 129 B | relative pan/tilt (from Link/Link 2) | untested |
| 14 | 1 B, SET | reset framing | accepted, no visible effect (no gimbal) |
| 17 | | **firmware-update switch** | [never write](#dangerous-writes) |
| 19 | 1 B | framing: `01` head, `02` half body, `03` full body | accepted, no visible effect yet |
| 27 | 2 B | feature bitmask, see [below](#feature-bitmask) | partly mapped |

### Unit 10

| Selector | Size | Meaning | Status |
|---:|---|---|---|
| 1 | 8 B, GET | tracking target on Link/Link 2 | read-only on the 2C |
| 9 | | **firmware-update switch** (`02`) | [never write](#dangerous-writes) |
| 15 | 2 B | privacy (gimbal down) | no effect, always reads `02 00` |

### Feature bitmask

Unit 9 selector 27. Only three bits are known so far; the rest belong to
features the vendor app toggles through the same call.

| Bit | Feature |
|---:|---|
| 3 | horizontal mirror |
| 5 | **firmware-update mode, [never set](#dangerous-writes)** |
| 12 | vertical mirror |

Bits 3 and 12 together are a 180° rotation. The tested unit shipped with
`0x11c8`, with both mirror bits set, which is why its image looked upside down out of
the box.

### Modes

Byte 0 of unit 9 selector 2 is the mode id, byte 1 a per-mode flag:

| Mode | Id | Flag |
|---|---:|---:|
| off | `00` | `00` |
| tracking | `01` | `00` |
| whiteboard | `04` | `00` |
| overhead | `05` | `03` |
| DeskView | `06` | `10` |

The selector is 61 bytes long; `link2c` writes the first two and zeroes the
rest. What the other bytes hold is still unknown.

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

## GUI preview

```
camera (MJPEG 1080p) → ffmpeg → scale to 640×360 at 15 fps → raw rgb24 pipe → Gdk.MemoryTexture
```

GStreamer's `v4l2src` would need `gst-plugins-good`, and OpenCV is a heavy
dependency for one picture, so the preview is a plain `ffmpeg` pipe read in a
worker thread. Each frame becomes a `Gdk.MemoryTexture` on the UI thread.
