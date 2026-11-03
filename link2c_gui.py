#!/usr/bin/env python3
"""
link2c-gui - control panel for the Insta360 Link 2C.

The camera only accepts mode changes while something is streaming from it,
so the live preview is not a luxury here: it is what makes the mode buttons
work. Turn the preview off and the modes go with it, unless another app
(OBS, a call) is holding the stream instead.

Camera access goes through the link2c CLI and v4l2-ctl rather than being
reimplemented here, so there is one place where the protocol lives.
"""
import configparser
import os
import re
import shutil
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

# Desktop launchers hand us the login session's PATH, which has no ~/.local/bin
# in it, so the link2c CLI sitting next to this script would go unfound and the
# tool check below would kill us before a window ever showed up.
os.environ["PATH"] = os.path.dirname(os.path.abspath(__file__)) + os.pathsep + os.environ.get("PATH", "")

BY_ID = "/dev/v4l/by-id/usb-Insta360_Insta360_Link_2C-video-index0"
# by-id rather than video0: another camera or v4l2loopback can take video0.
DEV = os.environ.get("LINK_DEV") or (BY_ID if os.path.exists(BY_ID) else "/dev/video0")
os.environ["LINK_DEV"] = DEV   # the link2c CLI picks the same device
# cameractrlsd re-applies this preset whenever the camera is plugged in (the
# camera forgets its image settings), so changes made here are saved into it.
PRESET = os.path.expanduser(
    "~/.config/hu.irl.cameractrls/" + os.path.basename(BY_ID) + ".ini")
PRESET_CTRLS = {
    "brightness", "contrast", "saturation", "hue", "sharpness",
    "white_balance_automatic", "white_balance_temperature",
    "power_line_frequency", "focus_automatic_continuous", "focus_absolute",
}
POWER_LINE = [(1, "50_hz", "50 Hz"), (2, "60_hz", "60 Hz"), (0, "disabled", "Off")]
# Heights the preview pipe may run at; it picks the first one that covers
# the widget's real (scale-factor aware) size, so the picture is not upscaled.
PREVIEW_STEPS = (360, 540, 720, 900, 1080)

MODES = [
    ("off", "Normal", "No processing"),
    ("track", "AI tracking", "Keeps the person in frame"),
    ("deskview", "DeskView", "Desk view with perspective correction"),
    ("overhead", "Overhead", "Document view"),
]

FRAMINGS = [("head", "Head"), ("half", "Half body"), ("full", "Full body")]
SPEEDS = [("fast", "Fast"), ("normal", "Normal"), ("slow", "Slow")]


def list_formats(dev):
    """Parse v4l2-ctl into {fourcc: {(w, h): [fps, ...]}} of what the camera offers."""
    ok, out = run(["v4l2-ctl", "-d", dev, "--list-formats-ext"])
    if not ok:
        return {}
    formats, fourcc, size = {}, None, None
    for line in out.splitlines():
        m = re.search(r"\]:\s*'(\w+)'", line)
        if m:
            fourcc = m.group(1)
            formats[fourcc] = {}
            continue
        m = re.search(r"Size:\s*Discrete\s*(\d+)x(\d+)", line)
        if m and fourcc:
            size = (int(m.group(1)), int(m.group(2)))
            formats[fourcc][size] = []
            continue
        m = re.search(r"\(([\d.]+)\s*fps\)", line)
        if m and fourcc and size:
            formats[fourcc][size].append(int(float(m.group(1))))
    return formats


def save_preset(values):
    """Write image controls into the cameractrls preset, keeping the rest."""
    values = {k: v for k, v in values.items() if k in PRESET_CTRLS}
    if not values:
        return
    cfg = configparser.ConfigParser()
    cfg.read(PRESET)
    if not cfg.has_section("preset_1"):
        cfg.add_section("preset_1")
    for k, v in values.items():
        if k == "power_line_frequency":
            v = next((n for i, n, _ in POWER_LINE if i == v), v)
        cfg["preset_1"][k] = str(v)
    os.makedirs(os.path.dirname(PRESET), exist_ok=True)
    with open(PRESET, "w") as f:
        cfg.write(f)


def run_bg(cmd, done=None):
    """Run a command off the UI thread; done(ok, out) comes back on it."""
    def work():
        ok, out = run(cmd)
        if done:
            GLib.idle_add(lambda: done(ok, out) and False)
    threading.Thread(target=work, daemon=True).start()


def run(cmd):
    """Run a command, returning (ok, combined output)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except Exception as exc:  # noqa: BLE001 - surfaced in the status bar
        return False, str(exc)


class V4L2:
    """Thin wrapper over v4l2-ctl for the standard UVC controls."""

    def __init__(self, dev):
        self.dev = dev
        self.ctrls = {}
        self.reload()

    def reload(self):
        ok, out = run(["v4l2-ctl", "-d", self.dev, "--list-ctrls"])
        if not ok:
            return
        pat = re.compile(
            r"^\s*(\w+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:\s*(.*)$", re.MULTILINE)
        for name, kind, rest in pat.findall(out):
            info = dict(re.findall(r"(\w+)=(-?\d+)", rest))
            self.ctrls[name] = {
                "type": kind,
                "min": int(info.get("min", 0)),
                "max": int(info.get("max", 100)),
                "step": int(info.get("step", 1)),
                "default": int(info.get("default", 0)),
                "value": int(info.get("value", 0)),
            }

    def has(self, name):
        return name in self.ctrls

    def get(self, name):
        ok, out = run(["v4l2-ctl", "-d", self.dev, f"--get-ctrl={name}"])
        m = re.search(r":\s*(-?\d+)", out) if ok else None
        return int(m.group(1)) if m else None

    def set(self, name, value):
        return run(["v4l2-ctl", "-d", self.dev, f"--set-ctrl={name}={int(value)}"])


class Preview(Gtk.Picture):
    """Live view, fed by a raw-RGB ffmpeg pipe running in a worker thread.

    The pipe decodes at the height the widget really occupies in physical
    pixels, snapped to a few steps so that resizing rarely costs a stream
    restart, and it runs at the camera's own frame rate: a fixed small
    preview looked soft on a scaled screen and a capped one looked jerky.
    """

    def __init__(self, dev, on_state):
        super().__init__()
        self.dev = dev
        self.on_state = on_state
        self.fourcc = "MJPG"
        self.size = (1920, 1080)
        self.fps = 30
        self.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.proc = None
        self.thread = None
        self.running = False
        self.out = (640, 360)      # what the pipe is producing right now
        self._want_h = PREVIEW_STEPS[0]
        self.wanted = False        # what the user asked for, vs. running = the pipe
        self._restart_id = 0
        self._pending = None       # newest frame waiting for the UI thread
        self._lock = threading.Lock()

    def set_format(self, fourcc, size, fps):
        """Re-open the stream with a new format; a running preview is restarted."""
        self.fourcc, self.size, self.fps = fourcc, size, fps
        # A new aspect or sensor mode can want a different step, and no
        # re-allocation follows a format change to recompute it for us.
        self._want_h = self._wanted_h(self.get_width(), self.get_height())
        # Picking a format walks the resolution and fps rows too, so this
        # arrives in bursts; reopen once, for the combination it lands on.
        self._later_restart()

    def _scale(self):
        """Real device scale - get_scale_factor() rounds 1.5 up to 2 and would
        make the preview ask for a third more pixels than the screen shows."""
        surface = self.get_native() and self.get_native().get_surface()
        return (surface.get_scale() if surface else 0) or self.get_scale_factor() or 1

    def _wanted_h(self, width, height):
        """Smallest step that still covers the allocation, capped by the source."""
        w, h = self.size
        # CONTAIN letterboxes, so the picture is the smaller of the two fits.
        shown = min(height, width * h / w) * self._scale()
        return min(next((s for s in PREVIEW_STEPS if s >= shown),
                        PREVIEW_STEPS[-1]), h)

    def do_measure(self, orientation, for_size):
        """Ask for a fixed size, never for the paintable's.

        Gtk.Picture normally measures its paintable, which here would feed the
        stream size back into the layout: a taller stream is a wider picture,
        a wider picture wins width from the side panel, and the wider slot asks
        for a taller stream again - the camera reopens in a loop. The widget
        expands, so it still fills whatever the window leaves it.
        """
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 320, 640, -1, -1
        return 180, 360, -1, -1

    def do_size_allocate(self, width, height, baseline):
        Gtk.Picture.do_size_allocate(self, width, height, baseline)
        want = self._wanted_h(width, height)
        if want == self._want_h:
            return
        self._want_h = want
        self._later_restart()

    def _later_restart(self):
        """Mapping a window and dragging its edge each fire a burst of
        allocations; only the size it settles on should reopen the camera."""
        if self._restart_id:
            GLib.source_remove(self._restart_id)
        self._restart_id = GLib.timeout_add(400, self._restart_now)

    def _restart_now(self):
        self._restart_id = 0
        if self.wanted:
            self.restart()
        return False

    def start(self):
        """Ask for the preview. It opens once the widget has settled on a size,
        which is not before __init__ returns, so this can run early."""
        self.wanted = True
        if self.running or self._restart_id or not self.get_height():
            return
        self._open()

    def stop(self):
        self.wanted = False
        self._close()
        self.set_paintable(None)
        self.on_state(False, "preview off")

    def restart(self):
        """Bounce the pipe - mirror, format and size only take on a fresh one."""
        if self.wanted:
            self._close()
            self._open()

    def _open(self):
        self.running = True
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _close(self):
        self.running = False
        proc, self.proc = self.proc, None
        if proc:
            proc.kill()
            try:
                # The camera stays busy until the pipe is reaped, so the reopen
                # has to wait for it - but never hang the UI on a wedged USB.
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        thread, self.thread = self.thread, None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)
        with self._lock:
            self._pending = None

    def _pump(self):
        w, h = self.size
        ph = min(self._want_h, h)
        pw = max(2, int(round(w * ph / h)) & ~1)   # keep the source aspect
        self.out = (pw, ph)
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-f", "v4l2",
            "-input_format", "h264" if self.fourcc == "H264" else "mjpeg",
            "-video_size", f"{w}x{h}",
            "-framerate", str(self.fps),
            "-i", self.dev,
            "-fps_mode", "passthrough",
            "-vf", f"scale={pw}:{ph}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            GLib.idle_add(self.on_state, False, "ffmpeg not found")
            self.running = False
            return

        nbytes = pw * ph * 3
        first = True
        while self.running:
            buf = self.proc.stdout.read(nbytes)
            if len(buf) != nbytes:
                break
            if first:
                first = False
                GLib.idle_add(self.on_state, True, "preview running")
            with self._lock:
                waiting = self._pending is not None
                self._pending = buf
            # A frame still waiting means the UI thread is behind; drop that
            # one rather than queue latency the viewer would see as lag.
            if not waiting:
                GLib.idle_add(self._show, priority=GLib.PRIORITY_DEFAULT)

        if self.running:  # ended on its own - the device is busy or gone
            err = ""
            if self.proc and self.proc.stderr:
                err = self.proc.stderr.read().decode(errors="replace").strip()
            msg = "camera is busy in another app" if "usy" in err else \
                  (err.splitlines()[-1] if err else "preview stopped")
            GLib.idle_add(self.on_state, False, msg)
        self.running = False

    def _show(self):
        with self._lock:
            buf, self._pending = self._pending, None
        pw, ph = self.out
        if buf is None or len(buf) != pw * ph * 3:   # a restart raced us
            return False
        self.set_paintable(Gdk.MemoryTexture.new(
            pw, ph, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(buf), pw * 3))
        return False


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Insta360 Link 2C")
        self.set_default_size(1080, 660)
        self.v4l2 = V4L2(DEV)
        self.formats = list_formats(DEV)
        self.busy = False
        self.streaming = False
        self._base_status = "starting preview…"
        self._pending = {}
        self.auto_rows = {}
        self.feature_rows = {}
        self._syncing = False
        self.mode_key = None

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.preview_btn = Gtk.ToggleButton(
            icon_name="video-display-symbolic", active=True,
            tooltip_text="Preview: holds the stream, modes need it")
        self.preview_btn.connect("toggled", self.on_preview_toggled)
        header.pack_end(self.preview_btn)
        self.spinner = Gtk.Spinner()
        header.pack_end(self.spinner)
        toolbar.add_top_bar(header)

        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_hexpand(True)
        left.set_margin_top(12)
        left.set_margin_bottom(12)
        left.set_margin_start(12)
        left.set_margin_end(6)
        frame = Gtk.Frame()
        frame.add_css_class("view")
        frame.set_hexpand(True)
        frame.set_vexpand(True)
        self.preview = Preview(DEV, self.on_preview_state)
        frame.set_child(self.preview)
        left.append(frame)
        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        left.append(self.status)
        split.append(left)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_size_request(380, -1)
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        panel.set_margin_top(12)
        panel.set_margin_bottom(12)
        panel.set_margin_start(6)
        panel.set_margin_end(12)
        scroller.set_child(panel)
        split.append(scroller)

        panel.append(self._build_modes())
        panel.append(self._build_tracking())
        panel.append(self._build_stream())
        panel.append(self._build_mirror())
        panel.append(self._build_view())
        panel.append(self._build_image())

        toolbar.set_content(split)
        self.set_content(toolbar)

        self.preview.start()
        self.refresh_state()

    # ---------------- panel sections ----------------

    def _group(self, title, description=None):
        g = Adw.PreferencesGroup(title=title)
        if description:
            g.set_description(description)
        return g

    def _build_modes(self):
        g = self._group("Mode", "Needs an active stream from the camera")
        self.mode_rows = {}
        self.mode_check = None
        for key, label, sub in MODES:
            row = Adw.ActionRow(title=label, subtitle=sub)
            check = Gtk.CheckButton()
            if self.mode_check is None:
                self.mode_check = check
            else:
                check.set_group(self.mode_check)
            check.connect("toggled", self.on_mode_toggled, key)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            g.add(row)
            self.mode_rows[key] = (row, check)
        return g

    def _build_tracking(self):
        g = self._group("Tracking")
        self._feature_row(g, "autoframe", "Auto framing")
        self.framing_row = Adw.ComboRow(title="Framing")
        self.framing_row.set_model(
            Gtk.StringList.new([label for _, label in FRAMINGS]))
        self.framing_row.connect("notify::selected", self.on_framing_changed)
        g.add(self.framing_row)
        self.speed_row = Adw.ComboRow(title="Speed")
        self.speed_row.set_model(
            Gtk.StringList.new([label for _, label in SPEEDS]))
        self.speed_row.connect("notify::selected", self.on_speed_changed)
        g.add(self.speed_row)
        self._feature_row(g, "smart", "Smart adjustment")
        return g

    def _feature_row(self, group, name, title):
        """A switch backed by `link2c feature <name>` (or `level`)."""
        row = Adw.SwitchRow(title=title)
        row.connect("notify::active", self.on_feature, name)
        group.add(row)
        self.feature_rows[name] = row
        return row

    def on_feature(self, row, _param, name):
        if self._syncing:
            return
        state = "on" if row.get_active() else "off"
        cmd = ["link2c", "level", state] if name == "level" else \
              ["link2c", "feature", name, state]
        self.update_framing_sensitivity()

        def done(ok, _out):
            if not ok:
                self._base_status = f"could not switch: {row.get_title()}"
                self.status.set_label(self._base_status)
                self.refresh_state()
        run_bg(cmd, done)

    def on_speed_changed(self, row, _param):
        if self._syncing:
            return
        run_bg(["link2c", "speed", SPEEDS[row.get_selected()][0]])

    def _build_mirror(self):
        g = self._group(
            "Orientation",
            "The camera mirrors the image itself, so it applies in OBS and calls too")
        self.mirror_rows = {}
        for axis, title in (("h", "Horizontal mirror"), ("v", "Vertical mirror")):
            row = Adw.SwitchRow(title=title)
            row.connect("notify::active", self.on_mirror, axis)
            g.add(row)
            self.mirror_rows[axis] = row
        self._feature_row(g, "level", "Horizontal correction")
        return g

    def on_mirror(self, row, _param, axis):
        if self._syncing:
            return
        state = "on" if row.get_active() else "off"
        self.set_busy(True, f"mirror {axis} -> {state}…")

        def work():
            ok, out = run(["link2c", "mirror", axis, state])
            GLib.idle_add(self.mirror_done, ok, out, axis, state)

        threading.Thread(target=work, daemon=True).start()

    def mirror_done(self, ok, out, axis, state):
        self.set_busy(False)
        if not ok:
            self._base_status = f"could not change mirror {axis}"
            self.status.set_label(self._base_status)
            return False
        self._base_status = f"mirror {axis}: {state}"
        self.status.set_label(self._base_status)
        # The camera only picks this up when a stream starts, so cycle ours.
        self.preview.restart()
        return False

    def _build_stream(self):
        g = self._group(
            "Stream",
            "Applies to the preview, other apps negotiate their own format")
        self._stream_ready = False

        self.fmt_row = Adw.ComboRow(title="Format")
        self.fmt_row.set_model(Gtk.StringList.new(list(self.formats) or ["—"]))
        self.fmt_row.connect("notify::selected", self.on_format_changed)
        g.add(self.fmt_row)

        self.res_row = Adw.ComboRow(title="Resolution")
        self.res_row.connect("notify::selected", self.on_res_changed)
        g.add(self.res_row)

        self.fps_row = Adw.ComboRow(title="Frame rate")
        self.fps_row.connect("notify::selected", self.on_fps_changed)
        g.add(self.fps_row)

        if not self.formats:
            for row in (self.fmt_row, self.res_row, self.fps_row):
                row.set_sensitive(False)
            self.fmt_row.set_subtitle("could not read from the camera")
            return g

        # Default to 1080p30 MJPEG when the camera offers it.
        if "MJPG" in self.formats:
            self.fmt_row.set_selected(list(self.formats).index("MJPG"))
        self.rebuild_resolutions(prefer=(1920, 1080))
        self._stream_ready = True
        return g

    def current_fourcc(self):
        names = list(self.formats)
        idx = self.fmt_row.get_selected()
        return names[idx] if 0 <= idx < len(names) else None

    def rebuild_resolutions(self, prefer=None):
        fourcc = self.current_fourcc()
        if not fourcc:
            return
        self.sizes = sorted(self.formats[fourcc], key=lambda s: s[0] * s[1])
        labels = [f"{w}×{h}" for w, h in self.sizes]
        self.res_row.handler_block_by_func(self.on_res_changed)
        self.res_row.set_model(Gtk.StringList.new(labels))
        pick = self.sizes.index(prefer) if prefer in self.sizes else len(self.sizes) - 1
        self.res_row.set_selected(pick)
        self.res_row.handler_unblock_by_func(self.on_res_changed)
        self.rebuild_fps(prefer=30)

    def rebuild_fps(self, prefer=None):
        fourcc = self.current_fourcc()
        if not fourcc:
            return
        size = self.sizes[self.res_row.get_selected()]
        self.fpses = sorted(self.formats[fourcc][size], reverse=True)
        self.fps_row.handler_block_by_func(self.on_fps_changed)
        self.fps_row.set_model(Gtk.StringList.new([str(f) for f in self.fpses]))
        pick = self.fpses.index(prefer) if prefer in self.fpses else 0
        self.fps_row.set_selected(pick)
        self.fps_row.handler_unblock_by_func(self.on_fps_changed)
        # 4K tops out below 60, so say why the choice shrank.
        self.fps_row.set_subtitle(
            "" if len(self.fpses) > 3 else "this resolution limits the frame rate")

    def on_format_changed(self, *_a):
        if not getattr(self, "_stream_ready", False):
            return
        self.rebuild_resolutions(prefer=(1920, 1080))
        self.apply_stream()

    def on_res_changed(self, *_a):
        if not getattr(self, "_stream_ready", False):
            return
        self.rebuild_fps(prefer=30)
        self.apply_stream()

    def on_fps_changed(self, *_a):
        if not getattr(self, "_stream_ready", False):
            return
        self.apply_stream()

    def apply_stream(self):
        fourcc = self.current_fourcc()
        if not fourcc:
            return
        size = self.sizes[self.res_row.get_selected()]
        fps = self.fpses[self.fps_row.get_selected()]
        self._base_status = f"stream: {fourcc} {size[0]}×{size[1]} @ {fps} fps"
        self.status.set_label(self._base_status)
        self.preview.set_format(fourcc, size, fps)

    def _build_view(self):
        g = self._group("View")
        self.zoom_row = self._slider_row(
            g, "Zoom", "zoom_absolute", fmt=lambda v: f"{v/100:.1f}×")
        self.pan_row = self._slider_row(g, "Pan", "pan_absolute",
                                        fmt=self._deg)
        self.tilt_row = self._slider_row(g, "Tilt", "tilt_absolute",
                                         fmt=self._deg)

        row = Adw.ActionRow(title="Center view")
        btn = Gtk.Button(label="Center", valign=Gtk.Align.CENTER)
        btn.connect("clicked", self.on_center)
        row.add_suffix(btn)
        g.add(row)
        return g

    def _build_image(self):
        g = self._group("Image")
        for title, name in [
            ("Brightness", "brightness"),
            ("Contrast", "contrast"),
            ("Saturation", "saturation"),
            ("Hue", "hue"),
            ("Sharpness", "sharpness"),
        ]:
            self._slider_row(g, title, name)
        self._feature_row(g, "hdr", "HDR")

        self.wb_temp_row = None
        if self.v4l2.has("white_balance_automatic"):
            self._auto_row(g, "Auto white balance", "white_balance_automatic")
            self.wb_temp_row = self._slider_row(
                g, "Color temperature", "white_balance_temperature",
                fmt=lambda v: f"{v} K")

        self.focus_row = None
        if self.v4l2.has("focus_automatic_continuous"):
            self._auto_row(g, "Autofocus", "focus_automatic_continuous")
            self.focus_row = self._slider_row(g, "Manual focus", "focus_absolute")
        self.update_auto_sensitivity()

        if self.v4l2.has("power_line_frequency"):
            row = Adw.ComboRow(title="Power line frequency")
            row.set_model(Gtk.StringList.new([lbl for _, _, lbl in POWER_LINE]))
            cur = self.v4l2.ctrls["power_line_frequency"]["value"]
            row.set_selected(next(
                (i for i, (v, _, _) in enumerate(POWER_LINE) if v == cur), 0))
            row.connect("notify::selected", self.on_power_line)
            g.add(row)
        return g

    def _auto_row(self, group, title, ctrl):
        row = Adw.SwitchRow(title=title)
        row.set_active(bool(self.v4l2.ctrls[ctrl]["value"]))
        row.connect("notify::active", self.on_auto, ctrl)
        group.add(row)
        self.auto_rows[ctrl] = row

    def update_auto_sensitivity(self):
        """Manual white balance and focus are ignored while their auto is on."""
        for ctrl, row in (("white_balance_automatic", self.wb_temp_row),
                          ("focus_automatic_continuous", self.focus_row)):
            if row and ctrl in self.auto_rows:
                row.set_sensitive(not self.auto_rows[ctrl].get_active())

    @staticmethod
    def _deg(v):
        return f"{v/3600:+.0f}°"

    def _slider_row(self, group, title, ctrl, fmt=None):
        if not self.v4l2.has(ctrl):
            return None
        info = self.v4l2.ctrls[ctrl]
        row = Adw.ActionRow(title=title)
        scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, info["min"], info["max"],
            max(info["step"], 1))
        scale.set_size_request(190, -1)
        scale.set_draw_value(False)
        scale.set_value(info["value"])
        scale.set_valign(Gtk.Align.CENTER)
        label = Gtk.Label(label=(fmt or str)(info["value"]), width_chars=5,
                          xalign=1)
        label.add_css_class("dim-label")
        label.add_css_class("numeric")
        scale.connect("value-changed", self.on_slider, ctrl, label, fmt)
        row.add_suffix(scale)
        row.add_suffix(label)
        group.add(row)
        row.scale = scale
        return row

    # ---------------- behaviour ----------------

    def on_preview_toggled(self, btn):
        if btn.get_active():
            self.preview.start()
        else:
            self.preview.stop()
        self.update_mode_sensitivity()

    def on_preview_state(self, live, msg):
        self.streaming = live
        self._base_status = msg
        self.status.set_label(msg)
        if not live and self.preview_btn.get_active() and msg != "preview off":
            self.preview_btn.set_active(False)
        self.update_mode_sensitivity()
        return False

    def update_mode_sensitivity(self):
        """Modes are dead without a stream, so say so instead of failing later."""
        live = self.streaming
        for row, _check in self.mode_rows.values():
            row.set_sensitive(live)
        self.update_framing_sensitivity()
        text = self._base_status
        if not live:
            text += "  ·  modes need a stream (turn the preview on)"
        self.status.set_label(text)

    def update_framing_sensitivity(self):
        """The camera ignores the framing choice unless auto framing is on."""
        self.framing_row.set_sensitive(self.feature_rows["autoframe"].get_active())

    def _select_mode(self, key):
        """Tick a mode row without it firing a camera write."""
        if key not in self.mode_rows:
            return
        check = self.mode_rows[key][1]
        check.handler_block_by_func(self.on_mode_toggled)
        check.set_active(True)
        check.handler_unblock_by_func(self.on_mode_toggled)

    def on_mode_toggled(self, check, key):
        if not check.get_active():
            return
        if self.busy:
            # One mode change at a time; put the tick back where the camera is.
            GLib.idle_add(lambda: self._select_mode(self.mode_key) and False)
            return
        self.set_busy(True, f"setting mode '{key}'…")

        def work():
            ok, out = run(["link2c", "mode", key])
            GLib.idle_add(self.mode_done, ok, out, key)

        threading.Thread(target=work, daemon=True).start()

    def mode_done(self, ok, out, key):
        self.set_busy(False)
        if ok:
            self.mode_key = key
            self._base_status = f"mode: {key}"
            self.status.set_label(self._base_status)
            self.update_framing_sensitivity()
        else:
            hint = ""
            if key == "track" and "0xff" in out:
                hint = ": the camera sees no one in frame"
            elif "0xff" in out:
                hint = ": no active stream"
            self._base_status = f"could not enable '{key}'{hint}"
            self.status.set_label(self._base_status)
            self.refresh_state()
        return False

    def on_framing_changed(self, row, _param):
        if self.busy:
            return
        key, label = FRAMINGS[row.get_selected()]

        def done(ok, _out):
            self._base_status = (f"framing: {label.lower()}" if ok
                                 else "could not change framing")
            self.status.set_label(self._base_status)
        run_bg(["link2c", "framing", key], done)

    def on_slider(self, scale, ctrl, label, fmt):
        value = int(scale.get_value())
        label.set_label((fmt or str)(value))
        self._pending[ctrl] = value
        if ctrl == "zoom_absolute":
            self.update_pan_hint(value)
        # Coalesce drags into one write every 120 ms.
        if not getattr(self, "_flush_queued", False):
            self._flush_queued = True
            GLib.timeout_add(120, self.flush_sliders)

    def flush_sliders(self):
        self._flush_queued = False
        pending, self._pending = self._pending, {}
        if not pending:
            return False
        args = [f"--set-ctrl={k}={v}" for k, v in pending.items()]
        run_bg(["v4l2-ctl", "-d", DEV] + args,
               lambda ok, _o: ok and save_preset(pending))
        return False

    def update_pan_hint(self, zoom):
        """Pan and tilt do nothing at 1x - the crop already fills the sensor."""
        usable = zoom > 100
        for row in (self.pan_row, self.tilt_row):
            if row is None:
                continue
            row.set_sensitive(usable)
            row.set_subtitle("" if usable else "needs zoom")

    def on_center(self, _btn):
        for ctrl, row in (("pan_absolute", self.pan_row),
                          ("tilt_absolute", self.tilt_row)):
            if row:
                row.scale.set_value(0)
        run_bg(["v4l2-ctl", "-d", DEV,
                "--set-ctrl=pan_absolute=0", "--set-ctrl=tilt_absolute=0"])

    def on_auto(self, row, _param, ctrl):
        value = 1 if row.get_active() else 0
        self.update_auto_sensitivity()
        run_bg(["v4l2-ctl", "-d", DEV, f"--set-ctrl={ctrl}={value}"],
               lambda ok, _o: ok and save_preset({ctrl: value}))

    def on_power_line(self, row, _param):
        value = POWER_LINE[row.get_selected()][0]
        run_bg(["v4l2-ctl", "-d", DEV, f"--set-ctrl=power_line_frequency={value}"],
               lambda ok, _o: ok and save_preset({"power_line_frequency": value}))

    def set_busy(self, busy, msg=None):
        self.busy = busy
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        if msg:
            self.status.set_label(msg)

    def refresh_state(self):
        """Read back what the camera actually reports and mirror it."""
        def work():
            ok, out = run(["link2c", "info"])
            GLib.idle_add(self.apply_info, ok, out)

        threading.Thread(target=work, daemon=True).start()

    def apply_info(self, ok, out):
        if not ok:
            return False
        m = re.search(r"^mode\s*:\s*(\w+)", out, re.MULTILINE)
        if m and m.group(1) in self.mode_rows:
            self.mode_key = m.group(1)
            self._select_mode(self.mode_key)
        m = re.search(r"^framing\s*:\s*(\w+)", out, re.MULTILINE)
        if m:
            for i, (key, _) in enumerate(FRAMINGS):
                if key == m.group(1):
                    self.framing_row.handler_block_by_func(self.on_framing_changed)
                    self.framing_row.set_selected(i)
                    self.framing_row.handler_unblock_by_func(self.on_framing_changed)
        m = re.search(r"^mirror\s*:\s*h=(\w+)\s+v=(\w+)", out, re.MULTILINE)
        self._syncing = True
        if m:
            self.mirror_rows["h"].set_active(m.group(1) == "on")
            self.mirror_rows["v"].set_active(m.group(2) == "on")
        m = re.search(r"^flags\s*:(.*)$", out, re.MULTILINE)
        for name, state in re.findall(r"([\w-]+)=(on|off)", m.group(1) if m else ""):
            if name in self.feature_rows:
                self.feature_rows[name].set_active(state == "on")
        m = re.search(r"^level\s*:\s*(on|off)", out, re.MULTILINE)
        if m:
            self.feature_rows["level"].set_active(m.group(1) == "on")
        m = re.search(r"^speed\s*:\s*(\w+)", out, re.MULTILINE)
        keys = [k for k, _ in SPEEDS]
        if m and m.group(1) in keys:
            self.speed_row.set_selected(keys.index(m.group(1)))
        self._syncing = False
        m = re.search(r"^serial\s*:\s*(\S+)", out, re.MULTILINE)
        if m:
            self.set_title(f"Insta360 Link 2C — {m.group(1)}")
        if self.zoom_row:
            self.update_pan_hint(int(self.zoom_row.scale.get_value()))
        self.update_framing_sensitivity()
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.srb404.Link2C")

    def do_activate(self):
        Window(self).present()


def main():
    for tool in ("link2c", "v4l2-ctl"):
        if not shutil.which(tool):
            print(f"{tool} not found in PATH")
            return 1
    if not os.path.exists(DEV):
        print(f"{DEV} not found")
        return 1
    return App().run(None)


if __name__ == "__main__":
    raise SystemExit(main())
