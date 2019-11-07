# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for src/provisioningserver/rackdservices/lease_socket_service.py"""

__all__ = []

import json
import os
import socket
import time
from unittest.mock import MagicMock, sentinel

from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
from provisioningserver.rackdservices import lease_socket_service
from provisioningserver.rackdservices.lease_socket_service import (
    LeaseSocketService,
)
from provisioningserver.rpc import getRegionClient
from provisioningserver.rpc.region import UpdateLease
from provisioningserver.rpc.testing import MockLiveClusterToRegionRPCFixture
from provisioningserver.utils.twisted import DeferredValue, pause, retries
from testtools.matchers import Not, PathExists
from twisted.application.service import Service
from twisted.internet import defer, reactor
from twisted.internet.protocol import DatagramProtocol
from twisted.internet.threads import deferToThread


class TestLeaseSocketService(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def patch_socket_path(self):
        path = self.make_dir()
        socket_path = os.path.join(path, "dhcpd.sock")
        self.patch(
            lease_socket_service, "get_socket_path"
        ).return_value = socket_path
        return socket_path

    def patch_rpc_UpdateLease(self):
        fixture = self.useFixture(MockLiveClusterToRegionRPCFixture())
        protocol, connecting = fixture.makeEventLoop(UpdateLease)
        return protocol, connecting

    def send_notification(self, socket_path, payload):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        conn.connect(socket_path)
        conn.send(json.dumps(payload).encode("utf-8"))
        conn.close()

    def test_init(self):
        socket_path = self.patch_socket_path()
        service = LeaseSocketService(sentinel.service, sentinel.reactor)
        self.assertIsInstance(service, Service)
        self.assertIsInstance(service, DatagramProtocol)
        self.assertIs(service.reactor, sentinel.reactor)
        self.assertIs(service.client_service, sentinel.service)
        self.assertEquals(socket_path, service.address)

    def test_startService_creates_socket(self):
        socket_path = self.patch_socket_path()
        service = LeaseSocketService(sentinel.service, reactor)
        service.startService()
        self.addCleanup(service.stopService)
        self.assertThat(socket_path, PathExists())

    @defer.inlineCallbacks
    def test_stopService_deletes_socket(self):
        socket_path = self.patch_socket_path()
        service = LeaseSocketService(sentinel.service, reactor)
        service.startService()
        yield service.stopService()
        self.assertThat(socket_path, Not(PathExists()))

    @defer.inlineCallbacks
    def test_notification_gets_added_to_notifications(self):
        socket_path = self.patch_socket_path()
        service = LeaseSocketService(sentinel.service, reactor)
        service.startService()
        self.addCleanup(service.stopService)

        # Stop the looping call to check that the notification gets added
        # to notifications.
        process_done = service.done
        service.processor.stop()
        yield process_done
        service.processor = MagicMock()

        # Create test payload to send.
        packet = {"test": factory.make_name("test")}

        # Send notification to the socket should appear in notifications.
        yield deferToThread(self.send_notification, socket_path, packet)

        # Loop until the notifications has a notification.
        for elapsed, remaining, wait in retries(5, 0.1, reactor):
            if len(service.notifications) > 0:
                break
            else:
                yield pause(wait, reactor)

        # Should have one notitication.
        self.assertEquals([packet], list(service.notifications))

    @defer.inlineCallbacks
    def test_processNotification_gets_called_with_notification(self):
        socket_path = self.patch_socket_path()
        service = LeaseSocketService(sentinel.service, reactor)
        dv = DeferredValue()

        # Mock processNotifcation to catch the call.
        def mock_processNotification(*args, **kwargs):
            dv.set(args)

        self.patch(service, "processNotification", mock_processNotification)

        # Start the service and stop it at the end of the test.
        service.startService()
        self.addCleanup(service.stopService)

        # Create test payload to send.
        packet = {"test": factory.make_name("test")}

        # Send notification to the socket and wait for notification.
        yield deferToThread(self.send_notification, socket_path, packet)
        yield dv.get(timeout=10)

        # Packet should be the argument passed to processNotifcation
        self.assertEquals((packet,), dv.value)

    @defer.inlineCallbacks
    def test_processNotification_gets_called_multiple_times(self):
        socket_path = self.patch_socket_path()
        service = LeaseSocketService(sentinel.service, reactor)
        dvs = [DeferredValue(), DeferredValue()]

        # Mock processNotifcation to catch the call.
        def mock_processNotification(*args, **kwargs):
            for dv in dvs:
                if not dv.isSet:
                    dv.set(args)
                    break

        self.patch(service, "processNotification", mock_processNotification)

        # Start the service and stop it at the end of the test.
        service.startService()
        self.addCleanup(service.stopService)

        # Create test payload to send.
        packet1 = {"test1": factory.make_name("test1")}
        packet2 = {"test2": factory.make_name("test2")}

        # Send notifications to the socket and wait for notifications.
        yield deferToThread(self.send_notification, socket_path, packet1)
        yield deferToThread(self.send_notification, socket_path, packet2)
        yield dvs[0].get(timeout=10)
        yield dvs[1].get(timeout=10)

        # Packet should be the argument passed to processNotification in
        # order.
        self.assertEquals((packet1,), dvs[0].value)
        self.assertEquals((packet2,), dvs[1].value)

    @defer.inlineCallbacks
    def test_processNotification_send_to_region(self):
        protocol, connecting = self.patch_rpc_UpdateLease()
        self.addCleanup((yield connecting))

        client = getRegionClient()
        rpc_service = MagicMock()
        rpc_service.getClientNow.return_value = defer.succeed(client)
        service = LeaseSocketService(rpc_service, reactor)

        # Notification to region.
        packet = {
            "action": "commit",
            "mac": factory.make_mac_address(),
            "ip_family": "ipv4",
            "ip": factory.make_ipv4_address(),
            "timestamp": int(time.time()),
            "lease_time": 30,
            "hostname": factory.make_name("host"),
        }
        yield service.processNotification(packet, clock=reactor)
        self.assertThat(
            protocol.UpdateLease,
            MockCalledOnceWith(
                protocol,
                cluster_uuid=client.localIdent,
                action=packet["action"],
                mac=packet["mac"],
                ip_family=packet["ip_family"],
                ip=packet["ip"],
                timestamp=packet["timestamp"],
                lease_time=packet["lease_time"],
                hostname=packet["hostname"],
            ),
        )
