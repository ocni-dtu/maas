# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maasserver.utils.threads`."""

__all__ = []

import random
from unittest.mock import sentinel

from crochet import wait_for
from django.db import connection
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils import orm, threads
from maastesting.testcase import MAASTestCase
from provisioningserver.utils.twisted import ThreadPool, ThreadUnpool
from testtools.matchers import Equals, Is, IsInstance
from twisted.internet import reactor
from twisted.internet.defer import DeferredSemaphore, inlineCallbacks


wait_for_reactor = wait_for(30)  # 30 seconds.


class TestMakeFunctions(MAASTestCase):
    """Tests for the `make_*` functions."""

    def test__make_default_pool_creates_disconnected_pool(self):
        pool = threads.make_default_pool()
        self.assertThat(pool, IsInstance(ThreadPool))
        self.assertThat(
            pool.context.contextFactory, Is(orm.TotallyDisconnected)
        )
        self.assertThat(pool.max, Equals(threads.max_threads_for_default_pool))
        self.assertThat(pool.min, Equals(0))

    def test__make_default_pool_accepts_max_threads_setting(self):
        maxthreads = random.randint(1, 1000)
        pool = threads.make_default_pool(maxthreads)
        self.assertThat(pool.max, Equals(maxthreads))
        self.assertThat(pool.min, Equals(0))

    def test__make_database_pool_creates_connected_pool(self):
        pool = threads.make_database_pool()
        self.assertThat(pool, IsInstance(ThreadPool))
        self.assertThat(pool.context.contextFactory, Is(orm.FullyConnected))
        self.assertThat(
            pool.max, Equals(threads.max_threads_for_database_pool)
        )
        self.assertThat(pool.min, Equals(0))

    def test__make_database_pool_accepts_max_threads_setting(self):
        maxthreads = random.randint(1, 1000)
        pool = threads.make_database_pool(maxthreads)
        self.assertThat(pool.max, Equals(maxthreads))
        self.assertThat(pool.min, Equals(0))

    def test__make_database_unpool_creates_unpool(self):
        pool = threads.make_database_unpool()
        self.assertThat(pool, IsInstance(ThreadUnpool))
        self.assertThat(pool.contextFactory, Is(orm.ExclusivelyConnected))
        self.assertThat(pool.lock, IsInstance(DeferredSemaphore))
        self.assertThat(
            pool.lock.limit, Equals(threads.max_threads_for_database_pool)
        )

    def test__make_database_unpool_accepts_max_threads_setting(self):
        maxthreads = random.randint(1, 1000)
        pool = threads.make_database_unpool(maxthreads)
        self.assertThat(pool.lock.limit, Equals(maxthreads))


class TestInstallFunctions(MAASTestCase):
    """Tests for the `install_*` functions."""

    def test__install_default_pool_will_not_work_now(self):
        error = self.assertRaises(AssertionError, threads.install_default_pool)
        self.assertDocTestMatches("Too late; ...", str(error))

    def test__default_pool_is_disconnected_pool(self):
        pool = reactor.threadpool
        self.assertThat(pool, IsInstance(ThreadPool))
        self.assertThat(
            pool.context.contextFactory, Is(orm.TotallyDisconnected)
        )
        self.assertThat(pool.min, Equals(0))

    def test__install_database_pool_will_not_work_now(self):
        error = self.assertRaises(
            AssertionError, threads.install_database_pool
        )
        self.assertDocTestMatches("Too late; ...", str(error))

    def test__database_pool_is_connected_unpool(self):
        pool = reactor.threadpoolForDatabase
        self.assertThat(pool, IsInstance(ThreadUnpool))
        self.assertThat(pool.contextFactory, Is(orm.ExclusivelyConnected))


class TestDeferToDatabase(MAASServerTestCase):
    @wait_for_reactor
    @inlineCallbacks
    def test__defers_to_database_threadpool(self):
        @orm.transactional
        def call_in_database_thread(a, b):
            orm.validate_in_transaction(connection)
            return sentinel.called, a, b

        result = yield threads.deferToDatabase(
            call_in_database_thread, sentinel.a, b=sentinel.b
        )
        self.assertThat(
            result, Equals((sentinel.called, sentinel.a, sentinel.b))
        )


class TestCallOutToDatabase(MAASServerTestCase):
    @wait_for_reactor
    @inlineCallbacks
    def test__calls_out_to_database_threadpool(self):
        @orm.transactional
        def call_in_database_thread(a, b):
            orm.validate_in_transaction(connection)
            return sentinel.bar, a, b

        result = yield threads.callOutToDatabase(
            sentinel.foo, call_in_database_thread, sentinel.a, b=sentinel.b
        )
        self.assertThat(result, Is(sentinel.foo))
