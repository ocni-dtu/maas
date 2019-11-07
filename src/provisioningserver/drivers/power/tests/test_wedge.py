# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `provisioningserver.drivers.power.wedge`."""

__all__ = []

from io import BytesIO
from random import choice
from socket import error as SOCKETError
from unittest.mock import Mock

from hypothesis import given
from hypothesis.strategies import sampled_from
from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase
from paramiko import SSHException
from provisioningserver.drivers.power import (
    PowerActionError,
    PowerConnError,
    PowerFatalError,
    wedge as wedge_module,
)
from provisioningserver.drivers.power.wedge import WedgePowerDriver, WedgeState
from testtools.matchers import Equals


def make_context():
    """Make and return a power parameters context."""
    return {
        "power_address": factory.make_name("power_address"),
        "power_user": factory.make_name("power_user"),
        "power_pass": factory.make_name("power_pass"),
    }


class TestWedgePowerDriver(MAASTestCase):
    def test_missing_packages(self):
        # there's nothing to check for, just confirm it returns []
        driver = wedge_module.WedgePowerDriver()
        missing = driver.detect_missing_packages()
        self.assertItemsEqual([], missing)

    def test_run_wedge_command_returns_command_output(self):
        driver = WedgePowerDriver()
        command = factory.make_name("command")
        context = make_context()
        SSHClient = self.patch(wedge_module, "SSHClient")
        AutoAddPolicy = self.patch(wedge_module, "AutoAddPolicy")
        ssh_client = SSHClient.return_value
        expected = factory.make_name("output").encode("utf-8")
        stdout = BytesIO(expected)
        streams = factory.make_streams(stdout=stdout)
        ssh_client.exec_command = Mock(return_value=streams)
        output = driver.run_wedge_command(command, **context)

        self.expectThat(expected.decode("utf-8"), Equals(output))
        self.expectThat(SSHClient, MockCalledOnceWith())
        self.expectThat(
            ssh_client.set_missing_host_key_policy,
            MockCalledOnceWith(AutoAddPolicy.return_value),
        )
        self.expectThat(
            ssh_client.connect,
            MockCalledOnceWith(
                context["power_address"],
                username=context["power_user"],
                password=context["power_pass"],
            ),
        )
        self.expectThat(ssh_client.exec_command, MockCalledOnceWith(command))

    @given(sampled_from([SSHException, EOFError, SOCKETError]))
    def test_run_wedge_command_crashes_for_ssh_connection_error(self, error):
        driver = WedgePowerDriver()
        command = factory.make_name("command")
        context = make_context()
        self.patch(wedge_module, "AutoAddPolicy")
        SSHClient = self.patch(wedge_module, "SSHClient")
        ssh_client = SSHClient.return_value
        ssh_client.connect.side_effect = error
        self.assertRaises(
            PowerConnError, driver.run_wedge_command, command, **context
        )

    def test_power_on_calls_run_wedge_command(self):
        driver = WedgePowerDriver()
        system_id = factory.make_name("system_id")
        context = make_context()
        power_query = self.patch(driver, "power_query")
        power_query.return_value = choice(WedgeState.ON)
        self.patch(driver, "power_off")
        run_wedge_command = self.patch(driver, "run_wedge_command")
        driver.power_on(system_id, context)
        self.assertThat(
            run_wedge_command,
            MockCalledOnceWith("/usr/local/bin/wedge_power.sh on", **context),
        )

    def test_power_on_crashes_for_connection_error(self):
        driver = WedgePowerDriver()
        system_id = factory.make_name("system_id")
        context = make_context()
        power_query = self.patch(driver, "power_query")
        power_query.return_value = "off"
        run_wedge_command = self.patch(driver, "run_wedge_command")
        run_wedge_command.side_effect = PowerConnError("Connection Error")
        self.assertRaises(
            PowerActionError, driver.power_on, system_id, context
        )

    def test_power_off_calls_run_wedge_command(self):
        driver = WedgePowerDriver()
        system_id = factory.make_name("system_id")
        context = make_context()
        run_wedge_command = self.patch(driver, "run_wedge_command")
        driver.power_off(system_id, context)
        self.assertThat(
            run_wedge_command,
            MockCalledOnceWith("/usr/local/bin/wedge_power.sh off", **context),
        )

    def test_power_off_crashes_for_connection_error(self):
        driver = WedgePowerDriver()
        system_id = factory.make_name("system_id")
        context = make_context()
        run_wedge_command = self.patch(driver, "run_wedge_command")
        run_wedge_command.side_effect = PowerConnError("Connection Error")
        self.assertRaises(
            PowerActionError, driver.power_off, system_id, context
        )

    @given(sampled_from(WedgeState.ON + WedgeState.OFF))
    def test_power_query_returns_power_state(self, power_state):
        def get_wedge_state(power_state):
            if power_state in WedgeState.OFF:
                return "off"
            elif power_state in WedgeState.ON:
                return "on"

        driver = WedgePowerDriver()
        system_id = factory.make_name("system_id")
        context = make_context()
        run_wedge_command = self.patch(driver, "run_wedge_command")
        run_wedge_command.return_value = power_state
        output = driver.power_query(system_id, context)
        self.assertThat(output, Equals(get_wedge_state(power_state)))

    def test_power_query_crashes_for_connection_error(self):
        driver = WedgePowerDriver()
        system_id = factory.make_name("system_id")
        context = make_context()
        run_wedge_command = self.patch(driver, "run_wedge_command")
        run_wedge_command.side_effect = PowerConnError("Connection Error")
        self.assertRaises(
            PowerActionError, driver.power_query, system_id, context
        )

    def test_power_query_crashes_when_unable_to_find_match(self):
        driver = WedgePowerDriver()
        system_id = factory.make_name("system_id")
        context = make_context()
        run_wedge_command = self.patch(driver, "run_wedge_command")
        run_wedge_command.return_value = "Rubbish"
        self.assertRaises(
            PowerFatalError, driver.power_query, system_id, context
        )
