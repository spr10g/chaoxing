from loguru import logger
from tqdm import tqdm
import sys
import os

tqdm_stream = sys.stderr

def tqdm_sink(msg):
    tqdm.write(msg.rstrip(), file=tqdm_stream)
    tqdm_stream.flush()

logger.remove()
logger.add(tqdm_sink, colorize=True, enqueue=False)
logger.add(os.path.join(os.environ.get('CHAOXING_DESKTOP_DATA', '.'), 'chaoxing.log'), rotation='10 MB', level='INFO')
