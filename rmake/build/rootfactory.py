#
# Copyright (c) 2006 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/cpl.php.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#

import copy
import errno
import os
import os.path
import pwd
import shutil
import signal
import sys
import time

#conary
from conary import conarycfg
from conary import conaryclient
from conary import callbacks
from conary.deps import deps
from conary.lib import util, log
from conary.repository import changeset

#rmake
from rmake import constants
from rmake import errors
from rmake.build import changesetcache
from rmake.build.chroot import server as chrootserver
from rmake.db import database
from rmake.lib import flavorutil

class ChrootCallback(callbacks.UpdateCallback):
    def __init__(self, buildTrove):
        callbacks.UpdateCallback.__init__(self)
        self.hunk = (0,0)
        self.buildTrove = buildTrove

    def _message(self, text):
        self.buildTrove.log(text)
        log.info("chroot: %s" % text)

    def setChangesetHunk(self, num, total):
        self.showedHunk = False
        self.hunk = (num, total)

    def setUpdateHunk(self, num, total):
        self.hunk = (num, total)

    def setUpdateJob(self, jobs):
        descriptions = []
        jobs.sort()
        for job in jobs:
            n,v,f = job[0], job[2][0], job[2][1]
            v = '%s/%s' % (v.trailingLabel(), v.trailingRevision())
            archDeps = [x.name for x in f.iterDepsByClass(deps.InstructionSetDependency)]
            if archDeps:
                f = '[is: %s]' % ' '.join(archDeps)
            else:
                f = ''
            descriptions.append('%s=%s%s' % (n,v,f))
        if self.hunk[1] > 1:
            self._message("installing %d of %d:\n    %s" % \
                            (self.hunk[0], self.hunk[1],
                             '\n    '.join(descriptions)))
        else:
            self._message("installing: \n    %s" % \
                          ('\n    '.join(descriptions),))

    def downloadingChangeSet(self, got, need):
        if not self.showedHunk:
            # we display our message here because here we have the size...
            # but we only want to display the message once per changeset
            self._message("Caching changeset %s of %s (%sKb)" % (
                                            self.hunk + (need/1024 or 1,)))
            self.showedHunk = True


class AbstractChroot:
    """
        The root manages a root environment, creating and installing
        the necessary files for the root to be usuable, and cleaning up
        after itself as much as possible.
    """

    def __init__(self, root, clean=False):
        self.root = root

        self.mounts = {}
        self.filesToCopy = []
        self.dirsToCopy = []
        self.dirsToAdd = []
        self.usersToSupport = []
        self.groupsToSupport = []
        self.devNodes = []

    def addMount(self, fromDir, toDir, type):
        #log.debug("adding mount point: %s -> %s (%s)", fromDir, toDir, type)
        # NOTE: we don't actually manage mount points any more.
        # This funtion is left in place to help document what the chroothelper
        # does.
        self.addDir(toDir, mode=0755)

    def addDir(self, directory, mode=0755, uid=0, gid=0):
        self.dirsToAdd.append((directory, mode, uid, gid))

    def copyDir(self, sourceDir, targetDir=None):
        if targetDir is None:
            targetDir = sourceDir
        self.dirsToCopy.append((sourceDir, targetDir))

    def copyFile(self, sourceFile, targetFile=None):
        if targetFile is None:
            targetFile = sourceFile
        self.filesToCopy.append((sourceFile, targetFile))

    def addUser(self, name, uid, gid=None, home=None, shell='/bin/bash'):
        if gid is None:
            gid = uid
        if gid == uid:
            self.groupsToSupport.append((name, gid, []))
        if home is None:
            home = '/home/%s' % name
        log.debug("adding user %s (%s,%s) home=%s", name, uid, gid, home)
        self.usersToSupport.append((name, uid, gid, home, shell))

    def addDeviceNode(self, path):
        self.devNodes.append(path)

    def create(self):
        self.clean()
        self._install()
        for m in self.mounts.values():
            m.mount()
        self._createDirs()
        self._copyFiles()
        self._copyDirs()
        self._supportGroups()
        self._supportUsers()
        self._addDeviceNodes()
        self._postInstall()

    def _createDirs(self):
        for dir, mode, uid, gid in self.dirsToAdd:
            dir = self.root + dir
            log.debug("creating chroot:%s", dir)
            util.mkdirChain(dir)
            if mode:
                os.chmod(dir, mode)
            if (uid or gid) and not os.getuid():
                os.chown(dir, uid, gid)

    def canChroot(self):
        return (pwd.getpwnam(constants.rmakeuser).pw_uid == os.getuid())

    def _copyFiles(self):
        for (sourceFile, targetFile) in self.filesToCopy:
            log.debug("copying file %s into chroot:%s", sourceFile, targetFile)
            shutil.copy(sourceFile, self.root + targetFile)

    def _copyDirs(self):
        for (sourceDir, targetDir) in self.dirsToCopy:
            targetDir = self.root + targetDir
            try:
                util.rmtree(targetDir)
            except:
                pass

            util.mkdirChain(os.path.dirname(targetDir))
            log.debug("copying dir %s into chroot:%s", sourceDir, targetDir)
            shutil.copytree(sourceDir, targetDir)

    def _supportGroups(self):
        if not self.groupsToSupport:
            return

        groupFile = self.root + '/etc/group'
        assert(os.path.exists(groupFile))
        names = []
        newGroupLines = []

        for (name, gid, users) in self.groupsToSupport:
            newLine = ':'.join((name, 'x', str(gid), ','.join(users) + '\n'))
            names.append(name)
            newGroupLines.append(newLine)

        groupLines = [ x for x in open(groupFile).readlines() if x.split(':', 1) not in names ] 
        groupLines.extend(newGroupLines)
        open(groupFile, 'w').write(''.join(groupLines))

    def _supportUsers(self):
        if not self.usersToSupport:
            return

        passwdFile = self.root + '/etc/passwd'
        assert(os.path.exists(passwdFile))

        names = []
        newPasswdLines = []
        newShadowLines = []

        for (name, uid, gid, home, shell) in self.usersToSupport:
            newLine = ':'.join((name, 'x', str(uid), str(gid), '', home, shell + '\n'))
            newShadow = ':'.join((name, '*', str(uid), '0', '99999', '7', '', '', '\n'))
            newShadowLines.append(':'.join(newShadow))
            newPasswdLines.append(newLine)

        passwdLines = [ x for x in open(passwdFile).readlines() if x.split(':', 1) not in names ] 
        passwdLines.extend(x + '\n' for x in newPasswdLines)
        open(passwdFile, 'w').write(''.join(passwdLines))

        shadowFile = self.root + '/etc/shadow'
        if os.path.exists(shadowFile) and not os.getuid():
            shadowLines = [ x for x in open(shadowFile).readlines() if x.split(':', 1) not in names ] 
            shadowLines.extend(newShadowLines)
            open(shadowFile, 'w').write(''.join(shadowLines))

    def _addDeviceNodes(self):
        if os.getuid(): # can only make device nodes as root
            util.mkdirChain('%s/dev' % self.root)
            return

        for devNode in self.devNodes:
            os.system("/sbin/MAKEDEV -d %s/dev/ -D /dev -x %s" % (self.root, devNode))

    def _install(self):
        raise NotImplementedError

    def clean(self):
        self.unmount()
        log.debug("removing old chroot tree: %s", self.root)
        os.system('rm -rf %s' % self.root)
        if os.path.exists(self.root):
            raise errors.OpenError(
            'Cannot create chroot - old root at %s could not be removed.'
            '  This may happen due to permissions problems such as root'
            ' owned files, or earlier build processes that have not'
            ' completely died.  Please remove the old root by hand.' \
            % self.root)


    def unmount(self):
        for m in self.mounts.values():
            log.debug("unmounting %s", m.toDir)
            m.unmount()

class BaseChroot(AbstractChroot):
    def __init__(self, root):
        AbstractChroot.__init__(self, root)
        self.addDir('/tmp', mode=01777)
        self.addDir('/var/tmp', mode=01777)
        self.addMount('/proc', '/proc', type='proc')
        self.addMount('/dev/pts', '/dev/pts', type='devpts')
        self.addDeviceNode('urandom') # needed for ssl and signing
        self.addDeviceNode('ptmx') # needed for pty use

class ConaryBasedRoot(BaseChroot):
    """ The root manages a root environment, creating and installing
        the necessary files for the root to be usuable, and cleaning up
        after itself as much as possible.
    """

    def __init__(self, buildTrove, root, chrootHelperPath, cfg, serverCfg,
                 jobList, uid=None, gid=None, csCache=None, targetArch=None):
        """ root: the path of the chroot
            uid/gid:  the uid/gid which special files in the chroot should be 
                      owned by
        """
        BaseChroot.__init__(self, root)

        assert(root and root[0] == '/' and root != "/")
        self.cfg = cfg
        self.jobId = buildTrove.jobId
        self.jobList = jobList
        self.buildTrove = buildTrove
        self.chrootHelperPath = chrootHelperPath
        self.targetArch = targetArch
        self.serverCfg = serverCfg

        buildTrove.log('Creating chroot')

        self.addDir('/etc')
        self.addDir('/etc/rmake')
        self.addDir('/etc/conary')
        self.copyFile('/etc/hosts')
        self.copyFile('/etc/resolv.conf')

        # make time outputs accurate
        if os.path.exists('/etc/localtime'):
            self.copyFile('/etc/localtime')
        if os.path.exists('/etc/nsswitch.conf'):
            self.copyFile('/etc/nsswitch.conf')

        # ********
        # NOTE:
        # We copy in local system files, including policy and use dirs,
        # in order to make the use of rmake as easy as possible.  If rMake
        # ever gets to the point where its use is distributed, we should 
        # no longer copy anything but required networking/system info 
        # from the host system, and instead generate or pass in this
        # information from the host system
        self.copyFile('/etc/passwd')
        self.copyFile('/etc/group')

        self._installRmake()

        self.copyFile('/etc/conary/macros')

        for policyDir in cfg.policyDirs:
            if os.path.exists(policyDir):
                self.copyDir(policyDir)

        for useDir in cfg.useDirs:
            if os.path.exists(useDir):
                self.copyDir(useDir)

        self.addDir(cfg.tmpDir, mode=01777)
        self.csCache = csCache

    def unmount(self):
        if not os.path.exists(self.cfg.root):
            return
        if self.canChroot():
            log.info('Running chroot helper to unmount...')
            rc = os.system('%s %s --clean' % (self.chrootHelperPath, 
                            self.cfg.root))
            if rc:
                raise errors.OpenError(
                                'Cannot create chroot - chroot helper failed to clean old chroot')

    def _installRmake(self):
        conaryDir = os.path.dirname(sys.modules['conary'].__file__)
        if not self.targetArch:
            self.copyDir(conaryDir)
            self.copyDir(conaryDir, '/usr/lib/python2.4/site-packages/conary')


        rmakeDir = os.path.dirname(sys.modules['rmake'].__file__)

        self.copyDir(rmakeDir)
        # just copy to a standard path
        self.copyDir(rmakeDir, '/usr/share/rmake/rmake')
        self.copyDir(rmakeDir, '/usr/lib/python2.4/site-packages/rmake')


    def _install(self):
        if not self.jobList:
            # should only be true in debugging situations
            return
        self.buildTrove.log('Creating Chroot')
        assert(self.cfg.root == self.root)
        client = conaryclient.ConaryClient(self.cfg)
        callback = ChrootCallback(self.buildTrove)

        if self.csCache:
            changeSetList = self.csCache.getChangeSets(client.getRepos(),
                                                       self.jobList, callback)
        else:
            changeSetList = []

        # log every trove that is going to be installed to stdout
        log.info('Troves To Install for %s=%s[%s]:' % self.buildTrove.getNameVersionFlavor())
        log.info('\n    '.join('%s=%s[%s]' % (x[0], x[2][0], x[2][1])
                               for x in sorted(self.jobList)))

        updJob, suggMap = client.updateChangeSet(
            self.jobList, keepExisting=False, resolveDeps=False,
            recurse=False, checkPathConflicts=False,
            callback = callback, fromChangesets=changeSetList,
            )
        util.mkdirChain(self.cfg.root + '/root')
        client.applyUpdate(updJob, replaceFiles=True, callback = callback,
                           tagScript=self.cfg.root + '/root/tagscripts')

    def _postInstall(self):
        self.createConaryRc()

    def createConaryRc(self):
        conaryrc = open('%s/etc/conaryrc' % self.root, 'w')
        conaryCfg = conarycfg.ConaryConfiguration(False)
        for key, value in self.cfg.iteritems():
            if key in conaryCfg:
                conaryCfg[key] = value
        try:
            if self.canChroot(): # then we will be chrooting into this dir
                oldroot = self.cfg.root
                conaryCfg.root = '/'
                conaryCfg.store(conaryrc, includeDocs=False)
                conaryCfg.root = oldroot
            else:
                conaryCfg.store(conaryrc, includeDocs=False)
        except Exception, msg:
            print "Error writing conaryrc:", msg
        conaryrc.close()

    def start(self):
        socketPath = '/tmp/rmake/lib/chrootsocket'
        uri = 'unix:%s%s' % (self.cfg.root, socketPath)


        pid = os.fork()
        if pid:
            # paths passed back from the server will be relative to the chroot
            # if we chroot into it, otherwise they'll be relative to /
            if self.canChroot():
                clientRoot = self.cfg.root
            else:
                clientRoot = '/'
            client = chrootserver.ChrootClient(clientRoot, uri, pid)

            def checkPid():
                checkedPid, status = os.waitpid(pid, os.WNOHANG)
                if checkedPid:
                    raise errors.OpenError('Chroot server failed to start - please check %s' % self.serverCfg.getBuildLogPath(self.jobId))


            timeSlept = 0
            while timeSlept < 180:
                if os.path.exists('%s%s' % (self.cfg.root, socketPath)):
                    break
                checkPid()
                time.sleep(.1)
                timeSlept += .1

            client.ping(hook=checkPid, seconds=60)
            self.buildTrove.log('Chroot Created')
            return client
        else:
            if self.canChroot():
                prog = self.chrootHelperPath
                args = [prog, self.cfg.root]
                if self.targetArch:
                    args.extend(['--arch', self.targetArch])
                os.execv(prog, args)
            else:
                # testsuite path
                rmakeDir = os.path.dirname(sys.modules['rmake'].__file__)
                conaryDir = os.path.dirname(sys.modules['conary'].__file__)
                prog = (self.cfg.root + constants.chrootRmakePath
                        + constants.chrootServerPath)
                util.mkdirChain(self.cfg.root + '/tmp/rmake/lib')
                args = [prog, 'start', '-n', '--config', 
                        'root %s' % self.cfg.root]
                os.execve(prog, args,
                      {'PYTHONPATH' : '%s:%s' % (os.path.dirname(rmakeDir),
                                                 os.path.dirname(conaryDir))})


class ChrootFactory(object):
    def __init__(self, job, baseDir, chrootHelperPath, buildCfg, serverCfg):
        self.job = job
        self.jobId = job.jobId
        self.cfg = buildCfg
        self.serverCfg = serverCfg
        self.cfg = copy.deepcopy(self.cfg)
        self.cfg.threaded = False
        self.baseDir = baseDir
        self.chrootHelperPath = chrootHelperPath
        cacheDir = self.baseDir + '/cscache'
        util.mkdirChain(cacheDir)
        self.csCache = changesetcache.ChangeSetCache(cacheDir)
        self.chroots = {}

    def info(self, message):
        log.info('[%s] [jobId %s] CF: %s', time.strftime('%x %X'), self.jobId,
                  message)

    def warning(self, message):
        log.warning('[%s] [jobId %s] CF: %s', time.strftime('%x %X'), 
                    self.jobId, message)



    def createRoot(self, jobList, buildTrove):
        rootDir = self.baseDir + '/chroot'
        self.cfg.root = rootDir
        self.cfg.logFile = '/var/log/conary'
        self.cfg.dbPath = '/var/lib/conarydb'
        socketPath = '/tmp/lib/rmake/chrootsocket'

        setArch, targetArch = flavorutil.getTargetArch(buildTrove.flavor)
        if not setArch:
            targetArch = None

        chroot = ConaryBasedRoot(buildTrove, rootDir,
                                 self.chrootHelperPath,
                                 self.cfg, self.serverCfg, jobList,
                                 csCache=self.csCache,
                                 targetArch=targetArch)
        chroot.create()

        client = chroot.start()
        self.chroots[client.getPid()] = (chroot, client)
        return client

    def cleanRoot(self, pid):
        root, client = self.chroots[pid]
        self.killChild(pid)
        root.clean()

    def __del__(self):
        self.killChildren()

    def killChildren(self):
        for pid in list(self.chroots): # make copy since is modified
            self.killChild(pid)

    def killChild(self, pid):
        root, client = self.chroots[pid]
        try:
            client.stop()
        except OSError, err:
            if err.errno != errno.ESRCH:
                raise
        else:
            died = False
            for i in xrange(400):
                foundPid, status = os.waitpid(pid, os.WNOHANG)
                if not foundPid:
                    time.sleep(.1)
                else:
                    died = True
                    break
            if not died:
                self.warning('child process %s did not shut down' % pid)
            else:
                del self.chroots[pid]
