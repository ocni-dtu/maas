# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for
:py:module:`~provisioningserver.rackdservices.node_power_monitor_service`."""

__all__ = []

from unittest.mock import ANY, Mock, sentinel

from fixtures import FakeLogger
from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
from maastesting.twisted import extract_result, TwistedLoggerFixture
from provisioningserver.rackdservices import node_power_monitor_service as npms
from provisioningserver.rpc import exceptions, getRegionClient, region
from provisioningserver.rpc.testing import MockClusterToRegionRPCFixture
from testtools.matchers import MatchesStructure
from twisted.internet.defer import fail, succeed
from twisted.internet.error import ConnectionDone
from twisted.internet.task import Clock


class TestNodePowerMonitorService(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_init_sets_up_timer_correctly(self):
        service = npms.NodePowerMonitorService()
        self.assertThat(
            service,
            MatchesStructure.byEquality(
                call=(service.try_query_nodes, tuple(), {}),
                step=15,
                clock=None,
            ),
        )

    def make_monitor_service(self):
        service = npms.NodePowerMonitorService(Clock())
        return service

    def test_query_nodes_calls_the_region(self):
        service = self.make_monitor_service()

        rpc_fixture = self.useFixture(MockClusterToRegionRPCFixture())
        proto_region, io = rpc_fixture.makeEventLoop(
            region.ListNodePowerParameters
        )
        proto_region.ListNodePowerParameters.return_value = succeed(
            {"nodes": []}
        )

        client = getRegionClient()
        d = service.query_nodes(client)
        io.flush()

        self.assertEqual(None, extract_result(d))
        self.assertThat(
            proto_region.ListNodePowerParameters,
            MockCalledOnceWith(ANY, uuid=client.localIdent),
        )

    def test_query_nodes_calls_query_all_nodes(self):
        service = self.make_monitor_service()
        service.max_nodes_at_once = sentinel.max_nodes_at_once

        example_power_parameters = {
            "system_id": factory.make_UUID(),
            "hostname": factory.make_hostname(),
            "power_state": factory.make_name("power_state"),
            "power_type": factory.make_name("power_type"),
            "context": {},
        }

        rpc_fixture = self.useFixture(MockClusterToRegionRPCFixture())
        proto_region, io = rpc_fixture.makeEventLoop(
            region.ListNodePowerParameters
        )
        proto_region.ListNodePowerParameters.side_effect = [
            succeed({"nodes": [example_power_parameters]}),
            succeed({"nodes": []}),
        ]

        query_all_nodes = self.patch(npms, "query_all_nodes")

        d = service.query_nodes(getRegionClient())
        io.flush()

        self.assertEqual(None, extract_result(d))
        self.assertThat(
            query_all_nodes,
            MockCalledOnceWith(
                [example_power_parameters],
                max_concurrency=sentinel.max_nodes_at_once,
                clock=service.clock,
            ),
        )

    def test_query_nodes_copes_with_NoSuchCluster(self):
        service = self.make_monitor_service()

        rpc_fixture = self.useFixture(MockClusterToRegionRPCFixture())
        proto_region, io = rpc_fixture.makeEventLoop(
            region.ListNodePowerParameters
        )
        client = getRegionClient()
        proto_region.ListNodePowerParameters.return_value = fail(
            exceptions.NoSuchCluster.from_uuid(client.localIdent)
        )

        d = service.query_nodes(client)
        d.addErrback(service.query_nodes_failed, client.localIdent)
        with FakeLogger("maas") as maaslog:
            io.flush()

        self.assertEqual(None, extract_result(d))
        self.assertDocTestMatches(
            "Rack controller '...' is not recognised.", maaslog.output
        )

    def test_query_nodes_copes_with_losing_connection_to_region(self):
        service = self.make_monitor_service()

        client = Mock(
            return_value=fail(ConnectionDone("Connection was closed cleanly."))
        )

        with FakeLogger("maas") as maaslog:
            d = service.query_nodes(client)
            d.addErrback(service.query_nodes_failed, sentinel.ident)

        self.assertEqual(None, extract_result(d))
        self.assertDocTestMatches(
            "Lost connection to region controller.", maaslog.output
        )

    def test_try_query_nodes_logs_other_errors(self):
        service = self.make_monitor_service()
        self.patch(npms, "getRegionClient").return_value = sentinel.client
        sentinel.client.localIdent = factory.make_UUID()

        query_nodes = self.patch(service, "query_nodes")
        query_nodes.return_value = fail(
            ZeroDivisionError("Such a shame I can't divide by zero")
        )

        with FakeLogger("maas") as maaslog, TwistedLoggerFixture():
            d = service.try_query_nodes()

        self.assertEqual(None, extract_result(d))
        self.assertDocTestMatches(
            "Failed to query nodes' power status: "
            "Such a shame I can't divide by zero",
            maaslog.output,
        )
