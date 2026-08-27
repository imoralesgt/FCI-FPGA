"""Application logging setup. Mirrors NSIL-Counter's logger_config.py: a rotating, gzip-compressed
file handler plus a console handler, both on the root logger."""

import gzip
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import config


def _gzip_namer(name: str) -> str:
    return name + ".gz"


def _gzip_rotator(source: str, dest: str) -> None:
    with open(source, "rb") as f_in, gzip.open(dest, "wb", compresslevel=9) as f_out:
        f_out.writelines(f_in)
    os.remove(source)


def initialize_logging() -> logging.Logger:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.LOG_DIR / "fci_gui.log"

    handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.namer = _gzip_namer
    handler.rotator = _gzip_rotator

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] (%(threadName)s) %(name)s:%(lineno)d - %(message)s"
    )
    handler.setFormatter(formatter)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)

    logger = logging.getLogger("MainBootstrap")
    logger.info(f"Logging initialized -> {log_path}")
    return logger
