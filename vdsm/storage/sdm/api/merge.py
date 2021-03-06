#
# Copyright 2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

"""
This is (cold) merge data operation job.
This job performs the following steps:
1. Prepare all volumes in chain
2. Executes qemuimg commit
3. Tears down the image
"""

from __future__ import absolute_import

import logging

from vdsm import qemuimg
from vdsm import utils

from vdsm.storage import constants as sc
from vdsm.storage import exception as se
from vdsm.storage import guarded

from . import base


class Job(base.Job):
    log = logging.getLogger('storage.sdm.merge')

    def __init__(self, job_id, subchain):
        super(Job, self).__init__(job_id, 'merge_subchain',
                                  subchain.host_id)
        self.subchain = subchain
        self.operation = None

    @property
    def progress(self):
        return getattr(self.operation, 'progress', None)

    def _run(self):
        self.log.info("Merging subchain %s", self.subchain)
        with guarded.context(self.subchain.locks):
            self.subchain.validate()
            # Base volume must be ILLEGAL. Otherwise, VM could be run while
            # performing cold merge.
            base_legality = self.subchain.base_vol.getLegality()
            if base_legality == sc.LEGAL_VOL:
                raise se.UnexpectedVolumeState(self.subchain.base_id,
                                               sc.ILLEGAL_VOL,
                                               base_legality)

            with self.subchain.prepare(), self.subchain.volume_operation():
                self.operation = qemuimg.commit(
                    self.subchain.top_vol.getVolumePath(),
                    topFormat=sc.fmt2str(self.subchain.top_vol.getFormat()),
                    base=self.subchain.base_vol.getVolumePath())
                with utils.closing(self.operation):
                    self.operation.wait_for_completion()
