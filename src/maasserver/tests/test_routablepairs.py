# Copyright 2016-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maasserver.routablepairs`."""

__all__ = []

from itertools import product, takewhile
import random

from maasserver.models.node import Node
from maasserver.routablepairs import find_addresses_between_nodes
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from testtools import ExpectedException
from testtools.matchers import AfterPreprocessing, Equals


class TestFindAddressesBetweenNodes(MAASServerTestCase):
    """Tests for `maasserver.routablepairs.find_addresses_between_nodes`."""

    def test__yields_nothing_when_no_nodes_given(self):
        self.assertItemsEqual([], find_addresses_between_nodes([], []))

    def test__rejects_unsaved_nodes_on_the_left(self):
        saved_node, unsaved_node = factory.make_Node(), Node()
        with ExpectedException(AssertionError, ".* not in the database"):
            list(find_addresses_between_nodes([unsaved_node], [saved_node]))

    def test__rejects_unsaved_nodes_on_the_right(self):
        saved_node, unsaved_node = factory.make_Node(), Node()
        with ExpectedException(AssertionError, ".* not in the database"):
            list(find_addresses_between_nodes([saved_node], [unsaved_node]))

    def make_node_with_address(self, space, cidr):
        node = factory.make_Node()
        iface = factory.make_Interface(node=node)
        subnet = factory.make_Subnet(space=space, cidr=cidr)
        sip = factory.make_StaticIPAddress(interface=iface, subnet=subnet)
        return node, sip.get_ipaddress()

    def test__yields_routes_between_nodes_on_same_space(self):
        space = factory.make_Space()
        network1 = factory.make_ip4_or_6_network()
        network2 = factory.make_ip4_or_6_network(version=network1.version)
        node1, ip1 = self.make_node_with_address(space, network1)
        node2, ip2 = self.make_node_with_address(space, network2)

        left = node1, ip1
        right = node2, ip2
        expected = [left + right]

        # A route from node1 to node2 is found.
        self.assertItemsEqual(
            expected, find_addresses_between_nodes([node1], [node2])
        )

    def test__yields_routes_between_multiple_nodes_on_same_space(self):
        space = factory.make_Space()

        lefts, rights = [], []
        for index in range(3):
            network1 = factory.make_ip4_or_6_network()
            network2 = factory.make_ip4_or_6_network(version=network1.version)
            lefts.append(self.make_node_with_address(space, network1))
            rights.append(self.make_node_with_address(space, network2))

        expected = [
            (n1, ip1, n2, ip2)
            for (n1, ip1), (n2, ip2) in product(lefts, rights)
            # Addresses are only routable when they're the same IP version.
            if ip1.version == ip2.version
        ]

        # A route from each node on the left is found to each on the right.
        self.assertItemsEqual(
            expected,
            find_addresses_between_nodes(
                (node for node, _ in lefts), (node for node, _ in rights)
            ),
        )

    def test__does_not_contain_routes_between_nodes_on_differing_spaces(self):
        space1 = factory.make_Space()
        space2 = factory.make_Space()
        network1 = factory.make_ip4_or_6_network()
        network2 = factory.make_ip4_or_6_network(version=network1.version)
        node1, ip1 = self.make_node_with_address(space1, network1)
        node2, ip2 = self.make_node_with_address(space2, network2)

        expected = []

        # No routable addresses are found.
        self.assertItemsEqual(
            expected, find_addresses_between_nodes([node1], [node2])
        )

    def test__does_not_contain_routes_between_addrs_of_diff_network_fams(self):
        space = factory.make_Space()  # One space.
        network1 = factory.make_ip4_or_6_network()
        network2 = factory.make_ip4_or_6_network(
            version=(4 if network1.version == 6 else 6)
        )
        node1, ip1 = self.make_node_with_address(space, network1)
        node2, ip2 = self.make_node_with_address(space, network2)

        expected = []

        # No routable addresses are found.
        self.assertItemsEqual(
            expected, find_addresses_between_nodes([node1], [node2])
        )

    def gen_disjoint_networks(self):
        """Generate disjoint networks.

        Can be IPv4 or IPv6, but once generation has begun they'll all be the
        same family.
        """
        make_network = random.choice(
            [factory.make_ipv4_network, factory.make_ipv6_network]
        )
        networks = []
        while True:
            network = make_network(disjoint_from=networks)
            networks.append(network)
            yield network

    def test__yields_routes_with_lowest_metrics_first(self):
        space = factory.make_Space()
        # Ensure networks are disjoint but of the same family.
        networks = self.gen_disjoint_networks()

        # Create the node for the "left" that has two IP addresses, one in the
        # null space, one in a non-null space.
        origin = factory.make_Node(hostname="origin")
        origin_iface = factory.make_Interface(
            node=origin, link_connected=False
        )
        origin_subnet = factory.make_Subnet(space=space, cidr=next(networks))
        origin_subnet_null_space = factory.make_Subnet(
            space=None, cidr=next(networks)
        )
        origin_sip = factory.make_StaticIPAddress(
            interface=origin_iface, subnet=origin_subnet
        )
        origin_sip_null_space = factory.make_StaticIPAddress(
            interface=origin_iface, subnet=origin_subnet_null_space
        )

        # Same subnet, different node.
        node_same_subnet = factory.make_Node(hostname="same-subnet")
        sip_same_subnet = factory.make_StaticIPAddress(
            interface=factory.make_Interface(
                node=node_same_subnet, link_connected=False
            ),
            subnet=origin_subnet,
        )

        # Same VLAN, different subnet, different node.
        node_same_vlan = factory.make_Node(hostname="same-vlan")
        sip_same_vlan = factory.make_StaticIPAddress(
            interface=factory.make_Interface(
                node=node_same_vlan, link_connected=False
            ),
            subnet=factory.make_Subnet(
                space=space, vlan=origin_subnet.vlan, cidr=next(networks)
            ),
        )

        # Same space, different VLAN, subnet, and node.
        node_same_space = factory.make_Node(hostname="same-space")
        sip_same_space = factory.make_StaticIPAddress(
            interface=factory.make_Interface(
                node=node_same_space, link_connected=False
            ),
            subnet=factory.make_Subnet(space=space, cidr=next(networks)),
        )

        # Null space, different VLAN, subnet, and node. (won't be included)
        node_null_space = factory.make_Node(hostname="null-space")
        factory.make_StaticIPAddress(
            interface=factory.make_Interface(
                node=node_null_space, link_connected=False
            ),
            subnet=factory.make_Subnet(space=None, cidr=next(networks)),
        )

        # We'll search for routes between `lefts` and `rights`.
        lefts = [origin]
        rights = [
            node_same_subnet,
            node_same_vlan,
            node_same_space,
            node_null_space,  # Should not be included.
        ]

        # This is in order, lowest "metric" first.
        expected = [
            (
                origin,
                origin_sip.get_ipaddress(),
                node_same_subnet,
                sip_same_subnet.get_ipaddress(),
            ),
            (
                origin,
                origin_sip.get_ipaddress(),
                node_same_vlan,
                sip_same_vlan.get_ipaddress(),
            ),
            (
                origin,
                origin_sip.get_ipaddress(),
                node_same_space,
                sip_same_space.get_ipaddress(),
            ),
        ]
        self.assertThat(
            find_addresses_between_nodes(lefts, rights),
            AfterPreprocessing(list, Equals(expected)),
        )

        # Same node, same space, different VLAN and subnet. We did not add
        # this earlier because its existence allows for a large number of
        # additional routes between the origin and the other nodes, which
        # would have obscured the test.
        origin_sip_2 = factory.make_StaticIPAddress(
            interface=factory.make_Interface(
                node=origin, link_connected=False
            ),
            subnet=factory.make_Subnet(space=space, cidr=next(networks)),
        )

        # Now the first addresses returned are between those addresses we
        # created on the same node, in no particular order.
        origin_ips = origin_sip.get_ipaddress(), origin_sip_2.get_ipaddress()
        expected_mutual = {
            (origin, ip1, origin, ip2)
            for ip1, ip2 in product(origin_ips, origin_ips)
        }
        # There's a mutual route for the null-space IP address too.
        expected_mutual.add(
            (
                origin,
                origin_sip_null_space.get_ipaddress(),
                origin,
                origin_sip_null_space.get_ipaddress(),
            )
        )
        observed_mutual = takewhile(
            (lambda route: route[0] == route[2]),  # Route is mutual.
            find_addresses_between_nodes(lefts, [origin, *rights]),
        )
        self.assertItemsEqual(expected_mutual, observed_mutual)

    def test__doesnt_include_matches_between_undefined_spaces(self):
        network1 = next(self.gen_disjoint_networks())
        next(self.gen_disjoint_networks())
        network2 = next(self.gen_disjoint_networks())

        # Create the node for the "left" that has two IP addresses, one in the
        # null space, one in a non-null space.
        origin = factory.make_Node(hostname="origin")
        origin_iface = factory.make_Interface(
            node=origin, link_connected=False
        )
        origin_subnet_null_space = factory.make_Subnet(
            space=None, cidr=network1
        )
        factory.make_StaticIPAddress(
            interface=origin_iface, subnet=origin_subnet_null_space
        )

        # Same subnet, different node.
        node_no_match = factory.make_Node(hostname="no-match")
        no_match_iface = factory.make_Interface(
            node=node_no_match, link_connected=False
        )
        no_match_subnet_null_space = factory.make_Subnet(
            space=None, cidr=network2
        )
        factory.make_StaticIPAddress(
            interface=no_match_iface, subnet=no_match_subnet_null_space
        )

        no_matches = list(
            find_addresses_between_nodes({origin}, {node_no_match})
        )
        self.assertEqual([], no_matches)
