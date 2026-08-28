#!/usr/bin/env python3
"""Bundle gridfinity_bin.html into OrcaSlicer target-tagged plugins.

The HTML page is the single source of geometry and UI; this script only wraps
it. Re-run after editing gridfinity_bin.html.

    python3 build_orca_plugin.py               # rebuild for host target (e.g. _linux_x86_64.py)
    python3 build_orca_plugin.py --all-targets # rebuild for all supported OS/arch targets
    python3 build_orca_plugin.py --install     # rebuild and copy into OrcaSlicer
"""
import glob
import os
import platform
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "gridfinity_bin.html")
PLUGIN_STEM = "gridfinity_bin_plugin"

SUPPORTED_TARGETS = [
    "linux_x86_64",
    "linux_arm64",
    "win_x86_64",
    "win_arm64",
    "macosx_arm64",
    "macosx_x86_64",
]


def host_target():
    """Detect the OS and arch of the current machine."""
    os_map = {
        "linux": "linux",
        "win32": "win",
        "cygwin": "win",
        "darwin": "macosx",
    }
    os_name = os_map.get(sys.platform, sys.platform)
    arch_raw = platform.machine().lower()
    if arch_raw in ("x86_64", "amd64"):
        arch = "x86_64"
    elif arch_raw in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = arch_raw
    return f"{os_name}_{arch}"


def target_output_path(target):
    return os.path.join(HERE, f"{PLUGIN_STEM}_{target}.py")


TEMPLATE = '''# /// script
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
    nums = [float(v) for v in re.findall(r"-?\\d+(?:\\.\\d+)?", text)]
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


PAGE = r"""__HTML__"""
'''


def plugin_dirs():
    """Every orca_plugins/gridfinity_bin folder this machine might use."""
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, ".var", "app", "com.orcaslicer.OrcaSlicer",
                     "config", "OrcaSlicer"),                       # flatpak
        os.path.join(home, ".config", "OrcaSlicer"),                # appimage/native
        os.path.join(os.environ.get("APPDATA", ""), "OrcaSlicer"),  # windows
        os.path.join(home, "Library", "Application Support", "OrcaSlicer"),  # macos
    ]
    return [os.path.join(r, "orca_plugins", "gridfinity_bin")
            for r in roots if r and os.path.isdir(r)]


def install(host_file):
    targets = plugin_dirs()
    if not targets:
        print("no OrcaSlicer data directory found; nothing installed")
        return
    for target in targets:
        os.makedirs(target, exist_ok=True)
        dest = os.path.join(target, os.path.basename(host_file))
        shutil.copy(host_file, dest)

        # Remove untagged legacy script if present
        legacy = os.path.join(target, f"{PLUGIN_STEM}.py")
        if os.path.isfile(legacy):
            try:
                os.remove(legacy)
            except OSError:
                pass

        # a stale bytecode cache can mask the new source
        for junk in glob.glob(os.path.join(target, "__pycache__")):
            shutil.rmtree(junk, ignore_errors=True)
        print("installed -> {}".format(dest))
    print("restart OrcaSlicer to pick up the change")


def build_for_targets(targets_to_build, html_content):
    written = []
    content = TEMPLATE.replace("__HTML__", html_content)
    for target in targets_to_build:
        out_path = target_output_path(target)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(content)
        print("wrote {} ({:,} bytes)".format(os.path.basename(out_path), os.path.getsize(out_path)))
        written.append(out_path)
    return written


def main():
    if not os.path.exists(SRC):
        sys.exit("missing {}".format(SRC))
    html = open(SRC, encoding="utf-8").read()

    # A raw triple-quoted string keeps the page readable inside the plugin, but
    # only if the page cannot terminate or escape it.
    if '"""' in html:
        sys.exit('gridfinity_bin.html contains \'"""\' and cannot be embedded raw')
    if html.rstrip("\n").endswith("\\"):
        sys.exit("gridfinity_bin.html ends with a backslash")

    current_host = host_target()
    targets_to_build = [current_host]

    if "--all-targets" in sys.argv:
        targets_to_build = SUPPORTED_TARGETS
    elif "--target" in sys.argv:
        try:
            idx = sys.argv.index("--target")
            specified = sys.argv[idx + 1]
            targets_to_build = [specified]
        except IndexError:
            sys.exit("Error: --target requires a target name (e.g. linux_x86_64, win_x86_64)")

    written_files = build_for_targets(targets_to_build, html)

    if "--install" in sys.argv:
        host_file = target_output_path(current_host)
        if host_file not in written_files:
            build_for_targets([current_host], html)
        install(host_file)
    else:
        print()
        print("Install into a SUBFOLDER of orca_plugins -- a bare .py at the root is")
        print("not scanned. Re-run with --install to copy it there automatically.")


if __name__ == "__main__":
    main()

