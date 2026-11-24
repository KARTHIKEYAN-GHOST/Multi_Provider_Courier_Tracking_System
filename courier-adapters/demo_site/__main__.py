"""Run the demo courier website: ``uv run python -m demo_site`` (DEMO_SITE_HOST / DEMO_SITE_PORT)."""

import os

from .server import make_server

if __name__ == "__main__":
    host, port = os.environ.get("DEMO_SITE_HOST", "127.0.0.1"), int(os.environ.get("DEMO_SITE_PORT", "8090"))
    server = make_server(host, port)
    print(f"demo courier website on http://{host}:{port}/track?number=DEMO-OK-1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
