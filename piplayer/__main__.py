"""Entry point: python3 -m piplayer [options]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from aiohttp import web

from . import __version__
from . import hw as hwmod
from .config import Store
from .engine import Engine
from .media import MediaLibrary
from .web import build_app

log = logging.getLogger("piplayer")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="piplayer", description="Network-controlled video player")
    p.add_argument("--data", default=os.environ.get("PIPLAYER_DATA", "./data"),
                   help="data directory (state, media, cache) [env PIPLAYER_DATA]")
    p.add_argument("--host", default=os.environ.get("PIPLAYER_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PIPLAYER_PORT", "8080")))
    p.add_argument("--backend", choices=("auto", "gst", "sim"),
                   default=os.environ.get("PIPLAYER_BACKEND", "auto"),
                   help="renderer: gst (GStreamer, real output), sim (simulated), auto")
    p.add_argument("--sink", choices=("kms", "fake", "window"),
                   default=os.environ.get("PIPLAYER_SINK", "kms"),
                   help="gst output: kms (HDMI via DRM/KMS), window (desktop), fake (headless)")
    p.add_argument("--no-autoplay", action="store_true", help="don't start the default playlist")
    p.add_argument("--log-level", default=os.environ.get("PIPLAYER_LOG", "info"))
    return p.parse_args()


def make_backend_factory(args, hw: dict):
    kind = args.backend
    if kind == "auto":
        kind = "gst" if hw["is_pi"] else "sim"
    if kind == "sim":
        from .backend_sim import SimBackend

        log.info("using simulated renderer (no video output)")
        return SimBackend
    if args.sink == "kms":
        from .backend_gst import configure_environment  # noqa: F401 (env must precede GL init)

        out = hwmod.pick_output()
        log.info("HDMI output: %s", out or "none found")
        configure_environment(out)
    from .backend_gst import GstBackend

    return lambda settings, hw_, emit: GstBackend(settings, hw_, emit, sink=args.sink)


async def main_async(args) -> None:
    data = Path(args.data).resolve()
    hw = hwmod.detect()
    log.info("PiPlayer %s on %s; data in %s", __version__, hw["model"] or "non-Pi host", data)
    store = Store(data / "state.json")
    library = MediaLibrary(data / "media", data / "cache", hw)
    engine = Engine(store, library, make_backend_factory(args, hw), hw)
    app = build_app(store, library, engine, hw)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    log.info("web UI on http://%s:%d/", args.host, args.port)
    await engine.start(autoplay=not args.no_autoplay)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("shutting down")
    await runner.cleanup()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
