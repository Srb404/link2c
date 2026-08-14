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
#define SEL_MODE       2   /* byte 0 = mode id, byte 1 = mode flag */
#define SEL_SERIAL    12   /* 32 bytes, ASCII */
#define SEL_RESET     14   /* 1 byte, SET only */
#define SEL_FRAMING   19   /* 1 byte: 1=head 2=half 3=full */

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

/* Writes are always the selector's full length, zero-padded. */
static int xu_set(int fd, __u8 unit, __u8 sel, const unsigned char *val, int nval)
{
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
        for (int sel = 1; sel <= 24; sel++) {
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

static int mode_set(int fd, const char *name)
{
    static const struct { const char *n; unsigned char id, flag; } modes[] = {
        { "off",        0x00, 0x00 },
        { "track",      0x01, 0x00 },
        { "whiteboard", 0x04, 0x00 },
        { "overhead",   0x05, 0x03 },
        { "deskview",   0x06, 0x10 },
    };
    for (unsigned i = 0; i < sizeof modes / sizeof *modes; i++) {
        if (strcmp(modes[i].n, name)) continue;
        unsigned char v[2] = { modes[i].id, modes[i].flag };
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
    fprintf(stderr, "unknown mode '%s' (off track whiteboard overhead deskview)\n", name);
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
        case 0x04: m = "whiteboard"; break;
        case 0x05: m = "overhead";   break;
        case 0x06: m = "deskview";   break;
        case 0xff: m = "none (no active stream)"; break;
        }
        printf("mode   : %s (0x%02x)\n", m, b[0]);
    }
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
      "  mode <name>             off | track | whiteboard | overhead | deskview\n"
      "  framing <name>          head | half | full\n"
      "  reset                   pulse the recentre control\n"
      "  scan                    dump every XU selector (length, access, value)\n"
      "  get <unit> <sel>        read one selector\n"
      "  set <unit> <sel> <hex>  write one selector, zero-padded to its length\n"
      "\n"
      "Modes only take effect while something streams from the camera.\n"
      "Zoom and pan/tilt are ordinary V4L2 controls - use v4l2-ctl; pan/tilt\n"
      "only moves the image once zoom_absolute is above 100.\n", p);
}

int main(int argc, char **argv)
{
    const char *e = getenv("LINK_DEV");
    if (e) dev_path = e;

    if (argc < 2) { usage(argv[0]); return 2; }

    int fd = open(dev_path, O_RDWR);
    if (fd < 0) { perror(dev_path); return 1; }

    int rc = 0;
    const char *cmd = argv[1];
    if (!strcmp(cmd, "scan"))                       scan(fd);
    else if (!strcmp(cmd, "info"))                  rc = info(fd);
    else if (!strcmp(cmd, "mode")    && argc == 3)  rc = mode_set(fd, argv[2]);
    else if (!strcmp(cmd, "framing") && argc == 3)  rc = framing_set(fd, argv[2]);
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
