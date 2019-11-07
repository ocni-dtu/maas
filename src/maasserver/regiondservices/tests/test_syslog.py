# Copyright 2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maasserver.regiondservices.syslog`."""

__all__ = []

from crochet import wait_for
from maasserver.models.config import Config
from maasserver.regiondservices import syslog
from maasserver.service_monitor import service_monitor
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
from maastesting.fixtures import MAASRootFixture
from maastesting.matchers import (
    DocTestMatches,
    Matches,
    MockCalledOnce,
    MockCalledOnceWith,
)
from maastesting.testcase import MAASTestCase
from maastesting.twisted import TwistedLoggerFixture
from netaddr import IPAddress
from provisioningserver.utils.testing import MAASIDFixture
from testtools.matchers import (
    AllMatch,
    ContainsAll,
    Equals,
    IsInstance,
    MatchesStructure,
)
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks


wait_for_reactor = wait_for(30)  # 30 seconds.


def make_region_rack_with_address(space):
    region = factory.make_RegionRackController()
    iface = factory.make_Interface(node=region)
    cidr4 = factory.make_ipv4_network(24)
    subnet4 = factory.make_Subnet(space=space, cidr=cidr4)
    cidr6 = factory.make_ipv6_network(64)
    subnet6 = factory.make_Subnet(space=space, cidr=cidr6)
    sip4 = factory.make_StaticIPAddress(interface=iface, subnet=subnet4)
    sip6 = factory.make_StaticIPAddress(interface=iface, subnet=subnet6)
    return region, sip4, sip6


class TestRegionSyslogService_Basic(MAASTestCase):
    """Basic tests for `RegionSyslogService`."""

    def test_service_uses__tryUpdate_as_periodic_function(self):
        service = syslog.RegionSyslogService(reactor)
        self.assertThat(service.call, Equals((service._tryUpdate, (), {})))

    def test_service_iterates_every_30_seconds(self):
        service = syslog.RegionSyslogService(reactor)
        self.assertThat(service.step, Equals(30.0))


class TestRegionSyslogService(MAASTransactionServerTestCase):
    """Tests for `RegionSyslogService`."""

    def setUp(self):
        super(TestRegionSyslogService, self).setUp()
        self.useFixture(MAASRootFixture())

    @transactional
    def make_example_configuration(self):
        # Set the syslog port.
        port = factory.pick_port()
        Config.objects.set_config("maas_syslog_port", port)
        # Populate the database with example peers.
        space = factory.make_Space()
        region, addr4, addr6 = make_region_rack_with_address(space)
        self.useFixture(MAASIDFixture(region.system_id))
        peer1, addr1_4, addr1_6 = make_region_rack_with_address(space)
        peer2, addr2_4, addr2_6 = make_region_rack_with_address(space)
        # Return the servers and all possible peer IP addresses.
        return (
            port,
            [
                (
                    peer1,
                    sorted([IPAddress(addr1_4.ip), IPAddress(addr1_6.ip)])[0],
                ),
                (
                    peer2,
                    sorted([IPAddress(addr2_4.ip), IPAddress(addr2_6.ip)])[0],
                ),
            ],
        )

    @wait_for_reactor
    @inlineCallbacks
    def test__tryUpdate_updates_syslog_server(self):
        service = syslog.RegionSyslogService(reactor)
        port, peers = yield deferToDatabase(self.make_example_configuration)
        write_config = self.patch_autospec(syslog, "write_config")
        restartService = self.patch_autospec(service_monitor, "restartService")
        yield service._tryUpdate()
        self.assertThat(
            write_config,
            MockCalledOnceWith(
                True,
                Matches(
                    ContainsAll(
                        [
                            {
                                "ip": service._formatIP(ip),
                                "name": node.hostname,
                            }
                            for node, ip in peers
                        ]
                    )
                ),
                port=port,
            ),
        )
        self.assertThat(restartService, MockCalledOnceWith("syslog_region"))
        # If the configuration has not changed then a second call to
        # `_tryUpdate` does not result in another call to `write_config`.
        yield service._tryUpdate()
        self.assertThat(write_config, MockCalledOnce())
        self.assertThat(restartService, MockCalledOnceWith("syslog_region"))


class TestRegionSyslogService_Errors(MAASTransactionServerTestCase):
    """Tests for error handing in `RegionSyslogService`."""

    scenarios = (
        ("_getConfiguration", dict(method="_getConfiguration")),
        ("_maybeApplyConfiguration", dict(method="_maybeApplyConfiguration")),
        ("_applyConfiguration", dict(method="_applyConfiguration")),
        ("_configurationApplied", dict(method="_configurationApplied")),
    )

    @wait_for_reactor
    @inlineCallbacks
    def test__tryUpdate_logs_errors_from_broken_method(self):
        service = syslog.RegionSyslogService(reactor)
        broken_method = self.patch_autospec(service, self.method)
        broken_method.side_effect = factory.make_exception()

        # Don't actually write the file.
        self.patch_autospec(syslog, "write_config")

        # Ensure that we never actually execute against systemd.
        self.patch_autospec(service_monitor, "restartService")

        with TwistedLoggerFixture() as logger:
            yield service._tryUpdate()

        self.assertThat(
            logger.output,
            DocTestMatches(
                """
                Failed to update syslog configuration.
                Traceback (most recent call last):
                ...
                maastesting.factory.TestException#...
                """
            ),
        )


class TestRegionSyslogService_Database(MAASServerTestCase):
    """Database tests for `RegionSyslogService`."""

    def test__getConfiguration_returns_configuration_object(self):
        service = syslog.RegionSyslogService(reactor)

        # Put all addresses in the same space so they're mutually routable.
        space = factory.make_Space()
        # Populate the database with "this" region rack and an example peer.
        region_rack, _, _ = make_region_rack_with_address(space)
        self.useFixture(MAASIDFixture(region_rack.system_id))
        peer, addr4, addr6 = make_region_rack_with_address(space)

        observed = service._getConfiguration()
        self.assertThat(observed, IsInstance(syslog._Configuration))

        expected_peers = AllMatch(Equals((peer.hostname, IPAddress(addr4.ip))))

        self.assertThat(observed, MatchesStructure(peers=expected_peers))
