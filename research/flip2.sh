#!/bin/bash
# Hunt for an in-camera image flip among the remaining one-byte XU selectors.
#
# The previous sweep lost the camera to unit 9 selector 17 (firmware-update
# mode) and kept writing blind for another ten selectors. This one checks the
# device is still on the bus after every single write and stops dead the
# moment it is not, so at worst one write is lost instead of the whole run.
cd "$(dirname "$0")"

DEV=/dev/video0
OUT=fl2
PHASES=fl2phases.txt

alive() {
    lsusb -d 2e1a:4c03 >/dev/null 2>&1 && [ -e "$DEV" ]
}

die_if_gone() {   # $1 = what we just wrote
    if ! alive; then
        echo "!!! CAMERA GONE after writing $1" | tee -a "$PHASES"
        echo "!!! stopping the sweep, replug needed"
        pkill -x ffmpeg 2>/dev/null
        exit 9
    fi
}

alive || { echo "camera is not attached in UVC mode"; exit 1; }

pkill -x ffmpeg 2>/dev/null; sleep 2
rm -rf "$OUT"; mkdir -p "$OUT"
v4l2-ctl -d "$DEV" --set-ctrl=pan_absolute=0 --set-ctrl=tilt_absolute=0 \
                   --set-ctrl=zoom_absolute=100 >/dev/null 2>&1
sleep 3

ffmpeg -nostdin -loglevel error -f v4l2 -input_format mjpeg -video_size 1920x1080 \
    -i "$DEV" -vf fps=2 -q:v 4 "$OUT"/x_%04d.jpg > capfl2.log 2>&1 &
FF=$!
sleep 6
: > "$PHASES"
echo "$(ls "$OUT" | wc -l) BASE - -" >> "$PHASES"
sleep 4

# Everything one-byte and writable that the first sweep never reached.
# Unit 9 selector 17 is deliberately absent: that is the firmware switch.
for c in "10 10" "10 18" "10 20" "10 22" "10 23" "11 2" "11 4"; do
    set -- $c; U=$1; S=$2
    ORIG=$(link2c get "$U" "$S" 2>/dev/null | tr -d ' ')
    [ -z "$ORIG" ] && { echo "skipping $U/$S - no read-back"; continue; }
    echo "--- unit $U sel $S (original $ORIG) ---"

    for V in 00 01 02 03; do
        [ "$ORIG" = "$V" ] && continue
        alive || { echo "!!! camera gone before writing $U/$S=$V"; exit 9; }
        link2c set "$U" "$S" "$V" >/dev/null 2>&1
        die_if_gone "unit $U sel $S = $V"
        sleep 3
        die_if_gone "unit $U sel $S = $V (after 3 s)"
        echo "$(ls "$OUT" | wc -l) $U $S $V" >> "$PHASES"
        sleep 0.5
    done

    link2c set "$U" "$S" "$ORIG" >/dev/null 2>&1
    die_if_gone "restoring $U/$S=$ORIG"
    sleep 2
done

kill $FF 2>/dev/null; sleep 1
echo "OK - frames: $(ls "$OUT" | wc -l), phases: $(wc -l < "$PHASES")"
