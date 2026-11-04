# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Benjamin Stanley Frohman
"""`jobbot-enterprise` command: run the server, or list installed plugins."""
from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> None:
    from . import __version__
    ap = argparse.ArgumentParser(prog="jobbot-enterprise", description="Self-hosted hiring engine (Apache-2.0)")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--list-plugins", action="store_true", help="print installed plugins as JSON and exit")
    a = ap.parse_args(argv)
    if a.list_plugins:
        from . import plugins
        reg = plugins.load()
        print(json.dumps({"plugins": reg.describe(), "load_errors": reg.errors}, indent=2))
        return
    import uvicorn
    uvicorn.run("jobbot_enterprise.main:get_app", factory=True, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
