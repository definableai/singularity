"""`from src.common.logger import log` — the one logging API app code uses.

The obs capture sink (05) registers here later; call sites never change.
"""

import sys

from loguru import logger

from src.core.asgi import request_id_var


def _patch(record):
    record["extra"].setdefault("request_id", request_id_var.get(""))


logger.remove()
logger.configure(patcher=_patch)
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss.SSS}</green> <level>{level: <7}</level> "
    "<dim>{extra[request_id]}</dim> {message} <dim>{name}:{line}</dim>",
    backtrace=False,
    diagnose=False,
)

log = logger
