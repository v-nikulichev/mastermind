from job_types import JobTypes
from move import MoveJob
from recover_dc import RecoverDcJob
from couple_defrag import CoupleDefragJob
from restore_group import RestoreGroupJob
from make_lrc_groups import MakeLrcGroupsJob


class JobFactory(object):

    @staticmethod
    def make_job(data):
        job_type = data.get('type')
        if job_type == JobTypes.TYPE_MOVE_JOB:
            return MoveJob.from_data(data)
        elif job_type == JobTypes.TYPE_RECOVER_DC_JOB:
            return RecoverDcJob.from_data(data)
        elif job_type == JobTypes.TYPE_COUPLE_DEFRAG_JOB:
            return CoupleDefragJob.from_data(data)
        elif job_type == JobTypes.TYPE_RESTORE_GROUP_JOB:
            return RestoreGroupJob.from_data(data)
        elif job_type == JobTypes.TYPE_MAKE_LRC_GROUPS_JOB:
            return MakeLrcGroupsJob.from_data(data)
        raise ValueError('Unknown job type {0}'.format(job_type))
