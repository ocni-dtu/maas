# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for reverse-DNS service."""

__all__ = []

from unittest.mock import Mock

from crochet import wait_for
from maasserver.models import RDNS
from maasserver.regiondservices import reverse_dns as reverse_dns_module
from maasserver.regiondservices.reverse_dns import ReverseDNSService
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASTransactionServerTestCase
from maasserver.utils.threads import deferToDatabase
from maastesting.matchers import MockCalledOnceWith
from provisioningserver.utils.testing import callWithServiceRunning
from provisioningserver.utils.tests.test_network import TestReverseResolveMixIn
from testtools.matchers import Equals, Is
from twisted.internet import defer
from twisted.internet.defer import inlineCallbacks


class TestReverseDNSService(
    TestReverseResolveMixIn, MAASTransactionServerTestCase
):
    """Tests for `RegionNetworksMonitoringService`."""

    def setUp(self):
        super().setUp()
        self.region = factory.make_RegionRackController()
        # This is so get_running_controller() works properly.
        RegionController = self.patch(reverse_dns_module, "RegionController")
        RegionController.objects = Mock()
        RegionController.objects.get_running_controller = Mock()
        RegionController.objects.get_running_controller.return_value = (
            self.region
        )

    @wait_for(30)
    @inlineCallbacks
    def test__caches_region_model_object(self):
        hostname = factory.make_hostname()
        self.set_fake_twisted_dns_reply([hostname])
        service = ReverseDNSService()
        yield service.startService()
        self.assertThat(service.region, Equals(self.region))
        service.stopService()

    @wait_for(30)
    @inlineCallbacks
    def test__adds_rdns_entry(self):
        hostname = factory.make_hostname()
        self.set_fake_twisted_dns_reply([hostname])
        service = ReverseDNSService()
        yield service.startService()
        ip = factory.make_ip_address(ipv6=False)
        yield service.consumeNeighbourEvent("create", "%s/32" % ip)
        service.stopService()
        result = yield deferToDatabase(RDNS.objects.first)
        self.assertThat(result.ip, Equals(ip))
        self.assertThat(result.hostname, Equals(hostname))

    @wait_for(30)
    @inlineCallbacks
    def test__updates_rdns_entry(self):
        hostname = factory.make_hostname()
        hostname2 = factory.make_hostname()
        self.set_fake_twisted_dns_reply([hostname])
        service = ReverseDNSService()
        yield service.startService()
        ip = factory.make_ip_address(ipv6=False)
        yield service.consumeNeighbourEvent("create", "%s/32" % ip)
        self.set_fake_twisted_dns_reply([hostname2])
        yield service.consumeNeighbourEvent("update", "%s/32" % ip)
        service.stopService()
        result = yield deferToDatabase(RDNS.objects.first)
        self.assertThat(result.ip, Equals(ip))
        self.assertThat(result.hostname, Equals(hostname2))

    @wait_for(30)
    @inlineCallbacks
    def test__deletes_rdns_entry(self):
        hostname = factory.make_hostname()
        self.set_fake_twisted_dns_reply([hostname])
        service = ReverseDNSService()
        yield service.startService()
        ip = factory.make_ip_address(ipv6=False)
        yield service.consumeNeighbourEvent("create", "%s/32" % ip)
        yield service.consumeNeighbourEvent("delete", "%s/32" % ip)
        service.stopService()
        result = yield deferToDatabase(RDNS.objects.first)
        self.assertThat(result, Is(None))

    @wait_for(30)
    @inlineCallbacks
    def test__registers_and_unregisters_listener(self):
        listener = Mock()
        listener.register = Mock()
        listener.unregister = Mock()
        service = ReverseDNSService(postgresListener=listener)
        yield service.startService()
        self.assertThat(
            listener.register,
            MockCalledOnceWith("neighbour", service.consumeNeighbourEvent),
        )
        service.stopService()
        self.assertThat(
            listener.unregister,
            MockCalledOnceWith("neighbour", service.consumeNeighbourEvent),
        )

    @wait_for(30)
    @inlineCallbacks
    def test__ignores_timeouts_when_consuming_neighbour_event(self):
        reverseResolve = self.patch(reverse_dns_module, "reverseResolve")
        reverseResolve.return_value = defer.fail(defer.TimeoutError())
        ip = factory.make_ip_address(ipv6=False)
        service = ReverseDNSService()
        yield callWithServiceRunning(
            service, service.consumeNeighbourEvent, "create", "%s/32" % ip
        )
        self.assertThat(reverseResolve, MockCalledOnceWith(ip))
        result = yield deferToDatabase(RDNS.objects.first)
        self.assertThat(result, Is(None))
