import os
import threading
import webbrowser

import uvicorn


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    if os.environ.get("OPEN_BROWSER", "1") == "1":
        threading.Timer(1.5, webbrowser.open, [f"http://localhost:{port}/"]).start()
    uvicorn.run("supsearch.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
