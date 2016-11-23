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
xlease - manage external leases

External leases are stored in the xleases special volume. A lease is a 2048
blocks area at some offset in the xleases volume, associated with a lockspace
(the domain id) and a unique name. Sanlock does not manage the mapping between
the lease name and the offset of the lease; this module removes this gap.

This module manages the mapping between Sanlock resource name and lease offset.
When creating a lease, we find the first free slot, allocate it for the lease,
and create a sanlock resource at the associated offset. If the xleases volume
is full, we extend it to make room for more leases. This operation must be
performed only on the SPM.

Once a lease is created, any host can get the lease offset using the lease id
and use the lease offset to acquire the sanlock resource.

When removing a lease, we clear the sanlock resource and mark the slot as free
in the index. This operation must also be done on the SPM.

Sanlock keeps the lockspace name and the resource name in the lease area.  We
can rebuild the mapping from lease id to lease offset by reading all the
resources in a volume . The index is actually a cache of the actual data on
storage.

The index format is:

block       used for
---------------------------------
0-3         metadata
4-503       lease records 0-3999
504-2047    unused

The lease offset is:

    lease_base + record_number * lease_size

"""

from __future__ import absolute_import

import io
import logging
import mmap
import os
import sys
import time

from collections import namedtuple

from vdsm import utils
from vdsm.common.osutils import uninterruptible

# TODO: Support 4K block size.  This should be encapsulated in the Index class
# instead of being a module constant.  We can can get the block size using
# sanlock.get_alignment(), ensuring that both vdsm and sanlock are using same
# size.
from vdsm.storage.constants import BLOCK_SIZE

# Size required for Sanlock lease.
LEASE_SIZE = 2048 * BLOCK_SIZE

# The first lease slot is used for the index.
LEASE_BASE = LEASE_SIZE

# The first blocks are used for index matadata
METADATA_SIZE = 4 * BLOCK_SIZE

# The offset of the first lease record
RECORD_BASE = METADATA_SIZE

# The number of lease records supported. We can use up 16352 records, but I
# don't expect that we will need more than 2000 vm leases per data center.  To
# be on the safe size, lets double that number.  Note that we need 1GiB lease
# space for 1024 leases.
MAX_RECORDS = 4000

# Size allocated for each lease record. The minimal size is 36 bytes using uuid
# string. To simplify record number calculation, we use the next power of 2.
# We use the extra space for metadata about each lease record.
RECORD_SIZE = 64

# Each lookup will read this size from storage.
INDEX_SIZE = METADATA_SIZE + (MAX_RECORDS * RECORD_SIZE)

# Record format - everything is text to make it easy to debug using standard
# tools like less and grep, but using fixed width to make it efficient if we
# integrate it into sanlock later.
#
# The format is:
#
#     <resource-name>:<state>:<timestamp>:<padding>\n
#
# Used record::
#
#     34e5a2a8-1a4d-45a0-a4b2-c88157f7a5a9:U:1479914506:0000000000000\n
#
# Stale record::
#
#     cce4623a-6229-447f-8156-c8d61d826085:S:1479914511:0000000000000\n
#
# Free record::
#
#     00000000-0000-0000-0000-000000000000:F:1479914552:0000000000000\n

# A sanlock resource exists for this lease record.
RECORD_USED = b"U"

# Record is not used.
RECORD_FREE = b"F"

# Add or remove operation is in progress or was interrupted. The record should
# be rebuild from storage.
RECORD_STALE = b"S"

# State names reported in leases
RECORD_STATES = {
    RECORD_USED: "USED",
    RECORD_FREE: "FREE",
    RECORD_STALE: "STALE",
}

RECORD_SEP = b":"
RECORD_TERM = b"\n"

# Placeholder lease id for free records.
BLANK_UUID = "00000000-0000-0000-0000-000000000000"

PY2 = sys.version_info[0] == 2

log = logging.getLogger("storage.xlease")

# TODO: Move errors to storage.exception?


class Error(Exception):
    msg = None

    def __init__(self, lease_id):
        self.lease_id = lease_id

    def __str__(self):
        return self.msg.format(self=self)


class NoSuchLease(Error):
    msg = "No such lease {self.lease_id}"


class LeaseExists(Error):
    msg = "Lease {self.lease_id} exists since {self.modified}"

    def __init__(self, lease_id, modified):
        self.lease_id = lease_id
        self.modified = modified


class StaleLease(Error):
    msg = "Lease {self.lease_id} is stale since {self.modified}"

    def __init__(self, lease_id, modified):
        self.lease_id = lease_id
        self.modified = modified


class NoSpace(Error):
    msg = "No space to add lease {self.lease_id}"


class InvalidRecord(Error):
    msg = "Inavlid record ({self.reason}): {self.record}"

    def __init__(self, reason, record):
        self.reason = reason
        self.record = record


LeaseInfo = namedtuple("LeaseInfo", (
    "lockspace",        # Sanlock lockspace name
    "resource",         # Sanlock resource name
    "path",             # Path to lease file or block device
    "offset",           # Offset in lease file
    "modified",         # Modification time in seconds since epoch
))


class Record(object):

    @classmethod
    def frombytes(cls, record):
        """
        Parse record data from storage and create a Record object.

        Arguments:
            record (bytes): record data, 64 bytes

        Returns:
            Record object

        Raises:
            InvalidRecord if record is not in the right format or a field
                cannot be parsed.
        """
        if len(record) != RECORD_SIZE:
            raise InvalidRecord("incorrect length", record)

        try:
            resource, state, modified, padding = record.split(RECORD_SEP, 4)
        except ValueError:
            raise InvalidRecord("incorrect number of fields", record)

        try:
            resource = resource.decode("ascii")
        except UnicodeDecodeError:
            raise InvalidRecord("cannot decode resource %r" % resource, record)

        if state not in (RECORD_USED, RECORD_FREE, RECORD_STALE):
            raise InvalidRecord("invalid state %r" % state, record)

        try:
            modified = int(modified)
        except ValueError:
            raise InvalidRecord("cannot parse timestamp %r" % modified, record)

        return cls(resource, state, modified=modified)

    def __init__(self, resource, state, modified=None):
        """
        Initialize a record.

        Arguments:
            resource (string): UUID string
            state (enum): record state (RECORD_USED, RECORD_STALE, RECORD_FREE)
            modified (int): modification time in seconds since the epoch
        """
        if modified is None:
            modified = int(time.time())
        self._resource = resource
        self._state = state
        self._modified = modified

    def bytes(self):
        """
        Returns record data in storage format.

        Returns:
            bytes object.
        """
        data = (self._resource.encode("ascii") +
                RECORD_SEP +
                self._state +
                RECORD_SEP +
                b"%010d" % self._modified +
                RECORD_SEP)
        padding = RECORD_SIZE - len(data) - 1
        data += b"0" * padding + RECORD_TERM
        return data

    @property
    def resource(self):
        return self._resource

    @property
    def state(self):
        return self._state

    @property
    def modified(self):
        return self._modified


class Index(object):
    """
    Lease index stored at the start of the xleases volume.

    The index is read from stroage when creating an instance, but never read
    again from storage. To update the index from storage, recreate it.

    Changes to the index are written immediately back to storage.
    """

    def __init__(self, lockspace, file):
        log.debug("Loading index for lockspace %r from %r",
                  lockspace, file.name)
        self._lockspace = lockspace
        self._file = file
        self._buf = IndexBuffer(file)

    @property
    def lockspace(self):
        return self._lockspace

    @property
    def path(self):
        return self._file.name

    def lookup(self, lease_id):
        """
        Lookup lease by lease_id and return LeaseInfo if found.

        Raises:
        - NoSuchLease if lease is not found.
        - InvalidRecord if corrupted lease record is found
        - OSError if io operation failed
        """
        # TODO: validate lease id is lower case uuid
        log.debug("Looking up lease %r in lockspace %r",
                  lease_id, self._lockspace)
        recnum = self._buf.find_record(lease_id)
        if recnum == -1:
            raise NoSuchLease(lease_id)

        record = self._buf.read_record(recnum)
        if record.state == RECORD_STALE:
            raise StaleLease(lease_id, record.modified)

        return LeaseInfo(self._lockspace, lease_id, self._file.name,
                         self._lease_offset(recnum), record.modified)

    def add(self, lease_id):
        """
        Add lease to index, returning LeaseInfo.

        Raises:
        - LeaseExists if lease already stored for lease_id
        - InvalidRecord if corrupted lease record is found
        - NoSpace if all slots are allocated
        - OSError if I/O operation failed
        """
        # TODO: validate lease id is lower case uuid
        log.info("Adding lease %r in lockspace %r",
                 lease_id, self._lockspace)
        recnum = self._buf.find_record(lease_id)
        if recnum != -1:
            record = self._buf.read_record(recnum)
            if record.state == RECORD_STALE:
                # TODO: rebuild this record instead of failing
                raise StaleLease(lease_id, record.modified)
            else:
                raise LeaseExists(lease_id, record.modified)

        recnum = self._buf.find_record(BLANK_UUID)
        if recnum == -1:
            raise NoSpace(lease_id)

        record = Record(lease_id, RECORD_USED)
        self._buf.write_record(recnum, record)
        self._buf.dump_record(recnum, self._file)

        return LeaseInfo(self._lockspace, lease_id, self._file.name,
                         self._lease_offset(recnum), record.modified)

    def remove(self, lease_id):
        """
        Remove lease from index

        Raises:
        - NoSuchLease if lease was not found
        - OSError if I/O operation failed
        """
        # TODO: validate lease id is lower case uuid
        log.info("Removing lease %r in lockspace %r",
                 lease_id, self._lockspace)
        recnum = self._buf.find_record(lease_id)
        if recnum == -1:
            raise NoSuchLease(lease_id)

        record = Record(BLANK_UUID, RECORD_FREE)
        self._buf.write_record(recnum, record)
        self._buf.dump_record(recnum, self._file)

    def format(self):
        """
        Format index, deleting all existing records.

        Raises:
        - OSError if I/O operation failed
        """
        # TODO: write metadata
        log.info("Formatting index for lockspace %r", self._lockspace)
        record = Record(BLANK_UUID, RECORD_FREE)
        for recnum in range(MAX_RECORDS):
            self._buf.write_record(recnum, record)
        self._buf.dump(self._file)

    def leases(self):
        """
        Return all leases in the index
        """
        log.debug("Getting all leases for lockspace %r", self._lockspace)
        leases = {}
        for recnum in range(MAX_RECORDS):
            # TODO: handle bad records - currently will raise InvalidRecord and
            # fail the request.
            record = self._buf.read_record(recnum)
            if record.state != RECORD_FREE:
                leases[record.resource] = {
                    "offset": self._lease_offset(recnum),
                    "state": RECORD_STATES[record.state],
                    "modified": record.modified,
                }
        return leases

    def close(self):
        log.debug("Closing index for lockspace %r", self._lockspace)
        self._buf.close()

    def _lease_offset(self, recnum):
        return LEASE_BASE + (recnum * LEASE_SIZE)


class IndexBuffer(object):

    def __init__(self, file):
        """
        Initialzie a buffer using file.
        """
        self._buf = mmap.mmap(-1, INDEX_SIZE, mmap.MAP_SHARED)
        try:
            file.seek(0)
            file.readinto(self._buf)
        except:
            self._buf.close()
            raise

    def find_record(self, lease_id):
        """
        Search for lease_id record. Returns record number if found, -1
        otherwise.
        """
        prefix = lease_id.encode("ascii") + RECORD_SEP
        offset = self._buf.find(prefix, RECORD_BASE)
        if offset == -1:
            return -1

        # TODO: check alignment
        return self._record_number(offset)

    def read_record(self, recnum):
        """
        Read record recnum, returns record info.
        """
        offset = self._record_offset(recnum)
        self._buf.seek(offset)
        data = self._buf.read(RECORD_SIZE)
        return Record.frombytes(data)

    def write_record(self, recnum, record):
        """
        Write record recnum with lease_id and modified time. The record is not
        written to storage; call dump_record to write it.
        """
        offset = self._record_offset(recnum)
        self._buf.seek(offset)
        self._buf.write(record.bytes())

    def dump_record(self, recnum, file):
        """
        Write the block where record is located to storage and wait until the
        data reach storage.
        """
        offset = self._record_offset(recnum)
        block_start = offset - (offset % BLOCK_SIZE)
        if PY2:
            block = buffer(self._buf, block_start, BLOCK_SIZE)
        else:
            block = memoryview(self._buf)[block_start:block_start + BLOCK_SIZE]
        file.seek(block_start)
        file.write(block)
        os.fsync(file.fileno())

    def dump(self, file):
        """
        Write the entire buffer to storage and wait until the data reach
        storage. This is not atomic operation; if the operation fail, some
        blocks may not be written.
        """
        file.seek(0)
        file.write(self._buf)
        os.fsync(file.fileno())

    def close(self):
        self._buf.close()

    def _record_offset(self, recnum):
        return RECORD_BASE + recnum * RECORD_SIZE

    def _record_number(self, offset):
        return (offset - RECORD_BASE) // RECORD_SIZE


class DirectFile(object):
    """
    File performing directio to/from mmap objects.
    """

    def __init__(self, path):
        self._path = path
        fd = os.open(path, os.O_RDWR | os.O_DIRECT)
        self._file = io.FileIO(fd, "r+", closefd=True)

    @property
    def name(self):
        return self._path

    def readinto(self, buf):
        pos = 0
        if PY2:
            # There is no way to create a writable memoryview on mmap object in
            # python 2, so we must read into a temporary buffer and copy into
            # the given buffer.
            rbuf = mmap.mmap(-1, len(buf), mmap.MAP_SHARED)
            with utils.closing(rbuf, log=log.name):
                while pos < len(buf):
                    nread = uninterruptible(self._file.readinto, rbuf)
                    buf.write(rbuf[:nread])
                    pos += nread
        else:
            # In python 3 we can read directly into the underlying buffer
            # without any copies using a memoryview.
            while pos < len(buf):
                rbuf = memoryview(buf)[pos:]
                pos += uninterruptible(self._file.readinto, rbuf)
        return pos

    def write(self, buf):
        pos = 0
        while pos < len(buf):
            if PY2:
                wbuf = buffer(buf, pos)
            else:
                wbuf = memoryview(buf)[pos:]
            pos += uninterruptible(self._file.write, wbuf)

    def fileno(self):
        return self._file.fileno()

    def seek(self, offset, whence=os.SEEK_SET):
        return self._file.seek(offset, whence)

    def close(self):
        self._file.close()