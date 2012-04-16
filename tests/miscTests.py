#
# Copyright 2012 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
import os
import tempfile
import uuid
import time
import threading
from testrunner import VdsmTestCase as TestCaseBase

import storage.misc as misc
import storage.fileUtils as fileUtils

EXT_DD = "/bin/dd"
EXT_CHMOD = "/bin/chmod"
EXT_CHOWN = "/bin/chown"

EXT_ECHO = "echo"
EXT_SLEEP = "sleep"
EXT_PYTHON = "python"
EXT_WHOAMI = "whoami"
SUDO_USER = "root"
SUDO_GROUP = "root"


def ddWatchCopy(srcPath, dstPath, callback, dataLen):
    try:
        rc, out, err = misc.ddWatchCopy(srcPath, dstPath, callback, dataLen)
    except TypeError:
        #For backwards compatibility
        rc, out, err = misc.ddWatchCopy(srcPath, dstPath, callback, None,
                dataLen)
    return rc, out, err


def watchCmd(cmd, stop, sudo=True, cwd=None, infile=None, outfile=None,
        data=None, recoveryCallback=None):
    try:
        ret, out, err = misc.watchCmd(cmd, stop, sudo=sudo, cwd=cwd,
                infile=infile, outfile=outfile, data=data,
                recoveryCallback=recoveryCallback)
    except TypeError:
        #For backwards compatibility
        ret, out, err = misc.watchCmd(cmd, stop, None, sudo=sudo, cwd=cwd,
                infile=infile, outfile=outfile, data=data,
                recoveryCallback=recoveryCallback)

    return ret, out, err


class PgrepTests(TestCaseBase):
    def test(self):
        sleepProcs = []
        for i in range(3):
            sleepProcs.append(misc.execCmd(["sleep", "3"], sync=False,
                sudo=False))

        time.sleep(1)

        pids = misc.pgrep("sleep")
        for proc in sleepProcs:
            self.assertTrue(proc.pid in pids, "pid %d was not located by pgrep"
                    % proc.pid)

        for proc in sleepProcs:
            proc.wait()


class GetCmdArgsTests(TestCaseBase):
    def test(self):
        args = ("sleep", "4")
        sproc = misc.execCmd(args, sync=False, sudo=False)
        time.sleep(1)
        self.assertEquals(misc.getCmdArgs(sproc.pid), args)
        sproc.wait()


class PidStatTests(TestCaseBase):
    def test(self):
        args = ["sleep", "3"]
        sproc = misc.execCmd(args, sync=False, sudo=False)
        time.sleep(1)
        stats = misc.pidStat(sproc.pid)
        pid = int(stats[0])
        # procName comes in the format of (procname)
        name = stats[1]
        self.assertEquals(pid, sproc.pid)
        self.assertEquals(name, args[0])
        sproc.wait()


class EventTests(TestCaseBase):
    def testEmit(self):
        ev = threading.Event()

        def callback():
            self.log.info("Callback called")
            ev.set()

        event = misc.Event("EndOfTheWorld")
        event.register(callback)
        event.emit()
        ev.wait(5)
        self.assertTrue(ev.isSet())

    def testEmitStale(self):
        ev = threading.Event()
        callback = lambda: ev.set()
        event = misc.Event("EndOfTheWorld")
        event.register(callback)
        del callback
        event.emit()
        ev.wait(5)
        self.assertFalse(ev.isSet())

    def testUnregister(self):
        ev = threading.Event()
        callback = lambda: ev.set()
        event = misc.Event("EndOfTheWorld")
        event.register(callback)
        event.unregister(callback)
        event.emit()
        ev.wait(5)
        self.assertFalse(ev.isSet())

    def testOneShot(self):
        ev = threading.Event()

        def callback():
            self.log.info("Callback called")
            ev.set()

        event = misc.Event("EndOfTheWorld")
        event.register(callback, oneshot=True)
        event.emit()
        ev.wait(5)
        self.assertTrue(ev.isSet())
        ev.clear()
        event.emit()
        ev.wait(5)
        self.assertFalse(ev.isSet())

    def testEmitCallbackException(self):
        ev = threading.Event()

        def callback1():
            raise Exception("AHHHHHHH!!!")

        def callback2():
            ev.set()

        event = misc.Event("EndOfTheWorld", sync=True)
        event.register(callback1)
        event.register(callback2)
        event.emit()
        ev.wait(5)
        self.assertTrue(ev.isSet())


class TMap(TestCaseBase):
    def test(self):
        def dummy(arg):
            # This will cause some of the operations to take longer
            # thus testing the result reordering mechanism
            if len(arg) % 2:
                time.sleep(1)
            return arg

        data = """Stephen Fry: Well next week I shall be examining the claims
                  of a man who says that in a previous existence he was
                  Education Secretary Kenneth Baker and I shall be talking to a
                  woman who claims she can make flowers grow just by planting
                  seeds in soil and watering them. Until then, wait very
                  quietly in your seats please. Goodnight."""
                   # (C) BBC - A Bit of Fry and Laury
        data = data.split()
        self.assertEquals(list(misc.tmap(dummy, data)), data)

    def testErrMethod(self):
        exceptionStr = ("It's time to kick ass and chew bubble gum... " +
                        "and I'm all outta gum.")

        def dummy(arg):
            raise Exception(exceptionStr)
        try:
            misc.tmap(dummy, [1, 2, 3, 4])
        except Exception, e:
            self.assertEquals(str(e), exceptionStr)
            return
        else:
            self.fail("tmap did not throw an exception")


class RotateFiles(TestCaseBase):
    def testNonExistingDir(self, persist=False):
        """
        Tests that the method fails correctly when given a non existing dir.
        """
        self.assertRaises(OSError, misc.rotateFiles, "/I/DONT/EXIST", "prefix",
                2, persist=persist)

    def testEmptyDir(self, persist=False):
        """
        Test that when given an empty dir the rotator works correctly.
        """
        prefix = "prefix"
        dir = tempfile.mkdtemp()

        misc.rotateFiles(dir, prefix, 0, persist=persist)

        os.rmdir(dir)

    def testFullDir(self, persist=False):
        """
        Test that rotator does it's basic functionality.
        """
        #Prepare
        prefix = "prefix"
        stubContent = ('"Multiple exclamation marks", ' +
                       'he went on, shaking his head, ' +
                       '"are a sure sign of a diseased mind."')
        # (C) Terry Pratchet - Small Gods
        dir = tempfile.mkdtemp()
        gen = 10

        expectedDirContent = []
        for i in range(gen):
            fname = "%s.txt.%d" % (prefix, i + 1)
            expectedDirContent.append("%s.txt.%d" % (prefix, i + 1))
            f = open(os.path.join(dir, fname), "wb")
            f.write(stubContent)
            f.flush()
            f.close()

        #Rotate
        misc.rotateFiles(dir, prefix, gen, persist=persist)

        #Test result
        currentDirContent = os.listdir(dir)
        expectedDirContent.sort()
        currentDirContent.sort()
        try:
            self.assertEquals(currentDirContent, expectedDirContent)
        finally:
            #Clean
            for f in os.listdir(dir):
                os.unlink(os.path.join(dir, f))
            os.rmdir(dir)


class ParseHumanReadableSize(TestCaseBase):
    def testValidInput(self):
        """
        Test that the method parses size correctly if given correct input.
        """
        for i in range(1, 1000):
            for schar, power in [("T", 40), ("G", 30), ("M", 20), ("K", 10)]:
                expected = misc.parseHumanReadableSize("%d%s" % (i, schar))
                self.assertEquals(expected, (2 ** power) * i)

    def testInvalidInput(self):
        """
        Test that parsing handles invalid input correctly
        """
        self.assertEquals(misc.parseHumanReadableSize("T"), 0)
        self.assertEquals(misc.parseHumanReadableSize("TNT"), 0)
        self.assertRaises(AttributeError, misc.parseHumanReadableSize, 5)
        self.assertEquals(misc.parseHumanReadableSize("4.3T"), 0)


class AsyncProcTests(TestCaseBase):
    def test(self):
        data = """Striker: You are a Time Lord, a lord of time.
                           Are there lords in such a small domain?
                  The Doctor: And where do you function?
                  Striker: Eternity. The endless wastes of eternity. """
                  # (C) BBC - Doctor Who
        p = misc.execCmd(["cat"], sync=False)
        self.log.info("Writing data to std out")
        p.stdin.write(data)
        p.stdin.flush()
        self.log.info("Written data reading")
        self.assertEquals(p.stdout.read(len(data)), data)

    def testMutiWrite(self):
        data = """The Doctor: Androzani Major was becoming quite developed
                              last time I passed this way.
                  Peri: When was that?
                  The Doctor: ...I don't remember.
                              I'm pretty sure it wasn't the future. """
                  # (C) BBC - Doctor Who
        halfPoint = len(data) / 2
        p = misc.execCmd(["cat"], sync=False)
        self.log.info("Writing data to std out")
        p.stdin.write(data[:halfPoint])
        self.log.info("Writing more data to std out")
        p.stdin.write(data[halfPoint:])
        p.stdin.flush()
        self.log.info("Written data reading")
        self.assertEquals(p.stdout.read(len(data)), data)

    def writeLargeData(self):
        data = """The Doctor: Davros, if you had created a virus in your
                              laboratory, something contagious and infectious
                              that killed on contact, a virus that would
                              destroy all other forms of life; would you allow
                              its use?
                  Davros: It is an interesting conjecture.
                  The Doctor: Would you do it?
                  Davros: The only living thing... The microscopic organism...
                          reigning supreme... A fascinating idea.
                  The Doctor: But would you do it?
                  Davros: Yes; yes. To hold in my hand, a capsule that
                          contained such power. To know that life and death on
                          such a scale was my choice. To know that the tiny
                          pressure on my thumb, enough to break the glass,
                          would end everything. Yes! I would do it! That power
                          would set me up above the gods! And through the
                          Daleks, I shall have that power! """
                  # (C) BBC - Doctor Who

        data = data * ((4096 / len(data)) * 2)
        self.assertTrue(data > 4096)
        p = misc.execCmd(["cat"], sync=False)
        self.log.info("Writing data to std out")
        p.stdin.write(data)
        p.stdin.flush()
        self.log.info("Written data reading")
        self.assertEquals(p.stdout.read(len(data)), data)

    def testWaitTimeout(self):
        ttl = 2
        p = misc.execCmd(["sleep", str(ttl + 10)], sudo=False, sync=False)
        startTime = time.time()
        p.wait(ttl)
        duration = time.time() - startTime
        self.assertTrue(duration < (ttl + 1))
        self.assertTrue(duration > (ttl))
        p.kill()

    def testWaitCond(self):
        ttl = 2
        p = misc.execCmd(["sleep", str(ttl + 10)], sudo=False, sync=False)
        startTime = time.time()
        p.wait(cond=lambda: time.time() - startTime > ttl)
        duration = time.time() - startTime
        self.assertTrue(duration < (ttl + 2))
        self.assertTrue(duration > (ttl))
        p.kill()


class DdWatchCopy(TestCaseBase):
    def testNonAlignedCopy(self, sudo=False):
        """
        Test that copying a file with odd length works.
        """

        data = '- "What\'re quantum mechanics?"' + \
               '- "I don\'t know. People who repair quantums, I suppose."'
               # (C) Terry Pratchet - Small Gods

        # Make sure the length is appropriate
        if (len(data) % 512) == 0:
            data += "!"

        srcFd, srcPath = tempfile.mkstemp()
        f = os.fdopen(srcFd, "wb")
        f.write(data)
        f.flush()
        f.close()
        os.chmod(srcPath, 0666)

        #Get a tempfilename
        dstFd, dstPath = tempfile.mkstemp()
        os.chmod(dstPath, 0666)

        #Copy
        rc, out, err = ddWatchCopy(srcPath, dstPath, None, len(data))

        #Get copied data
        readData = open(dstPath).read()

        #clean
        os.unlink(dstPath)
        os.unlink(srcPath)

        # Compare
        self.assertEquals(readData, data)

    def testCopy(self):
        """
        Test that regular copying works.
        """
        #Prepare source
        data = "Everything starts somewhere, " + \
               "though many physicists disagree." + \
               "But people have always been dimly aware of the " + \
               "problem with the start of things." + \
               "They wonder how the snowplough driver gets to work, or " + \
               "how the makers of dictionaries look up the spelling of words."
               # (C) Terry Pratchet - Small Gods
        # Makes sure we round up to a complete block size
        data *= 512

        srcFd, srcPath = tempfile.mkstemp()
        f = os.fdopen(srcFd, "wb")
        f.write(data)
        f.flush()
        f.close()
        os.chmod(srcPath, 0666)

        #Get a tempfilename
        dstFd, dstPath = tempfile.mkstemp()
        os.chmod(dstPath, 0666)

        #Copy
        rc, out, err = ddWatchCopy(srcPath, dstPath, None, len(data))

        #Get copied data
        readData = open(dstPath).read()

        #clean
        os.unlink(dstPath)
        os.unlink(srcPath)

        #Comapre
        self.assertEquals(readData, data)

    def testNonExistingFile(self):
        """
        Test that trying to copy a non existing file raises the right
        exception.
        """
        #Get a tempfilename
        srcFd, srcPath = tempfile.mkstemp()
        os.unlink(srcPath)

        #Copy
        self.assertRaises(misc.se.MiscBlockWriteException, ddWatchCopy,
                srcPath, "/tmp/tmp", None, 100)

    def testStop(self):
        """
        Test that stop really stops the copying process.
        """
        try:
            ddWatchCopy("/dev/zero", "/dev/null", lambda: True, 100)
        except misc.se.ActionStopped:
            self.log.info("Looks like stopped!")
        else:
            self.fail("Copying didn't stopped!")


class ValidateN(TestCaseBase):
    def testValidInput(self):
        """
        Test cases that the validator should validate.
        """
        try:
            value = 1
            misc.validateN(value, "a")
            value = "1"
            misc.validateN(value, "a")
            value = 1.0
            misc.validateN(value, "a")
            value = "471902437190237189236189"
            misc.validateN(value, "a")
        except misc.se.InvalidParameterException:
            self.fail("Failed while validating a valid value '%s'" % value)

    def testInvalidInput(self):
        """
        Test that the validator doesn't validate illegal input.
        """
        expectedException = misc.se.InvalidParameterException
        self.assertRaises(expectedException, misc.validateN, "A", "a")
        self.assertRaises(expectedException, misc.validateN, "-1", "a")
        self.assertRaises(expectedException, misc.validateN, -1, "a")
        self.assertRaises(expectedException, misc.validateN, "4.3", "a")
        self.assertRaises(expectedException, misc.validateN, "", "a")
        self.assertRaises(expectedException, misc.validateN, "*", "a")
        self.assertRaises(expectedException, misc.validateN, "2-1", "a")


class ValidateInt(TestCaseBase):
    def testValidInput(self):
        """
        Test cases that the validator should validate.
        """
        try:
            value = 1
            misc.validateInt(value, "a")
            value = -1
            misc.validateInt(value, "a")
            value = "1"
            misc.validateInt(value, "a")
            value = 1.0
            misc.validateInt(value, "a")
            value = "471902437190237189236189"
            misc.validateInt(value, "a")
        except misc.se.InvalidParameterException:
            self.fail("Failed while validating a valid value '%s'" % value)

    def testInvalidInput(self):
        """
        Test that the validator doesn't validate illegal input.
        """
        expectedException = misc.se.InvalidParameterException
        self.assertRaises(expectedException, misc.validateInt, "A", "a")
        self.assertRaises(expectedException, misc.validateInt, "4.3", "a")
        self.assertRaises(expectedException, misc.validateInt, "", "a")
        self.assertRaises(expectedException, misc.validateInt, "*", "a")
        self.assertRaises(expectedException, misc.validateInt, "2-1", "a")


class ValidateUuid(TestCaseBase):
    def testValidInput(self):
        """
        Test if the function succeeds in validating valid UUIDs.
        """
        for i in range(1000):
            tmpUuid = str(uuid.uuid4())
            try:
                misc.validateUUID(tmpUuid)
            except misc.se.InvalidParameterException:
                self.fail("Could not parse VALID UUID '%s'" % tmpUuid)

    def testInvalidInputNotHex(self):
        """
        Test that validator detects when a non HEX char is in the input.
        """
        self.assertRaises(misc.se.InvalidParameterException, misc.validateUUID,
                "Dc08ff668-4072-4191-9fbb-f1c8f2daz333")

    def testWrongLength(self):
        """
        Test that the validator detects when the input is not in the correct
        length
        """
        self.assertRaises(misc.se.InvalidParameterException, misc.validateUUID,
                "Dc08ff668-4072-4191-9fbb-f1c8f2daa33")
        self.assertRaises(misc.se.InvalidParameterException, misc.validateUUID,
                "Dc08ff668-4072-4191-9fb-f1c8f2daa333")
        self.assertRaises(misc.se.InvalidParameterException, misc.validateUUID,
                "Dc08ff68-4072-4191-9fbb-f1c8f2daa333")
        self.assertRaises(misc.se.InvalidParameterException, misc.validateUUID,
                "Dc08ff668-4072-4191-9fbb-f1c8f2daa3313")


class UuidPack(TestCaseBase):
    def test(self):
        """
        Test that the uuid that was packed can be unpacked without being
        changed
        """
        for i in range(1000):
            origUuid = str(uuid.uuid4())
            packedUuid = misc.packUuid(origUuid)
            self.assertEquals(misc.unpackUuid(packedUuid), origUuid)


class Checksum(TestCaseBase):
    def testConsistency(self):
        """
        Test if when given the same input in different times the user will get
        the same checksum.
        """
        data = open("/dev/urandom", "rb").read(50)
        self.assertEquals(misc.checksum(data, 16), misc.checksum(data, 16))


class ParseBool(TestCaseBase):
    def testValidInput(self):
        """
        Compare valid inputs with expected results.
        """
        self.assertEquals(misc.parseBool(True), True)
        self.assertEquals(misc.parseBool(False), False)
        self.assertEquals(misc.parseBool("true"), True)
        self.assertEquals(misc.parseBool("tRue"), True)
        self.assertEquals(misc.parseBool("false"), False)
        self.assertEquals(misc.parseBool("fAlse"), False)
        self.assertEquals(misc.parseBool("BOB"), False)

    def testInvalidInput(self):
        """
        See that the method is consistent when giver invalid input.
        """
        self.assertRaises(AttributeError, misc.parseBool, 1)
        self.assertRaises(AttributeError, misc.parseBool, None)


class AlignData(TestCaseBase):
    def test(self):
        """
        Test various inputs and see that they are correct.
        """
        self.assertEquals(misc._alignData(100, 100), (4, 25, 25))
        self.assertEquals(misc._alignData(512, 512), (512, 1, 1))
        self.assertEquals(misc._alignData(1, 1024), (1, 1, 1024))
        self.assertEquals(misc._alignData(10240, 512), (512, 20, 1))
        self.assertEquals(misc._alignData(1, 1), (1, 1, 1))


class ValidateDDBytes(TestCaseBase):
    def testValidInputTrue(self):
        """
        Test that it works when given valid and correct input.
        """
        count = 802
        cmd = [EXT_DD, "bs=1", "if=/dev/urandom", 'of=/dev/null',
                'count=%d' % count]
        rc, out, err = misc.execCmd(cmd, sudo=False)

        self.assertTrue(misc.validateDDBytes(err, count))

    def testValidInputFalse(self):
        """
        Test that is work when given valid but incorrect input.
        """
        count = 802
        cmd = [EXT_DD, "bs=1", "if=/dev/urandom", 'of=/dev/null',
                'count=%d' % count]
        rc, out, err = misc.execCmd(cmd, sudo=False)

        self.assertFalse(misc.validateDDBytes(err, count + 1))

    def testInvalidInput(self):
        """
        Test that the method handles wired input.
        """
        self.assertRaises(misc.se.InvalidParameterException,
                misc.validateDDBytes, ["I AM", "PRETENDING TO", "BE DD"], "BE")
        self.assertRaises(misc.se.InvalidParameterException,
                misc.validateDDBytes, ["I AM", "PRETENDING TO", "BE DD"], 32)


class ReadBlockSudo(TestCaseBase):
    def _createTempFile(self, neededFileSize, writeData):
        """
        Create a temp file with the data in *writeData* written continuously in
        it.

        :returns: the path of the new temp file.
        """
        dataLength = len(writeData)

        fd, path = tempfile.mkstemp()
        f = os.fdopen(fd, "wb")

        written = 0
        while written < neededFileSize:
            f.write(writeData)
            written += dataLength
        f.close()
        return path

    def testValidInputSUDO(self):
        """
        The same as testValidInput but with SUDO
        """
        self.testValidInput(True)

    def testValidInput(self, sudo=False):
        """
        Test that when all arguments are correct the method works smoothly.
        """
        writeData = "DON'T THINK OF IT AS DYING, said Death." + \
                    "JUST THINK OF IT AS LEAVING EARLY TO AVOID THE RUSH."
        # (C) Terry Pratchet - Good Omens
        dataLength = len(writeData)

        offset = 512
        size = 512

        path = self._createTempFile(offset + size, writeData)

        #Figure out what outcome should be
        timesInSize = int(size / dataLength) + 1
        relOffset = offset % dataLength
        expectedResultData = (writeData * timesInSize)
        expectedResultData = \
            (expectedResultData[relOffset:] + expectedResultData[:relOffset])
        expectedResultData = expectedResultData[:size]
        block = misc.readblockSUDO(path, offset, size, sudo=sudo)

        os.unlink(path)

        self.assertEquals(block[0], expectedResultData)

    def testInvalidOffsetSUDO(self):
        """
        The same as testInvalidOffset but with SUDO.
        """
        self.testInvalidOffset(True)

    def testInvalidOffset(self, sudo=False):
        """
        Make sure that we check for invalid (non 512 aligned) offset.
        """
        offset = 513
        self.assertRaises(misc.se.MiscBlockReadException, misc.readblockSUDO,
                "/dev/urandom", offset, 512, sudo)

    def testInvalidSizeSUDO(self):
        """
        The same as testInvalidSize but with SUDO.
        """
        self.testInvalidSize(True)

    def testInvalidSize(self, sudo=False):
        """
        Make sure that we check for invalid (non 512 aligned) size.
        """
        size = 513
        self.assertRaises(misc.se.MiscBlockReadException, misc.readblockSUDO,
                "/dev/urandom", 512, size, sudo)

    def testReadingMoreTheFileSizeSUDO(self):
        """
        The same as testReadingMoreTheFileSize but with SUDO.
        """
        self.testReadingMoreTheFileSize(True)

    def testReadingMoreTheFileSize(self, sudo=False):
        """
        See that correct exception is raised when trying to read more then the
        file has to offer.
        """
        writeData = "History, contrary to popular theories, " + \
                    "is kings and dates and battles."
        # (C) Terry Pratchet - Small Gods

        offset = 512
        size = 512

        path = self._createTempFile(offset + size - 100, writeData)

        self.assertRaises(misc.se.MiscBlockReadIncomplete, misc.readblockSUDO,
                path, offset, size, sudo)

        os.unlink(path)


class CleanUpDir(TestCaseBase):
    def testFullDir(self):
        """
        Test if method can clean a dir it should be able to.
        """
        #Populate dir
        baseDir = tempfile.mkdtemp()
        numOfFilesToCreate = 50
        for i in range(numOfFilesToCreate):
            tempfile.mkstemp(dir=baseDir)

        #clean it
        fileUtils.cleanupdir(baseDir)

        self.assertFalse(os.path.lexists(baseDir))

    def testEmptyDir(self):
        """
        Test if method can delete an empty dir.
        """
        baseDir = tempfile.mkdtemp()

        fileUtils.cleanupdir(baseDir)

        self.assertFalse(os.path.lexists(baseDir))

    def testNotExistingDir(self):
        """
        See that method doesn't throw a fit if given a non existing dir
        """
        fileUtils.cleanupdir(os.path.join("I", " DONT", "EXIST"))

    def testDirWithUndeletableFiles(self):
        """
        See that the method handles correctly a situation where it is given a
        dir it can't clean.
        """
        baseDir = "/proc/misc"  # This can't be deleted

        #Try and fail to clean it
        fileUtils.cleanupdir(baseDir, ignoreErrors=True)
        self.assertTrue(os.path.lexists(baseDir))

        self.assertRaises(misc.se.MiscDirCleanupFailure, fileUtils.cleanupdir,
                baseDir, False)
        self.assertTrue(os.path.lexists(baseDir))


class ReadFile(TestCaseBase):
    def testValidInput(self):
        """
        Test if method works when given a valid file.
        """
        #create
        writeData = ("Trust me, I know what self-loathing is," +
                     "but to kill myself? That would put a damper on my " +
                     "search for answers. Not at all productive.")
        # (C) Jhonen Vasquez - Johnny the Homicidal Maniac
        fd, path = tempfile.mkstemp()
        f = os.fdopen(fd, "wb")
        f.write(writeData)
        f.close()

        #read
        readData = misc.readfile(path)

        #clean
        os.unlink(path)

        self.assertEquals(writeData, readData[0])

    def testInvalidInput(self):
        """
        Test if method works when input is a non existing file.
        """
        fd, path = tempfile.mkstemp()
        os.unlink(path)

        self.assertRaises(misc.se.MiscFileReadException,  misc.readfile, path)


class PidExists(TestCaseBase):
    def testPidExists(self):
        """
        Test if pid given exists.
        """
        mypid = os.getpid()

        self.assertTrue(misc.pidExists(mypid))

    def testPidDoesNotExist(self):
        """
        Test if when given incorrect input the method works correctly.
        """
        #FIXME : There is no way real way know what process aren't working.
        #I'll just try and see if there is **any** occasion where it works
        #If anyone has any idea. You are welcome to change this

        pid = os.getpid()
        result = True
        while result:
            pid += 1
            result = misc.pidExists(pid)


class WatchCmd(TestCaseBase):

    def testExec(self):
        """
        Tests that watchCmd execs and returns the correct ret code
        """
        data = """
        Interrogator: You're a spy!
        The Doctor: Am I? Who am I spying for?
        Interrogator: I'm asking the questions. I repeat, you're a spy!
        The Doctor: That wasn't a question. That was a statement.
        Interrogator: Careful, our friends here don't get much fun.
                      [Gestures to the thuggish Ogron security guards.]
        The Doctor: Poor fellows. Sorry I can't oblige them at the moment,
                    I'm not in the mood for games. """
        # (C) BBC - Doctor Who
        data = data.strip()
        ret, out, err = watchCmd([EXT_ECHO, "-n", data], lambda: False,
                sudo=False)

        self.assertEquals(ret, 0)
        self.assertEquals(out, data.splitlines())

    def testStop(self, sudo=False):
        """
        Test that stopping the process really works.
        """
        sleepTime = "10"
        try:
            watchCmd([EXT_SLEEP, sleepTime], lambda: True, sudo=sudo)
        except misc.se.ActionStopped:
            self.log.info("Looks like task stopped!")
        else:
            self.fail("watchCmd didn't stop!")

    def testStopSudo(self):
        self.testStop(True)

    def testStdOut(self):
        """
        Tests that watchCmd correctly returns the standard output of the prog
        it executes.
        """
        line = "Real stupidity beats artificial intelligence every time."
        # (C) Terry Pratchet - Hogfather
        ret, stdout, stderr = watchCmd([EXT_ECHO, line], lambda: False,
                sudo=False)
        self.assertEquals(stdout[0], line)

    def testStdErr(self):
        """
        Tests that watchCmd correctly returns the standard error of the prog it
        executes.
        """
        line = "He says gods like to see an atheist around. " + \
               "Gives them something to aim at."
        # (C) Terry Pratchet - Small Gods
        code = "import sys; sys.stderr.write('%s')" % line
        ret, stdout, stderr = watchCmd([EXT_PYTHON, "-c", code],
                lambda: False, sudo=False)
        self.assertEquals(stderr[0], line)

    def testSudo(self):
        """
        Tests that when running with sudo the user really is root (or other
        desired user).
        """
        ret, stdout, stderr = watchCmd([EXT_WHOAMI], lambda: False, sudo=True)
        self.assertEquals(stdout[0], SUDO_USER)

    def testLeakFd(self):
        """
        Make sure that nothing leaks
        """
        openFdNum = lambda: len(misc.getfds())
        openFds = openFdNum()
        self.testStdOut()
        import gc
        gc.collect()
        self.assertEquals(len(gc.garbage), 0)
        self.assertEquals(openFdNum(), openFds)


class ExecCmd(TestCaseBase):
    def testExec(self):
        """
        Tests that execCmd execs and returns the correct ret code
        """
        ret, out, err = misc.execCmd([EXT_ECHO], sudo=False)

        self.assertEquals(ret, 0)

    def testStdOut(self):
        """
        Tests that execCmd correctly returns the standard output of the prog it
        executes.
        """
        line = "All I wanted was to have some pizza, hang out with dad, " + \
               "and not let your weirdness mess up my day"
        # (C) Nickolodeon - Invader Zim
        ret, stdout, stderr = misc.execCmd((EXT_ECHO, line), sudo=False)
        self.assertEquals(stdout[0], line)

    def testStdErr(self):
        """
        Tests that execCmd correctly returns the standard error of the prog it
        executes.
        """
        line = "Hey Scully, is this display of boyish agility " + \
               "turning you on at all?"
        # (C) Fox - The X Files
        code = "import sys; sys.stderr.write('%s')" % line
        ret, stdout, stderr = misc.execCmd([EXT_PYTHON, "-c", code],
                sudo=False)
        self.assertEquals(stderr[0], line)

    def testSudo(self):
        """
        Tests that when running with sudo the user really is root (or other
        desired user).
        """
        ret, stdout, stderr = misc.execCmd([EXT_WHOAMI], sudo=True)
        self.assertEquals(stdout[0], SUDO_USER)


class DeferableContextTests(TestCaseBase):
    def setUp(self):
        self._called = 0

    def _callDef(self):
        self._called += 1
        self.log.info("Incremented call count (%d)", self._called)

    def _raiseDef(self, ex=Exception()):
        self.log.info("Raised exception (%s)", ex.__class__.__name__)
        raise ex

    def test(self):
        with misc.DeferableContext() as dc:
            dc.defer(self._callDef)

        self.assertEquals(self._called, 1)

    def testRaise(self):
        """
        Test that raising an exception in a defered action does
        not block all subsequent actions from running
        """
        try:
            with misc.DeferableContext() as dc:
                dc.defer(self._callDef)
                dc.defer(self._raiseDef)
                dc.defer(self._callDef)
        except:
            self.assertEquals(self._called, 2)
            return

        self.fail("Exception was not raised")

    def testLastException(self):
        """
        Test that if multiple actions raise an exception only the last one is
        raised.
        This is done to preserve behaviour of:
        try:
            try:
                pass
            finally:
                raise Exception()
        finally:
            # This will swallow previous exception
            raise Exception()
        """
        try:
            with misc.DeferableContext() as dc:
                dc.defer(self._callDef)
                dc.defer(self._raiseDef)
                dc.defer(self._callDef)
                dc.defer(self._raiseDef, RuntimeError())
                dc.defer(self._callDef)
        except RuntimeError:
            self.assertEquals(self._called, 3)
            return
        except Exception:
            self.fail("Wrong exception was raised")

        self.fail("Exception was not raised")
