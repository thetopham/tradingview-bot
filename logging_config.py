# logging_config.py
"""Sets up rotating file and console logging for the bot."""

import logging
import os
from logging.handlers import RotatingFileHandler

def setup_logging(log_file=None, log_level=None):
    log_file = log_file or os.getenv('LOG_FILE', '/tmp/tradingview_projectx_bot.log')
    log_level = log_level or os.getenv('LOG_LEVEL', 'INFO').upper()
    file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
    file_handler.setLevel(getattr(logging, log_level))
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(getattr(logging, log_level))
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    root_logger.setLevel(getattr(logging, log_level))
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

