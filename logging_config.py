# logging_config.py
import logging
from logging.handlers import RotatingFileHandler

def setup_logging(log_file='/tmp/tradingview_projectx_bot.log'):
    file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5)
    file_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Remove any default handlers
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    root_logger.setLevel(logging.INFO)

    # Optionally mute Flask/Werkzeug HTTP logs
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
