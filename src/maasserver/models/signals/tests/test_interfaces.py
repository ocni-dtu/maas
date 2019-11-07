# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test the behaviour of interface signals."""

__all__ = []

import random
import threading
from unittest.mock import call

from maasserver.enum import INTERFACE_TYPE, IPADDRESS_TYPE, NODE_TYPE
from maasserver.models import Controller, Interface
from maasserver.models.config import Config, NetworkDiscoveryConfig
from maasserver.models.signals.interfaces import (
    ensure_link_up,
    update_parents_thread_local,
)
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.orm import reload_object
from maastesting.matchers import MockCallsMatch
from testtools.matchers import Contains, Not


def _mock_ensure_link_up(self):
    """Mock method to test the 'visited' pattern for recursion prevention."""
    ensure_link_up(self)


class TestEnableAndDisableInterface(MAASServerTestCase):
    def test__enable_interface_creates_link_up(self):
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, enabled=False
        )
        interface.enabled = True
        interface.save()
        link_ip = interface.ip_addresses.get(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=None
        )
        self.assertIsNotNone(link_ip)

    def test__enable_interface_creates_link_up_on_children(self):
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, enabled=False
        )
        vlan_interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, parents=[interface]
        )
        interface.enabled = True
        interface.save()
        link_ip = vlan_interface.ip_addresses.get(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=None
        )
        self.assertIsNotNone(link_ip)

    def test__ensure_link_up_only_called_once_per_interface(self):
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, enabled=False
        )
        interface.enabled = True
        self.patch(Interface, "ensure_link_up", _mock_ensure_link_up)
        # This will cause a RecursionError if the code doesn't work.
        interface.save()

    def test__disable_interface_removes_links(self):
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, enabled=True
        )
        interface.ensure_link_up()
        interface.enabled = False
        interface.save()
        self.assertItemsEqual([], interface.ip_addresses.all())

    def test__disable_interface_removes_links_on_children(self):
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, enabled=True
        )
        vlan_interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, parents=[interface]
        )
        vlan_interface.ensure_link_up()
        interface.enabled = False
        interface.save()
        self.assertItemsEqual([], vlan_interface.ip_addresses.all())

    def test__disable_interface_doesnt_remove_links_on_enabled_children(self):
        node = factory.make_Node()
        nic0 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, enabled=True
        )
        nic1 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, enabled=True
        )
        bond_interface = factory.make_Interface(
            INTERFACE_TYPE.BOND, parents=[nic0, nic1]
        )
        bond_interface.ensure_link_up()
        nic0.enabled = False
        nic0.save()
        self.assertEqual(1, bond_interface.ip_addresses.count())


class TestMTUParams(MAASServerTestCase):
    def test__updates_children_mtu(self):
        new_mtu = random.randint(800, 2000)
        physical_interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        vlan1_interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, parents=[physical_interface]
        )
        vlan_mtu = random.randint(new_mtu + 1, new_mtu * 2)
        vlan1_interface.params = {"mtu": vlan_mtu}
        vlan1_interface.save()
        vlan2_interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, parents=[physical_interface]
        )
        physical_interface.params = {"mtu": new_mtu}
        physical_interface.save()
        self.assertEqual(
            {"mtu": new_mtu}, reload_object(vlan1_interface).params
        )
        self.assertEqual("", reload_object(vlan2_interface).params)

    def test__updates_parents_mtu(self):
        node = factory.make_Node()
        physical1_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node
        )
        physical2_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node
        )
        physical3_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node
        )
        bond_interface = factory.make_Interface(
            INTERFACE_TYPE.BOND,
            parents=[
                physical1_interface,
                physical2_interface,
                physical3_interface,
            ],
        )
        # Smaller MTU will be set to larger MTU.
        physical1_mtu = random.randint(800, 1999)
        physical1_interface.params = {"mtu": physical1_mtu}
        physical1_interface.save()
        # Larger MTU will be left alone.
        physical2_mtu = random.randint(4000, 8000)
        physical2_interface.params = {"mtu": physical2_mtu}
        physical2_interface.save()
        # In between the smaller and larger MTU.
        bond_mtu = random.randint(2000, 3999)
        bond_interface.params = {"mtu": bond_mtu}
        bond_interface.save()
        self.assertEqual(
            {"mtu": bond_mtu}, reload_object(physical1_interface).params
        )
        self.assertEqual(
            {"mtu": physical2_mtu}, reload_object(physical2_interface).params
        )
        # Physical 3 should be set the the bond interface MTU.
        self.assertEqual(
            {"mtu": bond_mtu}, reload_object(physical3_interface).params
        )


class TestUpdateChildInterfaceParents(MAASServerTestCase):

    scenarios = (
        ("bond", {"iftype": INTERFACE_TYPE.BOND}),
        ("bridge", {"iftype": INTERFACE_TYPE.BRIDGE}),
    )

    def test__updates_interface_parents(self):
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        parent2 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=parent1.node
        )
        child = factory.make_Interface(self.iftype, parents=[parent1, parent2])
        self.assertEqual(child.vlan, reload_object(parent1).vlan)
        self.assertEqual(child.vlan, reload_object(parent2).vlan)

    def test__update_interface_clears_parent_links(self):
        parent1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL)
        parent2 = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=parent1.node
        )
        static_ip = factory.make_StaticIPAddress(interface=parent1)
        factory.make_Interface(self.iftype, parents=[parent1, parent2])
        self.assertIsNone(reload_object(static_ip))

    def test__visited_set_is_thread_local(self):
        thread_local = update_parents_thread_local
        thread_local.visiting.add("a")

        def check_a_does_not_exist():
            self.assertThat(thread_local.visiting, Not(Contains("a")))

        thread = threading.Thread(target=check_a_does_not_exist)
        thread.start()
        thread.join()
        self.assertThat(thread_local.visiting, Contains("a"))
        thread_local.visiting.discard("a")


class TestInterfaceVLANUpdateNotController(MAASServerTestCase):

    scenarios = (
        ("machine", {"maker": factory.make_Node}),
        ("device", {"maker": factory.make_Device}),
    )

    def test__removes_links(self):
        node = self.maker()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        static_ip = factory.make_StaticIPAddress(interface=interface)
        discovered_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, interface=interface
        )
        new_fabric = factory.make_Fabric()
        new_vlan = new_fabric.get_default_vlan()
        interface.vlan = new_vlan
        interface.save()
        self.assertIsNone(reload_object(static_ip))
        self.assertIsNotNone(reload_object(discovered_ip))

    def test__removes_links_when_goes_to_disconnected(self):
        node = self.maker()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        static_ip = factory.make_StaticIPAddress(interface=interface)
        discovered_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED, interface=interface
        )
        interface.vlan = None
        interface.save()
        self.assertIsNone(reload_object(static_ip))
        self.assertIsNotNone(reload_object(discovered_ip))


class TestInterfaceVLANUpdateController(MAASServerTestCase):

    scenarios = (
        ("region", {"maker": factory.make_RegionController}),
        ("rack", {"maker": factory.make_RackController}),
        (
            "region-rack",
            {
                "maker": lambda: factory.make_Node(
                    node_type=NODE_TYPE.REGION_AND_RACK_CONTROLLER
                )
            },
        ),
    )

    def test__moves_link_subnets_to_same_vlan(self):
        node = self.maker()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        subnet = factory.make_Subnet(vlan=interface.vlan)
        factory.make_StaticIPAddress(subnet=subnet, interface=interface)
        new_fabric = factory.make_Fabric()
        new_vlan = new_fabric.get_default_vlan()
        interface.vlan = new_vlan
        interface.save()
        self.assertEquals(new_vlan, reload_object(subnet).vlan)

    def test__doesnt_move_link_subnets_when_target_vlan_is_None(self):
        node = self.maker()
        interface = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        old_vlan = interface.vlan
        subnet = factory.make_Subnet(vlan=old_vlan)
        factory.make_StaticIPAddress(subnet=subnet, interface=interface)
        interface.vlan = None
        interface.save()
        self.assertEquals(old_vlan, reload_object(subnet).vlan)

    def test__doesnt_move_link_subnets_when_source_vlan_is_None(self):
        node = self.maker()
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=node, vlan=None
        )
        old_vlan = interface.vlan
        subnet = factory.make_Subnet(vlan=old_vlan)
        factory.make_StaticIPAddress(subnet=subnet, interface=interface)
        interface.vlan = factory.make_VLAN()
        interface.save()
        self.assertEquals(interface.vlan, reload_object(subnet).vlan)

    def test__moves_children_vlans_to_same_fabric(self):
        node = self.maker()
        parent = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        subnet = factory.make_Subnet(vlan=parent.vlan)
        factory.make_StaticIPAddress(subnet=subnet, interface=parent)
        old_vlan = factory.make_VLAN(fabric=parent.vlan.fabric)
        vlan_interface = factory.make_Interface(
            INTERFACE_TYPE.VLAN, vlan=old_vlan, parents=[parent]
        )
        vlan_subnet = factory.make_Subnet(vlan=old_vlan)
        factory.make_StaticIPAddress(
            subnet=vlan_subnet, interface=vlan_interface
        )
        new_fabric = factory.make_Fabric()
        new_vlan = new_fabric.get_default_vlan()
        parent.vlan = new_vlan
        parent.save()
        self.assertEquals(new_vlan, reload_object(subnet).vlan)
        vlan_interface = reload_object(vlan_interface)
        self.assertEquals(
            (new_fabric.id, old_vlan.vid),
            (vlan_interface.vlan.fabric.id, vlan_interface.vlan.vid),
        )
        vlan_subnet = reload_object(vlan_subnet)
        self.assertEquals(
            (new_fabric.id, old_vlan.vid),
            (vlan_subnet.vlan.fabric.id, vlan_subnet.vlan.vid),
        )


class TestDiscoveryConfigChanges(MAASServerTestCase):
    """Test that changes to the network discovery cause interface changes."""

    def test_active_configuration_causes_interface_update(self):
        factory.make_RegionRackController()
        factory.make_RegionController()
        factory.make_RackController()
        mock_update_discovery_state = self.patch(
            Controller, "update_discovery_state"
        )
        Config.objects.set_config("network_discovery", "active")
        self.expectThat(
            mock_update_discovery_state,
            MockCallsMatch(
                *[call(NetworkDiscoveryConfig(active=True, passive=True))] * 3
            ),
        )

    def test_passive_configuration_causes_interface_update(self):
        factory.make_RegionRackController()
        factory.make_RegionController()
        factory.make_RackController()
        mock_update_discovery_state = self.patch(
            Controller, "update_discovery_state"
        )
        Config.objects.set_config("network_discovery", "enabled")
        self.expectThat(
            mock_update_discovery_state,
            MockCallsMatch(
                *[call(NetworkDiscoveryConfig(active=False, passive=True))] * 3
            ),
        )

    def test_disabled_configuration_causes_interface_update(self):
        factory.make_RegionRackController()
        factory.make_RegionController()
        factory.make_RackController()
        mock_update_discovery_state = self.patch(
            Controller, "update_discovery_state"
        )
        Config.objects.set_config("network_discovery", "disabled")
        self.expectThat(
            mock_update_discovery_state,
            MockCallsMatch(
                *[call(NetworkDiscoveryConfig(active=False, passive=False))]
                * 3
            ),
        )


class TestDeleteInterface(MAASServerTestCase):
    def test_removes_links(self):
        ip1 = factory.make_StaticIPAddress()
        ip2 = factory.make_StaticIPAddress()
        node = factory.make_Node()
        interface1 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface2 = factory.make_Interface(INTERFACE_TYPE.PHYSICAL, node=node)
        interface1.ip_addresses.add(ip1)
        interface1.ip_addresses.add(ip2)
        interface1.save()
        interface2.ip_addresses.add(ip2)
        interface2.save()
        interface1.delete()
        self.assertIsNone(reload_object(ip1))
        self.assertIsNotNone(reload_object(ip2))
