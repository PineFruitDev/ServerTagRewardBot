"""Contextual logging service.

Every class grabs its own named logger so log lines read like
[timestamp] [Bot] INFO message, matching the rest of the stack.
"""

import logging
import os
import sys

_FORMAT = "[%(asctime)s] [%(name)s] %(levelname)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def get_logger(context: str) -> logging.Logger:
    """Return a logger tagged with the given context (class or module name)."""
    logger = logging.getLogger(context)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
        logger.addHandler(handler)
        level = logging.DEBUG if os.getenv("ENVIRONMENT") == "development" else logging.INFO
        logger.setLevel(level)
        logger.propagate = False

    return logger
