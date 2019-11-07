# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maasserver.utils.dbtasks`."""

__all__ = []

import random
import threading
from unittest.mock import sentinel

from crochet import wait_for
from maasserver.testing.testcase import MAASTransactionServerTestCase
from maasserver.utils.dbtasks import (
    DatabaseTaskAlreadyRunning,
    DatabaseTasksService,
)
from maasserver.utils.orm import transactional
from maastesting.factory import factory
from maastesting.testcase import MAASTestCase
from maastesting.twisted import TwistedLoggerFixture
from testtools.matchers import (
    Equals,
    HasLength,
    Is,
    IsInstance,
    MatchesAll,
    MatchesAny,
    MatchesStructure,
    Not,
)
from twisted.internet import reactor
from twisted.internet.defer import (
    Deferred,
    DeferredQueue,
    inlineCallbacks,
    QueueOverflow,
)


wait_for_reactor = wait_for(30)  # 30 seconds.


noop = lambda: None


class TestDatabaseTaskService(MAASTestCase):
    """Tests for `DatabaseTasksService`."""

    def test__init(self):
        service = DatabaseTasksService()
        self.assertThat(
            service,
            MatchesStructure(
                # The queue does not permit anything to go in it.
                queue=MatchesAll(
                    IsInstance(DeferredQueue),
                    MatchesStructure.byEquality(size=0, backlog=1),
                    first_only=True,
                )
            ),
        )

    def test__cannot_add_task_to_unstarted_service(self):
        service = DatabaseTasksService()
        self.assertRaises(QueueOverflow, service.addTask, noop)

    def test__cannot_add_task_to_stopped_service(self):
        service = DatabaseTasksService()
        service.startService()
        service.stopService()
        self.assertRaises(QueueOverflow, service.addTask, noop)

    def test__startup_creates_queue_with_previously_defined(self):
        service = DatabaseTasksService()
        service.startService()
        try:
            self.assertThat(
                service,
                MatchesStructure(
                    queue=MatchesAll(
                        IsInstance(DeferredQueue),
                        MatchesStructure.byEquality(backlog=1),
                        first_only=True,
                    )
                ),
            )
        finally:
            service.stopService()

    def test__task_is_executed_in_other_thread(self):
        get_thread_ident = lambda: threading.currentThread().ident
        service = DatabaseTasksService()
        service.startService()
        try:
            ident_from_task = service.deferTask(get_thread_ident).wait(30)
            ident_from_here = get_thread_ident()
            self.expectThat(ident_from_task, IsInstance(int, int))
            self.expectThat(ident_from_task, Not(Equals(ident_from_here)))
        finally:
            service.stopService()

    def test__arguments_are_passed_through_to_task(self):
        def return_args(*args, **kwargs):
            return sentinel.here, args, kwargs

        service = DatabaseTasksService()
        service.startService()
        try:
            result = service.deferTask(
                return_args, sentinel.arg, kw=sentinel.kw
            ).wait(30)
            self.assertThat(
                result,
                Equals((sentinel.here, (sentinel.arg,), {"kw": sentinel.kw})),
            )
        finally:
            service.stopService()

    def test__tasks_are_all_run_before_shutdown_completes(self):
        service = DatabaseTasksService()
        service.startService()
        try:
            queue = service.queue
            event = threading.Event()
            count = random.randint(20, 40)
            for _ in range(count):
                service.addTask(event.wait)
            # The queue has `count` tasks (or `count - 1` tasks; the first may
            # have already been pulled off the queue) still pending.
            self.assertThat(
                queue.pending,
                MatchesAny(HasLength(count), HasLength(count - 1)),
            )
        finally:
            event.set()
            service.stopService()
        # The queue is empty and nothing is waiting.
        self.assertThat(
            queue, MatchesStructure.byEquality(waiting=[], pending=[])
        )

    @wait_for_reactor
    @inlineCallbacks
    def test__deferred_task_can_be_cancelled_when_enqueued(self):
        things = []  # This will NOT be populated by tasks.

        service = DatabaseTasksService()
        yield service.startService()
        try:
            event = threading.Event()
            service.deferTask(event.wait)
            service.deferTask(things.append, 1).cancel()
        finally:
            event.set()
            yield service.stopService()

        self.assertThat(things, Equals([]))

    @wait_for_reactor
    @inlineCallbacks
    def test__deferred_task_cannot_be_cancelled_when_running(self):
        # DatabaseTaskAlreadyRunning is raised when attempting to cancel a
        # database task that's already running.
        service = DatabaseTasksService()
        yield service.startService()
        try:
            ready = Deferred()
            d = service.deferTask(reactor.callFromThread, ready.callback, None)
            # Wait for the task to begin running.
            yield ready
            # We have the reactor thread. Even if the task completes its
            # status will not be updated until the reactor's next iteration.
            self.assertRaises(DatabaseTaskAlreadyRunning, d.cancel)
        finally:
            yield service.stopService()

    @wait_for_reactor
    @inlineCallbacks
    def test__sync_task_can_be_cancelled_when_enqueued(self):
        things = []  # This will NOT be populated by tasks.

        service = DatabaseTasksService()
        yield service.startService()
        try:
            event = threading.Event()
            service.deferTask(event.wait)
            service.syncTask().cancel()
        finally:
            event.set()
            yield service.stopService()

        self.assertThat(things, Equals([]))

    def test__sync_task_fires_with_service(self):
        service = DatabaseTasksService()
        service.startService()
        try:
            self.assertThat(service.syncTask().wait(30), Is(service))
        finally:
            service.stopService()

    def test__failure_in_deferred_task_does_not_crash_service(self):
        things = []  # This will be populated by tasks.
        exception_type = factory.make_exception_type()

        def be_bad():
            raise exception_type("I'm being very naughty.")

        service = DatabaseTasksService()
        service.startService()
        try:
            service.deferTask(things.append, 1).wait(30)
            self.assertRaises(
                exception_type, service.deferTask(be_bad).wait, 30
            )
            service.deferTask(things.append, 2).wait(30)
        finally:
            service.stopService()

        self.assertThat(things, Equals([1, 2]))

    def test__failure_in_added_task_does_not_crash_service(self):
        things = []  # This will be populated by tasks.
        exception_type = factory.make_exception_type()

        def be_bad():
            raise exception_type("I'm bad, so bad.")

        service = DatabaseTasksService()
        service.startService()
        try:
            service.addTask(things.append, 1)
            service.addTask(be_bad)
            service.addTask(things.append, 2)
        finally:
            service.stopService()

        self.assertThat(things, Equals([1, 2]))

    def test__failure_in_task_is_logged(self):
        logger = self.useFixture(TwistedLoggerFixture())

        service = DatabaseTasksService()
        service.startService()
        try:
            service.addTask(lambda: 0 / 0)
        finally:
            service.stopService()

        self.assertDocTestMatches(
            """\
            ...Unhandled failure in database task.
            Traceback (most recent call last):
            ...
            builtins.ZeroDivisionError: ...
            """,
            logger.output,
        )


class TestDatabaseTaskServiceWithActualDatabase(MAASTransactionServerTestCase):
    """Tests for `DatabaseTasksService` with the databse."""

    def test__task_can_access_database_from_other_thread(self):
        @transactional
        def database_task():
            # Merely being here means we've accessed the database.
            return sentinel.beenhere

        service = DatabaseTasksService()
        service.startService()
        try:
            result = service.deferTask(database_task).wait(30)
            self.assertThat(result, Is(sentinel.beenhere))
        finally:
            service.stopService()
