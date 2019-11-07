# Copyright 2015-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the Interface model."""

__all__ = []

from collections import Iterable
import datetime
import random
import threading
from unittest.mock import call

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.utils import IntegrityError
from django.http import Http404
from fixtures import FakeLogger
from maasserver.enum import (
    BRIDGE_TYPE,
    BRIDGE_TYPE_CHOICES,
    INTERFACE_LINK_TYPE,
    INTERFACE_TYPE,
    IPADDRESS_TYPE,
)
from maasserver.exceptions import (
    StaticIPAddressOutOfRange,
    StaticIPAddressUnavailable,
)
from maasserver.models import (
    Fabric,
    interface as interface_module,
    MDNS,
    Neighbour,
    Space,
    StaticIPAddress,
    Subnet,
    VLAN,
)
from maasserver.models.config import NetworkDiscoveryConfig
from maasserver.models.interface import (
    BondInterface,
    BridgeInterface,
    Interface,
    InterfaceRelationship,
    PhysicalInterface,
    UnknownInterface,
    VLANInterface,
)
from maasserver.models.vlan import DEFAULT_MTU
from maasserver.permissions import NodePermission
from maasserver.testing.factory import factory
from maasserver.testing.orm import reload_objects
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.orm import get_one, reload_object, transactional
from maastesting.djangotestcase import CountQueries
from maastesting.matchers import (
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from netaddr import EUI, IPAddress, IPNetwork
from provisioningserver.utils.ipaddr import (
    get_first_and_last_usable_host_in_network,
)
from provisioningserver.utils.network import (
    annotate_with_default_monitored_interfaces,
)
from testtools import ExpectedException
from testtools.matchers import (
    Contains,
    Equals,
    Is,
    MatchesDict,
    MatchesListwise,
    MatchesStructure,
    Not,
)


class TestInterfaceManager(MAASServerTestCase):
    def test_get_queryset_returns_all_interface_types(self):
        physical = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        bond = factory.make_Interface(INTERFACE_TYPE.BOND, parents=[physical])
        vlan = factory.make_Interface(INTERFACE_TYPE.VLAN, parents=[bond])
        self.assertItemsEqual([physical, bond, vlan], Interface.objects.all())

    def test_get_interface_or_404_returns_interface(self):
        node = factory.make_Node()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        user = factory.make_User()
        self.assertEqual(
            interface,
            Interface.objects.get_interface_or_404(
                node.system_id, interface.id, user, NodePermission.view
            ),
        )

    def test_get_interface_or_404_returns_interface_for_admin(self):
        node = factory.make_Node()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        user = factory.make_admin()
        self.assertEqual(
            interface,
            Interface.objects.get_interface_or_404(
                node.system_id, interface.id, user, NodePermission.admin
            ),
        )

    def test_get_interface_or_404_raises_Http404_when_invalid_id(self):
        node = factory.make_Node()
        factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        user = factory.make_User()
        self.assertRaises(
            Http404,
            Interface.objects.get_interface_or_404,
            node.system_id,
            random.randint(1000 * 1000, 1000 * 1000 * 100),
            user,
            NodePermission.view,
        )

    def test_get_interface_or_404_raises_PermissionDenied_when_user(self):
        node = factory.make_Node()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        user = factory.make_User()
        self.assertRaises(
            PermissionDenied,
            Interface.objects.get_interface_or_404,
            node.system_id,
            interface.id,
            user,
            NodePermission.admin,
        )

    def test_get_interface_or_404_uses_device_perm(self):
        device = factory.make_Device()
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=device
        )
        user = factory.make_User()
        self.assertEqual(
            interface,
            Interface.objects.get_interface_or_404(
                device.system_id,
                interface.id,
                user,
                NodePermission.admin,
                NodePermission.edit,
            ),
        )

    def test_get_or_create_without_parents(self):
        node = factory.make_Node()
        mac_address = factory.make_mac_address()
        name = factory.make_name("eth")
        interface, created = PhysicalInterface.objects.get_or_create(
            node=node, mac_address=mac_address, name=name
        )
        self.assertTrue(created)
        self.assertIsNotNone(interface)
        retrieved_interface, created = PhysicalInterface.objects.get_or_create(
            node=node, mac_address=mac_address
        )
        self.assertFalse(created)
        self.assertEquals(interface, retrieved_interface)

    def test_get_or_create_with_parents(self):
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        parent2 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=parent1.node
        )
        interface, created = BondInterface.objects.get_or_create(
            node=parent1.node,
            mac_address=parent1.mac_address,
            name="bond0",
            parents=[parent1, parent2],
        )
        self.assertTrue(created)
        self.assertIsNotNone(interface)
        retrieved_interface, created = BondInterface.objects.get_or_create(
            node=parent1.node, parents=[parent1, parent2]
        )
        self.assertFalse(created)
        self.assertEquals(interface, retrieved_interface)

    def test__get_interface_dict_for_node(self):
        node1 = factory.make_Node()
        node1_eth0 = factory.make_Interface(node=node1, name="eth0")
        node1_eth1 = factory.make_Interface(node=node1, name="eth1")
        node2 = factory.make_Node()
        node2_eth0 = factory.make_Interface(node=node2, name="eth0")
        node2_eth1 = factory.make_Interface(node=node2, name="eth1")
        self.assertThat(
            Interface.objects.get_interface_dict_for_node(node1),
            Equals({"eth0": node1_eth0, "eth1": node1_eth1}),
        )
        self.assertThat(
            Interface.objects.get_interface_dict_for_node(node2),
            Equals({"eth0": node2_eth0, "eth1": node2_eth1}),
        )

    def test__get_interface_dict_for_node__by_names(self):
        node1 = factory.make_Node()
        node1_eth0 = factory.make_Interface(node=node1, name="eth0")
        node1_eth1 = factory.make_Interface(node=node1, name="eth1")
        node2 = factory.make_Node()
        node2_eth0 = factory.make_Interface(node=node2, name="eth0")
        node2_eth1 = factory.make_Interface(node=node2, name="eth1")
        self.assertThat(
            Interface.objects.get_interface_dict_for_node(
                node1, names=("eth0",)
            ),
            Equals({"eth0": node1_eth0}),
        )
        self.assertThat(
            Interface.objects.get_interface_dict_for_node(
                node1, names=("eth0", "eth1")
            ),
            Equals({"eth0": node1_eth0, "eth1": node1_eth1}),
        )
        self.assertThat(
            Interface.objects.get_interface_dict_for_node(
                node2, names=("eth0", "eth1")
            ),
            Equals({"eth0": node2_eth0, "eth1": node2_eth1}),
        )

    def test__get_all_interfaces_definition_for_node(self):
        node1 = factory.make_Node()
        eth0 = factory.make_Interface(node=node1, name="eth0")
        eth0_vlan = factory.make_Interface(
            iftype=INTERFACE_TYPE.VLAN, parents=[eth0], node=node1
        )
        eth1 = factory.make_Interface(node=node1, name="eth1", enabled=False)
        eth2 = factory.make_Interface(node=node1, name="eth2")
        eth4 = factory.make_Interface(node=node1, name="eth4")
        bond0 = factory.make_Interface(
            iftype=INTERFACE_TYPE.BOND,
            parents=[eth2],
            name="bond0",
            node=node1,
        )
        br0 = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE,
            parents=[eth4],
            name="br0",
            node=node1,
        )
        br1 = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE, parents=[], name="br1", node=node1
        )
        # Make sure we only got one Node's interfaces by creating a few
        # dummy interfaces.
        node2 = factory.make_Node()
        factory.make_Interface(node=node2, name="eth0")
        factory.make_Interface(node=node2, name="eth1")
        expected_result = {
            "eth0": {
                "type": "physical",
                "mac_address": str(eth0.mac_address),
                "enabled": True,
                "parents": [],
                "source": "maas-database",
                "obj": eth0,
                "monitored": True,
            },
            eth0_vlan.name: {
                "type": "vlan",
                "mac_address": str(eth0_vlan.mac_address),
                "enabled": True,
                "parents": ["eth0"],
                "source": "maas-database",
                "obj": eth0_vlan,
                "monitored": False,
            },
            "eth1": {
                "type": "physical",
                "mac_address": str(eth1.mac_address),
                "enabled": False,
                "parents": [],
                "source": "maas-database",
                "obj": eth1,
                "monitored": False,
            },
            "eth2": {
                "type": "physical",
                "mac_address": str(eth2.mac_address),
                "enabled": True,
                "parents": [],
                "source": "maas-database",
                "obj": eth2,
                "monitored": False,
            },
            "eth4": {
                "type": "physical",
                "mac_address": str(eth4.mac_address),
                "enabled": True,
                "parents": [],
                "source": "maas-database",
                "obj": eth4,
                # Physical bridge members are monitored.
                "monitored": True,
            },
            "bond0": {
                "type": "bond",
                "mac_address": str(bond0.mac_address),
                "enabled": True,
                "parents": ["eth2"],
                "source": "maas-database",
                "obj": bond0,
                # Bonds are monitored.
                "monitored": True,
            },
            "br0": {
                "type": "bridge",
                "mac_address": str(br0.mac_address),
                "enabled": True,
                "parents": ["eth4"],
                "source": "maas-database",
                "obj": br0,
                "monitored": False,
            },
            "br1": {
                "type": "bridge",
                "mac_address": str(br1.mac_address),
                "enabled": True,
                "parents": [],
                "source": "maas-database",
                "obj": br1,
                # Zero-parent bridges are monitored.
                "monitored": True,
            },
        }
        interfaces = Interface.objects.get_all_interfaces_definition_for_node(
            node1
        )
        # Need to ensure this call is compatible with the returned structure.
        annotate_with_default_monitored_interfaces(interfaces)
        self.assertDictEqual(interfaces, expected_result)

    def test__get_interface_dict_for_node__prefetches_on_request(self):
        node1 = factory.make_Node()
        factory.make_Interface(node=node1, name="eth0")
        counter = CountQueries()
        with counter:
            interfaces = Interface.objects.get_interface_dict_for_node(
                node1, fetch_fabric_vlan=True
            )
            # Need this line in order to cause the extra [potential] queries.
            self.assertIsNotNone(interfaces["eth0"].vlan.fabric)
        self.assertThat(counter.num_queries, Equals(1))

    def test__get_interface_dict_for_node__skips_prefetch_if_not_requested(
        self,
    ):
        node1 = factory.make_Node()
        factory.make_Interface(node=node1, name="eth0")
        counter = CountQueries()
        with counter:
            interfaces = Interface.objects.get_interface_dict_for_node(
                node1, fetch_fabric_vlan=False
            )
            self.assertIsNotNone(interfaces["eth0"].vlan.fabric)
        self.assertThat(counter.num_queries, Equals(3))

    def test_filter_by_ip(self):
        factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(cidr="10.0.0.0/24")
        ip = factory.make_StaticIPAddress(
            ip="10.0.0.1", interface=iface, subnet=subnet
        )
        fetched_iface = get_one(Interface.objects.filter_by_ip(ip))
        self.assertEqual(iface, fetched_iface)
        fetched_iface = get_one(Interface.objects.filter_by_ip("10.0.0.1"))
        self.assertEqual(iface, fetched_iface)


class TestInterfaceQueriesMixin(MAASServerTestCase):
    def test__filter_by_specifiers_default_matches_cidr_or_name(self):
        subnet1 = factory.make_Subnet(cidr="10.0.0.0/24")
        subnet2 = factory.make_Subnet(cidr="2001:db8::/64")
        node1 = factory.make_Node_with_Interface_on_Subnet(subnet=subnet1)
        node2 = factory.make_Node_with_Interface_on_Subnet(subnet=subnet2)
        iface1 = node1.get_boot_interface()
        iface2 = node2.get_boot_interface()
        iface3 = factory.make_Interface(
            iftype=INTERFACE_TYPE.BOND, parents=[iface2], name="bond0"
        )
        ip1 = factory.make_StaticIPAddress(
            ip="10.0.0.1", interface=iface1, subnet=subnet1
        )
        ip3 = factory.make_StaticIPAddress(
            ip="2001:db8::1", interface=iface3, subnet=subnet2
        )
        # First try with the '/prefixlen' string appended.
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("%s/24" % ip1.ip), [iface1]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("%s/64" % ip3.ip), [iface3]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                ["%s/24" % ip1.ip, "%s/64" % ip3.ip]
            ),
            [iface1, iface3],
        )
        # Next, try plain old IP addresses.
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("%s" % ip1.ip), [iface1]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("%s" % ip3.ip), [iface3]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                ["%s" % ip1.ip, "%s" % ip3.ip]
            ),
            [iface1, iface3],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(iface1.name), [iface1]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(iface2.name), [iface2]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(iface3.name), [iface3]
        )

    def test__filter_by_specifiers_matches_fabric_class(self):
        fabric1 = factory.make_Fabric(class_type="10g")
        fabric2 = factory.make_Fabric(class_type="1g")
        vlan1 = factory.make_VLAN(vid=1, fabric=fabric1)
        vlan2 = factory.make_VLAN(vid=2, fabric=fabric2)
        iface1 = factory.make_Interface(vlan=vlan1)
        iface2 = factory.make_Interface(vlan=vlan2)
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("fabric_class:10g"),
            [iface1],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("fabric_class:1g"), [iface2]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                ["fabric_class:1g", "fabric_class:10g"]
            ),
            [iface1, iface2],
        )

    def test__filter_by_specifiers_matches_fabric(self):
        fabric1 = factory.make_Fabric(name="fabric1")
        fabric2 = factory.make_Fabric(name="fabric2")
        vlan1 = factory.make_VLAN(vid=1, fabric=fabric1)
        vlan2 = factory.make_VLAN(vid=2, fabric=fabric2)
        iface1 = factory.make_Interface(vlan=vlan1)
        iface2 = factory.make_Interface(vlan=vlan2)
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("fabric:fabric1"), [iface1]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("fabric:fabric2"), [iface2]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                ["fabric:fabric1", "fabric:fabric2"]
            ),
            [iface1, iface2],
        )

    def test__filter_by_specifiers_matches_interface_id(self):
        iface1 = factory.make_Interface()
        iface2 = factory.make_Interface()
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("id:%s" % iface1.id),
            [iface1],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("id:%s" % iface2.id),
            [iface2],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                ["id:%s" % iface1.id, "id:%s" % iface2.id]
            ),
            [iface1, iface2],
        )

    def test__filter_by_specifiers_matches_vid(self):
        fabric1 = factory.make_Fabric()
        parent1 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=fabric1.get_default_vlan()
        )
        vlan1 = factory.make_VLAN(fabric=fabric1)
        iface1 = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan1, parents=[parent1]
        )
        fabric2 = factory.make_Fabric()
        parent2 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=fabric2.get_default_vlan()
        )
        vlan2 = factory.make_VLAN(fabric=fabric2)
        iface2 = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan2, parents=[parent2]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("vid:%s" % vlan1.vid),
            [iface1],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("vid:%s" % vlan2.vid),
            [iface2],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                ["vid:%s" % vlan1.vid, "vid:%s" % vlan2.vid]
            ),
            [iface1, iface2],
        )

    def test__filter_by_specifiers_matches_vlan(self):
        fabric1 = factory.make_Fabric()
        parent1 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=fabric1.get_default_vlan()
        )
        vlan1 = factory.make_VLAN(fabric=fabric1)
        iface1 = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan1, parents=[parent1]
        )
        fabric2 = factory.make_Fabric()
        parent2 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=fabric2.get_default_vlan()
        )
        vlan2 = factory.make_VLAN(fabric=fabric2)
        iface2 = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan2, parents=[parent2]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("vlan:%s" % vlan1.vid),
            [iface1],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("vlan:%s" % vlan2.vid),
            [iface2],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                ["vlan:%s" % vlan1.vid, "vlan:%s" % vlan2.vid]
            ),
            [iface1, iface2],
        )

    def test__filter_by_specifiers_matches_subnet_specifier(self):
        subnet1 = factory.make_Subnet()
        subnet2 = factory.make_Subnet()
        node1 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet1, with_dhcp_rack_primary=False
        )
        node2 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet2, with_dhcp_rack_primary=False
        )
        iface1 = node1.get_boot_interface()
        iface2 = node2.get_boot_interface()
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                "subnet:cidr:%s" % subnet1.cidr
            ),
            [iface1],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                "subnet:cidr:%s" % subnet2.cidr
            ),
            [iface2],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                [
                    "subnet:cidr:%s" % subnet1.cidr,
                    "subnet:cidr:%s" % subnet2.cidr,
                ]
            ),
            [iface1, iface2],
        )

    def test__filter_by_specifiers_matches_subnet_cidr_alias(self):
        subnet1 = factory.make_Subnet()
        subnet2 = factory.make_Subnet()
        node1 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet1, with_dhcp_rack_primary=False
        )
        node2 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet2, with_dhcp_rack_primary=False
        )
        iface1 = node1.get_boot_interface()
        iface2 = node2.get_boot_interface()
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                "subnet_cidr:%s" % subnet1.cidr
            ),
            [iface1],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                "subnet_cidr:%s" % subnet2.cidr
            ),
            [iface2],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                [
                    "subnet_cidr:%s" % subnet1.cidr,
                    "subnet_cidr:%s" % subnet2.cidr,
                ]
            ),
            [iface1, iface2],
        )

    def test__filter_by_specifiers_matches_space_by_subnet(self):
        space1 = factory.make_Space()
        space2 = factory.make_Space()
        vlan1 = factory.make_VLAN(space=space1)
        vlan2 = factory.make_VLAN(space=space2)
        subnet1 = factory.make_Subnet(vlan=vlan1, space=None)
        subnet2 = factory.make_Subnet(vlan=vlan2, space=None)
        node1 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet1, with_dhcp_rack_primary=False
        )
        node2 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet2, with_dhcp_rack_primary=False
        )
        iface1 = node1.get_boot_interface()
        iface2 = node2.get_boot_interface()
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("space:%s" % space1.name),
            [iface1],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("space:%s" % space2.name),
            [iface2],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                ["space:%s" % space1.name, "space:%s" % space2.name]
            ),
            [iface1, iface2],
        )

    def test__filter_by_specifiers_matches_space_by_vlan(self):
        space1 = factory.make_Space()
        space2 = factory.make_Space()
        vlan1 = factory.make_VLAN(space=space1)
        vlan2 = factory.make_VLAN(space=space2)
        subnet1 = factory.make_Subnet(vlan=vlan1, space=None)
        subnet2 = factory.make_Subnet(vlan=vlan2, space=None)
        node1 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet1, with_dhcp_rack_primary=False
        )
        node2 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet2, with_dhcp_rack_primary=False
        )
        iface1 = node1.get_boot_interface()
        iface2 = node2.get_boot_interface()
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("space:%s" % space1.name),
            [iface1],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("space:%s" % space2.name),
            [iface2],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                ["space:%s" % space1.name, "space:%s" % space2.name]
            ),
            [iface1, iface2],
        )

    def test__filter_by_specifiers_matches_undefined_space(self):
        space1 = factory.make_Space()
        vlan1 = factory.make_VLAN(space=space1)
        vlan2 = factory.make_VLAN(space=None)
        subnet1 = factory.make_Subnet(vlan=vlan1, space=None)
        subnet2 = factory.make_Subnet(vlan=vlan2, space=None)
        node1 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet1, with_dhcp_rack_primary=False
        )
        node2 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet2, with_dhcp_rack_primary=False
        )
        iface1 = node1.get_boot_interface()
        iface2 = node2.get_boot_interface()
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("space:%s" % space1.name),
            [iface1],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                "space:%s" % Space.UNDEFINED
            ),
            [iface2],
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers(
                ["space:%s" % space1.name, "space:%s" % Space.UNDEFINED]
            ),
            [iface1, iface2],
        )

    def test__filter_by_specifiers_matches_type(self):
        physical = factory.make_Interface()
        bond = factory.make_Interface(
            iftype=INTERFACE_TYPE.BOND, parents=[physical]
        )
        vlan = factory.make_Interface(
            iftype=INTERFACE_TYPE.VLAN, parents=[physical]
        )
        unknown = factory.make_Interface(iftype=INTERFACE_TYPE.UNKNOWN)
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("type:physical"), [physical]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("type:vlan"), [vlan]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("type:bond"), [bond]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("type:unknown"), [unknown]
        )

    def test__filter_by_specifiers_matches_ip(self):
        subnet1 = factory.make_Subnet(cidr="10.0.0.0/24")
        subnet2 = factory.make_Subnet(cidr="10.0.1.0/24")
        iface1 = factory.make_Interface()
        iface2 = factory.make_Interface()
        factory.make_StaticIPAddress(
            ip="10.0.0.1",
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet1,
            interface=iface1,
        )
        factory.make_StaticIPAddress(
            ip="10.0.1.1",
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet2,
            interface=iface2,
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("ip:10.0.0.1"), [iface1]
        )
        self.assertItemsEqual(
            Interface.objects.filter_by_specifiers("ip:10.0.1.1"), [iface2]
        )

    def test__filter_by_specifiers_matches_unconfigured_mode(self):
        subnet1 = factory.make_Subnet(cidr="10.0.0.0/24")
        subnet2 = factory.make_Subnet(cidr="10.0.1.0/24")
        subnet3 = factory.make_Subnet(cidr="10.0.2.0/24")
        iface1 = factory.make_Interface()
        iface2 = factory.make_Interface()
        iface3 = factory.make_Interface()
        factory.make_StaticIPAddress(
            ip="",
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=subnet1,
            interface=iface1,
        )
        factory.make_StaticIPAddress(
            ip=None,
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet2,
            interface=iface2,
        )
        factory.make_StaticIPAddress(
            ip="10.0.2.1",
            alloc_type=IPADDRESS_TYPE.AUTO,
            subnet=subnet3,
            interface=iface3,
        )
        self.assertItemsEqual(
            [iface1, iface2],
            Interface.objects.filter_by_specifiers("mode:unconfigured"),
        )

    def test__get_matching_node_map(self):
        space1 = factory.make_Space()
        space2 = factory.make_Space()
        vlan1 = factory.make_VLAN(space=space1)
        vlan2 = factory.make_VLAN(space=space2)
        subnet1 = factory.make_Subnet(vlan=vlan1, space=None)
        subnet2 = factory.make_Subnet(vlan=vlan2, space=None)
        node1 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet1, with_dhcp_rack_primary=False
        )
        node2 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet2, with_dhcp_rack_primary=False
        )
        iface1 = node1.get_boot_interface()
        iface2 = node2.get_boot_interface()
        nodes1, map1 = Interface.objects.get_matching_node_map(
            "space:%s" % space1.name
        )
        self.assertItemsEqual(nodes1, [node1.id])
        self.assertEqual(map1, {node1.id: [iface1.id]})
        nodes2, map2 = Interface.objects.get_matching_node_map(
            "space:%s" % space2.name
        )
        self.assertItemsEqual(nodes2, [node2.id])
        self.assertEqual(map2, {node2.id: [iface2.id]})
        nodes3, map3 = Interface.objects.get_matching_node_map(
            ["space:%s" % space1.name, "space:%s" % space2.name]
        )
        self.assertItemsEqual(nodes3, [node1.id, node2.id])
        self.assertEqual(map3, {node1.id: [iface1.id], node2.id: [iface2.id]})

    def test__get_matching_node_map_with_multiple_interfaces(self):
        space1 = factory.make_Space()
        space2 = factory.make_Space()
        vlan1 = factory.make_VLAN(space=space1)
        vlan2 = factory.make_VLAN(space=space2)
        subnet1 = factory.make_Subnet(vlan=vlan1, space=space1)
        subnet2 = factory.make_Subnet(vlan=vlan2, space=space2)
        node1 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet1, with_dhcp_rack_primary=False
        )
        node2 = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet2, with_dhcp_rack_primary=False
        )
        iface1 = node1.get_boot_interface()
        iface2 = node2.get_boot_interface()
        iface3 = factory.make_Interface(node=node1, subnet=subnet1)
        factory.make_StaticIPAddress(interface=iface3, subnet=subnet1)
        nodes1, map1 = Interface.objects.get_matching_node_map(
            "space:%s" % space1.name
        )
        self.assertItemsEqual(nodes1, {node1.id})
        map1[node1.id].sort()
        self.assertEqual(map1, {node1.id: sorted([iface1.id, iface3.id])})
        nodes2, map2 = Interface.objects.get_matching_node_map(
            "space:%s" % space2.name
        )
        self.assertItemsEqual(nodes2, {node2.id})
        self.assertEqual(map2, {node2.id: [iface2.id]})
        nodes3, map3 = Interface.objects.get_matching_node_map(
            ["space:%s" % space1.name, "space:%s" % space2.name]
        )
        self.assertItemsEqual(nodes3, {node1.id, node2.id})
        map3[node1.id].sort()
        self.assertEqual(
            map3,
            {node1.id: sorted([iface1.id, iface3.id]), node2.id: [iface2.id]},
        )

    def test__get_matching_node_map_by_multiple_tags(self):
        tags = [factory.make_name("tag")]
        tags_specifier = "tag:%s" % "&&".join(tags)
        node = factory.make_Node()
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, tags=tags
        )
        # Other interface with subset of tags.
        factory.make_Interface(INTERFACE_TYPE.PHYSICAL, tags=tags[1:])
        nodes, map = Interface.objects.get_matching_node_map(tags_specifier)
        self.assertItemsEqual(nodes, [node.id])
        self.assertEqual(map, {node.id: [interface.id]})

    def test__get_matching_node_map_by_tag(self):
        tags = [factory.make_name("tag")]
        node = factory.make_Node()
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, tags=tags
        )
        nodes, map = Interface.objects.get_matching_node_map(
            "tag:%s" % random.choice(tags)
        )
        self.assertItemsEqual(nodes, [node.id])
        self.assertEqual(map, {node.id: [interface.id]})


class TestAllInterfacesParentsFirst(MAASServerTestCase):
    def test__all_interfaces_parents_first(self):
        node1 = factory.make_Node()
        eth0 = factory.make_Interface(node=node1, name="eth0")
        eth0_vlan = factory.make_Interface(
            iftype=INTERFACE_TYPE.VLAN, parents=[eth0], node=node1
        )
        eth1 = factory.make_Interface(node=node1, name="eth1", enabled=False)
        eth2 = factory.make_Interface(node=node1, name="eth2")
        eth3 = factory.make_Interface(node=node1, name="eth3")
        eth4 = factory.make_Interface(node=node1, name="eth4")
        eth5 = factory.make_Interface(node=node1, name="eth5")
        bond0 = factory.make_Interface(
            iftype=INTERFACE_TYPE.BOND,
            parents=[eth2, eth3],
            name="bond0",
            node=node1,
        )
        br0 = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE,
            parents=[eth4, eth5],
            name="br0",
            node=node1,
        )
        br1 = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE, parents=[], name="br1", node=node1
        )
        br2 = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE,
            parents=[bond0],
            name="br2",
            node=node1,
        )
        # Make sure we only got one Node's interfaces by creating a few
        # dummy interfaces.
        node2 = factory.make_Node()
        n2_eth0 = factory.make_Interface(node=node2, name="eth0")
        n2_eth1 = factory.make_Interface(node=node2, name="eth1")
        ifaces = Interface.objects.all_interfaces_parents_first(node1)
        self.expectThat(isinstance(ifaces, Iterable), Is(True))
        iface_list = list(ifaces)
        # Expect alphabetical interface order, interleaved with a parents-first
        # search for each child interface. That is, child interfaces will
        # always be listed after /all/ of their parents.
        self.expectThat(
            iface_list,
            Equals(
                [
                    br1,
                    eth0,
                    eth0_vlan,
                    eth1,
                    eth2,
                    eth3,
                    bond0,
                    br2,
                    eth4,
                    eth5,
                    br0,
                ]
            ),
        )
        # Might as well test that the other host looks okay, too.
        n2_ifaces = list(Interface.objects.all_interfaces_parents_first(node2))
        self.expectThat(n2_ifaces, Equals([n2_eth0, n2_eth1]))

    def test__all_interfaces_parents_ignores_orphan_interfaces(self):
        # Previous versions of MAAS had a bug which resulted in an "orphan"
        # interface (an interface missing a pointer to its node). Because
        # we don't want this method to cause excessive querying, we expect
        # those to NOT show up.
        node = factory.make_Node()
        eth0 = factory.make_Interface(node=node, name="eth0")
        eth0_vlan = factory.make_Interface(
            iftype=INTERFACE_TYPE.VLAN, parents=[eth0], node=node
        )
        # Use the QuerySet update() to avoid calling the post-save handler,
        # which would otherwise automatically work around this.
        Interface.objects.filter(id=eth0_vlan.id).update(node=None)
        iface_list = list(Interface.objects.all_interfaces_parents_first(node))
        self.expectThat(iface_list, Equals([eth0]))


class InterfaceTest(MAASServerTestCase):
    def test_rejects_invalid_name(self):
        self.assertRaises(
            ValidationError,
            factory.make_Interface,
            INTERFACE_TYPE.PHYSICAL,
            name="invalid*name",
        )

    def test_rejects_invalid_mac_address(self):
        exception = self.assertRaises(
            ValidationError,
            factory.make_Interface,
            INTERFACE_TYPE.PHYSICAL,
            mac_address="invalid",
        )
        # XXX Danilo 2017-05-26 bug #1696108: we validate the MAC address
        # twice: once as part of Interface.clean() resulting in the __all__
        # error, and once as part of field validation that happens after a
        # few queries are done, so we cannot easily get rid of
        # validate_mac() in clean().
        self.assertThat(
            exception.message_dict,
            MatchesDict(
                {
                    "__all__": MatchesListwise(
                        [Equals("'invalid' is not a valid MAC address.")]
                    ),
                    "mac_address": MatchesListwise(
                        [Equals("'invalid' is not a valid MAC address.")]
                    ),
                }
            ),
        )

    def test_allows_blank_mac_address(self):
        factory.make_Interface(INTERFACE_TYPE.UNKNOWN, mac_address="")

    def test_allows_none_mac_address(self):
        factory.make_Interface(INTERFACE_TYPE.UNKNOWN, mac_address=None)

    def test_get_type_returns_None(self):
        self.assertIsNone(Interface.get_type())

    def test_creates_interface(self):
        name = factory.make_name("name")
        node = factory.make_Node()
        mac = factory.make_MAC()
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, name=name, node=node, mac_address=mac
        )
        self.assertThat(
            interface,
            MatchesStructure.byEquality(
                name=name,
                node=node,
                mac_address=mac,
                type=INTERFACE_TYPE.PHYSICAL,
            ),
        )

    def test_allows_null_vlan(self):
        name = factory.make_name("name")
        node = factory.make_Node()
        mac = factory.make_MAC()
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL,
            name=name,
            node=node,
            mac_address=mac,
            link_connected=False,
        )
        self.assertThat(
            interface,
            MatchesStructure.byEquality(
                name=name,
                node=node,
                mac_address=mac,
                type=INTERFACE_TYPE.PHYSICAL,
                vlan=None,
            ),
        )

    def test_doesnt_allow_acquired_to_be_true(self):
        name = factory.make_name("name")
        node = factory.make_Node()
        mac = factory.make_MAC()
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL,
            name=name,
            node=node,
            mac_address=mac,
            link_connected=False,
        )
        interface.acquired = True
        self.assertRaises(ValueError, interface.save)

    def test_string_representation_contains_essential_data(self):
        name = factory.make_name("name")
        node = factory.make_Node()
        mac = factory.make_MAC()
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, name=name, node=node, mac_address=mac
        )
        self.assertIn(mac.get_raw(), str(interface))
        self.assertIn(name, str(interface))

    def test_deletes_related_children(self):
        node = factory.make_Node()
        nic1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        nic2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        bond = factory.make_Interface(
            INTERFACE_TYPE.BOND, parents=[nic1, nic2]
        )
        vlan = factory.make_Interface(INTERFACE_TYPE.VLAN, parents=[bond])
        nic1.delete()
        # Should not be deleted yet.
        self.assertIsNotNone(reload_object(bond), "Bond was deleted.")
        self.assertIsNotNone(reload_object(vlan), "VLAN was deleted.")
        nic2.delete()
        # Should now all be deleted.
        self.assertIsNone(reload_object(bond), "Bond was not deleted.")
        self.assertIsNone(reload_object(vlan), "VLAN was not deleted.")

    def test_is_configured_returns_False_when_disabled(self):
        nic1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, enabled=False)
        self.assertFalse(nic1.is_configured())

    def test_is_configured_returns_False_when_no_links(self):
        nic1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, enabled=False)
        nic1.ip_addresses.clear()
        self.assertFalse(nic1.is_configured())

    def test_is_configured_returns_False_when_only_link_up(self):
        nic1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        nic1.ensure_link_up()
        self.assertFalse(nic1.is_configured())

    def test_is_configured_returns_True_when_other_link(self):
        nic1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        nic1.ensure_link_up()
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, interface=nic1
        )
        self.assertTrue(nic1.is_configured())

    def test_get_links_returns_links_for_each_type(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        links = []
        dhcp_subnet = factory.make_Subnet()
        dhcp_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DHCP,
            ip="",
            subnet=dhcp_subnet,
            interface=interface,
        )
        links.append(
            MatchesDict(
                {
                    "id": Equals(dhcp_ip.id),
                    "mode": Equals(INTERFACE_LINK_TYPE.DHCP),
                    "subnet": Equals(dhcp_subnet),
                }
            )
        )
        static_subnet = factory.make_Subnet()
        static_ip = factory.pick_ip_in_network(static_subnet.get_ipnetwork())
        sip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=static_ip,
            subnet=static_subnet,
            interface=interface,
        )
        links.append(
            MatchesDict(
                {
                    "id": Equals(sip.id),
                    "mode": Equals(INTERFACE_LINK_TYPE.STATIC),
                    "ip_address": Equals(static_ip),
                    "subnet": Equals(static_subnet),
                }
            )
        )
        temp_ip = factory.pick_ip_in_network(
            static_subnet.get_ipnetwork(), but_not=[static_ip]
        )
        temp_sip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            ip=temp_ip,
            subnet=static_subnet,
            interface=interface,
            temp_expires_on=datetime.datetime.utcnow(),
        )
        links.append(
            MatchesDict(
                {
                    "id": Equals(temp_sip.id),
                    "mode": Equals(INTERFACE_LINK_TYPE.AUTO),
                    "subnet": Equals(static_subnet),
                }
            )
        )
        link_subnet = factory.make_Subnet()
        link_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip="",
            subnet=link_subnet,
            interface=interface,
        )
        links.append(
            MatchesDict(
                {
                    "id": Equals(link_ip.id),
                    "mode": Equals(INTERFACE_LINK_TYPE.LINK_UP),
                    "subnet": Equals(link_subnet),
                }
            )
        )
        self.assertThat(interface.get_links(), MatchesListwise(links))

    def test_get_discovered_returns_None_when_empty(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        self.assertIsNone(interface.get_discovered())

    def test_get_discovered_returns_discovered_address_for_ipv4_and_ipv6(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        discovered_ips = []
        network_v4 = factory.make_ipv4_network()
        subnet_v4 = factory.make_Subnet(cidr=str(network_v4.cidr))
        ip_v4 = factory.pick_ip_in_network(network_v4)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            ip=ip_v4,
            subnet=subnet_v4,
            interface=interface,
        )
        discovered_ips.append(
            MatchesDict(
                {"ip_address": Equals(ip_v4), "subnet": Equals(subnet_v4)}
            )
        )
        network_v6 = factory.make_ipv6_network()
        subnet_v6 = factory.make_Subnet(cidr=str(network_v6.cidr))
        ip_v6 = factory.pick_ip_in_network(network_v6)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            ip=ip_v6,
            subnet=subnet_v6,
            interface=interface,
        )
        discovered_ips.append(
            MatchesDict(
                {"ip_address": Equals(ip_v6), "subnet": Equals(subnet_v6)}
            )
        )
        self.assertThat(
            interface.get_discovered(), MatchesListwise(discovered_ips)
        )

    def test_delete_deletes_related_ip_addresses(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        discovered_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, interface=interface
        )
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=interface
        )
        interface.delete()
        self.assertIsNone(reload_object(discovered_ip))
        self.assertIsNone(reload_object(static_ip))

    def test_remove_gateway_link_on_node_ipv4(self):
        node = factory.make_Node()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        network = factory.make_ipv4_network()
        subnet = factory.make_Subnet(cidr=str(network.cidr))
        ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_network(network),
            subnet=subnet,
            interface=interface,
        )
        node.gateway_link_ipv4 = ip
        node.save()
        reload_object(interface).ip_addresses.remove(ip)
        node = reload_object(node)
        self.assertIsNone(node.gateway_link_ipv4)

    def test_remove_gateway_link_on_node_ipv6(self):
        node = factory.make_Node()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        network = factory.make_ipv6_network()
        subnet = factory.make_Subnet(cidr=str(network.cidr))
        ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_network(network),
            subnet=subnet,
            interface=interface,
        )
        node.gateway_link_ipv6 = ip
        node.save()
        reload_object(interface).ip_addresses.remove(ip)
        node = reload_object(node)
        self.assertIsNone(node.gateway_link_ipv6)

    def test_get_ancestors_includes_grandparents(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        eth0_100 = factory.make_Interface(
            INTERFACE_TYPE.VLAN, node=node, parents=[eth0]
        )
        br0 = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE, node=node, parents=[eth0_100]
        )
        self.assertThat(br0.get_ancestors(), Equals({eth0, eth0_100}))

    def test_get_successors_includes_grandchildren(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        eth0_100 = factory.make_Interface(
            INTERFACE_TYPE.VLAN, node=node, parents=[eth0]
        )
        br0 = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE, node=node, parents=[eth0_100]
        )
        self.assertThat(eth0.get_successors(), Equals({eth0_100, br0}))

    def test_get_all_related_interafces_includes_all_related(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        eth0_100 = factory.make_Interface(
            INTERFACE_TYPE.VLAN, node=node, parents=[eth0]
        )
        eth0_101 = factory.make_Interface(
            INTERFACE_TYPE.VLAN, node=node, parents=[eth0]
        )
        br0 = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE, node=node, parents=[eth0_100]
        )
        self.assertThat(
            eth0_100.get_all_related_interfaces(),
            Equals({eth0, eth0_100, eth0_101, br0}),
        )

    def test_add_tag_adds_new_tag(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, tags=[])
        tag = factory.make_name("tag")
        interface.add_tag(tag)
        self.assertItemsEqual([tag], interface.tags)

    def test_add_tag_doesnt_duplicate(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, tags=[])
        tag = factory.make_name("tag")
        interface.add_tag(tag)
        interface.add_tag(tag)
        self.assertItemsEqual([tag], interface.tags)

    def test_remove_tag_deletes_tag(self):
        tag = factory.make_name("tag")
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, tags=[tag])
        interface.remove_tag(tag)
        self.assertItemsEqual([], interface.tags)

    def test_remove_tag_doesnt_error_on_missing_tag(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, tags=[])
        tag = factory.make_name("tag")
        #: Test is this doesn't raise an exception
        interface.remove_tag(tag)

    def test_save_link_speed_may_not_exceed_interface_speed(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface.interface_speed = 100
        interface.link_speed = 1000
        self.assertRaises(ValidationError, interface.save)

    def test_save_link_speed_may_exceed_unknown_interface_speed(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface.interface_speed = 0
        interface.link_speed = 1000
        interface.save()
        interface = reload_object(interface)
        self.assertEquals(0, interface.interface_speed)
        self.assertEquals(1000, interface.link_speed)

    def test_save_if_link_disconnected_set_link_speed_to_zero(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface.link_connected = False
        interface.save()
        self.assertEquals(0, interface.link_speed)


class InterfaceUpdateNeighbourTest(MAASServerTestCase):
    """Tests for `Interface.update_neighbour`."""

    def make_neighbour_json(self, ip=None, mac=None, time=None, **kwargs):
        """Returns a dictionary in the same JSON format that the region
        expects to receive from the rack.
        """
        if ip is None:
            ip = factory.make_ip_address(ipv6=False)
        if mac is None:
            mac = factory.make_mac_address()
        if time is None:
            time = random.randint(0, 200000000)
        if "vid" not in kwargs:
            has_vid = random.choice([True, False])
            if has_vid:
                vid = random.randint(1, 4094)
            else:
                vid = None
        return {"ip": ip, "mac": mac, "time": time, "vid": vid}

    def test__ignores_updates_if_neighbour_discovery_state_is_false(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.update_neighbour(self.make_neighbour_json())
        self.assertThat(Neighbour.objects.count(), Equals(0))

    def test___adds_new_neighbour_if_neighbour_discovery_state_is_true(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.neighbour_discovery_state = True
        iface.update_neighbour(self.make_neighbour_json())
        self.assertThat(Neighbour.objects.count(), Equals(1))

    def test___updates_existing_neighbour(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.neighbour_discovery_state = True
        json = self.make_neighbour_json()
        iface.update_neighbour(json)
        neighbour = get_one(Neighbour.objects.all())
        # Pretend this was updated one day ago.
        yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
        neighbour.save(_updated=yesterday, update_fields=["updated"])
        neighbour = reload_object(neighbour)
        self.assertThat(
            int(neighbour.updated.timestamp()),
            Equals(int(yesterday.timestamp())),
        )
        json["time"] += 1
        iface.update_neighbour(json)
        neighbour = reload_object(neighbour)
        self.assertThat(Neighbour.objects.count(), Equals(1))
        self.assertThat(neighbour.time, Equals(json["time"]))
        # This is the second time we saw this neighbour.
        neighbour = reload_object(neighbour)
        self.assertThat(neighbour.count, Equals(2))
        # Make sure the "last seen" time is correct.
        self.assertThat(neighbour.updated, Not(Equals(yesterday)))

    def test__replaces_obsolete_neighbour(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.neighbour_discovery_state = True
        json = self.make_neighbour_json()
        iface.update_neighbour(json)
        # Have a different MAC address claim ownership of the IP.
        json["time"] += 1
        json["mac"] = factory.make_mac_address()
        iface.update_neighbour(json)
        self.assertThat(Neighbour.objects.count(), Equals(1))
        self.assertThat(
            list(Neighbour.objects.all())[0].mac_address, Equals(json["mac"])
        )
        # This is the first time we saw this neighbour, because the original
        # binding was deleted.
        self.assertThat(list(Neighbour.objects.all())[0].count, Equals(1))

    def test__logs_new_binding(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.neighbour_discovery_state = True
        json = self.make_neighbour_json()
        with FakeLogger("maas.interface") as maaslog:
            iface.update_neighbour(json)
        self.assertDocTestMatches(
            "...: New MAC, IP binding observed...", maaslog.output
        )

    def test__logs_moved_binding(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.neighbour_discovery_state = True
        json = self.make_neighbour_json()
        iface.update_neighbour(json)
        # Have a different MAC address claim ownership of the IP.
        json["time"] += 1
        json["mac"] = factory.make_mac_address()
        with FakeLogger("maas.neighbour") as maaslog:
            iface.update_neighbour(json)
        self.assertDocTestMatches(
            "...: IP address...moved from...to...", maaslog.output
        )


class InterfaceUpdateMDNSEntryTest(MAASServerTestCase):
    """Tests for `Interface.update_mdns_entry`."""

    def make_mdns_entry_json(self, ip=None, hostname=None):
        """Returns a dictionary in the same JSON format that the region
        expects to receive from the rack.
        """
        if ip is None:
            ip = factory.make_ip_address(ipv6=False)
        if hostname is None:
            hostname = factory.make_hostname()
        return {"address": ip, "hostname": hostname}

    def test__ignores_updates_if_mdns_discovery_state_is_false(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.update_neighbour(self.make_mdns_entry_json())
        self.assertThat(MDNS.objects.count(), Equals(0))

    def test___adds_new_entry_if_mdns_discovery_state_is_true(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.mdns_discovery_state = True
        iface.update_mdns_entry(self.make_mdns_entry_json())
        self.assertThat(MDNS.objects.count(), Equals(1))

    def test___updates_existing_entry(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.mdns_discovery_state = True
        json = self.make_mdns_entry_json()
        iface.update_mdns_entry(json)
        yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
        mdns_entry = get_one(MDNS.objects.all())
        mdns_entry.save(_updated=yesterday, update_fields=["updated"])
        mdns_entry = reload_object(mdns_entry)
        self.assertThat(
            int(mdns_entry.updated.timestamp()),
            Equals(int(yesterday.timestamp())),
        )
        # First time we saw the entry.
        self.assertThat(mdns_entry.count, Equals(1))
        self.assertThat(MDNS.objects.count(), Equals(1))
        iface.update_mdns_entry(json)
        mdns_entry = reload_object(mdns_entry)
        self.assertThat(mdns_entry.ip, Equals(json["address"]))
        self.assertThat(mdns_entry.hostname, Equals(json["hostname"]))
        # This is the second time we saw this entry.
        self.assertThat(mdns_entry.count, Equals(2))
        self.assertThat(mdns_entry.updated, Not(Equals(yesterday)))

    def test__replaces_obsolete_entry(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.mdns_discovery_state = True
        json = self.make_mdns_entry_json()
        iface.update_mdns_entry(json)
        # Have a different IP address claim ownership of the hostname.
        json["address"] = factory.make_ip_address(ipv6=False)
        iface.update_mdns_entry(json)
        self.assertThat(MDNS.objects.count(), Equals(1))
        self.assertThat(MDNS.objects.first().ip, Equals(json["address"]))
        # This is the first time we saw this neighbour, because the original
        # binding was deleted.
        self.assertThat(MDNS.objects.count(), Equals(1))

    def test__logs_new_entry(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.mdns_discovery_state = True
        json = self.make_mdns_entry_json()
        with FakeLogger("maas.interface") as maaslog:
            iface.update_mdns_entry(json)
        self.assertDocTestMatches(
            "...: New mDNS entry resolved...", maaslog.output
        )

    def test__logs_moved_entry(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.mdns_discovery_state = True
        json = self.make_mdns_entry_json()
        iface.update_mdns_entry(json)
        # Have a different IP address claim ownership of the hostma,e.
        json["address"] = factory.make_ip_address(ipv6=False)
        with FakeLogger("maas.mDNS") as maaslog:
            iface.update_mdns_entry(json)
        self.assertDocTestMatches(
            "...: Hostname...moved from...to...", maaslog.output
        )

    def test__logs_updated_entry(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        iface.mdns_discovery_state = True
        json = self.make_mdns_entry_json()
        iface.update_mdns_entry(json)
        # Assign a different hostname to the IP.
        json["hostname"] = factory.make_hostname()
        with FakeLogger("maas.mDNS") as maaslog:
            iface.update_mdns_entry(json)
        self.assertDocTestMatches(
            "...: Hostname for...updated from...to...", maaslog.output
        )


class TestPhysicalInterface(MAASServerTestCase):
    def test_manager_returns_physical_interfaces(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        vlan = factory.make_VLAN()
        factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan, parents=[parent]
        )
        self.assertItemsEqual([parent], PhysicalInterface.objects.all())

    def test_get_node_returns_its_node(self):
        node = factory.make_Node()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        self.assertEqual(node, interface.get_node())

    def test_requires_node(self):
        interface = PhysicalInterface(
            name=factory.make_name("eth"),
            mac_address=factory.make_mac_address(),
        )
        error = self.assertRaises(ValidationError, interface.save)
        self.assertEqual(
            {"node": ["This field cannot be blank."]}, error.message_dict
        )

    def test_requires_mac_address(self):
        interface = PhysicalInterface(
            name=factory.make_name("eth"), node=factory.make_Node()
        )
        error = self.assertRaises(ValidationError, interface.save)
        self.assertEqual(
            {"mac_address": ["This field cannot be blank."]},
            error.message_dict,
        )

    def test_mac_address_must_be_unique(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        bad_interface = PhysicalInterface(
            node=interface.node,
            mac_address=interface.mac_address,
            name=factory.make_name("eth"),
        )
        error = self.assertRaises(ValidationError, bad_interface.save)
        self.assertEqual(
            {
                "mac_address": [
                    "This MAC address is already in use by %s."
                    % (interface.get_log_string())
                ]
            },
            error.message_dict,
        )

    def test_cannot_have_parents(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        error = self.assertRaises(
            ValidationError,
            factory.make_Interface,
            INTERFACE_TYPE.PHYSICAL,
            node=parent.node,
            parents=[parent],
        )
        self.assertEqual(
            {"parents": ["A physical interface cannot have parents."]},
            error.message_dict,
        )

    def test_can_be_disabled(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface.enabled = False
        # Test is that this does not fail.
        interface.save()
        self.assertFalse(reload_object(interface).enabled)


class PhysicalInterfaceTransactionalTest(MAASTransactionServerTestCase):
    """Test `PhysicalInterface` in across multiple transactions."""

    def test_duplicate_physical_macs_not_allowed(self):
        def _create_physical(mac):
            node = factory.make_Node(power_type="manual")
            vlan = factory.make_VLAN(dhcp_on=True)
            factory.make_Interface(
                INTERFACE_TYPE.PHYSICAL, node=node, vlan=vlan, mac_address=mac
            )

        def create_physical(mac):
            with transaction.atomic():
                _create_physical(mac)

        mac = factory.make_MAC()
        t = threading.Thread(target=create_physical, args=(mac,))

        with transaction.atomic():
            # Perform an actual query so that Django actually
            # starts the transaction.
            VLAN.objects.count()

            # Create same physical in another transaction.
            t.start()
            t.join()

            # Should fail as this is a duplicate physical MAC address.
            self.assertRaises(IntegrityError, _create_physical, mac)


class InterfaceMTUTest(MAASServerTestCase):
    def test_get_effective_mtu_returns_default_mtu(self):
        nic1 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, link_connected=False
        )
        self.assertEqual(DEFAULT_MTU, nic1.get_effective_mtu())

    def test_get_effective_mtu_returns_interface_mtu(self):
        nic1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        nic_mtu = random.randint(552, 9100)
        nic1.params = {"mtu": nic_mtu}
        nic1.save()
        self.assertEqual(nic_mtu, nic1.get_effective_mtu())

    def test_get_effective_mtu_returns_vlan_mtu(self):
        nic1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        vlan_mtu = random.randint(552, 9100)
        nic1.vlan.mtu = vlan_mtu
        nic1.vlan.save()
        self.assertEqual(vlan_mtu, nic1.get_effective_mtu())

    def test_get_effective_mtu_considers_jumbo_vlan_children(self):
        fabric = factory.make_Fabric()
        vlan = factory.make_VLAN(fabric=fabric)
        eth0 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=fabric.get_default_vlan()
        )
        eth0_vlan = factory.make_Interface(
            iftype=INTERFACE_TYPE.VLAN, vlan=vlan, parents=[eth0]
        )
        vlan_mtu = random.randint(DEFAULT_MTU + 1, 9100)
        eth0_vlan.vlan.mtu = vlan_mtu
        eth0_vlan.vlan.save()
        self.assertEqual(vlan_mtu, eth0.get_effective_mtu())

    def test_get_effective_mtu_returns_highest_vlan_mtu(self):
        fabric = factory.make_Fabric()
        vlan1 = factory.make_VLAN(fabric=fabric)
        vlan2 = factory.make_VLAN(fabric=fabric)
        eth0 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=fabric.get_default_vlan()
        )
        eth0_vlan1 = factory.make_Interface(
            iftype=INTERFACE_TYPE.VLAN, vlan=vlan1, parents=[eth0]
        )
        eth0_vlan2 = factory.make_Interface(
            iftype=INTERFACE_TYPE.VLAN, vlan=vlan2, parents=[eth0]
        )
        eth0_vlan1.vlan.mtu = random.randint(1000, 1999)
        eth0_vlan1.vlan.save()
        eth0_vlan2.vlan.mtu = random.randint(2000, 2999)
        eth0_vlan2.vlan.save()
        self.assertEqual(eth0_vlan2.vlan.mtu, eth0.get_effective_mtu())

    def test__creates_acquired_bridge_copies_mtu(self):
        mtu = random.randint(600, 9100)
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        parent.params = {"mtu": mtu}
        parent.save()
        bridge = parent.create_acquired_bridge()
        self.assertThat(
            bridge,
            MatchesStructure(
                name=Equals("%s" % parent.get_default_bridge_name()),
                mac_address=Equals(parent.mac_address),
                node=Equals(parent.node),
                vlan=Equals(parent.vlan),
                enabled=Equals(True),
                acquired=Equals(True),
                params=MatchesDict(
                    {
                        "bridge_type": Equals(BRIDGE_TYPE.STANDARD),
                        "bridge_stp": Equals(False),
                        "bridge_fd": Equals(15),
                        "mtu": Equals(mtu),
                    }
                ),
            ),
        )
        self.assertEquals([parent.id], [p.id for p in bridge.parents.all()])


class VLANInterfaceTest(MAASServerTestCase):
    def test_vlan_has_generated_name(self):
        name = factory.make_name("eth", size=2)
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, name=name)
        vlan = factory.make_VLAN()
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan, parents=[parent]
        )
        self.assertEqual(
            "%s.%d" % (parent.get_name(), vlan.vid), interface.name
        )

    def test_generated_name_gets_update_if_vlan_id_changes(self):
        name = factory.make_name("eth", size=2)
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, name=name)
        vlan = factory.make_VLAN()
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan, parents=[parent]
        )
        new_vlan = factory.make_VLAN()
        interface.vlan = new_vlan
        interface.save()
        self.assertEqual(
            "%s.%d" % (parent.get_name(), new_vlan.vid), interface.name
        )

    def test_vlan_on_rack_has_supplied_name(self):
        name = factory.make_name("eth", size=2)
        controller = random.choice(
            [
                factory.make_RegionController,
                factory.make_RackController,
                factory.make_RegionRackController,
            ]
        )()
        parent = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, name=name, node=controller
        )
        vlan = factory.make_VLAN()
        vlan_ifname = factory.make_name()
        interface = VLANInterface(
            node=controller,
            mac_address=factory.make_mac_address(),
            type=INTERFACE_TYPE.VLAN,
            name=vlan_ifname,
            vlan=vlan,
            enabled=True,
        )
        interface.save()
        InterfaceRelationship(child=interface, parent=parent).save()
        self.assertEqual(vlan_ifname, interface.name)

    def test_manager_returns_vlan_interfaces(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        vlan = factory.make_VLAN()
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan, parents=[parent]
        )
        self.assertItemsEqual([interface], VLANInterface.objects.all())

    def test_get_node_returns_parent_node(self):
        node = factory.make_Node()
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        vlan = factory.make_VLAN()
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan, parents=[parent]
        )
        self.assertEqual(node, interface.get_node())

    def test_removed_if_underlying_interface_gets_removed(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, parents=[parent]
        )
        parent.delete()
        self.assertIsNone(reload_object(interface))

    def test_can_only_have_one_parent(self):
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        error = self.assertRaises(
            ValidationError,
            factory.make_Interface,
            INTERFACE_TYPE.VLAN,
            parents=[parent1, parent2],
        )
        self.assertEqual(
            {"parents": ["VLAN interface must have exactly one parent."]},
            error.message_dict,
        )

    def test_must_have_one_parent(self):
        node = factory.make_Device()
        vlan = factory.make_VLAN(vid=1)
        error = self.assertRaises(
            ValidationError,
            factory.make_Interface,
            INTERFACE_TYPE.VLAN,
            node=node,
            vlan=vlan,
        )
        self.assertEqual(
            {"parents": ["VLAN interface must have exactly one parent."]},
            error.message_dict,
        )

    def test_parent_cannot_be_VLAN(self):
        physical = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        vlan = factory.make_Interface(INTERFACE_TYPE.VLAN, parents=[physical])
        error = self.assertRaises(
            ValidationError,
            factory.make_Interface,
            INTERFACE_TYPE.VLAN,
            parents=[vlan],
        )
        self.assertEqual(
            {
                "parents": [
                    "VLAN interface can only be created on a physical "
                    "or bond interface."
                ]
            },
            error.message_dict,
        )

    def test_node_set_to_parent_node(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, parents=[parent]
        )
        self.assertEqual(parent.node, interface.node)

    def test_mac_address_set_to_parent_mac_address(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, parents=[parent]
        )
        self.assertEqual(parent.mac_address, interface.mac_address)

    def test_updating_parent_mac_address_updates_vlan_mac_address(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, parents=[parent]
        )
        parent.mac_address = factory.make_mac_address()
        parent.save()
        interface = reload_object(interface)
        self.assertEqual(parent.mac_address, interface.mac_address)

    def test_disable_parent_disables_vlan_interface(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, parents=[parent]
        )
        parent.enabled = False
        parent.save()
        self.assertFalse(interface.is_enabled())
        self.assertFalse(reload_object(interface).enabled)

    def test_enable_parent_enables_vlan_interface(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, parents=[parent]
        )
        parent.enabled = False
        parent.save()
        parent.enabled = True
        parent.save()
        self.assertTrue(interface.is_enabled())
        self.assertTrue(reload_object(interface).enabled)

    def test_disable_bond_parents_disables_vlan_interface(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        bond = factory.make_Interface(
            INTERFACE_TYPE.BOND,
            mac_address=parent1.mac_address,
            parents=[parent1, parent2],
        )
        interface = factory.make_Interface(INTERFACE_TYPE.VLAN, parents=[bond])
        parent1.enabled = False
        parent1.save()
        parent2.enabled = False
        parent2.save()
        self.assertFalse(interface.is_enabled())
        self.assertFalse(reload_object(interface).enabled)

    def test_vlan_has_bootable_vlan_for_vlan(self):
        name = factory.make_name("eth", size=2)
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, name=name)
        vlan = factory.make_VLAN(dhcp_on=True)
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan, parents=[parent]
        )
        self.assertTrue(interface.has_bootable_vlan())

    def test_vlan_has_bootable_vlan_for_relay_vlan(self):
        name = factory.make_name("eth", size=2)
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, name=name)
        vlan = factory.make_VLAN(
            dhcp_on=False, relay_vlan=factory.make_VLAN(dhcp_on=True)
        )
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan, parents=[parent]
        )
        self.assertTrue(interface.has_bootable_vlan())

    def test_vlan_has_bootable_vlan_with_no_dhcp(self):
        name = factory.make_name("eth", size=2)
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, name=name)
        vlan = factory.make_VLAN()
        interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=vlan, parents=[parent]
        )
        self.assertFalse(interface.has_bootable_vlan())


class BondInterfaceTest(MAASServerTestCase):
    def test_manager_returns_bond_interfaces(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BOND, parents=[parent1, parent2]
        )
        self.assertItemsEqual([interface], BondInterface.objects.all())

    def test_get_node_returns_parent_node(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BOND, parents=[parent1, parent2]
        )
        self.assertItemsEqual([interface], BondInterface.objects.all())
        self.assertEqual(node, interface.get_node())

    def test_removed_if_underlying_interfaces_gets_removed(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BOND, parents=[parent1, parent2]
        )
        parent1.delete()
        parent2.delete()
        self.assertIsNone(reload_object(interface))

    def test_requires_mac_address(self):
        interface = BondInterface(
            name=factory.make_name("bond"), node=factory.make_Node()
        )
        error = self.assertRaises(ValidationError, interface.save)
        self.assertEqual(
            {"mac_address": ["This field cannot be blank."]},
            error.message_dict,
        )

    def test_parent_interfaces_must_belong_to_same_node(self):
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        error = self.assertRaises(
            ValidationError,
            factory.make_Interface,
            INTERFACE_TYPE.BOND,
            parents=[parent1, parent2],
        )
        self.assertEqual(
            {"parents": ["Parent interfaces do not belong to the same node."]},
            error.message_dict,
        )

    def test_parent_interfaces_must_be_physical(self):
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        vlan1 = factory.make_Interface(INTERFACE_TYPE.VLAN, parents=[parent1])
        error = self.assertRaises(
            ValidationError,
            factory.make_Interface,
            INTERFACE_TYPE.BOND,
            parents=[parent1, vlan1],
        )
        self.assertEqual(
            {"parents": ["Only physical interfaces can be bonded."]},
            error.message_dict,
        )

    def test_can_use_parents_mac_address(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        # Test is that no error is raised.
        factory.make_Interface(
            INTERFACE_TYPE.BOND,
            mac_address=parent1.mac_address,
            parents=[parent1, parent2],
        )

    def test_can_use_unique_mac_address(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        # Test is that no error is raised.
        factory.make_Interface(
            INTERFACE_TYPE.BOND,
            mac_address=factory.make_mac_address(),
            parents=[parent1, parent2],
        )

    def test_warns_for_non_unique_mac_address(self):
        logger = self.useFixture(FakeLogger("maas"))
        node = factory.make_Node()
        other_nic = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        iface = factory.make_Interface(
            INTERFACE_TYPE.BOND,
            mac_address=other_nic.mac_address,
            parents=[parent1, parent2],
        )
        self.assertThat(
            logger.output,
            Contains(
                "While adding %s: "
                "found a MAC address already in use by %s."
                % (iface.get_log_string(), other_nic.get_log_string())
            ),
        )

    def test_node_is_set_to_parents_node(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BOND,
            mac_address=factory.make_mac_address(),
            parents=[parent1, parent2],
        )
        self.assertEqual(interface.node, parent1.node)

    def test_disable_one_parent_doesnt_disable_the_bond(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BOND,
            mac_address=factory.make_mac_address(),
            parents=[parent1, parent2],
        )
        parent1.enabled = False
        parent1.save()
        self.assertTrue(interface.is_enabled())
        self.assertTrue(reload_object(interface).enabled)

    def test_disable_all_parents_disables_the_bond(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BOND,
            mac_address=factory.make_mac_address(),
            parents=[parent1, parent2],
        )
        parent1.enabled = False
        parent1.save()
        parent2.enabled = False
        parent2.save()
        self.assertFalse(interface.is_enabled())
        self.assertFalse(reload_object(interface).enabled)


class BridgeInterfaceTest(MAASServerTestCase):
    def test_manager_returns_bridge_interfaces(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE, parents=[parent1, parent2]
        )
        self.assertItemsEqual([interface], BridgeInterface.objects.all())

    def test_get_node_returns_parent_node(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE, parents=[parent1, parent2]
        )
        self.assertItemsEqual([interface], BridgeInterface.objects.all())
        self.assertEqual(node, interface.get_node())

    def test_removed_if_underlying_interfaces_gets_removed(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE, parents=[parent1, parent2]
        )
        parent1.delete()
        parent2.delete()
        self.assertIsNone(reload_object(interface))

    def test_requires_mac_address(self):
        interface = BridgeInterface(
            name=factory.make_name("bridge"), node=factory.make_Node()
        )
        error = self.assertRaises(ValidationError, interface.save)
        self.assertEqual(
            {"mac_address": ["This field cannot be blank."]},
            error.message_dict,
        )

    def test_allows_acquired_to_be_true(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        bridge = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE, parents=[parent]
        )
        bridge.acquired = True
        bridge.save()

    def test_parent_interfaces_must_belong_to_same_node(self):
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        error = self.assertRaises(
            ValidationError,
            factory.make_Interface,
            INTERFACE_TYPE.BRIDGE,
            parents=[parent1, parent2],
        )
        self.assertEqual(
            {"parents": ["Parent interfaces do not belong to the same node."]},
            error.message_dict,
        )

    def test_can_use_parents_mac_address(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        # Test is that no error is raised.
        factory.make_Interface(
            INTERFACE_TYPE.BRIDGE,
            mac_address=parent1.mac_address,
            parents=[parent1, parent2],
        )

    def test_can_use_unique_mac_address(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        # Test is that no error is raised.
        factory.make_Interface(
            INTERFACE_TYPE.BRIDGE,
            mac_address=factory.make_mac_address(),
            parents=[parent1, parent2],
        )

    def test_warns_for_non_unique_mac_address(self):
        logger = self.useFixture(FakeLogger("maas"))
        node = factory.make_Node()
        other_nic = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        # Test is that no error is raised.
        iface = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE,
            mac_address=other_nic.mac_address,
            parents=[parent1, parent2],
        )
        self.assertThat(
            logger.output,
            Contains(
                "While adding %s: "
                "found a MAC address already in use by %s."
                % (iface.get_log_string(), other_nic.get_log_string())
            ),
        )

    def test_node_is_set_to_parents_node(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE,
            mac_address=factory.make_mac_address(),
            parents=[parent1, parent2],
        )
        self.assertEqual(interface.node, parent1.node)

    def test_disable_one_parent_doesnt_disable_the_bridge(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE,
            mac_address=factory.make_mac_address(),
            parents=[parent1, parent2],
        )
        parent1.enabled = False
        parent1.save()
        self.assertTrue(interface.is_enabled())
        self.assertTrue(reload_object(interface).enabled)

    def test_disable_all_parents_disables_the_bridge(self):
        node = factory.make_Node()
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        parent2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE,
            mac_address=factory.make_mac_address(),
            parents=[parent1, parent2],
        )
        parent1.enabled = False
        parent1.save()
        parent2.enabled = False
        parent2.save()
        self.assertFalse(interface.is_enabled())
        self.assertFalse(reload_object(interface).enabled)


class UnknownInterfaceTest(MAASServerTestCase):
    def test_manager_returns_unknown_interfaces(self):
        unknown = factory.make_Interface(INTERFACE_TYPE.UNKNOWN)
        self.assertItemsEqual([unknown], UnknownInterface.objects.all())

    def test_get_node_returns_None(self):
        interface = factory.make_Interface(INTERFACE_TYPE.UNKNOWN)
        self.assertIsNone(interface.get_node())

    def test_doesnt_allow_node(self):
        interface = UnknownInterface(
            name="eth0",
            node=factory.make_Node(),
            mac_address=factory.make_mac_address(),
        )
        error = self.assertRaises(ValidationError, interface.save)
        self.assertEqual(
            {"node": ["This field must be blank."]}, error.message_dict
        )

    def test_warns_for_non_unique_unknown_mac(self):
        logger = self.useFixture(FakeLogger("maas"))
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        unknown = UnknownInterface(
            name="eth0", mac_address=interface.mac_address
        )
        unknown.save()
        self.assertThat(
            logger.output,
            Contains(
                "While adding %s: "
                "found a MAC address already in use by %s."
                % (unknown.get_log_string(), interface.get_log_string())
            ),
        )


class UpdateIpAddressesTest(MAASServerTestCase):
    def test__finds_ipv6_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network = factory.make_ipv6_network()
        subnet = factory.make_Subnet(cidr=network.cidr)
        cidr = "%s/128" % str(IPAddress(network.first + 1))
        interface.update_ip_addresses([cidr])

        self.assertFalse(Subnet.objects.filter(cidr=cidr).exists())
        self.assertEqual(interface.ip_addresses.first().subnet, subnet)

    def test__eui64_address_returns_correct_value(self):
        mac_address = factory.make_mac_address()
        network = factory.make_ipv6_network(slash=64)
        iface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, mac_address=mac_address
        )
        self.assertEqual(
            iface._eui64_address(network.cidr),
            EUI(mac_address).ipv6(network.first),
        )

    def test__does_not_add_eui_64_address(self):
        # See also LP#1639090.
        mac_address = factory.make_MAC()
        iface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, mac_address=mac_address
        )
        network = factory.make_ipv6_network(slash=64)
        cidr = "%s/64" % str(iface._eui64_address(network.cidr))
        iface.update_ip_addresses([cidr])
        self.assertEqual(0, iface.ip_addresses.count())
        self.assertEqual(1, Subnet.objects.filter(cidr=network.cidr).count())

    def test__does_not_add_addresses_from_duplicate_subnet(self):
        # See also LP#1803188.
        mac_address = factory.make_MAC()
        vlan = factory.make_VLAN()
        factory.make_Subnet(cidr="10.0.0.0/8", vlan=vlan)
        factory.make_Subnet(cidr="2001::/64", vlan=vlan)
        node = factory.make_Node()
        iface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL,
            mac_address=mac_address,
            vlan=vlan,
            node=node,
        )
        iface.update_ip_addresses(
            ["10.0.0.1/8", "10.0.0.2/8", "2001::1/64", "2001::2/64"]
        )
        self.assertEqual(2, iface.ip_addresses.count())

    def test__finds_ipv6_subnet_regardless_of_order(self):
        iface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network = factory.make_ipv6_network()
        subnet = factory.make_Subnet(cidr=network.cidr)
        cidr_net = str(network.cidr)
        cidr_128 = "%s/128" % str(IPAddress(network.first + 1))
        iface.update_ip_addresses([cidr_128, cidr_net])

        self.assertFalse(Subnet.objects.filter(cidr=cidr_128).exists())
        self.assertFalse(iface.ip_addresses.exclude(subnet=subnet).exists())

    def test__creates_missing_slash_64_ipv6_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network = factory.make_ipv6_network()
        cidr = "%s/128" % str(IPAddress(network.first + 1))
        interface.update_ip_addresses([cidr])

        subnets = Subnet.objects.filter(cidr="%s/64" % str(network.ip))
        self.assertEqual(1, len(subnets))
        self.assertFalse(Subnet.objects.filter(cidr=cidr).exists())
        self.assertEqual(interface.ip_addresses.first().subnet, subnets[0])

    def test__creates_missing_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network = factory.make_ip4_or_6_network()
        cidr = str(network)
        address = str(network.ip)
        interface.update_ip_addresses([cidr])

        default_fabric = Fabric.objects.get_default_fabric()
        subnets = Subnet.objects.filter(
            cidr=str(network.cidr), vlan__fabric=default_fabric
        )
        self.assertEqual(1, len(subnets))
        self.assertEqual(1, interface.ip_addresses.count())
        self.assertThat(
            interface.ip_addresses.first(),
            MatchesStructure.byEquality(
                alloc_type=IPADDRESS_TYPE.DISCOVERED,
                subnet=subnets[0],
                ip=address,
            ),
        )

    def test__creates_discovered_ip_addresses(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        vlan = VLAN.objects.get_default_vlan()
        num_connections = 3
        cidr_list = [
            str(factory.make_ip4_or_6_network())
            for _ in range(num_connections)
        ]
        subnet_list = [
            factory.make_Subnet(cidr=cidr, vlan=vlan) for cidr in cidr_list
        ]

        interface.update_ip_addresses(cidr_list)

        self.assertEqual(num_connections, interface.ip_addresses.count())
        for i in range(num_connections):
            ip = interface.ip_addresses.get(ip=cidr_list[i].split("/")[0])
            self.assertThat(
                ip,
                MatchesStructure.byEquality(
                    alloc_type=IPADDRESS_TYPE.DISCOVERED,
                    subnet=subnet_list[i],
                    ip=str(IPNetwork(cidr_list[i]).ip),
                ),
            )

    def test__links_interface_to_vlan_on_existing_subnet_with_logging(self):
        fabric1 = factory.make_Fabric()
        fabric2 = factory.make_Fabric()
        fabric3 = factory.make_Fabric()
        vlan1 = factory.make_VLAN(fabric=fabric1)
        vlan2 = factory.make_VLAN(fabric=fabric2)
        vlan3 = factory.make_VLAN(fabric=fabric3)
        subnet1 = factory.make_Subnet(vlan=vlan1)
        subnet2 = factory.make_Subnet(vlan=vlan2)
        subnet3 = factory.make_Subnet(vlan=vlan3)
        interface1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface3 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        maaslog = self.patch_autospec(interface_module, "maaslog")
        interface1.update_ip_addresses([subnet1.cidr])
        interface2.update_ip_addresses([subnet2.cidr])
        interface3.update_ip_addresses([subnet3.cidr])
        self.assertThat(interface1.vlan, Equals(vlan1))
        self.assertThat(interface2.vlan, Equals(vlan2))
        self.assertThat(interface3.vlan, Equals(vlan3))
        self.assertThat(
            maaslog.info,
            MockCallsMatch(
                call(
                    (
                        "%s: Observed connected to %s via %s."
                        % (
                            interface1.get_log_string(),
                            interface1.vlan.fabric.get_name(),
                            subnet1.cidr,
                        )
                    )
                ),
                call(
                    (
                        "%s: Observed connected to %s via %s."
                        % (
                            interface2.get_log_string(),
                            interface2.vlan.fabric.get_name(),
                            subnet2.cidr,
                        )
                    )
                ),
                call(
                    (
                        "%s: Observed connected to %s via %s."
                        % (
                            interface3.get_log_string(),
                            interface3.vlan.fabric.get_name(),
                            subnet3.cidr,
                        )
                    )
                ),
            ),
        )

    def test__deletes_old_discovered_ip_addresses_on_interface(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        # Create existing DISCOVERED IP address on the interface. These should
        # all be deleted.
        existing_discovered = [
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.DISCOVERED, interface=interface
            )
            for i in range(3)
        ]
        interface.update_ip_addresses([])
        self.assertEqual(
            0,
            len(reload_objects(StaticIPAddress, existing_discovered)),
            "Discovered IP address should have been deleted.",
        )

    def test__deletes_old_discovered_ip_addresses(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        vlan = VLAN.objects.get_default_vlan()
        num_connections = 3
        cidr_list = [
            str(factory.make_ip4_or_6_network())
            for _ in range(num_connections)
        ]
        subnet_list = [
            factory.make_Subnet(cidr=cidr, vlan=vlan) for cidr in cidr_list
        ]

        # Create existing DISCOVERED IP address with the same IP as those
        # that are going to be connected to the interface. These objects
        # should be deleted.
        existing_discovered = [
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.DISCOVERED,
                ip=str(IPNetwork(cidr_list[i]).ip),
                subnet=subnet_list[i],
            )
            for i in range(num_connections)
        ]

        interface.update_ip_addresses(cidr_list)

        self.assertEqual(
            0,
            len(reload_objects(StaticIPAddress, existing_discovered)),
            "Discovered IP address should have been deleted.",
        )
        self.assertEqual(num_connections, interface.ip_addresses.count())
        for i in range(num_connections):
            ip = interface.ip_addresses.get(ip=cidr_list[i].split("/")[0])
            self.assertThat(
                ip,
                MatchesStructure.byEquality(
                    alloc_type=IPADDRESS_TYPE.DISCOVERED,
                    subnet=subnet_list[i],
                    ip=str(IPNetwork(cidr_list[i]).ip),
                ),
            )

    def test__deletes_old_discovered_ip_addresses_with_unknown_nics(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        vlan = VLAN.objects.get_default_vlan()
        num_connections = 3
        cidr_list = [
            str(factory.make_ip4_or_6_network())
            for _ in range(num_connections)
        ]
        subnet_list = [
            factory.make_Subnet(cidr=cidr, vlan=vlan) for cidr in cidr_list
        ]

        # Create existing DISCOVERED IP address with the same IP as those
        # that are going to be connected to the interface. Each IP address
        # is linked to an UnknownInterface. The interfaces and the static IP
        # address should be deleted.
        existing_nics = [
            UnknownInterface.objects.create(
                name="eth0",
                mac_address=factory.make_mac_address(),
                vlan=subnet_list[i].vlan,
            )
            for i in range(num_connections)
        ]
        existing_discovered = [
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.DISCOVERED,
                ip=str(IPNetwork(cidr_list[i]).ip),
                subnet=subnet_list[i],
                interface=existing_nics[i],
            )
            for i in range(num_connections)
        ]

        interface.update_ip_addresses(cidr_list)

        self.assertEqual(
            0,
            len(reload_objects(StaticIPAddress, existing_discovered)),
            "Discovered IP address should have been deleted.",
        )
        self.assertEqual(
            0,
            len(reload_objects(UnknownInterface, existing_nics)),
            "Unknown interfaces should have been deleted.",
        )
        self.assertEqual(num_connections, interface.ip_addresses.count())
        for i in range(num_connections):
            ip = interface.ip_addresses.get(ip=cidr_list[i].split("/")[0])
            self.assertThat(
                ip,
                MatchesStructure.byEquality(
                    alloc_type=IPADDRESS_TYPE.DISCOVERED,
                    subnet=subnet_list[i],
                    ip=str(IPNetwork(cidr_list[i]).ip),
                ),
            )

    def test__deletes_old_sticky_ip_addresses_not_linked(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        vlan = VLAN.objects.get_default_vlan()
        num_connections = 3
        cidr_list = [
            str(factory.make_ip4_or_6_network())
            for _ in range(num_connections)
        ]
        subnet_list = [
            factory.make_Subnet(cidr=cidr, vlan=vlan) for cidr in cidr_list
        ]

        # Create existing DISCOVERED IP address with the same IP as those
        # that are going to be connected to the interface. These objects
        # should be deleted.
        existing_discovered = [
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.STICKY,
                ip=str(IPNetwork(cidr_list[i]).ip),
                subnet=subnet_list[i],
            )
            for i in range(num_connections)
        ]

        interface.update_ip_addresses(cidr_list)

        self.assertEqual(
            0,
            len(reload_objects(StaticIPAddress, existing_discovered)),
            "Sticky IP address should have been deleted.",
        )
        self.assertEqual(num_connections, interface.ip_addresses.count())
        for i in range(num_connections):
            ip = interface.ip_addresses.get(ip=cidr_list[i].split("/")[0])
            self.assertThat(
                ip,
                MatchesStructure.byEquality(
                    alloc_type=IPADDRESS_TYPE.DISCOVERED,
                    subnet=subnet_list[i],
                    ip=str(IPNetwork(cidr_list[i]).ip),
                ),
            )

    def test__deletes_old_ip_address_on_managed_subnet_with_log(self):
        network = factory.make_ip4_or_6_network()
        cidr = str(network)
        address = str(network.ip)
        vlan = VLAN.objects.get_default_vlan()
        vlan.dhcp_on = True
        vlan.save()
        subnet = factory.make_Subnet(cidr=cidr, vlan=vlan)
        other_interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=address,
            subnet=subnet,
            interface=other_interface,
        )
        maaslog = self.patch_autospec(interface_module, "maaslog")

        # Update that ip address on another interface. Which will log the
        # error message and delete the IP address.
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface.update_ip_addresses([cidr])

        self.assertThat(
            maaslog.warning,
            MockCalledOnceWith(
                "%s IP address (%s)%s was deleted because "
                "it was handed out by the MAAS DHCP server "
                "from the dynamic range.",
                ip.get_log_name_for_alloc_type(),
                address,
                " on " + other_interface.node.fqdn,
            ),
        )

    def test__deletes_old_ip_address_on_unmanaged_subnet_with_log(self):
        network = factory.make_ip4_or_6_network()
        cidr = str(network)
        address = str(network.ip)
        vlan = VLAN.objects.get_default_vlan()
        subnet = factory.make_Subnet(cidr=cidr, vlan=vlan)
        other_interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=address,
            subnet=subnet,
            interface=other_interface,
        )
        maaslog = self.patch_autospec(interface_module, "maaslog")

        # Update that ip address on another interface. Which will log the
        # error message and delete the IP address.
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface.update_ip_addresses([cidr])

        self.assertThat(
            maaslog.warning,
            MockCalledOnceWith(
                "%s IP address (%s)%s was deleted because "
                "it was handed out by an external DHCP "
                "server.",
                ip.get_log_name_for_alloc_type(),
                address,
                " on " + other_interface.node.fqdn,
            ),
        )


class TestLinkSubnet(MAASTransactionServerTestCase):
    """Tests for `Interface.link_subnet`."""

    def test__AUTO_creates_link_to_AUTO_with_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        auto_subnet = factory.make_Subnet(vlan=interface.vlan)
        interface.link_subnet(INTERFACE_LINK_TYPE.AUTO, auto_subnet)
        interface = reload_object(interface)
        auto_ip = interface.ip_addresses.get(alloc_type=IPADDRESS_TYPE.AUTO)
        self.assertEqual(auto_subnet, auto_ip.subnet)

    def test__DHCP_creates_link_to_DHCP_with_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        dhcp_subnet = factory.make_Subnet(vlan=interface.vlan)
        interface.link_subnet(INTERFACE_LINK_TYPE.DHCP, dhcp_subnet)
        interface = reload_object(interface)
        dhcp_ip = interface.ip_addresses.get(alloc_type=IPADDRESS_TYPE.DHCP)
        self.assertEqual(dhcp_subnet, dhcp_ip.subnet)

    def test__DHCP_creates_link_to_DHCP_without_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface.link_subnet(INTERFACE_LINK_TYPE.DHCP, None)
        interface = reload_object(interface)
        self.assertIsNotNone(
            get_one(
                interface.ip_addresses.filter(alloc_type=IPADDRESS_TYPE.DHCP)
            )
        )

    def test__STATIC_not_allowed_if_ip_address_not_in_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network = factory.make_ipv4_network()
        subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network.cidr)
        )
        ip_not_in_subnet = factory.make_ipv6_address()
        error = self.assertRaises(
            StaticIPAddressOutOfRange,
            interface.link_subnet,
            INTERFACE_LINK_TYPE.STATIC,
            subnet,
            ip_address=ip_not_in_subnet,
        )
        self.assertEqual(
            "IP address is not in the given subnet '%s'." % subnet, str(error)
        )

    def test__AUTO_link_sets_vlan_if_vlan_undefined(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network = factory.make_ipv4_network()
        subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network.cidr)
        )
        interface.vlan = None
        interface.save()
        interface.link_subnet(INTERFACE_LINK_TYPE.AUTO, subnet)
        interface = reload_object(interface)
        self.assertThat(interface.vlan, Equals(subnet.vlan))

    def test__STATIC_not_allowed_if_ip_address_in_dynamic_range(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_ipv4_Subnet_with_IPRanges(vlan=interface.vlan)
        ip_in_dynamic = IPAddress(subnet.get_dynamic_ranges().first().start_ip)
        error = self.assertRaises(
            StaticIPAddressOutOfRange,
            interface.link_subnet,
            INTERFACE_LINK_TYPE.STATIC,
            subnet,
            ip_address=ip_in_dynamic,
        )
        expected_range = subnet.get_dynamic_range_for_ip(ip_in_dynamic)
        self.assertEqual(
            "IP address is inside a dynamic range %s-%s."
            % (expected_range.start_ip, expected_range.end_ip),
            str(error),
        )

    def test__STATIC_sets_ip_in_no_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        ip = factory.make_ip_address()
        interface.link_subnet(INTERFACE_LINK_TYPE.STATIC, None, ip_address=ip)
        interface = reload_object(interface)
        self.assertIsNotNone(
            get_one(
                interface.ip_addresses.filter(
                    alloc_type=IPADDRESS_TYPE.STICKY, ip=ip, subnet=None
                )
            )
        )

    def test__STATIC_sets_ip_in_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        ip = factory.pick_ip_in_network(subnet.get_ipnetwork())
        interface.link_subnet(
            INTERFACE_LINK_TYPE.STATIC, subnet, ip_address=ip
        )
        interface = reload_object(interface)
        self.assertIsNotNone(
            get_one(
                interface.ip_addresses.filter(
                    alloc_type=IPADDRESS_TYPE.STICKY, ip=ip, subnet=subnet
                )
            )
        )

    @transactional
    def test__STATIC_picks_ip_in_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        interface.link_subnet(INTERFACE_LINK_TYPE.STATIC, subnet)
        interface = reload_object(interface)
        ip_address = get_one(
            interface.ip_addresses.filter(
                alloc_type=IPADDRESS_TYPE.STICKY, subnet=subnet
            )
        )
        self.assertIsNotNone(ip_address)
        self.assertIn(IPAddress(ip_address.ip), subnet.get_ipnetwork())

    def test__LINK_UP_creates_link_STICKY_with_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        link_subnet = factory.make_Subnet(vlan=interface.vlan)
        interface.link_subnet(INTERFACE_LINK_TYPE.LINK_UP, link_subnet)
        interface = reload_object(interface)
        link_ip = interface.ip_addresses.get(alloc_type=IPADDRESS_TYPE.STICKY)
        self.assertIsNone(link_ip.ip)
        self.assertEqual(link_subnet, link_ip.subnet)

    def test__LINK_UP_creates_link_STICKY_without_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface.link_subnet(INTERFACE_LINK_TYPE.LINK_UP, None)
        interface = reload_object(interface)
        link_ip = get_one(
            interface.ip_addresses.filter(alloc_type=IPADDRESS_TYPE.STICKY)
        )
        self.assertIsNotNone(link_ip)
        self.assertIsNone(link_ip.ip)


class TestForceAutoOrDHCPLink(MAASServerTestCase):
    """Tests for `Interface.force_auto_or_dhcp_link`."""

    def test__does_nothing_when_disconnected(self):
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, link_connected=False
        )
        self.assertIsNone(interface.force_auto_or_dhcp_link())

    def test__sets_to_AUTO_on_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = interface.force_auto_or_dhcp_link()
        self.assertEqual(IPADDRESS_TYPE.AUTO, static_ip.alloc_type)
        self.assertEqual(subnet, static_ip.subnet)

    def test__sets_to_DHCP(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        static_ip = interface.force_auto_or_dhcp_link()
        self.assertEqual(IPADDRESS_TYPE.DHCP, static_ip.alloc_type)
        self.assertIsNone(static_ip.subnet)


class TestEnsureLinkUp(MAASServerTestCase):
    """Tests for `Interface.ensure_link_up`."""

    def test__does_nothing_if_has_link(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        interface.link_subnet(INTERFACE_LINK_TYPE.DHCP, subnet)
        interface.ensure_link_up()
        interface = reload_object(interface)
        self.assertEqual(
            1,
            interface.ip_addresses.count(),
            "Should only have one IP address assigned.",
        )

    def test__does_nothing_if_no_vlan(self):
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, link_connected=False
        )
        interface.ensure_link_up()
        interface = reload_object(interface)
        self.assertEqual(
            0,
            interface.ip_addresses.count(),
            "Should only have no IP address assigned.",
        )

    def test__removes_other_link_ups_if_other_link_exists(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        link_ups = [
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.STICKY, ip="", interface=interface
            )
            for _ in range(3)
        ]
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, interface=interface
        )
        interface.ensure_link_up()
        self.assertItemsEqual([], reload_objects(StaticIPAddress, link_ups))

    def test__creates_link_up_to_discovered_subnet_on_same_vlan(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        interface.ensure_link_up()
        link_ip = interface.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.STICKY
        ).first()
        self.assertIsNone(link_ip.ip)
        self.assertEqual(subnet, link_ip.subnet)

    def test__creates_link_up_to_no_subnet_when_on_different_vlan(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet()
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        interface.ensure_link_up()
        link_ip = interface.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.STICKY
        ).first()
        self.assertIsNone(link_ip.ip)
        self.assertIsNone(link_ip.subnet)

    def test__creates_link_up_to_no_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        interface.ensure_link_up()
        link_ip = interface.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.STICKY
        ).first()
        self.assertIsNone(link_ip.ip)
        self.assertIsNone(link_ip.subnet)


class TestUnlinkIPAddress(MAASServerTestCase):
    """Tests for `Interface.unlink_ip_address`."""

    def test__doesnt_call_ensure_link_up_if_clearing_config(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        auto_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        mock_ensure_link_up = self.patch_autospec(interface, "ensure_link_up")
        interface.unlink_ip_address(auto_ip, clearing_config=True)
        self.assertIsNone(reload_object(auto_ip))
        self.assertThat(mock_ensure_link_up, MockNotCalled())


class TestUnlinkSubnet(MAASServerTestCase):
    """Tests for `Interface.unlink_subnet`."""

    def test__AUTO_deletes_link(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        auto_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        interface.unlink_subnet_by_id(auto_ip.id)
        self.assertIsNone(reload_object(auto_ip))

    def test__DHCP_deletes_link_with_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        dhcp_subnet = factory.make_Subnet(vlan=interface.vlan)
        interface.link_subnet(INTERFACE_LINK_TYPE.DHCP, dhcp_subnet)
        interface = reload_object(interface)
        dhcp_ip = interface.ip_addresses.get(alloc_type=IPADDRESS_TYPE.DHCP)
        interface.unlink_subnet_by_id(dhcp_ip.id)
        self.assertIsNone(reload_object(dhcp_ip))

    def test__STATIC_deletes_link_in_no_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        ip = factory.make_ip_address()
        interface.link_subnet(INTERFACE_LINK_TYPE.STATIC, None, ip_address=ip)
        interface = reload_object(interface)
        static_ip = get_one(
            interface.ip_addresses.filter(
                alloc_type=IPADDRESS_TYPE.STICKY, ip=ip, subnet=None
            )
        )
        interface.unlink_subnet_by_id(static_ip.id)
        self.assertIsNone(reload_object(static_ip))

    def test__STATIC_deletes_link_in_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        ip = factory.pick_ip_in_network(subnet.get_ipnetwork())
        interface.link_subnet(
            INTERFACE_LINK_TYPE.STATIC, subnet, ip_address=ip
        )
        interface = reload_object(interface)
        static_ip = get_one(
            interface.ip_addresses.filter(
                alloc_type=IPADDRESS_TYPE.STICKY, ip=ip, subnet=subnet
            )
        )
        interface.unlink_subnet_by_id(static_ip.id)
        self.assertIsNone(reload_object(static_ip))

    def test__LINK_UP_deletes_link(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        link_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        interface.unlink_subnet_by_id(link_ip.id)
        self.assertIsNone(reload_object(link_ip))

    def test__always_has_LINK_UP(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        link_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        interface.unlink_subnet_by_id(link_ip.id)
        self.assertIsNone(reload_object(link_ip))
        self.assertIsNotNone(
            interface.ip_addresses.filter(
                alloc_type=IPADDRESS_TYPE.STICKY, ip=None
            ).first()
        )


class TestUpdateIPAddress(MAASTransactionServerTestCase):
    """Tests for `Interface.update_ip_address`."""

    def test__switch_dhcp_to_auto(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DHCP,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        new_subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.AUTO, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.AUTO, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNone(static_ip.ip)

    def test__switch_dhcp_to_link_up(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DHCP,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        new_subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.LINK_UP, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.STICKY, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNone(static_ip.ip)

    @transactional
    def test__switch_dhcp_to_static(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network_v4 = factory.make_ipv4_network(slash=24)
        subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network_v4.cidr)
        )
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DHCP,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        network_v6 = factory.make_ipv6_network(slash=24)
        new_subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network_v6.cidr)
        )
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.STATIC, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.STICKY, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNotNone(static_ip.ip)

    def test__switch_auto_to_dhcp(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        new_subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.DHCP, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.DHCP, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNone(static_ip.ip)

    def test__switch_auto_to_link_up(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        new_subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.LINK_UP, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.STICKY, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNone(static_ip.ip)

    @transactional
    def test__switch_auto_to_static(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network_v4 = factory.make_ipv4_network(slash=24)
        subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network_v4.cidr)
        )
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        network_v6 = factory.make_ipv6_network(slash=24)
        new_subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network_v6.cidr)
        )
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.STATIC, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.STICKY, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNotNone(static_ip.ip)

    def test__switch_link_up_to_auto(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        new_subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.AUTO, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.AUTO, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNone(static_ip.ip)

    def test__switch_link_up_to_dhcp(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        new_subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.DHCP, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.DHCP, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNone(static_ip.ip)

    @transactional
    def test__switch_link_up_to_static(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network_v4 = factory.make_ipv4_network(slash=24)
        subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network_v4.cidr)
        )
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        network_v6 = factory.make_ipv6_network(slash=24)
        new_subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network_v6.cidr)
        )
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.STATIC, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.STICKY, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNotNone(static_ip.ip)

    def test__switch_static_to_dhcp(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_Subnet(subnet),
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        new_subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.DHCP, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.DHCP, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNone(static_ip.ip)

    def test__switch_static_to_auto(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_Subnet(subnet),
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        new_subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.AUTO, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.AUTO, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNone(static_ip.ip)

    def test__switch_static_to_link_up(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_Subnet(subnet),
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        new_subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.LINK_UP, new_subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.STICKY, static_ip.alloc_type)
        self.assertEqual(new_subnet, static_ip.subnet)
        self.assertIsNone(static_ip.ip)

    def test__switch_static_to_same_subnet_does_nothing(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_Subnet(subnet),
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        static_ip_address = static_ip.ip
        static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.STATIC, subnet
        )
        self.assertEqual(static_id, static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.STICKY, static_ip.alloc_type)
        self.assertEqual(subnet, static_ip.subnet)
        self.assertEqual(static_ip_address, static_ip.ip)

    def test__switch_static_to_already_used_ip_address(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_Subnet(subnet),
            subnet=subnet,
            interface=interface,
        )
        other_interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        used_ip_address = factory.pick_ip_in_Subnet(
            subnet, but_not=[static_ip.ip]
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=used_ip_address,
            subnet=subnet,
            interface=other_interface,
        )
        with ExpectedException(StaticIPAddressUnavailable):
            interface.update_ip_address(
                static_ip,
                INTERFACE_LINK_TYPE.STATIC,
                subnet,
                ip_address=used_ip_address,
            )

    def test__switch_static_to_same_subnet_with_different_ip(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network = factory.make_ipv4_network(slash=24)
        subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network.cidr)
        )
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_Subnet(subnet),
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        static_ip_address = static_ip.ip
        new_ip_address = factory.pick_ip_in_Subnet(
            subnet, but_not=[static_ip_address]
        )
        new_static_ip = interface.update_ip_address(
            static_ip,
            INTERFACE_LINK_TYPE.STATIC,
            subnet,
            ip_address=new_ip_address,
        )
        self.assertEqual(static_id, new_static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.STICKY, new_static_ip.alloc_type)
        self.assertEqual(subnet, new_static_ip.subnet)
        self.assertEqual(new_ip_address, new_static_ip.ip)

    @transactional
    def test__switch_static_to_another_subnet(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network_v4 = factory.make_ipv4_network(slash=24)
        subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network_v4.cidr)
        )
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_Subnet(subnet),
            subnet=subnet,
            interface=interface,
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_Subnet(subnet, but_not=[static_ip.ip]),
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        network_v6 = factory.make_ipv6_network(slash=24)
        new_subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network_v6.cidr)
        )
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_Subnet(new_subnet),
            subnet=new_subnet,
            interface=interface,
        )
        new_static_ip = interface.update_ip_address(
            static_ip, INTERFACE_LINK_TYPE.STATIC, new_subnet
        )
        self.assertEqual(static_id, new_static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.STICKY, new_static_ip.alloc_type)
        self.assertEqual(new_subnet, new_static_ip.subnet)
        self.assertIsNotNone(new_static_ip.ip)

    def test__switch_static_to_another_subnet_with_ip_address(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        network_v4 = factory.make_ipv4_network(slash=24)
        subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network_v4.cidr)
        )
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip=factory.pick_ip_in_Subnet(subnet),
            subnet=subnet,
            interface=interface,
        )
        static_id = static_ip.id
        network_v6 = factory.make_ipv6_network(slash=24)
        new_subnet = factory.make_Subnet(
            vlan=interface.vlan, cidr=str(network_v6.cidr)
        )
        new_ip_address = factory.pick_ip_in_Subnet(new_subnet)
        new_static_ip = interface.update_ip_address(
            static_ip,
            INTERFACE_LINK_TYPE.STATIC,
            new_subnet,
            ip_address=new_ip_address,
        )
        self.assertEqual(static_id, new_static_ip.id)
        self.assertEqual(IPADDRESS_TYPE.STICKY, new_static_ip.alloc_type)
        self.assertEqual(new_subnet, new_static_ip.subnet)
        self.assertEqual(new_ip_address, new_static_ip.ip)


class TestUpdateLinkById(MAASServerTestCase):
    """Tests for `Interface.update_link_by_id`."""

    def test__calls_update_ip_address_with_ip_address(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DHCP,
            ip="",
            subnet=subnet,
            interface=interface,
        )
        mock_update_ip_address = self.patch_autospec(
            interface, "update_ip_address"
        )
        interface.update_link_by_id(
            static_ip.id, INTERFACE_LINK_TYPE.AUTO, subnet
        )
        self.expectThat(
            mock_update_ip_address,
            MockCalledOnceWith(
                static_ip, INTERFACE_LINK_TYPE.AUTO, subnet, ip_address=None
            ),
        )


class TestClaimAutoIPs(MAASTransactionServerTestCase):
    """Tests for `Interface.claim_auto_ips`."""

    def test__claims_all_auto_ip_addresses(self):
        with transaction.atomic():
            interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
            for _ in range(3):
                subnet = factory.make_ipv4_Subnet_with_IPRanges(
                    vlan=interface.vlan
                )
                factory.make_StaticIPAddress(
                    alloc_type=IPADDRESS_TYPE.AUTO,
                    ip="",
                    subnet=subnet,
                    interface=interface,
                )
        with transaction.atomic():
            observed = interface.claim_auto_ips()
        # Should now have 3 AUTO with IP addresses assigned.
        interface = reload_object(interface)
        assigned_addresses = interface.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.AUTO
        )
        assigned_addresses = [ip for ip in assigned_addresses if ip.ip]
        self.assertEqual(
            3,
            len(assigned_addresses),
            "Should have 3 AUTO IP addresses with an IP address assigned.",
        )
        self.assertItemsEqual(assigned_addresses, observed)

    def test__keeps_ip_address_ids_consistent(self):
        auto_ip_ids = []
        with transaction.atomic():
            interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
            for _ in range(3):
                subnet = factory.make_ipv4_Subnet_with_IPRanges(
                    vlan=interface.vlan
                )
                auto_ip = factory.make_StaticIPAddress(
                    alloc_type=IPADDRESS_TYPE.AUTO,
                    ip="",
                    subnet=subnet,
                    interface=interface,
                )
                auto_ip_ids.append(auto_ip.id)
        with transaction.atomic():
            observed = interface.claim_auto_ips()
        # Should now have 3 AUTO with IP addresses assigned.
        interface = reload_object(interface)
        assigned_addresses = interface.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.AUTO
        )
        assigned_addresses = [ip for ip in assigned_addresses if ip.ip]
        self.assertEqual(
            3,
            len(assigned_addresses),
            "Should have 3 AUTO IP addresses with an IP address assigned.",
        )
        self.assertItemsEqual(assigned_addresses, observed)
        # Make sure the IDs didn't change upon allocation.
        self.assertItemsEqual(
            auto_ip_ids, (ip.id for ip in assigned_addresses)
        )

    def test__claims_all_missing_assigned_auto_ip_addresses(self):
        with transaction.atomic():
            interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
            for _ in range(3):
                subnet = factory.make_Subnet(vlan=interface.vlan)
                ip = factory.pick_ip_in_network(subnet.get_ipnetwork())
                factory.make_StaticIPAddress(
                    alloc_type=IPADDRESS_TYPE.AUTO,
                    ip=ip,
                    subnet=subnet,
                    interface=interface,
                )
            subnet = factory.make_Subnet(vlan=interface.vlan)
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                ip="",
                subnet=subnet,
                interface=interface,
            )
        with transaction.atomic():
            observed = interface.claim_auto_ips()
        self.assertEqual(
            1,
            len(observed),
            "Should have 1 AUTO IP addresses with an IP address assigned.",
        )
        self.assertEqual(subnet, observed[0].subnet)
        self.assertTrue(
            IPAddress(observed[0].ip) in observed[0].subnet.get_ipnetwork(),
            "Assigned IP address should be inside the subnet network.",
        )

    def test__claims_ip_address_not_in_dynamic_ip_range(self):
        with transaction.atomic():
            subnet = factory.make_ipv4_Subnet_with_IPRanges()
            interface = factory.make_Interface(
                INTERFACE_TYPE.PHYSICAL, vlan=subnet.vlan
            )
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                ip="",
                subnet=subnet,
                interface=interface,
            )
        with transaction.atomic():
            observed = interface.claim_auto_ips()
        self.assertEqual(
            1,
            len(observed),
            "Should have 1 AUTO IP addresses with an IP address assigned.",
        )
        self.assertEqual(subnet, observed[0].subnet)
        self.assertThat(
            subnet.get_dynamic_range_for_ip(observed[0].ip), Equals(None)
        )
        self.assertTrue(subnet.is_valid_static_ip(observed[0].ip))

    def test__claims_ip_address_in_static_ip_range_skips_gateway_ip(self):
        with transaction.atomic():
            interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
            network = factory.make_ipv4_network(slash=30)
            subnet = factory.make_Subnet(
                vlan=interface.vlan, cidr=str(network.cidr)
            )
            # Make it so only one IP is available.
            subnet.gateway_ip = str(IPAddress(network.first))
            subnet.save()
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                ip="",
                subnet=subnet,
                interface=interface,
            )
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.STICKY,
                ip=str(IPAddress(network.first + 1)),
                subnet=subnet,
                interface=interface,
            )
        with transaction.atomic():
            observed = interface.claim_auto_ips()
        self.assertEquals(
            1,
            len(observed),
            "Should have 1 AUTO IP addresses with an IP address assigned.",
        )
        self.assertEquals(subnet, observed[0].subnet)
        self.assertEquals(
            IPAddress(network.first + 2), IPAddress(observed[0].ip)
        )

    def test__claim_fails_if_subnet_missing(self):
        with transaction.atomic():
            interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
            subnet = factory.make_Subnet(vlan=interface.vlan)
            ip = factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                ip="",
                subnet=subnet,
                interface=interface,
            )
            ip.subnet = None
            ip.save()
            maaslog = self.patch_autospec(interface_module, "maaslog")
        with transaction.atomic():
            with ExpectedException(StaticIPAddressUnavailable):
                interface.claim_auto_ips()
        self.expectThat(
            maaslog.error,
            MockCalledOnceWith(
                "Could not find subnet for interface %s."
                % interface.get_log_string()
            ),
        )

    def test__excludes_ip_addresses_in_exclude_addresses(self):
        with transaction.atomic():
            interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
            subnet = factory.make_Subnet(vlan=interface.vlan)
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                ip="",
                subnet=subnet,
                interface=interface,
            )
            exclude = get_first_and_last_usable_host_in_network(
                subnet.get_ipnetwork()
            )[0]
        with transaction.atomic():
            interface.claim_auto_ips(exclude_addresses=set([str(exclude)]))
            auto_ip = interface.ip_addresses.get(
                alloc_type=IPADDRESS_TYPE.AUTO
            )
        self.assertNotEqual(IPAddress(exclude), IPAddress(auto_ip.ip))

    def test__can_acquire_multiple_address_from_the_same_subnet(self):
        with transaction.atomic():
            interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
            subnet = factory.make_ipv4_Subnet_with_IPRanges(
                vlan=interface.vlan
            )
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                ip="",
                subnet=subnet,
                interface=interface,
            )
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                ip="",
                subnet=subnet,
                interface=interface,
            )
        with transaction.atomic():
            interface.claim_auto_ips()
            auto_ips = interface.ip_addresses.filter(
                alloc_type=IPADDRESS_TYPE.AUTO
            ).order_by("id")
        self.assertEqual(
            IPAddress(auto_ips[0].ip) + 1, IPAddress(auto_ips[1].ip)
        )

    def test__claims_all_auto_ip_addresses_with_temp_expires_on(self):
        with transaction.atomic():
            interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
            for _ in range(3):
                subnet = factory.make_ipv4_Subnet_with_IPRanges(
                    vlan=interface.vlan
                )
                factory.make_StaticIPAddress(
                    alloc_type=IPADDRESS_TYPE.AUTO,
                    ip="",
                    subnet=subnet,
                    interface=interface,
                )
        with transaction.atomic():
            observed = interface.claim_auto_ips(
                temp_expires_after=datetime.timedelta(minutes=5)
            )
        # Should now have 3 AUTO with IP addresses assigned.
        interface = reload_object(interface)
        assigned_addresses = interface.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.AUTO
        )
        assigned_addresses = [
            ip for ip in assigned_addresses if ip.ip and ip.temp_expires_on
        ]
        self.assertEqual(
            3,
            len(assigned_addresses),
            "Should have 3 AUTO IP addresses with an IP address assigned "
            "and temp_expires_on set.",
        )
        self.assertItemsEqual(assigned_addresses, observed)


class TestCreateAcquiredBridge(MAASServerTestCase):
    """Tests for `Interface.create_acquired_bridge`."""

    def test__raises_ValueError_for_bridge(self):
        bridge = factory.make_Interface(INTERFACE_TYPE.BRIDGE)
        self.assertRaises(ValueError, bridge.create_acquired_bridge)

    def test__creates_acquired_bridge_with_default_options(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        bridge = parent.create_acquired_bridge()
        self.assertThat(
            bridge,
            MatchesStructure(
                name=Equals("%s" % parent.get_default_bridge_name()),
                mac_address=Equals(parent.mac_address),
                node=Equals(parent.node),
                vlan=Equals(parent.vlan),
                enabled=Equals(True),
                acquired=Equals(True),
                params=MatchesDict(
                    {
                        "bridge_type": Equals(BRIDGE_TYPE.STANDARD),
                        "bridge_stp": Equals(False),
                        "bridge_fd": Equals(15),
                    }
                ),
            ),
        )
        self.assertEquals([parent.id], [p.id for p in bridge.parents.all()])

    def test__creates_acquired_bridge_with_passed_options(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        bridge_type = factory.pick_choice(BRIDGE_TYPE_CHOICES)
        bridge_stp = factory.pick_bool()
        bridge_fd = random.randint(0, 500)
        bridge = parent.create_acquired_bridge(
            bridge_type=bridge_type, bridge_stp=bridge_stp, bridge_fd=bridge_fd
        )
        self.assertThat(
            bridge,
            MatchesStructure(
                name=Equals("%s" % parent.get_default_bridge_name()),
                mac_address=Equals(parent.mac_address),
                node=Equals(parent.node),
                vlan=Equals(parent.vlan),
                enabled=Equals(True),
                acquired=Equals(True),
                params=MatchesDict(
                    {
                        "bridge_type": Equals(bridge_type),
                        "bridge_stp": Equals(bridge_stp),
                        "bridge_fd": Equals(bridge_fd),
                    }
                ),
            ),
        )
        self.assertEquals([parent.id], [p.id for p in bridge.parents.all()])

    def test__creates_acquired_bridge_moves_links_from_parent_to_bridge(self):
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        auto_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO, interface=parent
        )
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, interface=parent
        )
        bridge = parent.create_acquired_bridge()
        self.assertThat(
            bridge,
            MatchesStructure(
                name=Equals("%s" % parent.get_default_bridge_name()),
                mac_address=Equals(parent.mac_address),
                node=Equals(parent.node),
                vlan=Equals(parent.vlan),
                enabled=Equals(True),
                acquired=Equals(True),
                params=MatchesDict(
                    {
                        "bridge_type": Equals(BRIDGE_TYPE.STANDARD),
                        "bridge_stp": Equals(False),
                        "bridge_fd": Equals(15),
                    }
                ),
            ),
        )
        self.assertEquals([parent.id], [p.id for p in bridge.parents.all()])
        self.assertEquals(
            [bridge.id], [nic.id for nic in auto_ip.interface_set.all()]
        )
        self.assertEquals(
            [bridge.id], [nic.id for nic in static_ip.interface_set.all()]
        )


class TestReleaseAutoIPs(MAASServerTestCase):
    """Tests for `Interface.release_auto_ips`."""

    def test__clears_all_auto_ips_with_ips(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        for _ in range(3):
            subnet = factory.make_Subnet(vlan=interface.vlan)
            ip = factory.pick_ip_in_network(subnet.get_ipnetwork())
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                ip=ip,
                subnet=subnet,
                interface=interface,
            )
        observed = interface.release_auto_ips()

        # Should now have 3 AUTO with no IP addresses assigned.
        interface = reload_object(interface)
        releases_addresses = interface.ip_addresses.filter(
            alloc_type=IPADDRESS_TYPE.AUTO
        )
        releases_addresses = [rip for rip in releases_addresses if not rip.ip]
        self.assertEqual(
            3,
            len(releases_addresses),
            "Should have 3 AUTO IP addresses with no IP address assigned.",
        )
        self.assertItemsEqual(releases_addresses, observed)

    def test__clears_only_auto_ips_with_ips(self):
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        for _ in range(2):
            subnet = factory.make_Subnet(vlan=interface.vlan)
            factory.make_StaticIPAddress(
                alloc_type=IPADDRESS_TYPE.AUTO,
                ip="",
                subnet=subnet,
                interface=interface,
            )
        subnet = factory.make_Subnet(vlan=interface.vlan)
        ip = factory.pick_ip_in_network(subnet.get_ipnetwork())
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            ip=ip,
            subnet=subnet,
            interface=interface,
        )
        observed = interface.release_auto_ips()
        self.assertEqual(
            1,
            len(observed),
            "Should have 1 AUTO IP addresses that was released.",
        )
        self.assertEqual(subnet, observed[0].subnet)
        self.assertIsNone(observed[0].ip)


class TestInterfaceUpdateDiscovery(MAASServerTestCase):
    """Tests for `Interface.update_discovery_state`.

    Note: these tests make extensive use of reload_object() to help ensure that
    the update_fields=[...] parameter to save() is correct.
    """

    def test__monitored_flag_vetoes_discovery_state(self):
        settings = {"monitored": False}
        iface = factory.make_Interface()
        iface.update_discovery_state(
            NetworkDiscoveryConfig(passive=True, active=False),
            settings=settings,
        )
        iface = reload_object(iface)
        self.expectThat(iface.neighbour_discovery_state, Is(False))

    def test__sets_neighbour_state_true_when_monitored_flag_is_true(self):
        settings = {"monitored": True}
        iface = factory.make_Interface()
        iface.update_discovery_state(
            NetworkDiscoveryConfig(passive=True, active=False),
            settings=settings,
        )
        iface = reload_object(iface)
        self.expectThat(iface.neighbour_discovery_state, Is(True))

    def test__sets_mdns_state_based_on_passive_setting(self):
        settings = {"monitored": False}
        iface = factory.make_Interface()
        iface.update_discovery_state(
            NetworkDiscoveryConfig(passive=False, active=False),
            settings=settings,
        )
        iface = reload_object(iface)
        self.expectThat(iface.mdns_discovery_state, Is(False))
        iface.update_discovery_state(
            NetworkDiscoveryConfig(passive=True, active=False),
            settings=settings,
        )
        iface = reload_object(iface)
        self.expectThat(iface.mdns_discovery_state, Is(True))


class TestInterfaceGetDiscoveryStateTest(MAASServerTestCase):
    def test__reports_correct_parameters(self):
        iface = factory.make_Interface()
        iface.neighbour_discovery_state = random.choice([True, False])
        iface.mdns_discovery_state = random.choice([True, False])
        state = iface.get_discovery_state()
        self.assertThat(
            state["neighbour"], Equals(iface.neighbour_discovery_state)
        )
        self.assertThat(state["mdns"], Equals(iface.mdns_discovery_state))


class TestReportVID(MAASServerTestCase):
    """Tests for `Interface.release_auto_ips`."""

    def test__creates_vlan_if_necessary(self):
        fabric = factory.make_Fabric()
        vlan = fabric.get_default_vlan()
        iface = factory.make_Interface(vlan=vlan)
        vid = random.randint(1, 4094)
        vlan_before = get_one(VLAN.objects.filter(fabric=fabric, vid=vid))
        self.assertIsNone(vlan_before)
        iface.report_vid(vid)
        vlan_after = get_one(VLAN.objects.filter(fabric=fabric, vid=vid))
        self.assertIsNotNone(vlan_after)
        # Report it one more time to make sure we can handle it if we already
        # observed it. (expect nothing to happen.)
        iface.report_vid(vid)

    def test__logs_vlan_creation_and_sets_description(self):
        fabric = factory.make_Fabric()
        vlan = fabric.get_default_vlan()
        iface = factory.make_Interface(vlan=vlan)
        vid = random.randint(1, 4094)
        with FakeLogger("maas.interface") as maaslog:
            iface.report_vid(vid)
        self.assertDocTestMatches(
            "...: Automatically created VLAN %d..." % vid, maaslog.output
        )
        new_vlan = get_one(VLAN.objects.filter(fabric=fabric, vid=vid))
        self.assertDocTestMatches(
            "Automatically created VLAN (observed by %s)."
            % (iface.get_log_string()),
            new_vlan.description,
        )


class TestInterfaceGetDefaultBridgeName(MAASServerTestCase):

    # Normally we would use scenarios for this, but this was copied and
    # pasted from Juju code in bridgepolicy_test.go.
    expected_bridge_names = {
        "eno0": "br-eno0",
        "twelvechars0": "br-twelvechars0",
        "thirteenchars": "b-thirteenchars",
        "enfourteenchar": "b-fourteenchar",
        "enfifteenchars0": "b-fifteenchars0",
        "fourteenchars1": "b-5590a4-chars1",
        "fifteenchars.12": "b-7e0acf-ars.12",
        "zeros0526193032": "b-000000-193032",
        "enx00e07cc81e1d": "b-x00e07cc81e1d",
    }

    def test__returns_expected_bridge_names_consistent_with_juju(self):
        interface = factory.make_Interface()
        for ifname, expected_bridge_name in self.expected_bridge_names.items():
            interface.name = ifname
            self.assertThat(
                interface.get_default_bridge_name(),
                Equals(expected_bridge_name),
            )
