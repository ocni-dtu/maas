# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for :py:module:`~provisioningserver.rpc.dhcp`."""

__all__ = []

import copy
from operator import itemgetter
from unittest.mock import ANY, call, Mock, sentinel

from fixtures import FakeLogger
from maastesting.factory import factory
from maastesting.matchers import (
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
from provisioningserver.dhcp.testing.config import (
    DHCPConfigNameResolutionDisabled,
    fix_shared_networks_failover,
    make_failover_peer_config,
    make_global_dhcp_snippets,
    make_host,
    make_host_dhcp_snippets,
    make_interface,
    make_shared_network,
    make_subnet_dhcp_snippets,
)
from provisioningserver.rpc import dhcp, exceptions
from provisioningserver.utils.service_monitor import (
    SERVICE_STATE,
    ServiceActionError,
    ServiceState,
)
from provisioningserver.utils.shell import ExternalProcessError
from testtools import ExpectedException
from testtools.matchers import MatchesStructure
from twisted.internet.defer import inlineCallbacks


class TestDHCPState(MAASTestCase):
    def make_args(self):
        omapi_key = factory.make_name("omapi_key")
        failover_peers = [make_failover_peer_config() for _ in range(3)]
        shared_networks = [make_shared_network() for _ in range(3)]
        shared_networks = fix_shared_networks_failover(
            shared_networks, failover_peers
        )
        hosts = [make_host() for _ in range(3)]
        interfaces = [make_interface() for _ in range(3)]
        return (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            make_global_dhcp_snippets(),
        )

    def test_new_sorts_properties(self):
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        self.assertThat(
            state,
            MatchesStructure.byEquality(
                omapi_key=omapi_key,
                failover_peers=sorted(failover_peers, key=itemgetter("name")),
                shared_networks=sorted(
                    shared_networks, key=itemgetter("name")
                ),
                hosts={host["mac"]: host for host in hosts},
                interfaces=sorted(
                    [interface["name"] for interface in interfaces]
                ),
                global_dhcp_snippets=sorted(
                    global_dhcp_snippets, key=itemgetter("name")
                ),
            ),
        )

    def test_requires_restart_returns_True_when_omapi_key_different(self):
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        new_state = dhcp.DHCPState(
            factory.make_name("new_omapi_key"),
            copy.deepcopy(failover_peers),
            copy.deepcopy(shared_networks),
            copy.deepcopy(hosts),
            copy.deepcopy(interfaces),
            copy.deepcopy(global_dhcp_snippets),
        )
        self.assertTrue(new_state.requires_restart(state))

    def test_requires_restart_returns_True_when_failover_different(self):
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        changed_failover_peers = copy.deepcopy(failover_peers)
        changed_failover_peers[0]["name"] = factory.make_name("failover")
        new_state = dhcp.DHCPState(
            omapi_key,
            changed_failover_peers,
            copy.deepcopy(shared_networks),
            copy.deepcopy(hosts),
            copy.deepcopy(interfaces),
            copy.deepcopy(global_dhcp_snippets),
        )
        self.assertTrue(new_state.requires_restart(state))

    def test_requires_restart_returns_True_when_network_different(self):
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        changed_shared_networks = copy.deepcopy(shared_networks)
        changed_shared_networks[0]["name"] = factory.make_name("network")
        new_state = dhcp.DHCPState(
            omapi_key,
            copy.deepcopy(failover_peers),
            changed_shared_networks,
            copy.deepcopy(hosts),
            copy.deepcopy(interfaces),
            copy.deepcopy(global_dhcp_snippets),
        )
        self.assertTrue(new_state.requires_restart(state))

    def test_requires_restart_returns_True_when_interfaces_different(self):
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        changed_interfaces = copy.deepcopy(interfaces)
        changed_interfaces[0]["name"] = factory.make_name("eth")
        new_state = dhcp.DHCPState(
            omapi_key,
            copy.deepcopy(failover_peers),
            copy.deepcopy(shared_networks),
            copy.deepcopy(hosts),
            changed_interfaces,
            copy.deepcopy(global_dhcp_snippets),
        )
        self.assertTrue(new_state.requires_restart(state))

    def test_requires_restart_returns_False_when_all_the_same(self):
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        new_state = dhcp.DHCPState(
            omapi_key,
            copy.deepcopy(failover_peers),
            copy.deepcopy(shared_networks),
            copy.deepcopy(hosts),
            copy.deepcopy(interfaces),
            copy.deepcopy(global_dhcp_snippets),
        )
        self.assertFalse(new_state.requires_restart(state))

    def test_requires_restart_returns_False_when_hosts_different(self):
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        changed_hosts = copy.deepcopy(hosts)
        changed_hosts.append(make_host(dhcp_snippets=[]))
        new_state = dhcp.DHCPState(
            omapi_key,
            copy.deepcopy(failover_peers),
            copy.deepcopy(shared_networks),
            changed_hosts,
            copy.deepcopy(interfaces),
            copy.deepcopy(global_dhcp_snippets),
        )
        self.assertFalse(new_state.requires_restart(state))

    def test_requires_restart_True_when_global_dhcp_snippets_diff(self):
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        changed_global_dhcp_snippets = make_global_dhcp_snippets(
            allow_empty=False
        )
        new_state = dhcp.DHCPState(
            omapi_key,
            copy.deepcopy(failover_peers),
            copy.deepcopy(shared_networks),
            copy.deepcopy(hosts),
            copy.deepcopy(interfaces),
            changed_global_dhcp_snippets,
        )
        self.assertTrue(new_state.requires_restart(state))

    def test_requires_restart_True_when_subnet_dhcp_snippets_diff(self):
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        changed_shared_networks = copy.deepcopy(shared_networks)
        for shared_network in changed_shared_networks:
            for subnet in shared_network["subnets"]:
                subnet["dhcp_snippets"] = make_subnet_dhcp_snippets(
                    allow_empty=False
                )
        new_state = dhcp.DHCPState(
            omapi_key,
            copy.deepcopy(failover_peers),
            changed_shared_networks,
            copy.deepcopy(hosts),
            copy.deepcopy(interfaces),
            copy.deepcopy(global_dhcp_snippets),
        )
        self.assertTrue(new_state.requires_restart(state))

    def test_requires_restart_True_when_hosts_dhcp_snippets_diff(self):
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        changed_hosts = copy.deepcopy(hosts)
        for host in changed_hosts:
            host["dhcp_snippets"] = make_host_dhcp_snippets(allow_empty=False)
        new_state = dhcp.DHCPState(
            omapi_key,
            copy.deepcopy(failover_peers),
            copy.deepcopy(shared_networks),
            changed_hosts,
            copy.deepcopy(interfaces),
            copy.deepcopy(global_dhcp_snippets),
        )
        self.assertTrue(new_state.requires_restart(state))

    def test_host_diff_returns_removal_added_and_modify(self):
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        changed_hosts = copy.deepcopy(hosts)
        removed_host = changed_hosts.pop()
        modified_host = changed_hosts[0]
        modified_host["ip"] = factory.make_ip_address()
        added_host = make_host()
        changed_hosts.append(added_host)
        new_state = dhcp.DHCPState(
            omapi_key,
            copy.deepcopy(failover_peers),
            copy.deepcopy(shared_networks),
            changed_hosts,
            copy.deepcopy(interfaces),
            copy.deepcopy(global_dhcp_snippets),
        )
        self.assertEqual(
            ([removed_host], [added_host], [modified_host]),
            new_state.host_diff(state),
        )

    def test_get_config_returns_config_and_calls_with_params(self):
        mock_get_config = self.patch_autospec(dhcp, "get_config")
        mock_get_config.return_value = sentinel.config
        (
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ) = self.make_args()
        state = dhcp.DHCPState(
            omapi_key,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        )
        server = Mock()
        self.assertEqual(
            (sentinel.config, " ".join(state.interfaces)),
            state.get_config(server),
        )
        self.assertThat(
            mock_get_config,
            MockCalledOnceWith(
                server.template_basename,
                omapi_key=omapi_key,
                ipv6=ANY,
                failover_peers=state.failover_peers,
                shared_networks=state.shared_networks,
                hosts=sorted(state.hosts.values(), key=itemgetter("host")),
                global_dhcp_snippets=sorted(
                    global_dhcp_snippets, key=itemgetter("name")
                ),
            ),
        )


class TestRemoveHostMap(MAASTestCase):
    def test_calls_omshell_remove(self):
        omshell = Mock()
        mac = factory.make_mac_address()
        dhcp._remove_host_map(omshell, mac)
        self.assertThat(omshell.remove, MockCalledOnceWith(mac))

    def test_raises_error_when_omshell_crashes(self):
        error_message = factory.make_name("error").encode("ascii")
        omshell = Mock()
        omshell.remove.side_effect = ExternalProcessError(
            returncode=2, cmd=("omshell",), output=error_message
        )
        mac = factory.make_mac_address()
        with FakeLogger("maas.dhcp") as logger:
            error = self.assertRaises(
                exceptions.CannotRemoveHostMap,
                dhcp._remove_host_map,
                omshell,
                mac,
            )
        # The CannotRemoveHostMap exception includes a message describing the
        # problematic mapping.
        self.assertDocTestMatches(
            "Could not remove host map for %s: ..." % mac, str(error)
        )
        # A message is also written to the maas.dhcp logger that describes the
        # problematic mapping.
        self.assertDocTestMatches(
            "Could not remove host map for %s: ..." % mac, logger.output
        )

    def test_raises_error_when_omshell_not_connected(self):
        error = ExternalProcessError(returncode=2, cmd=("omshell",), output="")
        self.patch(ExternalProcessError, "output_as_unicode", "not connected.")
        omshell = Mock()
        omshell.remove.side_effect = error
        mac = factory.make_mac_address()
        with FakeLogger("maas.dhcp") as logger:
            error = self.assertRaises(
                exceptions.CannotRemoveHostMap,
                dhcp._remove_host_map,
                omshell,
                mac,
            )
        # The CannotCreateHostMap exception includes a message describing the
        # problematic mapping.
        self.assertDocTestMatches(
            "Could not remove host map for %s: "
            "The DHCP server could not be reached." % mac,
            str(error),
        )
        # A message is also written to the maas.dhcp logger that describes the
        # problematic mapping.
        self.assertDocTestMatches(
            "Could not remove host map for %s: "
            "The DHCP server could not be reached." % mac,
            logger.output,
        )


class TestCreateHostMap(MAASTestCase):
    def test_calls_omshell_create(self):
        omshell = Mock()
        mac = factory.make_mac_address()
        ip = factory.make_ip_address()
        dhcp._create_host_map(omshell, mac, ip)
        self.assertThat(omshell.create, MockCalledOnceWith(ip, mac))

    def test_raises_error_when_omshell_crashes(self):
        error_message = factory.make_name("error").encode("ascii")
        omshell = Mock()
        omshell.create.side_effect = ExternalProcessError(
            returncode=2, cmd=("omshell",), output=error_message
        )
        mac = factory.make_mac_address()
        ip = factory.make_ip_address()
        with FakeLogger("maas.dhcp") as logger:
            error = self.assertRaises(
                exceptions.CannotCreateHostMap,
                dhcp._create_host_map,
                omshell,
                mac,
                ip,
            )
        # The CannotCreateHostMap exception includes a message describing the
        # problematic mapping.
        self.assertDocTestMatches(
            "Could not create host map for %s -> %s: ..." % (mac, ip),
            str(error),
        )
        # A message is also written to the maas.dhcp logger that describes the
        # problematic mapping.
        self.assertDocTestMatches(
            "Could not create host map for %s -> %s: ..." % (mac, ip),
            logger.output,
        )

    def test_raises_error_when_omshell_not_connected(self):
        error = ExternalProcessError(returncode=2, cmd=("omshell",), output="")
        self.patch(ExternalProcessError, "output_as_unicode", "not connected.")
        omshell = Mock()
        omshell.create.side_effect = error
        mac = factory.make_mac_address()
        ip = factory.make_ip_address()
        with FakeLogger("maas.dhcp") as logger:
            error = self.assertRaises(
                exceptions.CannotCreateHostMap,
                dhcp._create_host_map,
                omshell,
                mac,
                ip,
            )
        # The CannotCreateHostMap exception includes a message describing the
        # problematic mapping.
        self.assertDocTestMatches(
            "Could not create host map for %s -> %s: "
            "The DHCP server could not be reached." % (mac, ip),
            str(error),
        )
        # A message is also written to the maas.dhcp logger that describes the
        # problematic mapping.
        self.assertDocTestMatches(
            "Could not create host map for %s -> %s: "
            "The DHCP server could not be reached." % (mac, ip),
            logger.output,
        )


class TestUpdateHost(MAASTestCase):
    def test__creates_omshell_with_correct_arguments(self):
        omshell = self.patch(dhcp, "Omshell")
        server = Mock()
        server.ipv6 = factory.pick_bool()
        dhcp._update_hosts(server, [], [], [])
        self.assertThat(
            omshell,
            MockCallsMatch(
                call(
                    ipv6=server.ipv6,
                    server_address="127.0.0.1",
                    shared_key=server.omapi_key,
                )
            ),
        )

    def test__performs_operations(self):
        omshell = Mock()
        self.patch(dhcp, "Omshell").return_value = omshell
        remove_host = make_host()
        add_host = make_host()
        modify_host = make_host()
        server = Mock()
        server.ipv6 = factory.pick_bool()
        dhcp._update_hosts(server, [remove_host], [add_host], [modify_host])
        self.assertThat(
            omshell.remove, MockCallsMatch(call(remove_host["mac"]))
        )
        self.assertThat(
            omshell.create,
            MockCallsMatch(call(add_host["ip"], add_host["mac"])),
        )
        self.assertThat(
            omshell.modify,
            MockCallsMatch(call(modify_host["ip"], modify_host["mac"])),
        )


class TestConfigureDHCP(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    scenarios = (
        ("DHCPv4", {"server": dhcp.DHCPv4Server}),
        ("DHCPv6", {"server": dhcp.DHCPv6Server}),
    )

    def setUp(self):
        super(TestConfigureDHCP, self).setUp()
        # The service monitor is an application global and so are the services
        # it monitors, and tests must leave them as they found them.
        self.addCleanup(dhcp.service_monitor.getServiceByName("dhcpd").off)
        self.addCleanup(dhcp.service_monitor.getServiceByName("dhcpd6").off)
        # The dhcp server states are global so we clean them after each test.
        self.addCleanup(dhcp._current_server_state.clear)
        # Temporarily prevent hostname resolution when generating DHCP
        # configuration. This is tested elsewhere.
        self.useFixture(DHCPConfigNameResolutionDisabled())

    def configure(
        self,
        omapi_key,
        failover_peers,
        shared_networks,
        hosts,
        interfaces,
        dhcp_snippets,
    ):
        server = self.server(omapi_key)
        return dhcp.configure(
            server,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            dhcp_snippets,
        )

    def patch_os_exists(self):
        return self.patch_autospec(dhcp.os.path, "exists")

    def patch_sudo_delete_file(self):
        return self.patch_autospec(dhcp, "sudo_delete_file")

    def patch_sudo_write_file(self):
        return self.patch_autospec(dhcp, "sudo_write_file")

    def patch_restartService(self):
        return self.patch(dhcp.service_monitor, "restartService")

    def patch_ensureService(self):
        return self.patch(dhcp.service_monitor, "ensureService")

    def patch_getServiceState(self):
        return self.patch(dhcp.service_monitor, "getServiceState")

    def patch_get_config(self):
        return self.patch_autospec(dhcp, "get_config")

    def patch_update_hosts(self):
        return self.patch(dhcp, "_update_hosts")

    @inlineCallbacks
    def test__deletes_dhcp_config_if_no_subnets_defined(self):
        mock_exists = self.patch_os_exists()
        mock_exists.return_value = True
        mock_sudo_delete = self.patch_sudo_delete_file()
        dhcp_service = dhcp.service_monitor.getServiceByName(
            self.server.dhcp_service
        )
        self.patch_autospec(dhcp_service, "off")
        self.patch_restartService()
        self.patch_ensureService()
        yield self.configure(factory.make_name("key"), [], [], [], [], [])
        self.assertThat(
            mock_sudo_delete, MockCalledOnceWith(self.server.config_filename)
        )

    @inlineCallbacks
    def test__stops_dhcp_server_if_no_subnets_defined(self):
        mock_exists = self.patch_os_exists()
        mock_exists.return_value = False
        dhcp_service = dhcp.service_monitor.getServiceByName(
            self.server.dhcp_service
        )
        off = self.patch_autospec(dhcp_service, "off")
        restart_service = self.patch_restartService()
        ensure_service = self.patch_ensureService()
        yield self.configure(factory.make_name("key"), [], [], [], [], [])
        self.assertThat(off, MockCalledOnceWith())
        self.assertThat(
            ensure_service, MockCalledOnceWith(self.server.dhcp_service)
        )
        self.assertThat(restart_service, MockNotCalled())

    @inlineCallbacks
    def test__stops_dhcp_server_clears_state(self):
        dhcp._current_server_state[self.server.dhcp_service] = sentinel.state
        mock_exists = self.patch_os_exists()
        mock_exists.return_value = False
        dhcp_service = dhcp.service_monitor.getServiceByName(
            self.server.dhcp_service
        )
        self.patch_autospec(dhcp_service, "off")
        self.patch_restartService()
        self.patch_ensureService()
        yield self.configure(factory.make_name("key"), [], [], [], [], [])
        self.assertIsNone(dhcp._current_server_state[self.server.dhcp_service])

    @inlineCallbacks
    def test__writes_config_and_calls_restart_when_no_current_state(self):
        write_file = self.patch_sudo_write_file()
        restart_service = self.patch_restartService()

        failover_peers = make_failover_peer_config()
        shared_network = make_shared_network()
        [shared_network] = fix_shared_networks_failover(
            [shared_network], [failover_peers]
        )
        host = make_host()
        interface = make_interface()
        global_dhcp_snippets = make_global_dhcp_snippets()
        expected_config = factory.make_name("config")
        self.patch_get_config().return_value = expected_config

        dhcp_service = dhcp.service_monitor.getServiceByName(
            self.server.dhcp_service
        )
        on = self.patch_autospec(dhcp_service, "on")

        omapi_key = factory.make_name("omapi_key")
        yield self.configure(
            omapi_key,
            [failover_peers],
            [shared_network],
            [host],
            [interface],
            global_dhcp_snippets,
        )

        self.assertThat(
            write_file,
            MockCallsMatch(
                call(
                    self.server.config_filename,
                    expected_config.encode("utf-8"),
                    mode=0o640,
                ),
                call(
                    self.server.interfaces_filename,
                    interface["name"].encode("utf-8"),
                    mode=0o640,
                ),
            ),
        )
        self.assertThat(on, MockCalledOnceWith())
        self.assertThat(
            restart_service, MockCalledOnceWith(self.server.dhcp_service)
        )
        self.assertEquals(
            dhcp._current_server_state[self.server.dhcp_service],
            dhcp.DHCPState(
                omapi_key,
                [failover_peers],
                [shared_network],
                [host],
                [interface],
                global_dhcp_snippets,
            ),
        )

    @inlineCallbacks
    def test__writes_config_and_calls_restart_when_non_host_state_diff(self):
        write_file = self.patch_sudo_write_file()
        restart_service = self.patch_restartService()

        failover_peers = make_failover_peer_config()
        shared_network = make_shared_network()
        [shared_network] = fix_shared_networks_failover(
            [shared_network], [failover_peers]
        )
        host = make_host()
        interface = make_interface()
        global_dhcp_snippets = make_global_dhcp_snippets()
        expected_config = factory.make_name("config")
        self.patch_get_config().return_value = expected_config

        dhcp_service = dhcp.service_monitor.getServiceByName(
            self.server.dhcp_service
        )
        on = self.patch_autospec(dhcp_service, "on")

        old_state = dhcp.DHCPState(
            factory.make_name("omapi_key"),
            [failover_peers],
            [shared_network],
            [host],
            [interface],
            global_dhcp_snippets,
        )
        dhcp._current_server_state[self.server.dhcp_service] = old_state

        omapi_key = factory.make_name("omapi_key")
        yield self.configure(
            omapi_key,
            [failover_peers],
            [shared_network],
            [host],
            [interface],
            global_dhcp_snippets,
        )

        self.assertThat(
            write_file,
            MockCallsMatch(
                call(
                    self.server.config_filename,
                    expected_config.encode("utf-8"),
                    mode=0o640,
                ),
                call(
                    self.server.interfaces_filename,
                    interface["name"].encode("utf-8"),
                    mode=0o640,
                ),
            ),
        )
        self.assertThat(on, MockCalledOnceWith())
        self.assertThat(
            restart_service, MockCalledOnceWith(self.server.dhcp_service)
        )
        self.assertEquals(
            dhcp._current_server_state[self.server.dhcp_service],
            dhcp.DHCPState(
                omapi_key,
                [failover_peers],
                [shared_network],
                [host],
                [interface],
                global_dhcp_snippets,
            ),
        )

    @inlineCallbacks
    def test__writes_config_and_calls_ensure_when_nothing_changed(self):
        write_file = self.patch_sudo_write_file()
        restart_service = self.patch_restartService()
        ensure_service = self.patch_ensureService()

        failover_peers = make_failover_peer_config()
        shared_network = make_shared_network()
        [shared_network] = fix_shared_networks_failover(
            [shared_network], [failover_peers]
        )
        host = make_host()
        interface = make_interface()
        dhcp_snippets = make_global_dhcp_snippets()
        expected_config = factory.make_name("config")
        self.patch_get_config().return_value = expected_config

        dhcp_service = dhcp.service_monitor.getServiceByName(
            self.server.dhcp_service
        )
        on = self.patch_autospec(dhcp_service, "on")

        omapi_key = factory.make_name("omapi_key")
        old_state = dhcp.DHCPState(
            omapi_key,
            [failover_peers],
            [shared_network],
            [host],
            [interface],
            dhcp_snippets,
        )
        dhcp._current_server_state[self.server.dhcp_service] = old_state

        yield self.configure(
            omapi_key,
            [failover_peers],
            [shared_network],
            [host],
            [interface],
            dhcp_snippets,
        )

        self.assertThat(
            write_file,
            MockCallsMatch(
                call(
                    self.server.config_filename,
                    expected_config.encode("utf-8"),
                    mode=0o640,
                ),
                call(
                    self.server.interfaces_filename,
                    interface["name"].encode("utf-8"),
                    mode=0o640,
                ),
            ),
        )
        self.assertThat(on, MockCalledOnceWith())
        self.assertThat(restart_service, MockNotCalled())
        self.assertThat(
            ensure_service, MockCalledOnceWith(self.server.dhcp_service)
        )
        self.assertEquals(
            dhcp._current_server_state[self.server.dhcp_service],
            dhcp.DHCPState(
                omapi_key,
                [failover_peers],
                [shared_network],
                [host],
                [interface],
                dhcp_snippets,
            ),
        )

    @inlineCallbacks
    def test__writes_config_and_doesnt_use_omapi_when_was_off(self):
        write_file = self.patch_sudo_write_file()
        get_service_state = self.patch_getServiceState()
        get_service_state.return_value = ServiceState(
            SERVICE_STATE.OFF, "dead"
        )
        restart_service = self.patch_restartService()
        ensure_service = self.patch_ensureService()
        update_hosts = self.patch_update_hosts()

        failover_peers = make_failover_peer_config()
        shared_network = make_shared_network()
        [shared_network] = fix_shared_networks_failover(
            [shared_network], [failover_peers]
        )
        host = make_host(dhcp_snippets=[])
        interface = make_interface()
        global_dhcp_snippets = make_global_dhcp_snippets()
        expected_config = factory.make_name("config")
        self.patch_get_config().return_value = expected_config

        dhcp_service = dhcp.service_monitor.getServiceByName(
            self.server.dhcp_service
        )
        on = self.patch_autospec(dhcp_service, "on")

        omapi_key = factory.make_name("omapi_key")
        old_host = make_host(dhcp_snippets=[])
        old_state = dhcp.DHCPState(
            omapi_key,
            [failover_peers],
            [shared_network],
            [old_host],
            [interface],
            global_dhcp_snippets,
        )
        dhcp._current_server_state[self.server.dhcp_service] = old_state

        yield self.configure(
            omapi_key,
            [failover_peers],
            [shared_network],
            [host],
            [interface],
            global_dhcp_snippets,
        )

        self.assertThat(
            write_file,
            MockCallsMatch(
                call(
                    self.server.config_filename,
                    expected_config.encode("utf-8"),
                    mode=0o640,
                ),
                call(
                    self.server.interfaces_filename,
                    interface["name"].encode("utf-8"),
                    mode=0o640,
                ),
            ),
        )
        self.assertThat(on, MockCalledOnceWith())
        self.assertThat(
            get_service_state,
            MockCalledOnceWith(self.server.dhcp_service, now=True),
        )
        self.assertThat(restart_service, MockNotCalled())
        self.assertThat(
            ensure_service, MockCalledOnceWith(self.server.dhcp_service)
        )
        self.assertThat(update_hosts, MockNotCalled())
        self.assertEquals(
            dhcp._current_server_state[self.server.dhcp_service],
            dhcp.DHCPState(
                omapi_key,
                [failover_peers],
                [shared_network],
                [host],
                [interface],
                global_dhcp_snippets,
            ),
        )

    @inlineCallbacks
    def test__writes_config_and_uses_omapi_to_update_hosts(self):
        write_file = self.patch_sudo_write_file()
        get_service_state = self.patch_getServiceState()
        get_service_state.return_value = ServiceState(
            SERVICE_STATE.ON, "running"
        )
        restart_service = self.patch_restartService()
        ensure_service = self.patch_ensureService()
        update_hosts = self.patch_update_hosts()

        failover_peers = make_failover_peer_config()
        shared_network = make_shared_network()
        [shared_network] = fix_shared_networks_failover(
            [shared_network], [failover_peers]
        )
        old_hosts = [make_host(dhcp_snippets=[]) for _ in range(3)]
        interface = make_interface()
        global_dhcp_snippets = make_global_dhcp_snippets()
        expected_config = factory.make_name("config")
        self.patch_get_config().return_value = expected_config

        dhcp_service = dhcp.service_monitor.getServiceByName(
            self.server.dhcp_service
        )
        on = self.patch_autospec(dhcp_service, "on")

        omapi_key = factory.make_name("omapi_key")
        old_state = dhcp.DHCPState(
            omapi_key,
            [failover_peers],
            [shared_network],
            old_hosts,
            [interface],
            global_dhcp_snippets,
        )
        dhcp._current_server_state[self.server.dhcp_service] = old_state

        new_hosts = copy.deepcopy(old_hosts)
        removed_host = new_hosts.pop()
        modified_host = new_hosts[0]
        modified_host["ip"] = factory.make_ip_address()
        added_host = make_host(dhcp_snippets=[])
        new_hosts.append(added_host)

        yield self.configure(
            omapi_key,
            [failover_peers],
            [shared_network],
            new_hosts,
            [interface],
            global_dhcp_snippets,
        )

        self.assertThat(
            write_file,
            MockCallsMatch(
                call(
                    self.server.config_filename,
                    expected_config.encode("utf-8"),
                    mode=0o640,
                ),
                call(
                    self.server.interfaces_filename,
                    interface["name"].encode("utf-8"),
                    mode=0o640,
                ),
            ),
        )
        self.assertThat(on, MockCalledOnceWith())
        self.assertThat(
            get_service_state,
            MockCalledOnceWith(self.server.dhcp_service, now=True),
        )
        self.assertThat(restart_service, MockNotCalled())
        self.assertThat(
            ensure_service, MockCalledOnceWith(self.server.dhcp_service)
        )
        self.assertThat(
            update_hosts,
            MockCalledOnceWith(
                ANY, [removed_host], [added_host], [modified_host]
            ),
        )
        self.assertEquals(
            dhcp._current_server_state[self.server.dhcp_service],
            dhcp.DHCPState(
                omapi_key,
                [failover_peers],
                [shared_network],
                new_hosts,
                [interface],
                global_dhcp_snippets,
            ),
        )

    @inlineCallbacks
    def test__writes_config_and_restarts_when_omapi_fails(self):
        write_file = self.patch_sudo_write_file()
        get_service_state = self.patch_getServiceState()
        get_service_state.return_value = ServiceState(
            SERVICE_STATE.ON, "running"
        )
        restart_service = self.patch_restartService()
        ensure_service = self.patch_ensureService()
        update_hosts = self.patch_update_hosts()
        update_hosts.side_effect = factory.make_exception()

        failover_peers = make_failover_peer_config()
        shared_network = make_shared_network()
        [shared_network] = fix_shared_networks_failover(
            [shared_network], [failover_peers]
        )
        old_hosts = [make_host(dhcp_snippets=[]) for _ in range(3)]
        interface = make_interface()
        global_dhcp_snippets = make_global_dhcp_snippets()
        expected_config = factory.make_name("config")
        self.patch_get_config().return_value = expected_config

        dhcp_service = dhcp.service_monitor.getServiceByName(
            self.server.dhcp_service
        )
        on = self.patch_autospec(dhcp_service, "on")

        omapi_key = factory.make_name("omapi_key")
        old_state = dhcp.DHCPState(
            omapi_key,
            [failover_peers],
            [shared_network],
            old_hosts,
            [interface],
            global_dhcp_snippets,
        )
        dhcp._current_server_state[self.server.dhcp_service] = old_state

        new_hosts = copy.deepcopy(old_hosts)
        removed_host = new_hosts.pop()
        modified_host = new_hosts[0]
        modified_host["ip"] = factory.make_ip_address()
        added_host = make_host(dhcp_snippets=[])
        new_hosts.append(added_host)

        with FakeLogger("maas") as logger:
            yield self.configure(
                omapi_key,
                [failover_peers],
                [shared_network],
                new_hosts,
                [interface],
                global_dhcp_snippets,
            )

        self.assertThat(
            write_file,
            MockCallsMatch(
                call(
                    self.server.config_filename,
                    expected_config.encode("utf-8"),
                    mode=0o640,
                ),
                call(
                    self.server.interfaces_filename,
                    interface["name"].encode("utf-8"),
                    mode=0o640,
                ),
            ),
        )
        self.assertThat(on, MockCalledOnceWith())
        self.assertThat(
            get_service_state,
            MockCalledOnceWith(self.server.dhcp_service, now=True),
        )
        self.assertThat(
            restart_service, MockCalledOnceWith(self.server.dhcp_service)
        )
        self.assertThat(
            ensure_service, MockCalledOnceWith(self.server.dhcp_service)
        )
        self.assertThat(
            update_hosts,
            MockCalledOnceWith(
                ANY, [removed_host], [added_host], [modified_host]
            ),
        )
        self.assertEquals(
            dhcp._current_server_state[self.server.dhcp_service],
            dhcp.DHCPState(
                omapi_key,
                [failover_peers],
                [shared_network],
                new_hosts,
                [interface],
                global_dhcp_snippets,
            ),
        )
        self.assertDocTestMatches(
            "Failed to update all host maps. Restarting DHCPv... "
            "service to ensure host maps are in-sync.",
            logger.output,
        )

    @inlineCallbacks
    def test__converts_failure_writing_file_to_CannotConfigureDHCP(self):
        self.patch_sudo_delete_file()
        self.patch_sudo_write_file().side_effect = ExternalProcessError(
            1, "sudo something"
        )
        self.patch_restartService()
        failover_peers = [make_failover_peer_config()]
        shared_networks = fix_shared_networks_failover(
            [make_shared_network()], failover_peers
        )
        with ExpectedException(exceptions.CannotConfigureDHCP):
            yield self.configure(
                factory.make_name("key"),
                failover_peers,
                shared_networks,
                [make_host()],
                [make_interface()],
                make_global_dhcp_snippets(),
            )

    @inlineCallbacks
    def test__converts_dhcp_restart_failure_to_CannotConfigureDHCP(self):
        self.patch_sudo_write_file()
        self.patch_sudo_delete_file()
        self.patch_restartService().side_effect = ServiceActionError()
        failover_peers = [make_failover_peer_config()]
        shared_networks = fix_shared_networks_failover(
            [make_shared_network()], failover_peers
        )
        with ExpectedException(exceptions.CannotConfigureDHCP):
            yield self.configure(
                factory.make_name("key"),
                failover_peers,
                shared_networks,
                [make_host()],
                [make_interface()],
                make_global_dhcp_snippets(),
            )

    @inlineCallbacks
    def test__converts_stop_dhcp_server_failure_to_CannotConfigureDHCP(self):
        self.patch_sudo_write_file()
        self.patch_sudo_delete_file()
        self.patch_ensureService().side_effect = ServiceActionError()
        with ExpectedException(exceptions.CannotConfigureDHCP):
            yield self.configure(factory.make_name("key"), [], [], [], [], [])

    @inlineCallbacks
    def test__does_not_log_ServiceActionError(self):
        self.patch_sudo_write_file()
        self.patch_sudo_delete_file()
        self.patch_ensureService().side_effect = ServiceActionError()
        with FakeLogger("maas") as logger:
            with ExpectedException(exceptions.CannotConfigureDHCP):
                yield self.configure(
                    factory.make_name("key"), [], [], [], [], []
                )
        self.assertDocTestMatches("", logger.output)

    @inlineCallbacks
    def test__does_log_other_exceptions(self):
        self.patch_sudo_write_file()
        self.patch_sudo_delete_file()
        self.patch_ensureService().side_effect = factory.make_exception(
            "DHCP is on strike today"
        )
        with FakeLogger("maas") as logger:
            with ExpectedException(exceptions.CannotConfigureDHCP):
                yield self.configure(
                    factory.make_name("key"), [], [], [], [], []
                )
        self.assertDocTestMatches(
            "DHCPv... server failed to stop: DHCP is on strike today",
            logger.output,
        )

    @inlineCallbacks
    def test__does_not_log_ServiceActionError_when_restarting(self):
        self.patch_sudo_write_file()
        self.patch_restartService().side_effect = ServiceActionError()
        failover_peers = [make_failover_peer_config()]
        shared_networks = fix_shared_networks_failover(
            [make_shared_network()], failover_peers
        )
        with FakeLogger("maas") as logger:
            with ExpectedException(exceptions.CannotConfigureDHCP):
                yield self.configure(
                    factory.make_name("key"),
                    failover_peers,
                    shared_networks,
                    [make_host()],
                    [make_interface()],
                    make_global_dhcp_snippets(),
                )
        self.assertDocTestMatches("", logger.output)

    @inlineCallbacks
    def test__does_log_other_exceptions_when_restarting(self):
        self.patch_sudo_write_file()
        self.patch_restartService().side_effect = factory.make_exception(
            "DHCP is on strike today"
        )
        failover_peers = [make_failover_peer_config()]
        shared_networks = fix_shared_networks_failover(
            [make_shared_network()], failover_peers
        )
        with FakeLogger("maas") as logger:
            with ExpectedException(exceptions.CannotConfigureDHCP):
                yield self.configure(
                    factory.make_name("key"),
                    failover_peers,
                    shared_networks,
                    [make_host()],
                    [make_interface()],
                    make_global_dhcp_snippets(),
                )
        self.assertDocTestMatches(
            "DHCPv... server failed to restart: " "DHCP is on strike today",
            logger.output,
        )


class TestValidateDHCP(MAASTestCase):

    scenarios = (
        ("DHCPv4", {"server": dhcp.DHCPv4Server}),
        ("DHCPv6", {"server": dhcp.DHCPv6Server}),
    )

    def setUp(self):
        super(TestValidateDHCP, self).setUp()
        # Temporarily prevent hostname resolution when generating DHCP
        # configuration. This is tested elsewhere.
        self.useFixture(DHCPConfigNameResolutionDisabled())
        self.mock_call_and_check = self.patch(dhcp, "call_and_check")

    def validate(
        self,
        omapi_key,
        failover_peers,
        shared_networks,
        hosts,
        interfaces,
        dhcp_snippets,
    ):
        server = self.server(omapi_key)
        ret = dhcp.validate(
            server,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            dhcp_snippets,
        )
        # Regression test for LP:1585814
        self.assertThat(
            self.mock_call_and_check,
            MockCalledOnceWith(
                ["dhcpd", "-t", "-cf", "-6" if self.server.ipv6 else "-4", ANY]
            ),
        )
        return ret

    def test__good_config(self):
        omapi_key = factory.make_name("omapi_key")
        failover_peers = make_failover_peer_config()
        shared_network = make_shared_network()
        [shared_network] = fix_shared_networks_failover(
            [shared_network], [failover_peers]
        )
        host = make_host()
        interface = make_interface()
        global_dhcp_snippets = make_global_dhcp_snippets()

        self.assertEqual(
            None,
            self.validate(
                omapi_key,
                [failover_peers],
                [shared_network],
                [host],
                [interface],
                global_dhcp_snippets,
            ),
        )

    def test__bad_config(self):
        omapi_key = factory.make_name("omapi_key")
        failover_peers = make_failover_peer_config()
        shared_network = make_shared_network()
        [shared_network] = fix_shared_networks_failover(
            [shared_network], [failover_peers]
        )
        host = make_host()
        interface = make_interface()
        global_dhcp_snippets = make_global_dhcp_snippets()
        dhcpd_error = (
            "Internet Systems Consortium DHCP Server 4.3.3\n"
            "Copyright 2004-2015 Internet Systems Consortium.\n"
            "All rights reserved.\n"
            "For info, please visit https://www.isc.org/software/dhcp/\n"
            "/tmp/maas-dhcpd-z5c7hfzt line 14: semicolon expected.\n"
            "ignore \n"
            "^\n"
            "Configuration file errors encountered -- exiting\n"
            "\n"
            "If you think you have received this message due to a bug rather\n"
            "than a configuration issue please read the section on submitting"
            "\n"
            "bugs on either our web page at www.isc.org or in the README file"
            "\n"
            "before submitting a bug.  These pages explain the proper\n"
            "process and the information we find helpful for debugging..\n"
            "\n"
            "exiting."
        )
        self.mock_call_and_check.side_effect = ExternalProcessError(
            returncode=1, cmd=("dhcpd",), output=dhcpd_error
        )

        self.assertEqual(
            [
                {
                    "error": "semicolon expected.",
                    "line_num": 14,
                    "line": "ignore ",
                    "position": "^",
                }
            ],
            self.validate(
                omapi_key,
                [failover_peers],
                [shared_network],
                [host],
                [interface],
                global_dhcp_snippets,
            ),
        )
