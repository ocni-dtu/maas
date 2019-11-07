# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Services related to DNS publication."""

__all__ = ["DNSPublicationGarbageService"]

from datetime import datetime, timedelta
import random

from maasserver.models.dnspublication import DNSPublication
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
from provisioningserver.logger import LegacyLogger
from provisioningserver.utils.twisted import callOut
from pytz import UTC
from twisted.application.service import Service
from twisted.internet import reactor
from twisted.internet.task import LoopingCall


log = LegacyLogger()


class DNSPublicationGarbageService(Service):
    """Periodically delete DNS publications more than 7 days old."""

    clock = None

    def startService(self):
        super().startService()
        self._loop = LoopingCall(self._tryCollectGarbage)
        self._loop.clock = reactor if self.clock is None else self.clock
        self._loopDone = self._loop.start(self._getInterval(), now=False)
        self._loopDone.addErrback(log.err, "Garbage loop failed.")

    def stopService(self):
        if self._loop.running:
            self._loop.stop()
        return self._loopDone.addBoth(callOut, super().stopService)

    def _getInterval(self):
        """Return a random interval between 3 and 6 hours.

        :return: The number of seconds.
        """
        return random.randrange(
            timedelta(hours=3).total_seconds(),
            timedelta(hours=6).total_seconds(),
        )

    def _updateInterval(self):
        """Update the loop's interval.

        Only call this when the loop is running, otherwise it will crash.
        Also, it only really makes sense to call it when the loop's function
        is executing otherwise it will have no effect until the next loop.
        """
        self._loop.interval = self._getInterval()

    def _tryCollectGarbage(self):
        cutoff = datetime.utcnow().replace(tzinfo=UTC) - timedelta(days=7)
        d = deferToDatabase(self._collectGarbage, cutoff)  # In a transaction.
        d.addBoth(callOut, self._updateInterval)  # Always adjust the schedule.
        d.addErrback(log.err, "Failure when removing old DNS publications.")
        return d

    @transactional
    def _collectGarbage(self, cutoff):
        return DNSPublication.objects.collect_garbage(cutoff)
