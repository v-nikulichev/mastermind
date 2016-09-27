#!/usr/bin/python
# encoding: utf-8
import logging
import signal
import sys
from time import sleep

# NB: pool should be initialized before importing
# any of cocaine-framework-python modules to avoid
# tornado ioloop dispatcher issues
import monitor_pool

from cocaine.worker import Worker

sys.path.append('/usr/lib')

import elliptics

import log
log.setup_logger('mm_logging')

# storage should be imported before balancer
# TODO: remove this dependency
import storage
import balancer
from db.mongo.pool import MongoReplicaSetClient
import external_storage
import helpers
import history
import infrastructure
import jobs
import couple_records
import minions
import node_info_updater
from planner import Planner
from config import config
from manual_locks import manual_locker

logger = logging.getLogger()
i = iter(xrange(100))
logger.info("trace %d" % (i.next()))


def term_handler(signo, frame):
    # required to guarantee execution of cleanup functions registered
    # with atexit.register
    sys.exit(0)


signal.signal(signal.SIGTERM, term_handler)

nodes = config.get('elliptics', {}).get('nodes', []) or config["elliptics_nodes"]
logger.info("config: %s" % str(nodes))

logger.info("trace %d" % (i.next()))
log = elliptics.Logger(str(config["dnet_log"]), config["dnet_log_mask"])

node_config = elliptics.Config()
node_config.io_thread_num = config.get('io_thread_num', 1)
node_config.nonblocking_io_thread_num = config.get('nonblocking_io_thread_num', 1)
node_config.net_thread_num = config.get('net_thread_num', 1)

logger.info(
    'Node config: io_thread_num {0}, nonblocking_io_thread_num {1}, '
    'net_thread_num {2}'.format(
        node_config.io_thread_num,
        node_config.nonblocking_io_thread_num,
        node_config.net_thread_num
    )
)

n = elliptics.Node(log, node_config)

logger.info("trace %d" % (i.next()))

addresses = []
for node in nodes:
    try:
        addresses.append(elliptics.Address(
            host=str(node[0]), port=node[1], family=node[2]))
    except Exception as e:
        logger.error('Failed to connect to storage node: {0}:{1}:{2}'.format(
            node[0], node[1], node[2]))
        pass

try:
    n.add_remotes(addresses)
except Exception as e:
    logger.error('Failed to connect to any elliptics storage node: {0}'.format(
        e))
    raise ValueError('Failed to connect to any elliptics storage node')

logger.info("trace %d" % (i.next()))
meta_node = elliptics.Node(log, node_config)

addresses = []
for node in config["metadata"]["nodes"]:
    try:
        addresses.append(elliptics.Address(
            host=str(node[0]), port=node[1], family=node[2]))
    except Exception as e:
        logger.error('Failed to connect to meta node: {0}:{1}:{2}'.format(
            node[0], node[1], node[2]))
        pass

logger.info('Connecting to meta nodes: {0}'.format(config["metadata"]["nodes"]))

try:
    meta_node.add_remotes(addresses)
except Exception as e:
    logger.error('Failed to connect to any elliptics meta storage node: {0}'.format(
        e))
    raise ValueError('Failed to connect to any elliptics storage META node')


wait_timeout = config.get('wait_timeout', 5)
logger.info('sleeping for wait_timeout for nodes to collect data ({0} sec)'.format(wait_timeout))
sleep(wait_timeout)

meta_wait_timeout = config['metadata'].get('wait_timeout', 5)

meta_session = elliptics.Session(meta_node)
meta_session.set_timeout(meta_wait_timeout)
meta_session.add_groups(list(config["metadata"]["groups"]))
logger.info("trace %d" % (i.next()))
n.meta_session = meta_session

mrsc_options = config['metadata'].get('options', {})

meta_db = None
if config['metadata'].get('url'):
    meta_db = MongoReplicaSetClient(config['metadata']['url'], **mrsc_options)


logger.info("trace %d" % (i.next()))
logger.info("before creating worker")
W = Worker(
    disown_timeout=config.get('disown_timeout', 2),
    heartbeat_timeout=config.get('heartbeat_timeout', 5),
)
logger.info("after creating worker")


b = balancer.Balancer(n, meta_db)

logger.info("after creating balancer")


def init_infrastructure(jf, ghf):
    infstruct = infrastructure.infrastructure
    infstruct.init(n, jf, ghf)
    helpers.register_handle(W, infstruct.shutdown_node_cmd)
    helpers.register_handle(W, infstruct.start_node_cmd)
    helpers.register_handle(W, infstruct.disable_node_backend_cmd)
    helpers.register_handle(W, infstruct.enable_node_backend_cmd)
    helpers.register_handle(W, infstruct.reconfigure_node_cmd)
    helpers.register_handle(W, infstruct.recover_group_cmd)
    helpers.register_handle(W, infstruct.defrag_node_backend_cmd)
    helpers.register_handle(W, infstruct.search_history_by_path)
    b._set_infrastructure(infstruct)
    return infstruct


def init_node_info_updater(jf, crf, statistics):
    logger.info("trace node info updater %d" % (i.next()))
    niu = node_info_updater.NodeInfoUpdater(
        node=n,
        job_finder=jf,
        couple_record_finder=crf,
        prepare_namespaces_states=True,
        prepare_flow_stats=True,
        statistics=statistics)
    logger.info('node info updater: starting')
    niu.start()
    logger.info('node info updater: started')
    helpers.register_handle(W, niu.force_nodes_update)
    helpers.register_handle(W, niu.force_update_namespaces_states)
    helpers.register_handle(W, niu.force_update_flow_stats)

    return niu


def init_statistics():
    helpers.register_handle(W, b.statistics.get_groups_tree)
    helpers.register_handle(W, b.statistics.get_couple_statistics)
    return b.statistics


def init_minions():
    m = minions.Minions(n)
    helpers.register_handle(W, m.get_command)
    helpers.register_handle(W, m.get_commands)
    helpers.register_handle(W, m.execute_cmd)
    helpers.register_handle(W, m.terminate_cmd)
    return m


def init_planner(job_processor, niu):
    planner = Planner(n.meta_session, meta_db, niu, job_processor)
    helpers.register_handle(W, planner.restore_group)
    helpers.register_handle(W, planner.move_group)
    helpers.register_handle(W, planner.move_groups_from_host)
    helpers.register_handle(W, planner.convert_external_storage_to_groupset)
    helpers.register_handle(W, planner.restore_groups_from_path)
    helpers.register_handle(W, planner.ttl_cleanup)
    return planner


def init_job_finder():
    if not config['metadata'].get('jobs', {}).get('db'):
        logger.error(
            'Job finder metadb is not set up '
            '("metadata.jobs.db" key), will not be initialized'
        )
        return None
    jf = jobs.JobFinder(meta_db)
    helpers.register_handle(W, jf.get_job_list)
    helpers.register_handle(W, jf.get_job_status)
    helpers.register_handle(W, jf.get_jobs_status)
    return jf


def init_external_storage_meta():
    if not config['metadata'].get('external_storage', {}).get('db'):
        logger.error(
            'External storage metadb is not set up '
            '("metadata.external_storage.db" key), will not be initialized'
        )
    external_storage_meta = external_storage.ExternalStorageMeta(meta_db)
    helpers.register_handle(W, external_storage_meta.get_external_storage_mapping)
    return external_storage_meta


def init_couple_record_finder():
    if not config['metadata'].get('couples', {}).get('db'):
        msg = (
            'Couple finder metadb is not set up '
            '("metadata.couples.db" key), will not be initialized'
        )
        logger.error(msg)
        raise RuntimeError(msg)
    crf = couple_records.CoupleRecordFinder(meta_db)
    return crf


def init_group_history_finder():
    if not config['metadata'].get('history', {}).get('db'):
        logger.error(
            'History finder metadb is not set up '
            '("metadata.history.db" key), will not be initialized'
        )
        return None
    ghf = history.GroupHistoryFinder(meta_db)
    return ghf


def init_job_processor(jf, minions, niu, external_storage_meta, couple_record_finder):
    if jf is None:
        logger.error(
            'Job processor will not be initialized because '
            'job finder is not initialized'
        )
        return None
    j = jobs.JobProcessor(
        jf,
        n,
        meta_db,
        niu,
        minions,
        external_storage_meta=external_storage_meta,
        couple_record_finder=couple_record_finder,
    )
    j = jobs.JobProcessor(jf, n, meta_db, niu, minions)
    helpers.register_handle(W, j.create_job)
    helpers.register_handle(W, j.cancel_job)
    helpers.register_handle(W, j.approve_job)
    helpers.register_handle(W, j.stop_jobs)
    helpers.register_handle(W, j.retry_failed_job_task)
    helpers.register_handle(W, j.skip_failed_job_task)
    helpers.register_handle(W, j.restart_failed_to_start_job)
    helpers.register_handle(W, j.build_lrc_groups)
    helpers.register_handle(W, j.add_groupset_to_couple)
    return j


def init_manual_locker(manual_locker):
    helpers.register_handle(W, manual_locker.host_acquire_lock)
    helpers.register_handle(W, manual_locker.host_release_lock)
    return manual_locker


try:
    jf = init_job_finder()
    logger.info('Job finder module initialized')
    external_storage_meta = init_external_storage_meta()
    logger.info('External storage meta module initialized')
    crf = init_couple_record_finder()
    logger.info('Couple record finder module initialized')
    ghf = init_group_history_finder()
    logger.info('Group history finder module initialized')
    io = init_infrastructure(jf, ghf)
    logger.info('Infrastructure module initialized')
    niu = init_node_info_updater(jf, crf, b.statistics)
    logger.info('Node info updater module initialized')
    b.niu = niu
    b.start()
    init_statistics()
    logger.info('Statistics module initialized')
    m = init_minions()
    logger.info('Minions module initialized')
    j = init_job_processor(jf, m, niu, external_storage_meta, crf)
    logger.info('Job processor module initialized')
    if j:
        po = init_planner(j, niu)
        j.planner = po
    else:
        po = None
    ml = init_manual_locker(manual_locker)
except Exception:
    logger.exception('Module initialization failed')
    raise


for handler in balancer.handlers(b):
    logger.info("registering bounded function %s" % handler)
    if getattr(handler, '__wne', False):
        helpers.register_handle_wne(W, handler)
    else:
        helpers.register_handle(W, handler)

logger.info('activating timed queues')
try:
    tq_to_activate = [io, b.niu, m, j, po, b]
    for tqo in tq_to_activate:
        if tqo is None:
            continue
        tqo._start_tq()
except Exception as e:
    logger.error('failed to activate timed queue: {0}'.format(e))
    raise
logger.info('finished activating timed queues')

logger.info("Starting worker")
W.run()
logger.info("Initialized")
