# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `provisioningserver.drivers.power.ipmi`."""

__all__ = []

from unittest.mock import call

from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith, MockCallsMatch
from maastesting.testcase import MAASTestCase
from provisioningserver.drivers.power import (
    moonshot as moonshot_module,
    PowerActionError,
)
from provisioningserver.drivers.power.moonshot import MoonshotIPMIPowerDriver
from provisioningserver.utils.shell import (
    ExternalProcessError,
    get_env_with_locale,
    has_command_available,
)
from testtools.matchers import Equals


def make_context():
    return {
        "ipmitool": "ipmitool",
        "power_address": factory.make_name("power_address"),
        "power_user": factory.make_name("power_user"),
        "power_pass": factory.make_name("power_pass"),
        "power_hwaddress": factory.make_string(spaces=True),
    }


def make_command(
    ipmitool, power_address, power_user, power_pass, power_hwaddress
):
    return (
        ipmitool,
        "-I",
        "lanplus",
        "-H",
        power_address,
        "-U",
        power_user,
        "-P",
        power_pass,
    ) + tuple(power_hwaddress.split())


def make_pxe_command(context):
    return make_command(
        context["ipmitool"],
        context["power_address"],
        context["power_user"],
        context["power_pass"],
        context["power_hwaddress"],
    ) + ("chassis", "bootdev", "pxe")


def make_ipmitool_command(power_change, context):
    return make_command(
        context["ipmitool"],
        context["power_address"],
        context["power_user"],
        context["power_pass"],
        context["power_hwaddress"],
    ) + ("power", power_change)


class TestMoonshotIPMIPowerDriver(MAASTestCase):
    def test_missing_packages(self):
        mock = self.patch(has_command_available)
        mock.return_value = False
        driver = moonshot_module.MoonshotIPMIPowerDriver()
        missing = driver.detect_missing_packages()
        self.assertItemsEqual(["ipmitool"], missing)

    def test_no_missing_packages(self):
        mock = self.patch(has_command_available)
        mock.return_value = True
        driver = moonshot_module.MoonshotIPMIPowerDriver()
        missing = driver.detect_missing_packages()
        self.assertItemsEqual([], missing)

    def test__issue_ipmitool_command_sets_pxe_boot(self):
        context = make_context()
        env = get_env_with_locale()
        pxe_command = make_pxe_command(context)
        moonshot_driver = MoonshotIPMIPowerDriver()
        call_and_check_mock = self.patch(moonshot_module, "call_and_check")

        moonshot_driver._issue_ipmitool_command("pxe", **context)

        self.assertThat(
            call_and_check_mock, MockCalledOnceWith(pxe_command, env=env)
        )

    def test__issue_ipmitool_command_returns_stdout_if_no_match(self):
        context = make_context()
        env = get_env_with_locale()
        ipmitool_command = make_ipmitool_command("status", context)
        moonshot_driver = MoonshotIPMIPowerDriver()
        call_and_check_mock = self.patch(moonshot_module, "call_and_check")
        call_and_check_mock.return_value = b"other"

        result = moonshot_driver._issue_ipmitool_command("status", **context)

        self.expectThat(
            call_and_check_mock, MockCalledOnceWith(ipmitool_command, env=env)
        )
        self.expectThat(result, Equals("other"))

    def test__issue_ipmitool_raises_power_action_error(self):
        context = make_context()
        moonshot_driver = MoonshotIPMIPowerDriver()
        call_and_check_mock = self.patch(moonshot_module, "call_and_check")
        call_and_check_mock.side_effect = ExternalProcessError(
            1, "ipmitool something"
        )

        self.assertRaises(
            PowerActionError,
            moonshot_driver._issue_ipmitool_command,
            "status",
            **context
        )

    def test_power_on_calls__issue_ipmitool_command(self):
        context = make_context()
        moonshot_driver = MoonshotIPMIPowerDriver()
        _issue_ipmitool_command_mock = self.patch(
            moonshot_driver, "_issue_ipmitool_command"
        )
        system_id = factory.make_name("system_id")
        moonshot_driver.power_on(system_id, context)

        self.assertThat(
            _issue_ipmitool_command_mock,
            MockCallsMatch(call("pxe", **context), call("on", **context)),
        )

    def test_power_off_calls__issue_ipmitool_command(self):
        context = make_context()
        moonshot_driver = MoonshotIPMIPowerDriver()
        _issue_ipmitool_command_mock = self.patch(
            moonshot_driver, "_issue_ipmitool_command"
        )
        system_id = factory.make_name("system_id")
        moonshot_driver.power_off(system_id, context)

        self.assertThat(
            _issue_ipmitool_command_mock, MockCalledOnceWith("off", **context)
        )

    def test_power_query_calls__issue_ipmitool_command(self):
        context = make_context()
        moonshot_driver = MoonshotIPMIPowerDriver()
        _issue_ipmitool_command_mock = self.patch(
            moonshot_driver, "_issue_ipmitool_command"
        )
        system_id = factory.make_name("system_id")
        moonshot_driver.power_query(system_id, context)

        self.assertThat(
            _issue_ipmitool_command_mock,
            MockCalledOnceWith("status", **context),
        )
