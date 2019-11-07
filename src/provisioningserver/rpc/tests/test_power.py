# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for :py:module:`~provisioningserver.rpc.power`."""

__all__ = []

import logging
import random
from unittest.mock import ANY, call, MagicMock, sentinel

from fixtures import FakeLogger
from maastesting.factory import factory
from maastesting.matchers import (
    MockCalledOnceWith,
    MockCalledWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
from maastesting.twisted import (
    always_fail_with,
    always_succeed_with,
    extract_result,
    TwistedLoggerFixture,
)
from provisioningserver.drivers.power import (
    DEFAULT_WAITING_POLICY,
    get_error_message as get_driver_error_message,
    PowerError,
)
from provisioningserver.drivers.power.registry import PowerDriverRegistry
from provisioningserver.events import EVENT_TYPES
from provisioningserver.rpc import exceptions, power, region
from provisioningserver.rpc.testing import (
    MockClusterToRegionRPCFixture,
    MockLiveClusterToRegionRPCFixture,
)
from provisioningserver.testing.events import EventTypesAllRegistered
from testtools import ExpectedException
from testtools.deferredruntest import assert_fails_with
from testtools.matchers import Equals, IsInstance, Not
from twisted.internet import reactor
from twisted.internet.defer import (
    Deferred,
    fail,
    inlineCallbacks,
    maybeDeferred,
    returnValue,
    succeed,
)
from twisted.internet.task import Clock
from twisted.python.failure import Failure


def suppress_reporting(test):
    # Skip telling the region; just pass-through the query result.
    report_power_state = test.patch(power, "report_power_state")
    report_power_state.side_effect = lambda d, system_id, hostname: d


class TestPowerHelpers(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def setUp(self):
        super(TestPowerHelpers, self).setUp()
        self.useFixture(EventTypesAllRegistered())

    def patch_rpc_methods(self):
        fixture = self.useFixture(MockClusterToRegionRPCFixture())
        protocol, io = fixture.makeEventLoop(
            region.MarkNodeFailed,
            region.UpdateNodePowerState,
            region.SendEvent,
        )
        return protocol, io

    def test_power_state_update_calls_UpdateNodePowerState(self):
        system_id = factory.make_name("system_id")
        state = random.choice(["on", "off"])
        protocol, io = self.patch_rpc_methods()
        d = power.power_state_update(system_id, state)
        # This blocks until the deferred is complete
        io.flush()
        self.expectThat(extract_result(d), Equals({}))
        self.assertThat(
            protocol.UpdateNodePowerState,
            MockCalledOnceWith(ANY, system_id=system_id, power_state=state),
        )

    def test_power_change_success_emits_event(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_change = "on"
        protocol, io = self.patch_rpc_methods()
        d = power.power_change_success(system_id, hostname, power_change)
        io.flush()
        self.assertThat(
            protocol.UpdateNodePowerState,
            MockCalledOnceWith(
                ANY, system_id=system_id, power_state=power_change
            ),
        )
        self.assertThat(
            protocol.SendEvent,
            MockCalledOnceWith(
                ANY,
                type_name=EVENT_TYPES.NODE_POWERED_ON,
                system_id=system_id,
                description="",
            ),
        )
        self.assertIsNone(extract_result(d))

    def test_power_change_starting_emits_event(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_change = "on"
        protocol, io = self.patch_rpc_methods()
        d = power.power_change_starting(system_id, hostname, power_change)
        io.flush()
        self.assertThat(
            protocol.SendEvent,
            MockCalledOnceWith(
                ANY,
                type_name=EVENT_TYPES.NODE_POWER_ON_STARTING,
                system_id=system_id,
                description="",
            ),
        )
        self.assertIsNone(extract_result(d))

    def test_power_change_failure_emits_event(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        message = factory.make_name("message")
        power_change = "on"
        protocol, io = self.patch_rpc_methods()
        d = power.power_change_failure(
            system_id, hostname, power_change, message
        )
        io.flush()
        self.assertThat(
            protocol.SendEvent,
            MockCalledOnceWith(
                ANY,
                type_name=EVENT_TYPES.NODE_POWER_ON_FAILED,
                system_id=system_id,
                description=message,
            ),
        )
        self.assertIsNone(extract_result(d))


class TestChangePowerState(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def setUp(self):
        super(TestChangePowerState, self).setUp()
        self.useFixture(EventTypesAllRegistered())

    @inlineCallbacks
    def patch_rpc_methods(self, return_value={}, side_effect=None):
        fixture = self.useFixture(MockLiveClusterToRegionRPCFixture())
        protocol, connecting = fixture.makeEventLoop(
            region.MarkNodeFailed,
            region.UpdateNodePowerState,
            region.SendEvent,
        )
        protocol.MarkNodeFailed.return_value = return_value
        protocol.MarkNodeFailed.side_effect = side_effect
        self.addCleanup((yield connecting))
        returnValue(protocol.MarkNodeFailed)

    def test_change_power_state_calls_power_change_starting_early_on(self):
        # The first, or one of the first, things that change_power_state()
        # does is write to the node event log via power_change_starting().

        class ArbitraryException(Exception):
            """This allows us to return early from a function."""

        # Raise this exception when power_change_starting() is called, to
        # return early from change_power_state(). This lets us avoid set-up
        # for parts of the function that we're presently not interested in.
        pcs = self.patch_autospec(power, "power_change_starting")
        pcs.return_value = fail(ArbitraryException())

        d = power.change_power_state(
            sentinel.system_id,
            sentinel.hostname,
            sentinel.power_type,
            sentinel.power_change,
            sentinel.context,
        )
        self.assertRaises(ArbitraryException, extract_result, d)
        self.assertThat(
            power.power_change_starting,
            MockCalledOnceWith(
                sentinel.system_id, sentinel.hostname, sentinel.power_change
            ),
        )

    @inlineCallbacks
    def test___handles_power_driver_power_types(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = random.choice(["on", "off"])
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }
        self.patch(power, "is_driver_available").return_value = True
        perform_power_driver_change = self.patch_autospec(
            power, "perform_power_driver_change"
        )
        perform_power_driver_query = self.patch_autospec(
            power, "perform_power_driver_query"
        )
        perform_power_driver_query.return_value = succeed(power_change)
        power_change_success = self.patch_autospec(
            power, "power_change_success"
        )
        yield self.patch_rpc_methods()

        yield power.change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )

        self.expectThat(
            perform_power_driver_change,
            MockCalledOnceWith(
                system_id, hostname, power_driver.name, power_change, context
            ),
        )
        self.expectThat(
            perform_power_driver_query,
            MockCalledOnceWith(
                system_id, hostname, power_driver.name, context
            ),
        )
        self.expectThat(
            power_change_success,
            MockCalledOnceWith(system_id, hostname, power_change),
        )

    @inlineCallbacks
    def test__return_none_when_unqueryable(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [
                driver
                for _, driver in PowerDriverRegistry
                if not driver.queryable
            ]
        )
        power_change = "on"
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }
        self.patch(power, "is_driver_available").return_value = True
        get_item = self.patch(PowerDriverRegistry, "get_item")
        get_item.return_value = MagicMock()
        get_item.return_value.queryable = False
        perform_power_driver_query = self.patch(
            power, "perform_power_driver_query"
        )
        perform_power_driver_query.return_value = succeed(power_change)
        self.patch(power, "power_change_success")
        yield self.patch_rpc_methods()

        result = yield power.change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )

        self.expectThat(get_item, MockCalledWith(power_driver.name))
        self.expectThat(perform_power_driver_query, MockNotCalled())
        self.expectThat(power.power_change_success, MockNotCalled())
        self.expectThat(result, Equals(None))

    @inlineCallbacks
    def test__calls_power_driver_on_for_power_driver(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = "on"
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }
        self.patch(power, "is_driver_available").return_value = True
        get_item = self.patch(PowerDriverRegistry, "get_item")
        perform_power_driver_query = self.patch(
            power, "perform_power_driver_query"
        )
        perform_power_driver_query.return_value = succeed(power_change)
        self.patch(power, "power_change_success")
        yield self.patch_rpc_methods()

        result = yield power.change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )

        self.expectThat(get_item, MockCalledWith(power_driver.name))
        self.expectThat(
            perform_power_driver_query,
            MockCalledOnceWith(
                system_id, hostname, power_driver.name, context
            ),
        )
        self.expectThat(
            power.power_change_success,
            MockCalledOnceWith(system_id, hostname, power_change),
        )
        self.expectThat(result, Equals("on"))

    @inlineCallbacks
    def test__calls_power_driver_off_for_power_driver(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = "off"
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }
        self.patch(power, "is_driver_available").return_value = True
        get_item = self.patch(PowerDriverRegistry, "get_item")
        perform_power_driver_query = self.patch(
            power, "perform_power_driver_query"
        )
        perform_power_driver_query.return_value = succeed(power_change)
        self.patch(power, "power_change_success")
        yield self.patch_rpc_methods()

        result = yield power.change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )

        self.expectThat(get_item, MockCalledWith(power_driver.name))
        self.expectThat(
            perform_power_driver_query,
            MockCalledOnceWith(
                system_id, hostname, power_driver.name, context
            ),
        )
        self.expectThat(
            power.power_change_success,
            MockCalledOnceWith(system_id, hostname, power_change),
        )
        self.expectThat(result, Equals("off"))

    @inlineCallbacks
    def test__calls_power_driver_cycle_for_power_driver(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = "cycle"
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }
        self.patch(power, "is_driver_available").return_value = True
        get_item = self.patch(PowerDriverRegistry, "get_item")
        perform_power_driver_query = self.patch(
            power, "perform_power_driver_query"
        )
        perform_power_driver_query.return_value = succeed("on")
        self.patch(power, "power_change_success")
        yield self.patch_rpc_methods()

        result = yield power.change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )

        self.expectThat(get_item, MockCalledWith(power_driver.name))
        self.expectThat(
            perform_power_driver_query,
            MockCalledOnceWith(
                system_id, hostname, power_driver.name, context
            ),
        )
        self.expectThat(
            power.power_change_success,
            MockCalledOnceWith(system_id, hostname, "on"),
        )
        self.expectThat(result, Equals("on"))

    @inlineCallbacks
    def test__marks_the_node_broken_if_exception_for_power_driver(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = "on"
        context = {
            factory.make_name("context-key"): factory.make_name("context-val"),
            "system_id": system_id,
        }
        self.patch(power, "is_driver_available").return_value = True
        exception = PowerError(factory.make_string())
        get_item = self.patch(PowerDriverRegistry, "get_item")
        power_driver = get_item.return_value
        power_driver.on.return_value = fail(exception)

        markNodeBroken = yield self.patch_rpc_methods()

        with ExpectedException(PowerError):
            yield power.change_power_state(
                system_id, hostname, power_driver.name, power_change, context
            )

        error_message = "Power on for the node failed: %s" % (
            get_driver_error_message(exception)
        )
        self.expectThat(
            markNodeBroken,
            MockCalledOnceWith(
                ANY, system_id=system_id, error_description=error_message
            ),
        )


class TestMaybeChangePowerState(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def setUp(self):
        super(TestMaybeChangePowerState, self).setUp()
        self.patch(power, "power_action_registry", {})
        for _, power_driver in PowerDriverRegistry:
            self.patch(
                power_driver, "detect_missing_packages"
            ).return_value = []
        self.useFixture(EventTypesAllRegistered())
        # Defer later won't run during the test so replace it with a
        # maybeDeferred.
        self.patch(
            power, "deferLater"
        ).side_effect = lambda clock, delay, func, *args, **kwargs: maybeDeferred(
            func, *args, **kwargs
        )

    def patch_methods_using_rpc(self):
        pcs = self.patch_autospec(power, "power_change_starting")
        pcs.return_value = always_succeed_with(None)
        cps = self.patch_autospec(power, "change_power_state")
        cps.return_value = always_succeed_with(None)

    def test_always_returns_deferred(self):
        clock = Clock()
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        d = power.maybe_change_power_state(
            sentinel.system_id,
            sentinel.hostname,
            power_driver.name,
            random.choice(("on", "off")),
            sentinel.context,
            clock=clock,
        )
        self.assertThat(d, IsInstance(Deferred))

    @inlineCallbacks
    def test_adds_action_to_registry(self):
        self.patch_methods_using_rpc()

        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = random.choice(["on", "off", "cycle"])
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }

        d = power.maybe_change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )
        # XXX - maybe_change_power_state is resolving before the yield. This
        # causes power.power_action_registry to be reset before it can be
        # checked.
        # self.assertEqual(
        #    {system_id: (power_change, ANY)},
        #    power.power_action_registry)
        yield d
        self.assertEqual({}, power.power_action_registry)

    @inlineCallbacks
    def test_checks_missing_packages(self):
        self.patch_methods_using_rpc()

        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = random.choice(["on", "off", "cycle"])
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }
        yield power.maybe_change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )
        self.assertThat(
            power_driver.detect_missing_packages, MockCalledOnceWith()
        )

    @inlineCallbacks
    def test_errors_when_missing_packages(self):
        self.patch_methods_using_rpc()

        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = random.choice(["on", "off", "cycle"])
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }
        power_driver.detect_missing_packages.return_value = ["gone"]
        with ExpectedException(exceptions.PowerActionFail):
            yield power.maybe_change_power_state(
                system_id, hostname, power_driver.name, power_change, context
            )
        self.assertThat(
            power_driver.detect_missing_packages, MockCalledOnceWith()
        )

    @inlineCallbacks
    def test_errors_when_change_conflicts_with_in_progress_change(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_changes = ["on", "off"]
        random.shuffle(power_changes)
        current_power_change, power_change = power_changes
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }
        power.power_action_registry[system_id] = (
            current_power_change,
            sentinel.d,
        )
        with ExpectedException(exceptions.PowerActionAlreadyInProgress):
            yield power.maybe_change_power_state(
                system_id, hostname, power_driver.name, power_change, context
            )

    @inlineCallbacks
    def test_does_nothing_when_change_matches_in_progress_change(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        current_power_change = power_change = random.choice(
            ["on", "off", "cycle"]
        )
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }
        power.power_action_registry[system_id] = (
            current_power_change,
            sentinel.d,
        )
        yield power.maybe_change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )
        self.assertThat(
            power.power_action_registry,
            Equals({system_id: (power_change, sentinel.d)}),
        )

    @inlineCallbacks
    def test_calls_change_power_state_later(self):
        self.patch_methods_using_rpc()

        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = random.choice(["on", "off", "cycle"])
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }

        yield power.maybe_change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )
        self.assertThat(
            power.change_power_state,
            MockCalledOnceWith(
                system_id,
                hostname,
                power_driver.name,
                power_change,
                context,
                power.reactor,
            ),
        )

    @inlineCallbacks
    def test_clears_lock_if_change_power_state_success(self):
        self.patch_methods_using_rpc()

        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = random.choice(["on", "off", "cycle"])
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }

        yield power.maybe_change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )
        self.assertNotIn(system_id, power.power_action_registry)

    @inlineCallbacks
    def test_clears_lock_if_change_power_state_fails(self):
        class TestException(Exception):
            pass

        pcs = self.patch_autospec(power, "power_change_starting")
        pcs.return_value = fail(TestException("boom"))

        system_id = factory.make_name("system_id")
        hostname = factory.make_hostname()
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = random.choice(["on", "off", "cycle"])
        context = sentinel.context

        logger = self.useFixture(TwistedLoggerFixture())

        yield power.maybe_change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )
        self.assertNotIn(system_id, power.power_action_registry)
        self.assertDocTestMatches(
            """\
            %s: Power %s failed.
            Traceback (most recent call last):
            ...
            %s.TestException: boom
            """
            % (hostname, power_change, __name__),
            logger.dump(),
        )

    @inlineCallbacks
    def test_clears_lock_if_change_power_state_is_cancelled(self):
        # Patch in an unfired Deferred here. This will pause the call so that
        # we can grab the delayed call from the registry in time to cancel it.
        self.patch_autospec(power, "change_power_state")
        power.change_power_state.return_value = Deferred()
        self.patch_autospec(power, "power_change_failure")

        system_id = factory.make_name("system_id")
        hostname = factory.make_hostname()
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = random.choice(["on", "off", "cycle"])
        context = sentinel.context

        logger = self.useFixture(TwistedLoggerFixture())

        ret = yield power.maybe_change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )

        # Get the Deferred from the registry and cancel it.
        _, d = power.power_action_registry[system_id]
        d.cancel()
        yield d

        # LP: #1761600 - If the deferLater value is returned the RPC call waits
        # for the power change to complete. This holds up the UI. Make sure
        # nothing is returned.
        self.assertIsNone(ret)
        self.assertNotIn(system_id, power.power_action_registry)
        self.assertDocTestMatches(
            """\
            %s: Power could not be set to %s; timed out.
            """
            % (hostname, power_change),
            logger.dump(),
        )
        self.assertThat(
            power.power_change_failure,
            MockCalledOnceWith(system_id, hostname, power_change, "Timed out"),
        )

    @inlineCallbacks
    def test__calls_change_power_state_with_timeout(self):
        self.patch_methods_using_rpc()
        defer_with_timeout = self.patch(power, "deferWithTimeout")

        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_change = random.choice(["on", "off", "cycle"])
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }

        yield power.maybe_change_power_state(
            system_id, hostname, power_driver.name, power_change, context
        )
        self.assertThat(
            defer_with_timeout,
            MockCalledOnceWith(
                power.CHANGE_POWER_STATE_TIMEOUT,
                power.change_power_state,
                system_id,
                hostname,
                power_driver.name,
                power_change,
                context,
                power.reactor,
            ),
        )


class TestPowerQuery(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def setUp(self):
        super(TestPowerQuery, self).setUp()
        self.useFixture(EventTypesAllRegistered())
        self.patch(power, "deferToThread", maybeDeferred)
        for _, power_driver in PowerDriverRegistry:
            self.patch(
                power_driver, "detect_missing_packages"
            ).return_value = []

    def patch_rpc_methods(self, return_value={}, side_effect=None):
        fixture = self.useFixture(MockClusterToRegionRPCFixture())
        protocol, io = fixture.makeEventLoop(
            region.MarkNodeFailed,
            region.SendEvent,
            region.UpdateNodePowerState,
        )
        protocol.MarkNodeFailed.return_value = return_value
        protocol.MarkNodeFailed.side_effect = side_effect
        return protocol.SendEvent, protocol.MarkNodeFailed, io

    def test_power_query_failure_emits_event(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        message = factory.make_name("message")
        SendEvent, _, io = self.patch_rpc_methods()
        d = power.power_query_failure(
            system_id, hostname, Failure(Exception(message))
        )
        # This blocks until the deferred is complete.
        io.flush()
        self.assertIsNone(extract_result(d))
        self.assertThat(
            SendEvent,
            MockCalledOnceWith(
                ANY,
                type_name=EVENT_TYPES.NODE_POWER_QUERY_FAILED,
                system_id=system_id,
                description=message,
            ),
        )

    def test_power_query_success_emits_event(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        state = factory.make_name("state")
        message = "Power state queried: %s" % state
        SendEvent, _, io = self.patch_rpc_methods()
        d = power.power_query_success(system_id, hostname, state)
        # This blocks until the deferred is complete.
        io.flush()
        self.assertIsNone(extract_result(d))
        self.assertThat(
            SendEvent,
            MockCalledOnceWith(
                ANY,
                type_name=EVENT_TYPES.NODE_POWER_QUERIED_DEBUG,
                system_id=system_id,
                description=message,
            ),
        )

    def test_get_power_state_queries_node(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        power_state = random.choice(["on", "off"])
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }
        self.patch(power, "is_driver_available").return_value = True
        _, markNodeBroken, io = self.patch_rpc_methods()
        mock_perform_power_driver_query = self.patch(
            power, "perform_power_driver_query"
        )
        mock_perform_power_driver_query.return_value = power_state

        d = power.get_power_state(
            system_id, hostname, power_driver.name, context
        )
        # This blocks until the deferred is complete.
        io.flush()
        self.assertEqual(power_state, extract_result(d))
        self.assertThat(
            power_driver.detect_missing_packages, MockCalledOnceWith()
        )
        self.assertThat(
            mock_perform_power_driver_query,
            MockCallsMatch(
                call(system_id, hostname, power_driver.name, context)
            ),
        )

    def test_get_power_state_fails_for_missing_packages(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        context = {
            factory.make_name("context-key"): factory.make_name("context-val")
        }
        self.patch(power, "is_driver_available").return_value = False
        _, markNodeBroken, io = self.patch_rpc_methods()

        power_driver.detect_missing_packages.return_value = ["gone"]

        d = power.get_power_state(
            system_id, hostname, power_driver.name, context
        )
        # This blocks until the deferred is complete.
        io.flush()

        self.assertThat(
            power_driver.detect_missing_packages, MockCalledOnceWith()
        )
        return assert_fails_with(d, exceptions.PowerActionFail)

    def test_report_power_state_changes_power_state_if_failure(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        err_msg = factory.make_name("error")

        _, _, io = self.patch_rpc_methods()
        self.patch_autospec(power, "power_state_update")

        # Simulate a failure when querying state.
        query = fail(exceptions.PowerActionFail(err_msg))
        report = power.report_power_state(query, system_id, hostname)
        # This blocks until the deferred is complete.
        io.flush()

        error = self.assertRaises(
            exceptions.PowerActionFail, extract_result, report
        )
        self.assertEqual(err_msg, str(error))
        self.assertThat(
            power.power_state_update, MockCalledOnceWith(system_id, "error")
        )

    def test_report_power_state_changes_power_state_if_success(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_state = random.choice(["on", "off"])

        _, _, io = self.patch_rpc_methods()
        self.patch_autospec(power, "power_state_update")

        # Simulate a success when querying state.
        query = succeed(power_state)
        report = power.report_power_state(query, system_id, hostname)
        # This blocks until the deferred is complete.
        io.flush()

        self.assertEqual(power_state, extract_result(report))
        self.assertThat(
            power.power_state_update,
            MockCalledOnceWith(system_id, power_state),
        )

    def test_report_power_state_changes_power_state_if_unknown(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_state = "unknown"

        _, _, io = self.patch_rpc_methods()
        self.patch_autospec(power, "power_state_update")

        # Simulate a success when querying state.
        query = succeed(power_state)
        report = power.report_power_state(query, system_id, hostname)
        # This blocks until the deferred is complete.
        io.flush()

        self.assertEqual(power_state, extract_result(report))
        self.assertThat(
            power.power_state_update,
            MockCalledOnceWith(system_id, power_state),
        )


class TestPowerQueryExceptions(MAASTestCase):

    scenarios = tuple(
        (
            driver.name,
            {
                "power_type": driver.name,
                "power_driver": driver,
                "func": (  # Function to invoke power driver.
                    "perform_power_driver_query"
                ),
                "waits": (  # Pauses between retries.
                    []
                    if driver.name in PowerDriverRegistry
                    else DEFAULT_WAITING_POLICY
                ),
                "calls": (  # No. of calls to the driver.
                    1
                    if driver.name in PowerDriverRegistry
                    else len(DEFAULT_WAITING_POLICY)
                ),
            },
        )
        for _, driver in PowerDriverRegistry
        if driver.queryable
    )

    def test_report_power_state_reports_all_exceptions(self):
        logger_twisted = self.useFixture(TwistedLoggerFixture())
        logger_maaslog = self.useFixture(FakeLogger("maas"))

        # Avoid threads here.
        self.patch(power, "deferToThread", maybeDeferred)

        exception_type = factory.make_exception_type()
        exception_message = factory.make_string()
        exception = exception_type(exception_message)

        # Pretend the query always fails with `exception`.
        query = self.patch_autospec(power, self.func)
        query.side_effect = always_fail_with(exception)

        # Intercept calls to power_state_update() and send_node_event().
        power_state_update = self.patch_autospec(power, "power_state_update")
        power_state_update.return_value = succeed(None)
        send_node_event = self.patch_autospec(power, "send_node_event")
        send_node_event.return_value = succeed(None)

        self.patch(
            self.power_driver, "detect_missing_packages"
        ).return_value = []

        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        context = sentinel.context
        clock = Clock()

        d = power.get_power_state(
            system_id, hostname, self.power_type, context, clock
        )
        d = power.report_power_state(d, system_id, hostname)

        # Crank through some number of retries.
        for wait in self.waits:
            self.assertFalse(d.called)
            clock.advance(wait)
        self.assertTrue(d.called)

        # Finally the exception from the query is raised.
        self.assertRaises(exception_type, extract_result, d)

        # The broken power query function patched earlier was called the same
        # number of times as there are steps in the default waiting policy.
        expected_call = call(system_id, hostname, self.power_type, context)
        expected_calls = [expected_call] * self.calls
        self.assertThat(query, MockCallsMatch(*expected_calls))

        expected_message = "%s: Power state could not be queried: %s" % (
            hostname,
            exception_message,
        )

        # An attempt was made to report the failure to the region.
        self.assertThat(
            power_state_update, MockCalledOnceWith(system_id, "error")
        )
        # An attempt was made to log a node event with details.
        self.assertThat(
            send_node_event,
            MockCalledOnceWith(
                EVENT_TYPES.NODE_POWER_QUERY_FAILED,
                system_id,
                hostname,
                exception_message,
            ),
        )

        # Nothing was logged to the Twisted log.
        self.assertEqual("", logger_twisted.output)
        # A brief message is written to maaslog.
        self.assertEqual(expected_message + "\n", logger_maaslog.output)


class TestPowerQueryAsync(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def setUp(self):
        super(TestPowerQueryAsync, self).setUp()

    def make_node(self, power_type=None):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        if power_type is None:
            power_type = random.choice(
                [
                    driver.name
                    for _, driver in PowerDriverRegistry
                    if driver.queryable
                ]
            )
        state = random.choice(["on", "off", "unknown", "error"])
        context = {
            factory.make_name("context-key"): (
                factory.make_name("context-val")
            )
        }
        return {
            "context": context,
            "hostname": hostname,
            "power_state": state,
            "power_type": power_type,
            "system_id": system_id,
        }

    def make_nodes(self, count=3):
        nodes = [self.make_node() for _ in range(count)]
        # Sanity check that these nodes are something that can emerge
        # from a call to ListNodePowerParameters.
        region.ListNodePowerParameters.makeResponse({"nodes": nodes}, None)
        return nodes

    def pick_alternate_state(self, state):
        return random.choice(
            [
                value
                for value in ["on", "off", "unknown", "error"]
                if value != state
            ]
        )

    @inlineCallbacks
    def test_query_all_nodes_gets_and_reports_power_state(self):
        nodes = self.make_nodes()

        # Report back that all nodes' power states are as recorded.
        power_states = [node["power_state"] for node in nodes]
        queries = list(map(succeed, power_states))
        get_power_state = self.patch(power, "get_power_state")
        get_power_state.side_effect = queries
        report_power_state = self.patch(power, "report_power_state")
        report_power_state.side_effect = lambda d, sid, hn: d

        yield power.query_all_nodes(nodes)
        self.assertThat(
            get_power_state,
            MockCallsMatch(
                *(
                    call(
                        node["system_id"],
                        node["hostname"],
                        node["power_type"],
                        node["context"],
                        clock=reactor,
                    )
                    for node in nodes
                )
            ),
        )
        self.assertThat(
            report_power_state,
            MockCallsMatch(
                *(
                    call(query, node["system_id"], node["hostname"])
                    for query, node in zip(queries, nodes)
                )
            ),
        )

    @inlineCallbacks
    def test_query_all_nodes_skips_nodes_in_action_registry(self):
        nodes = self.make_nodes()

        # First node is in the registry.
        power.power_action_registry[nodes[0]["system_id"]] = sentinel.action

        # Report back power state of nodes' not in registry.
        power_states = [node["power_state"] for node in nodes[1:]]
        get_power_state = self.patch(power, "get_power_state")
        get_power_state.side_effect = map(succeed, power_states)
        suppress_reporting(self)

        yield power.query_all_nodes(nodes)
        self.assertThat(
            get_power_state,
            MockCallsMatch(
                *(
                    call(
                        node["system_id"],
                        node["hostname"],
                        node["power_type"],
                        node["context"],
                        clock=reactor,
                    )
                    for node in nodes[1:]
                )
            ),
        )
        self.assertThat(
            get_power_state,
            Not(
                MockCalledWith(
                    nodes[0]["system_id"],
                    nodes[0]["hostname"],
                    nodes[0]["power_type"],
                    nodes[0]["context"],
                    clock=reactor,
                )
            ),
        )

    @inlineCallbacks
    def test_query_all_nodes_only_queries_queryable_power_types(self):
        nodes = self.make_nodes()
        # nodes are all queryable, so add one that isn't:
        nodes.append(self.make_node(power_type="manual"))

        # Report back that all nodes' power states are as recorded.
        power_states = [node["power_state"] for node in nodes]
        get_power_state = self.patch(power, "get_power_state")
        get_power_state.side_effect = map(succeed, power_states)
        suppress_reporting(self)

        yield power.query_all_nodes(nodes)
        self.assertThat(
            get_power_state,
            MockCallsMatch(
                *(
                    call(
                        node["system_id"],
                        node["hostname"],
                        node["power_type"],
                        node["context"],
                        clock=reactor,
                    )
                    for node in nodes
                    if node["power_type"] in PowerDriverRegistry
                )
            ),
        )

    @inlineCallbacks
    def test_query_all_nodes_swallows_PowerActionFail(self):
        node1, node2 = self.make_nodes(2)
        new_state_2 = self.pick_alternate_state(node2["power_state"])
        get_power_state = self.patch(power, "get_power_state")
        error_msg = factory.make_name("error")
        get_power_state.side_effect = [
            fail(exceptions.PowerActionFail(error_msg)),
            succeed(new_state_2),
        ]
        suppress_reporting(self)

        with FakeLogger("maas.power", level=logging.DEBUG) as maaslog:
            yield power.query_all_nodes([node1, node2])

        self.assertDocTestMatches(
            """\
            hostname-...: Could not query power state: %s.
            hostname-...: Power state has changed from ... to ...
            """
            % error_msg,
            maaslog.output,
        )

    @inlineCallbacks
    def test_query_all_nodes_swallows_PowerError(self):
        node1, node2 = self.make_nodes(2)
        new_state_2 = self.pick_alternate_state(node2["power_state"])
        get_power_state = self.patch(power, "get_power_state")
        error_msg = factory.make_name("error")
        get_power_state.side_effect = [
            fail(PowerError(error_msg)),
            succeed(new_state_2),
        ]
        suppress_reporting(self)

        with FakeLogger("maas.power", level=logging.DEBUG) as maaslog:
            yield power.query_all_nodes([node1, node2])

        self.assertDocTestMatches(
            """\
            %s: Could not query power state: %s.
            %s: Power state has changed from %s to %s.
            """
            % (
                node1["hostname"],
                error_msg,
                node2["hostname"],
                node2["power_state"],
                new_state_2,
            ),
            maaslog.output,
        )

    @inlineCallbacks
    def test_query_all_nodes_swallows_NoSuchNode(self):
        node1, node2 = self.make_nodes(2)
        new_state_2 = self.pick_alternate_state(node2["power_state"])
        get_power_state = self.patch(power, "get_power_state")
        get_power_state.side_effect = [
            fail(exceptions.NoSuchNode()),
            succeed(new_state_2),
        ]
        suppress_reporting(self)

        with FakeLogger("maas.power", level=logging.DEBUG) as maaslog:
            yield power.query_all_nodes([node1, node2])

        self.assertDocTestMatches(
            """\
            hostname-...: Power state has changed from ... to ...
            """,
            maaslog.output,
        )

    @inlineCallbacks
    def test_query_all_nodes_swallows_Exception(self):
        node1, node2 = self.make_nodes(2)
        error_message = factory.make_name("error")
        error_type = factory.make_exception_type()
        new_state_2 = self.pick_alternate_state(node2["power_state"])
        get_power_state = self.patch(power, "get_power_state")
        get_power_state.side_effect = [
            fail(error_type(error_message)),
            succeed(new_state_2),
        ]
        suppress_reporting(self)

        maaslog = FakeLogger("maas.power", level=logging.DEBUG)
        twistlog = TwistedLoggerFixture()

        with maaslog, twistlog:
            yield power.query_all_nodes([node1, node2])

        self.assertDocTestMatches(
            """\
            hostname-...: Failed to refresh power state: %s
            hostname-...: Power state has changed from ... to ...
            """
            % error_message,
            maaslog.output,
        )

    @inlineCallbacks
    def test_query_all_nodes_returns_deferredlist_of_number_of_nodes(self):
        node1, node2 = self.make_nodes(2)
        get_power_state = self.patch(power, "get_power_state")
        get_power_state.side_effect = [
            succeed(node1["power_state"]),
            succeed(node2["power_state"]),
        ]
        suppress_reporting(self)

        results = yield power.query_all_nodes([node1, node2])
        self.assertEqual(
            [(True, node1["power_state"]), (True, node2["power_state"])],
            results,
        )
