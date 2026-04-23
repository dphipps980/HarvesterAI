"""
HarvesterAI - Desktop launcher entry point (used by PyInstaller EXE).
Starts the uvicorn server and opens the app in the default browser.
"""
import sys
import threading
import time
import webbrowser

import uvicorn
from app import app as fastapi_app  # direct import so PyInstaller bundles it

PORT = 7860
HOST = "127.0.0.1"
URL  = f"http://{HOST}:{PORT}"


def _open_browser():
    """Wait for the server to be ready, then open the browser."""
    time.sleep(1.5)
    webbrowser.open(URL)


if __name__ == "__main__":
    print("=" * 50)
    print("  HarvesterAI")
    print(f"  Opening at {URL}")
    print("  Close this window to stop the server.")
    print("=" * 50)

    t = threading.Thread(target=_open_browser, daemon=True)
    t.start()

    uvicorn.run(
        fastapi_app,
        host=HOST,
        port=PORT,
        workers=1,
        log_level="info",
    )
