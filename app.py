"""Start the loopback-only prototype using the existing Python environment."""
from pathlib import Path
import sys
from pm_app.web import serve

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    try:
        serve(root, root / "app_data")
    except OSError as exc:
        print("Cannot start the local app. Port 8765 may be in use, or the data folder is not writable.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        sys.exit(1)
