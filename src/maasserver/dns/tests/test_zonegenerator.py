# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `ZoneGenerator` and supporting cast."""

__all__ = []


import random
import socket
from unittest.mock import ANY, call, Mock
from urllib.parse import urlparse

from maasserver import server_address
from maasserver.dns import zonegenerator
from maasserver.dns.zonegenerator import (
    get_dns_search_paths,
    get_dns_server_address,
    get_hostname_dnsdata_mapping,
    get_hostname_ip_mapping,
    InternalDomain,
    InternalDomainResourse,
    InternalDomainResourseRecord,
    lazydict,
    warn_loopback,
    WARNING_MESSAGE,
    ZoneGenerator,
)
from maasserver.enum import IPADDRESS_TYPE, NODE_STATUS, RDNS_MODE
from maasserver.exceptions import UnresolvableHost
from maasserver.models import Config, Domain, Subnet
from maasserver.models.dnsdata import HostnameRRsetMapping
from maasserver.models.staticipaddress import HostnameIPMapping
from maasserver.testing.config import RegionConfigurationFixture
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.orm import transactional
from maastesting.factory import factory as maastesting_factory
from maastesting.fakemethod import FakeMethod
from maastesting.matchers import MockAnyCall, MockCalledOnceWith, MockNotCalled
from netaddr import IPAddress, IPNetwork
from provisioningserver.dns.zoneconfig import (
    DNSForwardZoneConfig,
    DNSReverseZoneConfig,
)
from testtools import TestCase
from testtools.matchers import (
    Equals,
    IsInstance,
    MatchesAll,
    MatchesDict,
    MatchesSetwise,
    MatchesStructure,
)


class TestGetDNSServerAddress(MAASServerTestCase):
    def test_get_dns_server_address_resolves_hostname(self):
        url = maastesting_factory.make_simple_http_url()
        self.useFixture(RegionConfigurationFixture(maas_url=url))
        ip = factory.make_ipv4_address()
        resolver = self.patch(server_address, "resolve_hostname")
        resolver.return_value = {IPAddress(ip)}

        hostname = urlparse(url).hostname
        result = get_dns_server_address()
        self.assertEqual(ip, result)
        self.expectThat(resolver, MockAnyCall(hostname, 0))

    def test_get_dns_server_address_passes_on_IPv4_IPv6_selection(self):
        ipv4 = factory.pick_bool()
        ipv6 = factory.pick_bool()
        patch = self.patch(zonegenerator, "get_maas_facing_server_addresses")
        patch.return_value = [IPAddress(factory.make_ipv4_address())]

        get_dns_server_address(ipv4=ipv4, ipv6=ipv6)

        self.assertThat(
            patch,
            MockCalledOnceWith(
                ANY,
                include_alternates=False,
                ipv4=ipv4,
                ipv6=ipv6,
                default_region_ip=None,
            ),
        )

    def test_get_dns_server_address_raises_if_hostname_doesnt_resolve(self):
        url = maastesting_factory.make_simple_http_url()
        self.useFixture(RegionConfigurationFixture(maas_url=url))
        self.patch(
            zonegenerator,
            "get_maas_facing_server_addresses",
            FakeMethod(failure=socket.error),
        )
        self.assertRaises(UnresolvableHost, get_dns_server_address)

    def test_get_dns_server_address_logs_warning_if_ip_is_localhost(self):
        logger = self.patch(zonegenerator, "logger")
        self.patch(
            zonegenerator,
            "get_maas_facing_server_addresses",
            Mock(return_value=[IPAddress("127.0.0.1")]),
        )
        get_dns_server_address()
        self.assertEqual(
            call(WARNING_MESSAGE % "127.0.0.1"), logger.warning.call_args
        )

    def test_get_dns_server_address_uses_rack_controller_url(self):
        ip = factory.make_ipv4_address()
        resolver = self.patch(server_address, "resolve_hostname")
        resolver.return_value = {IPAddress(ip)}
        hostname = factory.make_hostname()
        maas_url = "http://%s" % hostname
        rack_controller = factory.make_RackController(url=maas_url)
        result = get_dns_server_address(rack_controller)
        self.expectThat(ip, Equals(result))
        self.expectThat(resolver, MockAnyCall(hostname, 0))


class TestGetDNSSearchPaths(MAASServerTestCase):
    def test__returns_all_authoritative_domains(self):
        domain_names = get_dns_search_paths()
        domain_names.update(
            factory.make_Domain(authoritative=True).name for _ in range(3)
        )
        for _ in range(3):
            factory.make_Domain(authoritative=False)
        self.assertItemsEqual(domain_names, get_dns_search_paths())


class TestWarnLoopback(MAASServerTestCase):
    def test_warn_loopback_warns_about_IPv4_loopback(self):
        logger = self.patch(zonegenerator, "logger")
        loopback = "127.0.0.1"
        warn_loopback(loopback)
        self.assertThat(
            logger.warning, MockCalledOnceWith(WARNING_MESSAGE % loopback)
        )

    def test_warn_loopback_warns_about_any_IPv4_loopback(self):
        logger = self.patch(zonegenerator, "logger")
        loopback = "127.254.100.99"
        warn_loopback(loopback)
        self.assertThat(logger.warning, MockCalledOnceWith(ANY))

    def test_warn_loopback_warns_about_IPv6_loopback(self):
        logger = self.patch(zonegenerator, "logger")
        loopback = "::1"
        warn_loopback(loopback)
        self.assertThat(logger.warning, MockCalledOnceWith(ANY))

    def test_warn_loopback_does_not_warn_about_sensible_IPv4(self):
        logger = self.patch(zonegenerator, "logger")
        warn_loopback("10.1.2.3")
        self.assertThat(logger.warning, MockNotCalled())

    def test_warn_loopback_does_not_warn_about_sensible_IPv6(self):
        logger = self.patch(zonegenerator, "logger")
        warn_loopback("1::9")
        self.assertThat(logger.warning, MockNotCalled())


class TestLazyDict(TestCase):
    """Tests for `lazydict`."""

    def test_empty_initially(self):
        self.assertEqual({}, lazydict(Mock()))

    def test_populates_on_demand(self):
        value = factory.make_name("value")
        value_dict = lazydict(lambda key: value)
        key = factory.make_name("key")
        retrieved_value = value_dict[key]
        self.assertEqual(value, retrieved_value)
        self.assertEqual({key: value}, value_dict)

    def test_remembers_elements(self):
        value_dict = lazydict(lambda key: factory.make_name("value"))
        key = factory.make_name("key")
        self.assertEqual(value_dict[key], value_dict[key])

    def test_holds_one_value_per_key(self):
        value_dict = lazydict(lambda key: key)
        key1 = factory.make_name("key")
        key2 = factory.make_name("key")

        value1 = value_dict[key1]
        value2 = value_dict[key2]

        self.assertEqual((key1, key2), (value1, value2))
        self.assertEqual({key1: key1, key2: key2}, value_dict)


class TestGetHostnameMapping(MAASServerTestCase):
    """Test for `get_hostname_ip_mapping`."""

    def test_get_hostname_ip_mapping_containts_both_static_and_dynamic(self):
        node1 = factory.make_Node(interface=True)
        node1_interface = node1.get_boot_interface()
        subnet = factory.make_Subnet()
        static_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.AUTO,
            ip=factory.pick_ip_in_Subnet(subnet),
            subnet=subnet,
            interface=node1_interface,
        )
        node2 = factory.make_Node(interface=True)
        node2_interface = node2.get_boot_interface()
        subnet = factory.make_ipv4_Subnet_with_IPRanges()
        dynamic_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.DISCOVERED,
            ip=factory.pick_ip_in_IPRange(subnet.get_dynamic_ranges()[0]),
            subnet=subnet,
            interface=node2_interface,
        )
        ttl = random.randint(10, 300)
        Config.objects.set_config("default_dns_ttl", ttl)
        expected_mapping = {
            "%s.maas"
            % node1.hostname: HostnameIPMapping(
                node1.system_id, ttl, {static_ip.ip}, node1.node_type
            ),
            "%s.maas"
            % node2.hostname: HostnameIPMapping(
                node2.system_id, ttl, {dynamic_ip.ip}, node2.node_type
            ),
        }
        actual = get_hostname_ip_mapping(Domain.objects.get_default_domain())
        self.assertItemsEqual(expected_mapping.items(), actual.items())

    def test_get_hostname_dnsdata_mapping_contains_node_and_non_node(self):
        node = factory.make_Node(interface=True)
        dnsdata1 = factory.make_DNSData(
            name=node.hostname, domain=node.domain, rrtype="MX"
        )
        dnsdata2 = factory.make_DNSData(domain=node.domain)
        ttl = random.randint(10, 300)
        Config.objects.set_config("default_dns_ttl", ttl)
        expected_mapping = {
            dnsdata1.dnsresource.name: HostnameRRsetMapping(
                node.system_id,
                {(ttl, dnsdata1.rrtype, dnsdata1.rrdata)},
                node.node_type,
            ),
            dnsdata2.dnsresource.name: HostnameRRsetMapping(
                None, {(ttl, dnsdata2.rrtype, dnsdata2.rrdata)}, None
            ),
        }
        actual = get_hostname_dnsdata_mapping(node.domain)
        self.assertItemsEqual(expected_mapping.items(), actual.items())


def forward_zone(domain):
    """Create a matcher for a :class:`DNSForwardZoneConfig`.

    Returns a matcher which asserts that the test value is a
    `DNSForwardZoneConfig` with the given domain.
    """
    return MatchesAll(
        IsInstance(DNSForwardZoneConfig),
        MatchesStructure.byEquality(domain=domain),
    )


def reverse_zone(domain, network):
    """Create a matcher for a :class:`DNSReverseZoneConfig`.

    Returns a matcher which asserts that the test value is a
    :class:`DNSReverseZoneConfig` with the given domain and network.
    """
    network = network if network is None else IPNetwork(network)
    return MatchesAll(
        IsInstance(DNSReverseZoneConfig),
        MatchesStructure.byEquality(domain=domain, _network=network),
    )


class TestZoneGenerator(MAASServerTestCase):
    """Tests for :class:`ZoneGenerator`."""

    def setUp(self):
        super(TestZoneGenerator, self).setUp()
        self.useFixture(RegionConfigurationFixture())

    def test_empty_yields_nothing(self):
        self.assertEqual(
            [],
            ZoneGenerator((), (), serial=random.randint(0, 65535)).as_list(),
        )

    def test_defaults_ttl(self):
        zonegen = ZoneGenerator((), (), serial=random.randint(0, 65535))
        self.assertEqual(
            Config.objects.get_config("default_dns_ttl"), zonegen.default_ttl
        )
        self.assertEqual([], zonegen.as_list())

    def test_accepts_default_ttl(self):
        default_ttl = random.randint(10, 1000)
        zonegen = ZoneGenerator(
            (), (), default_ttl=default_ttl, serial=random.randint(0, 65535)
        )
        self.assertEqual(default_ttl, zonegen.default_ttl)

    def test_yields_forward_and_reverse_zone(self):
        default_domain = Domain.objects.get_default_domain().name
        domain = factory.make_Domain(name="henry")
        subnet = factory.make_Subnet(cidr=str(IPNetwork("10/29").cidr))
        zones = ZoneGenerator(
            domain, subnet, serial=random.randint(0, 65535)
        ).as_list()
        self.assertThat(
            zones,
            MatchesSetwise(
                forward_zone("henry"),
                reverse_zone(default_domain, "10/29"),
                reverse_zone(default_domain, "10/24"),
            ),
        )

    def test_with_node_yields_fwd_and_rev_zone(self):
        default_domain = Domain.objects.get_default_domain().name
        domain = factory.make_Domain(name="henry")
        subnet = factory.make_Subnet(cidr=str(IPNetwork("10/29").cidr))
        factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet, vlan=subnet.vlan, fabric=subnet.vlan.fabric
        )
        zones = ZoneGenerator(
            domain, subnet, serial=random.randint(0, 65535)
        ).as_list()
        self.assertThat(
            zones,
            MatchesSetwise(
                forward_zone("henry"),
                reverse_zone(default_domain, "10/29"),
                reverse_zone(default_domain, "10/24"),
            ),
        )

    def test_with_child_domain_yields_delegation(self):
        default_domain = Domain.objects.get_default_domain().name
        domain = factory.make_Domain(name="henry")
        factory.make_Domain(name="john.henry")
        subnet = factory.make_Subnet(cidr=str(IPNetwork("10/29").cidr))
        factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet, vlan=subnet.vlan, fabric=subnet.vlan.fabric
        )
        zones = ZoneGenerator(
            domain, subnet, serial=random.randint(0, 65535)
        ).as_list()
        self.assertThat(
            zones,
            MatchesSetwise(
                forward_zone("henry"),
                reverse_zone(default_domain, "10/29"),
                reverse_zone(default_domain, "10/24"),
            ),
        )
        expected_map = {
            "john": HostnameRRsetMapping(None, {(30, "NS", default_domain)})
        }
        self.assertEqual(expected_map, zones[0]._other_mapping)

    def test_with_child_domain_yields_glue_when_needed(self):
        default_domain = Domain.objects.get_default_domain().name
        domain = factory.make_Domain(name="henry")
        john = factory.make_Domain(name="john.henry")
        subnet = factory.make_Subnet(cidr=str(IPNetwork("10/29").cidr))
        sip = factory.make_StaticIPAddress(subnet=subnet)
        factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet, vlan=subnet.vlan, fabric=subnet.vlan.fabric
        )
        factory.make_DNSResource(name="ns", domain=john, ip_addresses=[sip])
        factory.make_DNSData(name="@", domain=john, rrtype="NS", rrdata="ns")
        # We have a subdomain (john.henry) which as an NS RR of
        # 'ns.john.henry', and we should see glue records for it in the parent
        # zone, as well as the A RR in the child.
        zones = ZoneGenerator(
            domain, subnet, serial=random.randint(0, 65535)
        ).as_list()
        self.assertThat(
            zones,
            MatchesSetwise(
                forward_zone("henry"),
                reverse_zone(default_domain, "10/29"),
                reverse_zone(default_domain, "10/24"),
            ),
        )
        expected_map = {
            "john": HostnameRRsetMapping(
                None, {(30, "NS", default_domain), (30, "NS", "ns")}
            ),
            "ns": HostnameRRsetMapping(None, {(30, "A", sip.ip)}),
        }
        self.assertEqual(expected_map, zones[0]._other_mapping)

    def test_parent_of_default_domain_gets_glue(self):
        default_domain = Domain.objects.get_default_domain()
        default_domain.name = "maas.example.com"
        default_domain.save()
        domains = [default_domain, factory.make_Domain("example.com")]
        self.patch(zonegenerator, "get_dns_server_addresses").return_value = [
            IPAddress("5.5.5.5")
        ]
        subnet = factory.make_Subnet(cidr=str(IPNetwork("10/29").cidr))
        factory.make_StaticIPAddress(subnet=subnet)
        factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet, vlan=subnet.vlan, fabric=subnet.vlan.fabric
        )
        zones = ZoneGenerator(
            domains, subnet, serial=random.randint(0, 65535)
        ).as_list()
        self.assertThat(
            zones,
            MatchesSetwise(
                forward_zone(domains[0].name),
                forward_zone(domains[1].name),
                reverse_zone(domains[0].name, "10/29"),
                reverse_zone(domains[0].name, "10/24"),
            ),
        )
        # maas.example.com is the default zone, and has an A RR for its NS RR.
        # example.com has NS maas.example.com., and a glue record for that.
        expected_map_0 = {
            "@": HostnameRRsetMapping(None, {(30, "A", "5.5.5.5")}, None)
        }
        expected_map_1 = {
            "maas": HostnameRRsetMapping(
                None,
                {
                    (30, "A", IPAddress("5.5.5.5")),
                    (30, "NS", "maas.example.com"),
                },
                None,
            )
        }
        self.assertEqual(expected_map_0, zones[0]._other_mapping)
        self.assertEqual(expected_map_1, zones[1]._other_mapping)

    def test_returns_interface_ips_but_no_nulls(self):
        default_domain = Domain.objects.get_default_domain().name
        domain = factory.make_Domain(name="henry")
        subnet = factory.make_Subnet(cidr=str(IPNetwork("10/29").cidr))
        subnet.gateway_ip = str(IPAddress(IPNetwork(subnet.cidr).ip + 1))
        subnet.save()
        # Create a node with two interfaces, with NULL ips
        node = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet,
            vlan=subnet.vlan,
            fabric=subnet.vlan.fabric,
            domain=domain,
            interface_count=3,
        )
        dnsdata = factory.make_DNSData(domain=domain)
        boot_iface = node.boot_interface
        interfaces = list(node.interface_set.all().exclude(id=boot_iface.id))
        # Now go add IP addresses to the boot interface, and one other
        boot_ip = factory.make_StaticIPAddress(
            interface=boot_iface, subnet=subnet
        )
        sip = factory.make_StaticIPAddress(
            interface=interfaces[0], subnet=subnet
        )
        default_ttl = random.randint(10, 300)
        Config.objects.set_config("default_dns_ttl", default_ttl)
        zones = ZoneGenerator(
            domain,
            subnet,
            default_ttl=default_ttl,
            serial=random.randint(0, 65535),
        ).as_list()
        self.assertThat(
            zones,
            MatchesSetwise(
                forward_zone("henry"),
                reverse_zone(default_domain, "10/29"),
                reverse_zone(default_domain, "10/24"),
            ),
        )
        self.assertEqual(
            {
                node.hostname: HostnameIPMapping(
                    node.system_id,
                    default_ttl,
                    {"%s" % boot_ip.ip},
                    node.node_type,
                ),
                "%s.%s"
                % (interfaces[0].name, node.hostname): HostnameIPMapping(
                    node.system_id,
                    default_ttl,
                    {"%s" % sip.ip},
                    node.node_type,
                ),
            },
            zones[0]._mapping,
        )
        self.assertEqual(
            {
                dnsdata.dnsresource.name: HostnameRRsetMapping(
                    None, {(default_ttl, dnsdata.rrtype, dnsdata.rrdata)}
                )
            }.items(),
            zones[0]._other_mapping.items(),
        )
        self.assertEqual(
            {
                node.fqdn: HostnameIPMapping(
                    node.system_id,
                    default_ttl,
                    {"%s" % boot_ip.ip},
                    node.node_type,
                ),
                "%s.%s"
                % (interfaces[0].name, node.fqdn): HostnameIPMapping(
                    node.system_id,
                    default_ttl,
                    {"%s" % sip.ip},
                    node.node_type,
                ),
            },
            zones[1]._mapping,
        )
        self.assertEqual({}, zones[2]._mapping)

    def rfc2317_network(self, network):
        """Returns the network that rfc2317 glue goes in, if any."""
        net = network
        if net.version == 4 and net.prefixlen > 24:
            net = IPNetwork("%s/24" % net.network)
            net = IPNetwork("%s/24" % net.network)
        if net.version == 6 and net.prefixlen > 124:
            net = IPNetwork("%s/124" % net.network)
            net = IPNetwork("%s/124" % net.network)
        if net != network:
            return net
        return None

    def test_supernet_inherits_rfc2317_net(self):
        domain = Domain.objects.get_default_domain()
        subnet1 = factory.make_Subnet(host_bits=2)
        net = IPNetwork(subnet1.cidr)
        if net.version == 6:
            prefixlen = random.randint(121, 124)
        else:
            prefixlen = random.randint(22, 24)
        parent = IPNetwork("%s/%d" % (net.network, prefixlen))
        parent = IPNetwork("%s/%d" % (parent.network, prefixlen))
        subnet2 = factory.make_Subnet(cidr=parent)
        node = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet1,
            vlan=subnet1.vlan,
            fabric=subnet1.vlan.fabric,
            domain=domain,
        )
        boot_iface = node.boot_interface
        factory.make_StaticIPAddress(interface=boot_iface, subnet=subnet1)
        default_ttl = random.randint(10, 300)
        Config.objects.set_config("default_dns_ttl", default_ttl)
        zones = ZoneGenerator(
            domain,
            [subnet1, subnet2],
            default_ttl=default_ttl,
            serial=random.randint(0, 65535),
        ).as_list()
        self.assertThat(
            zones,
            MatchesSetwise(
                forward_zone(domain.name),
                reverse_zone(domain.name, subnet1.cidr),
                reverse_zone(domain.name, subnet2.cidr),
            ),
        )
        self.assertEqual(set(), zones[1]._rfc2317_ranges)
        self.assertEqual({net}, zones[2]._rfc2317_ranges)

    def test_two_managed_interfaces_yields_one_forward_two_reverse_zones(self):
        default_domain = Domain.objects.get_default_domain().name
        domain = factory.make_Domain()
        subnet1 = factory.make_Subnet()
        subnet2 = factory.make_Subnet()
        expected_zones = [
            forward_zone(domain.name),
            reverse_zone(default_domain, subnet1.cidr),
            reverse_zone(default_domain, subnet2.cidr),
        ]
        subnets = Subnet.objects.all()

        expected_zones = (
            [forward_zone(domain.name)]
            + [
                reverse_zone(default_domain, subnet.get_ipnetwork())
                for subnet in subnets
            ]
            + [
                reverse_zone(
                    default_domain,
                    self.rfc2317_network(subnet.get_ipnetwork()),
                )
                for subnet in subnets
                if self.rfc2317_network(subnet.get_ipnetwork()) is not None
            ]
        )
        self.assertThat(
            ZoneGenerator(
                domain, [subnet1, subnet2], serial=random.randint(0, 65535)
            ).as_list(),
            MatchesSetwise(*expected_zones),
        )

    def test_with_many_yields_many_zones(self):
        # This demonstrates ZoneGenerator in all-singing all-dancing mode.
        default_domain = Domain.objects.get_default_domain()
        domains = [default_domain] + [factory.make_Domain() for _ in range(3)]
        for _ in range(3):
            factory.make_Subnet()
        subnets = Subnet.objects.all()
        expected_zones = set()
        for domain in domains:
            expected_zones.add(forward_zone(domain.name))
        for subnet in subnets:
            expected_zones.add(reverse_zone(default_domain.name, subnet.cidr))
            rfc2317_net = self.rfc2317_network(subnet.get_ipnetwork())
            if rfc2317_net is not None:
                expected_zones.add(
                    reverse_zone(default_domain.name, rfc2317_net.cidr)
                )
        actual_zones = ZoneGenerator(
            domains, subnets, serial=random.randint(0, 65535)
        ).as_list()
        self.assertThat(actual_zones, MatchesSetwise(*expected_zones))

    def test_zone_generator_handles_rdns_mode_equal_enabled(self):
        Domain.objects.get_or_create(name="one")
        subnet = factory.make_Subnet(cidr="10.0.0.0/29")
        subnet.rdns_mode = RDNS_MODE.ENABLED
        subnet.save()
        default_domain = Domain.objects.get_default_domain()
        domains = Domain.objects.filter(name="one")
        subnets = Subnet.objects.all()
        expected_zones = (
            forward_zone("one"),
            reverse_zone(default_domain.name, "10/29"),
        )
        self.assertThat(
            ZoneGenerator(
                domains, subnets, serial=random.randint(0, 65535)
            ).as_list(),
            MatchesSetwise(*expected_zones),
        )

    def test_yields_internal_forward_zones(self):
        default_domain = Domain.objects.get_default_domain()
        subnet = factory.make_Subnet(cidr=str(IPNetwork("10/29").cidr))
        domains = []
        for _ in range(3):
            record = InternalDomainResourseRecord(
                rrtype="A", rrdata=factory.pick_ip_in_Subnet(subnet)
            )
            resource = InternalDomainResourse(
                name=factory.make_name("resource"), records=[record]
            )
            domain = InternalDomain(
                name=factory.make_name("domain"),
                ttl=random.randint(15, 300),
                resources=[resource],
            )
            domains.append(domain)
        zones = ZoneGenerator(
            [],
            [subnet],
            serial=random.randint(0, 65535),
            internal_domains=domains,
        ).as_list()
        self.assertThat(
            zones,
            MatchesSetwise(
                *[
                    MatchesAll(
                        forward_zone(domain.name),
                        MatchesStructure(
                            _other_mapping=MatchesDict(
                                {
                                    domain.resources[0].name: MatchesStructure(
                                        rrset=MatchesSetwise(
                                            Equals(
                                                (
                                                    domain.ttl,
                                                    domain.resources[0]
                                                    .records[0]
                                                    .rrtype,
                                                    domain.resources[0]
                                                    .records[0]
                                                    .rrdata,
                                                )
                                            )
                                        )
                                    )
                                }
                            )
                        ),
                    )
                    for domain in domains
                ]
                + [
                    reverse_zone(default_domain.name, "10/29"),
                    reverse_zone(default_domain.name, "10/24"),
                ]
            ),
        )


class TestZoneGeneratorTTL(MAASTransactionServerTestCase):
    """Tests for TTL in :class:ZoneGenerator`."""

    @transactional
    def test_domain_ttl_overrides_global(self):
        global_ttl = random.randint(100, 199)
        Config.objects.set_config("default_dns_ttl", global_ttl)
        subnet = factory.make_Subnet(cidr="10.0.0.0/23")
        domain = factory.make_Domain(ttl=random.randint(200, 299))
        node = factory.make_Node_with_Interface_on_Subnet(
            status=NODE_STATUS.READY, subnet=subnet, domain=domain
        )
        boot_iface = node.get_boot_interface()
        [boot_ip] = boot_iface.claim_auto_ips()
        expected_forward = {
            node.hostname: HostnameIPMapping(
                node.system_id, domain.ttl, {boot_ip.ip}, node.node_type
            )
        }
        expected_reverse = {
            node.fqdn: HostnameIPMapping(
                node.system_id, domain.ttl, {boot_ip.ip}, node.node_type
            )
        }
        zones = ZoneGenerator(
            domain,
            subnet,
            default_ttl=global_ttl,
            serial=random.randint(0, 65535),
        ).as_list()
        self.assertEqual(expected_forward, zones[0]._mapping)
        self.assertEqual(expected_reverse, zones[1]._mapping)

    @transactional
    def test_node_ttl_overrides_domain(self):
        global_ttl = random.randint(100, 199)
        Config.objects.set_config("default_dns_ttl", global_ttl)
        subnet = factory.make_Subnet(cidr="10.0.0.0/23")
        domain = factory.make_Domain(ttl=random.randint(200, 299))
        node = factory.make_Node_with_Interface_on_Subnet(
            status=NODE_STATUS.READY,
            subnet=subnet,
            domain=domain,
            address_ttl=random.randint(300, 399),
        )
        boot_iface = node.get_boot_interface()
        [boot_ip] = boot_iface.claim_auto_ips()
        expected_forward = {
            node.hostname: HostnameIPMapping(
                node.system_id, node.address_ttl, {boot_ip.ip}, node.node_type
            )
        }
        expected_reverse = {
            node.fqdn: HostnameIPMapping(
                node.system_id, node.address_ttl, {boot_ip.ip}, node.node_type
            )
        }
        zones = ZoneGenerator(
            domain,
            subnet,
            default_ttl=global_ttl,
            serial=random.randint(0, 65535),
        ).as_list()
        self.assertEqual(expected_forward, zones[0]._mapping)
        self.assertEqual(expected_reverse, zones[1]._mapping)

    @transactional
    def test_dnsresource_address_does_not_affect_addresses_when_node_set(self):
        # If a node has the same FQDN as a DNSResource, then we use whatever
        # address_ttl there is on the Node (whether None, or not) rather than
        # that on any DNSResource addresses with the same FQDN.
        global_ttl = random.randint(100, 199)
        Config.objects.set_config("default_dns_ttl", global_ttl)
        subnet = factory.make_Subnet(cidr="10.0.0.0/23")
        domain = factory.make_Domain(ttl=random.randint(200, 299))
        node = factory.make_Node_with_Interface_on_Subnet(
            status=NODE_STATUS.READY,
            subnet=subnet,
            domain=domain,
            address_ttl=random.randint(300, 399),
        )
        boot_iface = node.get_boot_interface()
        [boot_ip] = boot_iface.claim_auto_ips()
        dnsrr = factory.make_DNSResource(
            name=node.hostname,
            domain=domain,
            address_ttl=random.randint(400, 499),
        )
        ips = {ip.ip for ip in dnsrr.ip_addresses.all() if ip is not None}
        ips.add(boot_ip.ip)
        expected_forward = {
            node.hostname: HostnameIPMapping(
                node.system_id, node.address_ttl, ips, node.node_type, dnsrr.id
            )
        }
        expected_reverse = {
            node.fqdn: HostnameIPMapping(
                node.system_id, node.address_ttl, ips, node.node_type, dnsrr.id
            )
        }
        zones = ZoneGenerator(
            domain,
            subnet,
            default_ttl=global_ttl,
            serial=random.randint(0, 65535),
        ).as_list()
        self.assertEqual(expected_forward, zones[0]._mapping)
        self.assertEqual(expected_reverse, zones[1]._mapping)

    @transactional
    def test_dnsresource_address_overrides_domain(self):
        # DNSResource.address_ttl _does_, however, override Domain.ttl for
        # addresses that do not have nodes associated with them.
        global_ttl = random.randint(100, 199)
        Config.objects.set_config("default_dns_ttl", global_ttl)
        subnet = factory.make_Subnet(cidr="10.0.0.0/23")
        domain = factory.make_Domain(ttl=random.randint(200, 299))
        node = factory.make_Node_with_Interface_on_Subnet(
            status=NODE_STATUS.READY,
            subnet=subnet,
            domain=domain,
            address_ttl=random.randint(300, 399),
        )
        boot_iface = node.get_boot_interface()
        [boot_ip] = boot_iface.claim_auto_ips()
        dnsrr = factory.make_DNSResource(
            domain=domain, address_ttl=random.randint(400, 499)
        )
        node_ips = {boot_ip.ip}
        dnsrr_ips = {
            ip.ip for ip in dnsrr.ip_addresses.all() if ip is not None
        }
        expected_forward = {
            node.hostname: HostnameIPMapping(
                node.system_id, node.address_ttl, node_ips, node.node_type
            ),
            dnsrr.name: HostnameIPMapping(
                None, dnsrr.address_ttl, dnsrr_ips, None, dnsrr.id
            ),
        }
        expected_reverse = {
            node.fqdn: HostnameIPMapping(
                node.system_id,
                node.address_ttl,
                node_ips,
                node.node_type,
                None,
            ),
            dnsrr.fqdn: HostnameIPMapping(
                None, dnsrr.address_ttl, dnsrr_ips, None, dnsrr.id
            ),
        }
        zones = ZoneGenerator(
            domain,
            subnet,
            default_ttl=global_ttl,
            serial=random.randint(0, 65535),
        ).as_list()
        self.assertEqual(expected_forward, zones[0]._mapping)
        self.assertEqual(expected_reverse, zones[1]._mapping)

    @transactional
    def test_dnsdata_inherits_global(self):
        # If there is no ttl on the DNSData or Domain, then we get the global
        # value.
        global_ttl = random.randint(100, 199)
        Config.objects.set_config("default_dns_ttl", global_ttl)
        subnet = factory.make_Subnet(cidr="10.0.0.0/23")
        domain = factory.make_Domain()
        dnsrr = factory.make_DNSResource(
            no_ip_addresses=True,
            domain=domain,
            address_ttl=random.randint(400, 499),
        )
        dnsdata = factory.make_DNSData(dnsresource=dnsrr)
        expected_forward = {
            dnsrr.name: HostnameRRsetMapping(
                None, {(global_ttl, dnsdata.rrtype, dnsdata.rrdata)}
            )
        }
        zones = ZoneGenerator(
            domain,
            subnet,
            default_ttl=global_ttl,
            serial=random.randint(0, 65535),
        ).as_list()
        self.assertEqual(expected_forward, zones[0]._other_mapping)
        self.assertEqual({}, zones[0]._mapping)
        self.assertEqual({}, zones[1]._mapping)
        self.assertEqual(None, dnsdata.ttl)

    @transactional
    def test_dnsdata_inherits_domain(self):
        # If there is no ttl on the DNSData, but is on Domain, then we get the
        # domain value.
        global_ttl = random.randint(100, 199)
        Config.objects.set_config("default_dns_ttl", global_ttl)
        subnet = factory.make_Subnet(cidr="10.0.0.0/23")
        domain = factory.make_Domain(ttl=random.randint(200, 299))
        dnsrr = factory.make_DNSResource(
            no_ip_addresses=True,
            domain=domain,
            address_ttl=random.randint(400, 499),
        )
        dnsdata = factory.make_DNSData(dnsresource=dnsrr)
        expected_forward = {
            dnsrr.name: HostnameRRsetMapping(
                None, {(domain.ttl, dnsdata.rrtype, dnsdata.rrdata)}
            )
        }
        zones = ZoneGenerator(
            domain,
            subnet,
            default_ttl=global_ttl,
            serial=random.randint(0, 65535),
        ).as_list()
        self.assertEqual(expected_forward, zones[0]._other_mapping)
        self.assertEqual({}, zones[0]._mapping)
        self.assertEqual({}, zones[1]._mapping)
        self.assertEqual(None, dnsdata.ttl)

    @transactional
    def test_dnsdata_overrides_domain(self):
        # If DNSData has a ttl, we use that in preference to anything else.
        global_ttl = random.randint(100, 199)
        Config.objects.set_config("default_dns_ttl", global_ttl)
        subnet = factory.make_Subnet(cidr="10.0.0.0/23")
        domain = factory.make_Domain(ttl=random.randint(200, 299))
        dnsrr = factory.make_DNSResource(
            no_ip_addresses=True,
            domain=domain,
            address_ttl=random.randint(400, 499),
        )
        dnsdata = factory.make_DNSData(
            dnsresource=dnsrr, ttl=random.randint(500, 599)
        )
        expected_forward = {
            dnsrr.name: HostnameRRsetMapping(
                None, {(dnsdata.ttl, dnsdata.rrtype, dnsdata.rrdata)}
            )
        }
        zones = ZoneGenerator(
            domain,
            subnet,
            default_ttl=global_ttl,
            serial=random.randint(0, 65535),
        ).as_list()
        self.assertEqual(expected_forward, zones[0]._other_mapping)
        self.assertEqual({}, zones[0]._mapping)
        self.assertEqual({}, zones[1]._mapping)

    @transactional
    def test_domain_ttl_overrides_default_ttl(self):
        # If the domain has a ttl, we use that as the default ttl.
        Config.objects.set_config("default_dns_ttl", 42)
        domain = factory.make_Domain(ttl=84)
        [zone_config] = ZoneGenerator(domains=[domain], subnets=[], serial=123)
        self.assertEqual(domain.name, zone_config.domain)
        self.assertEqual(domain.ttl, zone_config.default_ttl)

    @transactional
    def test_none_domain_ttl_doesnt_override_default_ttl(self):
        # If the domain doesn't hae a ttl, the global default ttl is used.
        Config.objects.set_config("default_dns_ttl", 42)
        domain = factory.make_Domain(ttl=None)
        [zone_config] = ZoneGenerator(domains=[domain], subnets=[], serial=123)
        self.assertEqual(domain.name, zone_config.domain)
        self.assertEqual(42, zone_config.default_ttl)
