# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maasserver.regiondservices.ntp`."""

__all__ = []

from crochet import wait_for
from maasserver.models.config import Config
from maasserver.regiondservices import ntp
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
    ContainedBy,
    DocTestMatches,
    Matches,
    MockCalledOnce,
    MockCalledOnceWith,
)
from maastesting.testcase import MAASTestCase
from maastesting.twisted import always_succeed_with, TwistedLoggerFixture
from provisioningserver.utils.testing import MAASIDFixture
from testtools.matchers import AllMatch, Equals, IsInstance, MatchesStructure
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks


wait_for_reactor = wait_for(30)  # 30 seconds.


def make_region_with_address(space):
    region = factory.make_RegionController()
    iface = factory.make_Interface(node=region)
    cidr4 = factory.make_ipv4_network(24)
    subnet4 = factory.make_Subnet(space=space, cidr=cidr4)
    cidr6 = factory.make_ipv6_network(64)
    subnet6 = factory.make_Subnet(space=space, cidr=cidr6)
    sip4 = factory.make_StaticIPAddress(interface=iface, subnet=subnet4)
    sip6 = factory.make_StaticIPAddress(interface=iface, subnet=subnet6)
    return region, sip4, sip6


class TestRegionNetworkTimeProtocolService_Basic(MAASTestCase):
    """Basic tests for `RegionNetworkTimeProtocolService`."""

    def test_service_uses__tryUpdate_as_periodic_function(self):
        service = ntp.RegionNetworkTimeProtocolService(reactor)
        self.assertThat(service.call, Equals((service._tryUpdate, (), {})))

    def test_service_iterates_every_30_seconds(self):
        service = ntp.RegionNetworkTimeProtocolService(reactor)
        self.assertThat(service.step, Equals(30.0))


class TestRegionNetworkTimeProtocolService(MAASTransactionServerTestCase):
    """Tests for `RegionNetworkTimeProtocolService`."""

    def setUp(self):
        super(TestRegionNetworkTimeProtocolService, self).setUp()
        self.useFixture(MAASRootFixture())

    @transactional
    def make_example_configuration(self):
        # Configure example time references.
        ntp_servers = {factory.make_name("ntp-server") for _ in range(5)}
        Config.objects.set_config("ntp_servers", " ".join(ntp_servers))
        # Populate the database with example peers.
        space = factory.make_Space()
        region, addr4, addr6 = make_region_with_address(space)
        self.useFixture(MAASIDFixture(region.system_id))
        peer1, addr1_4, addr1_6 = make_region_with_address(space)
        peer2, addr2_4, addr2_6 = make_region_with_address(space)
        # Return the servers and all possible peer IP addresses.
        return ntp_servers, {addr1_4.ip, addr1_6.ip, addr2_4.ip, addr2_6.ip}

    @wait_for_reactor
    @inlineCallbacks
    def test__tryUpdate_updates_ntp_server(self):
        service = ntp.RegionNetworkTimeProtocolService(reactor)
        refs, peers = yield deferToDatabase(self.make_example_configuration)
        configure_region = self.patch_autospec(ntp, "configure_region")
        restartService = self.patch_autospec(service_monitor, "restartService")
        yield service._tryUpdate()
        self.assertThat(
            configure_region,
            MockCalledOnceWith(refs, Matches(AllMatch(ContainedBy(peers)))),
        )
        self.assertThat(restartService, MockCalledOnceWith("ntp_region"))
        # If the configuration has not changed then a second call to
        # `_tryUpdate` does not result in another call to `configure_region`.
        yield service._tryUpdate()
        self.assertThat(configure_region, MockCalledOnce())
        self.assertThat(restartService, MockCalledOnceWith("ntp_region"))


class TestRegionNetworkTimeProtocolService_Errors(
    MAASTransactionServerTestCase
):
    """Tests for error handing in `RegionNetworkTimeProtocolService`."""

    scenarios = (
        ("_getConfiguration", dict(method="_getConfiguration")),
        ("_maybeApplyConfiguration", dict(method="_maybeApplyConfiguration")),
        ("_applyConfiguration", dict(method="_applyConfiguration")),
        ("_configurationApplied", dict(method="_configurationApplied")),
    )

    @wait_for_reactor
    @inlineCallbacks
    def test__tryUpdate_logs_errors_from_broken_method(self):
        service = ntp.RegionNetworkTimeProtocolService(reactor)
        broken_method = self.patch_autospec(service, self.method)
        broken_method.side_effect = factory.make_exception()

        # Ensure that we never actually execute against systemd or write an
        # actual configuration file.
        self.patch_autospec(
            ntp, "deferToThread"
        ).side_effect = always_succeed_with(None)
        self.patch_autospec(service_monitor, "restartService")

        with TwistedLoggerFixture() as logger:
            yield service._tryUpdate()

        self.assertThat(
            logger.output,
            DocTestMatches(
                """
                Failed to update NTP configuration.
                Traceback (most recent call last):
                ...
                maastesting.factory.TestException#...
                """
            ),
        )


class TestRegionNetworkTimeProtocolService_Database(MAASServerTestCase):
    """Database tests for `RegionNetworkTimeProtocolService`."""

    def test__getConfiguration_returns_configuration_object(self):
        service = ntp.RegionNetworkTimeProtocolService(reactor)

        # Configure example time references.
        ntp_servers = {factory.make_name("ntp-server") for _ in range(5)}
        Config.objects.set_config("ntp_servers", " ".join(ntp_servers))

        # Put all addresses in the same space so they're mutually routable.
        space = factory.make_Space()
        # Populate the database with "this" region and an example peer.
        region, _, _ = make_region_with_address(space)
        self.useFixture(MAASIDFixture(region.system_id))
        peer, addr4, addr6 = make_region_with_address(space)

        observed = service._getConfiguration()
        self.assertThat(observed, IsInstance(ntp._Configuration))

        expected_references = Equals(frozenset(ntp_servers))
        expected_peers = AllMatch(ContainedBy({addr4.ip, addr6.ip}))

        self.assertThat(
            observed,
            MatchesStructure(
                references=expected_references, peers=expected_peers
            ),
        )
