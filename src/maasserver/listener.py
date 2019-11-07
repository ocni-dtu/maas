# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Listens for NOTIFY events from the postgres database."""

__all__ = ["PostgresListenerNotifyError", "PostgresListenerService"]

from collections import defaultdict
from contextlib import closing
from errno import ENOENT

from django.db import connections
from django.db.utils import load_backend
from provisioningserver.utils.enum import map_enum
from provisioningserver.utils.events import EventGroup
from provisioningserver.utils.twisted import callOut, suppress, synchronous
from twisted.application.service import Service
from twisted.internet import defer, error, interfaces, reactor, task
from twisted.internet.defer import CancelledError, Deferred, succeed
from twisted.internet.task import deferLater
from twisted.internet.threads import deferToThread
from twisted.logger import Logger
from twisted.python.failure import Failure
from zope.interface import implementer


class ACTIONS:
    """Notify action types."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class PostgresListenerNotifyError(Exception):
    """Error raised when the listener gets a notify message that cannot be
    decoded or is not being handled."""


class PostgresListenerRegistrationError(Exception):
    """Error raised when registering a handler fails."""


class PostgresListenerUnregistrationError(Exception):
    """Error raised when unregistering a handler fails."""


@implementer(interfaces.IReadDescriptor)
class PostgresListenerService(Service, object):
    """Listens for NOTIFY messages from postgres.

    A new connection is made to postgres with the isolation level of
    autocommit. This connection is only used for listening for notifications.
    Any query that needs to take place because of a notification should use
    its own connection. This class runs inside of the reactor. Any long running
    action that occurrs based on a notification should defer its action to a
    thread to not block the reactor.

    :ivar connection: A database connection within one of Django's wrapper.
    :ivar connectionFileno: The fileno of the underlying database connection.
    :ivar connecting: a :class:`Deferred` while connecting, `None` at all
        other times.
    :ivar disconnecting: a :class:`Deferred` while disconnecting, `None`
        at all other times.
    """

    # Seconds to wait to handle new notifications. When the notifications set
    # is empty it will wait this amount of time to check again for new
    # notifications.
    HANDLE_NOTIFY_DELAY = 0.5

    def __init__(self, alias="default"):
        self.alias = alias
        self.listeners = defaultdict(list)
        self.autoReconnect = False
        self.connection = None
        self.connectionFileno = None
        self.notifications = set()
        self.notifier = task.LoopingCall(self.handleNotifies)
        self.notifierDone = None
        self.connecting = None
        self.disconnecting = None
        self.registeredChannels = False
        self.log = Logger(__name__, self)
        self.events = EventGroup("connected", "disconnected")

    def startService(self):
        """Start the listener."""
        super(PostgresListenerService, self).startService()
        self.autoReconnect = True
        return self.tryConnection()

    def stopService(self):
        """Stop the listener."""
        super(PostgresListenerService, self).stopService()
        self.autoReconnect = False
        return self.loseConnection()

    def connected(self):
        """Return True if connected."""
        if self.connection is None:
            return False
        if self.connection.connection is None:
            return False
        return self.connection.connection.closed == 0

    def logPrefix(self):
        """Return nice name for twisted logging.

        This is required to satisfy `IReadDescriptor`, which inherits from
        `ILoggingContext`.
        """
        return self.log.namespace

    def isSystemChannel(self, channel):
        """Return True if channel is a system channel."""
        return channel.startswith("sys_")

    def doRead(self):
        """Poll the connection and process any notifications."""
        try:
            self.connection.connection.poll()
        except Exception:
            # If the connection goes down then `OperationalError` is raised.
            # It contains no pgcode or pgerror to identify the reason so no
            # special consideration can be made for it. Hence all errors are
            # treated the same, and we assume that the connection is broken.
            #
            # We do NOT return a failure, which would signal to the reactor
            # that the connection is broken in some way, because the reactor
            # will end up removing this instance from its list of selectables
            # but not from its list of readable fds, or something like that.
            # The point is that the reactor's accounting gets muddled. Things
            # work correctly if we manage the disconnection ourselves.
            #
            self.loseConnection(Failure(error.ConnectionLost()))
        else:
            # Add each notify to to the notifications set. This removes
            # duplicate notifications when one entity in the database is
            # updated multiple times in a short interval. Accumulating
            # notifications and allowing the listener to pick them up in
            # batches is imperfect but good enough, and simple.
            notifies = self.connection.connection.notifies
            if len(notifies) != 0:
                for notify in notifies:
                    if self.isSystemChannel(notify.channel):
                        # System level message; pass it to the registered
                        # handler immediately.
                        if notify.channel in self.listeners:
                            # Be defensive in that if a handler does not exist
                            # for this channel then the channel should be
                            # unregisted and removed from listeners.
                            if len(self.listeners[notify.channel]) > 0:
                                handler = self.listeners[notify.channel][0]
                                handler(notify.channel, notify.payload)
                            else:
                                self.unregisterChannel(notify.channel)
                                del self.listeners[notify.channel]
                        else:
                            # Unregister the channel since no listener is
                            # registered for this channel.
                            self.unregisterChannel(notify.channel)
                    else:
                        # Place non-system messages into the queue to be
                        # processed.
                        self.notifications.add(
                            (notify.channel, notify.payload)
                        )
                # Delete the contents of the connection's notifies list so
                # that we don't process them a second time.
                del notifies[:]

    def fileno(self):
        """Return the fileno of the connection."""
        return self.connectionFileno

    def startReading(self):
        """Add this listener to the reactor."""
        self.connectionFileno = self.connection.connection.fileno()
        reactor.addReader(self)

    def stopReading(self):
        """Remove this listener from the reactor."""
        try:
            reactor.removeReader(self)
        except IOError as error:
            # ENOENT here means that the fd has already been unregistered
            # from the underlying poller. It is as yet unclear how we get
            # into this state, so for now we ignore it. See epoll_ctl(2).
            if error.errno != ENOENT:
                raise
        finally:
            self.connectionFileno = None

    def register(self, channel, handler):
        """Register listening for notifications from a channel.

        When a notification is received for that `channel` the `handler` will
        be called with the action and object id.
        """
        handlers = self.listeners[channel]
        if self.isSystemChannel(channel) and len(handlers) > 0:
            # A system can only be registered once. This is because the
            # message is passed directly to the handler and the `doRead`
            # method does not wait for it to finish if its a defer. This is
            # different from normal handlers where we will call each and wait
            # for all to resolve before continuing to the next event.
            raise PostgresListenerRegistrationError(
                "System channel '%s' has already been registered." % channel
            )
        else:
            handlers.append(handler)
        if self.registeredChannels and self.connection:
            # Channels have already been registered. Register the
            # new channel on the already existing connection.
            self.registerChannel(channel)

    def unregister(self, channel, handler):
        """Unregister listening for notifications from a channel.

        `handler` needs to be same handler that was registered.
        """
        if channel not in self.listeners:
            raise PostgresListenerUnregistrationError(
                "Channel '%s' is not registered with the listener." % channel
            )
        handlers = self.listeners[channel]
        if handler in handlers:
            handlers.remove(handler)
        else:
            raise PostgresListenerUnregistrationError(
                "Handler is not registered on that channel '%s'." % channel
            )
        if self.registeredChannels and self.connection and len(handlers) == 0:
            # Channels have already been registered. Unregister the channel.
            self.unregisterChannel(channel)

    @synchronous
    def createConnection(self):
        """Create new database connection."""
        db = connections.databases[self.alias]
        backend = load_backend(db["ENGINE"])
        return backend.DatabaseWrapper(
            db, self.alias, allow_thread_sharing=True
        )

    @synchronous
    def startConnection(self):
        """Start the database connection."""
        self.registeredChannels = False
        self.connection = self.createConnection()
        self.connection.connect()
        self.connection.set_autocommit(True)

    @synchronous
    def stopConnection(self):
        """Stop database connection."""
        # The connection is often in an unexpected state here -- for
        # unexplained reasons -- so be careful when unpealing layers.
        connection_wrapper, self.connection = self.connection, None
        if connection_wrapper is not None:
            connection = connection_wrapper.connection
            if connection is not None and not connection.closed:
                connection_wrapper.commit()
                connection_wrapper.close()
        self.registeredChannels = False

    def tryConnection(self):
        """Keep retrying to make the connection."""
        if self.connecting is None:
            if self.disconnecting is not None:
                raise RuntimeError(
                    "Cannot attempt to make new connection before "
                    "pending disconnection has finished."
                )

            def cb_connect(_):
                self.log.info("Listening for database notifications.")

            def eb_connect(failure):
                self.log.error(
                    "Unable to connect to database: {error}",
                    error=failure.getErrorMessage(),
                )
                if failure.check(CancelledError):
                    return failure
                elif self.autoReconnect:
                    return deferLater(reactor, 3, connect)
                else:
                    return failure

            def connect(interval=self.HANDLE_NOTIFY_DELAY):
                d = deferToThread(self.startConnection)
                d.addCallback(callOut, deferToThread, self.registerChannels)
                d.addCallback(callOut, self.events.connected.fire)
                d.addCallback(callOut, self.startReading)
                d.addCallback(callOut, self.runHandleNotify, interval)
                # On failure ensure that the database connection is stopped.
                d.addErrback(callOut, deferToThread, self.stopConnection)
                d.addCallbacks(cb_connect, eb_connect)
                return d

            def done():
                self.connecting = None

            self.connecting = connect().addBoth(callOut, done)

        return self.connecting

    def loseConnection(self, reason=Failure(error.ConnectionDone())):
        """Request that the connection be dropped."""
        if self.disconnecting is None:
            d = self.disconnecting = Deferred()
            d.addBoth(callOut, self.stopReading)
            d.addBoth(callOut, self.cancelHandleNotify)
            d.addBoth(callOut, deferToThread, self.stopConnection)
            d.addBoth(callOut, self.connectionLost, reason)

            def done():
                self.disconnecting = None

            d.addBoth(callOut, done)

            if self.connecting is None:
                # Already/never connected: begin shutdown now.
                self.disconnecting.callback(None)
            else:
                # Still connecting: cancel before disconnect.
                self.connecting.addErrback(suppress, CancelledError)
                self.connecting.chainDeferred(self.disconnecting)
                self.connecting.cancel()

        return self.disconnecting

    def connectionLost(self, reason):
        """Reconnect when the connection is lost."""
        self.connection = None
        if reason.check(error.ConnectionDone):
            self.log.debug("Connection closed.")
        elif reason.check(error.ConnectionLost):
            self.log.debug("Connection lost.")
        else:
            self.log.failure("Connection lost.", reason)
        if self.autoReconnect:
            reactor.callLater(3, self.tryConnection)
        self.events.disconnected.fire(reason)

    def registerChannel(self, channel):
        """Register the channel."""
        with closing(self.connection.cursor()) as cursor:
            if self.isSystemChannel(channel):
                # This is a system channel so listen only called once.
                cursor.execute("LISTEN %s;" % channel)
            else:
                # Not a system channel so listen called once for each action.
                for action in sorted(map_enum(ACTIONS).values()):
                    cursor.execute("LISTEN %s_%s;" % (channel, action))

    def unregisterChannel(self, channel):
        """Unregister the channel."""
        with closing(self.connection.cursor()) as cursor:
            if self.isSystemChannel(channel):
                # This is a system channel so unlisten only called once.
                cursor.execute("UNLISTEN %s;" % channel)
            else:
                # Not a system channel so unlisten called once for each action.
                for action in sorted(map_enum(ACTIONS).values()):
                    cursor.execute("UNLISTEN %s_%s;" % (channel, action))

    def registerChannels(self):
        """Register the all the channels."""
        for channel in self.listeners.keys():
            self.registerChannel(channel)
        self.registeredChannels = True

    def convertChannel(self, channel):
        """Convert the postgres channel to a registered channel and action.

        :raise PostgresListenerNotifyError: When {channel} is not registered or
            {action} is not in `ACTIONS`.
        """
        channel, action = channel.split("_", 1)
        if channel not in self.listeners:
            raise PostgresListenerNotifyError(
                "%s is not a registered channel." % channel
            )
        if action not in map_enum(ACTIONS).values():
            raise PostgresListenerNotifyError(
                "%s action is not supported." % action
            )
        return channel, action

    def runHandleNotify(self, delay=0, clock=reactor):
        """Defer later the `handleNotify`."""
        if not self.notifier.running:
            self.notifierDone = self.notifier.start(delay, now=False)

    def cancelHandleNotify(self):
        """Cancel the deferred `handleNotify` call."""
        if self.notifier.running:
            self.notifier.stop()
            return self.notifierDone
        else:
            return succeed(None)

    def handleNotifies(self, clock=reactor):
        """Process all notify message in the notifications set."""

        def gen_notifications(notifications):
            while len(notifications) != 0:
                yield notifications.pop()

        return task.coiterate(
            self.handleNotify(notification, clock=clock)
            for notification in gen_notifications(self.notifications)
        )

    def handleNotify(self, notification, clock=reactor):
        """Process a notify message in the notifications set."""
        channel, payload = notification
        try:
            channel, action = self.convertChannel(channel)
        except PostgresListenerNotifyError:
            # Log the error and continue processing the remaining
            # notifications.
            self.log.failure(
                "Failed to convert channel {channel!r}.", channel=channel
            )
        else:
            defers = []
            handlers = self.listeners[channel]
            # XXX: There could be an arbitrary number of listeners. Should we
            # limit concurrency here? Perhaps even do one at a time.
            for handler in handlers:
                d = defer.maybeDeferred(handler, action, payload)
                d.addErrback(
                    lambda failure: self.log.failure(
                        "Failure while handling notification to {channel!r}: "
                        "{payload!r}",
                        failure,
                        channel=channel,
                        payload=payload,
                    )
                )
                defers.append(d)
            return defer.DeferredList(defers)
