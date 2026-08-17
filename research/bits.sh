#!/bin/bash
# Toggle each bit of unit 9 selector 27 in turn and watch for an image flip.
#
# The vendor app reaches horizontal mirror by read-modify-writing a single bit
# of this two-byte control, so the whole feature set lives in these 16 bits.
# Every write is a one-bit change off the current value and is reverted before
# the next, and the camera is checked for presence after each one.
cd "$(dirname "$0")"

DEV=/dev/video0
SEL="9 27"
OUT=bits
PHASES=bitsphases.txt

alive() { lsusb -d 2e1a:4c03 >/dev/null 2>&1 && [ -e "$DEV" ]; }

alive || { echo "camera is not in UVC mode"; exit 1; }

ORIG=$(link2c get $SEL | tr -d ' ')
echo "initial unit 9 sel 27: $ORIG"
[ ${#ORIG} -ne 4 ] && { echo "expected 2 bytes"; exit 1; }
B0=$((0x${ORIG:0:2})); B1=$((0x${ORIG:2:2}))
WORD=$(( B0 | (B1 << 8) ))
printf "as 16 bits: 0x%04x\n\n" $WORD

pkill -x ffmpeg 2>/dev/null; sleep 2
rm -rf "$OUT"; mkdir -p "$OUT"
v4l2-ctl -d "$DEV" --set-ctrl=pan_absolute=0 --set-ctrl=tilt_absolute=0 \
                   --set-ctrl=zoom_absolute=100 >/dev/null 2>&1
sleep 3
ffmpeg -nostdin -loglevel error -f v4l2 -input_format mjpeg -video_size 1920x1080 \
    -i "$DEV" -vf fps=2 -q:v 4 "$OUT"/x_%04d.jpg > capbits.log 2>&1 &
FF=$!
sleep 6
: > "$PHASES"
echo "$(ls "$OUT" | wc -l) BASE - -" >> "$PHASES"
sleep 4

restore() { link2c set 9 27 $(printf "%02x %02x" $((WORD & 0xff)) $((WORD >> 8))); }

for BIT in $(seq 0 15); do
    NEW=$(( WORD ^ (1 << BIT) ))
    HEX=$(printf "%02x %02x" $((NEW & 0xff)) $((NEW >> 8)))
    alive || { echo "!!! camera gone before bit $BIT"; exit 9; }
    link2c set 9 27 $HEX >/dev/null 2>&1
    if ! alive; then
        echo "!!! CAMERA GONE after toggling bit $BIT (wrote $HEX)"
        pkill -x ffmpeg 2>/dev/null
        exit 9
    fi
    sleep 3
    if ! alive; then
        echo "!!! CAMERA GONE 3 s after bit $BIT"
        pkill -x ffmpeg 2>/dev/null
        exit 9
    fi
    READ=$(link2c get 9 27 | tr -d ' ')
    echo "$(ls "$OUT" | wc -l) bit $BIT $READ" >> "$PHASES"
    printf "  bit %2d -> wrote %s, read %s\n" "$BIT" "$HEX" "$READ"
    restore >/dev/null 2>&1
    sleep 2
done

kill $FF 2>/dev/null; sleep 1
echo
echo "restored: $(link2c get 9 27)"
echo "frames: $(ls "$OUT" | wc -l)"
