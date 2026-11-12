/*
 * link2c - control an Insta360 Link 2C from Linux, without the vendor app.
 *
 * The 2C is a UVC device: zoom and digital pan/tilt are plain V4L2 controls
 * (use v4l2-ctl for those). Everything proprietary lives behind three UVC
 * Extension Units, reached here via UVCIOC_CTRL_QUERY.
 *
 * Three things the camera insists on, all measured on this unit:
 *   - Modes need a live stream. With nothing reading from the device every
 *     mode, 'off' included, reads back as 0xff however it was written.
 *   - The mode byte settles a second or two after the write, so read it back
 *     after a pause; an immediate GET_CUR still says 0xff.
 *   - Digital pan/tilt does nothing at 1x zoom; the crop already covers the
 *     whole sensor. Zoom in first, then pan.
 *
 * 0xff on read-back means the camera declined the mode. Streaming, that only
 * happens for tracking, which wants a person in frame before it will engage.
 *
 * Mirroring lives in unit 9 selector 27, a two-byte bitfield: bit 3 flips the
 * image horizontally, bit 12 vertically, and both together read as a 180-degree
 * rotation. The camera applies the change when a stream starts, so a running
 * preview keeps showing the old orientation until it is restarted. Bit 5 of the
 * same word is the firmware-update switch, so writes here go through a mask.
 *
 * DANGEROUS: three writes drop the camera off USB and bring it back as an
 * Ambarella A9 mass-storage device (firmware-update mode): unit 9 selector 17
 * (any non-zero value), unit 10 selector 9 = 0x02, and bit 5 of unit 9
 * selector 27 - the last one is stored, so the camera keeps falling back into
 * that mode until the bit is cleared. All three are refused below unless
 * --force is given; link2c-recover brings the camera back without a replug.
 *
 * The 2C's XU GUIDs differ from the Link 1/2, so the selector map below was
 * inherited from the community Link 1/2 work and re-checked here. Confirmed
 * on this camera: off, deskview, overhead (accepted and held),
 * and deskview visibly transforms the image. Tracking is accepted only with
 * a subject present; whether it then reframes is not yet confirmed.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <sys/ioctl.h>
#include <linux/uvcvideo.h>
#include <linux/usb/video.h>

#define XU_MAIN        9   /* main vendor unit */
#define XU_AUX        10   /* second vendor unit */
#define SEL_FLAGS     27   /* 2-byte bitfield of feature toggles, see below */
#define BIT_AUTOFRAME  0   /* auto framing; composition only applies with it */
#define BIT_HDR        2
#define BIT_MIRROR_H   3   /* horizontal mirror */
#define BIT_USB_MODE   5   /* DO NOT SET: drops to firmware-update mode */
#define BIT_STARTUP    6   /* camera keeps its settings across power-up */
#define BIT_ONETOUCH   7   /* one-touch tracking */
#define BIT_AUTOTRACK  8   /* auto tracking */
#define BIT_SMART      9   /* "smart adjustment": keeps the head centred */
#define BIT_PORTRAIT  10   /* forced portrait */
#define BIT_PRIVACY   11   /* full privacy */
#define BIT_MIRROR_V  12   /* vertical mirror */
#define SEL_MODE       2   /* 61 bytes, see mode_set */
#define SEL_SERIAL    12   /* 32 bytes, ASCII */
#define SEL_RESET     14   /* 1 byte, SET only */
#define SEL_SPEED     18   /* 1 byte: 0=fast 1=normal 2=slow */
#define SEL_FRAMING   19   /* 1 byte: 1=head 2=half 3=full */
#define SEL_LEVEL     28   /* unit 10, 25 bytes: byte[0]=horizontal correction */

/* The by-id link survives other cameras (or v4l2loopback) taking video0. */
#define DEV_BY_ID "/dev/v4l/by-id/usb-Insta360_Insta360_Link_2C-video-index0"
static const char *dev_path = DEV_BY_ID;

static int xu(int fd, __u8 unit, __u8 sel, __u8 query, void *data, __u16 size)
{
    struct uvc_xu_control_query q = {
        .unit = unit, .selector = sel, .query = query,
        .size = size, .data = data,
    };
    return ioctl(fd, UVCIOC_CTRL_QUERY, &q);
}

static int xu_len(int fd, __u8 unit, __u8 sel)
{
    unsigned char l[2] = { 0, 0 };
    if (xu(fd, unit, sel, UVC_GET_LEN, l, 2) < 0) return -1;
    return l[0] | (l[1] << 8);
}

static int force_writes = 0;

/* Selectors that do something other than what a control panel wants. */
static int is_dangerous(__u8 unit, __u8 sel)
{
    /* Both of these drop the camera off USB into firmware-update mode.
       Found by sweeping one-byte selectors; recovery needs link2c-recover. */
    if (unit == 9  && sel == 17) return 1;
    if (unit == 10 && sel == 9)  return 1;
    return 0;
}

/* Writes are always the selector's full length, zero-padded. */
static int xu_set(int fd, __u8 unit, __u8 sel, const unsigned char *val, int nval)
{
    if (unit == XU_MAIN && sel == SEL_FLAGS && nval >= 1 &&
        (val[0] & (1 << BIT_USB_MODE)) && !force_writes) {
        fprintf(stderr, "refusing to set bit %d of unit 9 selector 27: that is the\n"
                        "firmware-update switch, not an image setting.\n", BIT_USB_MODE);
        return -1;
    }
    if (is_dangerous(unit, sel) && !force_writes) {
        fprintf(stderr,
                "refusing to write unit %d selector %d: this drops the camera off USB\n"
                "into firmware-update mode. Run link2c-recover to bring it back.\n"
                "Pass --force if you really mean it.\n",
                unit, sel);
        return -1;
    }
    int len = xu_len(fd, unit, sel);
    if (len <= 0) { fprintf(stderr, "unit %d sel %d: no such control\n", unit, sel); return -1; }
    unsigned char *buf = calloc(1, len);
    memcpy(buf, val, nval < len ? nval : len);
    int rc = xu(fd, unit, sel, UVC_SET_CUR, buf, len);
    if (rc < 0) fprintf(stderr, "SET_CUR unit %d sel %d: %s\n", unit, sel, strerror(errno));
    free(buf);
    return rc;
}

static int xu_get(int fd, __u8 unit, __u8 sel, unsigned char *buf, int max)
{
    int len = xu_len(fd, unit, sel);
    if (len <= 0 || len > max) return -1;
    if (xu(fd, unit, sel, UVC_GET_CUR, buf, len) < 0) return -1;
    return len;
}

static void scan(int fd)
{
    const int units[] = { 9, 10, 11 };
    for (unsigned u = 0; u < sizeof units / sizeof *units; u++) {
        printf("\n=== XU unit %d ===\n", units[u]);
        for (int sel = 1; sel <= 32; sel++) {
            int len = xu_len(fd, units[u], sel);
            if (len < 0) continue;
            unsigned char info = 0;
            xu(fd, units[u], sel, UVC_GET_INFO, &info, 1);
            printf("  sel %2d: len=%3d  GET=%c SET=%c", sel, len,
                   (info & 1) ? 'Y' : 'N', (info & 2) ? 'Y' : 'N');
            unsigned char b[64];
            if ((info & 1) && len > 0 && len <= 64 &&
                xu(fd, units[u], sel, UVC_GET_CUR, b, len) == 0) {
                printf("  cur=[");
                for (int i = 0; i < (len > 16 ? 16 : len); i++)
                    printf("%s%02x", i ? " " : "", b[i]);
                printf("%s]", len > 16 ? " ..." : "");
            }
            putchar('\n');
        }
    }
}

/*
 * The mode selector is a 61-byte struct, not a mode byte. The vendor app
 * (setVideoMode, read from its binary) fills it like this for the 2C:
 *   [0]      mode id
 *   [2..37]  whiteboard corners (unused here)
 *   [38..41] two words, 0xffff = unset (the camera reads them back clamped)
 *   [42..49] two dwords, both 0x0e1a
 *   [52..53] camera pitch in 0.1 degree, signed; the app sends -45.0 by
 *            default and, for deskview, the live pitch the camera itself
 *            reports in this same field - the perspective fix is built on it
 * Writing just two bytes and zeros after them used to zero all of that.
 */
#define MODE_LEN    61
#define MODE_PITCH  52

static int mode_set(int fd, const char *name)
{
    static const struct { const char *n; unsigned char id, flag; } modes[] = {
        { "off",        0x00, 0x00 },
        { "track",      0x01, 0x00 },
        { "overhead",   0x05, 0x03 },
        { "deskview",   0x06, 0x10 },
    };
    for (unsigned i = 0; i < sizeof modes / sizeof *modes; i++) {
        if (strcmp(modes[i].n, name)) continue;

        unsigned char cur[MODE_LEN] = { 0 };
        int have = xu_get(fd, XU_MAIN, SEL_MODE, cur, sizeof cur) == MODE_LEN;
        short pitch = have ? (short)(cur[MODE_PITCH] | (cur[MODE_PITCH + 1] << 8)) : 0;
        if (!have || modes[i].id != 0x06 || pitch == 0)
            pitch = -450;

        unsigned char v[MODE_LEN] = { 0 };
        v[0] = modes[i].id;
        v[1] = modes[i].flag;
        memset(v + 38, 0xff, 4);
        v[42] = v[46] = 0x1a;
        v[43] = v[47] = 0x0e;
        v[MODE_PITCH] = pitch & 0xff;
        v[MODE_PITCH + 1] = (pitch >> 8) & 0xff;
        if (xu_set(fd, XU_MAIN, SEL_MODE, v, sizeof v) < 0) return 1;

        /* The mode byte needs a moment to settle before it reads true. */
        struct timespec ts = { .tv_sec = 2, .tv_nsec = 500000000 };
        nanosleep(&ts, NULL);

        unsigned char back[64];
        int n = xu_get(fd, XU_MAIN, SEL_MODE, back, sizeof back);
        if (n <= 0) { fprintf(stderr, "mode '%s': read-back failed\n", name); return 1; }

        if (back[0] == modes[i].id) {
            printf("mode '%s' active\n", name);
            return 0;
        }
        fprintf(stderr, "mode '%s' declined (reads back 0x%02x)\n", name, back[0]);
        if (back[0] == 0xff)
            fprintf(stderr, "  the camera takes modes only while something streams from %s;\n"
                            "  tracking additionally needs a person in frame.\n", dev_path);
        return 1;
    }
    fprintf(stderr, "unknown mode '%s' (off track overhead deskview)\n", name);
    return 2;
}

static int framing_set(int fd, const char *name)
{
    unsigned char v;
    if (!strcmp(name, "head")) v = 1;
    else if (!strcmp(name, "half")) v = 2;
    else if (!strcmp(name, "full")) v = 3;
    else { fprintf(stderr, "unknown framing '%s' (head half full)\n", name); return 2; }
    if (xu_set(fd, XU_MAIN, SEL_FRAMING, &v, 1) < 0) return 1;
    printf("framing '%s' set\n", name);
    return 0;
}

static const struct { const char *n; int bit; } features[] = {
    { "autoframe", BIT_AUTOFRAME },
    { "hdr",       BIT_HDR },
    { "mirror-h",  BIT_MIRROR_H },
    { "mirror-v",  BIT_MIRROR_V },
    { "smart",     BIT_SMART },
    { "startup",   BIT_STARTUP },
    { "onetouch",  BIT_ONETOUCH },
    { "autotrack", BIT_AUTOTRACK },
    { "portrait",  BIT_PORTRAIT },
    { "privacy",   BIT_PRIVACY },
};

static int feature_word(int fd, unsigned *word)
{
    unsigned char b[8];
    if (xu_get(fd, XU_MAIN, SEL_FLAGS, b, sizeof b) < 2) {
        fprintf(stderr, "cannot read feature word\n");
        return -1;
    }
    *word = b[0] | (b[1] << 8);
    return 0;
}

static int parse_state(const char *state, int cur)
{
    if (!strcmp(state, "on"))     return 1;
    if (!strcmp(state, "off"))    return 0;
    if (!strcmp(state, "toggle")) return !cur;
    fprintf(stderr, "state must be on, off or toggle\n");
    return -1;
}

/* Read-modify-write one bit of the feature word, exactly as the vendor app does. */
static int feature_set(int fd, const char *name, const char *state)
{
    int bit = -1;
    for (unsigned i = 0; i < sizeof features / sizeof *features; i++)
        if (!strcmp(features[i].n, name)) bit = features[i].bit;
    if (bit < 0) {
        fprintf(stderr, "unknown feature '%s' (", name);
        for (unsigned i = 0; i < sizeof features / sizeof *features; i++)
            fprintf(stderr, "%s%s", i ? " " : "", features[i].n);
        fprintf(stderr, ")\n");
        return 2;
    }

    unsigned word;
    if (feature_word(fd, &word) < 0) return 1;
    int cur = !!(word & (1u << bit));
    if (!state) {
        printf("%s: %s\n", name, cur ? "on" : "off");
        return 0;
    }
    int on = parse_state(state, cur);
    if (on < 0) return 2;
    word = on ? word | (1u << bit) : word & ~(1u << bit);

    word &= ~(1u << BIT_USB_MODE);          /* never carry the mode bit along */
    unsigned char out[2] = { word & 0xff, (word >> 8) & 0xff };
    if (xu_set(fd, XU_MAIN, SEL_FLAGS, out, 2) < 0) return 1;

    printf("%s -> %s (word 0x%04x)\n", name, on ? "on" : "off", word);
    if (bit == BIT_MIRROR_H || bit == BIT_MIRROR_V)
        printf("restart the stream for it to take effect\n");
    return 0;
}

static int speed_set(int fd, const char *name)
{
    static const char *names[] = { "fast", "normal", "slow" };
    for (unsigned char v = 0; v < 3; v++) {
        if (strcmp(names[v], name)) continue;
        if (xu_set(fd, XU_MAIN, SEL_SPEED, &v, 1) < 0) return 1;
        printf("tracking speed '%s' set\n", name);
        return 0;
    }
    fprintf(stderr, "unknown speed '%s' (fast normal slow)\n", name);
    return 2;
}

static int level_set(int fd, const char *state)
{
    unsigned char b[32];
    if (xu_get(fd, XU_AUX, SEL_LEVEL, b, sizeof b) < 1) {
        fprintf(stderr, "cannot read horizontal correction\n");
        return 1;
    }
    if (!state) {
        printf("level: %s\n", b[0] ? "on" : "off");
        return 0;
    }
    int on = parse_state(state, b[0]);
    if (on < 0) return 2;
    unsigned char v = on;
    if (xu_set(fd, XU_AUX, SEL_LEVEL, &v, 1) < 0) return 1;
    printf("level -> %s\n", on ? "on" : "off");
    return 0;
}

static int info(int fd)
{
    unsigned char b[64];
    int n = xu_get(fd, XU_MAIN, SEL_SERIAL, b, sizeof b);
    if (n > 0) {
        b[n - 1] = 0;
        printf("serial : %s\n", (char *)b);
    }
    n = xu_get(fd, XU_MAIN, SEL_MODE, b, sizeof b);
    if (n > 0) {
        const char *m = "unknown";
        switch (b[0]) {
        case 0x00: m = "off";        break;
        case 0x01: m = "track";      break;
        case 0x05: m = "overhead";   break;
        case 0x06: m = "deskview";   break;
        case 0xff: m = "none (no active stream)"; break;
        }
        printf("mode   : %s (0x%02x)\n", m, b[0]);
    }
    n = xu_get(fd, XU_MAIN, SEL_FLAGS, b, sizeof b);
    if (n >= 2) {
        unsigned w = b[0] | (b[1] << 8);
        printf("mirror : h=%s v=%s (word 0x%04x)\n",
               (w & (1u << BIT_MIRROR_H)) ? "on" : "off",
               (w & (1u << BIT_MIRROR_V)) ? "on" : "off", w);
        printf("flags  :");
        for (unsigned i = 0; i < sizeof features / sizeof *features; i++)
            if (features[i].bit != BIT_MIRROR_H && features[i].bit != BIT_MIRROR_V)
                printf(" %s=%s", features[i].n,
                       (w & (1u << features[i].bit)) ? "on" : "off");
        putchar('\n');
    }
    n = xu_get(fd, XU_MAIN, SEL_SPEED, b, sizeof b);
    if (n > 0)
        printf("speed  : %s (0x%02x)\n",
               b[0] == 0 ? "fast" : b[0] == 1 ? "normal" : b[0] == 2 ? "slow" : "?", b[0]);
    n = xu_get(fd, XU_AUX, SEL_LEVEL, b, sizeof b);
    if (n > 0)
        printf("level  : %s\n", b[0] ? "on" : "off");
    n = xu_get(fd, XU_MAIN, SEL_FRAMING, b, sizeof b);
    if (n > 0)
        printf("framing: %s (0x%02x)\n",
               b[0] == 1 ? "head" : b[0] == 2 ? "half" : b[0] == 3 ? "full" : "?", b[0]);
    return 0;
}

static void usage(const char *p)
{
    fprintf(stderr,
      "usage: %s <command> [args]        (device: $LINK_DEV, default the by-id link)\n\n"
      "  info                    serial, current mode and framing\n"
      "  mode <name>             off | track | overhead | deskview\n"
      "  framing <name>          head | half | full (needs autoframe on)\n"
      "  speed <name>            tracking speed: fast | normal | slow\n"
      "  feature <name> [state]  autoframe | hdr | smart | startup | onetouch |\n"
      "                          autotrack | portrait | privacy | mirror-h |\n"
      "                          mirror-v; state on | off | toggle, none reports\n"
      "  mirror <h|v> [state]    shorthand for feature mirror-h / mirror-v\n"
      "  level [state]           horizontal correction (auto-levels the image)\n"
      "  reset                   pulse the recentre control\n"
      "  scan                    dump every XU selector (length, access, value)\n"
      "  get <unit> <sel>        read one selector\n"
      "  set <unit> <sel> <hex>  write one selector, zero-padded to its length\n"
      "  --force                 permit writes to selectors known to be unsafe\n\n"
      "Unit 9 selector 17, unit 10 selector 9 and bit 5 of unit 9 selector 27 switch\n"
      "the camera into firmware-update mode; all are refused without --force.\n"
      "link2c-recover undoes it.\n\n"
      "Mirroring is applied when a stream starts, so restart the stream after\n"
      "changing it. Modes only take effect while something streams from the camera.\n"
      "Zoom and pan/tilt are ordinary V4L2 controls - use v4l2-ctl; pan/tilt\n"
      "only moves the image once zoom_absolute is above 100.\n", p);
}

int main(int argc, char **argv)
{
    const char *e = getenv("LINK_DEV");
    if (e) dev_path = e;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--force")) continue;
        force_writes = 1;
        for (int j = i; j < argc - 1; j++) argv[j] = argv[j + 1];
        argc--;
        break;
    }
    if (argc < 2) { usage(argv[0]); return 2; }

    int fd = open(dev_path, O_RDWR);
    if (fd < 0) { perror(dev_path); return 1; }

    int rc = 0;
    const char *cmd = argv[1];
    if (!strcmp(cmd, "scan"))                       scan(fd);
    else if (!strcmp(cmd, "info"))                  rc = info(fd);
    else if (!strcmp(cmd, "mode")    && argc == 3)  rc = mode_set(fd, argv[2]);
    else if (!strcmp(cmd, "framing") && argc == 3)  rc = framing_set(fd, argv[2]);
    else if (!strcmp(cmd, "speed")   && argc == 3)  rc = speed_set(fd, argv[2]);
    else if (!strcmp(cmd, "feature") && (argc == 3 || argc == 4))
        rc = feature_set(fd, argv[2], argc == 4 ? argv[3] : NULL);
    else if (!strcmp(cmd, "mirror")  && (argc == 3 || argc == 4)) {
        if (strcmp(argv[2], "h") && strcmp(argv[2], "v")) {
            fprintf(stderr, "axis must be h or v\n"); rc = 2;
        } else {
            char name[16];
            snprintf(name, sizeof name, "mirror-%s", argv[2]);
            rc = feature_set(fd, name, argc == 4 ? argv[3] : NULL);
        }
    }
    else if (!strcmp(cmd, "level")   && (argc == 2 || argc == 3))
        rc = level_set(fd, argc == 3 ? argv[2] : NULL);
    else if (!strcmp(cmd, "reset")) {
        unsigned char one = 1;
        rc = xu_set(fd, XU_MAIN, SEL_RESET, &one, 1) < 0;
    } else if (!strcmp(cmd, "get") && argc == 4) {
        unsigned char b[512];
        int n = xu_get(fd, atoi(argv[2]), atoi(argv[3]), b, sizeof b);
        if (n < 0) { fprintf(stderr, "read failed: %s\n", strerror(errno)); rc = 1; }
        else { for (int i = 0; i < n; i++) printf("%s%02x", i ? " " : "", b[i]); putchar('\n'); }
    } else if (!strcmp(cmd, "set") && argc >= 5) {
        unsigned char b[512];
        int n = 0;
        for (int i = 4; i < argc && n < (int)sizeof b; i++)
            b[n++] = (unsigned char)strtoul(argv[i], NULL, 16);
        rc = xu_set(fd, atoi(argv[2]), atoi(argv[3]), b, n) < 0;
    } else { usage(argv[0]); rc = 2; }

    close(fd);
    return rc;
}
