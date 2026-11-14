"""Small local event log; no player names, Steam IDs or complete GSI payloads."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import time

_logger = logging.getLogger('cs2toolkit.flash')
_logger.setLevel(logging.INFO)
_logger.propagate = False


def record(event, **details):
    try:
        if not _logger.handlers:
            directory = os.path.join(os.environ.get('LOCALAPPDATA', os.getcwd()), 'CS2Toolkit')
            os.makedirs(directory, exist_ok=True)
            handler = RotatingFileHandler(os.path.join(directory, 'flashfix-events.jsonl'),
                                          maxBytes=512000, backupCount=1, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'))
            _logger.addHandler(handler)
        _logger.info(json.dumps({'time': time.time(), 'monotonic': time.monotonic(),
                                'event': event, **details}, ensure_ascii=False))
    except (OSError, ValueError, TypeError):
        pass
