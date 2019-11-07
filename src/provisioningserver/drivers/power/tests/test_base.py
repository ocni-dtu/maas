# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `provisioningserver.drivers.power`."""

__all__ = []

import random
from unittest.mock import call, sentinel

from jsonschema import validate
from maastesting.factory import factory
from maastesting.matchers import (
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.runtest import MAASTwistedRunTest
from maastesting.testcase import MAASTestCase
from provisioningserver.drivers import make_setting_field, power
from provisioningserver.drivers.power import (
    get_error_message,
    JSON_POWER_DRIVER_SCHEMA,
    PowerActionError,
    PowerAuthError,
    PowerConnError,
    PowerDriver,
    PowerDriverBase,
    PowerError,
    PowerFatalError,
    PowerSettingError,
    PowerToolError,
)
from provisioningserver.utils.twisted import asynchronous
from testtools.matchers import Equals
from testtools.testcase import ExpectedException
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, succeed


class FakePowerDriverBase(PowerDriverBase):

    name = ""
    chassis = False
    description = ""
    settings = []
    ip_extractor = None
    queryable = True

    def __init__(self, name, description, settings):
        self.name = name
        self.description = description
        self.settings = settings
        super(FakePowerDriverBase, self).__init__()

    def on(self, system_id, context):
        raise NotImplementedError

    def off(self, system_id, context):
        raise NotImplementedError

    def cycle(self, system_id, context):
        raise NotImplementedError

    def query(self, system_id, context):
        raise NotImplementedError

    def detect_missing_packages(self):
        return []


def make_power_driver_base(name=None, description=None, settings=None):
    if name is None:
        name = factory.make_name("diskless")
    if description is None:
        description = factory.make_name("description")
    if settings is None:
        settings = []
    return FakePowerDriverBase(name, description, settings)


class TestFakePowerDriverBase(MAASTestCase):
    def test_attributes(self):
        fake_name = factory.make_name("name")
        fake_description = factory.make_name("description")
        fake_setting = factory.make_name("setting")
        fake_settings = [
            make_setting_field(fake_setting, fake_setting.title())
        ]
        attributes = {
            "name": fake_name,
            "description": fake_description,
            "settings": fake_settings,
        }
        fake_driver = FakePowerDriverBase(
            fake_name, fake_description, fake_settings
        )
        self.assertAttributes(fake_driver, attributes)

    def test_make_power_driver_base(self):
        fake_name = factory.make_name("name")
        fake_description = factory.make_name("description")
        fake_setting = factory.make_name("setting")
        fake_settings = [
            make_setting_field(fake_setting, fake_setting.title())
        ]
        attributes = {
            "name": fake_name,
            "description": fake_description,
            "settings": fake_settings,
        }
        fake_driver = make_power_driver_base(
            name=fake_name,
            description=fake_description,
            settings=fake_settings,
        )
        self.assertAttributes(fake_driver, attributes)

    def test_make_power_driver_base_makes_name_and_description(self):
        fake_driver = make_power_driver_base()
        self.assertNotEqual("", fake_driver.name)
        self.assertNotEqual("", fake_driver.description)

    def test_on_raises_not_implemented(self):
        fake_driver = make_power_driver_base()
        self.assertRaises(
            NotImplementedError,
            fake_driver.on,
            sentinel.system_id,
            sentinel.context,
        )

    def test_off_raises_not_implemented(self):
        fake_driver = make_power_driver_base()
        self.assertRaises(
            NotImplementedError,
            fake_driver.off,
            sentinel.system_id,
            sentinel.context,
        )

    def test_cycle_raises_not_implemented(self):
        fake_driver = make_power_driver_base()
        self.assertRaises(
            NotImplementedError,
            fake_driver.cycle,
            sentinel.system_id,
            sentinel.context,
        )

    def test_query_raises_not_implemented(self):
        fake_driver = make_power_driver_base()
        self.assertRaises(
            NotImplementedError,
            fake_driver.query,
            sentinel.system_id,
            sentinel.context,
        )


class TestPowerDriverBase(MAASTestCase):
    def test_get_schema(self):
        fake_name = factory.make_name("name")
        fake_description = factory.make_name("description")
        fake_setting = factory.make_name("setting")
        fake_settings = [
            make_setting_field(fake_setting, fake_setting.title())
        ]
        fake_driver = make_power_driver_base(
            name=fake_name,
            description=fake_description,
            settings=fake_settings,
        )
        self.assertEqual(
            {
                "driver_type": "power",
                "name": fake_name,
                "description": fake_description,
                "fields": fake_settings,
                "queryable": fake_driver.queryable,
                "missing_packages": fake_driver.detect_missing_packages(),
            },
            fake_driver.get_schema(),
        )

    def test_get_schema_returns_valid_schema(self):
        fake_driver = make_power_driver_base()
        #: doesn't raise ValidationError
        validate(fake_driver.get_schema(), JSON_POWER_DRIVER_SCHEMA)


class TestGetErrorMessage(MAASTestCase):

    scenarios = [
        (
            "auth",
            dict(
                exception=PowerAuthError("auth"),
                message="Could not authenticate to node's BMC: auth",
            ),
        ),
        (
            "conn",
            dict(
                exception=PowerConnError("conn"),
                message="Could not contact node's BMC: conn",
            ),
        ),
        (
            "setting",
            dict(
                exception=PowerSettingError("setting"),
                message="Missing or invalid power setting: setting",
            ),
        ),
        (
            "tool",
            dict(
                exception=PowerToolError("tool"),
                message="Missing power tool: tool",
            ),
        ),
        (
            "action",
            dict(
                exception=PowerActionError("action"),
                message="Failed to complete power action: action",
            ),
        ),
        (
            "unknown",
            dict(
                exception=PowerError("unknown error"),
                message="Failed talking to node's BMC: unknown error",
            ),
        ),
    ]

    def test_return_msg(self):
        self.assertEqual(self.message, get_error_message(self.exception))


class FakePowerDriver(PowerDriver):

    name = ""
    chassis = False
    description = ""
    settings = []
    ip_extractor = None
    queryable = True

    def __init__(
        self, name, description, settings, wait_time=None, clock=reactor
    ):
        self.name = name
        self.description = description
        self.settings = settings
        if wait_time is not None:
            self.wait_time = wait_time
        super(FakePowerDriver, self).__init__(clock)

    def detect_missing_packages(self):
        return []

    def power_on(self, system_id, context):
        raise NotImplementedError

    def power_off(self, system_id, context):
        raise NotImplementedError

    def power_query(self, system_id, context):
        raise NotImplementedError


def make_power_driver(
    name=None, description=None, settings=None, wait_time=None, clock=reactor
):
    if name is None:
        name = factory.make_name("diskless")
    if description is None:
        description = factory.make_name("description")
    if settings is None:
        settings = []
    return FakePowerDriver(
        name, description, settings, wait_time=wait_time, clock=clock
    )


class AsyncFakePowerDriver(FakePowerDriver):
    def __init__(
        self,
        name,
        description,
        settings,
        wait_time=None,
        clock=reactor,
        query_result=None,
    ):
        super(AsyncFakePowerDriver, self).__init__(
            name, description, settings, wait_time=None, clock=reactor
        )
        self.power_query_result = query_result
        self.power_on_called = 0
        self.power_off_called = 0

    @asynchronous
    def power_on(self, system_id, context):
        self.power_on_called += 1
        return succeed(None)

    @asynchronous
    def power_off(self, system_id, context):
        self.power_off_called += 1
        return succeed(None)

    @asynchronous
    def power_query(self, system_id, context):
        return succeed(self.power_query_result)


def make_async_power_driver(
    name=None,
    description=None,
    settings=None,
    wait_time=None,
    clock=reactor,
    query_result=None,
):
    if name is None:
        name = factory.make_name("diskless")
    if description is None:
        description = factory.make_name("description")
    if settings is None:
        settings = []
    return AsyncFakePowerDriver(
        name,
        description,
        settings,
        wait_time=wait_time,
        clock=clock,
        query_result=query_result,
    )


class TestPowerDriverPowerAction(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    scenarios = [
        ("on", dict(action="on", action_func="power_on", bad_state="off")),
        ("off", dict(action="off", action_func="power_off", bad_state="on")),
    ]

    def make_error_message(self):
        error = factory.make_name("msg")
        self.patch(power, "get_error_message").return_value = error
        return error

    @inlineCallbacks
    def test_success(self):
        system_id = factory.make_name("system_id")
        context = {"context": factory.make_name("context")}
        driver = make_power_driver(wait_time=[0])
        self.patch(driver, self.action_func)
        self.patch(driver, "power_query").return_value = self.action
        method = getattr(driver, self.action)
        result = yield method(system_id, context)
        self.assertEqual(result, None)

    @inlineCallbacks
    def test_success_async(self):
        system_id = factory.make_name("system_id")
        context = {"context": factory.make_name("context")}
        mock_deferToThread = self.patch(power, "deferToThread")
        driver = make_async_power_driver(
            wait_time=[0], query_result=self.action
        )
        method = getattr(driver, self.action)
        result = yield method(system_id, context)
        self.assertEqual(result, None)
        call_count = getattr(driver, "%s_called" % self.action_func)
        self.assertEqual(1, call_count)
        self.assertThat(mock_deferToThread, MockNotCalled())

    @inlineCallbacks
    def test_handles_fatal_error_on_first_call(self):
        system_id = factory.make_name("system_id")
        context = {"context": factory.make_name("context")}
        driver = make_power_driver(wait_time=[0, 0])
        mock_on = self.patch(driver, self.action_func)
        mock_on.side_effect = [PowerFatalError(), None]
        mock_query = self.patch(driver, "power_query")
        mock_query.return_value = self.action
        method = getattr(driver, self.action)
        with ExpectedException(PowerFatalError):
            yield method(system_id, context)
        self.expectThat(mock_query, MockNotCalled())

    @inlineCallbacks
    def test_handles_non_fatal_error_on_first_call(self):
        system_id = factory.make_name("system_id")
        context = {"context": factory.make_name("context")}
        driver = make_power_driver(wait_time=[0, 0])
        mock_on = self.patch(driver, self.action_func)
        mock_on.side_effect = [PowerError(), None]
        mock_query = self.patch(driver, "power_query")
        mock_query.return_value = self.action
        method = getattr(driver, self.action)
        result = yield method(system_id, context)
        self.expectThat(mock_query, MockCalledOnceWith(system_id, context))
        self.expectThat(result, Equals(None))

    @inlineCallbacks
    def test_handles_non_fatal_error_and_holds_error(self):
        system_id = factory.make_name("system_id")
        context = {"context": factory.make_name("context")}
        driver = make_power_driver(wait_time=[0])
        error_msg = factory.make_name("error")
        self.patch(driver, self.action_func)
        mock_query = self.patch(driver, "power_query")
        mock_query.side_effect = PowerError(error_msg)
        method = getattr(driver, self.action)
        with ExpectedException(PowerError):
            yield method(system_id, context)
        self.expectThat(mock_query, MockCalledOnceWith(system_id, context))

    @inlineCallbacks
    def test_handles_non_fatal_error(self):
        system_id = factory.make_name("system_id")
        context = {"context": factory.make_name("context")}
        driver = make_power_driver(wait_time=[0])
        mock_on = self.patch(driver, self.action_func)
        mock_on.side_effect = PowerError()
        method = getattr(driver, self.action)
        with ExpectedException(PowerError):
            yield method(system_id, context)

    @inlineCallbacks
    def test_handles_fails_to_complete_power_action_in_time(self):
        system_id = factory.make_name("system_id")
        context = {"context": factory.make_name("context")}
        driver = make_power_driver(wait_time=[0])
        self.patch(driver, self.action_func)
        mock_query = self.patch(driver, "power_query")
        mock_query.return_value = self.bad_state
        method = getattr(driver, self.action)
        with ExpectedException(PowerError):
            yield method(system_id, context)

    @inlineCallbacks
    def test_doesnt_power_query_if_unqueryable(self):
        system_id = factory.make_name("system_id")
        context = {"context": factory.make_name("context")}
        driver = make_power_driver(wait_time=[0])
        driver.queryable = False
        self.patch(driver, self.action_func)
        mock_query = self.patch(driver, "power_query")
        method = getattr(driver, self.action)
        yield method(system_id, context)
        self.assertThat(mock_query, MockNotCalled())


class TestPowerDriverCycle(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    @inlineCallbacks
    def test_cycles_power_when_node_is_powered_on(self):
        system_id = factory.make_name("system_id")
        context = {"context": factory.make_name("context")}
        driver = make_power_driver()
        mock_perform_power = self.patch(driver, "perform_power")
        self.patch(driver, "power_query").return_value = "on"
        yield driver.cycle(system_id, context)
        self.assertThat(
            mock_perform_power,
            MockCallsMatch(
                call(driver.power_off, "off", system_id, context),
                call(driver.power_on, "on", system_id, context),
            ),
        )

    @inlineCallbacks
    def test_cycles_power_when_node_is_powered_off(self):
        system_id = factory.make_name("system_id")
        context = {"context": factory.make_name("context")}
        driver = make_power_driver()
        mock_perform_power = self.patch(driver, "perform_power")
        self.patch(driver, "power_query").return_value = "off"
        yield driver.cycle(system_id, context)
        self.assertThat(
            mock_perform_power,
            MockCalledOnceWith(driver.power_on, "on", system_id, context),
        )


class TestPowerDriverQuery(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def setUp(self):
        super(TestPowerDriverQuery, self).setUp()
        self.patch(power, "pause")

    @inlineCallbacks
    def test_returns_state(self):
        system_id = factory.make_name("system_id")
        context = {"context": factory.make_name("context")}
        driver = make_power_driver()
        state = factory.make_name("state")
        self.patch(driver, "power_query").return_value = state
        output = yield driver.query(system_id, context)
        self.assertEqual(state, output)

    @inlineCallbacks
    def test_retries_on_failure_then_returns_state(self):
        driver = make_power_driver()
        self.patch(driver, "power_query").side_effect = [
            PowerError("one"),
            PowerError("two"),
            sentinel.state,
        ]
        output = yield driver.query(sentinel.system_id, sentinel.context)
        self.assertEqual(sentinel.state, output)

    @inlineCallbacks
    def test_raises_last_exception_after_all_retries_fail(self):
        wait_time = [random.randrange(1, 10) for _ in range(3)]
        driver = make_power_driver(wait_time=wait_time)
        exception_types = list(
            factory.make_exception_type((PowerError,)) for _ in wait_time
        )
        self.patch(driver, "power_query").side_effect = exception_types
        with ExpectedException(exception_types[-1]):
            yield driver.query(sentinel.system_id, sentinel.context)

    @inlineCallbacks
    def test_pauses_between_retries(self):
        wait_time = [random.randrange(1, 10) for _ in range(3)]
        driver = make_power_driver(wait_time=wait_time)
        self.patch(driver, "power_query").side_effect = PowerError
        with ExpectedException(PowerError):
            yield driver.query(sentinel.system_id, sentinel.context)
        self.assertThat(
            power.pause,
            MockCallsMatch(*(call(wait, reactor) for wait in wait_time)),
        )
