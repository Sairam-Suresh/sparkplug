"""Main CLI entrypoint for SparkPlug daemon."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .config import load_config
from .web import create_app


def setup_logging(log_level: str) -> None:
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SparkPlug: Wake-on-LAN and SSH shutdown daemon for Caddy"
    )
    parser.add_argument(
        "-c",
        "--config",
        dest="config_path",
        default=None,
        help="Path to YAML configuration file (default: /etc/sparkplug/config.yaml or $SPARKPLUG_CONFIG_PATH)",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config_path)
    except Exception as err:
        print(f"Error loading SparkPlug configuration: {err}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.sparkplug.log_level)
    logger = logging.getLogger("sparkplug")
    logger.info(
        "Starting SparkPlug on %s:%d with %d configured host(s)",
        config.sparkplug.host,
        config.sparkplug.port,
        len(config.hosts),
    )

    app = create_app(config)
    uvicorn.run(
        app,
        host=config.sparkplug.host,
        port=config.sparkplug.port,
        log_level=config.sparkplug.log_level.lower(),
    )


if __name__ == "__main__":
    main()

