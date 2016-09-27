#!/usr/bin/env python
import logging
import signal
import sys

from cocaine.worker import Worker

import log

try:
    log.setup_logger('mm_inventory_logging')
    logger = logging.getLogger('mm.init')
except Exception:
    log.setup_logger()
    logger = logging.getLogger('mm.init')
    logger.warn(
        'mm_inventory_logging is not set up properly in '
        'cocaine.conf, fallback to default logging service'
    )

from config import config
# TODO: rename inv module to 'inventory' when switched to using inventory worker
import inv as inventory
import helpers


def init_inventory_worker(worker):
    helpers.register_handle_wne(worker, inventory.Inventory.get_dc_by_host)


DEFAULT_DISOWN_TIMEOUT = 2

if __name__ == '__main__':

    def term_handler(signo, frame):
        # required to guarantee execution of cleanup functions registered
        # with atexit.register
        sys.exit(0)

    signal.signal(signal.SIGTERM, term_handler)

    logger.info("before creating inventory worker")
    worker = Worker(disown_timeout=config.get('disown_timeout', DEFAULT_DISOWN_TIMEOUT))
    logger.info("after creating inventory worker")

    init_inventory_worker(worker)

    logger.info("Starting inventory worker")
    worker.run()
    logger.info("Inventory worker stopped")
