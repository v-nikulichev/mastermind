# encoding: utf-8
import copy
from contextlib import contextmanager
import itertools
import json
import logging
import operator
import re
import time
import traceback

import elliptics
import msgpack
# from mastermind.service import ReconnectableService
from tornado.ioloop import IOLoop

from config import config
from db.mongo.pool import Collection
import helpers as h
import infrastructure
import inventory
import jobs.job
import keys
from mastermind_core.response import CachedGzipResponse
from mastermind_core.helpers import gzip_compress
import monitor
import statistics
import storage
import timed_queue
from timer import periodic_timer
from sync import sync_manager
from sync.error import LockError, LockAlreadyAcquiredError


logger = logging.getLogger('mm.balancer')

logger.info('balancer.py')

CONFIG_REMOTES = config.get('elliptics', {}).get('nodes', [])


class Balancer(object):
    # TODO: remove cycle dependency for Statistics and Balancer object,
    # this should fix NodeInfoUpdater constructor parameters
    # (prepare_namespace_states)

    DT_FORMAT = '%Y-%m-%d %H:%M:%S'
    MIN_NS_UNITS = config.get('balancer_config', {}).get('min_units', 1)
    ADD_NS_UNITS = config.get('balancer_config', {}).get('add_units', 1)

    CLUSTER_CHANGES_LOCK = 'cluster'

    MAKE_IOLOOP = 'make_ioloop'

    def __init__(self, n, meta_db):
        self.node = n
        self.infrastructure = None
        self.statistics = statistics.Statistics(self)
        self.niu = None

        self._cached_keys = CachedGzipResponse()
        self.cached_keys_timer = periodic_timer(seconds=config.get('nodes_reload_period', 60))

        self.statistics_monitor_enabled = bool(monitor.CoupleFreeEffectiveSpaceMonitor.STAT_CFG)

        if self.statistics_monitor_enabled:
            self.couple_free_eff_space_monitor = monitor.CoupleFreeEffectiveSpaceMonitor(meta_db)
            self.couples_free_eff_space_collect_timer = periodic_timer(
                seconds=self.couple_free_eff_space_monitor.DATA_COLLECT_PERIOD
            )

        try:
            keys_db_uri = config['metadata']['cache']['db']
        except KeyError:
            logger.error('Config parameter metadata.cache.db is required '
                         'for cache manager')
            self._keys_db = None
        else:
            self._keys_db = Collection(meta_db[keys_db_uri], 'keys')

        self.__tq = timed_queue.TimedQueue()
        self.__tq.add_task_in(
            self.MAKE_IOLOOP,
            0,
            self._make_tq_thread_ioloop
        )

    def start(self):
        assert self.niu
        self._update_cached_keys()
        if self.statistics_monitor_enabled:
            self.__tq.add_task_at(
                'couples_free_effective_space_collect',
                self.couples_free_eff_space_collect_timer.next(),
                self._collect_couples_free_eff_space
            )

    def _make_tq_thread_ioloop(self):
        logger.debug('Balancer task queue, creating thread ioloop')
        io_loop = IOLoop()
        io_loop.make_current()

    def _start_tq(self):
        self.__tq.start()

    def _set_infrastructure(self, infrastructure):
        self.infrastructure = infrastructure

    @h.concurrent_handler
    def get_symmetric_groups(self, request):
        result = self._good_couples()
        logger.debug('good_symm_groups: ' + str(result))
        return result

    def _good_couples(self):
        # TODO: decide if lrc groupsets should be here
        return [couple.as_tuple() for couple in storage.replicas_groupsets if couple.status == storage.Status.OK]

    @h.concurrent_handler
    def get_bad_groups(self, request):
        # TODO: decide if lrc groupsets should be here
        result = [couple.as_tuple() for couple in storage.replicas_groupsets if couple.status not in storage.NOT_BAD_STATUSES]
        logger.debug('bad_symm_groups: ' + str(result))
        return result

    @h.concurrent_handler
    def get_frozen_groups(self, request):
        result = self._frozen_couples()
        logger.debug('frozen_couples: ' + str(result))
        return result

    def _frozen_couples(self):
        return [couple.as_tuple() for couple in storage.replicas_groupsets if couple.status == storage.Status.FROZEN]

    @h.concurrent_handler
    def get_closed_groups(self, request):
        result = self._closed_couples()

        logger.debug('closed couples: ' + str(result))
        return result

    def _closed_couples(self):
        return [couple.as_tuple() for couple in storage.replicas_groupsets
                if couple.status == storage.Status.FULL]

    @h.concurrent_handler
    def get_empty_groups(self, request):
        options = request and request[0] or {}

        result = self._empty_group_ids(
            in_service=options.get('in_service', False),
            status=storage.Status.BROKEN if options.get('state') == 'bad' else storage.Status.INIT)
        logger.debug('uncoupled groups: ' + str(result))
        return result

    def _empty_group_ids(self, in_service=False, status=storage.Status.INIT):
        try:
            return [group.group_id
                    for group in infrastructure.infrastructure.get_good_uncoupled_groups(
                        including_in_service=in_service,
                        status=status)]
        except Exception:
            logger.exception('Failed to fetch uncoupled groups list')
            pass
        return []

    COUPLE_STATES = {
        'good': [storage.Status.OK],
        'full': [storage.Status.FULL],
        'frozen': [storage.Status.FROZEN],
        'bad': [
            storage.Status.INIT,
            storage.Status.BAD,
            storage.Status.BAD_DATA_UNAVAILABLE,
            storage.Status.BAD_INDICES_UNAVAILABLE,
        ],
        'broken': [storage.Status.BROKEN],
        'service-stalled': [storage.Status.SERVICE_STALLED],
        'service-active': [storage.Status.SERVICE_ACTIVE],
        'archived': [storage.Status.ARCHIVED],
    }

    @h.concurrent_handler
    def get_groupsets_list(self, request):
        filter = request.get('filter', {})
        return self._get_groupsets_list(filter=filter)

    def _get_groupsets_list(self, filter):
        # TODO: think on checking input filter parameters and
        # cleaning all that have a value of 'None'. This
        # should be applied to all methods that support filter-like
        # input with possible 'None' values meaning 'disable filter for this
        # parameter'.
        if filter.get('state') is not None and filter['state'] not in self.COUPLE_STATES:
            raise ValueError('Invalid state: {0}'.format(filter['state']))

        groupsets = storage.groupsets

        if filter.get('namespace') is not None:
            ns = filter['namespace']
            if ns not in storage.namespaces:
                return []
            groupsets = storage.namespaces[ns].groupsets

        if filter.get('type') is not None:
            if filter['type'] not in groupsets.types:
                raise ValueError('Unexpected groupsets type: "{}"'.format(filter['type']))
            groupsets = groupsets.types[filter['type']]

        def filtered_out(groupset):
            if filter.get('state') is not None:
                if groupset.status not in self.COUPLE_STATES[filter['state']]:
                    return True

            return False

        return [
            gs.info().serialize()
            for gs in groupsets
            if not filtered_out(gs)
        ]

    @h.concurrent_handler
    def get_groupset_by_id(self, request):
        groupset_id = str(request)
        groupset = storage.groupsets[groupset_id]

        return groupset.info().serialize()

    @h.concurrent_handler
    def get_couples_list(self, request):
        options = request[0]
        return self._get_couples_list(options)

    def _get_couples_list(self, _filter):
        # TODO: think on checking input filter parameters and
        # cleaning all that have a value of 'None'. This
        # should be applied to all methods that support filter-like
        # input with possible 'None' values meaning 'disable filter for this
        # parameter'.
        if _filter.get('state') is not None and _filter['state'] not in self.COUPLE_STATES:
            raise ValueError('Invalid state: {0}'.format(_filter['state']))

        if _filter.get('namespace') is not None:
            ns = _filter['namespace']
            if ns not in storage.namespaces:
                return []
            couples = storage.namespaces[ns].couples
        else:
            # TODO: use 'couples' container here
            couples = storage.replicas_groupsets.keys()

        def filtered_out(couple):
            if _filter.get('state') is not None:
                if couple.status not in self.COUPLE_STATES[_filter['state']]:
                    return True

            return False

        data = []
        for c in couples:
            if filtered_out(c):
                continue
            data.append(c.info().serialize())
        return data

    GROUP_STATES = {
        'init': [storage.Status.INIT],
        'good': [storage.Status.COUPLED],
        'bad': [storage.Status.INIT, storage.Status.BAD],
        'broken': [storage.Status.BROKEN],
        'ro': [storage.Status.RO],
        'migrating': [storage.Status.MIGRATING],
    }

    @h.concurrent_handler
    def get_groups_list(self, request):
        options = request[0]
        return self._get_groups_list(options)

    def _get_groups_list(self, _filter):
        data = []
        if _filter.get('state') is not None and _filter['state'] not in self.GROUP_STATES:
            raise ValueError('Invalid state: {0}'.format(_filter['state']))

        in_service_group_ids = set()
        if _filter.get('in_jobs') is not None and self.infrastructure:
            in_service_group_ids = set(self.infrastructure.get_group_ids_in_service())

        def filtered_out(group):

            if _filter.get('state') is not None:
                if group.status not in self.GROUP_STATES[_filter['state']]:
                    return True

            if _filter.get('type') is not None:
                if group.type != _filter['type']:
                    return True
            elif _filter.get('uncoupled') is not None:
                # support for deprecated 'uncoupled' filter option
                if bool(group.couple) != (not _filter['uncoupled']):
                    return True

            if _filter.get('in_jobs') is not None:
                if (group.group_id in in_service_group_ids) != _filter['in_jobs']:
                    return True

            return False

        for group in storage.groups.keys():
            if filtered_out(group):
                continue
            data.append(group.info().serialize())
        return data

    @h.concurrent_handler
    def get_group_meta(self, request):
        gid = request[0]
        key = request[1] or keys.SYMMETRIC_GROUPS_KEY
        unpack = request[2]

        group = storage.groups[gid]

        logger.info('Creating elliptics session')

        s = elliptics.Session(self.node)
        wait_timeout = config.get('elliptics', {}).get('wait_timeout') or \
            config.get('wait_timeout', 5)
        s.set_timeout(wait_timeout)
        s.add_groups([group.group_id])

        data = s.read_data(key).get()[0]

        logger.info('Read key {0} from group {1}: {2}'.format(
            key.replace('\0', r'\0'), group, data.data))

        return {'id': repr(data.id),
                'full_id': str(data.id),
                'data': msgpack.unpackb(data.data) if unpack else data.data}

    @h.concurrent_handler
    def groups_by_dc(self, request):
        groups = request[0]
        logger.info('Groups: %s' % (groups,))
        groups_by_dcs = {}
        for g in groups:

            if g not in storage.groups:
                logger.info('Group %s not found' % (g,))
                continue

            group = storage.groups[g]
            group_data = {
                'group': group.group_id,
                'node_backends': [nb.info() for nb in group.node_backends],
            }
            if group.couple:
                group_data.update({
                    'couple': str(group.couple),
                    'couple_status': group.couple.status})

            if not group.node_backends:
                dc_groups = groups_by_dcs.setdefault('unknown', {})
                dc_groups[group.group_id] = group_data
                continue

            for node_backend in group.node_backends:
                dc = node_backend.node.host.dc_or_not
                dc_groups = groups_by_dcs.setdefault(dc, {})
                dc_groups[group.group_id] = group_data

        return groups_by_dcs

    @h.concurrent_handler
    def get_group_weights(self, request):
        try:
            ns = request[0]
        except IndexError:
            ns = None

        namespaces_states = self.niu._namespaces_states.get_result(compressed=False)
        if ns and ns not in namespaces_states:
            raise ValueError('Namespace "{0}" does not exist'.format(ns))

        if ns is None:
            namespaces = namespaces_states.iterkeys()
        else:
            namespaces = [ns]

        result = {}
        for namespace in namespaces:
            try:
                result[namespace] = namespaces_states[namespace]['weights']
            except KeyError:
                # No couples to write to in namespace
                pass

        return result

    @h.concurrent_handler
    def repair_groups(self, request):
        logger.info('----------------------------------------')
        logger.info('New repair groups request: ' + str(request))

        group_id = int(request[0])
        try:
            force_namespace = request[1]
        except IndexError:
            force_namespace = None

        group = storage.groups[group_id]

        if group.couple is None:
            raise ValueError(
                'cannot repair, group {group_id} is not a member of any couple'.format(
                    group_id=group.group_id,
                )
            )

        groupset = group.couple

        if groupset.status in storage.NOT_BAD_STATUSES:
            raise ValueError(
                'cannot repair, group {group_id}, groupset {groupset} is in status {status}'.format(
                    group_id=group.group_id,
                    groupset=groupset,
                    status=groupset.status,
                )
            )

        namespace_to_use = force_namespace or groupset.namespace.id
        if not namespace_to_use:
            raise ValueError(
                'cannot identify a namespace to use for group {group_id}'.format(
                    group_id=group.group_id,
                )
            )

        if namespace_to_use != groupset.namespace.id:
            if groupset.namespace:
                groupset.namespace.remove_couple(groupset)
            ns = storage.namespaces[namespace_to_use]
            ns.add_couple(groupset)

        write_groupset_metakey(
            self.node,
            couple=groupset.couple,
            groupset=groupset,
            settings=groupset.groupset_settings,
            rollback_on_error=False,
        )
        groupset.update_status()

        return True

    @h.concurrent_handler
    def get_group_info(self, request):
        group = int(request)
        logger.info('get_group_info: request: %s' % (str(request),))

        logger.info('Group %d: %s' % (group, repr(storage.groups[group])))

        return storage.groups[group].info().serialize()

    @h.concurrent_handler
    def get_group_history(self, request):
        group = int(request[0])

        if self.infrastructure:
            group_history = self.infrastructure.get_group_history(group)
            return group_history.dump()

        raise ValueError('History for group {} is not found'.format(group))

    @h.concurrent_handler
    def get_group_histories_list(self, request):
        _filter = request.get('filter', {})
        return self._get_group_histories_list(_filter)

    def _get_group_histories_list(self, _filter):
        data = []
        group_ids = _filter.get('group_ids')
        for group_history in self.infrastructure.get_group_histories(group_ids=group_ids):
            data.append(group_history.dump())
        return data

    NODE_BACKEND_RE = re.compile(
        '(?P<host>.+?)'
        ':(?P<port>\d+)'
        '(?::(?P<family>\d+))?'  # support for empty family
        '/(?P<backend_id>\d+)'
    )

    @h.concurrent_handler
    def group_detach_node(self, request):
        group_id = int(request[0])
        node_backend_str = request[1]

        group = (group_id in storage.groups and
                 storage.groups[group_id] or
                 None)
        node_backend = (node_backend_str in storage.node_backends and
                        storage.node_backends[node_backend_str] or
                        None)

        logger.info('Node backend: {0}'.format(node_backend))
        try:
            m = self.NODE_BACKEND_RE.match(node_backend_str)
            host = m.group('host')
            port = m.group('port')
            family = m.group('family')
            backend_id = m.group('backend_id')
            port, backend_id = int(port), int(backend_id)
            if family:
                family = int(family)
            logger.info('host, port, backend_id: {0}'.format((host, port, backend_id)))
        except (IndexError, ValueError, AttributeError):
            raise ValueError(
                'Node backend has unexpected value {}, expected form: '
                '<host>:<port>:<family>/<backend_id>'.format(
                    node_backend_str
                )
            )

        if group and node_backend and node_backend in group.node_backends:
            logger.info('Removing node backend {0} from group {1} nodes'.format(node_backend, group))
            group.remove_node_backend(node_backend)
            group.update_status_recursive()
            logger.info('Removed node backend {0} from group {1} nodes'.format(node_backend, group))

        logger.info('Removing node backend {0} from group {1} history'.format(node_backend_str, group_id))
        try:
            self.infrastructure.detach_node(
                group_id=group_id,
                hostname=host,
                port=port,
                family=family,
                backend_id=backend_id,
            )
            logger.info('Removed node backend {0} from group {1} history'.format(node_backend_str, group_id))
        except Exception as e:
            logger.error('Failed to remove {0} from group {1} history: {2}'.format(node_backend_str, group_id, str(e)))
            raise

        return True

    @h.concurrent_handler
    def get_couple_info(self, request):
        group_id = int(request)
        logger.info('get_couple_info: request: %s' % (str(request),))

        if group_id not in storage.groups:
            raise ValueError('Group %d is not found' % group_id)

        group = storage.groups[group_id]
        couple = group.couple

        if not couple:
            raise ValueError('Group %s is not coupled' % group)

        logger.info('Group %s: %s' % (group, repr(group)))
        logger.info('Couple %s: %s' % (couple, repr(couple)))

        return couple.info().serialize()

    @h.concurrent_handler
    def get_couple_info_by_coupleid(self, request):
        couple_id = str(request)
        # TODO: use 'couples' container
        couple = storage.replicas_groupsets[couple_id]

        return couple.info().serialize()

    @h.concurrent_handler
    def update_couple_settings(self, request):
        if 'couple' not in request:
            raise ValueError('Request should contain "couple" field')
        couple = storage.groupsets.get_couple(request['couple'])

        if 'settings' not in request:
            raise ValueError('Request should contain "settings" field')
        settings = request['settings']

        couple_record = self.niu.couple_record_finder.couple_record(couple)
        couple_record.set_settings(
            settings=settings,
            update=request.get('update', True),
        )
        couple_record.save()

        # this is required to allow get_couple_info request be able to respond
        # with new setting right away and not wait till the end of the next
        # cluster update cycle
        couple.settings = couple_record.settings

    @h.concurrent_handler
    def attach_groupset_to_couple(self, request):
        if 'couple' not in request:
            raise ValueError('Request should contain "couple" field')
        couple = storage.groupsets.get_couple(request['couple'])

        if 'groupset' not in request:
            raise ValueError('Request should contain "groupset" field')
        group_ids = [int(g) for g in request['groupset'].split(':')]
        groups = [storage.groups[gid] for gid in group_ids]

        if 'type' not in request:
            raise ValueError('Request should contain groupset "type" field')

        if 'settings' not in request:
            raise ValueError('Request should contain "settings" field')

        Groupset = storage.groupsets.make_groupset_type(
            type=request['type'],
            settings=request['settings'],
        )

        Groupset.check_settings(request['settings'])

        groupset = Groupset(groups=groups)
        storage.groupsets.add_groupset(groupset)
        couple.namespace.add_groupset(groupset)

        try:
            # TODO: check if options contains extra keys
            write_groupset_metakey(
                self.node,
                couple=couple,
                groupset=groupset,
                settings=request['settings'],
            )
        except Exception:
            groupset.destroy()
            raise

        # TODO: couple should link groupset on its own
        couple.lrc822v1_groupset = groupset

        for group in groups:
            self.infrastructure.update_group_history(group)

        groupset.update_status()
        couple.update_status()

    VALID_COUPLE_INIT_STATES = (storage.Status.COUPLED, storage.Status.FROZEN)

    def __update_cluster_state(self, namespace=None):
        logger.info('Starting concurrent cluster info update')
        self.niu._force_nodes_update()
        if namespace:
            infrastructure.infrastructure.sync_single_ns_settings(namespace)
        logger.info('Concurrent cluster info update completed')

    @staticmethod
    def _remove_unusable_groups(groups_by_total_space, groups):
        for ts, group_ids in groups_by_total_space.iteritems():
            for group_to_remove in groups[:]:
                if group_to_remove in group_ids:
                    group_ids.remove(group_to_remove)
                    groups.remove(group_to_remove)

    @contextmanager
    def _locked_uncoupled_groups(self, uncoupled_groups, groups_by_total_space, comment=''):
        locks = dict(('{0}{1}'.format(jobs.job.Job.GROUP_LOCK_PREFIX, ug), ug)
                     for ug in uncoupled_groups)
        try:
            sync_manager.persistent_locks_acquire(locks.keys(), data=comment)
        except LockAlreadyAcquiredError as e:
            failed_group_ids = [locks[lock_id] for lock_id in e.lock_ids]
            self._remove_unusable_groups(groups_by_total_space, failed_group_ids)
            yield [ug for ug in uncoupled_groups if ug not in failed_group_ids]

        else:
            try:
                yield uncoupled_groups
            finally:
                try:
                    sync_manager.persistent_locks_release(locks.keys())
                except LockError as e:
                    raise RuntimeError(
                        'Couple {couple} is built but locks release failed: {error}'.format(
                            couple=uncoupled_groups,
                            error=e,
                        )
                    )
                finally:
                    self._remove_unusable_groups(groups_by_total_space, uncoupled_groups)

    def __couple_groups(self, size, couples, options, ns, groups_by_total_space):

        res = []
        created_couples = []

        try:
            tree, nodes = self.infrastructure.filtered_cluster_tree(self.NODE_TYPES)
            self.infrastructure.account_ns_couples(tree, nodes, ns)

            units = self.infrastructure.groups_units(
                [storage.groups[group_id]
                    for group_ids in groups_by_total_space.itervalues()
                    for group_id in group_ids],
                self.NODE_TYPES)

        except Exception as e:
            logger.exception('Failed to build couples')
            res.extend([str(e)] * (couples - len(res)))
            return res

        for _, mandatory_groups in itertools.izip_longest(
                xrange(couples), options['mandatory_groups'][:couples]):

            try:
                mandatory_groups = mandatory_groups or []

                if len(mandatory_groups) > size:
                    raise ValueError(
                        "Mandatory groups list's {} length is greater than couple "
                        "size {}".format(mandatory_groups, size))

                for m_group in mandatory_groups:
                    if m_group not in units:
                        raise ValueError(
                            'Mandatory group {0} is either not found '
                            'in cluster, is not uncoupled, is located on a locked host or '
                            'is unsuitable in some other way'.format(m_group))

                if mandatory_groups:
                    self.infrastructure.account_ns_groups(
                        nodes, [storage.groups[g] for g in mandatory_groups])
                    self.infrastructure.update_groups_list(tree)

                ns_current_state = self.infrastructure.ns_current_state(
                    nodes, self.NODE_TYPES[1:])

                couple = self._build_couple(
                    ns_current_state, units, size,
                    groups_by_total_space, mandatory_groups,
                    namespace=options['namespace'],
                    init_state=options['init_state'],
                    groupsets=options['groupsets'],
                    dry_run=options['dry_run'])

                if couple is None:
                    # not enough valid groups
                    break

                self.infrastructure.account_ns_groups(nodes, couple.groups)
                self.infrastructure.update_groups_list(tree)

                created_couples.append(couple)

                res.append(couple.info().serialize())
            except Exception as e:
                logger.exception('Failed to build couple')
                res.append(str(e))
                continue

        res.extend(['Not enough valid dcs and/or groups of appropriate '
                    'total space for remaining couples creation'] * (couples - len(res)))

        if options['dry_run']:
            for couple in created_couples:
                couple.destroy()

        return res

    NODE_TYPES = ['root'] + inventory.get_balancer_node_types() + ['hdd']
    DC_NODE_TYPE = inventory.get_dc_node_type()

    def __weight_combination(self, ns_current_type_state, comb):
        comb_groups_count = copy.copy(ns_current_type_state['nodes'])
        for selected_units in comb:
            for unit in selected_units:
                comb_groups_count.setdefault(unit, 0)
                comb_groups_count[unit] += 1
        return sum((c - ns_current_type_state['avg']) ** 2
                   for c in comb_groups_count.values())

    def __weight_couple_groups(self, ns_current_state, units, group_ids):
        weight = []
        for node_type in self.NODE_TYPES[1:]:
            comb = []
            for group_id in group_ids:
                ng_keys = tuple(gu[node_type] for gu in units[group_id])
                comb.append(ng_keys)

            weight.append(self.__weight_combination(
                ns_current_state[node_type],
                comb))

        return weight

    def __choose_groups(self, ns_current_state, units, count, group_ids, levels, mandatory_groups):
        levels = levels[1:]
        node_type = levels[0]
        logger.info('Selecting {0} groups on level {1} among groups {2}'.format(
            count, node_type, group_ids))

        if len(group_ids) < count:
            logger.warn(
                'Not enough groups for choosing on level {0}: {1} uncoupled, {2} needed'.format(
                    node_type, len(group_ids), count
                )
            )
            return []

        if count == 0:
            return []

        groups_by_level_units = {}
        for group_id in group_ids:
            level_units = tuple(gp[node_type] for gp in units[group_id])
            groups_by_level_units.setdefault(level_units, []).append(group_id)

        logger.info('Level {0} current state: avg {1}, nodes {2}'.format(
            node_type,
            ns_current_state[node_type]['avg'],
            ns_current_state[node_type]['nodes']
        ))
        choice_list = []
        for choice, groups in groups_by_level_units.iteritems():
            choice_list.extend([choice] * min(count, len(groups)))

        logger.info('Nodes type: {0}, choice list: {1}'.format(node_type, choice_list))

        weights = {}
        mandatory_groups_units = []
        for group_id in mandatory_groups:
            level_units = [gp[node_type] for gp in units[group_id]]
            mandatory_groups_units.extend(level_units)

        comb_set = set()
        for c in itertools.combinations(choice_list, count):
            comb_set.add(c)

        for comb in comb_set:
            if config.get('forbidden_dc_sharing_among_groups', False) and node_type == self.DC_NODE_TYPE:
                comb_units = list(reduce(operator.add, comb))
                unique_units = set(comb_units) | set(mandatory_groups_units)
                if (len(comb_units + mandatory_groups_units) != len(unique_units)):
                    continue
            weights[comb] = self.__weight_combination(ns_current_state[node_type], comb)

        if not weights:
            logger.warn(
                'Not enough groups for choosing on level {0}: '
                'could not find groups satisfying restrictions'.format(node_type)
            )
            return []

        logger.info('Combination weights: {0}'.format(weights))
        sorted_weights = sorted(weights.items(), key=lambda x: x[1])

        logger.info('Least weight combination: {0}'.format(sorted_weights[0]))

        node_counts = {}
        for node in sorted_weights[0][0]:
            node_counts.setdefault(node, 0)
            node_counts[node] += 1

        logger.info('Level {0}: selected units: {1}'.format(node_type, node_counts))

        if len(levels) == 1:
            groups = reduce(
                operator.add,
                (groups_by_level_units[level_units][:_count]
                 for level_units, _count in node_counts.iteritems()),
                [])
        else:
            groups = reduce(
                operator.add,
                (self.__choose_groups(ns_current_state, units, _count,
                                      groups_by_level_units[level_units],
                                      levels, mandatory_groups)
                 for level_units, _count in node_counts.iteritems()),
                [])

        if len(groups) < count:
            logger.warn(
                'Not enough groups for choosing on level {0}: could not find groups '
                'satisfying restrictions, got {1} groups, expected {2}'.format(
                    node_type,
                    len(groups),
                    count
                )
            )
            return []

        return groups

    def _build_couple(self,
                      ns_current_state,
                      units,
                      size,
                      groups_by_total_space,
                      mandatory_groups,
                      namespace,
                      init_state,
                      groupsets,
                      dry_run=False):

        while True:
            groups_to_couple = self.__choose_groups_to_couple(
                ns_current_state, units, size, groups_by_total_space, mandatory_groups)

            if not groups_to_couple:
                return None

            groupsets_groups = []

            for groupset in groupsets:
                if groupset['type'] == 'lrc':
                    scheme = storage.Lrc.make_scheme(groupset['settings']['scheme'])
                    builder = scheme.builder()
                    try:
                        lrc_uncoupled_group_ids = next(
                            builder.select_uncoupled_groups(
                                skip_groups=groups_to_couple,
                            )
                        )
                    except StopIteration:
                        logger.error(
                            'Failed to find appropriate groups for LRC groupset construction'
                        )
                        return None
                    groupsets_groups.append(lrc_uncoupled_group_ids)

            involved_groups = groups_to_couple + [
                group_id
                for groupset_groups in groupsets_groups
                for group_id in groupset_groups
            ]

            with self._locked_uncoupled_groups(involved_groups,
                                               groups_by_total_space,
                                               'couple build') as locked_uncoupled_group_ids:

                if involved_groups != locked_uncoupled_group_ids:
                    logger.warn('Failed to lock all uncoupled groups: locked {} / {}'.format(
                        locked_uncoupled_group_ids, involved_groups))
                    continue

                logger.info('Chosen groups to couple: {0}'.format(groups_to_couple))

                unsuitable_group_ids = get_unsuitable_uncoupled_group_ids(
                    self.node,
                    involved_groups,
                )
                if unsuitable_group_ids:
                    logger.error(
                        'Groups {} cannot be coupled: failed to ensure empty metakey '
                        'for groups {}'.format(
                            involved_groups,
                            unsuitable_group_ids,
                        )
                    )
                    continue

                couple = storage.replicas_groupsets.add(
                    [storage.groups[g] for g in groups_to_couple]
                )

                couple_groupsets = []
                for groupset, groupset_groups in itertools.izip(groupsets, groupsets_groups):
                    if groupset['type'] == 'lrc':
                        scheme = storage.Lrc.make_scheme(groupset['settings']['scheme'])
                        if scheme == storage.Lrc.Scheme822v1:
                            couple_groupset = storage.Lrc822v1Groupset(
                                [storage.groups[g] for g in groupset_groups]
                            )
                            storage.groupsets.add_groupset(couple_groupset)
                            logger.info('Created new groupset {} for couple {}'.format(
                                couple_groupset,
                                couple
                            ))
                            couple_groupsets.append(couple_groupset)
                            couple.lrc822v1_groupset = couple_groupset

                if namespace not in storage.namespaces:
                    ns = storage.namespaces.add(namespace)
                else:
                    ns = storage.namespaces[namespace]
                ns.add_couple(couple)
                for groupset in couple_groupsets:
                    ns.add_groupset(groupset)

                if not dry_run:
                    try:
                        write_groupset_metakey(
                            self.node,
                            couple=couple,
                            groupset=couple,
                            settings={
                                'frozen': init_state == storage.Status.FROZEN,
                            }
                        )
                        for group in couple.groups:
                            self.infrastructure.update_group_history(group)
                        for couple_groupset, groupset in itertools.izip(couple_groupsets, groupsets):
                            if isinstance(couple_groupset, storage.Lrc822v1Groupset):
                                write_groupset_metakey(
                                    self.node,
                                    couple=couple,
                                    groupset=couple_groupset,
                                    settings={
                                        'part_size': groupset['settings']['part_size'],
                                        'scheme': storage.Lrc.Scheme822v1.ID,
                                    }
                                )
                                couple_groupset.couple = couple
                            for group in couple_groupset.groups:
                                self.infrastructure.update_group_history(group)

                    except Exception:
                        couple.destroy()
                        for couple_groupset in couple_groupsets:
                            couple_groupset.destroy()
                        raise

                if not dry_run:
                    # update should happen after couple has been added to
                    # namespace
                    couple.update_status()
                    for couple_groupset in couple_groupsets:
                        couple_groupset.update_status()

            return couple

    def __choose_groups_to_couple(self, ns_current_state, units, count,
                                  groups_by_total_space, mandatory_groups):
        candidates = []
        for ts, group_ids in groups_by_total_space.iteritems():
            if not all(mg in group_ids for mg in mandatory_groups):
                logger.debug('Could not find mandatory groups {0} in a list '
                             'of groups with ts {1}'.format(mandatory_groups, ts))
                continue

            free_group_ids = [g for g in group_ids if g not in mandatory_groups]

            candidate = self.__choose_groups(
                ns_current_state, units, count - len(mandatory_groups),
                free_group_ids, self.NODE_TYPES, mandatory_groups)
            candidate += mandatory_groups
            if len(candidate) == count:
                candidates.append(candidate)

        if not candidates:
            return None

        candidate = candidates[0]

        if len(candidates) > 1:
            weights = [(self.__weight_couple_groups(ns_current_state, units, c), c)
                       for c in candidates]
            weights.sort()
            logger.info('Choosing candidate with least weight: {0}'.format(weights))
            candidate = weights[0][1]

        return candidate

    @h.concurrent_handler
    def build_couples(self, request):
        logger.info('----------------------------------------')
        logger.info('New build couple request: ' + str(request))

        size = int(request[0])
        couples = int(request[1])

        try:
            options = request[2]
            options['mandatory_groups'] = [
                [int(g) for g in mg]
                for mg in options.get('mandatory_groups', [])]
        except IndexError:
            options = {}

        # TODO: move validation to a separate method
        options.setdefault('namespace', storage.Group.DEFAULT_NAMESPACE)
        options.setdefault('match_group_space', True)
        options.setdefault('init_state', storage.Status.COUPLED)
        options.setdefault('dry_run', False)
        options.setdefault('mandatory_groups', [])
        options.setdefault('groupsets', [])

        options['init_state'] = options['init_state'].upper()
        if not options['init_state'] in self.VALID_COUPLE_INIT_STATES:
            raise ValueError('Couple "{0}" init state is invalid'.format(options['init_state']))

        for gs_options in options['groupsets']:
            if 'type' not in gs_options:
                raise ValueError('Groupset requires "type" field')
            if 'settings' not in gs_options:
                raise ValueError('Groupset requires "settings" field')

            if gs_options['type'] == 'lrc':
                gs_settings = gs_options['settings']
                if 'scheme' not in gs_settings:
                    raise ValueError('Lrc groupset requires "scheme" field')
                scheme_id = gs_settings['scheme']
                if not storage.Lrc.check_scheme(scheme_id):
                    raise ValueError('Unknown LRC scheme "{}"'.format(scheme_id))

        ns = options['namespace']
        logger.info('namespace from request: {0}'.format(ns))

        self.__check_namespace(ns)

        with sync_manager.lock(self.CLUSTER_CHANGES_LOCK, blocking=False):

            logger.info('Updating cluster info')
            self.__update_cluster_state(namespace=options['namespace'])
            logger.info('Updating cluster info completed')

            groups_by_total_space = infrastructure.infrastructure.groups_by_total_space(
                match_group_space=options['match_group_space']
            )

            logger.info('groups by total space: {0}'.format(groups_by_total_space))

            res = self.__couple_groups(size, couples, options, ns, groups_by_total_space)

        return res

    @h.concurrent_handler
    def break_couple(self, request):
        logger.info('----------------------------------------')
        logger.info('New break couple request: ' + str(request))

        with sync_manager.lock(self.CLUSTER_CHANGES_LOCK, blocking=False):

            couple_str = ':'.join(map(str, sorted(request[0], key=lambda x: int(x))))
            # TODO: use 'couples' container
            if couple_str not in storage.replicas_groupsets:
                raise KeyError('Couple %s was not found' % (couple_str))

            couple = storage.replicas_groupsets[couple_str]

            logger.info('Updating couple groups info')
            self.niu._force_nodes_update(groups=couple.groups)
            logger.info('Updating couple groups info completed')

            confirm = request[1]

            logger.info('groups: %s; confirmation: "%s"' % (couple_str, confirm))

            correct_confirms = []
            correct_confirm = 'Yes, I want to break '
            if couple.status in storage.NOT_BAD_STATUSES:
                correct_confirm += 'good'
            else:
                correct_confirm += 'bad'

            correct_confirm += ' couple '

            correct_confirms.append(correct_confirm + couple_str)
            correct_confirms.append(correct_confirm + '[' + couple_str + ']')

            if confirm not in correct_confirms:
                raise Exception('Incorrect confirmation string')

            kill_symm_group(self.node, self.node.meta_session, couple)
            # force cleaning meta from groups when destroying groupset (otherwise
            # will have to wait for the next cluster update cycle)
            for group in couple.groups:
                group.parse_meta(None)
            couple.destroy()

            return True

    @h.concurrent_handler
    def get_next_group_number(self, request):
        groups_count = int(request)
        if groups_count < 0 or groups_count > 100:
            raise Exception('Incorrect groups count')

        return self.infrastructure.reserve_group_ids(groups_count)

    # @h.concurrent_handler
    @h.handler_wne
    def get_config_remotes(self, request):
        return CONFIG_REMOTES

    ALPHANUM = 'a-zA-Z0-9'
    EXTRA = '\-_'
    NS_RE = re.compile('^[{alphanum}][{alphanum}{extra}]*[{alphanum}]$'.format(
        alphanum=ALPHANUM, extra=EXTRA))

    def __valid_namespace(self, namespace):
        return self.NS_RE.match(namespace) is not None

    TIME_UNITS_RE = re.compile('^(\d+)(?:[smhd])$')

    @staticmethod
    def __valid_time_units(time_units):
        match = Balancer.TIME_UNITS_RE.match(time_units)
        if match is None:
            return False
        time_units_num_val = int(match.group(1))
        if time_units_num_val <= 0:
            return False
        return True

    def __validate_ns_settings(self, namespace, settings):

        groups_count = None
        if settings.get('groups-count'):
            groups_count = settings['groups-count']
            if groups_count <= 0:
                raise ValueError('groups-count should be positive integer')
        elif not settings.get('static-couple'):
            raise ValueError('groups-count should be set')

        try:
            min_units = settings['min-units'] = int(settings['min-units'])
            if not min_units > 0:
                raise ValueError
        except KeyError:
            pass
        except ValueError:
            raise ValueError('min-units should be positive integer')

        try:
            add_units = settings['add-units'] = int(settings['add-units'])
            if not add_units > 0:
                raise ValueError
        except KeyError:
            pass
        except ValueError:
            raise ValueError('add-units should be positive integer')

        try:
            settings['check-for-update'] = bool(settings['check-for-update'])
        except KeyError:
            pass
        except (TypeError, ValueError):
            raise ValueError('check-for-update should be boolean')

        try:
            content_length_threshold = settings['redirect']['content-length-threshold'] = int(settings['redirect']['content-length-threshold'])
            if not content_length_threshold >= -1:
                raise ValueError
        except KeyError:
            pass
        except ValueError:
            raise ValueError('redirect content length threshold should be non-negative integer or -1')

        try:
            expire_time = settings['redirect']['expire-time'] = int(settings['redirect']['expire-time'])
            if not expire_time > 0:
                raise ValueError
        except KeyError:
            pass
        except ValueError:
            raise ValueError('redirect expire time should be positive integer')

        try:
            query_args = settings['redirect']['query-args']
            for query_arg in query_args:
                if not isinstance(query_arg, basestring):
                    raise ValueError('query-args should be a list of strings')
        except KeyError:
            pass

        try:
            reserved_space_percentage = settings['reserved-space-percentage'] = float(settings['reserved-space-percentage'])
            if not 0.0 <= reserved_space_percentage <= 1.0:
                raise ValueError
        except KeyError:
            pass
        except ValueError:
            raise ValueError('reserved-space-percentage should be a float in interval [0.0, 1.0]')

        if settings.get('success-copies-num', '') not in ('any', 'quorum', 'all'):
            raise ValueError('success-copies-num allowed values are "any", '
                             '"quorum" and "all"')

        if 'auth-keys' in settings:
            auth_keys_settings = settings['auth-keys']
            if 'read' not in auth_keys_settings:
                auth_keys_settings['read'] = ''
            elif auth_keys_settings['read'] is True:
                auth_keys_settings['read'] = h.random_hex_string(16)
            if 'write' not in auth_keys_settings:
                auth_keys_settings['write'] = ''
            elif auth_keys_settings['write'] is True:
                auth_keys_settings['write'] = h.random_hex_string(16)

        if 'attributes' in settings:
            attributes = settings['attributes']

            if 'filename' in attributes:
                if not isinstance(attributes['filename'], bool):
                    raise ValueError('attributes filename should be boolean')

            if 'ttl' in attributes:
                attributes_ttl = attributes['ttl']
                if 'enable' in attributes_ttl:
                    if not isinstance(attributes_ttl['enable'], bool):
                        raise ValueError('ttl "enable" flag should be boolean')
                if 'minimum' in attributes_ttl:
                    if not self.__valid_time_units(attributes_ttl['minimum']):
                        raise ValueError('minimum ttl should be a valid time unit')
                if 'maximum' in attributes_ttl:
                    if not self.__valid_time_units(attributes_ttl['maximum']):
                        raise ValueError('maximum ttl should be a valid time unit')

        is_ttl_enabled = settings.get('attributes', {}).get('ttl', {}).get('enable', False)
        is_check_for_update_disabled = settings.get('check-for-update', True) is False
        if is_ttl_enabled and is_check_for_update_disabled:
            raise ValueError(
                'ttl attribute cannot be enabled when check-for-update is disabled'
            )

        keys = (settings.get('redirect', {}).get('expire-time'),
                settings.get('signature', {}).get('token'),
                settings.get('signature', {}).get('path_prefix'))

        if not all(keys) and any(keys):
            raise ValueError(
                'Signature token, signature path prefix '
                'and redirect expire time should be set simultaneously'
            )

        if settings.get('static-couple'):
            couple = settings['static-couple']
            groups = [storage.groups[g] for g in couple]
            ref_couple = groups[0].couple

            couple_checks = [g.couple and g.couple == ref_couple
                             for g in groups]
            logger.debug('Checking couple {0}: {1}'.format(
                couple, couple_checks))

            if (not ref_couple or not all(couple_checks)):
                raise ValueError('Couple {0} is not found'.format(couple))

            logger.debug('Checking couple {0} namespace'.format(couple))
            if ref_couple.namespace != namespace:
                raise ValueError(
                    'Couple {0} namespace is {1}, not {2}'.format(
                        ref_couple,
                        ref_couple.namespace,
                        namespace
                    )
                )

            for c in storage.replicas_groupsets:
                if c.namespace == namespace and c != ref_couple:
                    raise ValueError(
                        'Namespace "{0}" has several couples, '
                        'should have only 1 couple for static couple setting'.format(
                            namespace
                        )
                    )

            for g in ref_couple:
                if g not in groups:
                    raise ValueError(
                        'Using incomplete couple {0}, full couple is {1}'.format(
                            couple, ref_couple
                        )
                    )

            if groups_count:
                if len(couple) != groups_count:
                    raise ValueError('Couple {0} does not have length {1}'.format(
                        couple, groups_count
                    ))
            else:
                groups_count = len(ref_couple.groups)

        settings['groups-count'] = groups_count

    ALLOWED_NS_KEYS = set([
        'success-copies-num', 'groups-count',
        'static-couple', 'auth-keys', 'signature', 'redirect',
        'min-units', 'add-units', 'features', 'reserved-space-percentage',
        'check-for-update', '__service',
        'attributes',
    ])
    ALLOWED_NS_SIGN_KEYS = set(['token', 'path_prefix'])
    ALLOWED_NS_AUTH_KEYS = set(['write', 'read'])
    ALLOWED_REDIRECT_KEYS = set([
        'content-length-threshold',
        'expire-time',
        'query-args',
        'add-orig-path-query-arg',
    ])
    ALLOWED_SERVICE_KEYS = set(['is_deleted'])
    ALLOWED_ATTRIBUTES_KEYS = set([
        'filename',
        'ttl',
    ])
    ALLOWED_ATTRIBUTES_TTL_KEYS = set([
        'enable',
        'minimum',
        'maximum',
    ])

    def __merge_dict(self, dst, src):
        for k, val in src.iteritems():
            if k not in dst:
                dst[k] = val
            else:
                if not isinstance(val, dict):
                    dst[k] = val
                else:
                    self.__merge_dict(dst[k], src[k])

    @h.concurrent_handler
    def namespace_setup(self, request):
        try:
            namespace, overwrite, settings = request[:3]
        except Exception:
            raise ValueError('Invalid parameters')

        try:
            options = request[3]
        except IndexError:
            options = {}

        cur_settings = {}
        if not overwrite:
            try:
                self.infrastructure.sync_single_ns_settings(namespace)
                cur_settings = self.infrastructure.ns_settings[namespace]
            except elliptics.NotFoundError:
                pass
            except Exception as e:
                logger.error('Failed to update namespace {0} settings: {1}\n{2}'.format(
                    namespace, str(e), traceback.format_exc()
                ))
                raise

        if cur_settings.get('__service', {}).get('is_deleted'):
            logger.info(
                'Namespace {0} is deleted, will not merge old settings with new ones'.format(
                    namespace
                )
            )
            cur_settings = {'__service': cur_settings['__service']}

        cur_settings.setdefault('__service', {})

        if options.get('json'):
            try:
                settings = json.loads(settings)
                logger.info('Namespace {0}: input settings {1}'.format(namespace, settings))
            except Exception as e:
                logger.error('Namespace {0}, invalid json settings: {1}'.format(namespace, e))
                raise ValueError('Invalid json settings')

        logger.info('Namespace {0}, old settings found: {1}, updating with {2}'.format(
            namespace, cur_settings, settings))

        for auth_key_type in ('read', 'write'):
            if (not overwrite and
                cur_settings.get('auth-keys', {}).get(auth_key_type) and
                    settings.get('auth-keys', {}).get(auth_key_type)):

                raise ValueError('{} auth key is already set'.format(auth_key_type))

        self.__merge_dict(cur_settings, settings)

        if not self.__valid_namespace(namespace):
            raise ValueError('Namespace "{0}" is invalid'.format(namespace))

        settings = cur_settings

        if not options.get('skip_validation'):

            # filtering settings
            for k in settings.keys():
                if k not in self.ALLOWED_NS_KEYS:
                    del settings[k]
            for k in settings.get('signature', {}).keys():
                if k not in self.ALLOWED_NS_SIGN_KEYS:
                    del settings['signature'][k]
            for k in settings.get('auth-keys', {}).keys():
                if k not in self.ALLOWED_NS_AUTH_KEYS:
                    del settings['auth-keys'][k]
            for k in settings.get('redirect', {}).keys():
                if k not in self.ALLOWED_REDIRECT_KEYS:
                    del settings['redirect'][k]
            for k in settings['__service'].keys():
                if k not in self.ALLOWED_SERVICE_KEYS:
                    del settings['__service'][k]
            for k in settings.get('attributes', {}).keys():
                if k not in self.ALLOWED_ATTRIBUTES_KEYS:
                    del settings['attributes'][k]
            for k in settings.get('attributes', {}).get('ttl', {}).keys():
                if k not in self.ALLOWED_ATTRIBUTES_TTL_KEYS:
                    del settings['attributes']['ttl'][k]

            try:
                self.__validate_ns_settings(namespace, settings)
            except Exception as e:
                logger.error(e)
                raise

        self.infrastructure.set_ns_settings(namespace, settings)

        logger.info('Namespace {0}, settings set to {1}'.format(namespace, settings))

        return self.infrastructure.ns_settings[namespace]

    def __check_namespace(self, namespace):
        if namespace not in self.infrastructure.ns_settings:
            raise ValueError('Namespace "{0}" does not exist'.format(namespace))
        else:
            logger.info('Current namespace {0} settings: {1}'.format(namespace, self.infrastructure.ns_settings[namespace]))
            if self.infrastructure.ns_settings[namespace]['__service'].get('is_deleted'):
                raise ValueError('Namespace "{0}" is deleted'.format(namespace))

    @h.concurrent_handler
    def namespace_delete(self, request):
        try:
            namespace = request[0]
        except Exception:
            raise ValueError('Namespace is required')

        with sync_manager.lock(self.CLUSTER_CHANGES_LOCK, blocking=False):

            logger.info('Updating cluster info')
            self.__update_cluster_state(namespace=namespace)
            logger.info('Updating cluster info completed')

            self.__check_namespace(namespace)

            if namespace in storage.namespaces and storage.namespaces[namespace].couples:
                raise ValueError('Cannot delete non-empty namespace'.format(namespace))

            try:
                settings = self.infrastructure.ns_settings[namespace]

                settings.setdefault('__service', {})
                settings['__service']['is_deleted'] = True

                self.infrastructure.set_ns_settings(namespace, settings)
            except Exception as e:
                logger.error('Failed to delete namespace {0}: {1}\n{2}'.format(
                    namespace, str(e), traceback.format_exc()
                ))
                raise

        return True

    @h.concurrent_handler
    def get_namespace_settings(self, request):
        try:
            namespace = request[0]
        except Exception:
            raise ValueError('Invalid parameters')

        if namespace not in self.infrastructure.ns_settings:
            raise ValueError('Namespace "{}" is not found'.format(namespace))

        return self.infrastructure.ns_settings[namespace]

    @h.concurrent_handler
    def get_namespaces_settings(self, request):
        return self.infrastructure.ns_settings

    @h.concurrent_handler
    def get_namespaces_statistics(self, request):
        return self.statistics.per_ns_statistics()

    @h.concurrent_handler
    def freeze_couple(self, request):
        logger.info('freezing couple %s' % str(request))
        couple = storage.replicas_groupsets[request]

        if couple.frozen:
            raise ValueError('Couple {0} is already frozen'.format(couple))

        self.__do_set_meta_freeze(couple, freeze=True)
        couple.update_status()

        return True

    @h.concurrent_handler
    def unfreeze_couple(self, request):
        logger.info('unfreezing couple %s' % str(request))
        couple = storage.replicas_groupsets[request]

        if not couple.frozen:
            raise ValueError('Couple {0} is not frozen'.format(couple))

        self.__do_set_meta_freeze(couple, freeze=False)
        couple.update_status()

        return True

    def __do_set_meta_freeze(self, couple, freeze):

        group_meta = couple.compose_group_meta(
            couple=couple,
            settings={
                'frozen': freeze,
            },
        )

        packed = msgpack.packb(group_meta)
        logger.info('packed meta for couple {0}: "{1}"'.format(
            couple, str(packed).encode('hex')))

        s = elliptics.Session(self.node)
        wait_timeout = config.get('elliptics', {}).get('wait_timeout') or config.get('wait_timeout', 5)
        s.set_timeout(wait_timeout)
        s.add_groups(group.group_id for group in couple)

        _, failed_groups = h.write_retry(s, keys.SYMMETRIC_GROUPS_KEY, packed)

        if failed_groups:
            s = 'Failed to write meta key for couple {0} to groups {1}'.format(
                couple, list(failed_groups)
            )
            logger.error(s)
            raise RuntimeError(s)

        try:
            for group in couple:
                group.parse_meta(packed)
        except Exception as e:
            logging.error('Failed to parse meta key for groups {0}: {1}'.format(
                [g.group_id for g in couple.groups], e))
            raise

    @h.concurrent_handler
    def get_namespaces(self, request):
        return self.infrastructure.ns_settings.keys()

    @h.concurrent_handler
    def get_namespaces_list(self, request):
        try:
            _filter = request[0]
        except IndexError:
            _filter = {}

        def filtered_out(ns, settings):
            if _filter.get('deleted') is not None:
                is_deleted = settings['__service'].get('is_deleted', False)
                if _filter['deleted'] != is_deleted:
                    return True
            return False

        res = []
        for ns, settings in self.infrastructure.ns_settings.items():
            if filtered_out(ns, settings):
                continue
            s = copy.deepcopy(settings)
            s['namespace'] = ns
            res.append(s)

        return res

    # @h.concurrent_handler
    @h.handler_wne
    def get_namespaces_states(self, request):
        request = request or {}
        namespaces = request.get('namespaces', [])

        if namespaces:
            # TODO: optimize this case or drop 'namespaces' parameter support
            res = {}
            namespaces_states = self.niu._namespaces_states.get_result(
                compressed=False
            )
            for ns in namespaces:
                if ns not in namespaces_states:
                    continue
                res[ns] = namespaces_states[ns]
            if request.get('gzip', False):
                res = gzip_compress(json.dumps(res))
        else:
            res = self.niu._namespaces_states.get_result(
                compressed=request.get('gzip', False)
            )

        return res

    # @h.concurrent_handler
    @h.handler_wne
    def get_flow_stats(self, request):
        flow_stats = self.niu._flow_stats

        if isinstance(flow_stats, Exception):
            raise flow_stats

        return flow_stats

    @h.concurrent_handler
    def storage_keys_diff(self, request):
        couples_diff = {}
        for couple in storage.replicas_groupsets:
            group_keys = []
            for group in couple.groups:
                if not len(group.node_backends):
                    continue
                if not all(nb.stat for nb in group.node_backends):
                    continue
                group_keys.append(group.get_stat().files)
            if not group_keys:
                continue
            group_keys.sort(reverse=True)
            couples_diff[str(couple)] = sum(group_keys[0] - gk for gk in group_keys[1:])
        return {'couples': couples_diff,
                'total_keys_diff': sum(couples_diff.itervalues())}

    def _update_cached_keys(self):
        start_ts = time.time()
        logger.info('Cached keys updating: started')
        try:
            self._do_update_cached_keys()
        except Exception as e:
            self._cached_keys.set_exception(e)
        finally:
            logger.info('Cached keys updating: finished, time: {0:.3f}'.format(
                time.time() - start_ts))
            self.__tq.add_task_at(
                'cached_keys_update',
                self.cached_keys_timer.next(),
                self._update_cached_keys)

    @h.concurrent_handler
    def force_update_cached_keys(self, request):
        start_ts = time.time()
        logger.info('Cached keys forced updating: started')
        try:
            self._do_update_cached_keys()
        except Exception as e:
            self._cached_keys.set_exception(e)
        finally:
            logger.info('Cached keys forced updating: finished, time: {0:.3f}'.format(
                time.time() - start_ts))

    def _do_update_cached_keys(self):
        # TODO:
        # Uncomment when cocaine-framework-python is fixed or upgrade to cocaine >= 12
        # if not config.get("cache") or not config.get('metadata', {}).get('cache', {}):
        #     self._cached_keys = {}
        #     return
        # mc = ReconnectableService(
        #     '{base_name}-cache'.format(base_name=config.get('app_name', 'mastermind')),
        #     attempts=3, timeout=10, logger=logger)
        # try:
        #     self._cached_keys = mc.run_sync('get_cached_keys', msgpack.packb(None))
        # except Exception as e:
        #     logger.exception('Cached keys updating: failed')
        #     self._cached_keys = e

        # Copy from src/cocaine-app/cache.py:get_cached_keys
        if self._keys_db is None:
            self._cached_keys.set_result({})
            return
        res = {}
        keys = self._keys_db.find({'cache_groups': {'$ne': []}})
        for key in keys:
            by_key = res.setdefault(key['id'], {})
            couple_id = str(key['data_groups'][0])
            by_key[couple_id] = {
                'data_groups': key['data_groups'],
                'cache_groups': key['cache_groups'],
            }
        self._cached_keys.set_result(res)

    # @h.concurrent_handler
    @h.handler_wne
    def get_cached_keys(self, request):
        request = request or {}
        cached_keys = self._cached_keys.get_result(compressed=request.get('gzip', False))

        return cached_keys

    def _collect_couples_free_eff_space(self):
        try:
            self.couple_free_eff_space_monitor.collect()
        except Exception:
            logger.exception('Failed to collect couples effective free space')
        finally:
            self.__tq.add_task_at(
                'couples_free_effective_space_collect',
                self.couples_free_eff_space_collect_timer.next(),
                self._collect_couples_free_eff_space
            )

    @h.concurrent_handler
    def get_monitor_effective_free_space(self, request):
        try:
            namespace = request[0]
        except IndexError:
            raise ValueError('Namespace is required')

        try:
            options = request[1]
        except IndexError:
            raise ValueError('Query options are required')

        try:
            samples_limit = int(options['limit'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('Query options should contain "limit" parameter')

        return self.couple_free_eff_space_monitor.get_namespace_samples(
            namespace=namespace,
            limit=samples_limit,
            skip=int(options.get('offset', 0))
        )


def handlers(b):
    handlers = []
    try:
        private_prefix = '_' + Balancer.__name__ + '__'
        for attr_name in dir(b):
            attr = b.__getattribute__(attr_name)
            if (not callable(attr) or
                    attr_name.startswith('_') or
                    attr_name.startswith(private_prefix) or
                    attr.__name__.startswith('_')):
                continue
            logger.debug('adding handler: attr_name: {0}, attr.__name__ {1}'.format(attr_name, attr.__name__))
            handlers.append(attr)
    except Exception as e:
        logger.error('handler exception: {0}'.format(e))
        pass
    return handlers


def consistent_write(session, key, data, retries=3, rollback_on_error=True):
    s = session.clone()

    key_esc = key.replace('\0', '\\0')

    groups = set(s.groups)

    logger.debug('Performing consistent write of key {0} to groups {1}'.format(
        key_esc, list(groups)))

    suc_groups, failed_groups = h.write_retry(s, key, data, retries=retries)

    if failed_groups:
        # failed to write key to all destination groups

        if rollback_on_error:
            logger.info('Failed to write key consistently, removing key {0} from groups {1}'.format(
                key_esc, list(suc_groups)
            ))

            s.set_groups(suc_groups)
            _, left_groups = h.remove_retry(s, key, retries=retries)

            if left_groups:
                logger.error('Failed to remove key {0} from groups {1}'.format(
                    key_esc, list(left_groups)))
            else:
                logger.info('Successfully removed key {0} from groups {1}'.format(
                    key_esc, list(suc_groups)))

        raise RuntimeError(
            'Failed to write key to groups: {f_groups}, '
            'successful write to groups: {s_groups}'.format(
                s_groups=list(suc_groups),
                f_groups=list(failed_groups),
            )
        )


def kill_symm_group(n, meta_session, couple):
    groups = [group.group_id for group in couple]
    logger.info('Killing symm groups: %s' % str(groups))
    s = elliptics.Session(n)
    wait_timeout = config.get('elliptics', {}).get('wait_timeout') or config.get('wait_timeout', 5)
    s.set_timeout(wait_timeout)
    s.add_groups(groups)

    _, failed_groups = h.remove_retry(s, keys.SYMMETRIC_GROUPS_KEY)

    if failed_groups:
        s = 'Failed to remove couple {0} meta key for from groups {1}'.format(
            couple, list(failed_groups)
        )
        logger.error(s)
        raise RuntimeError(s)


def get_unsuitable_uncoupled_group_ids(n, group_ids):
    logger.info('Checking empty meta key for groups {0}'.format(group_ids))

    s = elliptics.Session(n)
    wait_timeout = (
        config
        .get('elliptics', {})
        .get('wait_timeout')
    ) or config.get('wait_timeout', 5)
    s.set_timeout(wait_timeout)
    s.set_exceptions_policy(elliptics.exceptions_policy.no_exceptions)
    s.set_filter(elliptics.filters.all_final)

    results = {}
    for group_id in group_ids:
        session = s.clone()
        session.add_groups([group_id])

        logger.debug('Request to check {0} for group {1}'.format(
            keys.SYMMETRIC_GROUPS_KEY.replace('\0', '\\0'), group_id))
        results[group_id] = session.read_data(keys.SYMMETRIC_GROUPS_KEY)

    unsuitable_uncoupled_groups = []

    def update_unsuitable_groups(entry, group_id, elapsed_time=None, end_time=None):
        if entry.error.code != -2:
            # -2 is the one and only sign that this uncoupled group is suitable
            unsuitable_uncoupled_groups.append(group_id)

    while results:
        group_id, result = results.popitem()
        h.process_elliptics_async_result(
            result=result,
            processor=update_unsuitable_groups,
            group_id=group_id,
            raise_on_error=False
        )

    return unsuitable_uncoupled_groups


def write_groupset_metakey(n, couple, groupset, settings, rollback_on_error=True):
    logger.info('Writing meta key for groupset {}'.format(groupset))

    s = elliptics.Session(n)
    wait_timeout = config.get('elliptics', {}).get('wait_timeout') or config.get('wait_timeout', 5)
    s.set_timeout(wait_timeout)

    s.add_groups(g.group_id for g in groupset.groups)
    packed = msgpack.packb(
        groupset.compose_group_meta(
            couple=couple,
            settings=settings,
        )
    )
    try:
        consistent_write(s, keys.SYMMETRIC_GROUPS_KEY, packed, rollback_on_error=rollback_on_error)
    except Exception:
        logger.exception('Failed to write meta key for groupset {}'.format(groupset))
        raise

    try:
        for group in groupset.groups:
            group.parse_meta(packed)
    except Exception:
        logging.exception('Failed to parse meta key for groups {}'.format([g.group_id for g in groupset.groups]))
        raise

    return
