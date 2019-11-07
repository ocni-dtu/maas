# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for :py:module:`provisioningserver.dns.actions`."""

__all__ = []

import os
from os.path import join
import random
from random import randint
from subprocess import CalledProcessError
from textwrap import dedent
from unittest.mock import call, sentinel

from fixtures import FakeLogger
from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith, MockCallsMatch
from maastesting.testcase import MAASTestCase
from netaddr import IPNetwork
from provisioningserver.dns import actions
from provisioningserver.dns.config import (
    MAAS_NAMED_CONF_NAME,
    MAAS_NAMED_CONF_OPTIONS_INSIDE_NAME,
)
from provisioningserver.dns.testing import patch_dns_config_path
from provisioningserver.dns.tests.test_zoneconfig import HostnameIPMapping
from provisioningserver.dns.zoneconfig import (
    DNSForwardZoneConfig,
    DNSReverseZoneConfig,
)
from provisioningserver.utils.shell import ExternalProcessError
from testtools.matchers import AllMatch, Contains, FileContains, FileExists


class TestReconfigure(MAASTestCase):
    """Tests for :py:func:`actions.bind_reconfigure`."""

    def test__executes_rndc_command(self):
        self.patch_autospec(actions, "execute_rndc_command")
        actions.bind_reconfigure()
        self.assertThat(
            actions.execute_rndc_command, MockCalledOnceWith(("reconfig",))
        )

    def test__logs_subprocess_error(self):
        erc = self.patch_autospec(actions, "execute_rndc_command")
        erc.side_effect = factory.make_CalledProcessError()
        with FakeLogger("maas") as logger:
            self.assertRaises(CalledProcessError, actions.bind_reconfigure)
        self.assertDocTestMatches(
            "Reloading BIND configuration failed: "
            "Command ... returned non-zero exit status ...",
            logger.output,
        )

    def test__upgrades_subprocess_error(self):
        erc = self.patch_autospec(actions, "execute_rndc_command")
        erc.side_effect = factory.make_CalledProcessError()
        self.assertRaises(ExternalProcessError, actions.bind_reconfigure)


class TestReload(MAASTestCase):
    """Tests for :py:func:`actions.bind_reload`."""

    def test__executes_rndc_command(self):
        self.patch_autospec(actions, "execute_rndc_command")
        actions.bind_reload()
        self.assertThat(
            actions.execute_rndc_command,
            MockCalledOnceWith(("reload",), timeout=2),
        )

    def test__logs_subprocess_error(self):
        erc = self.patch_autospec(actions, "execute_rndc_command")
        erc.side_effect = factory.make_CalledProcessError()
        with FakeLogger("maas") as logger:
            self.assertFalse(actions.bind_reload())
        self.assertDocTestMatches(
            "Reloading BIND failed (is it running?): "
            "Command ... returned non-zero exit status ...",
            logger.output,
        )

    def test__false_on_subprocess_error(self):
        erc = self.patch_autospec(actions, "execute_rndc_command")
        erc.side_effect = factory.make_CalledProcessError()
        self.assertFalse(actions.bind_reload())


class TestReloadWithRetries(MAASTestCase):
    """Tests for :py:func:`actions.bind_reload_with_retries`."""

    def test__calls_bind_reload_count_times(self):
        self.patch_autospec(actions, "sleep")  # Disable.
        bind_reload = self.patch_autospec(actions, "bind_reload")
        bind_reload.return_value = False
        attempts = randint(3, 13)
        actions.bind_reload_with_retries(attempts=attempts)
        expected_calls = [call(timeout=2)] * attempts
        self.assertThat(actions.bind_reload, MockCallsMatch(*expected_calls))

    def test__returns_on_success(self):
        self.patch_autospec(actions, "sleep")  # Disable.
        bind_reload = self.patch(actions, "bind_reload")
        bind_reload_return_values = [False, False, True]
        bind_reload.side_effect = lambda *args, **kwargs: (
            bind_reload_return_values.pop(0)
        )

        actions.bind_reload_with_retries(attempts=5)
        expected_calls = [call(timeout=2), call(timeout=2), call(timeout=2)]
        self.assertThat(actions.bind_reload, MockCallsMatch(*expected_calls))

    def test__sleeps_interval_seconds_between_attempts(self):
        self.patch_autospec(actions, "sleep")  # Disable.
        bind_reload = self.patch_autospec(actions, "bind_reload")
        bind_reload.return_value = False
        attempts = randint(3, 13)
        actions.bind_reload_with_retries(
            attempts=attempts, interval=sentinel.interval
        )
        expected_sleep_calls = [call(sentinel.interval)] * (attempts - 1)
        self.assertThat(actions.sleep, MockCallsMatch(*expected_sleep_calls))


class TestReloadZone(MAASTestCase):
    """Tests for :py:func:`actions.bind_reload_zones`."""

    def test__executes_rndc_command(self):
        self.patch_autospec(actions, "execute_rndc_command")
        self.assertTrue(actions.bind_reload_zones(sentinel.zone))
        self.assertThat(
            actions.execute_rndc_command,
            MockCalledOnceWith(("reload", sentinel.zone)),
        )

    def test__logs_subprocess_error(self):
        erc = self.patch_autospec(actions, "execute_rndc_command")
        erc.side_effect = factory.make_CalledProcessError()
        with FakeLogger("maas") as logger:
            self.assertFalse(actions.bind_reload_zones(sentinel.zone))
        self.assertDocTestMatches(
            "Reloading BIND zone ... failed (is it running?): "
            "Command ... returned non-zero exit status ...",
            logger.output,
        )

    def test__false_on_subprocess_error(self):
        erc = self.patch_autospec(actions, "execute_rndc_command")
        erc.side_effect = factory.make_CalledProcessError()
        self.assertFalse(actions.bind_reload_zones(sentinel.zone))


class TestConfiguration(MAASTestCase):
    """Tests for the `bind_write_*` functions."""

    def setUp(self):
        super(TestConfiguration, self).setUp()
        # Ensure that files are written to a temporary directory.
        self.dns_conf_dir = self.make_dir()
        patch_dns_config_path(self, self.dns_conf_dir)
        # Patch out calls to 'execute_rndc_command'.
        self.patch_autospec(actions, "execute_rndc_command")

    def test_bind_write_configuration_writes_file(self):
        domain = factory.make_string()
        zones = [
            DNSReverseZoneConfig(
                domain,
                serial=random.randint(1, 100),
                network=factory.make_ipv4_network(),
            ),
            DNSReverseZoneConfig(
                domain,
                serial=random.randint(1, 100),
                network=factory.make_ipv6_network(),
            ),
        ]
        actions.bind_write_configuration(zones=zones, trusted_networks=[])
        self.assertThat(
            os.path.join(self.dns_conf_dir, MAAS_NAMED_CONF_NAME), FileExists()
        )

    def test_bind_write_configuration_writes_file_with_acl(self):
        trusted_networks = [
            factory.make_ipv4_network(),
            factory.make_ipv6_network(),
        ]
        actions.bind_write_configuration(
            zones=[], trusted_networks=trusted_networks
        )
        expected_file = os.path.join(self.dns_conf_dir, MAAS_NAMED_CONF_NAME)
        self.assertThat(expected_file, FileExists())
        expected_content = dedent(
            """\
        acl "trusted" {
            %s;
            %s;
            localnets;
            localhost;
        };
        """
        )
        expected_content %= tuple(trusted_networks)
        self.assertThat(
            expected_file, FileContains(matcher=Contains(expected_content))
        )

    def test_bind_write_zones_writes_file(self):
        domain = factory.make_string()
        network = IPNetwork("192.168.0.3/24")
        dns_ip_list = [factory.pick_ip_in_network(network)]
        ip = factory.pick_ip_in_network(network)
        ttl = random.randint(10, 1000)
        forward_zone = DNSForwardZoneConfig(
            domain,
            serial=random.randint(1, 100),
            mapping={
                factory.make_string(): HostnameIPMapping(None, ttl, {ip})
            },
            dns_ip_list=dns_ip_list,
        )
        reverse_zone = DNSReverseZoneConfig(
            domain, serial=random.randint(1, 100), network=network
        )
        actions.bind_write_zones(zones=[forward_zone, reverse_zone])

        forward_file_name = "zone.%s" % domain
        reverse_file_name = "zone.0.168.192.in-addr.arpa"
        expected_files = [
            join(self.dns_conf_dir, forward_file_name),
            join(self.dns_conf_dir, reverse_file_name),
        ]
        self.assertThat(expected_files, AllMatch(FileExists()))

    def test_bind_write_options_sets_up_config(self):
        # bind_write_configuration_and_zones writes the config file, writes
        # the zone files, and reloads the dns service.
        upstream_dns = [
            factory.make_ipv4_address(),
            factory.make_ipv4_address(),
        ]
        dnssec_validation = random.choice(["auto", "yes", "no"])
        expected_dnssec_validation = dnssec_validation
        actions.bind_write_options(
            upstream_dns=upstream_dns, dnssec_validation=dnssec_validation
        )
        expected_options_file = join(
            self.dns_conf_dir, MAAS_NAMED_CONF_OPTIONS_INSIDE_NAME
        )
        self.assertThat(expected_options_file, FileExists())
        expected_options_content = dedent(
            """\
        forwarders {
            %s;
            %s;
        };

        dnssec-validation %s;

        allow-query { any; };
        allow-recursion { trusted; };
        allow-query-cache { trusted; };
        """
        )
        expected_options_content %= tuple(upstream_dns) + (
            expected_dnssec_validation,
        )

        self.assertThat(
            expected_options_file, FileContains(expected_options_content)
        )
