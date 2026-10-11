"""Run with: python -m bubblecam --config config.yaml"""

import argparse
import logging

import uvicorn

from .config import load_config
from .core import App
from .web.app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="bubblecam")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--synthetic", action="store_true", help="use a fake tank instead of the camera")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.synthetic:
        cfg.camera.source = "synthetic"

    core = App(cfg)
    core.start()

    try:
        uvicorn.run(create_app(core), host=cfg.web.host, port=cfg.web.port, log_level="warning")
    finally:
        core.stop()


if __name__ == "__main__":
    main()
