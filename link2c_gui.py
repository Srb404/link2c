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
MODES = [
    ("off", "Normal", "No processing"),
    ("track", "AI tracking", "Keeps the person in frame"),
    ("deskview", "DeskView", "Desk view with perspective correction"),
    ("overhead", "Overhead", "Document view"),
]

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
    """Live view, fed by a raw-RGB ffmpeg pipe running in a worker thread."""

    W, H = 640, 360

    def __init__(self, dev, on_state):
        super().__init__()
        self.dev = dev
        self.on_state = on_state
        self.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.proc = None
        self.running = False

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._pump, daemon=True).start()

    def stop(self):
        self._close()
        self.set_paintable(None)
        self.on_state(False, "preview off")

    def restart(self):
        """Bounce the pipe - mirror only takes on a fresh stream."""
        if self.running:
            self._close()
            self.start()

    def _close(self):
        self.running = False
        if self.proc:
            self.proc.kill()
            self.proc = None

    def _pump(self):
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", "1920x1080",
            "-i", self.dev,
            "-r", "15",
            "-vf", f"scale={self.W}:{self.H}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            GLib.idle_add(self.on_state, False, "ffmpeg not found")
            self.running = False
            return

        nbytes = self.W * self.H * 3
        first = True
        while self.running:
            buf = self.proc.stdout.read(nbytes)
            if len(buf) != nbytes:
                break
            if first:
                first = False
                GLib.idle_add(self.on_state, True, "preview running")
            GLib.idle_add(self._show, buf)

        if self.running:  # ended on its own - the device is busy or gone
            err = ""
            if self.proc and self.proc.stderr:
                err = self.proc.stderr.read().decode(errors="replace").strip()
            msg = "camera is busy in another app" if "usy" in err else \
                  (err.splitlines()[-1] if err else "preview stopped")
            GLib.idle_add(self.on_state, False, msg)
        self.running = False

    def _show(self, buf):
        self.set_paintable(Gdk.MemoryTexture.new(
            self.W, self.H, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(buf), self.W * 3))
        return False


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Insta360 Link 2C")
        self.set_default_size(1080, 660)
        self.v4l2 = V4L2(DEV)
        self.busy = False
        self.streaming = False
        self._base_status = "starting preview…"
        self._pending = {}
        self.auto_rows = {}
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
        text = self._base_status
        if not live:
            text += "  ·  modes need a stream (turn the preview on)"
        self.status.set_label(text)

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
        m = re.search(r"^mirror\s*:\s*h=(\w+)\s+v=(\w+)", out, re.MULTILINE)
        self._syncing = True
        if m:
            self.mirror_rows["h"].set_active(m.group(1) == "on")
            self.mirror_rows["v"].set_active(m.group(2) == "on")
        self._syncing = False
        m = re.search(r"^serial\s*:\s*(\S+)", out, re.MULTILINE)
        if m:
            self.set_title(f"Insta360 Link 2C — {m.group(1)}")
        if self.zoom_row:
            self.update_pan_hint(int(self.zoom_row.scale.get_value()))
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
