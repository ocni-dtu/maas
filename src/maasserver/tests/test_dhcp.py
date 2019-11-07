# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for DHCP management."""

__all__ = []

from datetime import datetime
from operator import itemgetter
import random
from unittest.mock import ANY

from crochet import wait_for
from django.core.exceptions import ValidationError
from maasserver import (
    dhcp,
    dhcp as dhcp_module,
    server_address as server_address_module,
)
from maasserver.dhcp import get_default_dns_servers
from maasserver.enum import INTERFACE_TYPE, IPADDRESS_TYPE, SERVICE_STATUS
from maasserver.models import (
    Config,
    DHCPSnippet,
    Domain,
    Service,
    VersionedTextFile,
)
from maasserver.rpc.testing.fixtures import MockLiveRegionToClusterRPCFixture
from maasserver.testing.eventloop import (
    RegionEventLoopFixture,
    RunningEventLoopFixture,
)
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
from maastesting.djangotestcase import count_queries
from maastesting.matchers import MockCalledOnceWith, MockNotCalled
from maastesting.twisted import always_fail_with, always_succeed_with
from netaddr import IPAddress, IPNetwork
from provisioningserver.rpc.cluster import (
    ConfigureDHCPv4,
    ConfigureDHCPv4_V2,
    ConfigureDHCPv6,
    ConfigureDHCPv6_V2,
    ValidateDHCPv4Config,
    ValidateDHCPv4Config_V2,
    ValidateDHCPv6Config,
    ValidateDHCPv6Config_V2,
)
from provisioningserver.rpc.dhcp import downgrade_shared_networks
from provisioningserver.rpc.exceptions import CannotConfigureDHCP
from provisioningserver.utils.twisted import synchronous
from testtools import ExpectedException
from testtools.matchers import (
    AllMatch,
    ContainsAll,
    ContainsDict,
    Equals,
    HasLength,
    IsInstance,
    MatchesAll,
    MatchesStructure,
    Not,
)
from twisted.internet import defer
from twisted.internet.defer import inlineCallbacks
from twisted.internet.threads import deferToThread


wait_for_reactor = wait_for(30)  # 30 seconds.


class TestGetOMAPIKey(MAASServerTestCase):
    """Tests for `get_omapi_key`."""

    def test__returns_key_in_global_config(self):
        key = factory.make_name("omapi")
        Config.objects.set_config("omapi_key", key)
        self.assertEqual(key, dhcp.get_omapi_key())

    def test__sets_new_omapi_key_in_global_config(self):
        key = factory.make_name("omapi")
        mock_generate_omapi_key = self.patch(dhcp, "generate_omapi_key")
        mock_generate_omapi_key.return_value = key
        self.assertEqual(key, dhcp.get_omapi_key())
        self.assertEqual(key, Config.objects.get_config("omapi_key"))
        self.assertThat(mock_generate_omapi_key, MockCalledOnceWith())


class TestSplitIPv4IPv6Subnets(MAASServerTestCase):
    """Tests for `split_ipv4_ipv6_subnets`."""

    def test__separates_IPv4_from_IPv6_subnets(self):
        ipv4_subnets = [
            factory.make_Subnet(cidr=str(factory.make_ipv4_network().cidr))
            for _ in range(random.randint(0, 2))
        ]
        ipv6_subnets = [
            factory.make_Subnet(cidr=str(factory.make_ipv6_network().cidr))
            for _ in range(random.randint(0, 2))
        ]
        subnets = sorted(
            ipv4_subnets + ipv6_subnets,
            key=lambda *args: random.randint(0, 10),
        )

        ipv4_result, ipv6_result = dhcp.split_managed_ipv4_ipv6_subnets(
            subnets
        )

        self.assertItemsEqual(ipv4_subnets, ipv4_result)
        self.assertItemsEqual(ipv6_subnets, ipv6_result)

    def test_skips_unmanaged_subnets(self):
        ipv4_subnets = [
            factory.make_Subnet(
                cidr=str(factory.make_ipv4_network().cidr),
                managed=random.choice([True, False]),
            )
            for _ in range(random.randint(0, 2))
        ]
        ipv6_subnets = [
            factory.make_Subnet(
                cidr=str(factory.make_ipv6_network().cidr),
                managed=random.choice([True, False]),
            )
            for _ in range(random.randint(0, 2))
        ]
        subnets = sorted(
            ipv4_subnets + ipv6_subnets,
            key=lambda *args: random.randint(0, 10),
        )

        ipv4_result, ipv6_result = dhcp.split_managed_ipv4_ipv6_subnets(
            subnets
        )

        self.assertItemsEqual(
            [s for s in ipv4_subnets if s.managed is True], ipv4_result
        )
        self.assertItemsEqual(
            [s for s in ipv6_subnets if s.managed is True], ipv6_result
        )


class TestIPIsStickyOrAuto(MAASServerTestCase):
    """Tests for `ip_is_sticky_or_auto`."""

    scenarios = (
        ("sticky", {"alloc_type": IPADDRESS_TYPE.STICKY, "result": True}),
        ("auto", {"alloc_type": IPADDRESS_TYPE.AUTO, "result": True}),
        (
            "discovered",
            {"alloc_type": IPADDRESS_TYPE.DISCOVERED, "result": False},
        ),
        (
            "user_reserved",
            {"alloc_type": IPADDRESS_TYPE.USER_RESERVED, "result": False},
        ),
    )

    def test__returns_correct_result(self):
        ip_address = factory.make_StaticIPAddress(alloc_type=self.alloc_type)
        self.assertEquals(self.result, dhcp.ip_is_sticky_or_auto(ip_address))


class TestGetBestInterface(MAASServerTestCase):
    """Tests for `get_best_interface`."""

    def test__returns_bond_over_physical(self):
        rack_controller = factory.make_RackController()
        physical = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller
        )
        nic0 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller
        )
        nic1 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller
        )
        bond = factory.make_Interface(
            INTERFACE_TYPE.BOND, node=rack_controller, parents=[nic0, nic1]
        )
        self.assertEquals(bond, dhcp.get_best_interface([physical, bond]))

    def test__returns_physical_over_vlan(self):
        rack_controller = factory.make_RackController()
        physical = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller
        )
        vlan = factory.make_Interface(
            INTERFACE_TYPE.VLAN, node=rack_controller, parents=[physical]
        )
        self.assertEquals(physical, dhcp.get_best_interface([physical, vlan]))

    def test__returns_first_interface_when_all_physical(self):
        rack_controller = factory.make_RackController()
        interfaces = [
            factory.make_Interface(
                INTERFACE_TYPE.PHYSICAL, node=rack_controller
            )
            for _ in range(3)
        ]
        self.assertEquals(interfaces[0], dhcp.get_best_interface(interfaces))

    def test__returns_first_interface_when_all_vlan(self):
        rack_controller = factory.make_RackController()
        physical = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller
        )
        interfaces = [
            factory.make_Interface(
                INTERFACE_TYPE.VLAN, node=rack_controller, parents=[physical]
            )
            for _ in range(3)
        ]
        self.assertEquals(interfaces[0], dhcp.get_best_interface(interfaces))


class TestGetInterfacesWithIPOnVLAN(MAASServerTestCase):
    """Tests for `get_interfaces_with_ip_on_vlan`."""

    def test__always_same_number_of_queries(self):
        rack_controller = factory.make_RackController()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(cidr="10.0.0.0/8", vlan=vlan)
        factory.make_IPRange(
            subnet=subnet, start_ip="10.0.1.0", end_ip="10.0.1.254"
        )
        factory.make_IPRange(
            subnet=subnet, start_ip="10.0.2.0", end_ip="10.0.2.254"
        )
        factory.make_IPRange(
            subnet=subnet, start_ip="10.0.3.0", end_ip="10.0.3.254"
        )
        # Make a multiple interfaces.
        for _ in range(10):
            interface = factory.make_Interface(
                INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
            )
            for _ in range(random.randint(1, 3)):
                factory.make_StaticIPAddress(
                    alloc_type=IPADDRESS_TYPE.AUTO,
                    ip=factory.pick_ip_in_Subnet(subnet),
                    subnet=subnet,
                    interface=interface,
                )
        query_10_count, _ = count_queries(
            dhcp.get_interfaces_with_ip_on_vlan,
            rack_controller,
            vlan,
            subnet.get_ipnetwork().version,
        )
        # Add more interfaces and count the queries again.
        for _ in range(10):
            interface = factory.make_Interface(
                INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
            )
            for _ in range(random.randint(1, 3)):
                factory.make_StaticIPAddress(
                    alloc_type=IPADDRESS_TYPE.AUTO,
                    ip=factory.pick_ip_in_Subnet(subnet),
                    subnet=subnet,
                    interface=interface,
                )
        query_20_count, _ = count_queries(
            dhcp.get_interfaces_with_ip_on_vlan,
            rack_controller,
            vlan,
            subnet.get_ipnetwork().version,
        )

        # This check is to notify the developer that a change was made that
        # affects the number of queries performed when performing this
        # operation. It is important to keep this number as low as possible.
        self.assertEqual(
            query_10_count,
            6,
            "Number of queries has changed; make sure this is expected.",
        )
        self.assertEqual(
            query_10_count,
            query_20_count,
            "Number of queries is not independent to the number of objects.",
        )

    def test__returns_interface_with_static_ip(self):
        rack_controller = factory.make_RackController()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, subnet=subnet, interface=interface
        )
        self.assertEquals(
            [interface],
            dhcp.get_interfaces_with_ip_on_vlan(
                rack_controller, vlan, subnet.get_ipnetwork().version
            ),
        )

    def test__returns_interfaces_with_ips(self):
        rack_controller = factory.make_RackController()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        interface_one = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=interface_one,
        )
        interface_two = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=interface_two,
        )
        self.assertItemsEqual(
            [interface_one, interface_two],
            dhcp.get_interfaces_with_ip_on_vlan(
                rack_controller, vlan, subnet.get_ipnetwork().version
            ),
        )

    def test__returns_interfaces_with_dynamic_ranges_first(self):
        rack_controller = factory.make_RackController()
        vlan = factory.make_VLAN()
        network = factory.make_ipv4_network()
        subnet = factory.make_Subnet(cidr=str(network.cidr), vlan=vlan)
        interface_one = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=interface_one,
        )
        interface_two = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        subnet_with_dynamic_range = factory.make_ipv4_Subnet_with_IPRanges(
            vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet_with_dynamic_range,
            interface=interface_two,
        )
        self.assertEquals(
            [interface_two, interface_one],
            dhcp.get_interfaces_with_ip_on_vlan(
                rack_controller, vlan, subnet.get_ipnetwork().version
            ),
        )

    def test__returns_interfaces_with_discovered_ips(self):
        rack_controller = factory.make_RackController()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        interface_one = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            subnet=subnet,
            interface=interface_one,
        )
        interface_two = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            subnet=subnet,
            interface=interface_two,
        )
        self.assertItemsEqual(
            [interface_one, interface_two],
            dhcp.get_interfaces_with_ip_on_vlan(
                rack_controller, vlan, subnet.get_ipnetwork().version
            ),
        )

    def test__returns_interfaces_with_static_over_discovered(self):
        rack_controller = factory.make_RackController()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        interface_one = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=interface_one,
        )
        interface_two = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            subnet=subnet,
            interface=interface_two,
        )
        self.assertItemsEqual(
            [interface_one],
            dhcp.get_interfaces_with_ip_on_vlan(
                rack_controller, vlan, subnet.get_ipnetwork().version
            ),
        )

    def test__returns_no_interfaces_if_ip_empty(self):
        rack_controller = factory.make_RackController()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        self.assertEquals(
            [],
            dhcp.get_interfaces_with_ip_on_vlan(
                rack_controller, vlan, subnet.get_ipnetwork().version
            ),
        )

    def test__returns_only_interfaces_on_vlan_ipv4(self):
        rack_controller = factory.make_RackController()
        vlan = factory.make_VLAN()
        network = factory.make_ipv4_network()
        subnet = factory.make_Subnet(cidr=str(network.cidr), vlan=vlan)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=subnet,
            interface=interface,
        )
        other_vlan = factory.make_VLAN()
        other_network = factory.make_ipv4_network()
        other_subnet = factory.make_Subnet(
            cidr=str(other_network.cidr), vlan=other_vlan
        )
        other_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=other_vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=other_subnet,
            interface=other_interface,
        )
        self.assertEquals(
            [interface],
            dhcp.get_interfaces_with_ip_on_vlan(
                rack_controller, vlan, subnet.get_ipnetwork().version
            ),
        )

    def test__returns_only_interfaces_on_vlan_ipv6(self):
        rack_controller = factory.make_RackController()
        vlan = factory.make_VLAN()
        network = factory.make_ipv6_network()
        subnet = factory.make_Subnet(cidr=str(network.cidr), vlan=vlan)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=subnet,
            interface=interface,
        )
        other_vlan = factory.make_VLAN()
        other_network = factory.make_ipv6_network()
        other_subnet = factory.make_Subnet(
            cidr=str(other_network.cidr), vlan=other_vlan
        )
        other_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=other_vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=other_subnet,
            interface=other_interface,
        )
        self.assertEquals(
            [interface],
            dhcp.get_interfaces_with_ip_on_vlan(
                rack_controller, vlan, subnet.get_ipnetwork().version
            ),
        )

    def test__returns_interface_with_static_ip_on_vlan_from_relay(self):
        rack_controller = factory.make_RackController()
        vlan = factory.make_VLAN()
        relayed_to_another = factory.make_VLAN(relay_vlan=vlan)
        subnet = factory.make_Subnet(vlan=vlan)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, subnet=subnet, interface=interface
        )
        self.assertEquals(
            [interface],
            dhcp.get_interfaces_with_ip_on_vlan(
                rack_controller,
                relayed_to_another,
                subnet.get_ipnetwork().version,
            ),
        )

    def test__returns_interfaces_with_discovered_ips_on_vlan_from_relay(self):
        rack_controller = factory.make_RackController()
        vlan = factory.make_VLAN()
        relayed_to_another = factory.make_VLAN(relay_vlan=vlan)
        subnet = factory.make_Subnet(vlan=vlan)
        interface_one = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            subnet=subnet,
            interface=interface_one,
        )
        interface_two = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            subnet=subnet,
            interface=interface_two,
        )
        self.assertItemsEqual(
            [interface_one, interface_two],
            dhcp.get_interfaces_with_ip_on_vlan(
                rack_controller,
                relayed_to_another,
                subnet.get_ipnetwork().version,
            ),
        )


class TestGenManagedVLANsFor(MAASServerTestCase):
    """Tests for `gen_managed_vlans_for`."""

    def test__returns_all_managed_vlans(self):
        rack_controller = factory.make_RackController()

        # Two interfaces on one IPv4 and one IPv6 subnet where the VLAN is
        # being managed by the rack controller as the primary.
        vlan_one = factory.make_VLAN(
            dhcp_on=True, primary_rack=rack_controller, name="1"
        )
        primary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan_one
        )
        bond_parent_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan_one
        )
        bond_interface = factory.make_Interface(
            INTERFACE_TYPE.BOND,
            node=rack_controller,
            parents=[bond_parent_interface],
            vlan=vlan_one,
        )
        managed_ipv4_subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr), vlan=vlan_one
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=managed_ipv4_subnet,
            interface=primary_interface,
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=managed_ipv4_subnet,
            interface=bond_interface,
        )
        managed_ipv6_subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv6_network().cidr), vlan=vlan_one
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=managed_ipv6_subnet,
            interface=primary_interface,
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=managed_ipv6_subnet,
            interface=bond_interface,
        )

        # Interface on one IPv4 and one IPv6 subnet where the VLAN is being
        # managed by the rack controller as the secondary.
        vlan_two = factory.make_VLAN(
            dhcp_on=True, secondary_rack=rack_controller, name="2"
        )
        secondary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan_two
        )
        sec_managed_ipv4_subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr), vlan=vlan_two
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=sec_managed_ipv4_subnet,
            interface=secondary_interface,
        )
        sec_managed_ipv6_subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv6_network().cidr), vlan=vlan_two
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=sec_managed_ipv6_subnet,
            interface=secondary_interface,
        )

        # Interface on one IPv4 and one IPv6 subnet where the VLAN is not
        # managed by the rack controller.
        vlan_three = factory.make_VLAN(dhcp_on=True, name="3")
        not_managed_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan_three
        )
        not_managed_ipv4_subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr), vlan=vlan_three
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=not_managed_ipv4_subnet,
            interface=not_managed_interface,
        )
        not_managed_ipv6_subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv6_network().cidr), vlan=vlan_three
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=not_managed_ipv6_subnet,
            interface=not_managed_interface,
        )

        # Interface on one IPv4 and one IPv6 subnet where the VLAN dhcp is off.
        vlan_four = factory.make_VLAN(dhcp_on=False, name="4")
        dhcp_off_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan_four
        )
        dhcp_off_ipv4_subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr), vlan=vlan_four
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=dhcp_off_ipv4_subnet,
            interface=dhcp_off_interface,
        )
        dhcp_off_ipv6_subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv6_network().cidr), vlan=vlan_four
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=dhcp_off_ipv6_subnet,
            interface=dhcp_off_interface,
        )

        # Should only contain the subnets that are managed by the rack
        # controller and the best interface should have been selected.
        self.assertEquals(
            {vlan_one, vlan_two},
            set(dhcp.gen_managed_vlans_for(rack_controller)),
        )

    def test__returns_managed_vlan_with_relay_vlans(self):
        rack_controller = factory.make_RackController()
        vlan_one = factory.make_VLAN(
            dhcp_on=True, primary_rack=rack_controller, name="1"
        )
        primary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan_one
        )
        managed_ipv4_subnet = factory.make_Subnet(
            cidr=str(factory.make_ipv4_network().cidr), vlan=vlan_one
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=managed_ipv4_subnet,
            interface=primary_interface,
        )

        # Relay VLANs atteched to the vlan.
        relay_vlans = {
            factory.make_VLAN(relay_vlan=vlan_one) for _ in range(3)
        }

        # Should only contain the subnets that are managed by the rack
        # controller and the best interface should have been selected.
        self.assertEquals(
            relay_vlans.union(set([vlan_one])),
            set(dhcp.gen_managed_vlans_for(rack_controller)),
        )


class TestIPIsOnVLAN(MAASServerTestCase):
    """Tests for `ip_is_on_vlan`."""

    scenarios = (
        (
            "sticky_on_vlan_with_ip",
            {
                "alloc_type": IPADDRESS_TYPE.STICKY,
                "has_ip": True,
                "on_vlan": True,
                "on_subnet": True,
                "result": True,
            },
        ),
        (
            "sticky_not_on_vlan_with_ip",
            {
                "alloc_type": IPADDRESS_TYPE.STICKY,
                "has_ip": True,
                "on_vlan": False,
                "on_subnet": True,
                "result": False,
            },
        ),
        (
            "auto_on_vlan_with_ip",
            {
                "alloc_type": IPADDRESS_TYPE.AUTO,
                "has_ip": True,
                "on_vlan": True,
                "on_subnet": True,
                "result": True,
            },
        ),
        (
            "auto_on_vlan_without_ip",
            {
                "alloc_type": IPADDRESS_TYPE.AUTO,
                "has_ip": False,
                "on_vlan": True,
                "on_subnet": True,
                "result": False,
            },
        ),
        (
            "auto_not_on_vlan_with_ip",
            {
                "alloc_type": IPADDRESS_TYPE.AUTO,
                "has_ip": True,
                "on_vlan": False,
                "on_subnet": True,
                "result": False,
            },
        ),
        (
            "discovered",
            {
                "alloc_type": IPADDRESS_TYPE.DISCOVERED,
                "has_ip": True,
                "on_vlan": True,
                "on_subnet": True,
                "result": False,
            },
        ),
        (
            "user_reserved",
            {
                "alloc_type": IPADDRESS_TYPE.USER_RESERVED,
                "has_ip": True,
                "on_vlan": True,
                "on_subnet": True,
                "result": False,
            },
        ),
        (
            "not_on_subnet",
            {
                "alloc_type": IPADDRESS_TYPE.STICKY,
                "has_ip": True,
                "on_vlan": False,
                "on_subnet": False,
                "result": False,
            },
        ),
    )

    def test__returns_correct_result(self):
        expected_vlan = factory.make_VLAN()
        set_vlan = expected_vlan
        if not self.on_vlan:
            set_vlan = factory.make_VLAN()
        ip = ""
        subnet = factory.make_Subnet(vlan=set_vlan)
        if self.has_ip:
            ip = factory.pick_ip_in_Subnet(subnet)
        ip_address = factory.make_StaticIPAddress(
            alloc_type=self.alloc_type, ip=ip, subnet=subnet
        )
        if not self.on_subnet:
            # make_StaticIPAddress always creates a subnet so set it to None.
            ip_address.subnet = None
            ip_address.save()
        self.assertEquals(
            self.result, dhcp.ip_is_on_vlan(ip_address, expected_vlan)
        )


class TestGetIPAddressForInterface(MAASServerTestCase):
    """Tests for `get_ip_address_for_interface`."""

    def test__returns_ip_address_on_vlan(self):
        vlan = factory.make_VLAN()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, vlan=vlan)
        subnet = factory.make_Subnet(vlan=vlan)
        ip_address = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, subnet=subnet, interface=interface
        )
        self.assertEquals(
            ip_address, dhcp.get_ip_address_for_interface(interface, vlan)
        )

    def test__returns_None(self):
        vlan = factory.make_VLAN()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, vlan=vlan)
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, subnet=subnet, interface=interface
        )
        self.assertIsNone(
            dhcp.get_ip_address_for_interface(interface, factory.make_VLAN())
        )


class TestGetIPAddressForRackController(MAASServerTestCase):
    """Tests for `get_ip_address_for_rack_controller`."""

    def test__returns_ip_address_for_rack_controller_on_vlan(self):
        vlan = factory.make_VLAN()
        rack_controller = factory.make_RackController()
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        subnet = factory.make_Subnet(vlan=vlan)
        ip_address = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, subnet=subnet, interface=interface
        )
        self.assertEquals(
            ip_address,
            dhcp.get_ip_address_for_rack_controller(rack_controller, vlan),
        )

    def test__returns_ip_address_from_best_interface_on_rack_controller(self):
        vlan = factory.make_VLAN()
        rack_controller = factory.make_RackController()
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        parent_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        bond_interface = factory.make_Interface(
            INTERFACE_TYPE.BOND,
            node=rack_controller,
            parents=[parent_interface],
            vlan=vlan,
        )
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, subnet=subnet, interface=interface
        )
        bond_ip_address = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=bond_interface,
        )
        self.assertEquals(
            bond_ip_address,
            dhcp.get_ip_address_for_rack_controller(rack_controller, vlan),
        )


class TestGetNTPServerAddressesForRack(MAASServerTestCase):
    """Tests for `get_ntp_server_addresses_for_rack`."""

    def test__returns_empty_dict_for_unconnected_rack(self):
        rack = factory.make_RackController()
        self.assertThat(
            dhcp.get_ntp_server_addresses_for_rack(rack), Equals({})
        )

    def test__returns_dict_with_rack_addresses(self):
        rack = factory.make_RackController()
        space = factory.make_Space()
        subnet = factory.make_Subnet(space=space)
        interface = factory.make_Interface(node=rack)
        address = factory.make_StaticIPAddress(
            interface=interface,
            subnet=subnet,
            alloc_type=IPADDRESS_TYPE.STICKY,
        )

        self.assertThat(
            dhcp.get_ntp_server_addresses_for_rack(rack),
            Equals({(space.id, subnet.get_ipnetwork().version): address.ip}),
        )

    def test__handles_blank_subnet(self):
        rack = factory.make_RackController()
        ip = factory.make_ip_address()
        interface = factory.make_Interface(node=rack)
        factory.make_StaticIPAddress(
            interface=interface, alloc_type=IPADDRESS_TYPE.USER_RESERVED, ip=ip
        )

        self.assertThat(
            dhcp.get_ntp_server_addresses_for_rack(rack), Equals({})
        )

    def test__returns_dict_grouped_by_space_and_address_family(self):
        rack = factory.make_RackController()
        space1 = factory.make_Space()
        space2 = factory.make_Space()
        subnet1 = factory.make_Subnet(space=space1)
        subnet2 = factory.make_Subnet(space=space2)
        interface = factory.make_Interface(node=rack)
        address1 = factory.make_StaticIPAddress(
            interface=interface,
            subnet=subnet1,
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        address2 = factory.make_StaticIPAddress(
            interface=interface,
            subnet=subnet2,
            alloc_type=IPADDRESS_TYPE.STICKY,
        )

        self.assertThat(
            dhcp.get_ntp_server_addresses_for_rack(rack),
            Equals(
                {
                    (space1.id, subnet1.get_ipnetwork().version): address1.ip,
                    (space2.id, subnet2.get_ipnetwork().version): address2.ip,
                }
            ),
        )

    def test__returned_dict_chooses_minimum_address(self):
        rack = factory.make_RackController()
        space = factory.make_Space()
        cidr = factory.make_ip4_or_6_network(host_bits=16)
        subnet = factory.make_Subnet(space=space, cidr=cidr)
        interface = factory.make_Interface(node=rack)
        addresses = {
            factory.make_StaticIPAddress(
                interface=interface,
                subnet=subnet,
                alloc_type=IPADDRESS_TYPE.STICKY,
            )
            for _ in range(10)
        }

        self.assertThat(
            dhcp.get_ntp_server_addresses_for_rack(rack),
            Equals(
                {
                    (space.id, subnet.get_ipnetwork().version): min(
                        (address.ip for address in addresses), key=IPAddress
                    )
                }
            ),
        )

    def test__returned_dict_prefers_vlans_with_dhcp_on(self):
        rack = factory.make_RackController()
        space = factory.make_Space()
        ip_version = random.choice([4, 6])
        cidr1 = factory.make_ip4_or_6_network(version=ip_version, host_bits=16)
        cidr2 = factory.make_ip4_or_6_network(version=ip_version, host_bits=16)
        subnet1 = factory.make_Subnet(space=space, cidr=cidr1)
        subnet2 = factory.make_Subnet(space=space, cidr=cidr2)
        # Expect subnet2 to be selected, since DHCP is enabled.
        subnet2.vlan.dhcp_on = True
        subnet2.vlan.save()
        interface = factory.make_Interface(node=rack)
        # Make some addresses that won't be selected since they're on the
        # incorrect VLAN (without DHCP enabled).
        for _ in range(3):
            factory.make_StaticIPAddress(
                interface=interface,
                subnet=subnet1,
                alloc_type=IPADDRESS_TYPE.STICKY,
            )
        expected_address = factory.make_StaticIPAddress(
            interface=interface,
            subnet=subnet2,
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        self.assertThat(
            dhcp.get_ntp_server_addresses_for_rack(rack),
            Equals(
                {
                    (
                        space.id,
                        subnet2.get_ipnetwork().version,
                    ): expected_address.ip
                }
            ),
        )

    def test__constant_query_count(self):
        rack = factory.make_RackController()
        interface = factory.make_Interface(node=rack)

        count, result = count_queries(
            dhcp.get_ntp_server_addresses_for_rack, rack
        )
        self.assertThat(count, Equals(1))
        self.assertThat(result, Equals({}))

        for _ in (1, 2):
            space = factory.make_Space()
            for family in (4, 6):
                cidr = factory.make_ip4_or_6_network(family, host_bits=8)
                subnet = factory.make_Subnet(space=space, cidr=cidr)
                for _ in (1, 2):
                    factory.make_StaticIPAddress(
                        interface=interface,
                        subnet=subnet,
                        alloc_type=IPADDRESS_TYPE.STICKY,
                    )

        count, result = count_queries(
            dhcp.get_ntp_server_addresses_for_rack, rack
        )
        self.assertThat(count, Equals(1))
        self.assertThat(result, Not(Equals({})))


class TestGetDefaultDNSServers(MAASServerTestCase):
    """Tests for `get_default_dns_servers`."""

    def test__returns_default_region_ip_if_no_url_found(self):
        mock_get_source_address = self.patch(dhcp_module, "get_source_address")
        mock_get_source_address.return_value = "10.0.0.1"
        vlan = factory.make_VLAN()
        rack_controller = factory.make_RackController(interface=False, url="")
        subnet = factory.make_Subnet(vlan=vlan, cidr="10.0.0.0/24")
        servers = get_default_dns_servers(rack_controller, subnet)
        self.assertThat(servers, Equals([IPAddress("10.0.0.1")]))

    def test__returns_address_from_region_url_if_url_specified(self):
        mock_get_source_address = self.patch(dhcp_module, "get_source_address")
        mock_get_source_address.return_value = "10.0.0.1"
        vlan = factory.make_VLAN()
        rack_controller = factory.make_RackController(
            interface=False, url="http://192.168.0.1:5240/MAAS/"
        )
        subnet = factory.make_Subnet(vlan=vlan, cidr="10.0.0.0/24")
        servers = get_default_dns_servers(rack_controller, subnet)
        self.assertThat(servers, Equals([IPAddress("192.168.0.1")]))

    def test__chooses_alternate_from_known_reachable_subnet_no_proxy(self):
        mock_get_source_address = self.patch(dhcp_module, "get_source_address")
        mock_get_source_address.return_value = "10.0.0.1"
        vlan = factory.make_VLAN()
        r1 = factory.make_RegionRackController(interface=False)
        mock_get_maas_id = self.patch(server_address_module, "get_maas_id")
        mock_get_maas_id.return_value = r1.system_id
        r2 = factory.make_RegionRackController(interface=False)
        subnet = factory.make_Subnet(vlan=vlan, cidr="10.0.0.0/24")
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=r2
        )
        address = factory.make_StaticIPAddress(
            interface=interface,
            subnet=subnet,
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        servers = get_default_dns_servers(r1, subnet, False)
        self.assertThat(
            servers, Equals([IPAddress("10.0.0.1"), IPAddress(address.ip)])
        )

    def test__racks_on_subnet_comes_before_region(self):
        mock_get_source_address = self.patch(dhcp_module, "get_source_address")
        mock_get_source_address.return_value = "10.0.0.1"
        vlan = factory.make_VLAN()
        r1 = factory.make_RegionRackController(interface=False)
        mock_get_maas_id = self.patch(server_address_module, "get_maas_id")
        mock_get_maas_id.return_value = r1.system_id
        subnet = factory.make_Subnet(vlan=vlan, cidr="10.0.0.0/24")
        r1_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=r1
        )
        r1_address = factory.make_StaticIPAddress(
            interface=r1_interface,
            subnet=subnet,
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        r2 = factory.make_RegionRackController(interface=False)
        r2_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=r2
        )
        r2_address = factory.make_StaticIPAddress(
            interface=r2_interface,
            subnet=subnet,
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        servers = get_default_dns_servers(r1, subnet)
        self.assertThat(
            servers,
            Equals(
                [
                    IPAddress(r1_address.ip),
                    IPAddress(r2_address.ip),
                    IPAddress("10.0.0.1"),
                ]
            ),
        )


class TestMakeSubnetConfig(MAASServerTestCase):
    """Tests for `make_subnet_config`."""

    def test__includes_all_parameters(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller,
            subnet,
            [factory.make_name("dns")],
            [factory.make_name("ntp")],
            default_domain,
            search_list=default_domain.name,
        )
        self.assertIsInstance(config, dict)
        self.assertThat(
            config.keys(),
            ContainsAll(
                [
                    "subnet",
                    "subnet_mask",
                    "subnet_cidr",
                    "broadcast_ip",
                    "router_ip",
                    "dns_servers",
                    "ntp_servers",
                    "domain_name",
                    "search_list",
                    "pools",
                    "dhcp_snippets",
                ]
            ),
        )

    def test__sets_ipv4_dns_from_arguments(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan, dns_servers=[], version=4)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        maas_dns = IPAddress(factory.make_ipv4_address())
        ntp_servers = [factory.make_name("ntp")]
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller, subnet, [maas_dns], ntp_servers, default_domain
        )
        self.assertThat(config["dns_servers"], Equals([maas_dns]))

    def test__sets_ipv6_dns_from_arguments(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan, dns_servers=[], version=6)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        maas_dns = IPAddress(factory.make_ipv6_address())
        ntp_servers = [factory.make_name("ntp")]
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller, subnet, [maas_dns], ntp_servers, default_domain
        )
        self.assertThat(config["dns_servers"], Equals([maas_dns]))

    def test__sets_ntp_from_list_argument(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan, dns_servers=[])
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        ntp_servers = [factory.make_name("ntp")]
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller, subnet, [""], ntp_servers, default_domain
        )
        self.expectThat(config["ntp_servers"], Equals(ntp_servers))

    def test__sets_ntp_from_empty_dict_argument(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan, dns_servers=[])
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller, subnet, [""], {}, default_domain
        )
        self.expectThat(config["ntp_servers"], Equals([]))

    def test__sets_ntp_from_dict_argument(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan, dns_servers=[], space=None)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        address = factory.make_StaticIPAddress(
            interface=interface,
            subnet=subnet,
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        ntp_servers = {
            (vlan.space_id, subnet.get_ipnetwork().version): address.ip
        }
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller, subnet, [""], ntp_servers, default_domain
        )
        self.expectThat(config["ntp_servers"], Equals([address.ip]))

    def test__sets_ntp_from_dict_argument_with_alternates(self):
        r1 = factory.make_RackController(interface=False)
        r2 = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN(primary_rack=r1, secondary_rack=r2)
        subnet = factory.make_Subnet(vlan=vlan, dns_servers=[], space=None)
        r1_eth0 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=r1
        )
        r2_eth0 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=r2
        )
        a1 = factory.make_StaticIPAddress(
            interface=r1_eth0, subnet=subnet, alloc_type=IPADDRESS_TYPE.STICKY
        )
        a2 = factory.make_StaticIPAddress(
            interface=r2_eth0, subnet=subnet, alloc_type=IPADDRESS_TYPE.STICKY
        )
        r1_ntp_servers = {
            (vlan.space_id, subnet.get_ipnetwork().version): a1.ip
        }
        r2_ntp_servers = {
            (vlan.space_id, subnet.get_ipnetwork().version): a2.ip
        }
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            r1, subnet, [""], r1_ntp_servers, default_domain, peer_rack=r2
        )
        self.expectThat(config["ntp_servers"], Equals([a1.ip, a2.ip]))
        config = dhcp.make_subnet_config(
            r2, subnet, [""], r2_ntp_servers, default_domain, peer_rack=r1
        )
        self.expectThat(config["ntp_servers"], Equals([a2.ip, a1.ip]))

    def test__overrides_ipv4_dns_from_subnet(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan, version=4)
        maas_dns = factory.make_ipv4_address()
        subnet_dns_servers = ["8.8.8.8", "8.8.4.4"]
        subnet.dns_servers = subnet_dns_servers
        subnet.save()
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        ntp_servers = [factory.make_name("ntp")]
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller, subnet, [maas_dns], ntp_servers, default_domain
        )
        self.assertThat(
            config["dns_servers"],
            Equals([IPAddress(addr) for addr in subnet_dns_servers]),
        )

    def test__overrides_ipv6_dns_from_subnet(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan, version=6)
        maas_dns = factory.make_ipv6_address()
        subnet_dns_servers = ["2001:db8::1", "2001:db8::2"]
        subnet.dns_servers = subnet_dns_servers
        subnet.save()
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        ntp_servers = [factory.make_name("ntp")]
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller, subnet, [maas_dns], ntp_servers, default_domain
        )
        self.assertThat(
            config["dns_servers"],
            Equals([IPAddress(addr) for addr in subnet_dns_servers]),
        )

    def test__sets_domain_name_from_passed_domain(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller,
            subnet,
            [factory.make_name("dns")],
            [factory.make_name("ntp")],
            default_domain,
        )
        self.expectThat(config["domain_name"], Equals(default_domain.name))

    def test__sets_other_items_from_subnet_and_interface(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller,
            subnet,
            [factory.make_name("dns")],
            [factory.make_name("ntp")],
            default_domain,
        )
        self.expectThat(
            config["broadcast_ip"],
            Equals(str(subnet.get_ipnetwork().broadcast)),
        )
        self.expectThat(config["router_ip"], Equals(subnet.gateway_ip))

    def test__passes_IP_addresses_as_strings(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller,
            subnet,
            [factory.make_name("dns")],
            [factory.make_name("ntp")],
            default_domain,
        )
        self.expectThat(config["subnet"], IsInstance(str))
        self.expectThat(config["subnet_mask"], IsInstance(str))
        self.expectThat(config["subnet_cidr"], IsInstance(str))
        self.expectThat(config["broadcast_ip"], IsInstance(str))
        self.expectThat(config["router_ip"], IsInstance(str))

    def test__defines_IPv4_subnet(self):
        network = IPNetwork("10.9.8.7/24")
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(cidr=str(network.cidr), vlan=vlan)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        default_domain = Domain.objects.get_default_domain()
        search_list = [default_domain.name, "foo.example.com"]
        config = dhcp.make_subnet_config(
            rack_controller,
            subnet,
            [factory.make_name("dns")],
            [factory.make_name("ntp")],
            default_domain,
            search_list=search_list,
        )
        self.expectThat(config["subnet"], Equals("10.9.8.0"))
        self.expectThat(config["subnet_mask"], Equals("255.255.255.0"))
        self.expectThat(config["subnet_cidr"], Equals("10.9.8.0/24"))
        self.expectThat(config["broadcast_ip"], Equals("10.9.8.255"))
        self.expectThat(config["domain_name"], Equals(default_domain.name))
        self.expectThat(
            config["search_list"],
            Equals([default_domain.name, "foo.example.com"]),
        )

    def test__defines_IPv6_subnet(self):
        network = IPNetwork("fd38:c341:27da:c831::/64")
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(cidr=str(network.cidr), vlan=vlan)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        default_domain = Domain.objects.get_default_domain()
        search_list = [default_domain.name, "foo.example.com"]
        config = dhcp.make_subnet_config(
            rack_controller,
            subnet,
            [factory.make_name("dns")],
            [factory.make_name("ntp")],
            default_domain,
            search_list=search_list,
        )
        # Don't expect a specific literal value, like we do for IPv4; there
        # are different spellings.
        self.expectThat(
            IPAddress(config["subnet"]),
            Equals(IPAddress("fd38:c341:27da:c831::")),
        )
        # (Netmask is not used for the IPv6 config, so ignore it.)
        self.expectThat(
            IPNetwork(config["subnet_cidr"]),
            Equals(IPNetwork("fd38:c341:27da:c831::/64")),
        )
        self.expectThat(
            config["search_list"],
            Equals([default_domain.name, "foo.example.com"]),
        )

    def test__returns_multiple_pools(self):
        network = IPNetwork("10.9.8.0/24")
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(cidr=str(network.cidr), vlan=vlan)
        factory.make_IPRange(subnet, "10.9.8.11", "10.9.8.20")
        factory.make_IPRange(subnet, "10.9.8.21", "10.9.8.30")
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        default_domain = Domain.objects.get_default_domain()
        config = dhcp.make_subnet_config(
            rack_controller,
            subnet,
            [factory.make_name("dns")],
            [factory.make_name("ntp")],
            default_domain,
        )
        self.assertEquals(
            [
                {"ip_range_low": "10.9.8.11", "ip_range_high": "10.9.8.20"},
                {"ip_range_low": "10.9.8.21", "ip_range_high": "10.9.8.30"},
            ],
            config["pools"],
        )
        self.expectThat(config["domain_name"], Equals(default_domain.name))

    def test__returns_multiple_pools_with_failover_peer(self):
        network = IPNetwork("10.9.8.0/24")
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(cidr=str(network.cidr), vlan=vlan)
        factory.make_IPRange(subnet, "10.9.8.11", "10.9.8.20")
        factory.make_IPRange(subnet, "10.9.8.21", "10.9.8.30")
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        default_domain = Domain.objects.get_default_domain()
        failover_peer = factory.make_name("peer")
        config = dhcp.make_subnet_config(
            rack_controller,
            subnet,
            [factory.make_name("dns")],
            [factory.make_name("ntp")],
            default_domain,
            failover_peer=failover_peer,
        )
        self.assertEquals(
            [
                {
                    "ip_range_low": "10.9.8.11",
                    "ip_range_high": "10.9.8.20",
                    "failover_peer": failover_peer,
                },
                {
                    "ip_range_low": "10.9.8.21",
                    "ip_range_high": "10.9.8.30",
                    "failover_peer": failover_peer,
                },
            ],
            config["pools"],
        )

    def test__doesnt_convert_None_router_ip(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        default_domain = Domain.objects.get_default_domain()
        subnet.gateway_ip = None
        subnet.save()
        config = dhcp.make_subnet_config(
            rack_controller,
            subnet,
            [factory.make_name("dns")],
            [factory.make_name("ntp")],
            default_domain,
        )
        self.assertEqual("", config["router_ip"])

    def test__returns_dhcp_snippets(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        default_domain = Domain.objects.get_default_domain()
        dhcp_snippets = [
            factory.make_DHCPSnippet(subnet=subnet, enabled=True)
            for _ in range(3)
        ]
        config = dhcp.make_subnet_config(
            rack_controller,
            subnet,
            [factory.make_name("dns")],
            [factory.make_name("ntp")],
            default_domain,
            subnets_dhcp_snippets=dhcp_snippets,
        )
        self.assertItemsEqual(
            [
                {
                    "name": dhcp_snippet.name,
                    "description": dhcp_snippet.description,
                    "value": dhcp_snippet.value.data,
                }
                for dhcp_snippet in dhcp_snippets
            ],
            config["dhcp_snippets"],
        )


class TestMakeHostsForSubnet(MAASServerTestCase):
    def tests__returns_defined_hosts(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        node = factory.make_Node(interface=False)

        # Make AUTO IP without an IP. Should not be in output.
        auto_no_ip_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, vlan=subnet.vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            ip="",
            subnet=subnet,
            interface=auto_no_ip_interface,
        )

        # Make AUTO IP with an IP. Should be in the output.
        auto_with_ip_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, vlan=subnet.vlan
        )
        auto_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=auto_with_ip_interface,
        )

        # Make temp AUTO IP with an IP. Should not be in the output.
        auto_no_temp_ip_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, vlan=subnet.vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=auto_no_temp_ip_interface,
            temp_expires_on=datetime.utcnow(),
        )

        # Make STICKY IP. Should be in the output.
        sticky_ip_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, vlan=subnet.vlan
        )
        sticky_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=subnet,
            interface=sticky_ip_interface,
        )

        # Make DISCOVERED IP. Should not be in the output.
        discovered_ip_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, vlan=subnet.vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            subnet=subnet,
            interface=discovered_ip_interface,
        )

        # Make USER_RESERVED IP on Device. Should be in the output.
        device = factory.make_Device(interface=False)
        device_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=device, vlan=subnet.vlan
        )
        device_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.USER_RESERVED,
            subnet=subnet,
            interface=device_interface,
        )

        # Make USER_RESERVED IP on Unknown interface. Should be in the output.
        unknown_interface = factory.make_Interface(
            INTERFACE_TYPE.UNKNOWN, vlan=subnet.vlan
        )
        unknown_reserved_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.USER_RESERVED,
            subnet=subnet,
            interface=unknown_interface,
        )

        # Add DHCP some DHCP snippets
        node_dhcp_snippets = [
            factory.make_DHCPSnippet(node=node, enabled=True) for _ in range(3)
        ]
        device_dhcp_snippets = [
            factory.make_DHCPSnippet(node=device, enabled=True)
            for _ in range(3)
        ]

        expected_hosts = [
            {
                "host": "%s-%s" % (node.hostname, auto_with_ip_interface.name),
                "mac": str(auto_with_ip_interface.mac_address),
                "ip": str(auto_ip.ip),
                "dhcp_snippets": [
                    {
                        "name": dhcp_snippet.name,
                        "description": dhcp_snippet.description,
                        "value": dhcp_snippet.value.data,
                    }
                    for dhcp_snippet in node_dhcp_snippets
                ],
            },
            {
                "host": "%s-%s" % (node.hostname, sticky_ip_interface.name),
                "mac": str(sticky_ip_interface.mac_address),
                "ip": str(sticky_ip.ip),
                "dhcp_snippets": [
                    {
                        "name": dhcp_snippet.name,
                        "description": dhcp_snippet.description,
                        "value": dhcp_snippet.value.data,
                    }
                    for dhcp_snippet in node_dhcp_snippets
                ],
            },
            {
                "host": "%s-%s" % (device.hostname, device_interface.name),
                "mac": str(device_interface.mac_address),
                "ip": str(device_ip.ip),
                "dhcp_snippets": [
                    {
                        "name": dhcp_snippet.name,
                        "description": dhcp_snippet.description,
                        "value": dhcp_snippet.value.data,
                    }
                    for dhcp_snippet in device_dhcp_snippets
                ],
            },
            {
                "host": "unknown-%s-%s"
                % (unknown_interface.id, unknown_interface.name),
                "mac": str(unknown_interface.mac_address),
                "ip": str(unknown_reserved_ip.ip),
                "dhcp_snippets": [],
            },
        ]
        self.assertItemsEqual(
            expected_hosts,
            dhcp.make_hosts_for_subnets(
                [subnet], node_dhcp_snippets + device_dhcp_snippets
            ),
        )

    def tests__returns_hosts_interface_once_when_on_multiple_subnets(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        node = factory.make_Node(interface=False)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=node
        )
        subnet_one = factory.make_Subnet(vlan=vlan)
        ip_one = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet_one,
            interface=interface,
        )
        subnet_two = factory.make_Subnet(vlan=vlan)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet_two,
            interface=interface,
        )

        expected_hosts = [
            {
                "host": "%s-%s" % (node.hostname, interface.name),
                "mac": str(interface.mac_address),
                "ip": str(ip_one.ip),
                "dhcp_snippets": [],
            }
        ]
        self.assertItemsEqual(
            expected_hosts,
            dhcp.make_hosts_for_subnets([subnet_one, subnet_two]),
        )

    def tests__returns_hosts_for_bond(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        node = factory.make_Node(interface=False)

        # Create a bond with an IP address, to make sure all MAC address in
        # that bond get the same address.
        eth0 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, name="eth0", vlan=vlan
        )
        eth1 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, name="eth1", vlan=vlan
        )
        eth2 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, name="eth2", vlan=vlan
        )
        bond0 = factory.make_Interface(
            INTERFACE_TYPE.BOND,
            node=node,
            name="bond0",
            mac_address=eth2.mac_address,
            parents=[eth0, eth1, eth2],
            vlan=vlan,
        )
        auto_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, subnet=subnet, interface=bond0
        )

        expected_hosts = [
            {
                "host": "%s-bond0" % node.hostname,
                "mac": str(bond0.mac_address),
                "ip": str(auto_ip.ip),
                "dhcp_snippets": [],
            },
            {
                "host": "%s-eth0" % node.hostname,
                "mac": str(eth0.mac_address),
                "ip": str(auto_ip.ip),
                "dhcp_snippets": [],
            },
            {
                "host": "%s-eth1" % node.hostname,
                "mac": str(eth1.mac_address),
                "ip": str(auto_ip.ip),
                "dhcp_snippets": [],
            },
        ]

        self.assertItemsEqual(
            expected_hosts, dhcp.make_hosts_for_subnets([subnet])
        )

    def tests__returns_hosts_first_created_ip_address(self):
        rack_controller = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=rack_controller
        )
        node = factory.make_Node(interface=False)

        # Add two IP address to interface. Only the first should be added.
        eth0 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, vlan=vlan
        )
        auto_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, subnet=subnet, interface=eth0
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, subnet=subnet, interface=eth0
        )

        expected_hosts = [
            {
                "host": "%s-%s" % (node.hostname, eth0.name),
                "mac": str(eth0.mac_address),
                "ip": str(auto_ip.ip),
                "dhcp_snippets": [],
            }
        ]

        self.assertEqual(expected_hosts, dhcp.make_hosts_for_subnets([subnet]))


class TestMakeFailoverPeerConfig(MAASServerTestCase):
    """Tests for `make_failover_peer_config`."""

    def test__renders_config_for_primary(self):
        primary_rack = factory.make_RackController()
        secondary_rack = factory.make_RackController()
        vlan = factory.make_VLAN(
            dhcp_on=True,
            primary_rack=primary_rack,
            secondary_rack=secondary_rack,
        )
        subnet = factory.make_Subnet(vlan=vlan)
        primary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=primary_rack, vlan=vlan
        )
        primary_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=primary_interface,
        )
        secondary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=secondary_rack, vlan=vlan
        )
        secondary_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=secondary_interface,
        )
        failover_peer_name = "failover-vlan-%d" % vlan.id
        self.assertEquals(
            (
                failover_peer_name,
                {
                    "name": failover_peer_name,
                    "mode": "primary",
                    "address": str(primary_ip.ip),
                    "peer_address": str(secondary_ip.ip),
                },
                secondary_rack,
            ),
            dhcp.make_failover_peer_config(vlan, primary_rack),
        )

    def test__renders_config_for_secondary(self):
        primary_rack = factory.make_RackController()
        secondary_rack = factory.make_RackController()
        vlan = factory.make_VLAN(
            dhcp_on=True,
            primary_rack=primary_rack,
            secondary_rack=secondary_rack,
        )
        subnet = factory.make_Subnet(vlan=vlan)
        primary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=primary_rack, vlan=vlan
        )
        primary_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=primary_interface,
        )
        secondary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=secondary_rack, vlan=vlan
        )
        secondary_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=secondary_interface,
        )
        failover_peer_name = "failover-vlan-%d" % vlan.id
        self.assertEquals(
            (
                failover_peer_name,
                {
                    "name": failover_peer_name,
                    "mode": "secondary",
                    "address": str(secondary_ip.ip),
                    "peer_address": str(primary_ip.ip),
                },
                primary_rack,
            ),
            dhcp.make_failover_peer_config(vlan, secondary_rack),
        )


class TestGetDHCPConfigureFor(MAASServerTestCase):
    """Tests for `get_dhcp_configure_for`."""

    def test__returns_for_ipv4(self):
        primary_rack = factory.make_RackController()
        secondary_rack = factory.make_RackController()

        # VLAN for primary that has a secondary with multiple subnets.
        ha_vlan = factory.make_VLAN(
            dhcp_on=True,
            primary_rack=primary_rack,
            secondary_rack=secondary_rack,
        )
        ha_subnet = factory.make_ipv4_Subnet_with_IPRanges(
            vlan=ha_vlan, dns_servers=["127.0.0.1"]
        )
        ha_network = ha_subnet.get_ipnetwork()
        ha_dhcp_snippets = [
            factory.make_DHCPSnippet(subnet=ha_subnet, enabled=True)
            for _ in range(3)
        ]
        primary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=primary_rack, vlan=ha_vlan
        )
        primary_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=ha_subnet,
            interface=primary_interface,
        )
        secondary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=secondary_rack, vlan=ha_vlan
        )
        secondary_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=ha_subnet,
            interface=secondary_interface,
        )
        other_subnet = factory.make_ipv4_Subnet_with_IPRanges(
            vlan=ha_vlan, dns_servers=["127.0.0.1"]
        )
        other_network = other_subnet.get_ipnetwork()
        other_dhcp_snippets = [
            factory.make_DHCPSnippet(subnet=other_subnet, enabled=True)
            for _ in range(3)
        ]

        ntp_servers = [factory.make_name("ntp")]
        default_domain = Domain.objects.get_default_domain()
        search_list = [default_domain.name, "foo.example.com"]
        (
            observed_failover,
            observed_subnets,
            observed_hosts,
            observed_interface,
        ) = dhcp.get_dhcp_configure_for(
            4,
            primary_rack,
            ha_vlan,
            [ha_subnet, other_subnet],
            ntp_servers,
            default_domain,
            search_list=search_list,
            dhcp_snippets=DHCPSnippet.objects.all(),
        )

        self.assertEquals(
            {
                "name": "failover-vlan-%d" % ha_vlan.id,
                "mode": "primary",
                "address": str(primary_ip.ip),
                "peer_address": str(secondary_ip.ip),
            },
            observed_failover,
        )
        self.assertEquals(
            sorted(
                [
                    {
                        "subnet": str(ha_network.network),
                        "subnet_mask": str(ha_network.netmask),
                        "subnet_cidr": str(ha_network.cidr),
                        "broadcast_ip": str(ha_network.broadcast),
                        "router_ip": str(ha_subnet.gateway_ip),
                        "dns_servers": [IPAddress("127.0.0.1")],
                        "ntp_servers": ntp_servers,
                        "domain_name": default_domain.name,
                        "search_list": [
                            default_domain.name,
                            "foo.example.com",
                        ],
                        "dhcp_snippets": [
                            {
                                "name": dhcp_snippet.name,
                                "description": dhcp_snippet.description,
                                "value": dhcp_snippet.value.data,
                            }
                            for dhcp_snippet in ha_dhcp_snippets
                        ],
                        "pools": [
                            {
                                "ip_range_low": str(ip_range.start_ip),
                                "ip_range_high": str(ip_range.end_ip),
                                "failover_peer": "failover-vlan-%d"
                                % ha_vlan.id,
                            }
                            for ip_range in (
                                ha_subnet.get_dynamic_ranges().order_by("id")
                            )
                        ],
                    },
                    {
                        "subnet": str(other_network.network),
                        "subnet_mask": str(other_network.netmask),
                        "subnet_cidr": str(other_network.cidr),
                        "broadcast_ip": str(other_network.broadcast),
                        "router_ip": str(other_subnet.gateway_ip),
                        "dns_servers": [IPAddress("127.0.0.1")],
                        "ntp_servers": ntp_servers,
                        "domain_name": default_domain.name,
                        "search_list": [
                            default_domain.name,
                            "foo.example.com",
                        ],
                        "dhcp_snippets": [
                            {
                                "name": dhcp_snippet.name,
                                "description": dhcp_snippet.description,
                                "value": dhcp_snippet.value.data,
                            }
                            for dhcp_snippet in other_dhcp_snippets
                        ],
                        "pools": [
                            {
                                "ip_range_low": str(ip_range.start_ip),
                                "ip_range_high": str(ip_range.end_ip),
                                "failover_peer": "failover-vlan-%d"
                                % ha_vlan.id,
                            }
                            for ip_range in (
                                other_subnet.get_dynamic_ranges().order_by(
                                    "id"
                                )
                            )
                        ],
                    },
                ],
                key=itemgetter("subnet"),
            ),
            observed_subnets,
        )
        self.assertItemsEqual(
            dhcp.make_hosts_for_subnets([ha_subnet]), observed_hosts
        )
        self.assertEqual(primary_interface.name, observed_interface)

    def test__returns_for_ipv6(self):
        primary_rack = factory.make_RackController()
        secondary_rack = factory.make_RackController()

        # VLAN for primary that has a secondary with multiple subnets.
        ha_vlan = factory.make_VLAN(
            dhcp_on=True,
            primary_rack=primary_rack,
            secondary_rack=secondary_rack,
        )
        ha_subnet = factory.make_Subnet(
            vlan=ha_vlan, cidr="fd38:c341:27da:c831::/64"
        )
        ha_network = ha_subnet.get_ipnetwork()
        factory.make_IPRange(
            ha_subnet,
            "fd38:c341:27da:c831:0:1::",
            "fd38:c341:27da:c831:0:1:ffff:0",
        )
        ha_dhcp_snippets = [
            factory.make_DHCPSnippet(subnet=ha_subnet, enabled=True)
            for _ in range(3)
        ]
        primary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=primary_rack, vlan=ha_vlan
        )
        primary_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=ha_subnet,
            interface=primary_interface,
        )
        secondary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=secondary_rack, vlan=ha_vlan
        )
        secondary_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=ha_subnet,
            interface=secondary_interface,
        )
        other_subnet = factory.make_Subnet(
            vlan=ha_vlan, cidr="fd38:c341:27da:c832::/64"
        )
        other_network = other_subnet.get_ipnetwork()
        other_dhcp_snippets = [
            factory.make_DHCPSnippet(subnet=other_subnet, enabled=True)
            for _ in range(3)
        ]

        ntp_servers = [factory.make_name("ntp")]
        default_domain = Domain.objects.get_default_domain()
        (
            observed_failover,
            observed_subnets,
            observed_hosts,
            observed_interface,
        ) = dhcp.get_dhcp_configure_for(
            6,
            primary_rack,
            ha_vlan,
            [ha_subnet, other_subnet],
            ntp_servers,
            default_domain,
            dhcp_snippets=DHCPSnippet.objects.all(),
        )

        # Because developers running this unit test might not have an IPv6
        # address configured we remove the dns_servers from the generated
        # config.
        for observed_subnet in observed_subnets:
            del observed_subnet["dns_servers"]

        self.assertEquals(
            {
                "name": "failover-vlan-%d" % ha_vlan.id,
                "mode": "primary",
                "address": str(primary_ip.ip),
                "peer_address": str(secondary_ip.ip),
            },
            observed_failover,
        )
        self.assertEquals(
            sorted(
                [
                    {
                        "subnet": str(ha_network.network),
                        "subnet_mask": str(ha_network.netmask),
                        "subnet_cidr": str(ha_network.cidr),
                        "broadcast_ip": str(ha_network.broadcast),
                        "router_ip": str(ha_subnet.gateway_ip),
                        "ntp_servers": ntp_servers,
                        "domain_name": default_domain.name,
                        "dhcp_snippets": [
                            {
                                "name": dhcp_snippet.name,
                                "description": dhcp_snippet.description,
                                "value": dhcp_snippet.value.data,
                            }
                            for dhcp_snippet in ha_dhcp_snippets
                        ],
                        "pools": [
                            {
                                "ip_range_low": str(ip_range.start_ip),
                                "ip_range_high": str(ip_range.end_ip),
                                "failover_peer": "failover-vlan-%d"
                                % ha_vlan.id,
                            }
                            for ip_range in (
                                ha_subnet.get_dynamic_ranges().order_by("id")
                            )
                        ],
                    },
                    {
                        "subnet": str(other_network.network),
                        "subnet_mask": str(other_network.netmask),
                        "subnet_cidr": str(other_network.cidr),
                        "broadcast_ip": str(other_network.broadcast),
                        "router_ip": str(other_subnet.gateway_ip),
                        "ntp_servers": ntp_servers,
                        "domain_name": default_domain.name,
                        "dhcp_snippets": [
                            {
                                "name": dhcp_snippet.name,
                                "description": dhcp_snippet.description,
                                "value": dhcp_snippet.value.data,
                            }
                            for dhcp_snippet in other_dhcp_snippets
                        ],
                        "pools": [
                            {
                                "ip_range_low": str(ip_range.start_ip),
                                "ip_range_high": str(ip_range.end_ip),
                                "failover_peer": "failover-vlan-%d"
                                % ha_vlan.id,
                            }
                            for ip_range in (
                                other_subnet.get_dynamic_ranges().order_by(
                                    "id"
                                )
                            )
                        ],
                    },
                ],
                key=itemgetter("subnet"),
            ),
            observed_subnets,
        )
        self.assertItemsEqual(
            dhcp.make_hosts_for_subnets([ha_subnet]), observed_hosts
        )
        self.assertEqual(primary_interface.name, observed_interface)


class TestGetDHCPConfiguration(MAASServerTestCase):
    """Tests for `get_dhcp_configuration`."""

    def make_RackController_ready_for_DHCP(self):
        rack = factory.make_RackController()
        vlan = factory.make_VLAN(dhcp_on=True, primary_rack=rack)
        subnet4 = factory.make_Subnet(vlan=vlan, cidr="10.20.30.0/24")
        subnet6 = factory.make_Subnet(
            vlan=vlan, cidr="fd38:c341:27da:c831::/64"
        )
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack, vlan=vlan
        )
        address4 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=subnet4,
            interface=interface,
        )
        address6 = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=subnet6,
            interface=interface,
        )
        return rack, (address4, address6)

    def assertHasConfigurationForNTP(
        self, shared_network, subnet, ntp_servers
    ):
        self.assertThat(
            shared_network,
            MatchesAll(
                # Quick-n-dirty: match only one shared network.
                HasLength(1),
                AllMatch(
                    ContainsDict(
                        {
                            "subnets": MatchesAll(
                                # Quick-n-dirty: match only one subnet.
                                HasLength(1),
                                AllMatch(
                                    ContainsDict(
                                        {
                                            "subnet_cidr": Equals(subnet.cidr),
                                            "ntp_servers": Equals(ntp_servers),
                                        }
                                    )
                                ),
                            )
                        }
                    )
                ),
            ),
        )

    def test__uses_global_ntp_servers_when_ntp_external_only_is_set(self):
        ntp_servers = [factory.make_hostname(), factory.make_ip_address()]
        Config.objects.set_config("ntp_servers", ", ".join(ntp_servers))
        Config.objects.set_config("ntp_external_only", True)

        rack, (addr4, addr6) = self.make_RackController_ready_for_DHCP()
        config = dhcp.get_dhcp_configuration(rack)

        self.assertHasConfigurationForNTP(
            config.shared_networks_v4, addr4.subnet, ntp_servers
        )
        self.assertHasConfigurationForNTP(
            config.shared_networks_v6, addr6.subnet, ntp_servers
        )

    def test__finds_per_subnet_addresses_when_ntp_external_only_not_set(self):
        ntp_servers = [factory.make_hostname(), factory.make_ip_address()]
        Config.objects.set_config("ntp_servers", ", ".join(ntp_servers))
        Config.objects.set_config("ntp_external_only", False)

        rack, (addr4, addr6) = self.make_RackController_ready_for_DHCP()
        config = dhcp.get_dhcp_configuration(rack)

        self.assertHasConfigurationForNTP(
            config.shared_networks_v4, addr4.subnet, [addr4.ip]
        )
        self.assertHasConfigurationForNTP(
            config.shared_networks_v6, addr6.subnet, [addr6.ip]
        )


class TestConfigureDHCP(MAASTransactionServerTestCase):
    """Tests for `configure_dhcp`."""

    scenarios = (
        (
            "v1",
            dict(
                rpc_version=1,
                command_v4=ConfigureDHCPv4,
                command_v6=ConfigureDHCPv6,
                process_expected_shared_networks=downgrade_shared_networks,
            ),
        ),
        (
            "v2",
            dict(
                rpc_verson=2,
                command_v4=ConfigureDHCPv4_V2,
                command_v6=ConfigureDHCPv6_V2,
                process_expected_shared_networks=None,
            ),
        ),
    )

    @synchronous
    def prepare_rpc(self, rack_controller):
        """"Set up test case for speaking RPC to `rack_controller`."""
        self.useFixture(RegionEventLoopFixture("rpc"))
        self.useFixture(RunningEventLoopFixture())
        fixture = self.useFixture(MockLiveRegionToClusterRPCFixture())
        cluster = fixture.makeCluster(
            rack_controller, self.command_v4, self.command_v6
        )
        return (
            cluster,
            getattr(cluster, self.command_v4.commandName.decode("ascii")),
            getattr(cluster, self.command_v6.commandName.decode("ascii")),
        )

    @transactional
    def create_rack_controller(
        self, dhcp_on=True, missing_ipv4=False, missing_ipv6=False
    ):
        """Create a `rack_controller` in a state that will call both
        `ConfigureDHCPv4` and `ConfigureDHCPv6` with data."""
        primary_rack = factory.make_RackController(interface=False)
        secondary_rack = factory.make_RackController(interface=False)

        vlan = factory.make_VLAN(
            dhcp_on=dhcp_on,
            primary_rack=primary_rack,
            secondary_rack=secondary_rack,
        )
        primary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=primary_rack, vlan=vlan
        )
        secondary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=secondary_rack, vlan=vlan
        )

        subnet_v4 = factory.make_ipv4_Subnet_with_IPRanges(
            vlan=vlan, unmanaged=(not dhcp_on)
        )
        subnet_v6 = factory.make_Subnet(
            vlan=vlan,
            cidr="fd38:c341:27da:c831::/64",
            gateway_ip="fd38:c341:27da:c831::1",
            dns_servers=[],
        )
        factory.make_IPRange(
            subnet_v6,
            "fd38:c341:27da:c831:0:1::",
            "fd38:c341:27da:c831:0:1:ffff:0",
        )

        if not missing_ipv4:
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                subnet=subnet_v4,
                interface=primary_interface,
            )
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                subnet=subnet_v4,
                interface=secondary_interface,
            )
        if not missing_ipv6:
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                subnet=subnet_v6,
                interface=primary_interface,
            )
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                subnet=subnet_v6,
                interface=secondary_interface,
            )

        for _ in range(3):
            factory.make_DHCPSnippet(subnet=subnet_v4, enabled=True)
            factory.make_DHCPSnippet(subnet=subnet_v6, enabled=True)
            factory.make_DHCPSnippet(enabled=True)

        config = dhcp.get_dhcp_configuration(primary_rack)
        return primary_rack, config

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_configure_for_both_ipv4_and_ipv6(self):
        # ... when DHCP_CONNECT is True.
        self.patch(dhcp.settings, "DHCP_CONNECT", True)
        rack_controller, config = yield deferToDatabase(
            self.create_rack_controller
        )
        protocol, ipv4_stub, ipv6_stub = yield deferToThread(
            self.prepare_rpc, rack_controller
        )
        ipv4_stub.side_effect = always_succeed_with({})
        ipv6_stub.side_effect = always_succeed_with({})
        interfaces_v4 = [{"name": name} for name in config.interfaces_v4]
        interfaces_v6 = [{"name": name} for name in config.interfaces_v6]

        yield dhcp.configure_dhcp(rack_controller)

        if self.process_expected_shared_networks is not None:
            self.process_expected_shared_networks(config.shared_networks_v4)
            self.process_expected_shared_networks(config.shared_networks_v6)

        self.assertThat(
            ipv4_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v4,
                shared_networks=config.shared_networks_v4,
                hosts=config.hosts_v4,
                interfaces=interfaces_v4,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )
        self.assertThat(
            ipv6_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v6,
                shared_networks=config.shared_networks_v6,
                hosts=config.hosts_v6,
                interfaces=interfaces_v6,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )

    @wait_for_reactor
    @inlineCallbacks
    def test__doesnt_call_configure_for_both_ipv4_and_ipv6(self):
        # ... when DHCP_CONNECT is False.
        self.patch(dhcp.settings, "DHCP_CONNECT", False)
        rack_controller, config = yield deferToDatabase(
            self.create_rack_controller
        )
        protocol, ipv4_stub, ipv6_stub = yield deferToThread(
            self.prepare_rpc, rack_controller
        )
        ipv4_stub.side_effect = always_succeed_with({})
        ipv6_stub.side_effect = always_succeed_with({})

        yield dhcp.configure_dhcp(rack_controller)

        self.assertThat(ipv4_stub, MockNotCalled())
        self.assertThat(ipv6_stub, MockNotCalled())

    @wait_for_reactor
    @inlineCallbacks
    def test__updates_service_status_running_when_dhcp_on(self):
        self.patch(dhcp.settings, "DHCP_CONNECT", True)
        rack_controller, _ = yield deferToDatabase(self.create_rack_controller)
        protocol, ipv4_stub, ipv6_stub = yield deferToThread(
            self.prepare_rpc, rack_controller
        )
        ipv4_stub.side_effect = always_succeed_with({})
        ipv6_stub.side_effect = always_succeed_with({})

        @transactional
        def service_statuses_are_unknown():
            dhcpv4_service = Service.objects.get(
                node=rack_controller, name="dhcpd"
            )
            self.assertThat(
                dhcpv4_service,
                MatchesStructure.byEquality(
                    status=SERVICE_STATUS.UNKNOWN, status_info=""
                ),
            )
            dhcpv6_service = Service.objects.get(
                node=rack_controller, name="dhcpd6"
            )
            self.assertThat(
                dhcpv6_service,
                MatchesStructure.byEquality(
                    status=SERVICE_STATUS.UNKNOWN, status_info=""
                ),
            )

        yield deferToDatabase(service_statuses_are_unknown)

        yield dhcp.configure_dhcp(rack_controller)

        @transactional
        def services_are_running():
            dhcpv4_service = Service.objects.get(
                node=rack_controller, name="dhcpd"
            )
            self.assertThat(
                dhcpv4_service,
                MatchesStructure.byEquality(
                    status=SERVICE_STATUS.RUNNING, status_info=""
                ),
            )
            dhcpv6_service = Service.objects.get(
                node=rack_controller, name="dhcpd6"
            )
            self.assertThat(
                dhcpv6_service,
                MatchesStructure.byEquality(
                    status=SERVICE_STATUS.RUNNING, status_info=""
                ),
            )

        yield deferToDatabase(services_are_running)

    @wait_for_reactor
    @inlineCallbacks
    def test__updates_service_status_off_when_dhcp_off(self):
        self.patch(dhcp.settings, "DHCP_CONNECT", True)
        rack_controller, _ = yield deferToDatabase(
            self.create_rack_controller, dhcp_on=False
        )
        protocol, ipv4_stub, ipv6_stub = yield deferToThread(
            self.prepare_rpc, rack_controller
        )
        ipv4_stub.side_effect = always_succeed_with({})
        ipv6_stub.side_effect = always_succeed_with({})

        @transactional
        def service_statuses_are_unknown():
            dhcpv4_service = Service.objects.get(
                node=rack_controller, name="dhcpd"
            )
            self.assertThat(
                dhcpv4_service,
                MatchesStructure.byEquality(
                    status=SERVICE_STATUS.UNKNOWN, status_info=""
                ),
            )
            dhcpv6_service = Service.objects.get(
                node=rack_controller, name="dhcpd6"
            )
            self.assertThat(
                dhcpv6_service,
                MatchesStructure.byEquality(
                    status=SERVICE_STATUS.UNKNOWN, status_info=""
                ),
            )

        yield deferToDatabase(service_statuses_are_unknown)

        yield dhcp.configure_dhcp(rack_controller)

        @transactional
        def service_status_updated():
            dhcpv4_service = Service.objects.get(
                node=rack_controller, name="dhcpd"
            )
            self.assertThat(
                dhcpv4_service,
                MatchesStructure.byEquality(
                    status=SERVICE_STATUS.OFF, status_info=""
                ),
            )
            dhcpv6_service = Service.objects.get(
                node=rack_controller, name="dhcpd6"
            )
            self.assertThat(
                dhcpv6_service,
                MatchesStructure.byEquality(
                    status=SERVICE_STATUS.OFF, status_info=""
                ),
            )

        yield deferToDatabase(service_status_updated)

    @wait_for_reactor
    @inlineCallbacks
    def test__updates_service_status_dead_when_configuration_crashes(self):
        self.patch(dhcp.settings, "DHCP_CONNECT", True)
        rack_controller, _ = yield deferToDatabase(
            self.create_rack_controller, dhcp_on=False
        )
        protocol, ipv4_stub, ipv6_stub = yield deferToThread(
            self.prepare_rpc, rack_controller
        )
        ipv4_exc = factory.make_name("ipv4_failure")
        ipv4_stub.side_effect = always_fail_with(CannotConfigureDHCP(ipv4_exc))
        ipv6_exc = factory.make_name("ipv6_failure")
        ipv6_stub.side_effect = always_fail_with(CannotConfigureDHCP(ipv6_exc))

        with ExpectedException(CannotConfigureDHCP):
            yield dhcp.configure_dhcp(rack_controller)

        @transactional
        def service_status_updated():
            dhcpv4_service = Service.objects.get(
                node=rack_controller, name="dhcpd"
            )
            self.assertThat(
                dhcpv4_service,
                MatchesStructure.byEquality(
                    status=SERVICE_STATUS.DEAD, status_info=ipv4_exc
                ),
            )
            dhcpv6_service = Service.objects.get(
                node=rack_controller, name="dhcpd6"
            )
            self.assertThat(
                dhcpv6_service,
                MatchesStructure.byEquality(
                    status=SERVICE_STATUS.DEAD, status_info=ipv6_exc
                ),
            )

        yield deferToDatabase(service_status_updated)


class TestValidateDHCPConfig(MAASTransactionServerTestCase):
    """Tests for `validate_dhcp_config`."""

    scenarios = (
        (
            "v1",
            dict(
                rpc_version=1,
                command_v4=ValidateDHCPv4Config,
                command_v6=ValidateDHCPv6Config,
                process_expected_shared_networks=downgrade_shared_networks,
            ),
        ),
        (
            "v2",
            dict(
                rpc_version=2,
                command_v4=ValidateDHCPv4Config_V2,
                command_v6=ValidateDHCPv6Config_V2,
                process_expected_shared_networks=None,
            ),
        ),
    )

    def prepare_rpc(self, rack_controller, return_value=None):
        """"Set up test case for speaking RPC to `rack_controller`."""
        self.useFixture(RegionEventLoopFixture("rpc"))
        self.useFixture(RunningEventLoopFixture())
        fixture = self.useFixture(MockLiveRegionToClusterRPCFixture())
        cluster = fixture.makeCluster(
            rack_controller, self.command_v4, self.command_v6
        )
        ipv4_stub = getattr(
            cluster, self.command_v4.commandName.decode("ascii")
        )
        ipv4_stub.return_value = defer.succeed({"errors": return_value})
        ipv6_stub = getattr(
            cluster, self.command_v6.commandName.decode("ascii")
        )
        ipv6_stub.return_value = defer.succeed({"errors": return_value})
        return ipv4_stub, ipv6_stub

    def create_rack_controller(self):
        """Create a `rack_controller` in a state that will call both
        `ValidateDHCPv4Config` and `ValidateDHCPv6Config` with data."""
        primary_rack = factory.make_RackController(interface=False)
        secondary_rack = factory.make_RackController(interface=False)

        vlan = factory.make_VLAN(
            dhcp_on=True,
            primary_rack=primary_rack,
            secondary_rack=secondary_rack,
        )
        primary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=primary_rack, vlan=vlan
        )
        secondary_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=secondary_rack, vlan=vlan
        )

        subnet_v4 = factory.make_ipv4_Subnet_with_IPRanges(vlan=vlan)
        subnet_v6 = factory.make_Subnet(
            vlan=vlan, cidr="fd38:c341:27da:c831::/64"
        )
        factory.make_IPRange(
            subnet_v6,
            "fd38:c341:27da:c831:0:1::",
            "fd38:c341:27da:c831:0:1:ffff:0",
        )

        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet_v4,
            interface=primary_interface,
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet_v4,
            interface=secondary_interface,
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet_v6,
            interface=primary_interface,
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet_v6,
            interface=secondary_interface,
        )

        for _ in range(3):
            factory.make_DHCPSnippet(subnet=subnet_v4, enabled=True)
            factory.make_DHCPSnippet(subnet=subnet_v6, enabled=True)
            factory.make_DHCPSnippet(enabled=True)

        config = dhcp.get_dhcp_configuration(primary_rack)
        return primary_rack, config

    def test__calls_validate_for_both_ipv4_and_ipv6(self):
        rack_controller, config = self.create_rack_controller()
        ipv4_stub, ipv6_stub = self.prepare_rpc(rack_controller)
        interfaces_v4 = [{"name": name} for name in config.interfaces_v4]
        interfaces_v6 = [{"name": name} for name in config.interfaces_v6]

        dhcp.validate_dhcp_config()

        if self.process_expected_shared_networks is not None:
            self.process_expected_shared_networks(config.shared_networks_v4)
            self.process_expected_shared_networks(config.shared_networks_v6)

        self.assertThat(
            ipv4_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v4,
                shared_networks=config.shared_networks_v4,
                hosts=config.hosts_v4,
                interfaces=interfaces_v4,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )
        self.assertThat(
            ipv6_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v6,
                shared_networks=config.shared_networks_v6,
                hosts=config.hosts_v6,
                interfaces=interfaces_v6,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )

    def test__calls_connected_rack_when_subnet_primary_rack_is_disconn(self):
        rack_controller, config = self.create_rack_controller()
        ipv4_stub, ipv6_stub = self.prepare_rpc(rack_controller)
        interfaces_v4 = [{"name": name} for name in config.interfaces_v4]
        interfaces_v6 = [{"name": name} for name in config.interfaces_v6]

        disconnected_rack = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN(primary_rack=disconnected_rack)
        subnet = factory.make_ipv4_Subnet_with_IPRanges(vlan=vlan)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, subnet=subnet, interface=interface
        )
        dhcp_snippet = factory.make_DHCPSnippet(subnet=subnet)
        dhcp.validate_dhcp_config(dhcp_snippet)

        if self.process_expected_shared_networks is not None:
            self.process_expected_shared_networks(config.shared_networks_v4)
            self.process_expected_shared_networks(config.shared_networks_v6)

        self.assertThat(
            ipv4_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v4,
                shared_networks=config.shared_networks_v4,
                hosts=config.hosts_v4,
                interfaces=interfaces_v4,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )
        self.assertThat(
            ipv6_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v6,
                shared_networks=config.shared_networks_v6,
                hosts=config.hosts_v6,
                interfaces=interfaces_v6,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )

    def test__calls_connected_rack_when_node_primary_rack_is_disconn(self):
        rack_controller, config = self.create_rack_controller()
        ipv4_stub, ipv6_stub = self.prepare_rpc(rack_controller)
        interfaces_v4 = [{"name": name} for name in config.interfaces_v4]
        interfaces_v6 = [{"name": name} for name in config.interfaces_v6]

        disconnected_rack = factory.make_RackController(interface=False)
        vlan = factory.make_VLAN(
            primary_rack=disconnected_rack, secondary_rack=rack_controller
        )
        subnet = factory.make_ipv4_Subnet_with_IPRanges(vlan=vlan)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=disconnected_rack.get_boot_interface(),
        )
        node = factory.make_Node_with_Interface_on_Subnet(subnet=subnet)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet,
            interface=node.get_boot_interface(),
        )
        dhcp_snippet = factory.make_DHCPSnippet(node=node)
        dhcp.validate_dhcp_config(dhcp_snippet)

        if self.process_expected_shared_networks is not None:
            self.process_expected_shared_networks(config.shared_networks_v4)
            self.process_expected_shared_networks(config.shared_networks_v6)

        self.assertThat(
            ipv4_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v4,
                shared_networks=config.shared_networks_v4,
                hosts=config.hosts_v4,
                interfaces=interfaces_v4,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )
        self.assertThat(
            ipv6_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v6,
                shared_networks=config.shared_networks_v6,
                hosts=config.hosts_v6,
                interfaces=interfaces_v6,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )

    def test__calls_validate_with_new_dhcp_snippet(self):
        rack_controller, config = self.create_rack_controller()
        ipv4_stub, ipv6_stub = self.prepare_rpc(rack_controller)
        interfaces_v4 = [{"name": name} for name in config.interfaces_v4]
        interfaces_v6 = [{"name": name} for name in config.interfaces_v6]

        # DHCPSnippetForm generates a new DHCPSnippet in memory and validates
        # it with validate_dhcp_config before committing it.
        value = VersionedTextFile.objects.create(data=factory.make_string())
        new_dhcp_snippet = DHCPSnippet(
            name=factory.make_name("name"), value=value
        )
        dhcp.validate_dhcp_config(new_dhcp_snippet)
        config.global_dhcp_snippets.append(
            {
                "name": new_dhcp_snippet.name,
                "description": new_dhcp_snippet.description,
                "value": new_dhcp_snippet.value.data,
            }
        )

        if self.process_expected_shared_networks is not None:
            self.process_expected_shared_networks(config.shared_networks_v4)
            self.process_expected_shared_networks(config.shared_networks_v6)

        self.assertThat(
            ipv4_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v4,
                shared_networks=config.shared_networks_v4,
                hosts=config.hosts_v4,
                interfaces=interfaces_v4,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )
        self.assertThat(
            ipv6_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v6,
                shared_networks=config.shared_networks_v6,
                hosts=config.hosts_v6,
                interfaces=interfaces_v6,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )

    def test__calls_validate_with_disabled_dhcp_snippet(self):
        rack_controller, config = self.create_rack_controller()
        ipv4_stub, ipv6_stub = self.prepare_rpc(rack_controller)
        interfaces_v4 = [{"name": name} for name in config.interfaces_v4]
        interfaces_v6 = [{"name": name} for name in config.interfaces_v6]

        new_dhcp_snippet = factory.make_DHCPSnippet(enabled=False)
        dhcp.validate_dhcp_config(new_dhcp_snippet)
        config.global_dhcp_snippets.append(
            {
                "name": new_dhcp_snippet.name,
                "description": new_dhcp_snippet.description,
                "value": new_dhcp_snippet.value.data,
            }
        )

        if self.process_expected_shared_networks is not None:
            self.process_expected_shared_networks(config.shared_networks_v4)
            self.process_expected_shared_networks(config.shared_networks_v6)

        self.assertThat(
            ipv4_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v4,
                shared_networks=config.shared_networks_v4,
                hosts=config.hosts_v4,
                interfaces=interfaces_v4,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )
        self.assertThat(
            ipv6_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v6,
                shared_networks=config.shared_networks_v6,
                hosts=config.hosts_v6,
                interfaces=interfaces_v6,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )

    def test__calls_validate_with_updated_dhcp_snippet(self):
        rack_controller, config = self.create_rack_controller()
        ipv4_stub, ipv6_stub = self.prepare_rpc(rack_controller)
        interfaces_v4 = [{"name": name} for name in config.interfaces_v4]
        interfaces_v6 = [{"name": name} for name in config.interfaces_v6]

        updated_dhcp_snippet = DHCPSnippet.objects.get(
            name=random.choice(
                [
                    dhcp_snippet["name"]
                    for dhcp_snippet in config.global_dhcp_snippets
                ]
            )
        )
        updated_dhcp_snippet.value = updated_dhcp_snippet.value.update(
            factory.make_string()
        )
        dhcp.validate_dhcp_config(updated_dhcp_snippet)
        for i, dhcp_snippet in enumerate(config.global_dhcp_snippets):
            if dhcp_snippet["name"] == updated_dhcp_snippet.name:
                config.global_dhcp_snippets[i] = {
                    "name": updated_dhcp_snippet.name,
                    "description": updated_dhcp_snippet.description,
                    "value": updated_dhcp_snippet.value.data,
                }
                break

        if self.process_expected_shared_networks is not None:
            self.process_expected_shared_networks(config.shared_networks_v4)
            self.process_expected_shared_networks(config.shared_networks_v6)

        self.assertThat(
            ipv4_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v4,
                shared_networks=config.shared_networks_v4,
                hosts=config.hosts_v4,
                interfaces=interfaces_v4,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )
        self.assertThat(
            ipv6_stub,
            MockCalledOnceWith(
                ANY,
                omapi_key=config.omapi_key,
                failover_peers=config.failover_peers_v6,
                shared_networks=config.shared_networks_v6,
                hosts=config.hosts_v6,
                interfaces=interfaces_v6,
                global_dhcp_snippets=config.global_dhcp_snippets,
            ),
        )

    def test__returns_no_errors_when_valid(self):
        rack_controller, config = self.create_rack_controller()
        self.prepare_rpc(rack_controller)

        self.assertEquals([], dhcp.validate_dhcp_config())

    def test__returns_errors_when_invalid(self):
        rack_controller, config = self.create_rack_controller()
        dhcpd_error = {
            "error": factory.make_name("error"),
            "line_num": 14,
            "line": factory.make_name("line"),
            "position": factory.make_name("position"),
        }
        self.prepare_rpc(rack_controller, [dhcpd_error])

        self.assertItemsEqual([dhcpd_error], dhcp.validate_dhcp_config())

    def test__dedups_errors(self):
        rack_controller, config = self.create_rack_controller()
        dhcpd_error = {
            "error": factory.make_name("error"),
            "line_num": 14,
            "line": factory.make_name("line"),
            "position": factory.make_name("position"),
        }
        self.prepare_rpc(rack_controller, [dhcpd_error, dhcpd_error])

        self.assertItemsEqual([dhcpd_error], dhcp.validate_dhcp_config())

    def test__rack_not_found_raises_validation_error(self):
        subnet = factory.make_Subnet()
        dhcp_snippet = factory.make_DHCPSnippet(subnet=subnet)
        self.assertRaises(
            ValidationError, dhcp.validate_dhcp_config, dhcp_snippet
        )
