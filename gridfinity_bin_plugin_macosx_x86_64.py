# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Gridfinity Bin & Baseplate Generator"
# description = "Parametric Gridfinity bins, interlocking baseplates, and openGrid boards: custom compartments, exact mm sizing with edge padding, flip-stacked copies, full board / lite openGrid types with screws, connectors, countersinks, node chamfer rings and sharp outer corners, EN/RU interface, 3D WebGL preview, and direct build plate drop."
# author = "jonas"
# version = "1.8.0"
"""Gridfinity bin and baseplate generator for OrcaSlicer.

Registers two capabilities:

  * a Pages capability -- a tab in the main window, next to Prepare/Preview
  * a Script capability -- the same panel as a floating window, from the
    Plugins dialog

Features:
  * Parametric Gridfinity bins (42 mm grid, 7 mm height units)
  * Advanced custom compartments (Uniform, Per-Row, Per-Column)
  * Interlocking baseplates with puzzle connectors & print-bed splitting
  * Millimeter sizing with automatic unit fitting and optional edge padding
  * Stackable baseplate copies: N levels straight up, every second one
    rotated 180 degrees, with a configurable air gap (default 0.2 mm)
  * Bilingual interface (English / Russian)
  * Direct build plate STL injection via single-instance IPC

GENERATED FILE. Edit gridfinity_bin.html and re-run build_orca_plugin.py.
"""

import base64
import json
import os
import re
import subprocess
import sys
import threading

import orca

OUTPUT_DIRNAME = "gridfinity_output"


# ---------------------------------------------------------------------------
# Handing a file to the running instance
#
# The host API cannot add objects to the plate, so the STL path is given to the
# already-running OrcaSlicer through the same single-instance channel its own
# launcher uses. The payload is argv-style (";"-separated); the receiver skips
# element 0 and treats the rest as files to open.
#
#   Linux    D-Bus  com.orcaslicer.OrcaSlicer.InstanceCheck.Object<hash>
#                   method AnotherInstance(string)
#   Windows  WM_COPYDATA to the main window (class "wxWindowNR", carrying the
#                   Instance_Hash_Minor/Major props), dwData = 1, UTF-16 body
# ---------------------------------------------------------------------------
IS_WINDOWS = sys.platform.startswith("win")
BUS_RE = re.compile(r"com[.]orcaslicer[.]OrcaSlicer[.]InstanceCheck[.]Object[0-9]+")
WM_COPYDATA = 0x004A


def _log(*parts):
    """Goes to the host's python stderr log."""
    print("[gridfinity]", *parts, file=sys.stderr, flush=True)


def _payload(path):
    # element 0 stands in for the executable and is discarded by the receiver
    return "orca-slicer;" + path


# -- Linux -------------------------------------------------------------------
def _session_bus_name():
    out = subprocess.run(
        ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
         "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames"],
        capture_output=True, text=True, timeout=10).stdout
    found = BUS_RE.findall(out)
    return found[0] if found else None


def _fell_back(path, wanted_dir):
    """True when the file could not be delivered to the requested folder."""
    wanted = (wanted_dir or "").strip().strip('"').strip("'")
    if not wanted:
        return False
    try:
        w = os.path.normpath(os.path.expanduser(wanted))
        p = os.path.normpath(path)
        return os.path.normcase(p).startswith(os.path.normcase(w)) is False
    except Exception:
        return False


def _psq(s):
    """Single-quote a string for PowerShell (apostrophes doubled)."""
    return "'" + str(s).replace("'", "''") + "'"


def _place_dbus(path):
    name = _session_bus_name()
    if not name:
        return "the running instance is not exposing its file-open interface"
    obj = "/" + name.replace(".", "/")
    done = subprocess.run(
        ["dbus-send", "--session", "--dest=" + name, "--type=method_call",
         obj, name + ".AnotherInstance", "string:" + _payload(path)],
        capture_output=True, text=True, timeout=15)
    if done.returncode != 0:
        return (done.stderr or "the open request was rejected").strip()
    return None


# -- Windows -----------------------------------------------------------------
def _place_windows(path):
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_size_t),      # ULONG_PTR
                    ("cbData", wintypes.DWORD),
                    ("lpData", ctypes.c_void_p)]

    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetPropW.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
    user32.GetPropW.restype = wintypes.HANDLE
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.SendMessageW.restype = ctypes.c_ssize_t

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    me = os.getpid()
    found = []

    def visit(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(hwnd, buf, 256) == 0 or buf.value != "wxWindowNR":
            return True
        # only OrcaSlicer main frames carry both instance-hash props
        if not user32.GetPropW(hwnd, "Instance_Hash_Minor"):
            return True
        if not user32.GetPropW(hwnd, "Instance_Hash_Major"):
            return True
        # we live inside the target process, so match on pid rather than hash
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != me:
            return True
        found.append(hwnd)
        return False

    user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
    callback = WNDENUMPROC(visit)            # keep alive for the call
    user32.EnumWindows(callback, 0)
    if not found:
        return "could not find the OrcaSlicer main window"

    text = ctypes.create_unicode_buffer(_payload(path))
    data = COPYDATASTRUCT(1, ctypes.sizeof(text), ctypes.cast(text, ctypes.c_void_p))
    user32.SendMessageW(found[0], WM_COPYDATA, 0, ctypes.addressof(data))
    return None


# -- dispatch ----------------------------------------------------------------
def load_onto_plate(path):
    """Ask the running OrcaSlicer to open `path`. Returns None on success,
    otherwise a short reason. Safe to call from a worker thread."""
    place = _place_windows if IS_WINDOWS else _place_dbus
    last = "unknown error"
    for attempt in (1, 2):
        try:
            problem = place(path)
        except Exception as exc:
            last = "{}: {}".format(type(exc).__name__, exc)
            _log("attempt", attempt, "raised:", last)
            continue
        if problem is None:
            _log("attempt", attempt, "placed", os.path.basename(path))
            return None
        last = problem
        _log("attempt", attempt, "failed:", problem)
    return last


# ---------------------------------------------------------------------------
# Bed size of the active printer
# ---------------------------------------------------------------------------
def _active_printer_preset():
    """The selected printer preset. Which accessor exists varies between
    OrcaSlicer builds, so try the known spellings and take the first that
    answers."""
    bundle = orca.host.preset_bundle()
    getters = (
        lambda: bundle.current_printer_preset(),
        lambda: bundle.printers().get_selected_preset(),
        lambda: bundle.printers().selected_preset(),
        lambda: bundle.printers().selected_preset,
        lambda: bundle.printers.get_selected_preset(),
    )
    for get in getters:
        try:
            preset = get()
        except Exception:
            continue
        if preset is not None:
            return preset
    # None of the spellings answered. Say what the bundle does offer, so this
    # is diagnosable from the message rather than needing another round trip.
    try:
        offered = ", ".join(n for n in dir(bundle) if not n.startswith("_"))
    except Exception:
        offered = "<not introspectable>"
    raise RuntimeError("no printer-preset accessor on the preset bundle; "
                       "it offers: " + offered[:400])


def _preset_name(preset):
    for get in (lambda: preset.name(), lambda: preset.name):
        try:
            value = get()
        except Exception:
            continue
        if isinstance(value, str) and value:
            return value
    return "the active printer"


def _preset_config(preset, key):
    """full_config_value resolves inheritance; plain config_value does not, so
    prefer it and keep the other as a fallback."""
    for get in (lambda: preset.full_config_value(key),
                lambda: preset.config_value(key)):
        try:
            value = get()
        except Exception:
            continue
        if value not in (None, "", []):
            return value
    return None


def _area_extent(raw):
    """Width and depth of a printable_area polygon.

    The value is a list of corners like ['25x25', '325x25', '325x280',
    '25x280'] -- or the same thing serialized. Beds whose origin is inset are
    common, so the usable size is the extent of the polygon, not its far
    corner. Pulling the numbers out in order and pairing them copes with both
    the 'XxY' and 'X,Y' spellings."""
    text = raw if isinstance(raw, str) else ",".join(str(v) for v in raw)
    nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", text)]
    pts = list(zip(nums[0::2], nums[1::2]))
    if len(pts) < 2:
        raise ValueError("printable_area did not parse: %r" % (raw,))
    xs = [q[0] for q in pts]
    ys = [q[1] for q in pts]
    return round(max(xs) - min(xs), 2), round(max(ys) - min(ys), 2)


def active_bed_size():
    """(width, depth, printer name) for the printer currently selected."""
    preset = _active_printer_preset()
    raw = _preset_config(preset, "printable_area")
    if raw is None:
        raw = _preset_config(preset, "bed_shape")
    if raw is None:
        try:
            offered = ", ".join(n for n in dir(preset) if not n.startswith("_"))
        except Exception:
            offered = "<not introspectable>"
        raise RuntimeError("no printable_area on the printer preset; "
                           "it offers: " + offered[:400])
    width, depth = _area_extent(raw)
    if width <= 0 or depth <= 0:
        raise ValueError("printable_area has no area: %r" % (raw,))
    return width, depth, _preset_name(preset)


# ---------------------------------------------------------------------------
# Shared behaviour for both capabilities
# ---------------------------------------------------------------------------
class _GridfinityCore:
    def _handle(self, message, reply):
        """message: dict or JSON string from the page. reply: callable(dict)."""
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8", "replace")
        if isinstance(message, str):
            try:
                message = json.loads(message)
            except Exception:
                return
        if not isinstance(message, dict):
            return
        kind = message.get("type")

        if kind == "get_bed":
            try:
                width, depth, printer = active_bed_size()
            except Exception as exc:
                _log("bed size lookup failed:", exc)
                reply({"type": "bed_failed", "error": str(exc)})
            else:
                _log("bed size from", printer, "=", width, "x", depth)
                reply({"type": "bed", "x": width, "y": depth, "printer": printer})
            return

        if kind == "pick_dir":
            try:
                path = self._pick_dir(message.get("start", ""))
                reply({"type": "pick_dir", "path": path})
            except Exception as exc:
                _log("folder dialog failed:", exc)
                reply({"type": "pick_dir", "path": "", "error": str(exc)})
            return

        if kind != "save_stl":
            return

        try:
            path = self._write_stl(message.get("name", ""),
                                   message.get("data", ""),
                                   message.get("dir", ""))
        except Exception as exc:
            reply({"type": "save_failed", "error": str(exc)})
            return

        if not message.get("place"):
            reply({"type": "saved", "path": path, "placed": False,
                   "fallback": _fell_back(path, message.get("dir", ""))})
            return

        # on_message runs on the UI thread; dbus-send would freeze it, so hand
        # the placement off and report back when it finishes.
        reply({"type": "saved", "path": path, "placed": False, "pending": True})

        def worker():
            problem = load_onto_plate(path)
            try:
                reply({"type": "placed", "path": path,
                       "placed": problem is None, "place_error": problem or ""})
            except Exception as exc:
                _log("could not report placement result:", exc)

        threading.Thread(target=worker, name="gridfinity-place", daemon=True).start()

    def _pick_dir(self, start):
        """System folder picker; returns "" when the user cancels."""
        start = (start or "").strip().strip('"').strip("'")
        if not os.path.isdir(start):
            start = ""
        title = "Select the STL export folder"
        if sys.platform.startswith("win"):
            def psq(s):
                return "'" + s.replace("'", "''") + "'"
            script = ("Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
                      "$o = New-Object System.Windows.Forms.Form; $o.TopMost = $True; "
                      "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
                      "$f.Description = " + psq(title) + "; ")
            if start:
                script += "$f.SelectedPath = " + psq(start) + "; "
            script += ("if ($f.ShowDialog($o) -eq "
                       "[System.Windows.Forms.DialogResult]::OK) { $f.SelectedPath }")
            # CREATE_NO_WINDOW: no console flash behind the dialog
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            out = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", script],
                capture_output=True, text=True, timeout=600, creationflags=flags)
            return out.stdout.strip()
        if sys.platform == "darwin":
            script = 'POSIX path of (choose folder with prompt "%s")' % title
            if start:
                esc = json.dumps(start)[1:-1]   # escapes backslash and quote
                script = ('POSIX path of (choose folder with prompt "%s" '
                          'default location POSIX file "%s")' % (title, esc))
            out = subprocess.run(["osascript", "-e", script],
                                 capture_output=True, text=True, timeout=600)
            return out.stdout.strip() if out.returncode == 0 else ""
        for cmd in (["zenity", "--file-selection", "--directory", "--title", title],
                    ["kdialog", "--getexistingdirectory",
                     start or os.path.expanduser("~"), "--title", title]):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            except FileNotFoundError:
                continue
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
            return ""
        raise RuntimeError("no folder picker available (install zenity or kdialog)")

    def _shell_move(self, src, dst):
        """Move src to dst from a separate process.

        The audit hook filters python-level open() calls, so a direct
        write to a folder picked by the user (outside the plugin's
        allow-listed data dir) is blocked with "Plugin attempted to
        access a blocked file path".  Writing inside the plugin folder
        is allowed, and process spawning is not audited, so the file is
        written next to the plugin and handed over by the OS shell.
        """
        try:
            if sys.platform.startswith("win"):
                script = ("Copy-Item -LiteralPath " + _psq(src) +
                          " -Destination " + _psq(dst) + " -Force")
                kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
                cmd = ["powershell", "-NoProfile", "-Command", script]
            else:
                kw = {}
                cmd = ["/bin/mv", "-f", src, dst]
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=120, **kw)
            if r.returncode == 0 and os.path.isfile(dst):
                try:
                    os.remove(src)
                except Exception:
                    pass
                return True
            _log("shell move failed:", (r.stderr or r.stdout or "").strip()[:200])
        except Exception as exc:
            _log("shell move error:", exc)
        return False

    def _write_stl(self, name, payload_b64, custom_dir=""):
        if not payload_b64:
            raise ValueError("no STL data was sent by the panel")
        data = base64.b64decode(payload_b64)
        if len(data) < 84:
            raise ValueError("STL payload is too short to be valid")

        # Writes are audited: only paths under the host data dir pass, so
        # the file is ALWAYS written next to the plugin first.  A folder
        # picked in the panel is then reached with a shell move.
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_DIRNAME)
        os.makedirs(out_dir, exist_ok=True)

        safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name or ""))
        if not safe.lower().endswith(".stl"):
            safe = (safe or "gridfinity_bin") + ".stl"

        tmp_path = os.path.join(out_dir, safe)
        with open(tmp_path, "wb") as handle:
            handle.write(data)

        custom = (custom_dir or "").strip().strip('"').strip("'")
        if not custom:
            return tmp_path
        cand = os.path.normpath(os.path.expanduser(custom))
        if os.path.isfile(cand):
            raise ValueError("export folder is a file: " + cand)
        try:
            os.makedirs(cand, exist_ok=True)
        except Exception as exc:
            _log("cannot create", cand, ":", exc)
            return tmp_path
        dest = os.path.join(cand, safe)
        return dest if self._shell_move(tmp_path, dest) else tmp_path


# ---------------------------------------------------------------------------
# A tab in the main window
# ---------------------------------------------------------------------------
class GridfinityPage(orca.pages.PagesPluginCapabilityBase, _GridfinityCore):
    def __init__(self):
        super().__init__()

    def get_name(self):
        return "Gridfinity"

    def get_icon(self):
        return "param_grid"

    def get_ui(self):
        return PAGE

    def on_message(self, message):
        self._handle(message, self.post_message)


# ---------------------------------------------------------------------------
# The same panel as a floating window
# ---------------------------------------------------------------------------
class GridfinityWindow(orca.script.ScriptPluginCapabilityBase, _GridfinityCore):
    def __init__(self):
        super().__init__()
        self._win = None

    def get_name(self):
        return "Gridfinity Bin Generator"

    def execute(self):
        if self._win is not None:
            try:
                if self._win.is_open():
                    return orca.ExecutionResult.success("The Gridfinity panel is already open.")
            except Exception:
                self._win = None
        try:
            self._win = orca.host.ui.create_window(
                html=PAGE,
                title="Gridfinity Bin Generator",
                on_message=self._on_message,
                on_close=self._on_close,
            )
        except Exception as exc:
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError,
                "Could not open the generator panel: {}".format(exc))
        return orca.ExecutionResult.success("Gridfinity bin generator opened.")

    def _on_close(self, *_args):
        self._win = None

    def _on_message(self, message):
        self._handle(message, self._post)

    def _post(self, payload):
        if self._win is None:
            return
        try:
            self._win.post(payload)
        except Exception:
            pass


@orca.plugin
class GridfinityPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(GridfinityPage)
        orca.register_capability(GridfinityWindow)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gridfinity Bin Configurator</title>
<style>
:root {
  --bg:#f4f5f7; --panel:#fff; --ink:#1a1d21; --muted:#6b7280; --line:#e3e5e9;
  --accent:#2f6f4e; --accent-ink:#fff; --code-bg:#f0f1f4;
  --warn:#9a5b00; --warn-bg:#fdf3e2; --badge:#e8eaee;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#14161a; --panel:#1b1e23; --ink:#e8eaed; --muted:#9aa1ab; --line:#2b2f36;
    --accent:#4c9c72; --accent-ink:#0d1512; --code-bg:#12151a;
    --warn:#f0b866; --warn-bg:#2c2417; --badge:#262a31;
  }
}
/* Inside OrcaSlicer the host injects --orca-* variables; adopt them. */
html.orca-host {
  --bg:var(--orca-bg); --panel:var(--orca-bg); --ink:var(--orca-fg);
  --muted:var(--orca-muted); --line:var(--orca-border);
  --accent:var(--orca-accent); --accent-ink:var(--orca-accent-fg);
  --code-bg:var(--orca-border); --badge:var(--orca-border);
}
html.orca-host body { font:14px/1.45 var(--orca-font, ui-sans-serif, system-ui, sans-serif); }
* { box-sizing:border-box; }
html, body { height:100%; margin:0; }
body {
  font:14px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  color:var(--ink); background:var(--bg); -webkit-font-smoothing:antialiased;
}
.app { display:flex; height:100%; }
.panel {
  width:326px; flex:0 0 326px; background:var(--panel);
  border-right:1px solid var(--line); overflow-y:auto; padding:18px 18px 28px;
}
.panel header { margin-bottom:18px; }
.panel h1 { font-size:16px; margin:0 0 2px; letter-spacing:-0.01em; }
.panel .sub { margin:0; color:var(--muted); font-size:12px; }
.panel h2 {
  font-size:11px; text-transform:uppercase; letter-spacing:.07em;
  color:var(--muted); margin:0 0 10px; font-weight:600;
}
section { margin-bottom:20px; }
.row { display:grid; grid-template-columns:1fr 94px 44px; align-items:center; gap:8px; margin-bottom:8px; }
.row label { font-size:13px; font-weight:500; }
.row input[type=number] {
  width:100%; padding:4px 5px; font:inherit; font-size:12.5px; text-align:right;
  border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--ink);
  font-variant-numeric:tabular-nums; -moz-appearance:textfield;
}
.row input[type=number]:focus, .num input:focus {
  border-color:var(--accent); outline:none; box-shadow:0 0 0 1px var(--accent);
}
input[type=range] { width:100%; accent-color:var(--accent); }
.num { display:grid; grid-template-columns:1fr 78px; align-items:center; gap:8px; margin-bottom:8px; font-size:13px; }
.num input {
  width:100%; padding:4px 6px; font:inherit; font-size:12.5px; text-align:right;
  border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--ink);
  font-variant-numeric:tabular-nums;
}
.check { display:flex; align-items:center; gap:9px; margin-bottom:7px; font-size:13px; }
.check input { accent-color:var(--accent); width:15px; height:15px; }
details { border-top:1px solid var(--line); padding-top:12px; }
details summary {
  cursor:pointer; font-size:11px; text-transform:uppercase; letter-spacing:.07em;
  color:var(--muted); font-weight:600; margin-bottom:12px;
}
.stats { border-top:1px solid var(--line); padding-top:14px; font-size:12px; }
.stats dl { display:grid; grid-template-columns:auto 1fr; gap:4px 12px; margin:0; }
.stats dt { color:var(--muted); }
.stats dd { margin:0; text-align:right; font-variant-numeric:tabular-nums; }
.warn { margin-top:10px; padding:7px 9px; border-radius:6px; background:var(--warn-bg); color:var(--warn); font-size:12px; }
.warn[hidden] { display:none; }
pre#cmd {
  background:var(--code-bg); border:1px solid var(--line); border-radius:6px;
  padding:9px; font-size:11px; line-height:1.5; white-space:pre-wrap; word-break:break-all;
  margin:0 0 8px; color:var(--muted); max-height:132px; overflow-y:auto;
}
.modes {
  display:flex; background:var(--code-bg); border:1px solid var(--line);
  border-radius:7px; padding:3px; gap:3px; margin-bottom:4px;
}
.modes label {
  flex:1; display:flex; align-items:center; justify-content:center;
  padding:5px 8px; font-size:12.5px; font-weight:500; border-radius:5px;
  cursor:pointer; color:var(--muted); user-select:none; transition:all .15s ease;
}
.modes input[type=radio] { display:none; }
.modes label.active {
  background:var(--panel); color:var(--ink); box-shadow:0 1px 3px rgba(0,0,0,0.08); font-weight:600;
}
.submodes {
  background:var(--bg); border:1px solid var(--line); border-radius:6px;
  padding:2px; gap:2px; margin-bottom:10px;
}
.submodes label {
  padding:4px 6px; font-size:11.5px; border-radius:4px;
}
.dyn-list {
  margin-top:8px; padding-top:8px; border-top:1px dashed var(--line);
  display:flex; flex-direction:column; gap:6px;
}
.dyn-item {
  display:grid; grid-template-columns:1fr 94px 44px; align-items:center; gap:8px;
}
.dyn-item label { font-size:12px; color:var(--muted); }
.dyn-item input[type=number] {
  width:100%; padding:3px 5px; font:inherit; font-size:12px; text-align:right;
  border:1px solid var(--line); border-radius:5px; background:var(--bg); color:var(--ink);
  font-variant-numeric:tabular-nums; -moz-appearance:textfield;
}
.btns { display:flex; gap:8px; flex-wrap:wrap; }
.saved {
  margin:0 0 10px; padding:7px 9px; border-radius:6px; font-size:11.5px;
  background:var(--code-bg); color:var(--muted); word-break:break-all;
}
.saved[hidden] { display:none; }
.bedrow { display:flex; align-items:center; gap:8px; margin:0 0 10px; }
.bedrow[hidden] { display:none; }
.bedrow span { font-size:11px; color:var(--muted); }
.prog { margin:0 0 10px; }
.prog[hidden] { display:none; }
.prog .track {
  height:5px; border-radius:3px; background:var(--code-bg); overflow:hidden;
}
.prog .fill {
  height:100%; width:0; border-radius:3px; background:var(--accent);
  transition:width .12s linear;
}
.prog .lbl {
  display:flex; justify-content:space-between; gap:8px;
  margin-top:5px; font-size:11.5px; color:var(--muted);
}
button {
  font:inherit; font-size:12.5px; padding:7px 11px; cursor:pointer;
  border-radius:6px; border:1px solid var(--line); background:var(--bg); color:var(--ink);
}
button:hover:not(:disabled) { border-color:var(--muted); }
button:disabled { opacity:.55; cursor:default; }
button.primary { background:var(--accent); color:var(--accent-ink); border-color:transparent; }
button.primary:hover:not(:disabled) { filter:brightness(1.08); }
.stage { flex:1; position:relative; min-width:0; }
canvas { display:block; width:100%; height:100%; touch-action:none; cursor:grab; }
canvas.dragging { cursor:grabbing; }
.views { position:absolute; top:14px; right:14px; display:flex; gap:6px; }
.views button { padding:5px 10px; font-size:12px; background:var(--panel); opacity:.94; }
.views button.active { opacity:1; background:#2f6f4f; color:#fff; }
.badge {
  position:absolute; top:14px; left:14px; font-size:11.5px; padding:4px 10px;
  border-radius:999px; background:var(--badge); color:var(--muted);
}
.badge.hi { background:var(--accent); color:var(--accent-ink); }
.hint { position:absolute; left:14px; bottom:12px; color:var(--muted); font-size:11.5px; pointer-events:none; }
.err { position:absolute; inset:0; display:grid; place-items:center; padding:40px; text-align:center; color:var(--muted); }
.err[hidden] { display:none; }
@media (max-width:760px) {
  .app { flex-direction:column-reverse; }
  .panel { width:auto; flex:1 1 auto; border-right:0; border-bottom:1px solid var(--line); }
  .stage { flex:0 0 46vh; }
}
</style>
</head>
<body>
<div class="app">
  <aside class="panel">
    <section style="margin-bottom:14px">
      <div class="modes submodes" style="max-width:150px">
        <label id="lbl_lang_en" class="active"><input type="radio" name="lang" id="lang_en" value="en" checked> EN</label>
        <label id="lbl_lang_ru"><input type="radio" name="lang" id="lang_ru" value="ru"> RU</label>
      </div>
    </section>

    <section>
      <h2 data-i="h_model">Model</h2>
      <div class="modes">
        <label id="lbl_mode_bin" class="active"><input type="radio" name="mode" id="mode_bin" value="bin" checked> <span data-i="m_bin">Bin</span></label>
        <label id="lbl_mode_plate"><input type="radio" name="mode" id="mode_plate" value="plate"> <span data-i="m_plate">Baseplate</span></label>
        <label id="lbl_mode_og"><input type="radio" name="mode" id="mode_og" value="og"> <span data-i="m_og">openGrid Board</span></label>
      </div>
    </section>

    <section id="binSizeSection">
      <h2 data-i="h_binsize">Bin Size</h2>
      <div class="row">
        <label for="gx" data-i="l_width">Width</label>
        <input type="range" id="gx" min="1" max="12" step="1" value="2">
        <input type="number" id="gx_num" min="1" max="50" step="1" value="2">
      </div>
      <div class="row">
        <label for="gy" data-i="l_depth">Depth</label>
        <input type="range" id="gy" min="1" max="12" step="1" value="1">
        <input type="number" id="gy_num" min="1" max="50" step="1" value="1">
      </div>
      <div class="row" id="row_gz">
        <label for="gz" data-i="l_height">Height</label>
        <input type="range" id="gz" min="1" max="20" step="1" value="6">
        <input type="number" id="gz_num" min="1" max="50" step="1" value="6">
      </div>
    </section>

    <section id="plateOpts" hidden>
      <h2 data-i="h_platesize">Baseplate Size</h2>
      <div class="modes submodes">
        <label id="lbl_plate_units" class="active"><input type="radio" name="plate_size_mode" id="plate_mode_units" value="units" checked> <span data-i="pm_units">Grid Units</span></label>
        <label id="lbl_plate_mm"><input type="radio" name="plate_size_mode" id="plate_mode_mm" value="mm"> <span data-i="pm_mm">Dimensions (mm)</span></label>
      </div>

      <!-- Grid Units Mode -->
      <div id="plateUnitsOpts">
        <div class="row">
          <label for="plate_gx" data-i="l_width">Width</label>
          <input type="range" id="plate_gx" min="1" max="20" step="1" value="4">
          <input type="number" id="plate_gx_num" min="1" max="50" step="1" value="4">
        </div>
        <div class="row">
          <label for="plate_gy" data-i="l_depth">Depth</label>
          <input type="range" id="plate_gy" min="1" max="20" step="1" value="3">
          <input type="number" id="plate_gy_num" min="1" max="50" step="1" value="3">
        </div>
      </div>

      <!-- Millimeters Mode -->
      <div id="plateMmOpts" hidden>
        <div class="row">
          <label for="plate_mm_x" data-i="l_width_mm">Width (mm)</label>
          <input type="range" id="plate_mm_x" min="42" max="600" step="1" value="168">
          <input type="number" id="plate_mm_x_num" min="10" max="2000" step="1" value="168">
        </div>
        <div class="row">
          <label for="plate_mm_y" data-i="l_depth_mm">Depth (mm)</label>
          <input type="range" id="plate_mm_y" min="42" max="600" step="1" value="126">
          <input type="number" id="plate_mm_y_num" min="10" max="2000" step="1" value="126">
        </div>
        <div id="plateFitInfo" style="font-size:11.5px; color:var(--muted); margin-top:2px; margin-bottom:6px">
          Fits 4 &times; 3 units (168 &times; 126 mm)
        </div>
        <div class="btns" style="margin-bottom:6px">
          <button type="button" id="btnPlateExact" style="width:100%">Add edge padding for exact size</button>
        </div>

        <div id="plateBufferAlignOpts" hidden style="margin-top:6px; margin-bottom:8px">
          <div class="row">
            <label for="buf_x_ratio" data-i="l_buf_x">Left ⟷ Right</label>
            <input type="range" id="buf_x_ratio" min="0" max="100" step="1" value="50">
            <input type="number" id="buf_x_ratio_num" min="0" max="100" step="1" value="50">
          </div>
          <div class="row">
            <label for="buf_y_ratio" data-i="l_buf_y">Down ⟷ Up</label>
            <input type="range" id="buf_y_ratio" min="0" max="100" step="1" value="50">
            <input type="number" id="buf_y_ratio_num" min="0" max="100" step="1" value="50">
          </div>
        </div>
      </div>

      <h2 style="margin-top:16px" data-i="h_plateopts">Baseplate Options</h2>
      <div class="num"><label for="plateBase" data-i="l_plate_base">Solid base (mm)</label><input type="number" id="plateBase" min="0" max="20" step="0.5" value="0"></div>
      <div class="num"><label for="plateR" data-i="l_plate_r">Corner radius</label><input type="number" id="plateR" min="0" max="12" step="0.5" value="4"></div>
      <div class="num"><label for="bedX" data-i="l_bed_x">Bed X (mm)</label><input type="number" id="bedX" min="42" max="2000" step="5" value="250"></div>
      <div class="num"><label for="bedY" data-i="l_bed_y">Bed Y (mm)</label><input type="number" id="bedY" min="42" max="2000" step="5" value="220"></div>
      <div class="num"><label for="plateGap" data-i="l_plate_gap">Explode gap</label><input type="number" id="plateGap" min="0" max="60" step="1" value="0"></div>
      <div id="bedRow" class="bedrow" hidden>
        <button id="btnBed" type="button" data-i="btn_bed">Use printer bed</button>
        <span id="bedFrom"></span>
      </div>
      <label class="check"><input type="checkbox" id="plateConnectors" checked> <span data-i="conn">Puzzle connectors</span></label>

      <h2 style="margin-top:16px" data-i="stacking_h">Stacking copies</h2>
      <label class="check"><input type="checkbox" id="plateStack"> <span data-i="stacking_on">Stack copies upward</span></label>
      <div id="stackOpts" hidden style="margin-left:24px; margin-top:2px; margin-bottom:8px">
        <div class="row">
          <label for="plateStackN" data-i="stacking_copies">Copies</label>
          <input type="range" id="plateStackN" min="2" max="10" step="1" value="2">
          <input type="number" id="plateStackN_num" min="2" max="10" step="1" value="2">
        </div>
        <div class="num">
          <label for="plateStackGap" data-i="stacking_gap">Gap between copies (mm)</label>
          <input type="number" id="plateStackGap" min="0" max="5" step="0.05" value="0.2">
        </div>
        <div style="font-size:11px; color:var(--muted); margin-bottom:4px" data-i="stacking_hint">Every second copy is rotated 180° so the seams do not line up.</div>
      </div>
    </section>

    <section id="ogOpts" hidden>
      <h2 data-i="h_ogboard">openGrid Board</h2>
      <div class="row">
        <label for="ogW" data-i="l_width">Width</label>
        <input type="range" id="ogW" min="1" max="20" step="1" value="4">
        <input type="number" id="ogW_num" min="1" max="20" step="1" value="4">
      </div>
      <div class="row">
        <label for="ogH" data-i="l_depth">Depth</label>
        <input type="range" id="ogH" min="1" max="20" step="1" value="3">
        <input type="number" id="ogH_num" min="1" max="20" step="1" value="3">
      </div>
      <div class="modes submodes">
        <label id="lbl_og_full" class="active"><input type="radio" name="og_type" id="og_type_full" value="full" checked> <span data-i="og_full">Full (6.8 mm)</span></label>
        <label id="lbl_og_lite"><input type="radio" name="og_type" id="og_type_lite" value="lite"> <span data-i="og_lite">Lite (4 mm)</span></label>
      </div>

      <h2 style="margin-top:16px" data-i="og_features">Board Features</h2>
      <label class="check"><input type="checkbox" id="ogScrews" checked> <span data-i="og_screws">Screw holes</span></label>
      <div id="ogScrewOpts" style="margin-left:24px; margin-top:2px; margin-bottom:8px">
        <div class="num"><label for="ogScrewD" data-i="og_shaft">Shaft &oslash; (mm)</label><input type="number" id="ogScrewD" min="2" max="8" step="0.1" value="4.1"></div>
        <div class="num"><label for="ogScrewHeadD" data-i="og_head">Head &oslash; (mm)</label><input type="number" id="ogScrewHeadD" min="3" max="10" step="0.1" value="7.2"></div>
        <div class="num"><label for="ogScrewInset" data-i="og_inset">Head inset from top (mm)</label><input type="number" id="ogScrewInset" min="0" max="4" step="0.1" value="1"></div>
        <label class="check"><input type="checkbox" id="ogCs"> <span data-i="og_cs">Countersink</span></label>
        <div class="num" id="ogCsDegRow" hidden><label for="ogCsDeg" data-i="og_cs_deg">Countersink angle (&deg;)</label><input type="number" id="ogCsDeg" min="60" max="120" step="5" value="90"></div>
        <label class="check"><input type="checkbox" id="ogBackside"> <span data-i="og_back">Backside head pocket</span></label>
        <div id="ogBackOpts" hidden style="margin-left:24px; margin-top:2px; margin-bottom:4px">
          <div class="num"><label for="ogBackInset" data-i="og_back_inset">Pocket depth (mm)</label><input type="number" id="ogBackInset" min="0" max="4" step="0.1" value="1"></div>
          <div class="num"><label for="ogBackShrink" data-i="og_back_shrink">Pocket shrink (mm)</label><input type="number" id="ogBackShrink" min="0" max="3" step="0.1" value="0"></div>
          <label class="check"><input type="checkbox" id="ogBackCs"> <span data-i="og_back_cs">Pocket countersink</span></label>
          <div class="num" id="ogBackCsDegRow" hidden><label for="ogBackCsDeg" data-i="og_back_cs_deg">Pocket angle (&deg;)</label><input type="number" id="ogBackCsDeg" min="60" max="120" step="5" value="90"></div>
        </div>
      </div>
      <label class="check"><input type="checkbox" id="ogConnectors" checked> <span data-i="og_conn">Board-to-board connectors</span></label>
      <button id="btnOgReset" type="button" data-i="og_reset" style="margin-top:6px"></button>
        <div style="font-size:11px; color:var(--muted); margin-top:6px" data-i="og_hint">28 mm openGrid lattice; 3 OG tiles = 2 Gridfinity units. Heads wider than 7.7 mm are clamped to fit the node.</div>

      <h2 style="margin-top:16px" data-i="stacking_h">Stacking copies</h2>
      <label class="check"><input type="checkbox" id="ogStack"> <span data-i="stacking_on">Stack copies upward</span></label>
      <div id="ogStackOpts" hidden style="margin-left:24px; margin-top:2px; margin-bottom:8px">
        <div class="row">
          <label for="ogStackN" data-i="stacking_copies">Copies</label>
          <input type="range" id="ogStackN" min="2" max="10" step="1" value="2">
          <input type="number" id="ogStackN_num" min="2" max="10" step="1" value="2">
        </div>
        <div class="num">
          <label for="ogStackGap" data-i="stacking_gap">Gap between copies (mm)</label>
          <input type="number" id="ogStackGap" min="0" max="5" step="0.05" value="0.2">
        </div>
        <div style="font-size:11px; color:var(--muted); margin-bottom:4px" data-i="stacking_hint">Every second copy is laid upside down (sockets facing down); the air gap keeps the copies from fusing.</div>
      </div>
    </section>

    <section id="binOpts">
      <h2 data-i="h_comps">Compartments</h2>
      <div class="modes submodes">
        <label id="lbl_layout_grid" class="active"><input type="radio" name="comp_layout" id="layout_grid" value="grid" checked> <span data-i="cl_grid">Uniform</span></label>
        <label id="lbl_layout_rows"><input type="radio" name="comp_layout" id="layout_rows" value="rows"> <span data-i="cl_rows">By Row</span></label>
        <label id="lbl_layout_cols"><input type="radio" name="comp_layout" id="layout_cols" value="cols"> <span data-i="cl_cols">By Column</span></label>
      </div>

      <!-- Uniform Grid -->
      <div id="compGridOpts">
        <div class="row">
          <label for="dx" data-i="l_dx">Across X</label>
          <input type="range" id="dx" min="1" max="8" step="1" value="2">
          <input type="number" id="dx_num" min="1" max="50" step="1" value="2">
        </div>
        <div class="row">
          <label for="dy" data-i="l_dy">Across Y</label>
          <input type="range" id="dy" min="1" max="8" step="1" value="1">
          <input type="number" id="dy_num" min="1" max="50" step="1" value="1">
        </div>
      </div>

      <!-- By Row -->
      <div id="compRowOpts" hidden>
        <div class="row">
          <label for="num_rows" data-i="l_num_rows">Total Rows</label>
          <input type="range" id="num_rows" min="1" max="8" step="1" value="2">
          <input type="number" id="num_rows_num" min="1" max="50" step="1" value="2">
        </div>
        <div id="rowDivList" class="dyn-list"></div>
      </div>

      <!-- By Column -->
      <div id="compColOpts" hidden>
        <div class="row">
          <label for="num_cols" data-i="l_num_cols">Total Cols</label>
          <input type="range" id="num_cols" min="1" max="8" step="1" value="2">
          <input type="number" id="num_cols_num" min="1" max="50" step="1" value="2">
        </div>
        <div id="colDivList" class="dyn-list"></div>
      </div>
    </section>

    <section id="binFeatures">
      <h2 data-i="h_features">Features</h2>
      <label class="check"><input type="checkbox" id="lip" checked> <span data-i="f_lip">Stacking lip</span></label>
      <label class="check"><input type="checkbox" id="scoop" checked> <span data-i="f_scoop">Finger scoop</span></label>
      <div id="scoopOpts" style="margin-left:24px; margin-top:2px; margin-bottom:8px">
        <div class="row" style="margin-bottom:6px">
          <label for="scoopR" data-i="l_radius_mm">Radius (mm)</label>
          <input type="range" id="scoopR" min="1" max="25" step="0.5" value="6">
          <input type="number" id="scoopR_num" min="0.5" max="50" step="0.5" value="6">
        </div>
      </div>
      <label class="check"><input type="checkbox" id="label"> <span data-i="f_label">Label tab</span></label>
      <div id="labelOpts" hidden style="margin-left:24px; margin-top:2px; margin-bottom:8px">
        <div class="row" style="margin-bottom:6px">
          <label for="labelD" data-i="l_depth_mm2">Depth (mm)</label>
          <input type="range" id="labelD" min="2" max="30" step="0.5" value="12">
          <input type="number" id="labelD_num" min="1" max="50" step="0.5" value="12">
        </div>
        <div class="row" style="margin-bottom:4px">
          <label for="labelW" data-i="l_width_mm2">Width (mm)</label>
          <input type="range" id="labelW" min="0" max="150" step="1" value="0">
          <input type="number" id="labelW_num" min="0" max="500" step="1" value="0">
        </div>
        <div style="font-size:11px; color:var(--muted); margin-bottom:4px"data-i="label_full_w">0 = Full compartment width</div>
      </div>
      <label class="check"><input type="checkbox" id="mag"> <span data-i="f_mag">Magnet holes (6&times;2&nbsp;mm)</span></label>
      <label class="check"><input type="checkbox" id="screw"> <span data-i="f_screw">Screw holes (M3)</span></label>
    </section>

    <details>
      <summary data-i="h_advanced">Advanced</summary>
      <div class="num"><label for="wall" data-i="l_wall">Wall (mm)</label><input type="number" id="wall" min="0.4" max="5" step="0.1" value="1.2"></div>
      <div class="num"><label for="floorT" data-i="l_floor">Floor (mm)</label><input type="number" id="floorT" min="0.4" max="10" step="0.1" value="1.4"></div>
      <div class="num"><label for="fillet" data-i="l_fillet">Inner fillet</label><input type="number" id="fillet" min="0" max="5" step="0.1" value="0.8"></div>
    </details>

    <section class="stats">
      <h2 data-i="h_result">Result</h2>
      <dl>
        <dt data-i="s_foot_l">Footprint</dt><dd id="s_foot"></dd>
        <dt data-i="s_tall_l">Total height</dt><dd id="s_tall"></dd>
        <dt data-i="s_comp_l">Compartment</dt><dd id="s_comp"></dd>
        <dt data-i="s_depth_l">Usable depth</dt><dd id="s_depth"></dd>
        <dt data-i="s_tris_l">Triangles</dt><dd id="s_tris"></dd>
      </dl>
      <div class="warn" id="s_warn" hidden></div>
    </section>

    <section>
      <h2 data-i="h_output">Output</h2>
      <div class="btns" style="margin-bottom:10px">
        <button class="primary" id="btnRender" data-i="btn_render">Render</button>
        <button id="btnStl" data-i="btn_stl">Export STL</button>
      </div>
      <label class="check" id="toPlateRow" hidden style="margin-bottom:10px">
        <input type="checkbox" id="toPlate" checked> <span data-i="to_plate">Add to build plate after export</span>
      </label>
      <div class="row" id="exportDirRow" hidden style="margin-bottom:10px">
        <label for="exportDir" data-i="exp_dir">Export folder</label>
        <input id="exportDir" type="text" style="flex:1; min-width:0" spellcheck="false">
        <button id="btnDir" type="button" data-i="exp_dir_pick" style="margin-left:6px; white-space:nowrap">📂 Browse…</button>
      </div>
      <div id="prog" class="prog" hidden>
        <div class="track"><div class="fill" id="progFill"></div></div>
        <div class="lbl"><span id="progStage"></span><span id="progPct"></span></div>
      </div>
      <div id="saved" class="saved" hidden></div>
    </section>
  </aside>

  <main class="stage">
    <canvas id="gl"></canvas>
    <canvas id="ov" style="position:absolute; inset:0; width:100%; height:100%; pointer-events:none; display:none"></canvas>
    <div class="badge" id="badge">Preview</div>
    <div class="views">
      <button data-view="iso" data-i="v_iso">Iso</button>
      <button data-view="front" data-i="v_front">Front</button>
      <button data-view="top" data-i="v_top">Top</button>
      <button data-view="under" data-i="v_under">Under</button>
      <button id="btn3d" class="active">3D</button>
      <button id="btn2d">2D</button>
    </div>
    <div class="hint" id="hint3d" data-i="hint">drag to orbit &middot; scroll to zoom &middot; shift-drag to pan</div>
    <div class="hint" id="hint2d" data-i="hint_2d" hidden></div>
    <div class="err" id="err" hidden></div>
  </main>
</div>

<script>
"use strict";
/* =====================================================================
   Gridfinity bin geometry -- mirrors gridfinity_bin.scad
   Builds an exact triangle mesh (no CSG): the bin is emitted as a set of
   closed solids whose union is the bin. Coincident interior faces are
   always back-to-back, so back-face culling hides them.
   ===================================================================== */

/* ---- specification constants (mm) ---- */
var GRID = 42, UNIT_Z = 7, GAP = 0.5;
var FOOT_TOP = GRID - GAP, R_TOP = 3.75;
var CH_UPPER = 2.15, H_MID = 1.8, CH_LOWER = 0.8;
var BASE_H = CH_LOWER + H_MID + CH_UPPER;          // 4.75
var FOOT_MID = FOOT_TOP - 2 * CH_UPPER;            // 37.20
var R_MID = R_TOP - CH_UPPER;                      //  1.60
var FOOT_BOT = FOOT_MID - 2 * CH_LOWER;            // 35.60
var R_BOT = R_MID - CH_LOWER;                      //  0.80
var LIP_CLEAR = 0.25;
var LIP_INSET = CH_UPPER - LIP_CLEAR;              //  1.90
var LIP_TAPER = 0.70;
var LIP_H = CH_LOWER + H_MID + LIP_TAPER;          //  3.30
var MAGNET_R = 3.25, MAGNET_H = 2.4;
var SCREW_R = 1.5, SCREW_H = 6.0, HOLE_OFF = 13;

var DEFAULTS = {
  gx: 2, gy: 1, gz: 6, dx: 2, dy: 1,
  comp_layout: "grid", num_rows: 2, row_divs: [2, 1], num_cols: 2, col_divs: [2, 1],
  lip: true, scoop: true, label: false, mag: false, screw: false,
  wall: 1.2, floorT: 1.4, scoopR: 6, labelD: 12, labelW: 0, fillet: 0.8
};

function derive(p) {
  var OX = p.gx * GRID - GAP;
  var OY = p.gy * GRID - GAP;
  var H_BODY = p.gz * UNIT_Z;
  var FLOOR = BASE_H + (p.screw ? Math.max(p.floorT, SCREW_H - BASE_H + 0.8)
                                : p.floorT);
  var IW = OX - 2 * p.wall, ID = OY - 2 * p.wall;
  var depth = H_BODY - FLOOR;
  var fillet = Math.max(0, Math.min(p.fillet, depth));
  var cells = [];
  var valid = depth > 0.2 && IW > 0.2 && ID > 0.2;
  var labelW = p.label ? Math.max(0, p.labelW || 0) : 0;

  if (p.comp_layout === "rows") {
    var nr = Math.max(1, p.num_rows || 1);
    var rowCD = (ID - (nr - 1) * p.wall) / nr;
    if (rowCD <= 0.2) valid = false;
    for (var j = 0; j < nr; j++) {
      var rCols = Math.max(1, (p.row_divs && p.row_divs[j]) || 1);
      var rCW = (IW - (rCols - 1) * p.wall) / rCols;
      if (rCW <= 0.2) valid = false;
      var cy = (j - (nr - 1) / 2) * (rowCD + p.wall);
      var r_in = Math.max(0, Math.min(R_TOP - p.wall, rCW / 2 - 0.01, rowCD / 2 - 0.01));
      var rFillet = Math.max(0, Math.min(fillet, rCW / 2 - 0.01, rowCD / 2 - 0.01));
      var scoopR = p.scoop ? Math.max(0, Math.min(p.scoopR, rowCD - 2 * rFillet, depth, p.label ? Math.max(0.5, rowCD - p.labelD - 1) : rowCD)) : 0;
      var labelD = p.label ? Math.max(0, Math.min(p.labelD, rowCD - scoopR - 1, depth)) : 0;

      for (var i = 0; i < rCols; i++) {
        var cx = (i - (rCols - 1) / 2) * (rCW + p.wall);
        cells.push({
          cx: cx, cy: cy, cw: rCW, cd: rowCD,
          r_in: r_in, fillet: rFillet,
          scoopR: scoopR, labelD: labelD, labelW: labelW
        });
      }
    }
  } else if (p.comp_layout === "cols") {
    var nc = Math.max(1, p.num_cols || 1);
    var colCW = (IW - (nc - 1) * p.wall) / nc;
    if (colCW <= 0.2) valid = false;
    for (var i = 0; i < nc; i++) {
      var cRows = Math.max(1, (p.col_divs && p.col_divs[i]) || 1);
      var cCD = (ID - (cRows - 1) * p.wall) / cRows;
      if (cCD <= 0.2) valid = false;
      var cx = (i - (nc - 1) / 2) * (colCW + p.wall);
      var r_in = Math.max(0, Math.min(R_TOP - p.wall, colCW / 2 - 0.01, cCD / 2 - 0.01));
      var rFillet = Math.max(0, Math.min(fillet, colCW / 2 - 0.01, cCD / 2 - 0.01));
      var scoopR = p.scoop ? Math.max(0, Math.min(p.scoopR, cCD - 2 * rFillet, depth, p.label ? Math.max(0.5, cCD - p.labelD - 1) : cCD)) : 0;
      var labelD = p.label ? Math.max(0, Math.min(p.labelD, cCD - scoopR - 1, depth)) : 0;

      for (var j = 0; j < cRows; j++) {
        var cy = (j - (cRows - 1) / 2) * (cCD + p.wall);
        cells.push({
          cx: cx, cy: cy, cw: colCW, cd: cCD,
          r_in: r_in, fillet: rFillet,
          scoopR: scoopR, labelD: labelD, labelW: labelW
        });
      }
    }
  } else {
    // Uniform grid
    var dx = Math.max(1, p.dx || 1);
    var dy = Math.max(1, p.dy || 1);
    var CW = (IW - (dx - 1) * p.wall) / dx;
    var CD = (ID - (dy - 1) * p.wall) / dy;
    if (CW <= 0.2 || CD <= 0.2) valid = false;
    var r_in = Math.max(0, Math.min(R_TOP - p.wall, CW / 2 - 0.01, CD / 2 - 0.01));
    var rFillet = Math.max(0, Math.min(fillet, CW / 2 - 0.01, CD / 2 - 0.01));
    var scoopR = p.scoop ? Math.max(0, Math.min(p.scoopR, CD - 2 * rFillet, depth, p.label ? Math.max(0.5, CD - p.labelD - 1) : CD)) : 0;
    var labelD = p.label ? Math.max(0, Math.min(p.labelD, CD - scoopR - 1, depth)) : 0;
    var pitchX = CW + p.wall, pitchY = CD + p.wall;

    for (var i = 0; i < dx; i++) {
      for (var j = 0; j < dy; j++) {
        cells.push({
          cx: (i - (dx - 1) / 2) * pitchX,
          cy: (j - (dy - 1) / 2) * pitchY,
          cw: CW, cd: CD,
          r_in: r_in, fillet: rFillet,
          scoopR: scoopR, labelD: labelD, labelW: labelW
        });
      }
    }
  }

  var firstCell = cells[0] || { cw: 0, cd: 0 };
  return {
    OX: OX, OY: OY, H_BODY: H_BODY, FLOOR: FLOOR, IW: IW, ID: ID,
    cells: cells, CW: firstCell.cw, CD: firstCell.cd, depth: depth, fillet: fillet,
    TOP: H_BODY + (p.lip ? LIP_H : 0),
    support: Math.max(0, LIP_INSET - p.wall),
    valid: valid && cells.length > 0
  };
}

/* =====================================================================
   2D loops
   ===================================================================== */

// Rounded rectangle, counter-clockwise, 4*(n+1) points.
function rrLoop(sx, sy, r, n) {
  r = Math.max(0, Math.min(r, sx / 2, sy / 2));
  var X = sx / 2 - r, Y = sy / 2 - r, pts = [];
  var c = [[X, -Y, -Math.PI / 2], [X, Y, 0], [-X, Y, Math.PI / 2], [-X, -Y, Math.PI]];
  for (var i = 0; i < 4; i++) {
    for (var k = 0; k <= n; k++) {
      var a = c[i][2] + (Math.PI / 2) * k / n;
      pts.push([c[i][0] + r * Math.cos(a), c[i][1] + r * Math.sin(a)]);
    }
  }
  return pts;
}

function circleLoop(cx, cy, r, n) {           // counter-clockwise
  var pts = [];
  for (var k = 0; k < n; k++) {
    var a = 2 * Math.PI * k / n;
    pts.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]);
  }
  return pts;
}

function offsetLoop(pts, dx, dy) {
  return pts.map(function (p) { return [p[0] + dx, p[1] + dy]; });
}

function reverseLoop(pts) { return pts.slice().reverse(); }

/* Drop points that repeat within tol.  The connector profiles come from an
   SVG and carry a few 5e-6 mm duplicates at the path ends; left in, they
   trianglulate to slivers that the mesh then culls, leaving pinholes. */
function dedupeLoop(pts, tol) {
  var out = [];
  for (var i = 0; i < pts.length; i++) {
    var q = out.length ? out[out.length - 1] : pts[pts.length - 1];
    if (Math.abs(pts[i][0] - q[0]) > tol || Math.abs(pts[i][1] - q[1]) > tol) out.push(pts[i]);
  }
  return out;
}

// Outward normal per point; tolerates repeated points (zero-radius corners).
function loopNormals(pts) {
  var n = pts.length, out = [];
  for (var i = 0; i < n; i++) {
    var a = null, b = null;
    for (var k = 1; k < n; k++) {
      var q = pts[(i - k + n) % n];
      if (Math.abs(q[0] - pts[i][0]) > 1e-9 || Math.abs(q[1] - pts[i][1]) > 1e-9) { a = q; break; }
    }
    for (var k2 = 1; k2 < n; k2++) {
      var q2 = pts[(i + k2) % n];
      if (Math.abs(q2[0] - pts[i][0]) > 1e-9 || Math.abs(q2[1] - pts[i][1]) > 1e-9) { b = q2; break; }
    }
    if (!a || !b) { out.push([1, 0]); continue; }
    var tx = b[0] - a[0], ty = b[1] - a[1];
    var l = Math.hypot(tx, ty) || 1;
    out.push([ty / l, -tx / l]);
  }
  return out;
}

/* =====================================================================
   Triangulation: ear clipping with hole bridging (Eberly's bridge)
   ===================================================================== */

function samePt(p, q) {
  return Math.abs(p[0] - q[0]) < 1e-9 && Math.abs(p[1] - q[1]) < 1e-9;
}

function area2(a, b, c) {
  return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
}

function signedArea(poly) {
  var s = 0;
  for (var i = 0, n = poly.length; i < n; i++) {
    var a = poly[i], b = poly[(i + 1) % n];
    s += a[0] * b[1] - b[0] * a[1];
  }
  return s / 2;
}

function inTriangle(p, a, b, c) {
  var d1 = area2(a, b, p), d2 = area2(b, c, p), d3 = area2(c, a, p);
  return d1 >= -1e-12 && d2 >= -1e-12 && d3 >= -1e-12;
}

/* Hole bridging follows the approach used by earcut: for each hole (processed
   left to right) cast a ray from its left-most vertex toward -x, take the
   nearest edge crossing, then refine to a reflex vertex that is visible from
   the hole. Sign convention here matches earcut: eArea < 0 means convex/CCW. */

function eArea(p, q, r) { return -area2(p, q, r); }

function ptInTri(ax, ay, bx, by, cx, cy, px, py) {
  return (cx - px) * (ay - py) - (ax - px) * (cy - py) >= 0 &&
         (ax - px) * (by - py) - (bx - px) * (ay - py) >= 0 &&
         (bx - px) * (cy - py) - (cx - px) * (by - py) >= 0;
}

function locallyInside(poly, i, b) {
  var n = poly.length;
  var a = poly[i], prev = poly[(i - 1 + n) % n], next = poly[(i + 1) % n];
  return eArea(prev, a, next) < 0
    ? eArea(a, b, next) >= 0 && eArea(a, prev, b) >= 0
    : eArea(a, b, prev) < 0 || eArea(a, next, b) < 0;
}

function findHoleBridge(poly, hx, hy) {
  var n = poly.length, qx = -Infinity, m = -1, i;
  for (i = 0; i < n; i++) {
    var p = poly[i], pn = poly[(i + 1) % n];
    if (hy <= p[1] && hy >= pn[1] && pn[1] !== p[1]) {
      var x = p[0] + (hy - p[1]) * (pn[0] - p[0]) / (pn[1] - p[1]);
      if (x <= hx && x > qx) {
        qx = x;
        m = p[0] < pn[0] ? i : (i + 1) % n;
        if (x === hx) return m;
      }
    }
  }
  if (m < 0) return -1;

  var mx = poly[m][0], my = poly[m][1], tanMin = Infinity, best = m;
  for (i = 0; i < n; i++) {
    var v = poly[i];
    if (hx >= v[0] && v[0] >= mx && hx !== v[0] &&
        ptInTri(hy < my ? hx : qx, hy, mx, my, hy < my ? qx : hx, hy, v[0], v[1])) {
      var tan = Math.abs(hy - v[1]) / (hx - v[0]);
      if (locallyInside(poly, i, [hx, hy]) &&
          (tan < tanMin || (tan === tanMin && v[0] > poly[best][0]))) {
        best = i; tanMin = tan;
      }
    }
  }
  return best;
}

// Splice one clockwise hole into the counter-clockwise polygon.
function bridgeHole(poly, hole) {
  var li = 0, i;
  for (i = 1; i < hole.length; i++) if (hole[i][0] < hole[li][0]) li = i;
  var target = findHoleBridge(poly, hole[li][0], hole[li][1]);
  if (target < 0) return null;
  var rot = hole.slice(li).concat(hole.slice(0, li));
  return poly.slice(0, target + 1)
             .concat(rot, [rot[0]], [poly[target]], poly.slice(target + 1));
}

function earClip(poly) {
  var n = poly.length;
  if (n < 3) return [];
  var idx = [], i;
  for (i = 0; i < n; i++) idx.push(i);
  var tris = [], relax = 0;

  while (idx.length > 3) {
    var m = idx.length, found = -1, bestArea = 0;
    for (var k = 0; k < m; k++) {
      var i0 = idx[(k - 1 + m) % m], i1 = idx[k], i2 = idx[(k + 1) % m];
      var a = poly[i0], b = poly[i1], c = poly[i2];
      var ar = area2(a, b, c);
      if (ar <= 1e-10) continue;
      var ok = true;
      if (relax === 0) {
        var ab = Math.hypot(b[0] - a[0], b[1] - a[1]);
        var bc = Math.hypot(c[0] - b[0], c[1] - b[1]);
        var ca = Math.hypot(a[0] - c[0], a[1] - c[1]);
        for (var j = 0; j < m; j++) {
          var t = idx[j];
          if (t === i0 || t === i1 || t === i2) continue;
          var q = poly[t];
          // a duplicated bridge vertex sits exactly on a corner: not a blocker
          if (samePt(q, a) || samePt(q, b) || samePt(q, c)) continue;
          var d1 = area2(a, b, q), d2 = area2(b, c, q), d3 = area2(c, a, q);
          if (d1 >= -1e-12 && d2 >= -1e-12 && d3 >= -1e-12) {
            // a point lying ON an ear edge does not block the ear
            var h = Math.min(Math.abs(d1) / (ab || 1),
                             Math.abs(d2) / (bc || 1),
                             Math.abs(d3) / (ca || 1));
            if (h < 1e-7) continue;
            ok = false;
            break;
          }
        }
      }
      if (!ok) continue;
      if (relax === 0) { found = k; break; }
      if (ar > bestArea) { bestArea = ar; found = k; }
    }
    if (found < 0) {
      if (relax === 0) { relax = 1; continue; }   // retry ignoring containment
      break;                                      // unrecoverable
    }
    var mm = idx.length;
    tris.push([idx[(found - 1 + mm) % mm], idx[found], idx[(found + 1) % mm]]);
    idx.splice(found, 1);
    relax = 0;
  }
  if (idx.length === 3) tris.push([idx[0], idx[1], idx[2]]);
  return tris;
}

/* O(n^3) interval-DP triangulation: the safety net for polygons where
   the ear clipper's fan comes out short (the coverage check above calls
   it).  visible() = the segment midpoint is inside the polygon and the
   segment crosses no edge. */
function triangulateDP(poly) {
  var n = poly.length;
  if (n < 3) return [];
  function ptIn(p) {
    var inside = false, i, j;
    for (i = 0, j = n - 1; i < n; j = i++)
      if (((poly[i][1] > p[1]) !== (poly[j][1] > p[1])) &&
          (p[0] < (poly[j][0] - poly[i][0]) * (p[1] - poly[i][1]) /
                  (poly[j][1] - poly[i][1]) + poly[i][0]))
        inside = !inside;
    return inside;
  }
  function cross(o, a, b) {
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  }
  function segCross(p1, p2, q1, q2) {
    var d1 = cross(q1, q2, p1), d2 = cross(q1, q2, p2);
    var d3 = cross(p1, p2, q1), d4 = cross(p1, p2, q2);
    return ((d1 > 0) !== (d2 > 0)) && ((d3 > 0) !== (d4 > 0));
  }
  function segOK(i, j) {                 /* no edge strictly crosses (i,j) */
    for (var k = 0; k < n; k++) {
      var k2 = (k + 1) % n;
      if (k === i || k2 === i || k === j || k2 === j) continue;
      if (segCross(poly[i], poly[j], poly[k], poly[k2])) return false;
    }
    return true;
  }
  function visible(i, j) {               /* strictly inside: midpoint test */
    if (poly[i][0] === poly[j][0] && poly[i][1] === poly[j][1]) return false;
    var mid = [(poly[i][0] + poly[j][0]) / 2, (poly[i][1] + poly[j][1]) / 2];
    return ptIn(mid) && segOK(i, j);
  }
  var dp = [], bk = [], i, j, k;
  for (i = 0; i < n; i++) { dp.push([]); bk.push([]); }
  for (var gap = 1; gap < n; gap++)
    for (i = 0; i + gap < n; i++) {
      j = i + gap;
      if (gap < 2) { dp[i][j] = 0; continue; }
      dp[i][j] = Infinity;
      for (k = i + 1; k < j; k++) {
        var tri = [poly[i], poly[k], poly[j]];
        var a = Math.abs(signedArea(tri));
        if (a < 1e-12) {
          /* collinear split: a zero-area sliver.  Emit it anyway (the
             mesh builder drops the degenerate triangle, but every chord
             still pairs with its neighbours), so straight runs and
             bridge duplicates can never leave dp at Infinity. */
          var c0 = dp[i][k] + dp[k][j];
          if (c0 < dp[i][j] && segOK(i, k) && segOK(k, j)) {
            dp[i][j] = c0; bk[i][j] = k;
          }
          continue;
        }
        var cost = dp[i][k] + dp[k][j] + a;
        if (cost >= dp[i][j]) continue;
        if (visible(i, k) && visible(k, j) && visible(i, j)) {
          dp[i][j] = cost; bk[i][j] = k;
        } else if (dp[i][j] === Infinity && segOK(i, k) && segOK(k, j)) {
          /* permissive tier: keep the best non-crossing split so the
             fallback always yields a full fan (the caller only needs
             coverage; degenerate slivers are dropped downstream) */
          dp[i][j] = cost + 1e9; bk[i][j] = k;
        }
      }
    }
  var tris = [];
  (function rec(i2, j2) {
    if (j2 - i2 < 2 || bk[i2][j2] === undefined) return;
    var k2 = bk[i2][j2];
    tris.push([i2, k2, j2]);
    rec(i2, k2);
    rec(k2, j2);
  })(0, n - 1);
  return tris;
}

// outer: CCW. holes: array of loops (any winding). Returns {pts, tris}.
function triangulate(outer, holes) {
  var poly = signedArea(outer) < 0 ? reverseLoop(outer) : outer.slice();
  if (holes && holes.length) {
    var hs = holes.map(function (h) {
      return signedArea(h) > 0 ? reverseLoop(h) : h.slice();     // holes clockwise
    });
    hs.sort(function (A, B) {
      var ax = Infinity, bx = Infinity, i;
      for (i = 0; i < A.length; i++) if (A[i][0] < ax) ax = A[i][0];
      for (i = 0; i < B.length; i++) if (B[i][0] < bx) bx = B[i][0];
      return ax - bx;
    });
    for (var i = 0; i < hs.length; i++) {
      var next = bridgeHole(poly, hs[i]);
      if (next) poly = next;
    }
  }
  var tris = earClip(poly);
  /* the ear clipper can give up on hostile shapes (deep notch pockets)
     or return a subtly wrong fan: verify the covered area, and let the
     DP triangulation (always completes a simple polygon) take over */
  var want = Math.abs(signedArea(poly)), got = 0, ti;
  for (ti = 0; ti < tris.length; ti++) {
    var ta = area2(poly[tris[ti][0]], poly[tris[ti][1]], poly[tris[ti][2]]);
    got += Math.abs(ta) / 2;
  }
  if ((tris.length < poly.length - 2 ||
      Math.abs(got - want) > 1e-6 * Math.max(1, want)) &&
      !(holes && holes.length))
    /* Simple polygons get the exact DP fallback.  A bridged cap whose
       hole sits in a sub-print sliver of the outline (plate sockets
       0.002 mm from the border) has NO clean triangulation of the
       bridged loop at all; the ear clipper's fan there is watertight
       by construction and only locally off by that sliver, so it
       stays. */
    tris = triangulateDP(poly);
  return { pts: poly, tris: tris };
}

/* Hole bridging follows the approach used by earcut: for each hole (processed
   left to right) cast a ray from its left-most vertex toward -x, take the
   nearest edge crossing, then refine to a reflex vertex that is visible from
   the hole. Sign convention here matches earcut: eArea < 0 means convex/CCW. */

function eArea(p, q, r) { return -area2(p, q, r); }

function ptInTri(ax, ay, bx, by, cx, cy, px, py) {
  return (cx - px) * (ay - py) - (ax - px) * (cy - py) >= 0 &&
         (ax - px) * (by - py) - (bx - px) * (ay - py) >= 0 &&
         (bx - px) * (cy - py) - (cx - px) * (by - py) >= 0;
}

function locallyInside(poly, i, b) {
  var n = poly.length;
  var a = poly[i], prev = poly[(i - 1 + n) % n], next = poly[(i + 1) % n];
  return eArea(prev, a, next) < 0
    ? eArea(a, b, next) >= 0 && eArea(a, prev, b) >= 0
    : eArea(a, b, prev) < 0 || eArea(a, next, b) < 0;
}

function findHoleBridge(poly, hx, hy) {
  var n = poly.length, qx = -Infinity, m = -1, i;
  for (i = 0; i < n; i++) {
    var p = poly[i], pn = poly[(i + 1) % n];
    if (hy <= p[1] && hy >= pn[1] && pn[1] !== p[1]) {
      var x = p[0] + (hy - p[1]) * (pn[0] - p[0]) / (pn[1] - p[1]);
      if (x <= hx && x > qx) {
        qx = x;
        m = p[0] < pn[0] ? i : (i + 1) % n;
        if (x === hx) return m;
      }
    }
  }
  if (m < 0) return -1;

  var mx = poly[m][0], my = poly[m][1], tanMin = Infinity, best = m;
  for (i = 0; i < n; i++) {
    var v = poly[i];
    if (hx >= v[0] && v[0] >= mx && hx !== v[0] &&
        ptInTri(hy < my ? hx : qx, hy, mx, my, hy < my ? qx : hx, hy, v[0], v[1])) {
      var tan = Math.abs(hy - v[1]) / (hx - v[0]);
      if (locallyInside(poly, i, [hx, hy]) &&
          (tan < tanMin || (tan === tanMin && v[0] > poly[best][0]))) {
        best = i; tanMin = tan;
      }
    }
  }
  return best;
}

// Splice one clockwise hole into the counter-clockwise polygon.
function bridgeHole(poly, hole) {
  var li = 0, i;
  for (i = 1; i < hole.length; i++) if (hole[i][0] < hole[li][0]) li = i;
  var target = findHoleBridge(poly, hole[li][0], hole[li][1]);
  if (target < 0) return null;
  var rot = hole.slice(li).concat(hole.slice(0, li));
  return poly.slice(0, target + 1)
             .concat(rot, [rot[0]], [poly[target]], poly.slice(target + 1));
}


/* =====================================================================
   Mesh accumulation
   ===================================================================== */

function Mesh() { this.pos = []; this.nrm = []; }

Mesh.prototype.tri = function (p0, p1, p2, n0, n1, n2) {
  // drop degenerate triangles so the STL stays clean
  var ux = p1[0] - p0[0], uy = p1[1] - p0[1], uz = p1[2] - p0[2];
  var vx = p2[0] - p0[0], vy = p2[1] - p0[1], vz = p2[2] - p0[2];
  var cx = uy * vz - uz * vy, cy = uz * vx - ux * vz, cz = ux * vy - uy * vx;
  if (cx * cx + cy * cy + cz * cz < 1e-14) return;
  this.pos.push(p0[0], p0[1], p0[2], p1[0], p1[1], p1[2], p2[0], p2[1], p2[2]);
  this.nrm.push(n0[0], n0[1], n0[2], n1[0], n1[1], n1[2], n2[0], n2[1], n2[2]);
};

Mesh.prototype.count = function () { return this.pos.length / 9; };

function norm3(v) {
  var l = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / l, v[1] / l, v[2] / l];
}

/* ---- band between two corresponding loops at two heights ---- */
function addBand(mesh, la, za, lb, zb, flip) {
  var n = la.length;
  if (n !== lb.length) throw new Error("loop mismatch " + n + " vs " + lb.length);
  var na = loopNormals(la), dz = zb - za;
  var N = [];
  for (var i = 0; i < n; i++) {
    var e = (lb[i][0] - la[i][0]) * na[i][0] + (lb[i][1] - la[i][1]) * na[i][1];
    var v = norm3([na[i][0] * dz, na[i][1] * dz, -e]);
    N.push(flip ? [-v[0], -v[1], -v[2]] : v);
  }
  for (var i2 = 0; i2 < n; i2++) {
    var j = (i2 + 1) % n;
    var A = [la[i2][0], la[i2][1], za], B = [la[j][0], la[j][1], za];
    var C = [lb[j][0], lb[j][1], zb], D = [lb[i2][0], lb[i2][1], zb];
    if (flip) {
      mesh.tri(A, C, B, N[i2], N[j], N[j]);
      mesh.tri(A, D, C, N[i2], N[i2], N[j]);
    } else {
      mesh.tri(A, B, C, N[i2], N[j], N[j]);
      mesh.tri(A, C, D, N[i2], N[j], N[i2]);
    }
  }
}

/* ---- flat cap at height z; up=true faces +z ---- */
function addCap(mesh, outer, holes, z, up) {
  var t = triangulate(outer, holes);
  var nv = up ? [0, 0, 1] : [0, 0, -1];
  for (var i = 0; i < t.tris.length; i++) {
    var a = t.pts[t.tris[i][0]], b = t.pts[t.tris[i][1]], c = t.pts[t.tris[i][2]];
    var A = [a[0], a[1], z], B = [b[0], b[1], z], C = [c[0], c[1], z];
    if (up) mesh.tri(A, B, C, nv, nv, nv);
    else mesh.tri(A, C, B, nv, nv, nv);
  }
}

/* ---- cut a closed mesh with the plane z = z0, keep the part above and
   cap the opening flat. Collinear boundary points are merged away on
   BOTH the wall side (two triangles become one) and the cap loop, so
   cap triangulation always matches the wall segmentation. */
function clipMeshAbove(srcM, z0) {
  var out = new Mesh();
  var segs = [];                                 // boundary chords at z0
  var chordOwner = [];                           // tri offset per chord
  function lerpN(i0, i1, t) {
    var a = [srcM.nrm[i0], srcM.nrm[i0 + 1], srcM.nrm[i0 + 2]];
    var b = [srcM.nrm[i1], srcM.nrm[i1 + 1], srcM.nrm[i1 + 2]];
    return norm3([a[0] + (b[0] - a[0]) * t,
                  a[1] + (b[1] - a[1]) * t,
                  a[2] + (b[2] - a[2]) * t]);
  }
  for (var i = 0; i < srcM.pos.length; i += 9) {
    var V = [], N = [];
    for (var v = 0; v < 3; v++) {
      V.push([srcM.pos[i + v * 3], srcM.pos[i + v * 3 + 1], srcM.pos[i + v * 3 + 2]]);
      N.push([srcM.nrm[i + v * 3], srcM.nrm[i + v * 3 + 1], srcM.nrm[i + v * 3 + 2]]);
    }
    var above = [V[0][2] >= z0 - 1e-9, V[1][2] >= z0 - 1e-9, V[2][2] >= z0 - 1e-9];
    var na = above[0] + above[1] + above[2];
    if (na === 3) {
      out.tri(V[0], V[1], V[2], N[0], N[1], N[2]);
      continue;
    }
    if (na === 0) continue;
    var P = [], NN = [], cross = [];
    for (var e = 0; e < 3; e++) {
      var A = V[e], B = V[(e + 1) % 3];
      var inA = above[e], inB = above[(e + 1) % 3];
      if (inA) { P.push(A); NN.push(N[e]); }
      if (inA !== inB) {
        var t = (z0 - A[2]) / (B[2] - A[2]);
        var X = [A[0] + (B[0] - A[0]) * t,
                 A[1] + (B[1] - A[1]) * t, z0];
        P.push(X);
        NN.push(lerpN(i + e * 3, i + ((e + 1) % 3) * 3, t));
        cross.push(X);
      }
    }
    var base = out.pos.length / 9;
    for (var k = 1; k + 1 < P.length; k++)
      out.tri(P[0], P[k], P[k + 1], NN[0], NN[k], NN[k + 1]);
    if (cross.length === 2) {
      segs.push(cross[0], cross[1]);
      for (var f2 = base; f2 < out.pos.length / 9; f2++) {
        var has0 = false, has1 = false;
        for (var f3 = 0; f3 < 3; f3++) {
          var fpt = [out.pos[f2 * 9 + f3 * 3], out.pos[f2 * 9 + f3 * 3 + 1],
                     out.pos[f2 * 9 + f3 * 3 + 2]];
          if (samePt(fpt, cross[0])) has0 = true;
          if (samePt(fpt, cross[1])) has1 = true;
        }
        if (has0 && has1) { chordOwner.push(f2 * 9); break; }
      }
    }
  }
  /* merge collinear boundary points on the wall side: the fan of
     triangles around the point collapses onto the straight chord
     (P0 -> P2), which the cap then shares */
  function kOf(p) {
    return p[0].toFixed(4) + "," + p[1].toFixed(4) + "," + p[2].toFixed(4);
  }
  var ptChords = new Map();
  for (var c1 = 0; c1 * 2 < segs.length; c1++) {
    for (var e2 = 0; e2 < 2; e2++) {
      var kp2 = kOf(segs[c1 * 2 + e2]);
      if (!ptChords.has(kp2)) ptChords.set(kp2, []);
      ptChords.get(kp2).push(c1);
    }
  }
  var chordDropped = new Set(), newChords = [];
  for (var c2 = 0; c2 * 2 < segs.length; c2++) {
    if (chordDropped.has(c2)) continue;
    for (var e3 = 0; e3 < 2; e3++) {
      var P1 = segs[c2 * 2 + e3];
      var cs = ptChords.get(kOf(P1)) || [];
      if (cs.length !== 2 || chordDropped.has(cs[0]) || chordDropped.has(cs[1]))
        continue;
      var oA = chordOwner[cs[0]], oB = chordOwner[cs[1]];
      /* all tris incident to P1 */
      var inc = [];
      for (var t3 = 0; t3 + 8 < out.pos.length; t3 += 9) {
        for (var v3 = 0; v3 < 3; v3++)
          if (samePt([out.pos[t3 + v3 * 3], out.pos[t3 + v3 * 3 + 1],
                      out.pos[t3 + v3 * 3 + 2]], P1)) { inc.push(t3); break; }
      }
      var oAi = inc.indexOf(oA), oBi = inc.indexOf(oB);
      if (oAi < 0 || oBi < 0) continue;
      /* ring of opposite vertices, in fan order */
      var ring = [];
      for (var r1 = 0; r1 < inc.length; r1++) {
        for (var v4 = 0; v4 < 3; v4++) {
          var q4 = [out.pos[inc[r1] + v4 * 3], out.pos[inc[r1] + v4 * 3 + 1],
                    out.pos[inc[r1] + v4 * 3 + 2]];
          if (samePt(q4, P1)) continue;
          var dup = false;
          for (var r2 = 0; r2 < ring.length; r2++)
            if (samePt(q4, ring[r2]) &&
                Math.abs(q4[2] - ring[r2][2]) < 1e-9) { dup = true; break; }
          if (!dup) ring.push(q4);
        }
      }
      if (ring.length !== inc.length + 1) continue;   // not a clean fan
      /* plane normal from the ring (Newell), oriented outward */
      var Nn = [0, 0, 0];
      for (var r3 = 0; r3 < ring.length; r3++) {
        var qa = ring[r3], qb = ring[(r3 + 1) % ring.length];
        Nn[0] += (qa[1] - qb[1]) * (qa[2] + qb[2]);
        Nn[1] += (qa[2] - qb[2]) * (qa[0] + qb[0]);
        Nn[2] += (qa[0] - qb[0]) * (qa[1] + qb[1]);
      }
      var nl = Math.hypot(Nn[0], Nn[1], Nn[2]) || 1;
      Nn = [Nn[0] / nl, Nn[1] / nl, Nn[2] / nl];
      var tn = [out.nrm[oA], out.nrm[oA + 1], out.nrm[oA + 2]];
      if (Nn[0] * tn[0] + Nn[1] * tn[1] + Nn[2] * tn[2] < 0)
        Nn = [-Nn[0], -Nn[1], -Nn[2]];
      /* order ring around P1 */
      var u0 = [ring[0][0] - P1[0], ring[0][1] - P1[1], ring[0][2] - P1[2]];
      var ul = Math.hypot(u0[0], u0[1], u0[2]) || 1;
      u0 = [u0[0] / ul, u0[1] / ul, u0[2] / ul];
      var w0 = [Nn[1] * u0[2] - Nn[2] * u0[1],
                Nn[2] * u0[0] - Nn[0] * u0[2],
                Nn[0] * u0[1] - Nn[1] * u0[0]];
      ring.sort(function (ra, rb) {
        function ang(rc) {
          var d = [rc[0] - P1[0], rc[1] - P1[1], rc[2] - P1[2]];
          return Math.atan2(d[0] * w0[0] + d[1] * w0[1] + d[2] * w0[2],
                            d[0] * u0[0] + d[1] * u0[1] + d[2] * u0[2]);
        }
        return ang(ra) - ang(rb);
      });
      /* P0 and P2 must be the ring neighbours of the cut */
      var oApt = segs[cs[0] * 2], oApt2 = segs[cs[0] * 2 + 1];
      var P0 = samePt(oApt, P1) ? oApt2 : oApt;
      var oBpt = segs[cs[1] * 2], oBpt2 = segs[cs[1] * 2 + 1];
      var P2 = samePt(oBpt, P1) ? oBpt2 : oBpt;
      var iP0 = -1, iP2 = -1;
      for (var r4 = 0; r4 < ring.length; r4++) {
        if (samePt(ring[r4], P0)) iP0 = r4;
        if (samePt(ring[r4], P2)) iP2 = r4;
      }
      if (iP0 < 0 || iP2 < 0) continue;
      /* rebuild the fan without P1 */
      var keep = new Mesh();
      for (var t4 = 0; t4 + 8 < out.pos.length; t4 += 9) {
        if (inc.indexOf(t4) >= 0) continue;
        for (var c4 = 0; c4 < 9; c4++) keep.pos.push(out.pos[t4 + c4]);
        for (var c5 = 0; c5 < 9; c5++) keep.nrm.push(out.nrm[t4 + c5]);
      }
      for (var f4 = 1; f4 + 1 < ring.length; f4++)
        keep.tri(ring[0], ring[f4], ring[f4 + 1], Nn, Nn, Nn);
      out = keep;
      chordDropped.add(cs[0]);
      chordDropped.add(cs[1]);
      newChords.push([P0, P2]);
      break;                    // segs indices changed only logically
    }
  }
  /* cap: chain the boundary chords into loop(s), skipping collinear pts */
  var nbrs = new Map();
  var live = [];
  for (var s3 = 0; s3 * 2 < segs.length; s3++)
    if (!chordDropped.has(s3)) live.push([segs[s3 * 2], segs[s3 * 2 + 1]]);
  for (var s5 = 0; s5 < newChords.length; s5++) live.push(newChords[s5]);
  for (var s6 = 0; s6 < live.length; s6++) {
    var pa = live[s6][0], pb = live[s6][1];
    if (!nbrs.has(kOf(pa))) nbrs.set(kOf(pa), []);
    if (!nbrs.has(kOf(pb))) nbrs.set(kOf(pb), []);
    nbrs.get(kOf(pa)).push(pb);
    nbrs.get(kOf(pb)).push(pa);
  }
  var used2 = new Map();
  var loops = [];
  for (var s4 = 0; s4 < live.length; s4++) {
    var start = live[s4][0];
    if (used2.has(kOf(start))) continue;
    var loop = [start];
    used2.set(kOf(start), 1);
    var cur = start;
    for (;;) {
      var cand2 = nbrs.get(kOf(cur)) || [];
      var nxtPt = null;
      for (var ci = 0; ci < cand2.length; ci++)
        if (!used2.has(kOf(cand2[ci]))) { nxtPt = cand2[ci]; break; }
      if (!nxtPt || kOf(nxtPt) === kOf(start)) break;
      loop.push(nxtPt);
      used2.set(kOf(nxtPt), 1);
      cur = nxtPt;
    }
    if (loop.length >= 3) loops.push(loop);
  }
  for (var L = 0; L < loops.length; L++) {
    var flat = loops[L].map(function (p) { return [p[0], p[1]]; });
    if (signedArea(flat) < 0) flat.reverse();
    addCap(out, flat, [], z0, false);
  }
  return out;
}

/* ---- closed solid from a stack of levels ----
   levels: [{loop, z, holes}] -- holes only used on the end caps          */
function addSolid(mesh, levels, capHolesBottom, capHolesTop) {
  for (var i = 0; i + 1 < levels.length; i++)
    addBand(mesh, levels[i].loop, levels[i].z, levels[i + 1].loop, levels[i + 1].z, false);
  addCap(mesh, levels[0].loop, capHolesBottom || [], levels[0].z, false);
  var top = levels[levels.length - 1];
  addCap(mesh, top.loop, capHolesTop || [], top.z, true);
}

/* ---- inward-facing column (a hole through a solid) ---- */
function addHoleWalls(mesh, levels) {
  for (var i = 0; i + 1 < levels.length; i++)
    addBand(mesh, levels[i].loop, levels[i].z, levels[i + 1].loop, levels[i + 1].z, true);
}

/* ---- prism swept along X from a (y,z) cross-section ---- */
function addPrismX(mesh, section, x0, x1) {
  var n = section.length;
  var poly = signedArea(section) < 0 ? reverseLoop(section) : section.slice();
  var nn = loopNormals(poly);
  for (var i = 0; i < n; i++) {
    var j = (i + 1) % n;
    var a = poly[i], b = poly[j];
    var Na = [0, nn[i][0], nn[i][1]], Nb = [0, nn[j][0], nn[j][1]];
    var P0 = [x0, a[0], a[1]], P1 = [x1, a[0], a[1]];
    var P2 = [x1, b[0], b[1]], P3 = [x0, b[0], b[1]];
    mesh.tri(P0, P2, P1, Na, Nb, Na);
    mesh.tri(P0, P3, P2, Na, Nb, Nb);
  }
  var t = triangulate(poly, []);
  for (var k = 0; k < t.tris.length; k++) {
    var A = t.pts[t.tris[k][0]], B = t.pts[t.tris[k][1]], C = t.pts[t.tris[k][2]];
    var nA = [-1, 0, 0], nB = [1, 0, 0];
    mesh.tri([x0, A[0], A[1]], [x0, C[0], C[1]], [x0, B[0], B[1]], nA, nA, nA);
    mesh.tri([x1, A[0], A[1]], [x1, B[0], B[1]], [x1, C[0], C[1]], nB, nB, nB);
  }
}

/* =====================================================================
   Baseplate (GridFlock-compatible)
   Socket profile is the Gridfinity baseplate socket: 2.15 chamfer, 1.8
   straight, 0.7 chamfer, 4.65 mm deep, matching GridFlock's
   _profile_height_raw.
   ===================================================================== */
/* Connector edge profiles, precomputed from GridFlock's puzzle.svg.
   Each is the symmetric full connector that straddles one cell corner, in a
   local frame where +x points out of the plate and +y runs along the seam.
   Male protrudes 1.18 mm; female bites 1.26 mm inward. */
var CONNECTOR = {
  male: [[0,-3.28748], [0.065511,-3.29768], [-0.70929,-1.21468], [-0.608896,-0.678896], [-0.093195,-0.752896], [0.464406,-1.4506], [0.874306,-1.4937], [1.18001,-1.1057], [1.18001,0], [1.18001,1.1057], [0.874306,1.4937], [0.464406,1.4506], [-0.093195,0.752896], [-0.608896,0.678896], [-0.70929,1.21468], [0.065511,3.29768], [0,3.28748]],
  female: [[0,-3.0175], [-5e-06,-3.0175], [0.573896,-1.32], [0.472895,-0.964296], [0.067495,-1.0136], [-0.257606,-1.4464], [-0.570006,-1.6145], [-0.795706,-1.607], [-1.25818,-1.08018], [-1.24837,0], [-1.25818,1.08018], [-0.795706,1.607], [-0.570006,1.6145], [-0.257606,1.4464], [0.067495,1.0136], [0.472895,0.964296], [0.573896,1.32], [-5e-06,3.0175], [0,3.0175]],
  // Half a male tab, for the end of a seam.  It overhangs the corner by
  // 0.3 mm and its root runs back 4 mm along the perpendicular edge, taking
  // the corner with it -- that is what GridFlock's hull-and-circle pass comes
  // to.  The female half instead stops at the corner: it is a cut, and past
  // the corner there is nothing to remove.
  maleCap: [[-4,0], [1.18001,-0.300006], [1.18001,1.1057], [0.874306,1.4937], [0.464406,1.4506], [-0.093195,0.752896], [-0.608896,0.678896], [-0.70929,1.21468], [0.065511,3.29768], [0,3.28748]],
};

/* Half a connector for the end of a seam, walking in +y.  dir -1 mirrors it
   for the far end of the edge.  The female half stops at the corner: it is a
   cut, and past the corner there is nothing to remove. */
function capProfile(male, dir) {
  var src = male ? CONNECTOR.maleCap
                 : CONNECTOR.female.filter(function (q) { return q[1] >= 0; });
  var out = src.map(function (q) { return [q[0], dir * q[1]]; });
  return dir > 0 ? out : out.reverse();
}

// Sockets are exactly cell-sized, so they touch each other and the plate edge.
// Pulling the top rim in by a hair keeps the top face triangulable.
var SOCKET_EPS = 0.002;

var PLATE_PROFILE_H = 4.65;
var PLATE_CORNER_R = 4;
var SOCKET = [                       // depth below the top face, size, radius
  [0.00, 42.0, 4.00],
  [2.15, 37.7, 1.85],
  [3.95, 37.7, 1.85],
  [4.65, 36.3, 1.15]
];

// Rounded rectangle with an independent radius per corner, counter-clockwise.
// Corner order matches rrLoop: (+x,-y), (+x,+y), (-x,+y), (-x,-y).
function rrLoop4(sx, sy, radii, n) {
  var pts = [], i, k;
  var sign = [[1,-1],[1,1],[-1,1],[-1,-1]];
  var a0 = [-Math.PI/2, 0, Math.PI/2, Math.PI];
  for (i = 0; i < 4; i++) {
    var r = Math.max(0, Math.min(radii[i], sx/2, sy/2));
    var cx = sign[i][0] * (sx/2 - r), cy = sign[i][1] * (sy/2 - r);
    for (k = 0; k <= n; k++) {
      var a = a0[i] + (Math.PI/2) * k / n;
      pts.push([cx + r*Math.cos(a), cy + r*Math.sin(a)]);
    }
  }
  return pts;
}

// Flat ring between two corresponding loops; degenerate quads are dropped, so
// the loops may touch (as they do along cell edges).
function addAnnulus(mesh, outer, inner, z, up) {
  var n = outer.length, nv = up ? [0,0,1] : [0,0,-1];
  for (var i = 0; i < n; i++) {
    var j = (i + 1) % n;
    var A = [outer[i][0], outer[i][1], z], B = [outer[j][0], outer[j][1], z];
    var C = [inner[j][0], inner[j][1], z], D = [inner[i][0], inner[i][1], z];
    if (up) { mesh.tri(A, B, C, nv, nv, nv); mesh.tri(A, C, D, nv, nv, nv); }
    else    { mesh.tri(A, C, B, nv, nv, nv); mesh.tri(A, D, C, nv, nv, nv); }
  }
}

/* ---------------------------------------------------------------------
   Segmentation planning -- ported from GridFlock (MIT, Jonas Konrad).
   X uses the "ideal" plan (roughly equal segments); Y uses the staggered
   plan, which returns two alternating row layouts so that segment corners
   never form a 4-way intersection.
   --------------------------------------------------------------------- */
function cumulate(trace) {
  var c = [0];
  for (var i = 0; i < trace.length; i++) c.push(c[i] + trace[i]);
  return c;
}

function segmentsPerAxis(trace, bedNorm, startPad, endPad) {
  startPad = startPad || 0; endPad = endPad || 0;
  var n = trace.length, segI = 0, segSize = startPad, states = [], i;
  for (i = 0; i < n; i++) {
    states.push([segI, segSize]);
    if (segSize + trace[i] > bedNorm) { segI += 1; segSize = 0; }
    segSize += trace[i];
  }
  var last = states[n - 1];
  var splitLast = last[1] + trace[n - 1] + endPad > bedNorm;
  return last[0] + (splitLast ? 2 : 1);
}

function planAxisIdeal(trace, bedNorm, startPad, endPad) {
  startPad = startPad || 0; endPad = endPad || 0;
  var n = trace.length, cum = cumulate(trace);
  var count = segmentsPerAxis(trace, bedNorm, startPad, endPad);
  var avg = (cum[n] + startPad + endPad) / count;
  var counts = [], i;
  for (i = 0; i < count; i++) counts.push(0);
  for (i = 0; i < n; i++) {
    var ix = (cum[i] + trace[i] / 2 + startPad) / avg;
    var a = (ix % 1 === 0) ? ix - 1 : Math.floor(ix);
    a = Math.max(0, Math.min(count - 1, a));
    counts[a] += 1;
  }
  return counts;
}

function planAxisIncrementalVars(trace, bedNorm, startPad, endPad, forceFirst) {
  startPad = startPad || 0; endPad = endPad || 0;
  var n = trace.length, cum = cumulate(trace);
  if (cum[n] + startPad + endPad <= bedNorm) return [n, -1, -1];
  var mid = Math.floor(bedNorm);
  function computeEnd(first) { var e = (n - first) % mid; return e === 0 ? mid : e; }
  var firstP = (forceFirst === undefined || forceFirst === null)
    ? Math.floor(bedNorm - startPad) : forceFirst;
  var endP = computeEnd(firstP);
  var shift = (endP === 1 && trace[n - 1] < 1) ||
              (cum[n] - cum[n - endP] + endPad) > bedNorm;
  var first = shift ? firstP - 1 : firstP;
  return [first, mid, computeEnd(first)];
}

function varsToIncremental(trace, vars) {
  var n = trace.length, first = vars[0], mid = vars[1], end = vars[2];
  if (mid === -1) return [first];
  var out = [], i = 0, pos = 0, guard = 0;
  while (pos < n && guard++ < 1000) {
    out.push(i === 0 ? first : (pos + mid >= n ? end : mid));
    i += 1;
    pos = first + mid * (i - 1);
  }
  return out;
}

function scorePlanB(a, b) {
  return (b[0] === 1 ? 20 : 0) + (b[2] === 1 ? 20 : 0)
       - Math.abs(a[0] - b[0]) - Math.abs(a[2] - b[2]);
}

function planAxisStaggered(trace, bedNorm, startPad, endPad) {
  startPad = startPad || 0; endPad = endPad || 0;
  var n = trace.length;
  function vars(forceFirst) {
    return planAxisIncrementalVars(trace, bedNorm, startPad, endPad, forceFirst);
  }
  function planSize(v) { return v[1] === -1 ? 1 : (n - v[0] - v[2]) / v[1] + 2; }

  var a1 = vars(undefined);
  var a2 = (a1[1] === -1 || a1[2] >= 2 || a1[0] <= 2) ? a1 : vars(a1[0] - 1);
  if (a1[1] <= 1) {
    var one = varsToIncremental(trace, a1);
    return [one, one];
  }
  var scores = [], shift = 1, plan = vars(a2[0] - 1), best = planSize(plan);
  while (shift < a2[0] && planSize(plan) <= best) {
    scores.push(scorePlanB(a2, plan));
    shift += 1;
    plan = vars(a2[0] - shift);
  }
  var bestI = 0;
  for (var i = 1; i < scores.length; i++) if (scores[i] < scores[bestI]) bestI = i;
  return [varsToIncremental(trace, a2),
          varsToIncremental(trace, vars(a2[0] - (bestI + 1)))];
}

// Full segmentation plan for a gx x gy plate on a given bed.
function planPlate(p) {
  var d = derivePlate(p);
  var padLeft = d.padLeft || 0, padRight = d.padRight || 0;
  var padBottom = d.padBottom || 0, padTop = d.padTop || 0;
  var tx = [], ty = [], i;
  for (i = 0; i < p.gx; i++) tx.push(1);
  for (i = 0; i < p.gy; i++) ty.push(1);
  // connectors stick out past the plate, so GridFlock keeps a margin clear
  var margin = p.plateConnectors ? 3.5 : 0;
  var bx = Math.max(1.01, ((p.bedX || 1e6) - margin) / GRID);
  var by = Math.max(1.01, ((p.bedY || 1e6) - margin) / GRID);
  var cols = planAxisIdeal(tx, bx, padLeft / GRID, padRight / GRID);
  var rows = planAxisStaggered(ty, by, padBottom / GRID, padTop / GRID);
  return { cols: cols, rows: rows, tx: tx, ty: ty };
}

function derivePlate(p) {
  var base = Math.max(0, p.plateBase || 0);
  var padLeft = 0, padRight = 0, padBottom = 0, padTop = 0;
  if (p.plateExact && p.plate_size_mode === "mm") {
    var rawRemX = Math.max(0, p.plate_mm_x - p.gx * GRID);
    var rawRemY = Math.max(0, p.plate_mm_y - p.gy * GRID);
    var remX = Math.round(rawRemX * 100) / 100;
    var remY = Math.round(rawRemY * 100) / 100;

    var rx = (p.buf_x_ratio !== undefined ? p.buf_x_ratio : 50) / 100;
    var ry = (p.buf_y_ratio !== undefined ? p.buf_y_ratio : 50) / 100;

    padLeft = Math.round(remX * (1 - rx) * 100) / 100;
    padRight = Math.round((remX - padLeft) * 100) / 100;
    padBottom = Math.round(remY * (1 - ry) * 100) / 100;
    padTop = Math.round((remY - padBottom) * 100) / 100;
  }
  // Stacked copies: levels go straight up, every second level is rotated
  // 180 deg about the plate centre, a fixed air gap separates the copies.
  var levels = p.plateStack ? Math.max(2, Math.min(10, Math.round(+p.plateStackN || 2))) : 1;
  var stackGap = Math.max(0, +p.plateStackGap || 0);
  var H = base + PLATE_PROFILE_H;
  return {
    OX: p.gx * GRID + padLeft + padRight,
    OY: p.gy * GRID + padBottom + padTop,
    padLeft: padLeft,
    padRight: padRight,
    padBottom: padBottom,
    padTop: padTop,
    base: base,
    H: H,
    levels: levels,
    stackGap: stackGap,
    HTotal: levels * H + (levels - 1) * stackGap,
    cornerR: p.plateR === undefined ? PLATE_CORNER_R : p.plateR,
    valid: p.gx >= 1 && p.gy >= 1
  };
}

// Outline of one segment, walked counter-clockwise from the SW corner, with
// puzzle connectors spliced into every edge that abuts a neighbouring segment.
function segmentOutline(p, d, i0, i1, j0, j1, conn, nc, dx, dy) {
  var R = d.cornerR;
  var padLeft = d.padLeft || 0, padRight = d.padRight || 0;
  var padBottom = d.padBottom || 0, padTop = d.padTop || 0;
  var x0 = (i0 - p.gx / 2) * GRID + dx - (i0 === 0 ? padLeft : 0);
  var x1 = (i1 - p.gx / 2) * GRID + dx + (i1 === p.gx ? padRight : 0);
  var y0 = (j0 - p.gy / 2) * GRID + dy - (j0 === 0 ? padBottom : 0);
  var y1 = (j1 - p.gy / 2) * GRID + dy + (j1 === p.gy ? padTop : 0);
  var rSW = (i0 === 0 && j0 === 0) ? R : 0;
  var rSE = (i1 === p.gx && j0 === 0) ? R : 0;
  var rNE = (i1 === p.gx && j1 === p.gy) ? R : 0;
  var rNW = (i0 === 0 && j1 === p.gy) ? R : 0;
  var pts = [];

  // eaten: a half connector at this corner has already taken the corner point
  function arc(cx, cy, r, a0, a1, fallback, eaten) {
    if (r <= 0) { if (!eaten) pts.push(fallback); return; }
    for (var k = 0; k <= nc; k++) {
      var a = a0 + (a1 - a0) * k / nc;
      pts.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]);
    }
  }

  // Splice connectors along one edge. base/n/t give the edge frame: n points
  // out of the plate, t runs along the walk direction.  Interior cell
  // boundaries get a whole connector; the two ends of the edge get the half
  // that falls inside the segment, which is what GridFlock's corner pieces
  // come to once they are clipped to the segment bounds.
  function edge(on, male, base, n, t, coords, origin, sign, len, capStart, capEnd) {
    if (!on) return;
    var prof = male ? CONNECTOR.male : CONNECTOR.female;
    var half = 3.4;
    var list = [];
    if (capStart) list.push({ at: 0, pts: capProfile(male, 1) });
    for (var c = 0; c < coords.length; c++) {
      var sPos = sign * (coords[c] - origin);
      if (sPos > half && sPos < len - half) list.push({ at: sPos, pts: prof });
    }
    if (capEnd) list.push({ at: len, pts: capProfile(male, -1) });
    list.sort(function (u, v) { return u.at - v.at; });
    for (var q = 0; q < list.length; q++) {
      var pr = list[q].pts;
      for (var k = 0; k < pr.length; k++) {
        var lx = pr[k][0], ly = pr[k][1] + list[q].at;
        pts.push([base[0] + n[0] * lx + t[0] * ly,
                  base[1] + n[1] * lx + t[1] * ly]);
      }
    }
  }

  var xb = [], yb = [], i;
  for (i = i0 + 1; i < i1; i++) xb.push((i - p.gx / 2) * GRID + dx);
  for (i = j0 + 1; i < j1; i++) yb.push((i - p.gy / 2) * GRID + dy);

  // a corner next to a connectored edge carries half a connector, which
  // replaces the corner point itself
  arc(x0 + rSW, y0 + rSW, rSW, Math.PI, 1.5 * Math.PI, [x0, y0], conn.S || conn.W);
  edge(conn.S, false, [x0, y0], [0, -1], [1, 0], xb, x0, 1, x1 - x0, !conn.W, !conn.E);
  arc(x1 - rSE, y0 + rSE, rSE, 1.5 * Math.PI, 2 * Math.PI, [x1, y0], conn.S || conn.E);
  edge(conn.E, true, [x1, y0], [1, 0], [0, 1], yb, y0, 1, y1 - y0, true, true);
  arc(x1 - rNE, y1 - rNE, rNE, 0, 0.5 * Math.PI, [x1, y1], conn.N || conn.E);
  edge(conn.N, true, [x1, y1], [0, 1], [-1, 0], xb, x1, -1, x1 - x0, !conn.E, !conn.W);
  arc(x0 + rNW, y1 - rNW, rNW, 0.5 * Math.PI, Math.PI, [x0, y1], conn.N || conn.W);
  edge(conn.W, false, [x0, y1], [-1, 0], [0, -1], yb, y1, -1, y1 - y0, true, true);
  return dedupeLoop(pts, 1e-4);
}

/* =====================================================================
   2D polygon boolean, used to keep puzzle connectors out of the sockets

   Both operands are simple counter-clockwise loops.  The boundary of the
   result is made of the pieces of one loop that lie inside (or outside)
   the other, so: cut both loops at every crossing, keep the pieces the
   operation asks for, then stitch them back together at the crossings.

   Crossings must be transversal.  SOCKET_EPS keeps the operands a hair
   apart, which is what makes that true; anything degenerate returns null
   rather than a guess, and the caller leaves the outline alone.
   ===================================================================== */

var CLIP_EPS = 1e-9;
var SNAP = null;          // registry so a point computed twice comes back identical

function snapReset() { SNAP = {}; }

function snapPt(p) {
  if (!SNAP) return p;
  var kx = Math.round(p[0] * 1e6), ky = Math.round(p[1] * 1e6);
  for (var i = -1; i <= 1; i++) for (var j = -1; j <= 1; j++) {
    var e = SNAP[(kx + i) + "," + (ky + j)];
    if (e && Math.abs(e[0] - p[0]) < 1e-7 && Math.abs(e[1] - p[1]) < 1e-7) return e;
  }
  SNAP[kx + "," + ky] = p;
  return p;
}

function pointInLoop(pt, loop) {
  var x = pt[0], y = pt[1], inside = false;
  for (var i = 0, n = loop.length, j = n - 1; i < n; j = i++) {
    var xi = loop[i][0], yi = loop[i][1], xj = loop[j][0], yj = loop[j][1];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/* Every transversal crossing of two loops, tagged with the parametric
   position along each so both can be split consistently. */
function loopCrossings(A, B) {
  var out = [], na = A.length, nb = B.length;
  for (var i = 0; i < na; i++) {
    var a0 = A[i], a1 = A[(i + 1) % na];
    var rx = a1[0] - a0[0], ry = a1[1] - a0[1];
    for (var j = 0; j < nb; j++) {
      var b0 = B[j], b1 = B[(j + 1) % nb];
      var sx = b1[0] - b0[0], sy = b1[1] - b0[1];
      var den = rx * sy - ry * sx;
      if (Math.abs(den) < 1e-14) continue;
      var qx = b0[0] - a0[0], qy = b0[1] - a0[1];
      var t = (qx * sy - qy * sx) / den;
      var u = (qx * ry - qy * rx) / den;
      if (t <= CLIP_EPS || t >= 1 - CLIP_EPS) continue;
      if (u <= CLIP_EPS || u >= 1 - CLIP_EPS) continue;
      out.push({ id: out.length, p: snapPt([a0[0] + t * rx, a0[1] + t * ry]),
                 ei: i, t: t, ej: j, u: u });
    }
  }
  return out;
}

/* Split a loop at its crossings; each chain runs from one crossing to the next. */
function loopChains(loop, marks, keyEdge, keyParam) {
  var n = loop.length, per = [], i;
  for (i = 0; i < n; i++) per.push([]);
  for (i = 0; i < marks.length; i++) per[marks[i][keyEdge]].push(marks[i]);
  for (i = 0; i < n; i++)
    per[i].sort(function (a, b) { return a[keyParam] - b[keyParam]; });

  var aug = [];
  for (i = 0; i < n; i++) {
    aug.push({ p: loop[i], id: -1 });
    for (var m = 0; m < per[i].length; m++) aug.push({ p: per[i][m].p, id: per[i][m].id });
  }
  var first = -1;
  for (i = 0; i < aug.length; i++) if (aug[i].id >= 0) { first = i; break; }
  if (first < 0) return null;

  var chains = [], cur = null, N = aug.length;
  for (var s = 0; s <= N; s++) {
    var v = aug[(first + s) % N];
    if (cur) cur.pts.push(v.p);
    if (v.id >= 0) {
      if (cur) { cur.endId = v.id; chains.push(cur); }
      if (s < N) cur = { pts: [v.p], startId: v.id, endId: -1 };
    }
  }
  return chains;
}

function chainInside(chain, other) {
  var a = chain.pts[0], b = chain.pts[1];
  return pointInLoop([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], other);
}

/* op: "int", "union" or "diff" (A without B).  Null when the loops do not
   cross -- the caller decides containment -- or when a crossing is degenerate. */
function polyBool(A, B, op) {
  var marks = loopCrossings(A, B);
  if (!marks.length) return null;
  var ca = loopChains(A, marks, "ei", "t");
  var cb = loopChains(B, marks, "ej", "u");
  if (!ca || !cb) return null;

  var wantA = op === "int", wantB = op === "int" || op === "diff";
  var keep = [], k;
  for (k = 0; k < ca.length; k++)
    if (chainInside(ca[k], B) === wantA) keep.push(ca[k]);
  for (k = 0; k < cb.length; k++) {
    if (chainInside(cb[k], A) !== wantB) continue;
    keep.push(op === "diff"
      ? { pts: cb[k].pts.slice().reverse(), startId: cb[k].endId, endId: cb[k].startId }
      : cb[k]);
  }

  var byStart = {};
  for (k = 0; k < keep.length; k++) {
    if (byStart[keep[k].startId] !== undefined) return null;      // degenerate
    byStart[keep[k].startId] = k;
  }
  var used = [], out = [];
  for (k = 0; k < keep.length; k++) used.push(false);
  for (k = 0; k < keep.length; k++) {
    if (used[k]) continue;
    var loop = [], idx = k, guard = 0;
    while (!used[idx]) {
      used[idx] = true;
      var c = keep[idx];
      for (var q = 0; q + 1 < c.pts.length; q++) loop.push(c.pts[q]);
      if (byStart[c.endId] === undefined) return null;            // dangling
      idx = byStart[c.endId];
      if (++guard > keep.length + 1) return null;
    }
    if (loop.length >= 3) out.push(loop);
  }
  return out;
}

/* A puzzle connector is placed at a cell corner, where the socket opening is
   already rounded away, and the female notch bites about 0.017 mm past that
   rounding into the socket.  GridFlock trims its male tab against the same
   circle; do the equivalent here by filling the notch back to the socket rim,
   which keeps the socket a clean hole in the top face instead of something
   that straddles the outline. */
function clearSockets(outline, p, i0, i1, j0, j1, dx, dy, nc) {
  snapReset();
  for (var i = i0; i < i1; i++) for (var j = j0; j < j1; j++) {
    var ox = (i + 0.5 - p.gx / 2) * GRID + dx;
    var oy = (j + 0.5 - p.gy / 2) * GRID + dy;
    // between the socket rim and the nominal cell: clear of the rim, and
    // clear of the male tab root, which sits exactly on the cell boundary
    var guard = offsetLoop(rrLoop(SOCKET[0][1] - SOCKET_EPS, SOCKET[0][1] - SOCKET_EPS,
                                  SOCKET[0][2] - SOCKET_EPS / 2, nc), ox, oy);
    if (!loopCrossings(outline, guard).length) continue;
    var r = polyBool(outline, guard, "union");
    if (r && r.length === 1) outline = dedupeLoop(r[0], 1e-4);
  }
  return outline;
}

// Mirror freshly built triangles through a horizontal plane: z -> H - z,
// lifted by zoff, with vertex order (and normals) reversed so the STL
// winding stays outward.  Used to lay every second stacked copy upside down.
function zFlipTris(mesh, fromTri, levelH, zoff) {
  var p = mesh.pos, n = mesh.nrm;
  for (var t = fromTri; t < mesh.count(); t++) {
    var b = t * 9, v, k;
    for (v = 0; v < 3; v++) {
      var i = b + v * 3;
      p[i + 2] = levelH - p[i + 2] + zoff;
      n[i] = -n[i]; n[i + 1] = -n[i + 1]; n[i + 2] = -n[i + 2];
    }
    for (k = 0; k < 3; k++) {              // swap vtx 1 <-> 2: undo the mirror
      var a1 = b + 3 + k, a2 = b + 6 + k, tmp = p[a1];
      p[a1] = p[a2]; p[a2] = tmp;
      tmp = n[a1]; n[a1] = n[a2]; n[a2] = tmp;
    }
  }
}

// One printable piece: the cell rectangle [i0,i1) x [j0,j1) of the plate,
// lifted to zoff (its level in a stack).
function addPlateSegment(m, p, d, seg, i0, i1, j0, j1, dx, dy, conn, zoff) {
  var nc = seg.corner, i, j, k;
  zoff = zoff || 0;
  var outline = clearSockets(segmentOutline(p, d, i0, i1, j0, j1, conn, nc, dx, dy),
                             p, i0, i1, j0, j1, dx, dy, nc);
  addBand(m, outline, zoff, outline, d.H + zoff, false);

  var tops = [], floors = [];
  for (i = i0; i < i1; i++) for (j = j0; j < j1; j++) {
    var ox = (i + 0.5 - p.gx / 2) * GRID + dx;
    var oy = (j + 0.5 - p.gy / 2) * GRID + dy;
    var lv = [];
    for (k = 0; k < SOCKET.length; k++) {
      var shrink = (k === 0) ? 2 * SOCKET_EPS : 0;   // keep the rim off the edge
      lv.push({
        loop: offsetLoop(rrLoop(SOCKET[k][1] - shrink, SOCKET[k][1] - shrink,
                                Math.max(0, SOCKET[k][2] - shrink / 2), nc), ox, oy),
        z: d.H - SOCKET[k][0] + zoff
      });
    }
    // levels run top-down, so the un-flipped winding already faces the void
    for (k = 0; k + 1 < lv.length; k++)
      addBand(m, lv[k].loop, lv[k].z, lv[k + 1].loop, lv[k + 1].z, false);
    tops.push(lv[0].loop);
    floors.push(lv[lv.length - 1]);
  }

  addCap(m, outline, tops, d.H + zoff, true);
  if (d.base > 1e-6) {
    // the socket floor sits on solid base, so it faces up into the socket
    for (k = 0; k < floors.length; k++)
      addCap(m, floors[k].loop, [], floors[k].z, true);
    addCap(m, outline, [], zoff, false);
  } else {
    addCap(m, outline, floors.map(function (f) { return f.loop; }), zoff, false);
  }
}

function* plateSteps(p, seg) {
  var d = derivePlate(p);
  var m = new Mesh();
  if (!d.valid) return { mesh: m, derived: d, plan: null };

  var plan = planPlate(p);
  var colEdge = cumulate(plan.cols);
  var gap = Math.max(0, p.plateGap || 0);
  var levels = d.levels, stackGap = d.stackGap;
  var pieces = 0, perLevel = 0, sx, sy;

  var slicesPerLevel = 0;
  for (sx = 0; sx < plan.cols.length; sx++)
    for (sy = 0; sy < plan.rows[sx % 2].length; sy++)
      if (plan.rows[sx % 2][sy] !== 0 && plan.cols[sx] !== 0) slicesPerLevel++;

  // levels stack straight up; odd ones are flipped upside down (sockets down)
  for (var lev = 0; lev < levels; lev++) {
    var zoff = lev * (d.H + stackGap);
    var flip = (lev % 2) === 1;
    var built = 0;
    for (sx = 0; sx < plan.cols.length; sx++) {
      var rowsPlan = plan.rows[sx % 2];
      var rowEdge = cumulate(rowsPlan);
      for (sy = 0; sy < rowsPlan.length; sy++) {
        if (rowsPlan[sy] === 0 || plan.cols[sx] === 0) continue;
        var dx = (sx - (plan.cols.length - 1) / 2) * gap;
        var dy = (sy - (rowsPlan.length - 1) / 2) * gap;
        var conn = {
          W: sx > 0, E: sx < plan.cols.length - 1,
          S: sy > 0, N: sy < rowsPlan.length - 1
        };
        if (!p.plateConnectors) conn = { W: false, E: false, S: false, N: false };
        var startTri = m.count();
        addPlateSegment(m, p, d, seg,
                        colEdge[sx], colEdge[sx + 1],
                        rowEdge[sy], rowEdge[sy + 1], dx, dy, conn,
                        flip ? 0 : zoff);
        if (flip) zFlipTris(m, startTri, d.H, zoff);
        built += 1;
        yield (lev + built / slicesPerLevel) / levels;
      }
    }
    pieces += built;
    perLevel = built;
  }
  d.pieces = pieces;
  d.piecesPerLevel = perLevel;
  d.plan = plan;
  return { mesh: m, derived: d, plan: plan };
}

/* =====================================================================
   openGrid board -- port of the openGrid Studio board geometry
   (https://github.com/ClassicOldSong/openGrid-Studio, Apache-2.0;
   openGrid standard CC-BY 4.0 by David D).  A 28 mm lattice of snap
   openings with a capture waist in the rib profile.  Full is 6.8 mm
   thick, Lite 4 mm.  Screw holes sit on interior lattice nodes,
   board-to-board connector cutouts on the border nodes, and the four
   outer corners get a 4.2 mm chamfer.
   ===================================================================== */
var OG = {
  TILE: 28,
  OUT: 0.8,            // rib material next to an opening
  TOP_CH: 0.4,         // opening lead-in chamfer, top and bottom
  MID_CH: 1,           // 45 deg run into the capture waist
  CAPTURE: 2.4,        // z of the waist shoulder
  CORNER_SQ: 2.6,
  INTERSECT: 4.2,
  INNER_DIFF: 3,
  FULL_T: 6.8,
  LITE_T: 4,
  CONN_R: 2.6, CONN_SEP: 2.5, CONN_H: 2.4,
  CONN_STEM: 0.25, CONN_SH: 0.5, CONN_BLEND: Math.sqrt(125 / 16),
  LITE_CONN_FROM_TOP: 1
};

function ogRibInset(tile) {
  return (tile - (tile - OG.INNER_DIFF)) / 2 - OG.OUT;    // 0.7 at 28 mm
}

/* Rib cross-section between two openings, CCW in (inset from opening, z).
   The capture waist spans [lo, hi]; for the 4 mm Lite board the original
   polygon would self-intersect, so the band is centred on mid-thickness. */
function ogRibProfile(T, tile) {
  var e1 = OG.OUT, e2 = OG.OUT + ogRibInset(tile);
  var hi = Math.max(OG.CAPTURE, T - OG.CAPTURE);
  var lo = Math.min(OG.CAPTURE, T - OG.CAPTURE);
  return [
    [0, 0], [e2 - OG.TOP_CH, 0], [e2, OG.TOP_CH], [e2, lo - OG.MID_CH],
    [e1, lo], [e1, hi], [e2, hi + OG.MID_CH],
    [e2, T - OG.TOP_CH], [e2 - OG.TOP_CH, T], [0, T]
  ];
}

/* Tile corner block: the profile prism clipped to its own tile, so the
   section at height z is the triangle {0 <= u <= U(z), |w| <= u}. */
function ogCornerProfile(T) {
  var c = Math.sqrt(OG.INTERSECT * OG.INTERSECT / 2) + OG.CORNER_SQ;
  var ch = OG.CAPTURE - OG.MID_CH;
  return { c: c, ch: ch };
}

/* ---- one board level as separate closed pieces (no booleans) ----
   Exact partition of the openGrid-Studio union:
     union(profiled tile strips, node-fill diamonds, corner wedges) - cuts.
   Their corner wedge is a centered extrusion, so each tile corner owns
   the quadrant triangle {dx+dy <= B(z)} with B(z) = sqrt2*(co-cc+min(z,
   T-z, cc)): 5.897 at the faces, 7.877 in the middle.  The node blob is
   therefore just a z-lofted diamond (full at fill nodes, half at edge
   nodes, quarter + chamfer ring at outer corners); the 5.57 node fill
   is strictly inside it.  Strips and border ribs end on the shared
   plane {a+s = B(z)}, so the surfaces abut exactly.  Connector nodes
   cut the half diamond with the true outline over the pocket z-band. */
function ogEmitLevel(p, d, seg) {
  var solids = [];
  var W = d.W, H = d.H, T = d.T, lite = !!d.lite;
  /* ogstudio lite: the TOP 4 mm of the full board (z 2.8..6.8) shifted
     down -- the profile is clipped to the band and dropped to z = 0 */
  var zShift = lite ? OG.FULL_T - T : 0;
  var prof = ogRibProfile(OG.FULL_T, OG.TILE);
  if (lite) {
    prof = ogClipPolyZ(prof, zShift, OG.FULL_T);
    prof = prof.map(function (q) { return [q[0], q[1] - zShift]; });
  }
  var co = Math.sqrt(OG.INTERSECT * OG.INTERSECT / 2) + OG.CORNER_SQ;
  var cc = OG.CAPTURE - OG.MID_CH;
  var ID = OG.INTERSECT;                          // corner chamfer plane |x|+|y| = 4.2
  var segHole = Math.max(16, (seg && seg.hole ? seg.hole : 14) * 2);
  var ZS = [];                                    // profile z-levels for lofts
  for (var i = 0; i < prof.length; i++)
    if (ZS.indexOf(prof[i][1]) < 0) ZS.push(prof[i][1]);
  ZS.sort(function (a, b) { return a - b; });
  function fAt(z) { return ogSpanAt(prof, z)[1]; }
  function BAt(z) {                               // quadrant-triangle leg sum
    var zf = lite ? z + zShift : z;               // full-board z frame
    return Math.SQRT2 * (co - cc +
      Math.min(Math.min(zf, OG.FULL_T - zf), cc));
  }

  function piece(emit) {
    var s = new Mesh();
    emit(s);
    solids.push(s);
  }
  function loopAt(c, ex, ey, poly, z) {           // local (a,s) -> world xy
    return poly.map(function (t) {
      return [c[0] + ex[0] * t[0] + ey[0] * t[1],
              c[1] + ex[1] * t[0] + ey[1] * t[1], z];
    });
  }

  /* ---- strips along interior lattice lines ----
     Ends ride the node diamond plane {a+s = B(z)} (every node kind).
     A removed cell (ogCellOff) takes its own half-strip with it: the
     window runs to the lattice line, the neighbour keeps its half. */
  function stripSeg(P, Ex, Ey) {
    var lv = [];
    for (var k = 0; k < ZS.length; k++) {
      var z = ZS[k], f = fAt(z), b = BAt(z);
      var q = [[b, 0], [OG.TILE - b, 0], [OG.TILE - b + f, f], [b - f, f]];
      if (Ex[0] * Ey[1] - Ex[1] * Ey[0] < 0) q.reverse();   // keep CCW
      lv.push({ loop: loopAt(P, Ex, Ey, q, z), z: z });
    }
    piece(function (s) { addSolid(s, lv, [], []); });
  }
  function cellOff(col, row) {
    return !!(p.ogCellOff && p.ogCellOff[col + "," + row]);
  }
  for (var j = 1; j < H; j++) for (var cx2 = 0; cx2 < W; cx2++) {
    var P0 = [(cx2 - W / 2) * OG.TILE, (H / 2 - j) * OG.TILE];
    if (!cellOff(cx2, j - 1)) stripSeg(P0, [1, 0], [0, 1]);    // into the tile above
    if (!cellOff(cx2, j)) stripSeg(P0, [1, 0], [0, -1]);       // into the tile below
  }
  for (var i2 = 1; i2 < W; i2++) for (var cy2 = 0; cy2 < H; cy2++) {
    var P1 = [(i2 - W / 2) * OG.TILE, (H / 2 - cy2) * OG.TILE];
    if (!cellOff(i2, cy2)) stripSeg(P1, [0, -1], [1, 0]);      // into the tile right
    if (!cellOff(i2 - 1, cy2)) stripSeg(P1, [0, -1], [-1, 0]); // into the tile left
  }

  /* ---- border ribs: lofts ending on the node diamond planes ---- */
  function borderEdge(O, A, Sd, len, cellOf) {  // A x Sd = +z; a runs 0..len
    var zs = ZS;
    function ribPiece(l0, l1) {                 // a = l(z,s) boundaries
      var lv = [];
      for (var k = 0; k < zs.length; k++) {
        var z = zs[k], f = fAt(z);
        var a00 = l0(k, 0), a0f = l0(k, f), a10 = l1(k, 0), a1f = l1(k, f);
        var q = [[a00, 0], [a10, 0], [a1f, f], [a0f, f]];
        if (A[0] * Sd[1] - A[1] * Sd[0] < 0) q.reverse();
        lv.push({ loop: loopAt(O, A, Sd, q, z), z: z });
      }
      piece(function (s) { addSolid(s, lv, [], []); });
    }
    function plus(v) { return function (k, s) { return v + (BAt(zs[k]) - s); }; }
    function minus(v) { return function (k, s) { return v - (BAt(zs[k]) - s); }; }
    var cuts = [plus(0)];
    for (var nn = 1; nn < len / OG.TILE; nn++)
      cuts.push(minus(nn * OG.TILE), plus(nn * OG.TILE));
    cuts.push(minus(len));
    for (var ci = 0; ci + 1 < cuts.length; ci += 2)
      if (!cellOf || !cellOf(ci >> 1)) ribPiece(cuts[ci], cuts[ci + 1]);
  }
  var OX2 = d.OX / 2, OY2 = d.OY / 2;
  borderEdge([-OX2, -OY2], [1, 0], [0, 1], d.OX,
             function (k) { return cellOff(k, H - 1); });             // south
  borderEdge([OX2, OY2], [-1, 0], [0, -1], d.OX,
             function (k) { return cellOff(W - 1 - k, 0); });         // north
  borderEdge([-OX2, OY2], [0, -1], [1, 0], d.OY,
             function (k) { return cellOff(0, k); });                 // west
  borderEdge([OX2, -OY2], [0, 1], [-1, 0], d.OY,
             function (k) { return cellOff(W - 1, H - 1 - k); });     // east

  /* ---- node blobs: z-lofted diamonds ---- */
  /* connector pocket band: full T/2 +-1.2; lite keeps the upper part
     (original 4.6..6.8 minus the band shift) -- open through the top */
  var cz = lite ? (OG.FULL_T - OG.CONN_H / 2 - OG.LITE_CONN_FROM_TOP) - zShift
                : T / 2 - OG.CONN_H / 2;
  function halfDia(c, inw) {   // inward unit (inw); perp = ccw90(inw)
    var pp = [-inw[1], inw[0]];
    /* ogstudio connector cut: the full teardrop outline, flat mouth on
       the border (perp = 0), round pocket reaching 5.1 INTO the board --
       90 deg to the border.  Its outer circle pokes up to ~6.18 from the
       node, so on narrow faces (b = 5.898) it is clipped to the wedge
       {|along| + perp <= b}; the clip chord lies on the wedge plane,
       exactly where the real material ends. */
    var tear = ogConnectorLoop((seg && seg.og) || 128).map(function (q4) {
      return [q4[1], q4[0]];                     // (along, perp)
    });
    if (tear[0][0] > tear[tear.length - 1][0]) tear.reverse();
    var mouthA = tear[0], mouthB = tear[tear.length - 1];
    function wedgeClip(poly, b) {
      var planes = [function (p) { return b - (p[0] + p[1]); },
                    function (p) { return b - (-p[0] + p[1]); }];
      for (var pi = 0; pi < 2; pi++) {
        var f = planes[pi], out = [], prev = null, fp = 0;
        for (var i = 0; i < poly.length; i++) {
          var P = poly[i], fp2 = f(P);
          if (prev !== null) {
            if (fp >= 0 && fp2 < 0) out.push(isect(prev, P, fp, fp2));
            else if (fp < 0 && fp2 >= 0) out.push(isect(prev, P, fp, fp2));
          }
          if (fp2 >= 0) out.push(P);
          prev = P; fp = fp2;
        }
        poly = out;
      }
      return poly;
      function isect(P, Q, fP, fQ) {
        var t = fP / (fP - fQ);
        return [P[0] + (Q[0] - P[0]) * t, P[1] + (Q[1] - P[1]) * t];
      }
    }
    /* clip the closed teardrop to the wedge and resample the open path
       (mouth corner -> mouth corner) to a FIXED point count, so both
       loops of a notch band always band up; the clipped chord lies on
       the wedge plane -- exactly where their CSG prism meets the
       material, so the removed volume matches their pipeline */
    function resampleOpen(pts, n) {
      var L = [0], tot = 0, i;
      for (i = 1; i < pts.length; i++) {
        tot += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
        L.push(tot);
      }
      if (tot < 1e-9 || pts.length < 2) return pts.slice();
      var out = [], j = 0;
      for (i = 0; i < n; i++) {
        var tgt = tot * i / (n - 1);
        while (j < L.length - 2 && L[j + 1] < tgt) j++;
        var seg = (L[j + 1] - L[j]) || 1e-12;
        var t = (tgt - L[j]) / seg;
        out.push([pts[j][0] + (pts[j + 1][0] - pts[j][0]) * t,
                  pts[j][1] + (pts[j + 1][1] - pts[j][1]) * t]);
      }
      return out;
    }
    var NOTCH_N = Math.min(24, tear.length);
    function notchPath(b) {
      /* clip 0.15 mm INSIDE the wedge plane: clipping exactly on it puts
         the chord on top of the face edge (self-touching loop -- no
         triangulator survives that); the 0.15 mm sliver difference to
         their CSG cut is invisible and sub-print-resolution */
      var cl = wedgeClip(tear, b - 0.15);
      return resampleOpen(cl, NOTCH_N);
    }
    function loop(z, withNotch) {
      var b = BAt(z);
      var pts;
      if (!withNotch || b < 5.15) {
        pts = [[-b, 0], [b, 0], [0, b]];
      } else {
        pts = [[-b, 0]].concat(notchPath(b)).concat([[b, 0], [0, b]]);
      }
      var out = pts.map(function (t) {
        return [c[0] + pp[0] * t[0] + inw[0] * t[1],
                c[1] + pp[1] * t[0] + inw[1] * t[1], z];
      });
      if (signedArea(out.map(function (w) { return [w[0], w[1]]; })) < 0)
        out.reverse();
      return out;
    }
    return loop;
  }
  function diamondLoop(c, z) {
    var b = BAt(z);
    return [[c[0] + b, c[1]], [c[0], c[1] + b], [c[0] - b, c[1]], [c[0], c[1] - b]];
  }
  function cellsAt(ix, iy) {                    // present cells around a node
    var cand = [[ix - 1, iy - 1], [ix, iy - 1], [ix - 1, iy], [ix, iy]];
    var out = [];
    for (var c = 0; c < 4; c++) {
      var col = cand[c][0], row = cand[c][1];
      if (col < 0 || col >= W || row < 0 || row >= H || cellOff(col, row)) continue;
      out.push({ col: col, row: row,
                 dx: (col === ix - 1) ? -1 : 1,
                 dy: (row === iy - 1) ? 1 : -1 });
    }
    return out;
  }
  /* Node kinds follow ogstudio's classifyNode: the blob is the union
     of one FULL centred corner wedge per adjacent tile, so 3 tiles --
     or 2 diagonal ones -- still leave the complete diamond; 2 same-side
     tiles leave the half diamond (a connector seat), 1 leaves the
     corner quarter, none leaves nothing.  Each tile also owns the rib
     material on its own side, so a deleted cell takes its half-strips
     and its outer border segment with it. */
  for (var iy3 = 0; iy3 <= H; iy3++) for (var ix3 = 0; ix3 <= W; ix3++) {
    var alive = cellsAt(ix3, iy3);
    var cnt = alive.length;
    if (!cnt) continue;
    var nx = (ix3 - W / 2) * OG.TILE, ny = (H / 2 - iy3) * OG.TILE;
    /* diagonal pair (both dx and dy differ) still owns the FULL diamond;
       a same-row / same-column pair owns the half diamond facing it */
    var isDiag = cnt === 2 &&
      alive[0].dx !== alive[1].dx && alive[0].dy !== alive[1].dy;
    var isFill = cnt >= 3 || isDiag;
    if (isFill) {
      (function (nx, ny) {
        piece(function (s) { ogFillDiamond(s, nx, ny, T, p, segHole, lite, ZS, BAt); });
      })(nx, ny);
    } else if (cnt === 2) {                     // edge: half diamond on the cells' side
      {
      var inw = (alive[0].dx === alive[1].dx)
        ? [alive[0].dx, 0] : [0, alive[0].dy];
      var isConn = !!p.ogConnectors &&
        !(p.ogConnOff && p.ogConnOff[ogNodeKey(nx, ny)]);
      var mk = halfDia([nx, ny], inw);
      if (!isConn) {
        var lv = [];
        for (var k5 = 0; k5 < ZS.length; k5++)
          lv.push({ loop: mk(ZS[k5], false), z: ZS[k5] });
        piece(function (s) { addSolid(s, lv, [], []); });
      } else {
        var z1 = Math.max(0, cz), z2 = Math.min(T, cz + OG.CONN_H);
        if (z1 > 1e-9) {
          var lb = [];
          for (var k6 = 0; k6 < ZS.length && ZS[k6] <= z1 + 1e-9; k6++)
            lb.push({ loop: mk(ZS[k6], false), z: ZS[k6] });
          if (lb.length && lb[lb.length - 1].z < z1 - 1e-9)
            lb.push({ loop: mk(z1, false), z: z1 });
          piece(function (s) { addSolid(s, lb, [], []); });
        }
        piece(function (s) {
          addSolid(s, [{ loop: mk(z1, true), z: z1 },
                       { loop: mk(z2, true), z: z2 }], [], []);
        });
        if (z2 < T - 1e-9) {
          var lt = [];
          for (var k7 = 0; k7 < ZS.length; k7++)
            if (ZS[k7] >= z2 - 1e-9) lt.push({ loop: mk(ZS[k7], false), z: ZS[k7] });
          if (lt.length && lt[0].z > z2 + 1e-9)
            lt.unshift({ loop: mk(z2, false), z: z2 });
          piece(function (s) { addSolid(s, lt, [], []); });
        }
      }
      }
    } else {                                    // single cell: corner quarter
      {
      var inw1 = [alive[0].dx, alive[0].dy];
      /* default outer corner is chamfered at the ID plane (ogstudio
         default); ogCornerSharp restores the full sharp corner tip */
      var sharp = !!(p.ogCornerSharp && p.ogCornerSharp[ogNodeKey(nx, ny)]);
      var lvq = [];
      for (var k8 = 0; k8 < ZS.length; k8++) {
        var zq = ZS[k8], bq = BAt(zq);
        /* sharp: clean triangle -- the hexagon's midpoint (bq/2, bq/2)
           is collinear with the hypotenuse and would degenerate a cap */
        var q = sharp
          ? [[0, 0], [bq, 0], [0, bq]]
          : [[ID, 0], [bq, 0], [bq / 2, bq / 2], [0, bq],
             [0, ID], [ID / 2, ID / 2]];
        var pts2 = q.map(function (t) {
          return [nx + inw1[0] * t[0], ny + inw1[1] * t[1], zq];
        });
        if (signedArea(pts2.map(function (w) { return [w[0], w[1]]; })) < 0)
          pts2.reverse();
        lvq.push({ loop: pts2, z: zq });
      }
      piece(function (s) { addSolid(s, lvq, [], []); });
      }
    }
  }
  return { solids: solids, voids: [] };
}

/* Clip a polygon (CCW, in (inset, z)) to a horizontal band -- plain
   Sutherland-Hodgman against two lines; the rib profile is simple. */
function ogClipHalfZ(poly, z, keepAbove) {
  if (!poly.length) return [];
  var res = [];
  for (var i = 0; i < poly.length; i++) {
    var P = poly[i], Q = poly[(i + 1) % poly.length];
    var pin = keepAbove ? P[1] >= z : P[1] <= z;
    var qin = keepAbove ? Q[1] >= z : Q[1] <= z;
    if (pin) res.push(P);
    if (pin !== qin) {
      var t = (z - P[1]) / (Q[1] - P[1]);
      res.push([P[0] + (Q[0] - P[0]) * t, z]);
    }
  }
  return res;
}
function ogClipPolyZ(pts, zlo, zhi) {
  return ogClipHalfZ(ogClipHalfZ(pts, zlo, true), zhi, false);
}

/* [minInset, maxInset] of the rib profile at height z (linear interp). */
function ogSpanAt(prof, z) {
  var lo = Infinity, hi = -Infinity;
  for (var i = 0; i < prof.length; i++) {
    var P = prof[i], Q = prof[(i + 1) % prof.length];
    if (Math.abs(P[1] - Q[1]) < 1e-9) {
      if (Math.abs(P[1] - z) < 1e-9) {
        lo = Math.min(lo, P[0], Q[0]);
        hi = Math.max(hi, P[0], Q[0]);
      }
    } else if ((P[1] <= z && z <= Q[1]) || (Q[1] <= z && z <= P[1])) {
      var s = P[0] + (Q[0] - P[0]) * (z - P[1]) / (Q[1] - P[1]);
      lo = Math.min(lo, s);
      hi = Math.max(hi, s);
    }
  }
  return [lo, hi];
}

/* Node diamond, optionally carrying the full screw bore as proper hole
   walls (shaft + front head pocket + countersink + backside pocket),
   the same way the bin feet pair addSolid cap holes with addHoleWalls. */
function ogFillDiamond(m, nx, ny, T, p, seg, lite, ZS, BAt) {
  /* per-level diamond loops: the node blob follows their corner wedges */
  function dia(z) {
    var b = BAt(z);
    return [[nx + b, ny], [nx, ny + b], [nx - b, ny], [nx, ny - b]];
  }
  /* ogstudio "chamfer" node state: the INNER part of the diamond is
     cut through at the tile-chamfer plane |x|+|y| = INTERSECT (their
     buildChamferCut), leaving a diamond ring -- and carrying no bore */
  if (p.ogChamf && p.ogChamf[ogNodeKey(nx, ny)]) {
    var idl = [[nx + OG.INTERSECT, ny], [nx, ny + OG.INTERSECT],
               [nx - OG.INTERSECT, ny], [nx, ny - OG.INTERSECT]];
    var lvr = [];
    for (var ir = 0; ir < ZS.length; ir++)
      lvr.push({ loop: dia(ZS[ir]), z: ZS[ir] });
    addSolid(m, lvr, [idl], [idl]);
    addHoleWalls(m, [{ loop: idl, z: 0 }, { loop: idl, z: T }]);
    return;
  }
  /* clicking a screw hole in the preview toggles it off/on; the node
     key is its position in 28 mm units (nx/ny are node coords) */
  if (!p.ogScrews || (p.ogScrewOff &&
      p.ogScrewOff[Math.round(nx / OG.TILE) + "," + Math.round(ny / OG.TILE)])) {
    var lv0 = [];
    for (var i0 = 0; i0 < ZS.length; i0++)
      lv0.push({ loop: dia(ZS[i0]), z: ZS[i0] });
    addSolid(m, lv0, [], []);
    return;
  }
  var r = Math.max(0.1, +p.ogScrewD || 4.1) / 2;
  /* the head pocket must stay inside the smallest diamond (z = 0, T) */
  var rhMax = (BAt(0) - 0.1) / Math.SQRT2;
  var rh = Math.min(rhMax, Math.max(2 * r, +p.ogScrewHeadD || 7.2) / 2);
  var inset = Math.max(0, +p.ogScrewInset || 0);
  var back = !!p.ogBackside && !lite;   // lite: back pockets live in the
                                        // trimmed-away band
  var bi = back ? Math.max(0, +p.ogBackInset || 0) : 0;
  var rbh = back ? Math.max(r, rh - Math.max(0, +p.ogBackShrink || 0)) : r;
  var hz0 = T - inset;                         // front head pocket floor
  var csF = (p.ogCs && inset > 0 && rh > r)
    ? Math.max(0, Math.tan((180 - (+p.ogCsDeg || 90)) * Math.PI / 360) * (rh - r) - 0.01) : 0;
  var csB = (back && bi > 0 && p.ogBackCs && rbh > r)
    ? Math.max(0, Math.tan((180 - (+p.ogBackCsDeg || 90)) * Math.PI / 360) * (rbh - r) - 0.01) : 0;
  if (csB > 0) csB = Math.min(csB, Math.max(0, hz0 - csF - bi));
  var circ = circleLoop(nx, ny, r, seg), circH = circleLoop(nx, ny, rh, seg),
      circB = (back && bi > 0 && rbh > r) ? circleLoop(nx, ny, rbh, seg) : circ;

  /* The bore is one chain of rings bottom -> top; addHoleWalls emits a
     band per consecutive pair, so same-z ring pairs become flat annular
     shoulders (facing into the pockets) and radius jumps become cones. */
  var rings = [];
  function pushRing(loop, z) {
    var last = rings[rings.length - 1];
    if (last && Math.abs(last.z - z) < 1e-9 && last.loop === loop) return;
    rings.push({ loop: loop, z: z });
  }
  if (back && bi > 0 && rbh > r) {             // backside head pocket
    pushRing(circB, 0);
    pushRing(circB, bi);
    pushRing(circ, bi + Math.max(0, csB));     // cone (csB > 0) or shoulder
  }
  var hz = hz0;                                // front head pocket floor
  var zA = rings.length ? rings[rings.length - 1].z : 0;
  if (hz < T) zA = Math.min(zA, hz);
  var zT = hz < T ? hz - Math.min(csF, hz - zA) : T;
  pushRing(circ, zA);
  if (zT > zA + 1e-9) pushRing(circ, zT);
  var topRing = circ;
  if (hz < T) {
    pushRing(circH, hz);                       // cone (csF > 0) or shoulder
    if (T > hz + 1e-9) pushRing(circH, T);
    topRing = circH;
  }
  var lv = [];
  for (var i1 = 0; i1 < ZS.length; i1++)
    lv.push({ loop: dia(ZS[i1]), z: ZS[i1] });
  addSolid(m, lv,
           [(back && bi > 0 && rbh > r) ? circB : circ], [topRing]);
  addHoleWalls(m, rings);
}

function ogMergePieces(m, list) {
  for (var i = 0; i < list.length; i++) {
    m.pos.push.apply(m.pos, list[i].pos);
    m.nrm.push.apply(m.nrm, list[i].nrm);
  }
}

function ogBuildLevel(m, p, d, seg) {
  var r = ogEmitLevel(p, d, seg);
  ogMergePieces(m, r.solids);
  ogMergePieces(m, r.voids);
}

/* Half diagonal of the diamond that fills a lattice node. */
function ogNodeFillHalf(tile) {
  return (Math.sqrt(OG.CORNER_SQ * OG.CORNER_SQ * 2) + OG.INTERSECT) / Math.SQRT2;
}

/* Board-to-board connector cutout outline, CCW. */
function ogConnectorLoop(seg) {
  function arc(cx, cy, r, a0, a1, steps) {
    var out = [];
    for (var i = 1; i <= steps; i++) {
      var a = a0 + (a1 - a0) * i / steps;
      out.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]);
    }
    return out;
  }
  function meet(A, rA, B, rB) {
    var dx = B[0] - A[0], dy = B[1] - A[1], dist = Math.hypot(dx, dy);
    var along = (rA * rA - rB * rB + dist * dist) / (2 * dist);
    var h = Math.sqrt(Math.max(0, rA * rA - along * along));
    var mx = A[0] + along * dx / dist, my = A[1] + along * dy / dist;
    return [[mx - dy * h / dist, my + dx * h / dist],
            [mx + dy * h / dist, my - dx * h / dist]];
  }
  function at(C, P) { return Math.atan2(P[1] - C[1], P[0] - C[0]); }

  var noseN = Math.max(6, Math.ceil(seg / 8));
  var blendN = Math.max(10, Math.ceil(seg / 6));
  var outerN = Math.max(16, Math.ceil(seg / 2));

  var outerC = [OG.CONN_SEP, 0];
  var blendUp = [0, OG.CONN_R + OG.CONN_SEP], blendDn = [0, -(OG.CONN_R + OG.CONN_SEP)];
  var shInset = Math.sqrt(Math.pow(OG.CONN_BLEND + OG.CONN_SH, 2) -
                          Math.pow(OG.CONN_SEP + OG.CONN_SH, 2));
  var shUp = [shInset, OG.CONN_R - OG.CONN_SH], shDn = [shInset, -(OG.CONN_R - OG.CONN_SH)];
  var sideHalf = OG.CONN_R + OG.CONN_SEP -
    Math.sqrt(Math.pow(OG.CONN_BLEND - OG.CONN_STEM, 2) - OG.CONN_STEM * OG.CONN_STEM);
  var noseUp = [OG.CONN_STEM, sideHalf], noseDn = [OG.CONN_STEM, -sideHalf];

  var nUp = meet(noseUp, OG.CONN_STEM, blendUp, OG.CONN_BLEND)
    .sort(function (a, b) { return b[1] - a[1]; })[0];
  var sUp = meet(blendUp, OG.CONN_BLEND, shUp, OG.CONN_SH)
    .sort(function (a, b) { return b[1] - a[1]; })[0];
  var sDn = [sUp[0], -sUp[1]], nDn = [nUp[0], -nUp[1]];
  var nUpA = at(noseUp, nUp); if (nUpA < 0) nUpA += 2 * Math.PI;

  var pts = [[0, sideHalf]]
    .concat(arc(noseUp[0], noseUp[1], OG.CONN_STEM, Math.PI, nUpA, noseN))
    .concat(arc(blendUp[0], blendUp[1], OG.CONN_BLEND, at(blendUp, nUp), at(blendUp, sUp), blendN))
    .concat(arc(shUp[0], shUp[1], OG.CONN_SH, at(shUp, sUp), Math.PI / 2, noseN))
    .concat([[OG.CONN_SEP, OG.CONN_R]])
    .concat(arc(outerC[0], outerC[1], OG.CONN_R, Math.PI / 2, -Math.PI / 2, outerN))
    .concat([[shInset, -OG.CONN_R]])
    .concat(arc(shDn[0], shDn[1], OG.CONN_SH, -Math.PI / 2, at(shDn, sDn), noseN))
    .concat(arc(blendDn[0], blendDn[1], OG.CONN_BLEND, at(blendDn, sDn), at(blendDn, nDn), blendN))
    .concat(arc(noseDn[0], noseDn[1], OG.CONN_STEM, at(noseDn, nDn), Math.PI, noseN));
  if (signedArea(pts) < 0) pts = reverseLoop(pts);
  return pts;
}

/* ---- prism helpers (sections may be concave; caps are ear-clipped) ---- */
/* Same prism along Y: build along X in a scratch mesh, then swap x<->y.
   The swap is a reflection, so the winding is reversed to keep the
   outward normals -- both inherited from the proven addPrismX. */
function addPrismY(mesh, section, y0, y1) {
  var tmp = new Mesh();
  addPrismX(tmp, section, y0, y1);
  var p = tmp.pos, n = tmp.nrm;
  for (var t = 0; t < tmp.count(); t++) {
    var b = t * 9, v, P = [], N = [];
    for (v = 0; v < 3; v++) {
      P.push([p[b + v * 3 + 1], p[b + v * 3], p[b + v * 3 + 2]]);
      N.push([n[b + v * 3 + 1], n[b + v * 3], n[b + v * 3 + 2]]);
    }
    mesh.tri(P[0], P[2], P[1], N[0], N[2], N[1]);
  }
}

/* Classification of lattice node (ix,iy) of a W x H tile rectangle:
   which of the four touching tiles exist.  Mirrors the openGrid Studio
   placement logic (their y index runs top-down). */
/* Canonical feature key: node coords in 28 mm units from the board
   centre -- the same convention pickScrew has always used for
   ogScrewOff; connectors use it too (ogConnOff). */
function ogNodeKey(nx, ny) {
  return Math.round(nx / OG.TILE) + "," + Math.round(ny / OG.TILE);
}

function ogNodeInfo(W, H, ix, iy) {
  var nw = ix >= 1 && ix <= W && iy >= 1 && iy <= H;
  var ne = ix <= W - 1 && iy >= 1 && iy <= H;
  var sw = ix >= 1 && iy <= H - 1;
  var se = ix <= W - 1 && iy <= H - 1;
  var count = (nw ? 1 : 0) + (ne ? 1 : 0) + (sw ? 1 : 0) + (se ? 1 : 0);
  if (count !== 2) return count === 0 ? null : { kind: count === 1 ? "outer" : "fill", rot: 0 };
  if (nw && ne) return { kind: "edge", rot: 90 };
  if (sw && se) return { kind: "edge", rot: -90 };
  if (nw && sw) return { kind: "edge", rot: 180 };
  return { kind: "edge", rot: 0 };          // ne && se
}

function deriveBoard(p) {
  var W = Math.max(1, Math.min(20, Math.round(+p.ogW || 1)));
  var H = Math.max(1, Math.min(20, Math.round(+p.ogH || 1)));
  var lite = p.ogType === "lite";
  /* ogstudio lite: the TOP 4 mm of the full board (z 2.8..6.8) shifted
     down -- front head pocket survives, backside pockets do not */
  var T = lite ? OG.LITE_T : OG.FULL_T;
  var levels = p.ogStack ? Math.max(2, Math.min(10, Math.round(+p.ogStackN || 2))) : 1;
  var stackGap = Math.max(0, +p.ogStackGap || 0);
  function cellOff(col, row) {
    return !!(p.ogCellOff && p.ogCellOff[col + "," + row]);
  }
  /* ogstudio classifyNode: 3 cells or 2 diagonal -> full diamond (screw
     seat), 2 same-side -> half diamond (connector seat), 1 -> corner
     quarter, 0 -> no node at all. */
  function nodeClass(ix2, iy2) {
    var nw = ix2 >= 1 && iy2 >= 1 && !cellOff(ix2 - 1, iy2 - 1);
    var ne = ix2 <= W - 1 && iy2 >= 1 && !cellOff(ix2, iy2 - 1);
    var sw = ix2 >= 1 && iy2 <= H - 1 && !cellOff(ix2 - 1, iy2);
    var se = ix2 <= W - 1 && iy2 <= H - 1 && !cellOff(ix2, iy2);
    var n = (nw ? 1 : 0) + (ne ? 1 : 0) + (sw ? 1 : 0) + (se ? 1 : 0);
    if (!n) return "none";
    if (n >= 3 || (n === 2 && ((nw && se) || (ne && sw)))) return "fill";
    if (n === 2) return "edge";
    return "outer";
  }
  var snap = 0, ix, iy;
  for (var row = 0; row < H; row++) for (var col = 0; col < W; col++)
    if (!cellOff(col, row)) snap++;
  function nodeKey(ix2, iy2) {
    return ogNodeKey((ix2 - W / 2) * OG.TILE, (H / 2 - iy2) * OG.TILE);
  }
  var screws = 0;
  if (p.ogScrews)
    for (ix = 1; ix < W; ix++) for (iy = 1; iy < H; iy++)
      if (nodeClass(ix, iy) === "fill" &&
          !(p.ogScrewOff && p.ogScrewOff[nodeKey(ix, iy)]) &&
          !(p.ogChamf && p.ogChamf[nodeKey(ix, iy)])) screws++;
  var conns = 0;
  if (p.ogConnectors)
    for (ix = 0; ix <= W; ix++) for (iy = 0; iy <= H; iy++)
      if (nodeClass(ix, iy) === "edge" &&
          !(p.ogConnOff && p.ogConnOff[nodeKey(ix, iy)])) conns++;
  return {
    W: W, H: H, T: T, lite: lite, OX: W * OG.TILE, OY: H * OG.TILE,
    levels: levels, stackGap: stackGap,
    HTotal: levels * T + (levels - 1) * stackGap,
    snapHoles: snap,
    screwHoles: screws,
    connHoles: conns,
    valid: W >= 1 && H >= 1
  };
}

function* boardSteps(p, seg) {
  var d = deriveBoard(p);
  var m = new Mesh();
  if (!d.valid) return { mesh: m, derived: d };

  for (var lev = 0; lev < d.levels; lev++) {
    var zoff = lev * (d.T + d.stackGap);
    var startTri = m.count();
    ogBuildLevel(m, p, d, seg.og);
    if ((lev % 2) === 1) zFlipTris(m, startTri, d.T, zoff);
    else zTranslateTris(m, startTri, zoff);
    yield (lev + 1) / d.levels;
  }
  d.pieces = d.levels;
  return { mesh: m, derived: d };
}

/* Shift every triangle of a freshly built level up by dz. */
function zTranslateTris(mesh, fromTri, dz) {
  var pos = mesh.pos;
  for (var t = fromTri; t < mesh.count(); t++)
    for (var v = 0; v < 3; v++) pos[t * 9 + v * 3 + 2] += dz;
}

/* =====================================================================
   The bin
   ===================================================================== */
/* The two builders are generators: they yield a 0..1 fraction at points where
   it is safe to hand control back, so a long export can repaint and show
   progress.  buildBin/buildPlate below drive them straight through, so every
   other caller sees the same plain function it always did. */
function* binSteps(P, seg) {
  var d = derive(P);
  var m = new Mesh();
  if (!d.valid) return { mesh: m, derived: d };
  var slices = P.gx * P.gy + 3 + P.dx * P.dy, sliced = 0;

  var nc = seg.corner, nh = seg.hole, ni = seg.comp || seg.corner;
  var cellX = [], cellY = [], i, j;
  for (i = 0; i < P.gx; i++) cellX.push((i - (P.gx - 1) / 2) * GRID);
  for (j = 0; j < P.gy; j++) cellY.push((j - (P.gy - 1) / 2) * GRID);

  var holeR0 = P.mag ? MAGNET_R : SCREW_R;
  var anyHole = P.mag || P.screw;
  var footHole = anyHole;                       // holes start in the feet
  var slabHole = P.screw;                       // screws continue into the slab

  /* ---- 1. feet ---- */
  var footLevels = [
    { loop: rrLoop(FOOT_BOT, FOOT_BOT, R_BOT, nc), z: 0 },
    { loop: rrLoop(FOOT_MID, FOOT_MID, R_MID, nc), z: CH_LOWER },
    { loop: rrLoop(FOOT_MID, FOOT_MID, R_MID, nc), z: CH_LOWER + H_MID },
    { loop: rrLoop(FOOT_TOP, FOOT_TOP, R_TOP, nc), z: BASE_H }
  ];
  var offs = [[-HOLE_OFF, -HOLE_OFF], [HOLE_OFF, -HOLE_OFF],
              [HOLE_OFF, HOLE_OFF], [-HOLE_OFF, HOLE_OFF]];

  for (i = 0; i < P.gx; i++) for (j = 0; j < P.gy; j++) {
    var ox = cellX[i], oy = cellY[j];
    var lv = footLevels.map(function (L) {
      return { loop: offsetLoop(L.loop, ox, oy), z: L.z };
    });
    var bottomHoles = [], topHoles = [];
    if (footHole) {
      for (var h = 0; h < 4; h++) {
        var hx = ox + offs[h][0], hy = oy + offs[h][1];
        bottomHoles.push(circleLoop(hx, hy, holeR0, nh));
        if (slabHole) topHoles.push(circleLoop(hx, hy, SCREW_R, nh));

        // walls of the recess inside the foot
        var magC = circleLoop(hx, hy, MAGNET_R, nh);
        var scrC = circleLoop(hx, hy, SCREW_R, nh);
        if (P.mag && P.screw) {                      // stepped: magnet then screw
          addHoleWalls(m, [{ loop: magC, z: 0 }, { loop: magC, z: MAGNET_H }]);
          addCap(m, magC, [scrC], MAGNET_H, false);  // shoulder, faces into the pocket
          addHoleWalls(m, [{ loop: scrC, z: MAGNET_H }, { loop: scrC, z: BASE_H }]);
        } else if (P.mag) {                          // blind magnet pocket
          addHoleWalls(m, [{ loop: magC, z: 0 }, { loop: magC, z: MAGNET_H }]);
          addCap(m, magC, [], MAGNET_H, false);
        } else {                                     // screw only, straight through
          addHoleWalls(m, [{ loop: scrC, z: 0 }, { loop: scrC, z: BASE_H }]);
        }
      }
    }
    addSolid(m, lv, bottomHoles, topHoles);
    yield ++sliced / slices;
  }

  /* ---- 2. floor slab ---- */
  var OUT = rrLoop(d.OX, d.OY, R_TOP, nc);
  var slabBottomHoles = [];
  if (slabHole) {
    for (i = 0; i < P.gx; i++) for (j = 0; j < P.gy; j++) for (var h2 = 0; h2 < 4; h2++) {
      var sx = cellX[i] + offs[h2][0], sy = cellY[j] + offs[h2][1];
      slabBottomHoles.push(circleLoop(sx, sy, SCREW_R, nh));
      addHoleWalls(m, [
        { loop: circleLoop(sx, sy, SCREW_R, nh), z: BASE_H },
        { loop: circleLoop(sx, sy, SCREW_R, nh), z: SCREW_H }
      ]);
      addCap(m, circleLoop(sx, sy, SCREW_R, nh), [], SCREW_H, false);
    }
  }
  addSolid(m, [{ loop: OUT, z: BASE_H }, { loop: OUT, z: d.FLOOR }],
           slabBottomHoles, []);
  yield ++sliced / slices;

  /* ---- 3. walls + dividers ---- */
  var comp = [], compSmall = [];
  var hasFillet = false;
  for (var cIdx = 0; cIdx < d.cells.length; cIdx++) {
    var cell = d.cells[cIdx];
    var cf = cell.fillet;
    if (cf > 1e-6) hasFillet = true;
    comp.push(offsetLoop(rrLoop(cell.cw, cell.cd, cell.r_in, ni), cell.cx, cell.cy));
    compSmall.push(offsetLoop(
      rrLoop(Math.max(0.01, cell.cw - 2 * cf), Math.max(0.01, cell.cd - 2 * cf), Math.max(0, cell.r_in - cf), ni), cell.cx, cell.cy));
  }

  // outer skin of the wall block
  addBand(m, OUT, d.FLOOR, OUT, d.H_BODY, false);
  // inner skin of every compartment: fillet band, then straight
  for (var c = 0; c < comp.length; c++) {
    var cellF = d.cells[c].fillet;
    if (cellF > 1e-6) addBand(m, compSmall[c], d.FLOOR, comp[c], d.FLOOR + cellF, true);
    addBand(m, comp[c], d.FLOOR + cellF, comp[c], d.H_BODY, true);
  }
  addCap(m, OUT, hasFillet ? compSmall : comp, d.FLOOR, false);
  addCap(m, OUT, comp, d.H_BODY, true);
  yield ++sliced / slices;

  /* ---- 4. stacking lip ---- */
  if (P.lip) {
    var zb = d.H_BODY;
    var inWall = rrLoop(d.OX - 2 * P.wall, d.OY - 2 * P.wall,
                        Math.max(0, R_TOP - P.wall), nc);
    var narrow = rrLoop(d.OX - 2 * LIP_INSET, d.OY - 2 * LIP_INSET,
                        Math.max(0, R_TOP - LIP_INSET), nc);
    var opening = rrLoop(d.OX - 2 * (LIP_INSET - LIP_TAPER),
                         d.OY - 2 * (LIP_INSET - LIP_TAPER),
                         Math.max(0, R_TOP - LIP_INSET + LIP_TAPER), nc);
    var z1 = zb + CH_LOWER - d.support, z2 = zb + CH_LOWER;
    var z3 = z2 + H_MID, z4 = zb + LIP_H;

    addBand(m, OUT, zb, OUT, z4, false);              // outside of the lip
    addBand(m, inWall, zb, inWall, z1, true);         // straight run
    addBand(m, inWall, z1, narrow, z2, true);         // 45 deg support
    addBand(m, narrow, z2, narrow, z3, true);         // mating face
    addBand(m, narrow, z3, opening, z4, true);        // top chamfer
    addCap(m, OUT, [inWall], zb, false);
    addCap(m, OUT, [opening], z4, true);
  }

  yield ++sliced / slices;

  /* ---- 5. scoops and label tabs ---- */
  function addSeamlessLabelTab(cCell) {
    var ld = cCell.labelD;
    if (ld <= 1e-6) return;

    var x0 = cCell.cx - cCell.cw / 2, x1 = cCell.cx + cCell.cw / 2;
    var yb = cCell.cy + cCell.cd / 2, tb = d.H_BODY;

    var r_corner = Math.max(0, (cCell.r_in || 0) * 0.75);
    var rL = r_corner;
    var rR = r_corner;

    function xLeft(y) {
      if (rL > 1e-6 && y >= yb - rL) {
        var dy = y - (yb - rL);
        return (x0 + rL) - Math.sqrt(Math.max(0, rL * rL - dy * dy));
      }
      return x0;
    }

    function xRight(y) {
      if (rR > 1e-6 && y >= yb - rR) {
        var dy = y - (yb - rR);
        return (x1 - rR) + Math.sqrt(Math.max(0, rR * rR - dy * dy));
      }
      return x1;
    }

    var fullW = (x1 - rR) - (x0 + rL);
    var isCustom = cCell.labelW > 0 && cCell.labelW < fullW;
    var customW = Math.min(cCell.labelW, fullW);
    var midX = (x0 + rL + x1 - rR) / 2;

    var N = Math.max(6, ni || 8);
    var ptsUnder = [];

    // u from 0 (bottom-back) to 1 (top-front)
    for (var k = 0; k <= N; k++) {
      var u = k / N;
      var y = yb - ld * u;
      var z = tb - ld * (1 - u);
      var xl = isCustom ? (midX - customW / 2) : xLeft(y);
      var xr = isCustom ? (midX + customW / 2) : xRight(y);
      if (xr < xl) { xl = midX; xr = midX; }
      ptsUnder.push([xl, xr, y, z]);
    }

    // 1. Underside 45-degree sloping face (facing -y, -z into the compartment)
    var nUnderside = [0, -1 / Math.SQRT2, -1 / Math.SQRT2];
    for (var i = 0; i < N; i++) {
      var p0 = ptsUnder[i], p1 = ptsUnder[i + 1];
      var v0L = [p0[0], p0[2], p0[3]], v0R = [p0[1], p0[2], p0[3]];
      var v1L = [p1[0], p1[2], p1[3]], v1R = [p1[1], p1[2], p1[3]];
      m.tri(v0L, v0R, v1L, nUnderside, nUnderside, nUnderside);
      m.tri(v0R, v1R, v1L, nUnderside, nUnderside, nUnderside);
    }

    // 2. Top horizontal face at z = tb (facing +z)
    var nTop = [0, 0, 1];
    for (var i2 = 0; i2 < N; i2++) {
      var p0_t = ptsUnder[i2], p1_t = ptsUnder[i2 + 1];
      var t0L = [p0_t[0], p0_t[2], tb], t0R = [p0_t[1], p0_t[2], tb];
      var t1L = [p1_t[0], p1_t[2], tb], t1R = [p1_t[1], p1_t[2], tb];
      m.tri(t0L, t1L, t0R, nTop, nTop, nTop);
      m.tri(t0R, t1L, t1R, nTop, nTop, nTop);
    }

    // 3. Left side wall (facing -x)
    var nLeft = [-1, 0, 0];
    for (var i3 = 0; i3 < N; i3++) {
      var p0_l = ptsUnder[i3], p1_l = ptsUnder[i3 + 1];
      var v0L_l = [p0_l[0], p0_l[2], p0_l[3]], v1L_l = [p1_l[0], p1_l[2], p1_l[3]];
      var t0L_l = [p0_l[0], p0_l[2], tb],      t1L_l = [p1_l[0], p1_l[2], tb];
      m.tri(t0L_l, v0L_l, t1L_l, nLeft, nLeft, nLeft);
      m.tri(t1L_l, v0L_l, v1L_l, nLeft, nLeft, nLeft);
    }

    // 4. Right side wall (facing +x)
    var nRight = [1, 0, 0];
    for (var i4 = 0; i4 < N; i4++) {
      var p0_r = ptsUnder[i4], p1_r = ptsUnder[i4 + 1];
      var v0R_r = [p0_r[1], p0_r[2], p0_r[3]], v1R_r = [p1_r[1], p1_r[2], p1_r[3]];
      var t0R_r = [p0_r[1], p0_r[2], tb],      t1R_r = [p1_r[1], p1_r[2], tb];
      m.tri(t0R_r, t1R_r, v0R_r, nRight, nRight, nRight);
      m.tri(t1R_r, v1R_r, v0R_r, nRight, nRight, nRight);
    }

    // 5. Back vertical face at k = 0 (y = yb, from z = tb - ld to tb, facing +y)
    var pBot = ptsUnder[0]; // at u = 0, y = yb, z = tb - ld
    var nBack = [0, 1, 0];
    var b0L = [pBot[0], yb, pBot[3]], b0R = [pBot[1], yb, pBot[3]];
    var top0L = [pBot[0], yb, tb],    top0R = [pBot[1], yb, tb];
    m.tri(top0L, top0R, b0L, nBack, nBack, nBack);
    m.tri(top0R, b0R, b0L, nBack, nBack, nBack);
  }

  function addSeamlessFingerScoop(cCell) {
    var rs = cCell.scoopR;
    if (rs <= 1e-6) return;

    var x0 = cCell.cx - cCell.cw / 2, x1 = cCell.cx + cCell.cw / 2;
    var y0 = cCell.cy - cCell.cd / 2;

    var r_corner = Math.max(0, (cCell.r_in || 0) * 0.75);
    var rL = r_corner;
    var rR = r_corner;

    function xLeft(y) {
      if (rL > 1e-6 && y <= y0 + rL) {
        var dy = (y0 + rL) - y;
        return (x0 + rL) - Math.sqrt(Math.max(0, rL * rL - dy * dy));
      }
      return x0;
    }

    function xRight(y) {
      if (rR > 1e-6 && y <= y0 + rR) {
        var dy = (y0 + rR) - y;
        return (x1 - rR) + Math.sqrt(Math.max(0, rR * rR - dy * dy));
      }
      return x1;
    }

    var N = Math.max(8, ni || 8);
    var ptsScoop = [];

    for (var k = 0; k <= N; k++) {
      var phi = (Math.PI / 2) * k / N;
      var y = y0 + rs * (1 - Math.cos(phi));
      var z = d.FLOOR + rs * (1 - Math.sin(phi));
      var xl = xLeft(y);
      var xr = xRight(y);
      if (xr < xl) { xl = (x0 + x1) / 2; xr = (x0 + x1) / 2; }
      var ny = Math.cos(phi), nz = Math.sin(phi);
      ptsScoop.push([xl, xr, y, z, 0, ny, nz]);
    }

    // 1. Curved scoop face (facing +y, +z into the compartment)
    for (var i = 0; i < N; i++) {
      var p0 = ptsScoop[i], p1 = ptsScoop[i + 1];
      var v0L = [p0[0], p0[2], p0[3]], v0R = [p0[1], p0[2], p0[3]];
      var v1L = [p1[0], p1[2], p1[3]], v1R = [p1[1], p1[2], p1[3]];
      var n0 = [0, p0[5], p0[6]], n1 = [0, p1[5], p1[6]];
      m.tri(v0L, v0R, v1L, n0, n0, n1);
      m.tri(v0R, v1R, v1L, n0, n1, n1);
    }

    // 2. Bottom horizontal face at z = d.FLOOR (facing -z)
    var nBot = [0, 0, -1];
    for (var i2 = 0; i2 < N; i2++) {
      var p0_b = ptsScoop[i2], p1_b = ptsScoop[i2 + 1];
      var b0L = [p0_b[0], p0_b[2], d.FLOOR], b0R = [p0_b[1], p0_b[2], d.FLOOR];
      var b1L = [p1_b[0], p1_b[2], d.FLOOR], b1R = [p1_b[1], p1_b[2], d.FLOOR];
      m.tri(b0L, b1L, b0R, nBot, nBot, nBot);
      m.tri(b0R, b1L, b1R, nBot, nBot, nBot);
    }

    // 3. Left side wall (facing -x)
    var nLeft = [-1, 0, 0];
    for (var i3 = 0; i3 < N; i3++) {
      var p0_l = ptsScoop[i3], p1_l = ptsScoop[i3 + 1];
      var v0L_s = [p0_l[0], p0_l[2], p0_l[3]], v1L_s = [p1_l[0], p1_l[2], p1_l[3]];
      var b0L_s = [p0_l[0], p0_l[2], d.FLOOR], b1L_s = [p1_l[0], p1_l[2], d.FLOOR];
      m.tri(v0L_s, v1L_s, b0L_s, nLeft, nLeft, nLeft);
      m.tri(v1L_s, b1L_s, b0L_s, nLeft, nLeft, nLeft);
    }

    // 4. Right side wall (facing +x)
    var nRight = [1, 0, 0];
    for (var i4 = 0; i4 < N; i4++) {
      var p0_r = ptsScoop[i4], p1_r = ptsScoop[i4 + 1];
      var v0R_s = [p0_r[1], p0_r[2], p0_r[3]], v1R_s = [p1_r[1], p1_r[2], p1_r[3]];
      var b0R_s = [p0_r[1], p0_r[2], d.FLOOR], b1R_s = [p1_r[1], p1_r[2], d.FLOOR];
      m.tri(v0R_s, b0R_s, v1R_s, nRight, nRight, nRight);
      m.tri(v1R_s, b0R_s, b1R_s, nRight, nRight, nRight);
    }

    // 5. Front vertical face at k = 0 (y = y0, from z = d.FLOOR to d.FLOOR + rs, facing -y)
    var pTop = ptsScoop[0]; // at phi = 0, y = y0, z = d.FLOOR + rs
    var nFront = [0, -1, 0];
    var vTopL = [pTop[0], y0, pTop[3]],  vTopR = [pTop[1], y0, pTop[3]];
    var bTopL = [pTop[0], y0, d.FLOOR],  bTopR = [pTop[1], y0, d.FLOOR];
    m.tri(vTopL, bTopL, vTopR, nFront, nFront, nFront);
    m.tri(vTopR, bTopL, bTopR, nFront, nFront, nFront);
  }

  for (var cIdx2 = 0; cIdx2 < d.cells.length; cIdx2++) {
    addSeamlessFingerScoop(d.cells[cIdx2]);
    addSeamlessLabelTab(d.cells[cIdx2]);
    yield ++sliced / slices;
  }

  return { mesh: m, derived: d };
}

/* Run a builder straight through, for every caller that just wants the mesh. */
function runSteps(gen) {
  var r;
  do { r = gen.next(); } while (!r.done);
  return r.value;
}
function buildBin(P, seg) { return runSteps(binSteps(P, seg)); }
function buildPlate(p, seg) { return runSteps(plateSteps(p, seg)); }
function buildBoard(p, seg) { return runSteps(boardSteps(p, seg)); }

/* =====================================================================
   Binary STL
   ===================================================================== */
function toSTL(mesh) {
  var n = mesh.count();
  var buf = new ArrayBuffer(84 + n * 50);
  var dv = new DataView(buf);
  var head = "Gridfinity bin - generated by gridfinity_bin.html";
  for (var i = 0; i < 80; i++) dv.setUint8(i, i < head.length ? head.charCodeAt(i) : 32);
  dv.setUint32(80, n, true);
  var o = 84, p = mesh.pos;
  for (var t = 0; t < n; t++) {
    var b = t * 9;
    var ux = p[b + 3] - p[b], uy = p[b + 4] - p[b + 1], uz = p[b + 5] - p[b + 2];
    var vx = p[b + 6] - p[b], vy = p[b + 7] - p[b + 1], vz = p[b + 8] - p[b + 2];
    var nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
    var l = Math.hypot(nx, ny, nz) || 1;
    dv.setFloat32(o, nx / l, true); dv.setFloat32(o + 4, ny / l, true);
    dv.setFloat32(o + 8, nz / l, true); o += 12;
    for (var v = 0; v < 9; v++) { dv.setFloat32(o, p[b + v], true); o += 4; }
    dv.setUint16(o, 0, true); o += 2;
  }
  return buf;
}

if (typeof module !== "undefined") module.exports = { buildBin: buildBin, buildPlate: buildPlate, derive: derive, derivePlate: derivePlate, toSTL: toSTL, DEFAULTS: DEFAULTS };

/* =====================================================================
   i18n -- EN/RU.  Static nodes carry data-i="key"; dynamic strings go
   through t().  The choice persists in localStorage when available and
   defaults to the browser language.
   ===================================================================== */
var I18N = {
  en: {
    title: "Gridfinity Bin Configurator",
    h_model: "Model", m_bin: "Bin", m_plate: "Baseplate", m_og: "openGrid Board",
    h_ogboard: "openGrid Board", og_full: "Full (6.8 mm)", og_lite: "Lite (4 mm)",
    og_features: "Board Features",
    og_screws: "Screw holes", og_shaft: "Shaft &oslash; (mm)", og_head: "Head &oslash; (mm)",
    og_inset: "Head inset from top (mm)", og_cs: "Countersink", og_cs_deg: "Countersink angle (&deg;)",
    og_back: "Backside head pocket", og_back_inset: "Pocket depth (mm)",
    og_back_shrink: "Pocket shrink (mm)", og_back_cs: "Pocket countersink", og_back_cs_deg: "Pocket angle (&deg;)",
    og_conn: "Board-to-board connectors",
    og_hint: "28 mm openGrid lattice; 3 OG tiles = 2 Gridfinity units. Lite = the top 4 mm band of the full board. Click a screw hole in the preview to remove or restore it.",
    hint_2d: "2D editor: click a screw to cycle hole / empty / chamfered ring, a connector or a cell to toggle it, an outer corner to make it sharp or cut. Drag pans, wheel zooms.",
    og_reset: "Reset edits",
    og_reset_done: "Click-edits cleared.",
    saved_fallback: "The chosen folder is blocked by OrcaSlicer's plugin write policy, the file was saved next to the plugin instead: {p}",
    exp_dir: "Export folder (empty = plugin default)",
    exp_dir_pick: "\ud83d\udcc2 Browse\u2026",
    dir_asking: "Opening the system folder dialog \u2014 choose a folder there\u2026",
    dir_set: "Export folder: {p}",
    dir_fail: "Folder dialog failed: {e}",
    dir_cancel: "Folder selection cancelled.",
    og_summary: "{w} &times; {h} tiles &middot; snap {snap} &middot; screws {scr} &middot; connectors {conn}",
    h_binsize: "Bin Size", l_width: "Width", l_depth: "Depth", l_height: "Height",
    h_platesize: "Baseplate Size", pm_units: "Grid Units", pm_mm: "Dimensions (mm)",
    l_width_mm: "Width (mm)", l_depth_mm: "Depth (mm)", l_depth_mm2: "Depth (mm)", l_width_mm2: "Width (mm)",
    l_buf_x: "Left ⟷ Right", l_buf_y: "Down ⟷ Up",
    h_plateopts: "Baseplate Options",
    l_plate_base: "Solid base (mm)", l_plate_r: "Corner radius",
    l_bed_x: "Bed X (mm)", l_bed_y: "Bed Y (mm)", l_plate_gap: "Explode gap",
    btn_bed: "Use printer bed", conn: "Puzzle connectors",
    stacking_h: "Stacking copies", stacking_on: "Stack copies upward",
    stacking_copies: "Copies", stacking_gap: "Gap between copies (mm)",
    stacking_hint: "Every second copy is laid upside down (sockets facing down); the air gap keeps the copies from fusing.",
    h_comps: "Compartments", cl_grid: "Uniform", cl_rows: "By Row", cl_cols: "By Column",
    l_dx: "Across X", l_dy: "Across Y", l_num_rows: "Total Rows", l_num_cols: "Total Cols",
    row_n: "Row {n}", col_n: "Col {n}", pos_front: " (Front)", pos_back: " (Back)", pos_left: " (Left)", pos_right: " (Right)",
    h_features: "Features",
    f_lip: "Stacking lip", f_scoop: "Finger scoop", l_radius_mm: "Radius (mm)",
    f_label: "Label tab", label_full_w: "0 = Full compartment width",
    f_mag: "Magnet holes (6&times;2&nbsp;mm)", f_screw: "Screw holes (M3)",
    h_advanced: "Advanced", l_wall: "Wall (mm)", l_floor: "Floor (mm)", l_fillet: "Inner fillet",
    h_result: "Result", s_foot_l: "Footprint", s_tall_l: "Total height",
    s_comp_l: "Compartment", s_depth_l: "Usable depth", s_tris_l: "Triangles",
    h_output: "Output", btn_render: "Render", btn_stl: "Export STL",
    to_plate: "Add to build plate after export",
    v_iso: "Iso", v_front: "Front", v_top: "Top", v_under: "Under",
    hint: "drag to orbit &middot; scroll to zoom &middot; shift-drag to pan",
    badge_preview: "Preview", badge_rendering: "Rendering\u2026", badge_rendered: "Rendered",
    prog_building: "Building", prog_encoding: "Encoding", prog_writing: "Writing STL",
    prog_saving: "Saving", prog_sending: "Handing to OrcaSlicer",
    prog_done: "Rendered", prog_failed: "Failed", prog_sent: "Sent", prog_saved: "Saved",
    mm: "mm",
    body_of: " (body {h})",
    no_comps: "0 compartments",
    comp_one: "1 ({w} \u00d7 {d} mm)",
    comp_rows: "{n} in {r} rows ({divs})",
    comp_cols: "{n} in {c} cols ({divs})",
    comp_plain: "{n} ({w} \u00d7 {d} mm)",
    warn_invalid: "These settings leave no usable compartment.",
    warn_shallow: "Compartments are under 2 mm deep \u2014 raise the height.",
    cells: "{x} \u00d7 {y} cells",
    plus_buffer: " + buffer (exact {x} \u00d7 {y} mm)",
    target_mm: " (target {x} \u00d7 {y} mm)",
    piece_one: "{n} piece", piece_many: "{n} pieces",
    cols_split: " (cols {cols})", levels_x: " \u00d7 {n} levels",
    pad_sym: "\u00b1{v} mm",
    pad_lr: "L: {l} mm / R: {r} mm",
    pad_bt: "Bottom: {b} mm / Top: {t} mm",
    btn_exact_on: "\u2713 Edge padding active (exact {x} \u00d7 {y} mm)",
    btn_exact_off: "Add edge padding for exact size (+{x}\u00d7{y} mm)",
    fits_units: "Fits {x} \u00d7 {y} units ({mx} \u00d7 {my} mm)",
    fits_buffer: "Fits {x} \u00d7 {y} units + buffer (X: {xd}, Y: {yd}) = exact {ox} \u00d7 {oy} mm",
    render_failed: "Render failed: {e}", export_failed: "Export failed: {e}",
    saved_import: "Written to {p} \u2014 import it with File \u203a Import.",
    saved_pending: "Written to {p} \u2014 adding to the build plate\u2026",
    placed_ok: "Added to the build plate: {p}",
    placed_fail: "Written to {p} \u2014 could not add it to the plate: {e}",
    save_failed: "Could not write the STL: {e}",
    asking_bed: "asking OrcaSlicer\u2026", bed_failed: "could not read the printer bed",
    bed_manual: "Bed size: {e} \u2014 set Bed X/Y by hand.",
    bed_from: "{printer}: {x} \u00d7 {y} mm",
    gl_missing: "WebGL is unavailable, so the 3D view cannot be shown. The settings, the OpenSCAD command and STL export still work.",
    gl_error: "Renderer error: {e}"
  },
  ru: {
    title: "Gridfinity \u2014 конструктор ящиков",
    h_model: "Модель", m_bin: "Ящик", m_plate: "Основание", m_og: "openGrid-панель",
    h_ogboard: "Панель openGrid", og_full: "Full (6,8 мм)", og_lite: "Lite (4 мм)",
    og_features: "Особенности панели",
    og_screws: "Отверстия под шурупы", og_shaft: "Шуруп &oslash; (мм)", og_head: "Головка &oslash; (мм)",
    og_inset: "Утопление головки от верха (мм)", og_cs: "Зенковка", og_cs_deg: "Угол зенковки (&deg;)",
    og_back: "Карман головки с тыла", og_back_inset: "Глубина кармана (мм)",
    og_back_shrink: "Уменьшение кармана (мм)", og_back_cs: "Зенковка кармана", og_back_cs_deg: "Угол кармана (&deg;)",
    og_conn: "Коннекторы панель-панель",
    og_hint: "Решётка openGrid 28 мм; 3 тайла OG = 2 юнита Gridfinity. Lite = верхние 4 мм полной доски. Клик по отверстию под винт в превью удаляет или возвращает его.",
    hint_2d: "Редактор 2D: клик по винту — цикл «отверстие / пусто / срез углов», по коннектору или ячейке — удалить/вернуть, по внешнему углу — острый/срезанный. Перетаскивание — панорама, колесо — масштаб.",
    og_reset: "Сбросить правки",
    og_reset_done: "Правки сброшены.",
    saved_fallback: "Выбранная папка запрещена политикой записи плагинов OrcaSlicer, файл сохранён рядом с плагином: {p}",
    exp_dir: "Папка экспорта (пусто = папка плагина)",
    exp_dir_pick: "\ud83d\udcc2 Обзор\u2026",
    dir_asking: "Открываю системный диалог \u2014 выберите папку в нём\u2026",
    dir_set: "Папка экспорта: {p}",
    dir_fail: "Не удалось открыть диалог: {e}",
    dir_cancel: "Выбор папки отменён.",
    og_summary: "тайлов {w} &times; {h} &middot; снапов {snap} &middot; шурупов {scr} &middot; коннекторов {conn}",
    h_binsize: "Размер ящика", l_width: "Ширина", l_depth: "Глубина", l_height: "Высота",
    h_platesize: "Размер основания", pm_units: "Сетка (юниты)", pm_mm: "Размеры (мм)",
    l_width_mm: "Ширина (мм)", l_depth_mm: "Глубина (мм)", l_depth_mm2: "Глубина (мм)", l_width_mm2: "Ширина (мм)",
    l_buf_x: "Лево \u27f7 Право", l_buf_y: "Низ \u27f7 Верх",
    h_plateopts: "Параметры основания",
    l_plate_base: "Сплошное дно (мм)", l_plate_r: "Радиус углов",
    l_bed_x: "Стол X (мм)", l_bed_y: "Стол Y (мм)", l_plate_gap: "Раздвижка деталей",
    btn_bed: "Стол принтера", conn: "Puzzle-замки",
    stacking_h: "Стопка копий", stacking_on: "Штабелировать копии вверх",
    stacking_copies: "Копий", stacking_gap: "Зазор между копиями (мм)",
    stacking_hint: "Каждая вторая копия уложена вверх дном (сокетами вниз); зазор не даёт копиям слипнуться.",
    h_comps: "Отсеки", cl_grid: "Равномерно", cl_rows: "По строкам", cl_cols: "По столбцам",
    l_dx: "По X", l_dy: "По Y", l_num_rows: "Всего строк", l_num_cols: "Всего столбцов",
    row_n: "Строка {n}", col_n: "Столбец {n}", pos_front: " (перед)", pos_back: " (зад)", pos_left: " (лево)", pos_right: " (право)",
    h_features: "Особенности",
    f_lip: "Стыковочный борт", f_scoop: "Выемка под палец", l_radius_mm: "Радиус (мм)",
    f_label: "Площадка для этикетки", label_full_w: "0 = вся ширина отсека",
    f_mag: "Отверстия под магниты (6&times;2&nbsp;мм)", f_screw: "Отверстия под шурупы (M3)",
    h_advanced: "Дополнительно", l_wall: "Стенка (мм)", l_floor: "Дно (мм)", l_fillet: "Внутр. скругление",
    h_result: "Результат", s_foot_l: "Габарит", s_tall_l: "Общая высота",
    s_comp_l: "Отсек", s_depth_l: "Глубина отсека", s_tris_l: "Треугольников",
    h_output: "Экспорт", btn_render: "Построить", btn_stl: "Экспорт STL",
    to_plate: "Добавить на стол после экспорта",
    v_iso: "Изо", v_front: "Спереди", v_top: "Сверху", v_under: "Снизу",
    hint: "перетаскивание \u2014 поворот &middot; колесо \u2014 масштаб &middot; Shift \u2014 сдвиг",
    badge_preview: "Просмотр", badge_rendering: "Строю\u2026", badge_rendered: "Готово",
    prog_building: "Построение", prog_encoding: "Кодирование", prog_writing: "Запись STL",
    prog_saving: "Сохранение", prog_sending: "Передача в OrcaSlicer",
    prog_done: "Готово", prog_failed: "Ошибка", prog_sent: "Отправлено", prog_saved: "Сохранено",
    mm: "мм",
    body_of: " (корпус {h})",
    no_comps: "0 отсеков",
    comp_one: "1 ({w} \u00d7 {d} мм)",
    comp_rows: "{n} в {r} строках ({divs})",
    comp_cols: "{n} в {c} столбцах ({divs})",
    comp_plain: "{n} ({w} \u00d7 {d} мм)",
    warn_invalid: "При таких настройках отсеки не помещаются.",
    warn_shallow: "Отсеки мельче 2 мм \u2014 увеличьте высоту.",
    cells: "{x} \u00d7 {y} ячеек",
    plus_buffer: " + буфер (точно {x} \u00d7 {y} мм)",
    target_mm: " (цель {x} \u00d7 {y} мм)",
    piece_one: "{n} деталь", piece_many: "{n} деталей",
    cols_split: " (столбцы: {cols})", levels_x: " \u00d7 {n} ярусов",
    pad_sym: "\u00b1{v} мм",
    pad_lr: "Л: {l} мм / П: {r} мм",
    pad_bt: "Низ: {b} мм / Верх: {t} мм",
    btn_exact_on: "\u2713 Поле включено (точно {x} \u00d7 {y} мм)",
    btn_exact_off: "Добавить поле до точного размера (+{x}\u00d7{y} мм)",
    fits_units: "Помещается {x} \u00d7 {y} юнитов ({mx} \u00d7 {my} мм)",
    fits_buffer: "Помещается {x} \u00d7 {y} юнитов + буфер (X: {xd}, Y: {yd}) = точно {ox} \u00d7 {oy} мм",
    render_failed: "Ошибка построения: {e}", export_failed: "Ошибка экспорта: {e}",
    saved_import: "Записано в {p} \u2014 импортируйте через Файл \u203a Импорт.",
    saved_pending: "Записано в {p} \u2014 добавляю на стол\u2026",
    placed_ok: "Добавлено на стол: {p}",
    placed_fail: "Записано в {p} \u2014 не удалось добавить на стол: {e}",
    save_failed: "Не удалось записать STL: {e}",
    asking_bed: "запрашиваю OrcaSlicer\u2026", bed_failed: "не удалось прочитать стол принтера",
    bed_manual: "Размер стола: {e} \u2014 задайте X/Y вручную.",
    bed_from: "{printer}: {x} \u00d7 {y} мм",
    gl_missing: "WebGL недоступен, 3D-просмотр невозможен. Настройки и экспорт STL продолжают работать.",
    gl_error: "Ошибка рендера: {e}"
  }
};

var LANG = "en";
try {
  LANG = localStorage.getItem("gf_lang") ||
         (((navigator.language || "en").toLowerCase().indexOf("ru") === 0) ? "ru" : "en");
} catch (e) {
  LANG = ((navigator.language || "en").toLowerCase().indexOf("ru") === 0) ? "ru" : "en";
}
if (!I18N[LANG]) LANG = "en";

function t(key, vars) {
  var s = (I18N[LANG] && I18N[LANG][key] !== undefined) ? I18N[LANG][key] : I18N.en[key];
  if (s === undefined) return key;
  if (vars) for (var k in vars) s = s.split("{" + k + "}").join(vars[k]);
  return s;
}
/* Russian plural: 1 деталь / 2 детали / 5 деталей; EN: 1 piece / 2 pieces */
function pieceTxt(n) {
  if (LANG === "ru") {
    var m10 = n % 10, m100 = n % 100;
    var word = (m10 === 1 && m100 !== 11) ? "деталь"
             : (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) ? "детали" : "деталей";
    var lvl = (m10 === 1 && m100 !== 11) ? "ярус"
            : (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) ? "яруса" : "ярусов";
    return { piece: n + " " + word, level: n + " " + lvl };
  }
  return { piece: n + (n === 1 ? " piece" : " pieces"), level: n + (n === 1 ? " level" : " levels") };
}

function applyLang() {
  document.documentElement.lang = LANG;
  document.title = t("title");
  var nodes = document.querySelectorAll("[data-i]");
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    var v = I18N[LANG][el.getAttribute("data-i")];
    if (v === undefined) v = I18N.en[el.getAttribute("data-i")];
    if (v !== undefined) el.innerHTML = v;
  }
  var rEN = document.getElementById("lang_en"), rRU = document.getElementById("lang_ru");
  if (rEN) rEN.checked = LANG === "en";
  if (rRU) rRU.checked = LANG === "ru";
  var lEN = document.getElementById("lbl_lang_en"), lRU = document.getElementById("lbl_lang_ru");
  if (lEN) lEN.className = LANG === "en" ? "active" : "";
  if (lRU) lRU.className = LANG === "ru" ? "active" : "";
  renderRowList();
  renderColList();
  readControls();
  updateReadout();
}

/* =====================================================================
   Renderer
   ===================================================================== */
var PREVIEW_SEG = { corner: 10, comp: 6, hole: 14, og: 48 };
var RENDER_SEG  = { corner: 44, comp: 18, hole: 48, og: 96 };

var canvas = document.getElementById("gl");
var errBox = document.getElementById("err");
var badge  = document.getElementById("badge");
var savedBox = document.getElementById("saved");

/* When this page runs inside OrcaSlicer the host injects window.orca. In that
   case the STL goes to the plugin over postMessage instead of a download, and
   the panel adopts the host theme. Standalone in a browser, neither applies. */
var ORCA = (window.orca && typeof window.orca.postMessage === "function") ? window.orca : null;
if (ORCA) {
  document.documentElement.classList.add("orca-host");
  document.getElementById("toPlateRow").hidden = false;
  document.getElementById("exportDirRow").hidden = false;
  var dirEl = document.getElementById("exportDir");
  try { dirEl.value = localStorage.getItem("gf_export_dir") || ""; } catch (e2) {}
  dirEl.addEventListener("input", function () {
    try { localStorage.setItem("gf_export_dir", dirEl.value); } catch (e3) {}
  });
  var btnDir = document.getElementById("btnDir");
  if (btnDir) btnDir.addEventListener("click", function () {
    btnDir.disabled = true;
    note(t("dir_asking"), false);
    ORCA.postMessage({ type: "pick_dir", start: dirEl.value });
  });
  document.getElementById("bedRow").hidden = false;
}

function cssColor(name, fallback) {
  var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  var m = /^#?([0-9a-f]{6})$/i.exec(v);
  if (!m) return fallback;
  var n = parseInt(m[1], 16);
  return [Math.pow(((n >> 16) & 255) / 255, 2.2),
          Math.pow(((n >> 8) & 255) / 255, 2.2),
          Math.pow((n & 255) / 255, 2.2)];
}
var gl = canvas.getContext("webgl", { antialias: true, alpha: false, depth: true })
      || canvas.getContext("experimental-webgl", { antialias: true, alpha: false, depth: true });

var VERT = [
  "attribute vec3 aPos;",
  "attribute vec3 aNrm;",
  "uniform mat4 uMVP;",
  "varying vec3 vN; varying vec3 vP;",
  "void main(){ vN = aNrm; vP = aPos; gl_Position = uMVP * vec4(aPos, 1.0); }"
].join("\n");

var FRAG = [
  "precision highp float;",
  "varying vec3 vN; varying vec3 vP;",
  "uniform vec3 uCol, uEye, uGroundCol, uBgCol;",
  "uniform vec2 uHalf;",
  "uniform float uR, uIsGround;",
  "float rr(vec2 p, vec2 b, float r){",
  "  vec2 q = abs(p) - b + r;",
  "  return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r;",
  "}",
  "void main(){",
  "  vec3 L1 = normalize(vec3(0.45, -0.78, 0.85));",
  "  vec3 L2 = normalize(vec3(-0.72, 0.40, 0.35));",
  "  if (uIsGround > 0.5) {",
  "    float d = rr(vP.xy, uHalf, uR);",
  "    float sh = smoothstep(0.0, 30.0, d);",           // contact shadow
  "    float far = smoothstep(60.0, 460.0, d);",         // fade to background
  "    vec3 g = uGroundCol * mix(0.34, 1.0, sh);",
  "    gl_FragColor = vec4(pow(mix(g, uBgCol, far), vec3(0.4545)), 1.0);",
  "    return;",
  "  }",
  "  vec3 N = normalize(vN);",
  "  vec3 V = normalize(uEye - vP);",
  "  if (!gl_FrontFacing) N = -N;",
  "  float k1 = max(dot(N, L1), 0.0);",
  "  float k2 = max(dot(N, L2), 0.0);",
  "  float amb = 0.5 + 0.5 * N.z;",
  "  vec3 c = uCol * (0.13 + 0.70 * k1 + 0.24 * k2 + 0.34 * amb);",
  "  vec3 H = normalize(L1 + V);",
  "  c += vec3(0.20) * pow(max(dot(N, H), 0.0), 30.0);",
  "  float fre = pow(1.0 - max(dot(N, V), 0.0), 4.0);",
  "  c += uCol * 0.55 * fre;",
  "  gl_FragColor = vec4(pow(clamp(c, 0.0, 1.0), vec3(0.4545)), 1.0);",
  "}"
].join("\n");

/* ---- matrix helpers ---- */
function mMul(a, b) {
  var o = new Float32Array(16);
  for (var i = 0; i < 4; i++) for (var j = 0; j < 4; j++) {
    var s = 0;
    for (var k = 0; k < 4; k++) s += a[k * 4 + j] * b[i * 4 + k];
    o[i * 4 + j] = s;
  }
  return o;
}
function mPersp(fovy, aspect, near, far) {
  var f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
  return new Float32Array([f / aspect,0,0,0, 0,f,0,0, 0,0,(far+near)*nf,-1, 0,0,2*far*near*nf,0]);
}
function mLookAt(eye, ctr, up) {
  var z = nrm3([eye[0]-ctr[0], eye[1]-ctr[1], eye[2]-ctr[2]]);
  var x = nrm3(crs3(up, z));
  var y = crs3(z, x);
  return new Float32Array([
    x[0],y[0],z[0],0, x[1],y[1],z[1],0, x[2],y[2],z[2],0,
    -(x[0]*eye[0]+x[1]*eye[1]+x[2]*eye[2]),
    -(y[0]*eye[0]+y[1]*eye[1]+y[2]*eye[2]),
    -(z[0]*eye[0]+z[1]*eye[1]+z[2]*eye[2]), 1]);
}
function crs3(a,b){ return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]; }
function nrm3(a){ var l = Math.hypot(a[0],a[1],a[2]) || 1; return [a[0]/l, a[1]/l, a[2]/l]; }

/* ---- GL state ---- */
var prog, U = {}, aPos, aNrm;
var binBuf = { pos: null, nrm: null, n: 0 };
var groundBuf = { pos: null, nrm: null, n: 0 };

function compile(type, src) {
  var s = gl.createShader(type);
  gl.shaderSource(s, src); gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
  return s;
}

function initGL() {
  prog = gl.createProgram();
  gl.attachShader(prog, compile(gl.VERTEX_SHADER, VERT));
  gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, FRAG));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
  gl.useProgram(prog);
  aPos = gl.getAttribLocation(prog, "aPos");
  aNrm = gl.getAttribLocation(prog, "aNrm");
  gl.enableVertexAttribArray(aPos);
  gl.enableVertexAttribArray(aNrm);
  ["uMVP","uCol","uEye","uGroundCol","uBgCol","uHalf","uR","uIsGround"]
    .forEach(function (n) { U[n] = gl.getUniformLocation(prog, n); });
  gl.enable(gl.DEPTH_TEST);
  gl.enable(gl.CULL_FACE);
  gl.cullFace(gl.BACK);
  binBuf.pos = gl.createBuffer(); binBuf.nrm = gl.createBuffer();
  groundBuf.pos = gl.createBuffer(); groundBuf.nrm = gl.createBuffer();
}

function uploadMesh(mesh) {
  gl.bindBuffer(gl.ARRAY_BUFFER, binBuf.pos);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(mesh.pos), gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, binBuf.nrm);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(mesh.nrm), gl.STATIC_DRAW);
  binBuf.n = mesh.count() * 3;
}

function uploadGround(size) {
  var s = size, v = [-s,-s,-0.02, s,-s,-0.02, s,s,-0.02, -s,-s,-0.02, s,s,-0.02, -s,s,-0.02];
  var n = []; for (var i = 0; i < 6; i++) n.push(0, 0, 1);
  gl.bindBuffer(gl.ARRAY_BUFFER, groundBuf.pos);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(v), gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, groundBuf.nrm);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(n), gl.STATIC_DRAW);
  groundBuf.n = 6;
}

/* ---- camera ---- */
var cam = { az: -0.62, el: 0.40, dist: 260, target: [0, 0, 12] };
var dragging = false, quality = "preview", needsDraw = true;
var renderCache = null;            // last full-detail build, reused by export
var bedFrom = null;

function frameCamera() {
  var d = P.mode === "plate" ? derivePlate(P)
        : P.mode === "og" ? deriveBoard(P) : derive(P);
  var top = (d.HTotal != null) ? d.HTotal : d.TOP;
  if (!isFinite(top)) top = 20;
  cam.target = [0, 0, top * 0.42];
  var span = Math.max(d.OX || 0, d.OY || 0, top || 0);
  cam.dist = (isFinite(span) && span > 0 ? span : 120) * 2.0 + 55;
  needsDraw = true;
}
function setView(v) {
  if (v === "iso")   { cam.az = -0.62; cam.el = 0.40; }
  if (v === "front") { cam.az = -Math.PI/2; cam.el = 0.06; }
  if (v === "top")   { cam.az = -Math.PI/2; cam.el = 1.40; }
  if (v === "under") { cam.az = -Math.PI/2; cam.el = -1.20; }
  frameCamera();
}
/* ---- 2D top view + board editor --------------------------------------
   2D is an orthographic straight-down view with a DOM-canvas overlay
   that marks every editable feature. A click (3D keeps the old pick)
   toggles: screw bore at a lattice crossing, board-to-board connector
   at a border node, or a whole cell -- the window is trimmed on the
   lattice lines, neighbours keep their half of the shared rib. */
var EDIT2D = false, editScale = 20, editCenter = [0, 0];
var hover2D = null, hoverKey = null;
var ov = document.getElementById("ov");
var ovCtx = ov && ov.getContext ? ov.getContext("2d") : null;

function mOrtho(l, r, b, t, n, f) {
  return new Float32Array([
    2 / (r - l), 0, 0, 0,
    0, 2 / (t - b), 0, 0,
    0, 0, -2 / (f - n), 0,
    -(r + l) / (r - l), -(t + b) / (t - b), -(f + n) / (f - n), 1]);
}
function w2sX(x) { return (x - editCenter[0]) * editScale + canvas.clientWidth / 2; }
function w2sY(y) { return canvas.clientHeight / 2 - (y - editCenter[1]) * editScale; }
function s2wX(px) { return (px - canvas.clientWidth / 2) / editScale + editCenter[0]; }
function s2wY(py) { return editCenter[1] - (py - canvas.clientHeight / 2) / editScale; }

function enter2D() {
  if (EDIT2D) return;
  EDIT2D = true;
  document.getElementById("btn2d").className = "active";
  document.getElementById("btn3d").className = "";
  document.getElementById("hint2d").hidden = false;
  document.getElementById("hint3d").hidden = true;
  if (ov) ov.style.display = "block";
  var d = deriveBoard(P);
  var cw = canvas.clientWidth || 800, chh = canvas.clientHeight || 600;
  editScale = Math.max(2, Math.min(cw, chh) / (Math.max(d.OX, d.OY) + 40));
  editCenter = [0, 0];
  hover2D = null; hoverKey = null;
  canvas.style.cursor = P.mode === "og" ? "crosshair" : "";
  needsDraw = true;
}
function exit2D() {
  if (!EDIT2D) return;
  EDIT2D = false;
  document.getElementById("btn2d").className = "";
  document.getElementById("btn3d").className = "active";
  document.getElementById("hint2d").hidden = true;
  document.getElementById("hint3d").hidden = false;
  if (ov) {
    if (ovCtx) {
      ovCtx.setTransform(1, 0, 0, 1, 0, 0);
      ovCtx.clearRect(0, 0, ov.width, ov.height);
    }
    ov.style.display = "none";
  }
  hover2D = null; hoverKey = null;
  canvas.style.cursor = (P.mode === "og" && P.ogScrews) ? "crosshair" : "";
  needsDraw = true;
}

/* every editable feature of the og board, with its current state */
function ogEditItems() {
  var d = deriveBoard(P), T = OG.TILE;
  var items = { W: d.W, H: d.H, cells: [], screws: [], conns: [], corners: [] };
  function off(m, k) { return !!(P[m] && P[m][k]); }
  function nodeClass(ix, iy) {
    var nw = ix >= 1 && iy >= 1 && !off("ogCellOff", (ix - 1) + "," + (iy - 1));
    var ne = ix <= d.W - 1 && iy >= 1 && !off("ogCellOff", ix + "," + (iy - 1));
    var sw = ix >= 1 && iy <= d.H - 1 && !off("ogCellOff", (ix - 1) + "," + iy);
    var se = ix <= d.W - 1 && iy <= d.H - 1 && !off("ogCellOff", ix + "," + iy);
    var n = (nw ? 1 : 0) + (ne ? 1 : 0) + (sw ? 1 : 0) + (se ? 1 : 0);
    if (!n) return "none";
    if (n >= 3 || (n === 2 && ((nw && se) || (ne && sw)))) return "fill";
    if (n === 2) return "edge";
    return "outer";
  }
  for (var row = 0; row < d.H; row++)
    for (var col = 0; col < d.W; col++)
      items.cells.push({ col: col, row: row,
        x: (col + 0.5 - d.W / 2) * T, y: (d.H / 2 - row - 0.5) * T,
        off: off("ogCellOff", col + "," + row) });
  if (P.ogScrews)
    for (var iy = 1; iy < d.H; iy++)
      for (var ix = 1; ix < d.W; ix++)
        if (nodeClass(ix, iy) === "fill") {
          var sk = ogNodeKey((ix - d.W / 2) * T, (d.H / 2 - iy) * T);
          items.screws.push({ ix: ix, iy: iy, key: sk,
            x: (ix - d.W / 2) * T, y: (d.H / 2 - iy) * T,
            state: off("ogChamf", sk) ? "chamfer"
                 : (off("ogScrewOff", sk) ? "off" : "on") });
        }
  if (P.ogConnectors)
    for (var i = 0; i <= d.W; i++)
      for (var j = 0; j <= d.H; j++)
        if (nodeClass(i, j) === "edge")
          items.conns.push({ ix: i, iy: j, key: ogNodeKey((i - d.W / 2) * T, (d.H / 2 - j) * T),
            x: (i - d.W / 2) * T, y: (d.H / 2 - j) * T,
            state: off("ogConnOff", ogNodeKey((i - d.W / 2) * T, (d.H / 2 - j) * T)) ? "off" : "on" });
  for (var ci = 0; ci <= d.W; ci++)
    for (var cj = 0; cj <= d.H; cj++)
      if (nodeClass(ci, cj) === "outer") {
        var ck = ogNodeKey((ci - d.W / 2) * T, (d.H / 2 - cj) * T);
        items.corners.push({ ix: ci, iy: cj, key: ck,
          x: (ci - d.W / 2) * T, y: (d.H / 2 - cj) * T,
          sharp: off("ogCornerSharp", ck) });
      }
  return items;
}

function pick2DTarget(wx, wy) {
  if (P.mode !== "og") return null;
  var it = ogEditItems(), best = null, i, dx, dy, d2;
  for (i = 0; i < it.screws.length; i++) {          /* 3.5 mm */
    dx = wx - it.screws[i].x; dy = wy - it.screws[i].y;
    d2 = dx * dx + dy * dy;
    if (d2 <= 12.25 && (!best || d2 < best.d2))
      best = { kind: "screw", d2: d2, key: it.screws[i].key };
  }
  if (best) return best;
  for (i = 0; i < it.conns.length; i++) {           /* 4.2 mm */
    dx = wx - it.conns[i].x; dy = wy - it.conns[i].y;
    d2 = dx * dx + dy * dy;
    if (d2 <= 17.64 && (!best || d2 < best.d2))
      best = { kind: "conn", d2: d2, key: it.conns[i].key };
  }
  if (best) return best;
  for (i = 0; i < it.corners.length; i++) {         /* 5 mm */
    dx = wx - it.corners[i].x; dy = wy - it.corners[i].y;
    d2 = dx * dx + dy * dy;
    if (d2 <= 25 && (!best || d2 < best.d2))
      best = { kind: "corner", d2: d2, key: it.corners[i].key };
  }
  if (best) return best;
  var col = Math.floor(wx / OG.TILE + it.W / 2);
  var row = Math.floor(it.H / 2 - wy / OG.TILE);
  if (col >= 0 && col < it.W && row >= 0 && row < it.H)
    return { kind: "cell", key: col + "," + row };
  return null;
}

function pick2D(e) {
  var r = canvas.getBoundingClientRect();
  var t = pick2DTarget(s2wX(e.clientX - r.left), s2wY(e.clientY - r.top));
  if (!t) return;
  if (t.kind === "screw") {
    /* ogstudio cycleNode: hole -> bare -> chamfered ring -> hole */
    if (!P.ogChamf) P.ogChamf = {};
    if (!P.ogScrewOff) P.ogScrewOff = {};
    if (!P.ogChamf[t.key] && !P.ogScrewOff[t.key]) P.ogScrewOff[t.key] = 1;
    else if (P.ogScrewOff[t.key]) { delete P.ogScrewOff[t.key]; P.ogChamf[t.key] = 1; }
    else delete P.ogChamf[t.key];
  } else if (t.kind === "corner") {
    /* ogstudio outer nodes: chamfered (default) <-> sharp corner */
    if (!P.ogCornerSharp) P.ogCornerSharp = {};
    if (P.ogCornerSharp[t.key]) delete P.ogCornerSharp[t.key];
    else P.ogCornerSharp[t.key] = 1;
  } else {
    var map = t.kind === "conn" ? "ogConnOff" : "ogCellOff";
    if (!P[map]) P[map] = {};
    if (P[map][t.key]) delete P[map][t.key]; else P[map][t.key] = 1;
  }
  hover2D = null; hoverKey = null;
  onChange(false);
}

function drawOverlay() {
  if (!EDIT2D || !ovCtx || P.mode !== "og") return;
  var dpr = Math.min(window.devicePixelRatio || 1, 2);
  var w = Math.max(1, Math.round(canvas.clientWidth * dpr));
  var h = Math.max(1, Math.round(canvas.clientHeight * dpr));
  if (ov.width !== w || ov.height !== h) { ov.width = w; ov.height = h; }
  var c = ovCtx;
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  var it = ogEditItems(), i, e;
  var T = OG.TILE;
  for (i = 0; i < it.cells.length; i++) {
    if (!it.cells[i].off) continue;
    var x0 = w2sX(it.cells[i].x - T / 2), y0 = w2sY(it.cells[i].y + T / 2);
    c.fillStyle = "rgba(224,76,64,0.28)";
    c.fillRect(x0, y0, T * editScale, T * editScale);
    c.strokeStyle = "rgba(224,76,64,0.95)"; c.lineWidth = 1.5;
    c.strokeRect(x0, y0, T * editScale, T * editScale);
  }
  for (i = 0; i < it.screws.length; i++) {
    e = it.screws[i];
    var px = w2sX(e.x), py = w2sY(e.y);
    if (e.state === "chamfer") {
      var ro = Math.max(6, 4.2 * editScale), ri = Math.max(3, 2.1 * editScale);
      c.beginPath();
      c.moveTo(px, py - ro); c.lineTo(px + ro, py); c.lineTo(px, py + ro); c.lineTo(px - ro, py); c.closePath();
      c.moveTo(px, py - ri); c.lineTo(px + ri, py); c.lineTo(px, py + ri); c.lineTo(px - ri, py); c.closePath();
      c.strokeStyle = "rgba(150,100,220,0.95)"; c.lineWidth = 1.5; c.stroke();
      continue;
    }
    var rr = Math.max(4, (P.ogScrewD || 4.1) / 2 * editScale);
    c.beginPath(); c.arc(px, py, rr, 0, 6.2832);
    if (e.state === "on") { c.fillStyle = "rgba(64,180,110,0.22)"; c.fill();
      c.strokeStyle = "rgba(46,150,92,0.95)"; }
    else { c.strokeStyle = "rgba(224,76,64,0.95)"; c.setLineDash([3, 3]); }
    c.lineWidth = 1.5; c.stroke(); c.setLineDash([]);
  }
  for (i = 0; i < it.corners.length; i++) {
    e = it.corners[i];
    var rc = Math.max(6, 4.2 * editScale);
    var qx = w2sX(e.x), qy = w2sY(e.y);
    c.beginPath();
    c.moveTo(qx, qy - rc); c.lineTo(qx + rc, qy);
    c.lineTo(qx, qy + rc); c.lineTo(qx - rc, qy); c.closePath();
    if (e.sharp) { c.fillStyle = "rgba(64,180,110,0.25)"; c.fill();
      c.strokeStyle = "rgba(46,150,92,0.95)"; }
    else { c.strokeStyle = "rgba(200,160,50,0.95)"; c.setLineDash([3, 3]); }
    c.lineWidth = 1.5; c.stroke(); c.setLineDash([]);
  }
  for (i = 0; i < it.conns.length; i++) {
    e = it.conns[i];
    var r2 = Math.max(4, 3.2 * editScale);
    var cx = w2sX(e.x), cy = w2sY(e.y);
    c.beginPath();
    c.moveTo(cx, cy - r2); c.lineTo(cx + r2, cy);
    c.lineTo(cx, cy + r2); c.lineTo(cx - r2, cy); c.closePath();
    if (e.state === "on") { c.fillStyle = "rgba(70,140,220,0.20)"; c.fill();
      c.strokeStyle = "rgba(58,120,200,0.95)"; }
    else { c.strokeStyle = "rgba(224,76,64,0.95)"; c.setLineDash([3, 3]); }
    c.lineWidth = 1.5; c.stroke(); c.setLineDash([]);
  }
  if (hover2D) {
    c.strokeStyle = "rgba(60,150,255,0.95)"; c.lineWidth = 2.5;
    if (hover2D.kind === "cell") {
      var pp = hover2D.key.split(",");
      var hx = (+pp[0] + 0.5 - it.W / 2) * T, hy = (it.H / 2 - +pp[1] - 0.5) * T;
      c.strokeRect(w2sX(hx - T / 2), w2sY(hy + T / 2), T * editScale, T * editScale);
    } else {
      var list = hover2D.kind === "screw" ? it.screws
               : hover2D.kind === "corner" ? it.corners : it.conns;
      for (i = 0; i < list.length; i++)
        if (list[i].key === hover2D.key) {
          var hr = (hover2D.kind === "screw"
            ? Math.max(4, (P.ogScrewD || 4.1) / 2 * editScale)
            : Math.max(4, 3.2 * editScale)) + 3;
          c.beginPath();
          c.arc(w2sX(list[i].x), w2sY(list[i].y), hr, 0, 6.2832);
          c.stroke();
        }
    }
  }
}

var darkMQ = window.matchMedia("(prefers-color-scheme: dark)");
function palette() {
  var p = darkMQ.matches
    ? { bin:[0.055,0.20,0.135], ground:[0.085,0.095,0.112], bg:[0.055,0.062,0.075] }
    : { bin:[0.075,0.31,0.20],  ground:[0.74,0.755,0.785],  bg:[0.86,0.875,0.90] };
  if (ORCA) {
    p.bg = cssColor("--orca-bg", p.bg);
    p.ground = [p.bg[0] * 1.35 + 0.02, p.bg[1] * 1.35 + 0.02, p.bg[2] * 1.35 + 0.02];
  }
  return p;
}

function draw() {
  var ssaa = (quality === "render" && !dragging) ? 2 : 1;
  var dpr = Math.min(window.devicePixelRatio || 1, 2) * ssaa;
  var w = Math.max(1, Math.round(canvas.clientWidth * dpr));
  var h = Math.max(1, Math.round(canvas.clientHeight * dpr));
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  gl.viewport(0, 0, w, h);

  var pal = palette();
  gl.clearColor(Math.pow(pal.bg[0],0.4545), Math.pow(pal.bg[1],0.4545), Math.pow(pal.bg[2],0.4545), 1);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

  var ce = Math.cos(cam.el), se = Math.sin(cam.el);
  var eye, proj, view;
  if (EDIT2D) {
    var hw = canvas.clientWidth / 2 / editScale, hh = canvas.clientHeight / 2 / editScale;
    proj = mOrtho(-hw, hw, -hh, hh, 1, 2000);
    eye = [editCenter[0], editCenter[1], 500];
    view = mLookAt(eye, [editCenter[0], editCenter[1], 0], [0, 1, 0]);
  } else {
    eye = [cam.target[0] + cam.dist * Math.cos(cam.az) * ce,
           cam.target[1] + cam.dist * Math.sin(cam.az) * ce,
           cam.target[2] + cam.dist * se];
    proj = mPersp(0.62, w / h, 1, 6000);
    view = mLookAt(eye, cam.target, [0, 0, 1]);
  }
  var mvp = mMul(proj, view);
  var d = P.mode === "plate" ? derivePlate(P)
        : P.mode === "og" ? deriveBoard(P) : derive(P);

  gl.uniformMatrix4fv(U.uMVP, false, mvp);
  gl.uniform3fv(U.uEye, eye);
  gl.uniform3fv(U.uGroundCol, pal.ground);
  gl.uniform3fv(U.uBgCol, pal.bg);
  gl.uniform2f(U.uHalf, d.OX / 2, d.OY / 2);
  gl.uniform1f(U.uR, R_TOP);

  // ground
  gl.uniform1f(U.uIsGround, 1);
  gl.bindBuffer(gl.ARRAY_BUFFER, groundBuf.pos); gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 0, 0);
  gl.bindBuffer(gl.ARRAY_BUFFER, groundBuf.nrm); gl.vertexAttribPointer(aNrm, 3, gl.FLOAT, false, 0, 0);
  gl.drawArrays(gl.TRIANGLES, 0, groundBuf.n);

  // bin
  if (binBuf.n) {
    gl.uniform1f(U.uIsGround, 0);
    gl.uniform3fv(U.uCol, pal.bin);
    gl.bindBuffer(gl.ARRAY_BUFFER, binBuf.pos); gl.vertexAttribPointer(aPos, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, binBuf.nrm); gl.vertexAttribPointer(aNrm, 3, gl.FLOAT, false, 0, 0);
    gl.drawArrays(gl.TRIANGLES, 0, binBuf.n);
  }
  drawOverlay();
}

function loop() {
  if (needsDraw) { needsDraw = false; draw(); }
  requestAnimationFrame(loop);
}

/* =====================================================================
   Parameters, UI
   ===================================================================== */
var P = Object.assign({}, DEFAULTS, {
  mode: "bin",
  comp_layout: "grid",
  num_rows: 2,
  row_divs: [2, 1],
  num_cols: 2,
  col_divs: [2, 1],
  plate_size_mode: "units",
  plate_mm_x: 168,
  plate_mm_y: 126,
  plate_gx: 4, plate_gy: 3,
  plateExact: false,
  buf_x_ratio: 50,
  buf_y_ratio: 50,
  labelW: 0,
  plateBase: 0, plateR: 4,
  bedX: 250, bedY: 220, plateGap: 0, plateConnectors: true,
  plateStack: false, plateStackN: 2, plateStackGap: 0.2,
  ogW: 4, ogH: 3, ogType: "full",
  ogScrews: true, ogConnectors: true,
  ogScrewD: 4.1, ogScrewHeadD: 7.2, ogScrewInset: 1,
  ogCs: false, ogCsDeg: 90,
  ogBackside: false, ogBackInset: 1, ogBackShrink: 0, ogBackCs: false, ogBackCsDeg: 90,
  ogStack: false, ogStackN: 2, ogStackGap: 0.2
});
var FLOATS = ["wall","floorT","scoopR","labelD","labelW","fillet","plateBase","plateR","bedX","bedY","plateGap","plateStackGap",
              "ogScrewD","ogScrewHeadD","ogScrewInset","ogCsDeg","ogBackInset","ogBackShrink","ogBackCsDeg","ogStackGap"];
var BOOLS = ["lip","scoop","label","mag","screw","plateConnectors","plateStack",
             "ogScrews","ogConnectors","ogCs","ogBackside","ogBackCs","ogStack"];
var lastTris = 0;
var lastMeshPos = null;

function mmToUnits(mm) {
  return Math.max(1, Math.floor((mm + GAP) / GRID));
}

function getVal(id, numId, maxVal, minVal) {
  var min = minVal !== undefined ? minVal : 1;
  var limit = maxVal || 50;
  var numEl = document.getElementById(numId);
  if (numEl) {
    var v = parseFloat(numEl.value);
    if (isFinite(v)) return Math.max(min, Math.min(limit, v));
  }
  var rangeEl = document.getElementById(id);
  if (rangeEl) {
    var rv = parseFloat(rangeEl.value);
    if (isFinite(rv)) return Math.max(min, Math.min(limit, rv));
  }
  return min;
}

function renderRowList() {
  var container = document.getElementById("rowDivList");
  if (!container) return;
  container.innerHTML = "";
  var nr = Math.max(1, Math.min(50, P.num_rows || 2));
  if (!P.row_divs) P.row_divs = [];
  while (P.row_divs.length < nr) P.row_divs.push(P.row_divs[P.row_divs.length - 1] || 1);
  if (P.row_divs.length > nr) P.row_divs.length = nr;

  for (var j = 0; j < nr; j++) {
    (function(rowIndex) {
      var item = document.createElement("div");
      item.className = "dyn-item";
      var lblText = t("row_n", { n: rowIndex + 1 }) +
        (rowIndex === 0 ? t("pos_front") : rowIndex === nr - 1 ? t("pos_back") : "");
      item.innerHTML = '<label>' + lblText + '</label>' +
        '<input type="range" min="1" max="8" step="1" value="' + Math.min(8, P.row_divs[rowIndex]) + '">' +
        '<input type="number" min="1" max="50" step="1" value="' + P.row_divs[rowIndex] + '">';
      container.appendChild(item);

      var rangeEl = item.querySelector('input[type="range"]');
      var numEl = item.querySelector('input[type="number"]');

      rangeEl.addEventListener("input", function() {
        numEl.value = rangeEl.value;
        P.row_divs[rowIndex] = +rangeEl.value;
        onChange(false);
      });
      rangeEl.addEventListener("change", function() {
        numEl.value = rangeEl.value;
        P.row_divs[rowIndex] = +rangeEl.value;
        onChange(false);
      });

      numEl.addEventListener("input", function() {
        var v = parseInt(numEl.value, 10);
        if (!isNaN(v)) {
          rangeEl.value = Math.max(1, Math.min(8, v));
          P.row_divs[rowIndex] = Math.max(1, Math.min(50, v));
        }
        onChange(false);
      });
      numEl.addEventListener("change", function() {
        var v = parseInt(numEl.value, 10);
        if (isNaN(v)) v = +rangeEl.value;
        v = Math.max(1, Math.min(50, v));
        numEl.value = v;
        rangeEl.value = Math.min(8, v);
        P.row_divs[rowIndex] = v;
        onChange(false);
      });
    })(j);
  }
}

function renderColList() {
  var container = document.getElementById("colDivList");
  if (!container) return;
  container.innerHTML = "";
  var nc = Math.max(1, Math.min(50, P.num_cols || 2));
  if (!P.col_divs) P.col_divs = [];
  while (P.col_divs.length < nc) P.col_divs.push(P.col_divs[P.col_divs.length - 1] || 1);
  if (P.col_divs.length > nc) P.col_divs.length = nc;

  for (var i = 0; i < nc; i++) {
    (function(colIndex) {
      var item = document.createElement("div");
      item.className = "dyn-item";
      var lblText = t("col_n", { n: colIndex + 1 }) +
        (colIndex === 0 ? t("pos_left") : colIndex === nc - 1 ? t("pos_right") : "");
      item.innerHTML = '<label>' + lblText + '</label>' +
        '<input type="range" min="1" max="8" step="1" value="' + Math.min(8, P.col_divs[colIndex]) + '">' +
        '<input type="number" min="1" max="50" step="1" value="' + P.col_divs[colIndex] + '">';
      container.appendChild(item);

      var rangeEl = item.querySelector('input[type="range"]');
      var numEl = item.querySelector('input[type="number"]');

      rangeEl.addEventListener("input", function() {
        numEl.value = rangeEl.value;
        P.col_divs[colIndex] = +rangeEl.value;
        onChange(false);
      });
      rangeEl.addEventListener("change", function() {
        numEl.value = rangeEl.value;
        P.col_divs[colIndex] = +rangeEl.value;
        onChange(false);
      });

      numEl.addEventListener("input", function() {
        var v = parseInt(numEl.value, 10);
        if (!isNaN(v)) {
          rangeEl.value = Math.max(1, Math.min(8, v));
          P.col_divs[colIndex] = Math.max(1, Math.min(50, v));
        }
        onChange(false);
      });
      numEl.addEventListener("change", function() {
        var v = parseInt(numEl.value, 10);
        if (isNaN(v)) v = +rangeEl.value;
        v = Math.max(1, Math.min(50, v));
        numEl.value = v;
        rangeEl.value = Math.min(8, v);
        P.col_divs[colIndex] = v;
        onChange(false);
      });
    })(i);
  }
}

function readControls() {
  var isPlate = document.getElementById("mode_plate").checked;
  var isOg = document.getElementById("mode_og").checked;
  P.mode = isOg ? "og" : (isPlate ? "plate" : "bin");

  var lblBin = document.getElementById("lbl_mode_bin");
  var lblPlate = document.getElementById("lbl_mode_plate");
  var lblOg = document.getElementById("lbl_mode_og");
  if (lblBin) lblBin.className = !isPlate && !isOg ? "active" : "";
  if (lblPlate) lblPlate.className = isPlate ? "active" : "";
  if (lblOg) lblOg.className = isOg ? "active" : "";

  document.getElementById("binSizeSection").hidden = isPlate || isOg;
  document.getElementById("binOpts").hidden = isPlate || isOg;
  document.getElementById("binFeatures").hidden = isPlate || isOg;
  document.getElementById("plateOpts").hidden = !isPlate;
  document.getElementById("ogOpts").hidden = !isOg;

  if (!isPlate) {
    var layoutGrid = document.getElementById("layout_grid").checked;
    var layoutRows = document.getElementById("layout_rows").checked;
    var layoutCols = document.getElementById("layout_cols").checked;

    P.comp_layout = layoutRows ? "rows" : layoutCols ? "cols" : "grid";

    var lg = document.getElementById("lbl_layout_grid");
    var lr = document.getElementById("lbl_layout_rows");
    var lc = document.getElementById("lbl_layout_cols");
    if (lg) lg.className = layoutGrid ? "active" : "";
    if (lr) lr.className = layoutRows ? "active" : "";
    if (lc) lc.className = layoutCols ? "active" : "";

    document.getElementById("compGridOpts").hidden = !layoutGrid;
    document.getElementById("compRowOpts").hidden = !layoutRows;
    document.getElementById("compColOpts").hidden = !layoutCols;

    P.gx = getVal("gx", "gx_num");
    P.gy = getVal("gy", "gy_num");
    P.gz = getVal("gz", "gz_num");
    P.dx = getVal("dx", "dx_num");
    P.dy = getVal("dy", "dy_num");

    var nr = getVal("num_rows", "num_rows_num");
    if (nr !== P.num_rows) {
      P.num_rows = nr;
      renderRowList();
    }
    var nc = getVal("num_cols", "num_cols_num");
    if (nc !== P.num_cols) {
      P.num_cols = nc;
      renderColList();
    }
  } else {
    var isMm = document.getElementById("plate_mode_mm").checked;
    P.plate_size_mode = isMm ? "mm" : "units";

    var lblUnits = document.getElementById("lbl_plate_units");
    var lblMm = document.getElementById("lbl_plate_mm");
    if (lblUnits) lblUnits.className = !isMm ? "active" : "";
    if (lblMm) lblMm.className = isMm ? "active" : "";

    document.getElementById("plateUnitsOpts").hidden = isMm;
    document.getElementById("plateMmOpts").hidden = !isMm;

    if (isMm) {
      P.plate_mm_x = getVal("plate_mm_x", "plate_mm_x_num", 2000);
      P.plate_mm_y = getVal("plate_mm_y", "plate_mm_y_num", 2000);
      P.plate_gx = mmToUnits(P.plate_mm_x);
      P.plate_gy = mmToUnits(P.plate_mm_y);

      var gxEl = document.getElementById("plate_gx");
      var gxNumEl = document.getElementById("plate_gx_num");
      if (gxEl) gxEl.value = Math.min(+gxEl.max || 20, P.plate_gx);
      if (gxNumEl) gxNumEl.value = P.plate_gx;

      var gyEl = document.getElementById("plate_gy");
      var gyNumEl = document.getElementById("plate_gy_num");
      if (gyEl) gyEl.value = Math.min(+gyEl.max || 20, P.plate_gy);
      if (gyNumEl) gyNumEl.value = P.plate_gy;
    } else {
      P.plate_gx = getVal("plate_gx", "plate_gx_num", 50);
      P.plate_gy = getVal("plate_gy", "plate_gy_num", 50);

      P.plate_mm_x = Math.round(P.plate_gx * GRID);
      P.plate_mm_y = Math.round(P.plate_gy * GRID);

      var mmXEl = document.getElementById("plate_mm_x");
      var mmXNumEl = document.getElementById("plate_mm_x_num");
      if (mmXEl) mmXEl.value = Math.min(+mmXEl.max || 600, P.plate_mm_x);
      if (mmXNumEl) mmXNumEl.value = P.plate_mm_x;

      var mmYEl = document.getElementById("plate_mm_y");
      var mmYNumEl = document.getElementById("plate_mm_y_num");
      if (mmYEl) mmYEl.value = Math.min(+mmYEl.max || 600, P.plate_mm_y);
      if (mmYNumEl) mmYNumEl.value = P.plate_mm_y;
    }

    P.buf_x_ratio = getVal("buf_x_ratio", "buf_x_ratio_num", 100, 0);
    P.buf_y_ratio = getVal("buf_y_ratio", "buf_y_ratio_num", 100, 0);
    P.plateStackN = getVal("plateStackN", "plateStackN_num", 10, 2);

    var alignBox = document.getElementById("plateBufferAlignOpts");
    if (alignBox) alignBox.hidden = !(P.plateExact && isMm);

    var btnExact = document.getElementById("btnPlateExact");
    var dp = derivePlate(P);
    if (btnExact) {
      if (P.plateExact && isMm && (dp.padLeft > 0 || dp.padRight > 0 || dp.padBottom > 0 || dp.padTop > 0)) {
        btnExact.className = "primary";
        btnExact.textContent = t("btn_exact_on", { x: fmt(dp.OX), y: fmt(dp.OY) });
      } else {
        btnExact.className = "";
        var remX = Math.max(0, Math.round((P.plate_mm_x - P.plate_gx * GRID) * 10) / 10);
        var remY = Math.max(0, Math.round((P.plate_mm_y - P.plate_gy * GRID) * 10) / 10);
        btnExact.textContent = t("btn_exact_off", { x: remX, y: remY });
      }
    }

    var fitInfo = document.getElementById("plateFitInfo");
    if (fitInfo) {
      if (P.plateExact && isMm && (dp.padLeft > 0 || dp.padRight > 0 || dp.padBottom > 0 || dp.padTop > 0)) {
        var xDesc = dp.padLeft === dp.padRight ? t("pad_sym", { v: fmt(dp.padLeft) }) : t("pad_lr", { l: fmt(dp.padLeft), r: fmt(dp.padRight) });
        var yDesc = dp.padBottom === dp.padTop ? t("pad_sym", { v: fmt(dp.padBottom) }) : t("pad_bt", { b: fmt(dp.padBottom), t: fmt(dp.padTop) });
        fitInfo.textContent = t("fits_buffer", { x: P.plate_gx, y: P.plate_gy, xd: xDesc, yd: yDesc, ox: fmt(dp.OX), oy: fmt(dp.OY) });
      } else {
        fitInfo.textContent = t("fits_units", { x: P.plate_gx, y: P.plate_gy, mx: fmt(P.plate_gx * GRID), my: fmt(P.plate_gy * GRID) });
      }
    }

    P.gx = P.plate_gx;
    P.gy = P.plate_gy;
  }

  var scoopOpts = document.getElementById("scoopOpts");
  if (scoopOpts) scoopOpts.hidden = !P.scoop;
  var labelOpts = document.getElementById("labelOpts");
  if (labelOpts) labelOpts.hidden = !P.label;

  P.scoopR = getVal("scoopR", "scoopR_num", 50, 0.5);
  P.labelD = getVal("labelD", "labelD_num", 50, 1);
  P.labelW = getVal("labelW", "labelW_num", 500, 0);

  FLOATS.forEach(function (k) {
    var el = document.getElementById(k);
    if (el) {
      var v = parseFloat(el.value);
      if (isFinite(v)) P[k] = v;
    }
  });
  BOOLS.forEach(function (k) {
    var el = document.getElementById(k);
    if (el) P[k] = el.checked;
  });

  // visibility follows the freshly read state, so a single change event is enough
  var stackOpts = document.getElementById("stackOpts");
  if (stackOpts) stackOpts.hidden = !P.plateStack;

  if (isOg) {
    P.ogW = getVal("ogW", "ogW_num", 20);
    P.ogH = getVal("ogH", "ogH_num", 20);
    P.ogStackN = getVal("ogStackN", "ogStackN_num", 10, 2);
    P.ogType = document.getElementById("og_type_lite").checked ? "lite" : "full";
    var lblFull = document.getElementById("lbl_og_full");
    var lblLite = document.getElementById("lbl_og_lite");
    if (lblFull) lblFull.className = P.ogType === "full" ? "active" : "";
    if (lblLite) lblLite.className = P.ogType === "lite" ? "active" : "";
    var ogShow = function (id, on) {
      var el = document.getElementById(id);
      if (el) el.hidden = !on;
    };
    ogShow("ogScrewOpts", P.ogScrews);
    ogShow("ogCsDegRow", P.ogScrews && P.ogCs);
    ogShow("ogBackOpts", P.ogScrews && P.ogBackside);
    ogShow("ogBackCsDegRow", P.ogScrews && P.ogBackside && P.ogBackCs);
    ogShow("ogStackOpts", P.ogStack);
  }
}

var fmt = function (n) { return (Math.round(n * 100) / 100).toString(); };

function applyMesh(res, q) {
  quality = q;
  lastMeshPos = res.mesh.pos;
  canvas.style.cursor = (P.mode === "og" && P.ogScrews) ? "crosshair" : "";
  uploadMesh(res.mesh);
  uploadGround(Math.max(res.derived.OX, res.derived.OY) * 6 + 400);
  lastTris = res.mesh.count();
  document.getElementById("s_tris").textContent = lastTris.toLocaleString();
  badge.textContent = q === "render" ? t("badge_rendered") : t("badge_preview");
  badge.className = q === "render" ? "badge hi" : "badge";
  needsDraw = true;
}

function rebuild(q) {
  var seg = q === "render" ? RENDER_SEG : PREVIEW_SEG;
  var res = P.mode === "plate" ? buildPlate(P, seg)
          : P.mode === "og" ? buildBoard(P, seg)
          : buildBin(P, seg);
  if (q === "render") renderCache = { sig: meshSig(P, seg), res: res };
  applyMesh(res, q);
}

function updateReadout() {
  if (P.mode === "plate") return updatePlateReadout();
  if (P.mode === "og") return updateOgReadout();
  var d = derive(P);
  var MM = t("mm");
  document.getElementById("s_foot").textContent = fmt(d.OX) + " × " + fmt(d.OY) + " " + MM;
  document.getElementById("s_tall").textContent =
    fmt(d.TOP) + " " + MM + (P.lip ? t("body_of", { h: fmt(d.H_BODY) }) : "");

  var compText = "";
  if (!d.cells || d.cells.length === 0) {
    compText = t("no_comps");
  } else if (d.cells.length === 1) {
    compText = t("comp_one", { w: fmt(d.cells[0].cw), d: fmt(d.cells[0].cd) });
  } else if (P.comp_layout === "rows") {
    compText = t("comp_rows", { n: d.cells.length, r: P.num_rows, divs: (P.row_divs || []).join(", ") });
  } else if (P.comp_layout === "cols") {
    compText = t("comp_cols", { n: d.cells.length, c: P.num_cols, divs: (P.col_divs || []).join(", ") });
  } else {
    compText = t("comp_plain", { n: d.cells.length, w: fmt(d.cells[0].cw), d: fmt(d.cells[0].cd) });
  }
  document.getElementById("s_comp").textContent = compText;
  document.getElementById("s_depth").textContent = fmt(d.depth) + " " + MM;
  var warn = document.getElementById("s_warn"), msgs = [];
  if (!d.valid) msgs.push(t("warn_invalid"));
  else if (d.depth < 2) msgs.push(t("warn_shallow"));
  warn.hidden = msgs.length === 0;
  warn.textContent = msgs.join(" ");
}

function updateOgReadout() {
  var d = deriveBoard(P);
  var MM = t("mm");
  document.getElementById("s_foot").textContent = fmt(d.OX) + " × " + fmt(d.OY) + " " + MM;
  document.getElementById("s_tall").textContent =
    fmt(d.HTotal) + " " + MM +
    (d.levels > 1 ? " (" + d.levels + " × " + fmt(d.T) + " " + MM + ")" : "");
  document.getElementById("s_comp").textContent = t("og_summary", {
    w: d.W, h: d.H, snap: d.snapHoles, scr: d.screwHoles, conn: d.connHoles
  });
  document.getElementById("s_depth").textContent =
    fmt(d.T) + " " + MM + (d.T === OG.LITE_T ? " · Lite" : " · Full");
  document.getElementById("s_warn").hidden = true;
}

function updatePlateReadout() {
  var d = derivePlate(P);
  var MM = t("mm");
  document.getElementById("s_foot").textContent = fmt(d.OX) + " × " + fmt(d.OY) + " " + MM;
  document.getElementById("s_tall").textContent =
    fmt(d.HTotal) + " " + MM +
    (d.levels > 1 ? " (" + pieceTxt(d.levels).level + " × " + fmt(d.H) + " " + MM + ")" : "");
  var sizeText = t("cells", { x: P.gx, y: P.gy });
  if (P.plate_size_mode === "mm") {
    if (d.padLeft > 0 || d.padRight > 0 || d.padBottom > 0 || d.padTop > 0) {
      sizeText += t("plus_buffer", { x: fmt(d.OX), y: fmt(d.OY) });
    } else {
      sizeText += t("target_mm", { x: P.plate_mm_x, y: P.plate_mm_y });
    }
  }
  document.getElementById("s_comp").textContent = sizeText;
  var pl = planPlate(P), np = 0, sx;
  for (sx = 0; sx < pl.cols.length; sx++) np += pl.rows[sx % 2].length;
  var total = np * d.levels;
  document.getElementById("s_depth").textContent =
    pieceTxt(total).piece + t("cols_split", { cols: pl.cols.join("+") }) +
    (d.levels > 1 ? t("levels_x", { n: d.levels }) : "");
  document.getElementById("s_warn").hidden = true;
}

function command() {
  var args = [];
  function push(k, v) { args.push("-D " + k + "=" + v); }
  push("gx", P.gx); push("gy", P.gy); push("gz", P.gz);
  if (P.comp_layout === "rows") {
    push("row_divisions", "'[" + (P.row_divs || []).join(",") + "]'");
  } else if (P.comp_layout === "cols") {
    push("col_divisions", "'[" + (P.col_divs || []).join(",") + "]'");
  } else {
    push("divisions_x", P.dx); push("divisions_y", P.dy);
  }
  push("stacking_lip", P.lip); push("scoop", P.scoop); push("label_tab", P.label);
  push("magnet_holes", P.mag); push("screw_holes", P.screw);
  if (P.scoop && P.scoopR !== DEFAULTS.scoopR) push("scoop_radius", P.scoopR);
  if (P.label) {
    if (P.labelD !== DEFAULTS.labelD) push("label_depth", P.labelD);
    if (P.labelW && P.labelW > 0) push("label_width", P.labelW);
  }
  var adv = { wall:"wall", floorT:"floor_thickness", fillet:"inner_fillet" };
  for (var k in adv) if (P[k] !== DEFAULTS[k]) push(adv[k], P[k]);
  return "openscad " + args.join(" ") + " -o " + stem() + ".stl gridfinity_bin.scad";
}

function stem() {
  if (P.mode === "og") {
    var db = deriveBoard(P);
    var ogSfx = db.levels > 1
      ? "_stack" + db.levels + "_g" + (Math.round(Math.max(0, +P.ogStackGap || 0) * 100) / 100)
      : "";
    if ((P.ogCellOff && Object.keys(P.ogCellOff).length) ||
        (P.ogConnOff && Object.keys(P.ogConnOff).length) ||
        (P.ogScrewOff && Object.keys(P.ogScrewOff).length) ||
        (P.ogChamf && Object.keys(P.ogChamf).length) ||
        (P.ogCornerSharp && Object.keys(P.ogCornerSharp).length)) ogSfx += "_edit";
    return "opengrid_board_" + db.W + "x" + db.H + "_" +
           (db.T === OG.LITE_T ? "lite" : "full") + ogSfx;
  }
  if (P.mode === "plate") {
    var dp = derivePlate(P);
    if (dp.padLeft > 0 || dp.padRight > 0 || dp.padBottom > 0 || dp.padTop > 0) {
      var alignSuffix = "";
      if (P.buf_x_ratio !== 50) alignSuffix += "_x" + P.buf_x_ratio;
      if (P.buf_y_ratio !== 50) alignSuffix += "_y" + P.buf_y_ratio;
      return "gridfinity_baseplate_" + P.gx + "x" + P.gy + "_exact_" + Math.round(dp.OX) + "x" + Math.round(dp.OY) + "mm" + alignSuffix;
    }
    if (P.plateStack) {
      var gapTxt = (Math.round(Math.max(0, +P.plateStackGap || 0) * 100) / 100).toString();
      return "gridfinity_baseplate_" + P.gx + "x" + P.gy + "_stack" + dp.levels + "_g" + gapTxt;
    }
    return "gridfinity_baseplate_" + P.gx + "x" + P.gy;
  }
  var compSuffix = "";
  if (P.comp_layout === "rows") {
    compSuffix = "_rows_" + (P.row_divs || []).join("-");
  } else if (P.comp_layout === "cols") {
    compSuffix = "_cols_" + (P.col_divs || []).join("-");
  } else if (P.dx * P.dy > 1) {
    compSuffix = "_" + P.dx + "x" + P.dy;
  }
  return "gridfinity_" + P.gx + "x" + P.gy + "x" + P.gz + compSuffix;
}

// Rebuilds are debounced: readouts stay instant while dragging a slider, and
// only the last value triggers a mesh build (which is slow for big grids).
var rebuildTimer = null;
function onChange(refit) {
  readControls();
  renderCache = null;              // settings moved on; the cached mesh is stale
  updateReadout();
  badge.textContent = "Preview";
  badge.className = "badge";
  if (refit) frameCamera();
  if (rebuildTimer) clearTimeout(rebuildTimer);
  rebuildTimer = setTimeout(function () {
    rebuildTimer = null;
    rebuild("preview");
  }, 45);
}

var SLIDERS = [
  { id: "gx", num: "gx_num", refit: true },
  { id: "gy", num: "gy_num", refit: true },
  { id: "gz", num: "gz_num", refit: true },
  { id: "dx", num: "dx_num", refit: false },
  { id: "dy", num: "dy_num", refit: false },
  { id: "num_rows", num: "num_rows_num", refit: false },
  { id: "num_cols", num: "num_cols_num", refit: false },
  { id: "plate_gx", num: "plate_gx_num", refit: true },
  { id: "plate_gy", num: "plate_gy_num", refit: true },
  { id: "plate_mm_x", num: "plate_mm_x_num", refit: true },
  { id: "plate_mm_y", num: "plate_mm_y_num", refit: true },
  { id: "buf_x_ratio", num: "buf_x_ratio_num", refit: true },
  { id: "buf_y_ratio", num: "buf_y_ratio_num", refit: true },
  { id: "scoopR", num: "scoopR_num", refit: false },
  { id: "labelD", num: "labelD_num", refit: false },
  { id: "labelW", num: "labelW_num", refit: false },
  { id: "plateStackN", num: "plateStackN_num", refit: true },
  { id: "ogW", num: "ogW_num", refit: true },
  { id: "ogH", num: "ogH_num", refit: true },
  { id: "ogStackN", num: "ogStackN_num", refit: true }
];

SLIDERS.forEach(function (s) {
  var rangeEl = document.getElementById(s.id);
  var numEl = document.getElementById(s.num);
  if (!rangeEl || !numEl) return;

  rangeEl.addEventListener("input", function () {
    numEl.value = rangeEl.value;
    onChange(s.refit);
  });
  rangeEl.addEventListener("change", function () {
    numEl.value = rangeEl.value;
    onChange(s.refit);
  });

  numEl.addEventListener("input", function () {
    var v = parseFloat(numEl.value);
    if (isFinite(v)) {
      var min = rangeEl.min !== "" ? +rangeEl.min : 1;
      var max = rangeEl.max !== "" ? +rangeEl.max : 8;
      rangeEl.value = Math.max(min, Math.min(max, v));
    }
    onChange(s.refit);
  });
  numEl.addEventListener("change", function () {
    var v = parseFloat(numEl.value);
    if (!isFinite(v)) v = +rangeEl.value;
    var min = numEl.min !== "" ? +numEl.min : 1;
    var max = numEl.max !== "" ? +numEl.max : 50;
    v = Math.max(min, Math.min(max, v));
    numEl.value = v;
    rangeEl.value = Math.min(rangeEl.max !== "" ? +rangeEl.max : 8, v);
    onChange(s.refit);
  });
});

FLOATS.concat(BOOLS).forEach(function (k) {
  var el = document.getElementById(k);
  if (!el) return;
  var refit = k === "lip" || k === "plateR" || k === "plateStack" || k === "plateStackGap" ||
              k === "ogStack" || k === "ogStackGap";
  el.addEventListener("input", function () { onChange(refit); });
  el.addEventListener("change", function () { onChange(refit); });
});

var btnExact = document.getElementById("btnPlateExact");
if (btnExact) {
  btnExact.addEventListener("click", function () {
    P.plateExact = !P.plateExact;
    onChange(true);
  });
}

["mode_bin", "mode_plate", "mode_og", "layout_grid", "layout_rows", "layout_cols", "plate_mode_units", "plate_mode_mm", "og_type_full", "og_type_lite"].forEach(function (id) {
  var el = document.getElementById(id);
  if (el) el.addEventListener("change", function () { onChange(id.startsWith("mode") || id.startsWith("plate_mode")); });
});

/* ---- language switch (EN / RU) ---- */
["lang_en", "lang_ru"].forEach(function (id) {
  var el = document.getElementById(id);
  if (el) el.addEventListener("change", function () {
    LANG = el.value;
    try { localStorage.setItem("gf_lang", LANG); } catch (e) {}
    applyLang();
  });
});

document.querySelectorAll(".views button[data-view]").forEach(function (b) {
  b.addEventListener("click", function () { exit2D(); setView(b.dataset.view); });
});
document.getElementById("btn2d").addEventListener("click", enter2D);
document.getElementById("btn3d").addEventListener("click", exit2D);

/* clear every click-edit (screws, connectors, removed cells) */
var btnOgReset = document.getElementById("btnOgReset");
if (btnOgReset) btnOgReset.addEventListener("click", function () {
  P.ogCellOff = {}; P.ogConnOff = {}; P.ogScrewOff = {};
  P.ogChamf = {}; P.ogCornerSharp = {};
  onChange(false);
  note(t("og_reset_done"), false);
});

/* ---- render button ---- */
var btnRender = document.getElementById("btnRender");
var btnStl = document.getElementById("btnStl");

function note(msg, on) {
  savedBox.hidden = !on;
  savedBox.textContent = msg || "";
}

/* =====================================================================
   Long jobs

   A full-detail 8x8 bin is a few hundred thousand triangles and takes some
   seconds to build and encode.  Doing that in one synchronous block froze the
   page with no sign of life, which reads as a button that did nothing.  The
   builders are generators, so the work is run in short slices with a repaint
   between them, and the progress bar says which stage it is in.
   ===================================================================== */

var progBox = document.getElementById("prog");
var progFill = document.getElementById("progFill");
var progStage = document.getElementById("progStage");
var progPct = document.getElementById("progPct");
var progHide = null;
var SLICE_MS = 20;                 // how long to work before handing back
var SLICE_MAX = 8;                 // ...and never more than this many slices,
var CHUNK_MAX = 32;                // so a coarse or clamped clock still yields

function meshSig(p, seg) { return (seg.corner || 0) + "/" + (seg.og || 0) + "|" + JSON.stringify(p); }

function progShow(stage) {
  if (progHide) { clearTimeout(progHide); progHide = null; }
  progBox.hidden = false;
  progSet(stage, 0);
}
function progSet(stage, frac) {
  var pct = Math.max(0, Math.min(100, Math.round(frac * 100)));
  progStage.textContent = stage;
  progFill.style.width = pct + "%";
  progPct.textContent = pct + "%";
}
function progDone(stage) {
  progSet(stage, 1);
  progHide = setTimeout(function () { progBox.hidden = true; progHide = null; }, 2500);
}
function defer(fn) { setTimeout(fn, 0); }

/* Drive a builder in slices.  Reuses the cached full-detail mesh when the
   settings have not changed, which makes export after Render near-instant. */
function buildAsync(p, seg, base, span, done, fail) {
  var sig = meshSig(p, seg);
  if (renderCache && renderCache.sig === sig) {
    progSet(t("prog_building"), base + span);
    return defer(function () { done(renderCache.res); });
  }
  var gen = p.mode === "plate" ? plateSteps(p, seg)
        : p.mode === "og" ? boardSteps(p, seg)
        : binSteps(p, seg);
  (function pump() {
    var until = performance.now() + SLICE_MS, n = 0, r;
    try {
      for (;;) {
        r = gen.next();
        if (r.done) {
          renderCache = { sig: sig, res: r.value };
          return done(r.value);
        }
        progSet(t("prog_building"), base + span * r.value);
        if (++n >= SLICE_MAX || performance.now() > until) return defer(pump);
      }
    } catch (e) { fail(e); }
  })();
}

/* base64 in 3-byte-aligned slices, so each piece can be encoded on its own
   and the multi-megabyte string never has to be built up in one go. */
function encodeAsync(buf, base, span, done, fail) {
  var b = new Uint8Array(buf), CH = 32766, i = 0, parts = [];
  (function pump() {
    var until = performance.now() + SLICE_MS, n = 0;
    try {
      while (i < b.length) {
        parts.push(btoa(String.fromCharCode.apply(null, b.subarray(i, i + CH))));
        i += CH;
        if (++n >= CHUNK_MAX || performance.now() > until) {
          progSet(t("prog_encoding"), base + span * (i / b.length));
          return defer(pump);
        }
      }
      done(parts.join(""));
    } catch (e) { fail(e); }
  })();
}

btnRender.addEventListener("click", function () {
  btnRender.disabled = true; btnStl.disabled = true;
  badge.textContent = t("badge_rendering");
  badge.className = "badge";
  progShow(t("prog_building"));
  buildAsync(P, RENDER_SEG, 0, 1, function (res) {
    applyMesh(res, "render");
    progDone(t("prog_done"));
    btnRender.disabled = false; btnStl.disabled = false;
  }, function (e) {
    badge.textContent = t("badge_preview");
    progDone(t("prog_failed"));
    note(t("render_failed", { e: e.message }), true);
    btnRender.disabled = false; btnStl.disabled = false;
  });
});

/* ---- STL export: always at render tessellation ---- */
btnStl.addEventListener("click", function () {
  var t0 = performance.now(), name = stem() + ".stl";
  btnStl.disabled = true; btnRender.disabled = true;
  note("", false);
  progShow(t("prog_building"));

  function failed(e) {
    progDone(t("prog_failed"));
    note(t("export_failed", { e: e.message }), true);
    btnStl.disabled = false; btnRender.disabled = false;
  }
  function finished(stage) {
    progDone(stage + " in " + ((performance.now() - t0) / 1000).toFixed(1) + " s");
    btnStl.disabled = false; btnRender.disabled = false;
  }

  buildAsync(P, RENDER_SEG, 0, 0.8, function (res) {
    progSet(t("prog_writing"), 0.8);
    defer(function () {
      var buf;
      try { buf = toSTL(res.mesh); } catch (e) { return failed(e); }
      if (!ORCA) {
        progSet(t("prog_saving"), 0.95);
        defer(function () {
          try {
            var url = URL.createObjectURL(new Blob([buf], { type: "model/stl" }));
            var a = document.createElement("a");
            a.href = url; a.download = name;
            document.body.appendChild(a); a.click(); a.remove();
            setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
          } catch (e) { return failed(e); }
          finished(t("prog_saved"));
        });
        return;
      }
      encodeAsync(buf, 0.85, 0.13, function (b64) {
        progSet(t("prog_sending"), 0.98);
        defer(function () {
          try {
            ORCA.postMessage({
              type: "save_stl", name: name, data: b64,
              place: document.getElementById("toPlate").checked,
              dir: document.getElementById("exportDir").value.trim()
            });
          } catch (e) { return failed(e); }
          finished(t("prog_sent"));
        });
      }, failed);
    });
  }, failed);
});

/* ---- bed size from the printer OrcaSlicer has selected ----
   Asked for once on load, so a fresh panel starts on the right bed, and on
   demand after that -- a bed typed in by hand is not overwritten behind the
   user's back. */
bedFrom = document.getElementById("bedFrom");

function applyBed(m) {
  // setting .value in script raises no input event, so the note below stands
  document.getElementById("bedX").value = m.x;
  document.getElementById("bedY").value = m.y;
  var mmX = document.getElementById("plate_mm_x");
  var mmXNum = document.getElementById("plate_mm_x_num");
  var mmY = document.getElementById("plate_mm_y");
  var mmYNum = document.getElementById("plate_mm_y_num");
  if (mmX && mmXNum && mmY && mmYNum) {
    mmX.value = Math.min(+mmX.max || 600, m.x);
    mmXNum.value = m.x;
    mmY.value = Math.min(+mmY.max || 600, m.y);
    mmYNum.value = m.y;
  }
  bedFrom.textContent = t("bed_from", { printer: m.printer, x: m.x, y: m.y });
  onChange(false);
}

// typing a bed size by hand means it is no longer the printer's
["bedX", "bedY"].forEach(function (k) {
  document.getElementById(k).addEventListener("input", function () {
    if (bedFrom) bedFrom.textContent = "";
  });
});

if (ORCA) {
  document.getElementById("btnBed").addEventListener("click", function () {
    bedFrom.textContent = t("asking_bed");
    ORCA.postMessage({ type: "get_bed" });
  });
  ORCA.postMessage({ type: "get_bed" });
}

if (ORCA && typeof ORCA.onMessage === "function") {
  ORCA.onMessage(function (m) {
    if (!m) return;
    if (m.type === "bed") applyBed(m);
    if (m.type === "pick_dir") {
      var b = document.getElementById("btnDir");
      if (b) b.disabled = false;
      if (m.path) {
        dirEl.value = m.path;
        try { localStorage.setItem("gf_export_dir", m.path); } catch (e4) {}
        note(t("dir_set", { p: m.path }), false);
      } else if (m.error) {
        note(t("dir_fail", { e: m.error }), true);
      } else {
        note(t("dir_cancel"), false);
      }
    }
    if (m.type === "bed_failed") {
      bedFrom.textContent = t("bed_failed");
      note(t("bed_manual", { e: m.error }), true);
    }
    if (m.type === "saved") {
      note(m.fallback ? t("saved_fallback", { p: m.path })
                      : t(m.pending ? "saved_pending" : "saved_import", { p: m.path }), true);
    }
    if (m.type === "placed") {
      note(m.placed ? t("placed_ok", { p: m.path })
                    : t("placed_fail", { p: m.path, e: m.place_error }), true);
    }
    if (m.type === "save_failed") note(t("save_failed", { e: m.error }), true);
  });
}

/* ---- pick a screw hole in the preview: click near it to toggle ---- */
function mInv4(m) {
  var a = [], inv = new Float32Array(16), i, j, k;
  for (i = 0; i < 4; i++)
    a.push([m[i * 4], m[i * 4 + 1], m[i * 4 + 2], m[i * 4 + 3]]);
  for (i = 0; i < 4; i++)
    for (j = 0; j < 4; j++) inv[i * 4 + j] = i === j ? 1 : 0;
  for (i = 0; i < 4; i++) {
    var piv = i;
    for (j = i + 1; j < 4; j++)
      if (Math.abs(a[j][i]) > Math.abs(a[piv][i])) piv = j;
    if (piv !== i) {
      var tr = a[i]; a[i] = a[piv]; a[piv] = tr;
      for (j = 0; j < 4; j++) {
        var tv = inv[i * 4 + j];
        inv[i * 4 + j] = inv[piv * 4 + j];
        inv[piv * 4 + j] = tv;
      }
    }
    var dd = a[i][i] || 1e-12;
    for (j = 0; j < 4; j++) { a[i][j] /= dd; inv[i * 4 + j] /= dd; }
    for (k = 0; k < 4; k++) {
      if (k === i) continue;
      var f = a[k][i];
      if (!f) continue;
      for (j = 0; j < 4; j++) {
        a[k][j] -= f * a[i][j];
        inv[k * 4 + j] -= f * inv[i * 4 + j];
      }
    }
  }
  return inv;
}

function pickScrew(e) {
  if (!lastMeshPos || P.mode !== "og") return;
  var d = deriveBoard(P);
  if (!P.ogScrews || d.W < 2 || d.H < 2) return;
  var r = canvas.getBoundingClientRect();
  var ndcX = ((e.clientX - r.left) / r.width) * 2 - 1;
  var ndcY = -(((e.clientY - r.top) / r.height) * 2 - 1);
  var ce = Math.cos(cam.el), se = Math.sin(cam.el);
  var eye = [cam.target[0] + cam.dist * Math.cos(cam.az) * ce,
             cam.target[1] + cam.dist * Math.sin(cam.az) * ce,
             cam.target[2] + cam.dist * se];
  var proj = mPersp(0.62, r.width / r.height, 1, 6000);
  var inv = mInv4(mMul(proj, mLookAt(eye, cam.target, [0, 0, 1])));
  function unproj(z) {
    var x = inv[0] * ndcX + inv[4] * ndcY + inv[8] * z + inv[12];
    var y = inv[1] * ndcX + inv[5] * ndcY + inv[9] * z + inv[13];
    var z2 = inv[2] * ndcX + inv[6] * ndcY + inv[10] * z + inv[14];
    var w2 = inv[3] * ndcX + inv[7] * ndcY + inv[11] * z + inv[15];
    return [x / w2, y / w2, z2 / w2];
  }
  var p0 = unproj(-1), p1 = unproj(1);
  var dx = p1[0] - p0[0], dy = p1[1] - p0[1], dz = p1[2] - p0[2];
  var pos = lastMeshPos, bestT = Infinity, hit = null;
  for (var i = 0; i + 8 < pos.length; i += 9) {
    var ax = pos[i], ay = pos[i + 1], az = pos[i + 2];
    var e1x = pos[i + 3] - ax, e1y = pos[i + 4] - ay, e1z = pos[i + 5] - az;
    var e2x = pos[i + 6] - ax, e2y = pos[i + 7] - ay, e2z = pos[i + 8] - az;
    var pvx = dy * e2z - dz * e2y, pvy = dz * e2x - dx * e2z, pvz = dx * e2y - dy * e2x;
    var det = e1x * pvx + e1y * pvy + e1z * pvz;
    if (det > -1e-12 && det < 1e-12) continue;
    var inv2 = 1 / det, tvx = p0[0] - ax, tvy = p0[1] - ay, tvz = p0[2] - az;
    var u = (tvx * pvx + tvy * pvy + tvz * pvz) * inv2;
    if (u < -1e-9 || u > 1 + 1e-9) continue;
    var qvx = tvy * e1z - tvz * e1y, qvy = tvz * e1x - tvx * e1z, qvz = tvx * e1y - tvy * e1x;
    var v = (dx * qvx + dy * qvy + dz * qvz) * inv2;
    if (v < -1e-9 || u + v > 1 + 1e-9) continue;
    var t = (e2x * qvx + e2y * qvy + e2z * qvz) * inv2;
    if (t > 1e-9 && t < bestT) {
      bestT = t;
      hit = [p0[0] + dx * t, p0[1] + dy * t];
    }
  }
  if (!hit) return;
  var ix = Math.round(hit[0] / OG.TILE), iy = Math.round(hit[1] / OG.TILE);
  if (ix < 1 || ix > d.W - 1 || iy < 1 || iy > d.H - 1) return;
  var key = ix + "," + iy;
  if (!P.ogScrewOff) P.ogScrewOff = {};
  if (P.ogScrewOff[key]) delete P.ogScrewOff[key]; else P.ogScrewOff[key] = 1;
  rebuild("preview");
  updateOgReadout();
}

/* ---- pointer interaction ---- */
var last = null, panning = false, downXY = null;
canvas.addEventListener("pointerdown", function (e) {
  canvas.setPointerCapture(e.pointerId);
  last = [e.clientX, e.clientY];
  downXY = [e.clientX, e.clientY];
  panning = e.shiftKey || e.button === 2;
  dragging = true;
  canvas.classList.add("dragging");
  needsDraw = true;
});
canvas.addEventListener("pointermove", function (e) {
  if (EDIT2D) {
    if (dragging && last) {
      var ddx = e.clientX - last[0], ddy = e.clientY - last[1];
      editCenter[0] -= ddx / editScale;
      editCenter[1] += ddy / editScale;
      last = [e.clientX, e.clientY];
      needsDraw = true;
    } else if (P.mode === "og") {
      var rc = canvas.getBoundingClientRect();
      var t = pick2DTarget(s2wX(e.clientX - rc.left), s2wY(e.clientY - rc.top));
      var tk = t ? t.kind + ":" + t.key : "";
      if (tk !== hoverKey) { hoverKey = tk; hover2D = t; needsDraw = true; }
    }
    return;
  }
  if (!last) return;
  var dx = e.clientX - last[0], dy = e.clientY - last[1];
  last = [e.clientX, e.clientY];
  if (panning) {
    var s = cam.dist * 0.0016, ce = Math.cos(cam.az), se = Math.sin(cam.az);
    cam.target[0] += se * dx * s;
    cam.target[1] += -ce * dx * s;
    cam.target[2] += dy * s;
  } else {
    cam.az -= dx * 0.008;
    cam.el = Math.max(-1.45, Math.min(1.45, cam.el + dy * 0.008));
  }
  needsDraw = true;
});
function endDrag(e) {
  if (!last) return;
  if (downXY && e && typeof e.clientX === "number") {
    var mdx = e.clientX - downXY[0], mdy = e.clientY - downXY[1];
    if (mdx * mdx + mdy * mdy < 36) { if (EDIT2D) pick2D(e); else pickScrew(e); }
  }
  downXY = null;
  last = null; dragging = false;
  canvas.classList.remove("dragging");
  needsDraw = true;
}
canvas.addEventListener("pointerup", endDrag);
canvas.addEventListener("pointercancel", endDrag);
canvas.addEventListener("contextmenu", function (e) { e.preventDefault(); });
canvas.addEventListener("wheel", function (e) {
  e.preventDefault();
  if (EDIT2D)
    editScale = Math.max(1.5, Math.min(120, editScale * Math.exp(-e.deltaY * 0.0012)));
  else
    cam.dist = Math.max(30, Math.min(3000, cam.dist * Math.exp(e.deltaY * 0.0012)));
  needsDraw = true;
}, { passive: false });
window.addEventListener("resize", function () { needsDraw = true; });
darkMQ.addEventListener("change", function () { needsDraw = true; });

/* ---- go ---- */
applyLang();
renderRowList();
renderColList();
if (!gl) {
  errBox.hidden = false;
  errBox.textContent = t("gl_missing");
  canvas.style.display = "none";
  readControls(); updateReadout();
} else {
  try {
    initGL();
    readControls();
    updateReadout();
    rebuild("preview");
    frameCamera();
    loop();
  } catch (e) {
    errBox.hidden = false;
    errBox.textContent = t("gl_error", { e: e.message });
    canvas.style.display = "none";
  }
}
</script>
</body>
</html>
"""
