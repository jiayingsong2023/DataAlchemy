import logging
import sys
import warnings

# Suppress annoying third-party warnings (especially from jieba on Python 3.12)
warnings.filterwarnings("ignore", category=SyntaxWarning, module="jieba")
warnings.filterwarnings("ignore", category=UserWarning, module="jieba")
warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

from config import LOG_FORMAT, LOG_LEVEL


def setup_logger(name: str = "DataAlchemy") -> logging.Logger:
    """
    Setup and return a logger that streams to stdout (K8s Best Practice).
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(LOG_LEVEL)
    formatter = logging.Formatter(LOG_FORMAT)

    # Console Handler (Cloud-Native: let K8s handle log persistence)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger

# Create a default logger instance
logger = setup_logger()
