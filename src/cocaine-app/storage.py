# -*- coding: utf-8 -*-
import datetime
import errno
import functools
import itertools
import helpers as h
import logging
import math
import os.path
import random
import time
import types

import msgpack

from errors import CacheUpstreamError
import jobs.job
from jobs.job_types import JobTypes
from infrastructure import infrastructure
from infrastructure_cache import cache
from config import config
import lrc_builder
from mastermind.query.couples import Couple as CoupleInfo
from mastermind.query.groupsets import Groupset as GroupsetInfo
from mastermind.query.groups import Group as GroupInfo

logger = logging.getLogger('mm.storage')


VFS_RESERVED_SPACE = config.get('reserved_space', 112742891520)  # default is 105 Gb for one vfs
NODE_BACKEND_STAT_STALE_TIMEOUT = config.get('node_backend_stat_stale_timeout', 120)

FORBIDDEN_DHT_GROUPS = config.get('forbidden_dht_groups', False)
FORBIDDEN_DC_SHARING_AMONG_GROUPS = config.get('forbidden_dc_sharing_among_groups', False)
FORBIDDEN_NS_WITHOUT_SETTINGS = config.get('forbidden_ns_without_settings', False)
FORBIDDEN_UNMATCHED_GROUP_TOTAL_SPACE = config.get('forbidden_unmatched_group_total_space', False)

CACHE_GROUP_PATH_PREFIX = config.get('cache', {}).get('group_path_prefix')


def ts_str(ts):
    return time.asctime(time.localtime(ts))


class Status(object):
    INIT = 'INIT'
    OK = 'OK'
    FULL = 'FULL'
    COUPLED = 'COUPLED'
    BAD = 'BAD'
    BROKEN = 'BROKEN'
    RO = 'RO'
    STALLED = 'STALLED'
    FROZEN = 'FROZEN'

    ARCHIVED = 'ARCHIVED'  # for couples with LRC groupsets

    # LRC groupsets: unrecoverable configuration of stripe
    # parts is unavailable at the moment because corresponding groups
    # are in not-OK state;
    BAD_DATA_UNAVAILABLE = 'BAD_DATA_UNAVAILABLE'

    # LRC groupsets: an index shard of three groups
    # is unavailable at the moment because correspoding groups
    # are in not-OK state;
    BAD_INDICES_UNAVAILABLE = 'BAD_INDICES_UNAVAILABLE'

    MIGRATING = 'MIGRATING'

    SERVICE_ACTIVE = 'SERVICE_ACTIVE'
    SERVICE_STALLED = 'SERVICE_STALLED'

    def __init__(self, code, text):
        self.code = code
        self.text = text


GOOD_STATUSES = set([Status.OK, Status.FULL])
NOT_BAD_STATUSES = set([Status.OK, Status.FULL, Status.FROZEN])


def generate_lrc822v1_bad_parts_indices():

    # TODO: add tests to check indices generation

    combinations = itertools.combinations
    izip = itertools.izip

    local_groups = (
        (0, 1, 2, 3),  # first local group
        (4, 5, 6, 7),  # second local group
    )
    local_parity = (
        (8,),  # local parity for first local group
        (9,),  # local parity for second local group
    )
    global_parity = (10, 11)

    indices = []

    # all 4 data parts from local group
    for local_parts_ids in local_groups:
        indices.append(local_parts_ids)

    # 3 data parts from a local group + 1 corresponding local parity part
    for parts_ids, lp_parts_ids in izip(local_groups, local_parity):
        for data_parts_ids in combinations(parts_ids, 3):
            indices.append(data_parts_ids + lp_parts_ids)

    # 3 data parts from a local group + 1 global parity part
    for parts_ids in local_groups:
        for data_parts_ids in combinations(parts_ids, 3):
            for gp_parts_ids in combinations(global_parity, 1):
                indices.append(data_parts_ids + gp_parts_ids)

    # 2 data parts from a local group + 2 global parts
    for parts_ids in local_groups:
        for data_parts_ids in combinations(parts_ids, 2):
            indices.append(data_parts_ids + global_parity)

    # 2 data parts from a local group + 1 corresponding local parity part + 1 global part
    for parts_ids, lp_parts_ids in izip(local_groups, local_parity):
        for data_parts_ids in combinations(parts_ids, 2):
            for gp_parts_ids in combinations(global_parity, 1):
                indices.append(data_parts_ids + lp_parts_ids + gp_parts_ids)

    # 1 data part from a local group + 1 corresponding local parity part + 2 global parts
    for parts_ids, lp_parts_ids in izip(local_groups, local_parity):
        for data_parts_ids in combinations(parts_ids, 1):
            indices.append(data_parts_ids + lp_parts_ids + global_parity)

    return [
        tuple(sorted(part_ids))
        for part_ids in indices
    ]


class Lrc(object):

    class Scheme822v1(object):
        """ LRC scheme 8-2-2 (version 1)

        This object is a collection of routines and constants relevant
        to LRC scheme 8-2-2 (version 1).
        """

        ID = 'lrc-8-2-2-v1'

        STRIPE_SIZE = 12
        NUM_DATA_PARTS = 8
        NUM_PARITIES = 4
        NUM_LOCAL_PARITIES = 2
        NUM_GLOBAL_PARITIES = 2

        @staticmethod
        def order_groups(groups_lists):
            """ Order groups from groups_lists using scheme's specific group ordering

            Params:
                groups_list: a list of lists, where each nested list consists
                  of groups in a certain dc.
                Example:
                [
                    [1001, 1002, 1003, 1004],  # groups for data parts 0, 1, 4, 5
                    [1005, 1006, 1007, 1008],  # groups for data parts 2, 3, 6, 7
                    [1009, 1010, 1011, 1012],  # groups for L1, L2, G1, G2 parity parts
                ]

            Returns:
                A list of groups sorted in scheme specific order, e.g.:
                [1001, 1002, 1005, 1006, 1003, 1004, 1007, 1008, 1009, 1010, 1011, 1012]
            """
            return (
                groups_lists[0][0:2] +  # data parts 0, 1; located in DC 1
                groups_lists[1][0:2] +  # data parts 2, 3; located in DC 2
                groups_lists[0][2:4] +  # data parts 4, 5; located in DC 1
                groups_lists[1][2:4] +  # data parts 6, 7; located in DC 2
                groups_lists[2][0:4]    # parity parts L1, L2, G1, G2; located in DC 3
            )

        INDEX_SHARD_INDICES = [
            # index groups shards
            frozenset([0, 2, 8]),
            frozenset([1, 3, 9]),
            frozenset([4, 6, 10]),
            frozenset([5, 7, 11]),
        ]

        @staticmethod
        def get_unavailable_index_shard_indices(unavailable_data_parts_indices):
            """ Checks if indices are partially unavailable

            Index keys for each data key are sharded among LRC groups
            in a groupset in a way that each shard contains of three copies
            in three different DCs. If all three groups of shard are unavailable,
            indices are considered partially unavailable.

            Each shard occupies three groups of a column of LRC-8-2-2 scheme,
            so there are 4 index shards:

            0   1   4   5
            2   3   6   7
            8   9   10  11

            Parameters:
                unavailable_data_parts_indices: a list of indices of groups in lrc groupset
                    that are unavailable for any reason.
            """
            unavailable_data_parts_indices = set(unavailable_data_parts_indices)
            for indices in Lrc.Scheme822v1.INDEX_SHARD_INDICES:
                if unavailable_data_parts_indices.issuperset(indices):
                    # sorting is not required, but leads to group ids being
                    # displayed in a sorted order
                    return sorted(indices)
            return None

        BAD_DATA_PARTS_INDICES = set(generate_lrc822v1_bad_parts_indices())

        @staticmethod
        def is_data_partially_unavailable(unavailable_data_parts_indices):
            """ Checks if data is partially unavailable

            Data is considered partially unavailable when LRC stripe's
            data part groups are not available at the moment and their
            data cannot be restored using LRC restore mechanism.

            LRC 8-2-2 scheme allows to restore not more than 4 data part
            groups. Among all 4 groups combinations the ones that does not
            allow data restoring are listed in 'BAD_DATA_PARTS_INDICES'.

            Parameters:
                unavailable_data_parts_indices: a list of indices of groups in lrc groupset
                    that are unavailable for any reason.
            """
            if len(unavailable_data_parts_indices) > 4:
                return True

            if len(unavailable_data_parts_indices) < 4:
                return False

            unavailable_data_parts_indices = tuple(sorted(unavailable_data_parts_indices))
            if unavailable_data_parts_indices in Lrc.Scheme822v1.BAD_DATA_PARTS_INDICES:
                return True

            return False

        builder = lrc_builder.LRC_8_2_2_V1_Builder

    @staticmethod
    def make_scheme(scheme_id):
        if scheme_id == Lrc.Scheme822v1.ID:
            return Lrc.Scheme822v1
        raise ValueError('Unknown LRC scheme "{}"'.format(scheme_id))

    @staticmethod
    def check_scheme(scheme_id):
        try:
            return bool(Lrc.make_scheme(scheme_id))
        except ValueError:
            return False

    @staticmethod
    def select_groups_for_groupset(mandatory_dcs, skip_groups=None):
        '''Select appropriate groups for 'lrc' groupset.

        Returns:
            a list of selected group ids

        Parameters:
            mandatory_dcs - selected groups in such a way that each dc in this list is
                occupied by at least one group (for 'lrc-8-2-2-v1' scheme -- by at least
                one 4-group set);
            skip_groups - a list of groups to skip when selecting new groups;
        '''
        prepared_groups = []

        # a set of unsuitable groups and groups that are already checked
        checked_groups = set()

        groups_in_service = set(infrastructure.get_group_ids_in_service())
        mandatory_dcs = set(mandatory_dcs)
        skip_groups = set(skip_groups or [])

        def check_group(group):
            if group.type != Group.TYPE_UNCOUPLED_LRC_8_2_2_V1:
                return False
            if group in checked_groups:
                return False
            if group in skip_groups:
                return False
            if group in groups_in_service:
                return False
            if group.status != Status.COUPLED:
                return False
            if len(group.node_backends) != 1:
                return False
            return True

        def check_groupset(groups):
            dcs = set(group.node_backends[0].node.host.dc for group in groups)
            if not mandatory_dcs.issubset(dcs):
                return False
            return True

        global groups

        for group in groups:
            if not check_group(group):
                continue

            logger.debug('Lrc groupset group candidate: checking group {}'.format(group))

            check_linked_groups = all(
                group_id in groups and check_group(groups[group_id])
                for group_id in group.meta['lrc_groups']
            )

            checked_groups.update(group.meta['lrc_groups'])

            if not check_linked_groups:
                logger.info(
                    'Lrc groupset group candidate: group {}, linked groups check failed'.format(
                        group
                    )
                )
                continue

            if not check_groupset(groups[group_id] for group_id in group.meta['lrc_groups']):
                logger.info(
                    'Lrc groupset group candidate: group {group}, groupset {groupset} check '
                    'failed'.format(
                        group=group,
                        groupset=group.meta['lrc_groups'],
                    )
                )
                continue

            # all groups have been checked and can be used for groupset construction
            prepared_groups.append(group.meta['lrc_groups'])

            logger.info(
                'Lrc groupset group candidate: group {group}, groupset {groupset} check '
                'passed'.format(
                    group=group,
                    groupset=group.meta['lrc_groups'],
                )
            )

        if not prepared_groups:
            raise ValueError('Failed to find suitable groups for groupset')

        return random.choice(prepared_groups)


class ResourceError(KeyError):
    def __str__(self):
        return str(self.args[0])


class Repositary(object):
    def __init__(self, constructor, resource_desc=None):
        self.elements = {}
        self.constructor = constructor
        self.resource_desc = resource_desc or self.constructor.__name__

    def add(self, *args, **kwargs):
        e = self.constructor(*args, **kwargs)
        self.elements[e] = e
        return e

    def get(self, key, default=None):
        return self.elements.get(key, default)

    def remove(self, key):
        return self.elements.pop(key)

    def __getitem__(self, key):
        try:
            return self.elements[key]
        except KeyError:
            raise ResourceError('{} {} is not found'.format(
                self.resource_desc, key))

    def __setitem__(self, key, value):
        self.elements[key] = value

    def __contains__(self, key):
        return key in self.elements

    def __iter__(self):
        return self.elements.__iter__()

    def __repr__(self):
        return '<Repositary object: [%s] >' % (', '.join((repr(e) for e in self.elements.itervalues())))

    def keys(self):
        return self.elements.keys()

    def values(self):
        return self.elements.values()

    def iterkeys(self):
        return self.elements.iterkeys()

    def itervalues(self):
        return self.elements.itervalues()

    def __len__(self):
        return len(self.elements)


class MultiRepository(object):
    def __init__(self, repositories, resource_desc):
        self._repositories = repositories
        self.resource_desc = resource_desc
        self.types = {}
        for key, repo in repositories.iteritems():
            setattr(self, key, repo)
            self.types[key] = repo

    def __contains__(self, key):
        return any(key in r for r in self._repositories.itervalues())

    def get(self, key, default=None):
        for r in self._repositories.itervalues():
            if key in r:
                return r[key]
        return default

    def __getitem__(self, key):
        for r in self._repositories.itervalues():
            if key in r:
                return r[key]
        raise ResourceError('{} {} is not found'.format(
            self.resource_desc, key))

    def __setitem__(self, key):
        raise NotImplemented('Key cannot be inserted directly into multi-repository')

    def __delitem__(self, key):
        for r in self._repositories.itervalues():
            if key in r:
                del r[key]
                return
        raise KeyError(key)

    def add(self, *args, **kwargs):
        raise NotImplemented('Key cannot be inserted directly into multi-repository')

    def __iter__(self):
        return itertools.chain(*(r.itervalues() for r in self._repositories.itervalues()))

    def keys(self):
        # list comprehension should be used here to fix keys lists
        # when call to this method is performed
        return itertools.chain(*[r.keys() for r in self._repositories.itevalues()])

    def iterkeys(self):
        return itertools.chain(*(r.iterkeys() for r in self._repositories.itervalues()))

    def values(self):
        # list comprehension should be used here to fix values lists
        # when call to this method is performed
        return itertools.chain(*[r.values() for r in self._repositories.itervalues()])

    def itervalues(self):
        return itertools.chain(*(r.itervalues() for r in self._repositories.itervalues()))

    def items(self):
        # list comprehension should be used here to fix items lists
        # when call to this method is performed
        return itertools.chain(*[r.items() for r in self._repositories.itervalues()])

    def iteritems(self):
        return itertools.chain(*(r.iteritems() for r in self._repositories.itervalues()))


GROUPSET_REPLICAS = 'replicas'
GROUPSET_LRC = 'lrc'
GROUPSET_IDS = set([
    GROUPSET_REPLICAS,
    Lrc.Scheme822v1.ID,
])


class Groupsets(MultiRepository):

    def __init__(self, replicas, lrc, resource_desc):
        super(Groupsets, self).__init__(
            {
                GROUPSET_REPLICAS: replicas,
                GROUPSET_LRC: lrc,
            },
            resource_desc=resource_desc,
        )

    def add(self, groups, group_type):
        if group_type == Group.TYPE_DATA:
            couple = self.replicas.add(groups)
        elif group_type == Group.TYPE_CACHE:
            # cache groups reside in replicas groupsets of a couple in special
            # namespace
            couple = self.replicas.add(groups)
        elif group_type == Group.TYPE_LRC_8_2_2_V1:
            couple = self.lrc.add(groups)
        else:
            raise TypeError(
                'Cannot create couple for group type "{}"'.format(group_type)
            )
        return couple

    def add_groupset(self, groupset):
        if isinstance(groupset, Lrc822v1Groupset):
            self.lrc[groupset] = groupset
        elif isinstance(groupset, Couple):
            self.replicas[groupset] = groupset
        else:
            raise TypeError(
                'Unsupported groupset type {type} ({object})'.format(
                    type=type(groupset).__name__,
                    object=groupset,
                )
            )

    def remove_groupset(self, groupset):
        if isinstance(groupset, Lrc822v1Groupset):
            self.lrc.remove(groupset)
        elif isinstance(groupset, Couple):
            self.replicas.remove(groupset)
        else:
            raise TypeError(
                'Unsupported groupset type {type} ({object})'.format(
                    type=type(groupset).__name__,
                    object=groupset,
                )
            )

    # TODO: move to Couple class when new "Couple" instance is introduced
    @staticmethod
    def get_couple(couple_id):
        if isinstance(couple_id, int):
            # TODO: this is a "new" couple id, we should be able to index
            # couples by this id. Right now couple is checked against replicas
            # groupset
            group_id = int(couple_id)
            if group_id not in groups:
                raise ValueError('Couple {} is not found'.format(couple_id))
            group = groups[group_id]
            if not group.couple:
                raise ValueError('Couple {} is not found'.format(couple_id))
            return group.couple.couple
        else:
            return replicas_groupsets[couple_id]

    @staticmethod
    def get_groupset(group_or_groupset_id):
        if isinstance(group_or_groupset_id, int):
            if group_or_groupset_id not in groups:
                raise ValueError('Group {} is not found'.format(group_or_groupset_id))
            group = groups[group_or_groupset_id]
            if group.couple is None:
                raise ValueError('Group {} does not participate in any groupset'.format(
                    group_or_groupset_id
                ))
            return group.couple
        else:
            return groupsets[group_or_groupset_id]

    @staticmethod
    def make_groupset_type(type, settings):
        if type == GROUPSET_REPLICAS:
            return Couple
        elif type == GROUPSET_LRC:
            if 'scheme' not in settings:
                raise ValueError('Lrc groupset requires "scheme" setting')
            scheme = settings['scheme']
            if scheme == Lrc.Scheme822v1.ID:
                return Lrc822v1Groupset
        raise ValueError(
            'Groupset of type "{type}" cannot be constructed '
            'using settings {settings}'.format(
                type=type,
                settings=settings,
            )
        )


class NodeStat(object):
    def __init__(self):
        self.ts = None
        self.load_average = 0.0
        self.tx_bytes, self.rx_bytes = 0, 0
        self.tx_rate, self.rx_rate = 0.0, 0.0

        self.commands_stat = CommandsStat()

    def update(self, raw_stat, collect_ts):
        self.load_average = float(raw_stat['procfs']['vm']['la'][0]) / 100
        interfaces = raw_stat['procfs'].get('net', {}).get('net_interfaces', {})

        new_rx_bytes = sum(map(
            lambda if_: if_[1].get('receive', {}).get('bytes', 0) if if_[0] != 'lo' else 0,
            interfaces.items()))
        new_tx_bytes = sum(map(
            lambda if_: if_[1].get('transmit', {}).get('bytes', 0) if if_[0] != 'lo' else 0,
            interfaces.items()))

        if self.ts is not None and collect_ts > self.ts:
            # conditions are checked for the case of *x_bytes counter overflow
            diff_ts = collect_ts - self.ts
            self.tx_rate = h.unidirectional_value_map(
                self.tx_rate,
                self.tx_bytes,
                new_tx_bytes,
                func=lambda ov, nv: (nv - ov) / float(diff_ts)
            )
            self.rx_rate = h.unidirectional_value_map(
                self.rx_rate,
                self.rx_bytes,
                new_rx_bytes,
                func=lambda ov, nv: (nv - ov) / float(diff_ts)
            )

        self.tx_bytes = new_tx_bytes
        self.rx_bytes = new_rx_bytes

        self.ts = collect_ts

    def __add__(self, other):
        res = NodeStat()

        res.ts = min(self.ts, other.ts)
        res.load_average = max(self.load_average, other.load_average)

        return res

    def __mul__(self, other):
        res = NodeStat()
        res.ts = min(self.ts, other.ts)
        res.load_average = max(self.load_average, other.load_average)

        return res

    def update_commands_stats(self, node_backends):
        self.commands_stat = sum((nb.stat.commands_stat for nb in node_backends), CommandsStat())


class CommandsStat(object):
    def __init__(self):
        self.ts = None

        self.ell_disk_read_time_cnt, self.ell_disk_write_time_cnt = None, None
        self.ell_disk_read_size, self.ell_disk_write_size = None, None
        self.ell_net_read_size, self.ell_net_write_size = None, None

        self.ell_disk_read_time, self.ell_disk_write_time = 0, 0
        self.ell_disk_read_rate, self.ell_disk_write_rate = 0.0, 0.0
        self.ell_net_read_rate, self.ell_net_write_rate = 0.0, 0.0

    def update(self, raw_stat, collect_ts):

        disk_read_stats = self.commands_stats(
            raw_stat,
            read_ops=True,
            disk=True,
            internal=True,
            outside=True
        )
        new_ell_disk_read_time_cnt = self.sum(disk_read_stats, 'time')
        new_ell_disk_read_size = self.sum(disk_read_stats, 'size')
        disk_write_stats = self.commands_stats(
            raw_stat,
            write_ops=True,
            disk=True,
            internal=True,
            outside=True
        )
        new_ell_disk_write_time_cnt = self.sum(disk_write_stats, 'time')
        new_ell_disk_write_size = self.sum(disk_write_stats, 'size')

        net_read_stats = self.commands_stats(
            raw_stat,
            read_ops=True,
            disk=True,
            cache=True,
            internal=True,
            outside=True
        )
        new_ell_net_read_size = self.sum(net_read_stats, 'size')

        net_write_stats = self.commands_stats(
            raw_stat,
            write_ops=True,
            disk=True,
            cache=True,
            internal=True,
            outside=True
        )
        new_ell_net_write_size = self.sum(net_write_stats, 'size')

        if self.ts:
            diff_ts = collect_ts - self.ts
            if diff_ts <= 1:
                return
            self.ell_disk_read_time = h.unidirectional_value_map(
                self.ell_disk_read_time,
                self.ell_disk_read_time_cnt,
                new_ell_disk_read_time_cnt,
                func=lambda ov, nv: nv - ov
            )
            self.ell_disk_write_time = h.unidirectional_value_map(
                self.ell_disk_write_time,
                self.ell_disk_write_time_cnt,
                new_ell_disk_write_time_cnt,
                func=lambda ov, nv: nv - ov
            )

            self.ell_disk_read_rate = h.unidirectional_value_map(
                self.ell_disk_read_rate,
                self.ell_disk_read_size,
                new_ell_disk_read_size,
                func=lambda ov, nv: (nv - ov) / float(diff_ts)
            )
            self.ell_disk_write_rate = h.unidirectional_value_map(
                self.ell_disk_write_rate,
                self.ell_disk_write_size,
                new_ell_disk_write_size,
                func=lambda ov, nv: (nv - ov) / float(diff_ts)
            )

            self.ell_net_read_rate = h.unidirectional_value_map(
                self.ell_net_read_rate,
                self.ell_net_read_size,
                new_ell_net_read_size,
                func=lambda ov, nv: (nv - ov) / float(diff_ts)
            )
            self.ell_net_write_rate = h.unidirectional_value_map(
                self.ell_net_write_rate,
                self.ell_net_write_size,
                new_ell_net_write_size,
                func=lambda ov, nv: (nv - ov) / float(diff_ts)
            )

        self.ell_disk_read_time_cnt = new_ell_disk_read_time_cnt
        self.ell_disk_write_time_cnt = new_ell_disk_write_time_cnt
        self.ell_disk_read_size = new_ell_disk_read_size
        self.ell_disk_write_size = new_ell_disk_write_size

        self.ell_net_read_size = new_ell_net_read_size
        self.ell_net_write_size = new_ell_net_write_size

        self.ts = collect_ts

    @staticmethod
    def sum(stats, field):
        return sum(map(lambda s: s[field], stats), 0)

    @staticmethod
    def commands_stats(commands,
                       write_ops=False,
                       read_ops=False,
                       disk=False,
                       cache=False,
                       internal=False,
                       outside=False):

        def filter(cmd_type, src_type, dst_type):
            if not write_ops and cmd_type in ('WRITE', 'WRITE_NEW'):
                return False
            if not read_ops and cmd_type not in ('WRITE', 'WRITE_NEW'):
                return False
            if not disk and src_type == 'disk':
                return False
            if not cache and src_type == 'cache':
                return False
            if not internal and dst_type == 'internal':
                return False
            if not outside and dst_type == 'outside':
                return False
            return True

        commands_stats = [stat
                          for cmd_type, cmd_stat in commands.iteritems()
                          for src_type, src_stat in cmd_stat.iteritems()
                          for dst_type, stat in src_stat.iteritems()
                          if filter(cmd_type, src_type, dst_type)]
        return commands_stats

    def __add__(self, other):
        new = CommandsStat()

        new.ell_disk_read_time = self.ell_disk_read_time + other.ell_disk_read_time
        new.ell_disk_write_time = self.ell_disk_write_time + other.ell_disk_write_time
        new.ell_disk_read_rate = self.ell_disk_read_rate + other.ell_disk_read_rate
        new.ell_disk_write_rate = self.ell_disk_write_rate + other.ell_disk_write_rate
        new.ell_net_read_rate = self.ell_net_read_rate + other.ell_net_read_rate
        new.ell_net_write_rate = self.ell_net_write_rate + other.ell_net_write_rate

        return new


class NodeBackendStat(object):
    def __init__(self, node_stat):
        # TODO: not required anymore, remove (?)
        self.node_stat = node_stat
        self.ts = None

        self.free_space, self.total_space, self.used_space = 0, 0, 0
        self.vfs_free_space, self.vfs_total_space, self.vfs_used_space = 0, 0, 0

        self.commands_stat = CommandsStat()

        self.last_read, self.last_write = 0, 0
        self.read_rps, self.write_rps = 0, 0

        # Tupical SATA HDD performance is 100 IOPS
        # It will be used as first estimation for maximum node performance
        self.max_read_rps, self.max_write_rps = 100, 100

        self.fragmentation = 0.0
        self.files = 0
        self.files_removed, self.files_removed_size = 0, 0

        self.fsid = None
        self.defrag_state = None
        self.want_defrag = 0

        self.blob_size_limit = 0
        self.max_blob_base_size = 0
        self.blob_size = 0

        self.start_stat_commit_err_count = 0
        self.cur_stat_commit_err_count = 0

        self.io_blocking_size = 0
        self.io_nonblocking_size = 0

        self.backend_start_ts = 0

    def update(self, raw_stat, collect_ts):

        if self.ts and collect_ts > self.ts:
            dt = collect_ts - self.ts

            if 'dstat' in raw_stat['backend'] and 'error' not in raw_stat['backend']['dstat']:
                last_read = raw_stat['backend']['dstat']['read_ios']
                last_write = raw_stat['backend']['dstat']['write_ios']

                self.read_rps = (last_read - self.last_read) / dt
                self.write_rps = (last_write - self.last_write) / dt

                # Disk usage should be used here instead of load average
                self.max_read_rps = self.max_rps(self.read_rps, self.node_stat.load_average)
                self.max_write_rps = self.max_rps(self.write_rps, self.node_stat.load_average)

                self.last_read = last_read
                self.last_write = last_write

        self.ts = collect_ts

        self.vfs_total_space = raw_stat['backend']['vfs']['blocks'] * \
            raw_stat['backend']['vfs']['bsize']
        self.vfs_free_space = raw_stat['backend']['vfs']['bavail'] * \
            raw_stat['backend']['vfs']['bsize']
        self.vfs_used_space = self.vfs_total_space - self.vfs_free_space

        self.files = raw_stat['backend']['summary_stats']['records_total'] - \
            raw_stat['backend']['summary_stats']['records_removed']
        self.files_removed = raw_stat['backend']['summary_stats']['records_removed']
        self.files_removed_size = raw_stat['backend']['summary_stats']['records_removed_size']
        self.fragmentation = float(self.files_removed) / ((self.files + self.files_removed) or 1)

        self.fsid = raw_stat['backend']['vfs']['fsid']
        self.defrag_state = raw_stat['status']['defrag_state']
        self.want_defrag = raw_stat['backend']['summary_stats']['want_defrag']

        self.blob_size_limit = raw_stat['backend']['config'].get('blob_size_limit', 0)
        if self.blob_size_limit > 0:
            self.total_space = self.blob_size_limit
            self.used_space = raw_stat['backend']['summary_stats'].get('base_size', 0)
            self.free_space = min(max(0, self.total_space - self.used_space), self.vfs_free_space)
        else:
            self.total_space = self.vfs_total_space
            self.free_space = self.vfs_free_space
            self.used_space = self.vfs_used_space

        if len(raw_stat['backend'].get('base_stats', {})):
            self.max_blob_base_size = max(
                [blob_stat['base_size']
                    for blob_stat in raw_stat['backend']['base_stats'].values()])
        else:
            self.max_blob_base_size = 0

        self.blob_size = raw_stat['backend']['config']['blob_size']

        self.io_blocking_size = raw_stat['io']['blocking']['current_size']
        self.io_nonblocking_size = raw_stat['io']['nonblocking']['current_size']

        self.cur_stat_commit_err_count = raw_stat['stats'].get(
            'stat_commit', {}).get('errors', {}).get(errno.EROFS, 0)

        if self.backend_start_ts < raw_stat['status']['last_start']['tv_sec']:
            self.backend_start_ts = raw_stat['status']['last_start']['tv_sec']
            self._reset_stat_commit_errors()

        self.commands_stat.update(raw_stat['commands'], collect_ts)

    def _reset_stat_commit_errors(self):
        self.start_stat_commit_err_count = self.cur_stat_commit_err_count

    @property
    def stat_commit_errors(self):
        if self.cur_stat_commit_err_count < self.start_stat_commit_err_count:
            self._reset_stat_commit_errors()
        return self.cur_stat_commit_err_count - self.start_stat_commit_err_count

    def max_rps(self, rps, load_avg):
        return max(rps / max(load_avg, 0.01), 100)

    def __add__(self, other):
        node_stat = self.node_stat + other.node_stat
        res = NodeBackendStat(node_stat)

        res.ts = min(self.ts, other.ts)

        res.node_stat = node_stat

        res.total_space = self.total_space + other.total_space
        res.free_space = self.free_space + other.free_space
        res.used_space = self.used_space + other.used_space

        res.read_rps = self.read_rps + other.read_rps
        res.write_rps = self.write_rps + other.write_rps

        res.max_read_rps = self.max_read_rps + other.max_read_rps
        res.max_write_rps = self.max_write_rps + other.max_write_rps

        res.files = self.files + other.files
        res.files_removed = self.files_removed + other.files_removed
        res.files_removed_size = self.files_removed_size + other.files_removed_size
        res.fragmentation = float(res.files_removed) / (res.files_removed + res.files or 1)

        res.blob_size_limit = min(self.blob_size_limit, other.blob_size_limit)
        res.max_blob_base_size = max(self.max_blob_base_size, other.max_blob_base_size)
        res.blob_size = max(self.blob_size, other.blob_size)

        res.io_blocking_size = max(self.io_blocking_size, other.io_blocking_size)
        res.io_nonblocking_size = max(self.io_nonblocking_size, other.io_nonblocking_size)

        return res

    def __mul__(self, other):
        node_stat = self.node_stat * other.node_stat
        res = NodeBackendStat(node_stat)

        res.ts = min(self.ts, other.ts)

        res.node_stat = node_stat

        res.total_space = min(self.total_space, other.total_space)
        res.free_space = min(self.free_space, other.free_space)
        res.used_space = max(self.used_space, other.used_space)

        res.read_rps = max(self.read_rps, other.read_rps)
        res.write_rps = max(self.write_rps, other.write_rps)

        res.max_read_rps = min(self.max_read_rps, other.max_read_rps)
        res.max_write_rps = min(self.max_write_rps, other.max_write_rps)

        # files and files_removed are taken from the stat object with maximum
        # total number of keys. If total number of keys is equal,
        # the stat object with larger number of removed keys is more up-to-date
        files_stat = max(self, other, key=lambda stat: (stat.files + stat.files_removed, stat.files_removed))
        res.files = files_stat.files
        res.files_removed = files_stat.files_removed
        res.files_removed_size = max(self.files_removed_size, other.files_removed_size)

        # ATTENTION: fragmentation coefficient in this case would not necessary
        # be equal to [removed keys / total keys]
        res.fragmentation = max(self.fragmentation, other.fragmentation)

        res.blob_size_limit = min(self.blob_size_limit, other.blob_size_limit)
        res.max_blob_base_size = max(self.max_blob_base_size, other.max_blob_base_size)
        res.blob_size = max(self.blob_size, other.blob_size)

        res.io_blocking_size = max(self.io_blocking_size, other.io_blocking_size)
        res.io_nonblocking_size = max(self.io_nonblocking_size, other.io_nonblocking_size)

        return res

    def __repr__(self):
        return ('<NodeBackendStat object: ts=%s, write_rps=%d, max_write_rps=%d, read_rps=%d, '
                'max_read_rps=%d, total_space=%d, free_space=%d, files=%s, files_removed=%s, '
                'fragmentation=%s, node_load_average=%s>' % (
                    ts_str(self.ts), self.write_rps, self.max_write_rps, self.read_rps,
                    self.max_read_rps, self.total_space, self.free_space, self.files,
                    self.files_removed, self.fragmentation, self.node_stat.load_average))


class Host(object):
    def __init__(self, addr):
        self.addr = addr
        self.nodes = []

    @property
    def hostname(self):
        return cache.get_hostname_by_addr(self.addr)

    @property
    def hostname_or_not(self):
        return cache.get_hostname_by_addr(self.addr, strict=False)

    @property
    def dc(self):
        return cache.get_dc_by_host(self.hostname)

    @property
    def dc_or_not(self):
        return cache.get_dc_by_host(self.hostname, strict=False)

    @property
    def parents(self):
        return cache.get_host_tree(self.hostname)

    @property
    def full_path(self):
        parent = self.parents
        parts = [parent['name']]
        while 'parent' in parent:
            parent = parent['parent']
            parts.append(parent['name'])
        return '|'.join(reversed(parts))

    def index(self):
        return self.__str__()

    def __eq__(self, other):
        if isinstance(other, str):
            return self.addr == other

        if isinstance(other, Host):
            return self.addr == other.addr

        return False

    def __hash__(self):
        return hash(self.__str__())

    def __repr__(self):
        return ('<Host object: addr=%s, nodes=[%s] >' %
                (self.addr, ', '.join((repr(n) for n in self.nodes))))

    def __str__(self):
        return self.addr


class Node(object):
    def __init__(self, host, port, family):
        self.host = host
        self.port = int(port)
        self.family = int(family)
        self.host.nodes.append(self)

        self.stat = None

    def update_statistics(self, new_stat, collect_ts):
        if self.stat is None:
            self.stat = NodeStat()
        self.stat.update(new_stat, collect_ts)

    def __repr__(self):
        return '<Node object: host={host}, port={port}>'.format(
            host=str(self.host), port=self.port)

    def __str__(self):
        return '{host}:{port}'.format(host=self.host.addr, port=self.port)

    def __hash__(self):
        return hash(self.__str__())

    def __eq__(self, other):
        if isinstance(other, (str, unicode)):
            return self.__str__() == other

        if isinstance(other, Node):
            return self.host.addr == other.host.addr and self.port == other.port

    def update_commands_stats(self, node_backends):
        self.stat.update_commands_stats(node_backends)


class FsStat(object):

    SECTOR_BYTES = 512

    def __init__(self):
        self.ts = None
        self.total_space = 0

        self.dstat = {}
        self.vfs_stat = {}
        self.disk_util = 0.0
        self.disk_util_read = 0.0
        self.disk_util_write = 0.0

        self.disk_read_rate = 0.0
        self.disk_write_rate = 0.0

        self.commands_stat = CommandsStat()

    def apply_new_dstat(self, new_dstat):
        if not self.dstat.get('ts'):
            return
        diff_ts = new_dstat['ts'] - self.dstat['ts']
        if diff_ts <= 1.0:
            return

        disk_util = h.unidirectional_value_map(
            self.disk_util,
            self.dstat['io_ticks'],
            new_dstat['io_ticks'],
            func=lambda ov, nv: (nv - ov) / diff_ts / float(10 ** 3)
        )
        self.disk_util = disk_util

        read_ticks = new_dstat['read_ticks'] - self.dstat['read_ticks']
        write_ticks = new_dstat['write_ticks'] - self.dstat['write_ticks']
        total_rw_ticks = read_ticks + write_ticks
        self.disk_util_read = h.unidirectional_value_map(
            self.disk_util_read,
            self.dstat['read_ticks'],
            new_dstat['read_ticks'],
            func=lambda ov, nv: (total_rw_ticks and
                                 disk_util * (nv - ov) / float(total_rw_ticks) or
                                 0.0)
        )
        self.disk_util_write = h.unidirectional_value_map(
            self.disk_util_write,
            self.dstat['write_ticks'],
            new_dstat['write_ticks'],
            func=lambda ov, nv: (total_rw_ticks and
                                 disk_util * (nv - ov) / float(total_rw_ticks) or
                                 0.0)
        )
        self.disk_read_rate = h.unidirectional_value_map(
            self.disk_read_rate,
            self.dstat['read_sectors'],
            new_dstat['read_sectors'],
            func=lambda ov, nv: (nv - ov) * self.SECTOR_BYTES / float(diff_ts)
        )

    def apply_new_vfs_stat(self, new_vfs_stat):
        new_free_space = new_vfs_stat['bavail'] * new_vfs_stat['bsize']
        if self.vfs_stat.get('ts'):
            diff_ts = new_vfs_stat['ts'] - self.vfs_stat['ts']
            if diff_ts > 1.0:
                written_bytes = self.free_space - new_free_space
                self.disk_write_rate = written_bytes / diff_ts

        self.total_space = new_vfs_stat['blocks'] * new_vfs_stat['bsize']
        self.free_space = new_free_space

    def update(self, fs, raw_stat, collect_ts):
        self.ts = collect_ts
        vfs_stat = raw_stat['vfs']
        dstat_stat = raw_stat['dstat']

        if 'error' in dstat_stat:
            # do not update state
            new_dstat = {}
            logger.error(
                '{fs}: dstat error: {dstat}'.format(
                    fs=fs,
                    dstat=dstat_stat,
                )
            )
        else:
            new_dstat = dstat_stat
            new_dstat['ts'] = (dstat_stat['timestamp']['tv_sec'] +
                               dstat_stat['timestamp']['tv_usec'] / float(10 ** 6))
            self.apply_new_dstat(new_dstat)

        self.dstat = new_dstat

        if 'error' in vfs_stat:
            # do not update state
            new_vfs_stat = {}
            logger.error(
                '{fs}: vfs stat error: {vfs_stat}'.format(
                    fs=fs,
                    vfs_stat=vfs_stat,
                )
            )
        else:
            new_vfs_stat = vfs_stat
            new_vfs_stat['ts'] = (vfs_stat['timestamp']['tv_sec'] +
                                  vfs_stat['timestamp']['tv_usec'] / float(10 ** 6))
            self.apply_new_vfs_stat(new_vfs_stat)

        self.vfs_stat = new_vfs_stat

    def update_commands_stats(self, node_backends):
        self.commands_stat = sum((nb.stat.commands_stat for nb in node_backends), CommandsStat())


class Fs(object):
    def __init__(self, host, fsid):
        self.host = host
        self.fsid = fsid
        self.status = Status.OK

        self.node_backends = {}

        self.stat = None

    def add_node_backend(self, nb):
        if nb.fs:
            nb.fs.remove_node_backend(nb)
        self.node_backends[nb] = nb
        nb.fs = self

    def remove_node_backend(self, nb):
        del self.node_backends[nb]

    def update_statistics(self, new_stat, collect_ts):
        if self.stat is None:
            self.stat = FsStat()
        self.stat.update(self, new_stat, collect_ts)

    def update_commands_stats(self):
        self.stat.update_commands_stats(self.node_backends)

    def update_status(self):
        nbs = self.node_backends.keys()
        prev_status = self.status

        total_space = 0
        for nb in nbs:
            if nb.status not in (Status.OK, Status.BROKEN):
                continue
            total_space += nb.stat.total_space

        if total_space > self.stat.total_space:
            self.status = Status.BROKEN
        else:
            self.status = Status.OK

        # TODO: unwind cycle dependency between node backend status and fs
        # status. E.g., check node backend status and file system status
        # separately on group status updating.

        if self.status != prev_status:
            logger.info('Changing status of fs {0}, affecting node backends {1}'.format(
                self.fsid, [str(nb) for nb in nbs]))
            for nb in nbs:
                nb.update_status()

    def __repr__(self):
        return '<Fs object: host={host}, fsid={fsid}>'.format(
            host=str(self.host), fsid=self.fsid)

    def __str__(self):
        return '{host}:{fsid}'.format(host=self.host.addr, fsid=self.fsid)

    def __hash__(self):
        return hash(self.__str__())

    def __eq__(self, other):
        if isinstance(other, (str, unicode)):
            return self.__str__() == other

        if isinstance(other, Fs):
            return self.host.addr == other.host.addr and self.fsid == other.fsid


class NodeBackend(object):

    ACTIVE_STATUSES = (Status.OK, Status.RO, Status.BROKEN)

    def __init__(self, node, backend_id):

        self.node = node
        self.backend_id = backend_id
        self.fs = None
        self.group = None

        self.stat = None

        self.read_only = False
        self.disabled = False
        self.start_ts = 0
        self.status = Status.INIT
        self.status_text = "Node %s is not inititalized yet" % (self.__str__())

        self.stalled = False

        self.base_path = None

    def set_group(self, group):
        self.group = group

    def remove_group(self):
        self.group = None

    def disable(self):
        self.disabled = True

    def enable(self):
        self.disabled = False

    def make_read_only(self):
        self.read_only = True

    def make_writable(self):
        self.read_only = False

    def update_statistics(self, new_stat, collect_ts):
        if self.stat is None:
            self.stat = NodeBackendStat(self.node.stat)
        self.base_path = os.path.dirname(new_stat['backend']['config'].get('data') or
                                         new_stat['backend']['config'].get('file')) + '/'
        self.stat.update(new_stat, collect_ts)

    def update_status(self):
        if not self.stat:
            self.status = Status.INIT
            self.status_text = 'No statistics gathered for node backend {0}'.format(self.__str__())

        elif self.disabled:
            self.status = Status.STALLED
            self.status_text = 'Node backend {0} has been disabled'.format(str(self))

        elif self.stalled:
            self.status = Status.STALLED
            self.status_text = ('Statistics for node backend {} is too old: '
                                'it was gathered {} seconds ago'.format(
                                    self.__str__(), int(time.time() - self.stat.ts)))

        elif self.fs.status == Status.BROKEN:
            self.status = Status.BROKEN
            self.status_text = ("Node backends' space limit is not properly "
                                "configured on fs {0}".format(self.fs.fsid))

        elif self.read_only:
            self.status = Status.RO
            self.status_text = 'Node backend {0} is in read-only state'.format(str(self))

        else:
            self.status = Status.OK
            self.status_text = 'Node {0} is OK'.format(str(self))

        return self.status

    def update_statistics_status(self):
        if not self.stat:
            return

        self.stalled = self.stat.ts < (time.time() - NODE_BACKEND_STAT_STALE_TIMEOUT)

    @property
    def effective_space(self):

        if self.stat is None:
            return 0

        share = float(self.stat.total_space) / self.stat.vfs_total_space
        free_space_req_share = math.ceil(VFS_RESERVED_SPACE * share)

        return int(max(0, self.stat.total_space - free_space_req_share))

    @property
    def effective_free_space(self):
        if self.stat.vfs_free_space <= VFS_RESERVED_SPACE:
            return 0
        return max(
            self.stat.free_space - (self.stat.total_space - self.effective_space),
            0
        )

    def is_full(self, reserved_space=0.0):

        if self.stat is None:
            return False

        assert 0.0 <= reserved_space <= 1.0, 'Reserved space should have non-negative value lte 1.0'

        if self.stat.used_space >= self.effective_space * (1.0 - reserved_space):
            return True
        if self.effective_free_space <= 0:
            return True
        return False

    @property
    def stat_commit_errors(self):
        return (self.stat and
                self.stat.stat_commit_errors or
                0)

    def info(self):
        res = {}

        res['node'] = '{0}:{1}:{2}'.format(self.node.host, self.node.port, self.node.family)
        res['id'] = '{node}:{port}:{family}/{backend_id}'.format(
            node=self.node.host.addr,
            port=self.node.port,
            family=self.node.family,
            backend_id=self.backend_id,
        )
        res['host'] = self.node.host.addr
        res['port'] = self.node.port
        res['family'] = self.node.family
        res['backend_id'] = self.backend_id
        res['addr'] = str(self)
        res['hostname'] = self.node.host.hostname_or_not
        res['status'] = self.status
        res['status_text'] = self.status_text
        res['dc'] = self.node.host.dc_or_not
        res['last_stat_update'] = (
            self.stat and
            datetime.datetime.fromtimestamp(self.stat.ts).strftime('%Y-%m-%d %H:%M:%S') or
            'unknown')
        if self.node.stat:
            res['tx_rate'] = self.node.stat.tx_rate
            res['rx_rate'] = self.node.stat.rx_rate
        if self.stat:
            res['free_space'] = int(self.stat.free_space)
            res['effective_space'] = self.effective_space
            res['free_effective_space'] = self.effective_free_space
            res['used_space'] = int(self.stat.used_space)
            res['total_space'] = int(self.stat.total_space)
            res['total_files'] = self.stat.files + self.stat.files_removed
            res['records_alive'] = self.stat.files
            res['records_removed'] = self.stat.files_removed
            res['records_removed_size'] = self.stat.files_removed_size
            res['fragmentation'] = self.stat.fragmentation
            res['defrag_state'] = self.stat.defrag_state
            res['want_defrag'] = self.stat.want_defrag
            res['io_blocking_size'] = self.stat.io_blocking_size
            res['io_nonblocking_size'] = self.stat.io_nonblocking_size

        res['path'] = self.base_path or ''

        return res

    def __repr__(self):
        return ('<Node backend object: node=%s, backend_id=%d, '
                'status=%s, read_only=%s, stat=%s>' % (
                    str(self.node), self.backend_id,
                    self.status, str(self.read_only), repr(self.stat)))

    def __str__(self):
        return '%s:%d/%d' % (self.node.host.addr, self.node.port, self.backend_id)

    def __hash__(self):
        return hash(self.__str__())

    def __eq__(self, other):
        if isinstance(other, (str, unicode)):
            return self.__str__() == other

        if isinstance(other, NodeBackend):
            return (self.node.host.addr == other.node.host.addr and
                    self.node.port == other.node.port and
                    self.backend_id == other.backend_id)


class Group(object):

    DEFAULT_NAMESPACE = 'default'
    CACHE_NAMESPACE = 'storage_cache'

    TYPE_UNKNOWN = 'unknown'
    TYPE_UNCOUPLED = 'uncoupled'
    TYPE_DATA = 'data'
    TYPE_CACHE = 'cache'
    TYPE_UNCOUPLED_CACHE = 'uncoupled_cache'
    TYPE_LRC_8_2_2_V1 = 'lrc-8-2-2-v1'
    TYPE_UNCOUPLED_LRC_8_2_2_V1 = 'uncoupled_lrc-8-2-2-v1'

    AVAILABLE_TYPES = set([
        TYPE_DATA,
        TYPE_CACHE,
        TYPE_UNCOUPLED_CACHE,
        TYPE_LRC_8_2_2_V1,
        TYPE_UNCOUPLED_LRC_8_2_2_V1,
    ])

    def __init__(self, group_id, node_backends=None):
        self.group_id = group_id
        self.status = Status.INIT
        self.node_backends = []
        self.couple = None
        self.meta = None
        self.status_text = "Group %s is not inititalized yet" % (self)
        self.active_job = None

        self._type = Group.TYPE_UNKNOWN

        for node_backend in node_backends or []:
            self.add_node_backend(node_backend)

    @property
    def type(self):
        """ Return current group type

        Groups can have different types depending
        on the type of data they store or state they are in:
            - unknown - group is known of but metakey was not read;
            - uncoupled - group has no metakey (but can be assigned to a couple,
            so don't forget to check status);
            - data - group has metakey and belongs to a regular data couple;
            - uncoupled_cache - group has no metakey but its path matches cache
            groups' path from config (such group should be marked as 'cache');
            - cache - group stores gatlinggun cache data;
        """
        if self._type == Group.TYPE_UNCOUPLED and self.couple:
            # This is a special case for couples with some groups
            # with empty meta key.
            # When group belongs to a couple but is disabled or unavailable,
            # it can be added to Storage before the group that it is coupled
            # with or after. In former case mastermind cannot determine that
            # this group is actually a 'data' group so it considers it
            # 'uncoupled' one. But when cluster traversal is over mastermind has
            # enough data for this group to become a 'data' one (since it now
            # should have 'couple' attribute set up).
            # NOTE: this condition can be evaluated to true only once after this
            # group was added to Storage.
            self._type = self._get_type(self.meta)
        return self._type

    def add_node_backend(self, node_backend):
        self.node_backends.append(node_backend)
        if node_backend.group:
            node_backend.group.remove_node_backend(node_backend)
        node_backend.set_group(self)

    def remove_node_backend(self, node_backend):
        self.node_backends.remove(node_backend)
        if node_backend.group is self:
            node_backend.remove_group()

    @property
    def want_defrag(self):
        for nb in self.node_backends:
            if nb.stat and nb.stat.want_defrag > 3:
                return True
        return False

    def _get_type(self, meta):

        if self.meta:
            if 'type' in self.meta and self.meta['type'] not in self.AVAILABLE_TYPES:
                logger.error('Unknown type "{type}" of group {group}'.format(
                    group=self,
                    type=self.meta['type'],
                ))
                return self.TYPE_UNKNOWN

            return self.meta.get('type', self.TYPE_DATA)

        else:

            if self._type != Group.TYPE_UNKNOWN:
                # when meta is None keep current type if set
                return self._type

            if self.couple:
                return Group.TYPE_DATA

            def is_cache_group_backend(nb):
                if not CACHE_GROUP_PATH_PREFIX:
                    return False
                return nb.base_path.startswith(CACHE_GROUP_PATH_PREFIX)

            is_uncoupled_cache_group = any(is_cache_group_backend(nb) for nb in self.node_backends)
            if is_uncoupled_cache_group:
                return self.TYPE_UNCOUPLED_CACHE

            return Group.TYPE_UNCOUPLED

    def reset_meta(self):
        self.meta = None

    def parse_meta(self, raw_meta):

        if raw_meta is None:
            self.meta = None
        else:
            meta = msgpack.unpackb(raw_meta)

            if isinstance(meta, (tuple, list)):
                self.meta = {'version': 1, 'couple': meta, 'namespace': self.DEFAULT_NAMESPACE, 'frozen': False}
            elif isinstance(meta, dict) and meta['version'] == 2:
                self.meta = meta
            else:
                raise Exception('Unable to parse meta')

        self._type = self._get_type(self.meta)
        logger.debug(
            'Group {group}: meta parsed, group type is determined as "{type}"'.format(
                group=self,
                type=self._type,
            )
        )

    def equal_meta(self, other):
        if type(self.meta) != type(other.meta):
            return False
        if self.meta is None:
            return True

        negligeable_keys = ['service', 'version']
        for key in set(self.meta.keys() + other.meta.keys()):
            if key in negligeable_keys:
                continue
            if self.meta.get(key) != other.meta.get(key):
                return False

        return True

    def get_stat(self):
        return reduce(lambda res, x: res + x, [nb.stat for nb in self.node_backends if nb.stat])

    def update_status_recursive(self):
        if self.couple:
            self.couple.update_status()
            # update status of a couple if group is a part of a groupset
            if self.couple.couple is not self.couple:
                # self.couple is actually a groupset, and self.couple.couple is a couple
                # TODO: replace self.couple with a groupset when new Couple object is
                # implemented
                self.couple.couple.update_status()
        else:
            self.update_status()

    @property
    def effective_space(self):
        return sum(nb.effective_space for nb in self.node_backends)

    @property
    def effective_free_space(self):
        return sum(nb.effective_free_space for nb in self.node_backends)

    def update_status(self):
        """Updates group's own status.
        WARNING: This method should not take into consideration any of the
        groups' state coupled with itself nor any of the couple attributes,
        properties, state, etc."""

        if not self.node_backends:
            logger.info('Group {0}: no node backends, status set to INIT'.format(self.group_id))
            self.status = Status.INIT
            self.status_text = ('Group {0} is in INIT state because there is '
                                'no node backends serving this group'.format(self))
            return self.status

        # TODO: add status INIT for group if .couple is None

        if FORBIDDEN_DHT_GROUPS and len(self.node_backends) > 1:
            self.status = Status.BROKEN
            self.status_text = ('Group {} is in BROKEN state because '
                                'is has {} node backends but only 1 is allowed'.format(
                                    self.group_id, len(self.node_backends)))
            return self.status

        # node statuses should be updated before group status is set
        # statuses = tuple(nb.update_status() for nb in self.node_backends)
        statuses = tuple(nb.status for nb in self.node_backends)

        logger.info('In group {0} meta = {1}'.format(self, str(self.meta)))
        if not self.meta:
            self.status = Status.INIT
            self.status_text = ('Group {0} is in INIT state because meta key '
                                'was not read from it'.format(self))
            return self.status

        if Status.BROKEN in statuses:
            self.status = Status.BROKEN
            self.status_text = ('Group {0} has BROKEN status because '
                                'some node statuses are BROKEN'.format(self))
            return self.status

        if self.type == self.TYPE_DATA or self.type == self.TYPE_LRC_8_2_2_V1:
            # perform checks for common data group
            status = self.update_storage_group_status()
            if status:
                return status
        elif self.type == self.TYPE_CACHE:
            pass

        if Status.RO in statuses:
            self.status = Status.RO
            self.status_text = ('Group {0} is in Read-Only state because '
                                'there is RO node backends'.format(self))

            service_status = self.meta.get('service', {}).get('status')
            if service_status == Status.MIGRATING:
                if self.active_job and self.meta['service']['job_id'] == self.active_job['id']:
                    self.status = Status.MIGRATING
                    self.status_text = ('Group {0} is migrating, job id is {1}'.format(
                        self, self.meta['service']['job_id']))
                else:
                    self.status = Status.BAD
                    self.status_text = ('Group {0} has no active job, but marked as '
                                        'migrating by job id {1}'.format(
                                            self, self.meta['service']['job_id']))

            return self.status

        if not all(st == Status.OK for st in statuses):
            self.status = Status.BAD
            self.status_text = ('Group {0} is in Bad state because '
                                'some node statuses are not OK'.format(self))
            return self.status

        self.status = Status.COUPLED
        self.status_text = 'Group {0} is OK'.format(self)

        return self.status

    def update_storage_group_status(self):
        if not self.meta['couple']:
            self.status = Status.INIT
            self.status_text = ('Group {0} is in INIT state because there is '
                                'no coupling info'.format(self))
            return self.status

        if not self.couple:
            self.status = Status.BAD
            self.status_text = ('Group {0} is in Bad state because '
                                'couple was not created'.format(self))
            return self.status

        if not self.couple.check_groups(self.meta['couple']):
            self.status = Status.BAD
            self.status_text = ('Group {} is in Bad state because couple check fails'.format(self))
            return self.status

        if not self.meta.get('namespace'):
            self.status = Status.BAD
            self.status_text = ('Group {0} is in Bad state because '
                                'no namespace has been assigned to it'.format(self))
            return self.status

        if self.type == TYPE_DATA:
            siblings = self.meta['couple']
        else:
            siblings = self.meta['lrc']['groups']

        if self.group_id not in siblings:
            self.status = Status.BROKEN
            self.status_text = ('Group {0} is in BROKEN state because '
                                'its group id is missing from coupling info'.format(self))
            return self.status

    def info(self):
        g = GroupInfo(self.group_id)
        data = {
            'id': self.group_id,
            'status': self.status,
            'status_text': self.status_text,
            'node_backends': [nb.info() for nb in self.node_backends]
        }

        data['couple'] = None
        if isinstance(self.couple, Groupset):
            data['couple'] = str(self.couple.couple)

        groupset_id = str(self.couple) if self.couple else None
        data['groupset'] = groupset_id

        if self.meta:
            data['namespace'] = self.meta.get('namespace')
        if self.active_job:
            data['active_job'] = self.active_job

        g._set_raw_data(data)
        return g

    def set_active_job(self, job):
        if job is None:
            self.active_job = None
            return
        self.active_job = {
            'id': job.id,
            'type': job.type,
            'status': job.status,
        }

    def compose_cache_group_meta(self):
        return {
            'version': 2,
            'type': self.TYPE_CACHE,
            'namespace': self.CACHE_NAMESPACE,
            'couple': (self.group_id,)
        }

    @staticmethod
    def compose_uncoupled_lrc_group_meta(lrc_groups, scheme):
        if scheme == Lrc.Scheme822v1:
            group_type = Group.TYPE_UNCOUPLED_LRC_8_2_2_V1
        else:
            raise ValueError('Unknown scheme: {}'.format(scheme))
        return {
            'version': 2,
            'type': group_type,
            'lrc_groups': lrc_groups,
        }

    @property
    def coupled_groups(self):
        if not self.couple:
            return []

        return [g for g in self.couple if g is not self]

    def __hash__(self):
        return hash(self.group_id)

    def __str__(self):
        return str(self.group_id)

    def __repr__(self):
        return '<Group object: group_id=%d, status=%s node backends=[%s], meta=%s, couple=%s>' % (self.group_id, self.status, ', '.join((repr(nb) for nb in self.node_backends)), str(self.meta), str(self.couple))

    def __eq__(self, other):
        return self.group_id == other


def status_change_log(f):
    @functools.wraps(f)
    def wrapper(self, *args, **kwargs):
        old_status = self.status
        res = f(self, *args, **kwargs)
        if old_status != self.status:
            logger.info('Couple {0} status updated from {1} to {2} ({3})'.format(
                self, old_status, self.status, self.status_text))
        return res
    return wrapper


class Groupset(object):
    def __init__(self, groups):
        self.status = Status.INIT
        self.namespace = None
        self.groups = sorted(groups, key=lambda group: group.group_id)
        self.meta = None
        for group in self.groups:
            if group.couple:
                raise ValueError(
                    'Group {group} is already in couple {group_couple}'.format(
                        group=group,
                        group_couple=group.couple,
                    )
                )
        for group in self.groups:
            # NOTE: this assignment should not be mixed with previous loop to prevent
            # storing uninitialized Groupset objects as 'couple' in groups
            group.couple = self
        self.status_text = 'Couple {} is not inititalized yet'.format(self)
        self.active_job = None

    def get_stat(self):
        try:
            return reduce(lambda res, x: res * x, [group.get_stat() for group in self.groups])
        except TypeError:
            return None

    def _get_job_service_status(self):
        service_job_types = (
            JobTypes.TYPE_MOVE_JOB,
            JobTypes.TYPE_RESTORE_GROUP_JOB,
            JobTypes.TYPE_ADD_LRC_GROUPSET_JOB,
        )
        running_job_statuses = (jobs.job.Job.STATUS_NEW, jobs.job.Job.STATUS_EXECUTING)
        if self.active_job and self.active_job['type'] in service_job_types:
            if self.active_job['status'] in running_job_statuses:
                return Status(
                    code=Status.SERVICE_ACTIVE,
                    text='Couple {} has active job {}'.format(self, self.active_job['id']),
                )
            else:
                return Status(
                    code=Status.SERVICE_STALLED,
                    text='Couple {} has stalled job {}'.format(self, self.active_job['id']),
                )
        return None

    def _check_dc_sharing(self):
        if FORBIDDEN_DC_SHARING_AMONG_GROUPS:
            # checking if any pair of groups has its node backends in same dc
            groups_dcs = []
            for group in self.groups:
                group_dcs = set()
                for nb in group.node_backends:
                    try:
                        group_dcs.add(nb.node.host.dc)
                    except CacheUpstreamError:
                        raise CacheUpstreamError(
                            'Failed to resolve dc for host {}'.format(nb.node.host)
                        )
                groups_dcs.append(group_dcs)

            dc_set = set()
            for group_dcs in groups_dcs:
                if dc_set & group_dcs:
                    return False
                dc_set = dc_set | group_dcs

        return True

    @status_change_log
    def update_status(self):
        for group in self.groups:
            group.update_status()

        self.active_job = None
        for group in self.groups:
            if group.active_job:
                self.active_job = group.active_job
                break

        new_status = self._calculate_status()
        self.status = new_status.code
        self.status_text = new_status.text

    def _calculate_status(self):
        raise NotImplemented('Method should be implemented in a derived class')

    def _get_meta_unavailable_status(self):
        for group in self.groups:
            if not group.meta:
                return (
                    self._get_job_service_status() or
                    Status(
                        code=Status.BAD,
                        text="Couple's group {} has empty meta data".format(group),
                    )
                )
        return None

    def _get_improper_namespace_status(self):
        # NOTE: this check should be evaluated after '_get_meta_unavailable_status()'
        # because it relies on group's 'meta' availability
        for group in self.groups:
            if self.namespace != group.meta.get('namespace'):
                status_text = (
                    "Couple {couple} namespace '{couple_ns}' does not match namespace "
                    "in group {group} meta data '{group_ns}'".format(
                        couple=self,
                        couple_ns=self.namespace,
                        group=group,
                        group_ns=group.meta.get('namespace'),
                    )
                )
                return Status(
                    code=Status.BAD,
                    text=status_text,
                )
        return None

    def _get_unequal_meta_status(self):
        # NOTE: this check should be evaluated after '_get_meta_unavailable_status()'
        # because it relies on group's 'meta' availability
        if not all(self.groups[0].equal_meta(group) for group in self.groups):
            return (
                self._get_job_service_status() or
                Status(
                    code=Status.BAD,
                    text='Couple {} groups have unequal meta data'.format(self),
                )
            )
        return None

    def _get_couple_frozen_status(self):
        # NOTE: this check should be evaluated after '_get_meta_unavailable_status()'
        # because it relies on group's 'meta' availability
        if any(group.meta.get('frozen') for group in self.groups):
            return Status(
                code=Status.FROZEN,
                text='Couple {} is frozen'.format(self),
            )
        return None

    def _get_unset_namespace_settings_status(self):
        if FORBIDDEN_NS_WITHOUT_SETTINGS:
            if self.namespace.id == Group.CACHE_NAMESPACE:
                return None
            if not infrastructure.ns_settings.get(self.namespace.id):
                status_text = (
                    'Couple {couple} is assigned to the namespace {namespace}, '
                    'which is not set up'.format(
                        couple=self,
                        namespace=self.namespace,
                    )
                )
                return Status(
                    code=Status.BROKEN,
                    text=status_text,
                )

        return None

    def _get_dc_sharing_status(self):
        try:
            if not self._check_dc_sharing():
                return Status(
                    code=Status.BROKEN,
                    text='Couple {} has nodes sharing the same DC'.format(self),
                )
        except CacheUpstreamError as e:
            return Status(
                code=Status.BAD,
                text=str(e),
            )
        return None

    def _get_broken_groups_status(self):
        if any(g.status == Status.BROKEN for g in self.groups):
            return Status(
                code=Status.BROKEN,
                text='Couple {} has broken groups'.format(self),
            )
        return None

    def _get_bad_groups_status(self):
        if any(g.status == Status.BAD for g in self.groups):
            status_text = (
                'Couple {couple} has bad groups: [{groups_desc}]'.format(
                    couple=self,
                    groups_desc='; '.join(
                        '{group}: {status_text}'.format(group=g, status_text=g.status_text)
                        for g in self.groups
                        if g.status == Status.BAD
                    )
                )
            )
            return (
                self._get_job_service_status() or
                Status(
                    code=Status.BAD,
                    text=status_text,
                )
            )
        return None

    def _get_unmatched_total_space_status(self):
        if FORBIDDEN_UNMATCHED_GROUP_TOTAL_SPACE:
            group_stats = [g.get_stat() for g in self.groups]
            total_spaces = [gs.total_space for gs in group_stats if gs]
            if any(ts != total_spaces[0] for ts in total_spaces):
                return Status(
                    code=Status.BROKEN,
                    text='Couple {} has unequal total space in groups'.format(self),
                )
        return None

    def check_groups(self, groups):

        for group in self.groups:
            if group.meta is None or 'couple' not in group.meta or not group.meta['couple']:
                return False

            if set(groups) != set(group.meta['couple']):
                return False

        if set(groups) != set((g.group_id for g in self.groups)):
            return False

        return True

    @property
    def frozen(self):
        return any(group.meta.get('frozen') for group in self.groups if group.meta)

    @property
    def closed(self):
        return self.status == Status.FULL

    def destroy(self):
        if self.namespace:
            self.namespace.remove_couple(self)
        for group in self.groups:
            group.couple = None
            group.update_status()

        global groupsets
        if self in groupsets:
            groupsets.remove_groupset(self)
        self.groups = []
        self.status = Status.INIT
        self.couple = None

    RESERVED_SPACE_KEY = 'reserved-space-percentage'

    def is_full(self):

        ns_reserved_space = infrastructure.ns_settings.get(self.namespace.id, {}).get(self.RESERVED_SPACE_KEY, 0.0)

        # TODO: move this logic to effective_free_space property,
        #       it should handle all calculations by itself
        for group in self.groups:
            for nb in group.node_backends:
                if nb.is_full(ns_reserved_space):
                    return True

        if self.effective_free_space <= 0:
            return True

        return False

    @property
    def groups_effective_space(self):
        return min(g.effective_space for g in self.groups)

    @property
    def ns_reserved_space_percentage(self):
        return infrastructure.ns_settings.get(self.namespace.id, {}).get(self.RESERVED_SPACE_KEY, 0.0)

    @property
    def ns_reserved_space(self):
        return int(math.ceil(self.groups_effective_space * self.ns_reserved_space_percentage))

    @property
    def effective_space(self):
        return int(math.floor(
            self.groups_effective_space * (1.0 - self.ns_reserved_space_percentage)
        ))

    @property
    def effective_free_space(self):
        stat = self.get_stat()
        if not stat:
            return 0
        return int(max(stat.free_space -
                       (stat.total_space - self.effective_space), 0))

    @property
    def free_reserved_space(self):
        stat = self.get_stat()
        if not stat:
            return 0
        reserved_space = self.ns_reserved_space
        groups_free_eff_space = stat.free_space - (stat.total_space - self.groups_effective_space)
        if groups_free_eff_space <= 0:
            # when free space is less than what was reserved for service demands
            return 0
        if groups_free_eff_space > reserved_space:
            # when free effective space > 0
            return reserved_space
        return groups_free_eff_space

    def as_tuple(self):
        return tuple(group.group_id for group in self.groups)

    def info_data(self):
        data = {'id': str(self),
                'status': self.status,
                'status_text': self.status_text,
                'type': GROUPSET_REPLICAS,
                'settings': {},
                'tuple': self.as_tuple()}
        try:
            data['namespace'] = self.namespace.id
        except ValueError:
            pass

        data['effective_space'] = 0
        data['free_effective_space'] = 0
        data['free_reserved_space'] = 0

        stat = self.get_stat()
        if stat:
            try:
                data['effective_space'] = self.effective_space
                data['free_effective_space'] = self.effective_free_space
                data['free_reserved_space'] = self.free_reserved_space
            except ValueError:
                # failed to determine couple's namespace
                pass

        data['groups'] = [g.info().serialize() for g in self.groups]

        # Renaming 'tuple' to 'group_ids' and keeping it backward-compatible for
        # a while
        data['group_ids'] = data['tuple']

        return data

    @property
    def keys_diff(self):
        group_keys = []
        for group in self.groups:
            if not len(group.node_backends):
                continue
            group_keys.append(group.get_stat().files)
        if not group_keys:
            return None
        max_keys = max(group_keys)
        return sum(max_keys - gk for gk in group_keys)

    FALLBACK_HOSTS_PER_DC = config.get('fallback_hosts_per_dc', 10)

    def groupset_hosts(self):
        hosts = {'primary': [],
                 'fallback': []}
        used_hosts = set()
        used_dcs = set()

        def serialize_node(node):
            return {
                'host': node.host.hostname,
                'dc': node.host.dc,
            }

        for group in self.groups:
            for nb in group.node_backends:
                node = nb.node
                if node.host in used_hosts:
                    continue
                try:
                    hosts['primary'].append(serialize_node(node))
                except CacheUpstreamError:
                    continue
                used_hosts.add(node.host)
                used_dcs.add(node.host.dc)

        for dc in used_dcs:
            count = 0
            for node in dc_host_view[dc].by_la:
                if node.host in used_hosts:
                    continue
                try:
                    hosts['fallback'].append(serialize_node(node))
                except CacheUpstreamError:
                    continue
                used_hosts.add(node.host)
                count += 1
                if count >= self.FALLBACK_HOSTS_PER_DC:
                    break

        return hosts

    def __contains__(self, group):
        return group in self.groups

    def __iter__(self):
        return self.groups.__iter__()

    def __len__(self):
        return len(self.groups)

    def __str__(self):
        return ':'.join(str(group) for group in self.groups)

    def __hash__(self):
        return hash(self.__str__())

    def __eq__(self, other):
        if isinstance(other, (str, unicode)):
            return self.__str__() == other

        if isinstance(other, Couple):
            return self.groups == other.groups

    def __repr__(self):
        return '<Couple object: status={status}, groups=[{groups}] >'.format(
            status=self.status,
            groups=', '.join(repr(g) for g in self.groups),
        )


class Couple(Groupset):

    READ_PREFERENCE = 'read_preference'

    DEFAULT_SETTINGS = {
        READ_PREFERENCE: [GROUPSET_REPLICAS],
    }

    def __init__(self, groups):
        super(Couple, self).__init__(groups)
        # TODO: temporary variable to provide connection
        # between replicas groupset and lrc groupset,
        # should be removed when new "Couple" object is
        # introduced
        self.lrc822v1_groupset = None

        # TODO: this should be a link to a new "Couple" instance
        self.couple = self

        self.settings = self.DEFAULT_SETTINGS

    def info_data(self):
        data = super(Couple, self).info_data()

        # Replicas groupset should have 'couple' key
        data['couple'] = str(self.couple)

        # imitation of future "Couple"
        data['groupsets'] = {}

        # TODO: stop this nonsense when 'replicas' groupset is implemented
        # What am I doing? Renaming parameters!!!
        data['couple_status'] = self.status
        data['couple_status_text'] = self.status_text

        stat = self.get_stat()
        if stat:
            # TODO: make sure no one uses it
            data['free_space'] = int(stat.free_space)
            data['used_space'] = int(stat.used_space)

        if self.lrc822v1_groupset:
            data['groupsets'][Group.TYPE_LRC_8_2_2_V1] = self.lrc822v1_groupset.info().serialize()

            # NOTE: this is a temporary workaround for replicas groupset
            disabled_replicas_groupset = (
                GROUPSET_REPLICAS not in self.settings['read_preference'] and
                all(group.status == Status.INIT for group in self.groups)
            )

            if disabled_replicas_groupset:
                # NOTE: overwriting common groups info
                data['groups'] = []
                data['group_ids'] = []

        data['settings'] = self.settings
        # TODO: temporary backward compatibility, remove after libmastermind
        # refactoring
        data['read_preference'] = self.settings['read_preference']

        data['hosts'] = {
            'primary': []
        }

        return data

    def _calculate_status(self):

        if self.lrc822v1_groupset:
            # checking if couple has only one groupset - lrc groupset
            if all(g.status == Status.INIT and len(g.node_backends) == 0
                   for g in self.groups):
                # replicas groupset is detached, lrc groupset is present
                return Status(
                    code=Status.ARCHIVED,
                    text='Couple {} is archived'.format(self),
                )

        # TODO: this checks should be evaluated after
        # potentially threatening checks like bad_groups_status, etc.
        meta_status = (
            self._get_meta_unavailable_status() or
            self._get_improper_namespace_status() or
            self._get_unequal_meta_status() or
            self._get_couple_frozen_status()
        )
        if meta_status:
            return meta_status

        settings_status = (
            self._get_unset_namespace_settings_status() or
            self._get_dc_sharing_status() or
            self._get_broken_groups_status()
        )
        if settings_status:
            return settings_status

        bad_groups_status = self._get_bad_groups_status()
        if bad_groups_status:
            return bad_groups_status

        if self.lrc822v1_groupset:
            lrc_groups_status = (
                self._get_ro_groups_status() or
                self._get_migrating_groups_status()
            )
            if lrc_groups_status:
                return lrc_groups_status

            if all(g.status == Status.COUPLED for g in self.groups):
                # couple with lrc groupset is in good state
                return Status(
                    code=Status.ARCHIVED,
                    text='Couple {} is archived'.format(self),
                )
        else:
            groups_status = (
                self._get_ro_groups_status() or
                self._get_migrating_groups_status() or
                self._get_init_groups_status() or
                self._get_stalled_groups_status()
            )
            if groups_status:
                return groups_status

            if all(g.status == Status.COUPLED for g in self.groups):
                # couple without lrc groupset is in good state

                # TODO: move this check to meta_status group checks?
                settings_status = self._get_unmatched_total_space_status()
                if settings_status:
                    return settings_status

                if self.is_full():
                    return Status(
                        code=Status.FULL,
                        text='Couple {} is full'.format(self),
                    )
                else:
                    return Status(
                        code=Status.OK,
                        text='Couple {} is OK'.format(self),
                    )

        return Status(
            code=Status.BAD,
            text='Couple {} is bad for some reason'.format(self),
        )

    def _get_ro_groups_status(self):
        if any(g.status == Status.RO for g in self.groups):
            return (
                self._get_job_service_status() or
                Status(
                    code=Status.BAD,
                    text='Couple {} has read-only groups'.format(self),
                )
            )
        return None

    def _get_migrating_groups_status(self):
        if any(g.status == Status.MIGRATING for g in self.groups):
            return (
                self._get_job_service_status() or
                Status(
                    code=Status.BAD,
                    text='Couple {} has migrating groups'.format(self),
                )
            )
        return None

    def _get_init_groups_status(self):
        if any(g.status == Status.INIT for g in self.groups):
            return (
                self._get_job_service_status() or
                Status(
                    code=Status.BAD,
                    text='Couple {} has groups that are not initialized'.format(self),
                )
            )
        return None

    def _get_stalled_groups_status(self):
        if any(g.status == Status.STALLED for g in self.groups):
            return (
                self._get_job_service_status() or
                Status(
                    code=Status.BAD,
                    text='Couple {} has stalled groups'.format(self),
                )
            )
        return None

    def compose_group_meta(self, couple, settings):
        return {
            'version': 2,
            'couple': couple.as_tuple(),
            'namespace': couple.namespace.id,
            'frozen': bool(settings['frozen']),
        }

    def info(self):
        c = CoupleInfo(str(self))
        c._set_raw_data(self.info_data())
        return c

    @property
    def groupset_settings(self):
        return {
            'frozen': self.frozen,
        }

    @staticmethod
    def check_settings(settings):
        if 'frozen' in settings:
            if not isinstance(settings['frozen'], bool):
                raise ValueError('Replicas groupset "frozen" setting must be bool')


class Lrc822v1Groupset(Groupset):
    def __init__(self, groups):
        super(Lrc822v1Groupset, self).__init__(groups)
        # TODO: this should be a link to a new "Couple" instance
        self.couple = None
        self.scheme = Lrc.Scheme822v1.ID
        self.part_size = None

    def info_data(self):
        data = super(Lrc822v1Groupset, self).info_data()

        data['type'] = GROUPSET_LRC
        data['settings'] = {
            'scheme': self.scheme,
            'part_size': self.part_size,
        }
        if self.couple:
            data['couple'] = str(self.couple)
        else:
            data['couple'] = None

        return data

    def _check_dc_sharing(self):
        # LRC groupsets are allowed to share dcs
        return True

    def update_status(self):
        # update groupset attributes
        metas = [g.meta for g in self.groups if g.meta and 'lrc' in g.meta]
        if metas:
            part_sizes = filter(None, (meta['lrc'].get('part_size') for meta in metas))
            if not part_sizes:
                raise ValueError('"part_size" is not set in metakey')
            self.part_size = part_sizes[0]

        super(Lrc822v1Groupset, self).update_status()

    def _calculate_status(self):
        lrc_data_parts_status = (
            self._get_data_unavailable_status() or
            self._get_indices_unavailable_status()
        )
        if lrc_data_parts_status:
            return lrc_data_parts_status

        # TODO: this checks should be evaluated after
        # potentially threatening checks like bad_groups_status, etc.
        meta_status = (
            self._get_meta_unavailable_status() or
            self._get_improper_namespace_status() or
            self._get_unequal_part_size_status() or
            self._get_improper_lrc_scheme_settings_status() or
            self._get_unequal_meta_status()
        )
        if meta_status:
            return meta_status

        settings_status = (
            self._get_unset_namespace_settings_status() or
            self._get_broken_groups_status()
        )
        if settings_status:
            return settings_status

        groups_status = self._get_not_coupled_lrc_groups_status()
        if groups_status:
            return groups_status

        if all(g.status == Status.COUPLED for g in self.groups):
            # lrc groupset is in good state
            return Status(
                code=Status.ARCHIVED,
                text='Lrc groupset {} is archived'.format(self),
            )

        return Status(
            code=Status.BAD,
            text='Groupset {} is bad for some reason'.format(self),
        )

    @property
    def _unavailable_data_parts_indices(self):
        return [
            idx
            for idx, group in enumerate(self.groups)
            if group.status != Status.COUPLED
        ]

    def _get_data_unavailable_status(self):
        unavailable_data_parts_indices = self._unavailable_data_parts_indices
        if Lrc.Scheme822v1.is_data_partially_unavailable(unavailable_data_parts_indices):
            status_text = (
                'Data is partially unavailable (groups {groups} are not ok)'.format(
                    groups=', '.join(
                        str(self.groups[idx])
                        for idx in unavailable_data_parts_indices
                    )
                )
            )
            return Status(
                code=Status.BAD_DATA_UNAVAILABLE,
                text=status_text,
            )
        return None

    def _get_indices_unavailable_status(self):
        unavailable_data_parts_indices = self._unavailable_data_parts_indices
        bad_shard_indices = Lrc.Scheme822v1.get_unavailable_index_shard_indices(
            unavailable_data_parts_indices
        )
        if bad_shard_indices:
            status_text = (
                'Indices are partially unavailable (groups {groups} are not ok)'.format(
                    groups=', '.join(
                        str(self.groups[idx])
                        for idx in bad_shard_indices
                    )
                )
            )
            return Status(
                code=Status.BAD_INDICES_UNAVAILABLE,
                text=status_text,
            )
        return None

    def _get_unequal_part_size_status(self):
        # NOTE: this check should be evaluated after '_get_meta_unavailable_status()'
        # because it relies on group's 'meta' availability
        for g in self.groups:
            part_size = g.meta['lrc'].get('part_size')
            if part_size != self.part_size:
                status_text = (
                    'part_size does not match, groupset {groupset} has part_size = '
                    '{groupset_part_size}, group {group} has part_size = '
                    '{group_part_size}'.format(
                        groupset=self,
                        groupset_part_size=self.part_size,
                        group=g,
                        group_part_size=part_size,
                    )
                )
                return Status(
                    code=Status.BROKEN,
                    text=status_text,
                )
        return None

    def _get_improper_lrc_scheme_settings_status(self):
        # NOTE: this check should be evaluated after '_get_meta_unavailable_status()'
        # because it relies on group's 'meta' availability
        for g in self.groups:
            scheme = g.meta['lrc'].get('scheme')
            if scheme != self.scheme:
                status_text = (
                    'scheme does not match, groupset {groupset} has scheme = '
                    '{groupset_scheme}, group {group} has scheme = '
                    '{group_scheme}'.format(
                        groupset=self,
                        groupset_scheme=self.scheme,
                        group=g,
                        group_scheme=scheme,
                    )
                )
                return Status(
                    code=Status.BROKEN,
                    text=status_text,
                )
        return None

    def _get_not_coupled_lrc_groups_status(self):

        if not all(g.status == Status.COUPLED for g in self.groups):
            status_text = (
                'Couple {couple} has groups with unexpected '
                'status: [{groups_desc}], should all be COUPLED'.format(
                    couple=self,
                    groups_desc='; '.join(
                        '{group}: {status}'.format(group=g, status=g.status)
                        for g in self.groups
                        if g.status != Status.COUPLED
                    )
                )
            )
            return Status(
                code=Status.BAD,
                text=status_text,
            )
        return None

    def compose_group_meta(self, couple, settings):
        return {
            'version': 2,
            'couple': couple.as_tuple(),
            'namespace': couple.namespace.id,
            'frozen': couple.frozen,
            'type': Group.TYPE_LRC_8_2_2_V1,
            'lrc': {
                'groups': [g.group_id for g in self.groups],
                'part_size': settings['part_size'],
                'scheme': Lrc.Scheme822v1.ID,
            },
        }

    def info(self):
        c = GroupsetInfo(str(self))
        c._set_raw_data(self.info_data())
        return c

    def check_groups(self, groups):
        for group in self.groups:
            if group.meta is None or 'lrc' not in group.meta or 'groups' not in group.meta['lrc'] or not group.meta['lrc']['groups']:
                return False

            if set(groups) != set(group.meta['lrc']['groups']):
                return False

        if set(groups) != set((g.group_id for g in self.groups)):
            return False

        return True

    @property
    def groupset_settings(self):
        return {
            'scheme': Lrc.Scheme822v1.ID,
            'part_size': self.part_size,
        }

    @staticmethod
    def check_settings(settings):
        if 'scheme' not in settings:
            raise ValueError('Lrc groupset requires "scheme" setting')

        if not Lrc.check_scheme(settings['scheme']):
            raise ValueError('Unknown LRC scheme "{}"'.format(settings['scheme']))

        if 'part_size' not in settings:
            raise ValueError('Lrc groupset requires "part_size" setting')

        part_size = settings['part_size']
        if not isinstance(part_size, int) or part_size <= 0:
            raise ValueError('"part_size" must be a positive integer')

    @property
    def groups_effective_space(self):
        return sum(
            g.effective_space
            for g in self.groups[:Lrc.Scheme822v1.NUM_DATA_PARTS]
        )

    @property
    def ns_reserved_space_percentage(self):
        return 0

    @property
    def ns_reserved_space(self):
        return 0

    @property
    def effective_space(self):
        return self.groups_effective_space

    @property
    def effective_free_space(self):
        return sum(
            g.effective_free_space
            for g in self.groups[:Lrc.Scheme822v1.NUM_DATA_PARTS]
        )


class DcNodes(object):
    def __init__(self):
        self.nodes = []
        self.__by_la = None

    def append(self, node):
        self.nodes.append(node)

    @property
    def by_la(self):
        if self.__by_la is None:
            self.__by_la = sorted(self.nodes, key=lambda node: node.stat.load_average)
        return self.__by_la


class DcHostView(object):

    def __init__(self):
        self.dcs_hosts = {}

    def update(self):
        dcs_hosts = {}
        # TODO: iterate through hosts when host statistics will be moved
        # to a separate object
        hosts = set()
        for node in nodes:
            dc_hosts = dcs_hosts.setdefault(node.host.dc, DcNodes())
            if node.host in hosts:
                continue
            if node.stat is None:
                continue
            dc_hosts.append(node)
            hosts.add(node.host)
        self.dcs_hosts = dcs_hosts

    def __getitem__(self, key):
        return self.dcs_hosts[key]


class Namespace(object):
    def __init__(self, id):
        self.id = id
        self.couples = set()

        self.groupsets = Groupsets(
            replicas=Repositary(Couple, 'Replicas groupset'),
            lrc=Repositary(Lrc822v1Groupset, 'LRC groupset'),
            resource_desc='Groupset',
        )

        # TODO: this is obsolete, used for backward compatibility,
        # remove when not used anymore
        self.couples = self.groupsets.replicas

    def add_couple(self, couple):
        if couple.namespace:
            raise ValueError(
                'Couple {couple} already belongs to namespace {couple_namespace}, '
                'cannot be assigned to namespace {namespace}'.format(
                    couple=couple,
                    couple_namespace=couple.namespace,
                    namespace=self,
                )
            )
        self.groupsets.add_groupset(couple)
        couple.namespace = self

    def add_groupset(self, groupset):
        if groupset.namespace:
            raise ValueError(
                'Groupset {groupset} already belongs to namespace {groupset_namespace}, '
                'cannot be assigned to namespace {namespace}'.format(
                    groupset=groupset,
                    groupset_namespace=groupset.namespace,
                    namespace=self,
                )
            )
        self.groupsets.add_groupset(groupset)
        groupset.namespace = self

    def remove_couple(self, couple):
        self.groupsets.remove_groupset(couple)
        couple.namespace = None

    def __str__(self):
        return self.id

    def __hash__(self):
        return hash(str(self))

    def __eq__(self, other):
        if isinstance(other, types.StringTypes):
            return str(self) == other

        if isinstance(other, Namespace):
            return self.id == other.id

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return '<Namespace: id={id} >'.format(id=self.id)


hosts = Repositary(Host)
groups = Repositary(Group)
nodes = Repositary(Node)
node_backends = Repositary(NodeBackend, 'Node backend')
namespaces = Repositary(Namespace)
fs = Repositary(Fs)

dc_host_view = DcHostView()

replicas_groupsets = Repositary(Couple, 'Replicas groupset')

# TOOD: use namespace "storage_cache" couples instead of cache couples,
# this is added for backward compatibility
cache_ns = namespaces.add(Group.CACHE_NAMESPACE)
cache_couples = cache_ns.groupsets.replicas

lrc_groupsets = Repositary(Lrc822v1Groupset, 'LRC groupset')
groupsets = Groupsets(
    replicas=replicas_groupsets,
    lrc=lrc_groupsets,
    resource_desc='Groupset',
)

# TODO: backward compatibility, remove
couples = groupsets
