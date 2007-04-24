#
# Copyright (c) 2006-2007 rPath, Inc.  All Rights Reserved.
#
"""
Builder controls the process of building a set of troves.
"""
import signal
import sys
import os
import time
import traceback

from conary import conaryclient
from conary.repository import changeset

from rmake import failure
from rmake.build import buildtrove
from rmake.build import buildjob
from rmake.build import dephandler
from rmake.lib import logfile
from rmake.lib import logger
from rmake.lib import recipeutil
from rmake.lib import repocache
from rmake.worker import worker

class Builder(object):
    """
        Build manager for rMake.

        Basically:
            * get a set of troves in init.
            * load the troves to determine what packages they create,
              while flavors they use, and what build requirements they have.
            * while buildable troves left:
                * build one trove.
                * commit to internal repos if successful.

        Almost all passing of information from the builder is done through 
        subscription.  Instances register to listen to particular events on 
        the trove and job objects.  Those events are triggered by changing the
        states of the trove objects.

        Instances that listen on this side of the rMake server are called
        "Internal subscribers" - the database is one, the message passer that
        lets the rmake server know about status updates is another.

        See build/subscribe.py for more information.

        @param serverCfg: rmake server Configuration.  Used to determine 
        location to commit troves.
        @type serverCfg: rmake.server.servercfg.rMakeConfiguration
        @param buildCfg: build configuration, describes all parameters for 
        build.
        @type buildCfg: rmake.build.buildcfg.BuildConfiguration instance.
    """
    def __init__(self, serverCfg, buildCfg, job, jobContext=None):
        self.serverCfg = serverCfg
        self.buildCfg = buildCfg
        self.logger = BuildLogger(job.jobId,
                                  serverCfg.getBuildLogPath(job.jobId))
        self.logFile = logfile.LogFile(
                                    serverCfg.getBuildLogPath(job.jobId))
        self.repos = self.getRepos()
        self.job = job
        self.jobId = job.jobId
        self.worker = worker.Worker(serverCfg, self.logger, serverCfg.slots)
        self.eventHandler = EventHandler(job, self.worker)
        if jobContext:
            self.setJobContext(jobContext)
        else:
            self.jobContext = []
        self.initialized = False

    def _installSignalHandlers(self):
        signal.signal(signal.SIGTERM, self._signalHandler)
        signal.signal(signal.SIGINT, self._signalHandler)

    def _closeLog(self):
        self.logFile.close()
        self.logger.close()

    def setJobContext(self, jobList):
        self.jobContext = jobList

    def setWorker(self, worker):
        self.worker = worker

    def getWorker(self):
        return self.worker

    def getJob(self):
        return self.job

    def getRepos(self):
        repos = conaryclient.ConaryClient(self.buildCfg).getRepos()
        if self.serverCfg.useCache:
            return repocache.CachingTroveSource(repos,
                                            self.serverCfg.getCacheDir())
        return repos

    def info(self, state, message):
        self.logger.info(message)

    def _signalHandler(self, sigNum, frame):
        try:
            signal.signal(sigNum, signal.SIG_DFL)
            self.worker.stopAllCommands()
            self.job.jobFailed('Received signal %s' % sigNum)
            os.kill(os.getpid(), sigNum)
        finally:
            os._exit(1)

    def buildAndExit(self):
        try:
            try:
                signal.signal(signal.SIGTERM, self._signalHandler)
                self.logFile.redirectOutput() # redirect all output to the log 
                                              # file.
                                              # We do this to ensure that
                                              # output we don't control,
                                              # such as conary output, is
                                              # directed to a file.
                self.build()
                os._exit(0)
            except Exception, err:
                self.logger.error(traceback.format_exc())
                self.job.exceptionOccurred(err, traceback.format_exc())
                self.logFile.restoreOutput()
                try:
                    self.worker.stopAllCommands()
                finally:
                    if sys.stdin.isatty():
                        # this sets us back to be connected with the controlling
                        # terminal (owned by our parent, the rmake server)
                        import epdb
                        epdb.post_mortem(sys.exc_info()[2])
                    os._exit(0)
        finally:
            os._exit(1)

    def initializeBuild(self):
        self.initialized = True
        self.job.log('Build started - loading troves')
        buildTroves = recipeutil.getSourceTrovesFromJob(self.job,
                                                        self.buildCfg,
                                                        self.serverCfg,
                                                        self.repos)
        self._matchTrovesToJobContext(buildTroves, self.jobContext)
        self.job.setBuildTroves(buildTroves)

        self.dh = dephandler.DependencyHandler(self.job.getPublisher(),
                                               self.buildCfg,
                                               self.logger, buildTroves)

        if not self._checkBuildSanity(buildTroves):
            return False
        return True

    def build(self):
        self.job.jobStarted("Starting Build %s (pid %s)" % (self.job.jobId,
                            os.getpid()), pid=os.getpid())
        # main loop is here.
        if not self.initialized:
            if not self.initializeBuild():
                return False

        if self.dh.moreToDo():
            while self.dh.moreToDo():
                self.worker.handleRequestIfReady()
                if self.worker._checkForResults():
                    self.resolveIfReady()
                elif self.dh.hasBuildableTroves():
                    trv, (buildReqs, crossReqs) = self.dh.popBuildableTrove()
                    self.buildTrove(trv, buildReqs, crossReqs)
                elif not self.resolveIfReady():
                    time.sleep(0.1)
            if self.dh.jobPassed():
                self.job.jobPassed("build job finished successfully")
                return True
            self.job.jobFailed("build job had failures")
        else:
            self.job.jobFailed('Did not find any buildable troves')
        return False

    def buildTrove(self, troveToBuild, buildReqs, crossReqs):
        targetLabel = self.buildCfg.getTargetLabel(troveToBuild.getVersion())
        troveToBuild.troveQueued('Waiting to be assigned to chroot')
        troveToBuild.disown()
        logHost, logPort = self.worker.startTroveLogger(troveToBuild)
        if troveToBuild.isDelayed():
            builtTroves = self.job.getBuiltTroveList()
        else:
            builtTroves = []
        self.worker.buildTrove(self.buildCfg, troveToBuild.jobId,
                               troveToBuild, self.eventHandler, buildReqs,
                               crossReqs, targetLabel, logHost, logPort,
                               builtTroves=builtTroves)

    def resolveIfReady(self):
        resolveJob = self.dh.getNextResolveJob()
        if resolveJob:
            resolveJob.getTrove().disown()
            self.worker.resolve(resolveJob, self.eventHandler)
            return True
        return False

    def _matchTrovesToJobContext(self, buildTroves, jobContext):
        trovesByNVF = {}
        for trove in buildTroves:
            trovesByNVF[trove.getNameVersionFlavor()] = trove

        for job in reversed(jobContext): # go through last job first.
            for trove in job.iterTroves():
                if not trove.isBuilt():
                    continue
                toBuild = trovesByNVF.pop(trove.getNameVersionFlavor(), None)
                if toBuild:
                    buildReqs = None
                    binaries = trove.getBinaryTroves()
                    for troveTup in binaries:
                        if ':' not in troveTup[0]:
                            trv = self.repos.getTrove(withFiles=False,
                                                      *troveTup)
                            buildReqs = trv.getBuildRequirements()
                            break
                    if not buildReqs:
                        continue
                    toBuild.trovePrebuilt(buildReqs, binaries)

    def _checkBuildSanity(self, buildTroves):
        def _isSolitaryTrove(trv):
            return (trv.isRedirectRecipe() or trv.isFilesetRecipe())


        delayed = [ x for x in buildTroves if _isSolitaryTrove(x) ]
        if delayed and len(buildTroves) > 1:
            err = ('redirect and fileset packages must'
                   ' be alone in their own job')
            for trove in delayed:
                # publish failed status
                trove.troveFailed(failure.FailureReason('Trove failed sanity check: %s' % err))
            troveNames = ', '.join(x.getName().split(':')[0] for x in delayed)
            self.job.jobFailed(failure.FailureReason("Job failed sanity check: %s: %s" % (err, troveNames)))
            return False

        isGroup = [ x for x in buildTroves if x.isGroupRecipe() ]
        if isGroup and len(buildTroves) > 1:
            self.job.log("WARNING: Combining group troves with other troves"
                         " is EXPERIMENTAL - use at your own risk")
            time.sleep(3)
        return True

class BuildLogger(logger.Logger):
   def __init__(self, jobId, path):
        logger.Logger.__init__(self, 'build-%s' % jobId, path)

from rmake.lib import subscriber
class EventHandler(subscriber.StatusSubscriber):
    listeners = { 'TROVE_PREPARING_CHROOT' : 'trovePreparingChroot',
                  'TROVE_BUILT'            : 'troveBuilt',
                  'TROVE_FAILED'           : 'troveFailed',
                  'TROVE_RESOLVING'        : 'troveResolving',
                  'TROVE_RESOLVED'         : 'troveResolutionCompleted',
                  'TROVE_LOG_UPDATED'      : 'troveLogUpdated',
                  'TROVE_BUILDING'         : 'troveBuilding',
                  'TROVE_STATE_UPDATED'    : 'troveStateUpdated' }

    def __init__(self, job, server):
        self.server = server
        self.job = job
        self._hadEvent = False
        subscriber.StatusSubscriber.__init__(self, None, None)

    def hadEvent(self):
        return self._hadEvent

    def reset(self):
        self._hadEvent = False

    def troveBuilt(self, (jobId, troveTuple), binaryTroveList):
        self._hadEvent = True
        t = self.job.getTrove(*troveTuple)
        self.server.stopTroveLogger(t)
        t.troveBuilt(binaryTroveList)
        t.own()

    def troveLogUpdated(self, (jobId, troveTuple), state, log):
        t = self.job.getTrove(*troveTuple)
        t.log(log)

    def troveFailed(self, (jobId, troveTuple), failureReason):
        self._hadEvent = True
        t = self.job.getTrove(*troveTuple)
        self.server.stopTroveLogger(t)
        t.troveFailed(failureReason)
        t.own()

    def troveResolving(self, (jobId, troveTuple), chrootHost):
        t = self.job.getTrove(*troveTuple)
        t.troveResolvingBuildReqs(chrootHost)

    def troveResolutionCompleted(self, (jobId, troveTuple), resolveResults):
        self._hadEvent = True
        t = self.job.getTrove(*troveTuple)
        t.troveResolved(resolveResults)
        t.own()

    def trovePreparingChroot(self, (jobId, troveTuple), chrootHost, chrootPath):
        t = self.job.getTrove(*troveTuple)
        t.creatingChroot(chrootHost, chrootPath)

    def troveBuilding(self, (jobId, troveTuple), logPath, pid):
        t = self.job.getTrove(*troveTuple)
        t.troveBuilding(logPath, pid)

    def troveStateUpdated(self, (jobId, troveTuple), state, status):
        if state not in (buildtrove.TROVE_STATE_FAILED,
                         buildtrove.TROVE_STATE_UNBUILDABLE,
                         buildtrove.TROVE_STATE_BUILT,
                         buildtrove.TROVE_STATE_RESOLVING,
                         buildtrove.TROVE_STATE_PREPARING,
                         buildtrove.TROVE_STATE_BUILDING):
            t = self.job.getTrove(*troveTuple)
            t._setState(state, status)
