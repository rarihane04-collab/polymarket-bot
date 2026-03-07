import sys
from loguru import logger

# Configuration du logger pour la console et les fichiers
logger.remove()
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>")
logger.add("logs/polymarket_bot.log", rotation="10 MB", retention="10 days", level="DEBUG")

def get_logger(name: str):
    return logger.bind(name=name)
