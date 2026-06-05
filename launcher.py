"""Entry point for PyInstaller-bundled chat-analyzer backend.
Supports --config=<path> argument (passed by Tauri shell) for
finding config.json in the macOS app bundle's Resources directory.
"""
import sys
import os

# Write debug log to temp file
_debug_log = "/tmp/chat-analyzer-debug.log"
def _debug(msg):
    with open(_debug_log, "a") as f:
        f.write(f"{msg}\n")

_debug(f"=== launcher.py starting ===")
_debug(f"sys.argv: {sys.argv}")
_debug(f"frozen: {getattr(sys, 'frozen', False)}")
_debug(f"cwd before: {os.getcwd()}")

# Parse --config=<path> argument before anything else
config_override = None
for a in sys.argv[1:]:
    if a.startswith("--config="):
        config_override = a.split("=", 1)[1]
        _debug(f"config_override (equals): {config_override}")
        break

if not config_override:
    for i, a in enumerate(sys.argv):
        if a == "--config" and i + 1 < len(sys.argv):
            config_override = sys.argv[i + 1]
            _debug(f"config_override (space): {config_override}")
            break

_debug(f"final config_override: {config_override}")

# PyInstaller sets sys._MEIPASS to the temp extraction dir
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = sys._MEIPASS
    _debug(f"BUNDLE_DIR (MEIPASS): {BUNDLE_DIR}")
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    _debug(f"BUNDLE_DIR (source): {BUNDLE_DIR}")

# If --config was provided, use its directory as cwd
if config_override:
    cfg_dir = os.path.dirname(os.path.abspath(config_override))
    _debug(f"chdir to: {cfg_dir}")
    os.chdir(cfg_dir)
else:
    _debug(f"chdir to BUNDLE_DIR: {BUNDLE_DIR}")
    os.chdir(BUNDLE_DIR)

_debug(f"cwd after chdir: {os.getcwd()}")
_debug(f"config.json exists: {os.path.exists('config.json')}")
_debug(f"templates/index.html exists: {os.path.exists('templates/index.html')}")

_debug("about to import server...")
try:
    from server import app, _load_server_config
    _debug("server imported successfully")
except Exception as e:
    _debug(f"IMPORT ERROR: {e}")
    raise

_debug("about to import sys.stdout.reconfigure...")
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception as e:
    _debug(f"reconfigure error (non-fatal): {e}")

_debug("about to call _load_server_config...")
try:
    cfg = _load_server_config()
    _debug(f"config loaded: {cfg}")
except Exception as e:
    _debug(f"config load error: {e}")
    raise

if __name__ == '__main__':
    lan_enabled = cfg.get('lan_enabled', False)
    host = '0.0.0.0' if lan_enabled else '127.0.0.1'
    _debug(f"starting Flask on {host}:8899, debug=False")
    print("Backend ready → http://localhost:8899", flush=True)
    app.run(host=host, port=8899, debug=False)
