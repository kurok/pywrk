#!/usr/bin/env python3
"""A trivial HTTP target so the action can be exercised in CI without a network.

Only used by the action's own dogfooding workflow. ``--delay`` exists so the
workflow can prove the threshold gate actually fails when it should — a gate
that has never been seen to fail is not a gate.
"""

from __future__ import annotations

import argparse
import asyncio

from aiohttp import web


def build_app(delay: float) -> web.Application:
    async def handler(request: web.Request) -> web.Response:
        if delay:
            await asyncio.sleep(delay)
        return web.json_response({"ok": True, "path": request.path})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to stall each response")
    args = parser.parse_args()
    web.run_app(build_app(args.delay), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
