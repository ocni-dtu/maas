# -*- coding: utf-8 -*-

# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test `provisioningserver.rpc.utils`."""

__all__ = []

import json
from random import choice

from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
from provisioningserver.rpc import region
from provisioningserver.rpc.exceptions import (
    CommissionNodeFailed,
    NodeAlreadyExists,
)
from provisioningserver.rpc.testing import MockLiveClusterToRegionRPCFixture
import provisioningserver.rpc.utils
from provisioningserver.rpc.utils import commission_node, create_node
import provisioningserver.utils
from twisted.internet import defer


class TestCreateNode(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def prepare_region_rpc(self):
        fixture = self.useFixture(MockLiveClusterToRegionRPCFixture())
        protocol, connecting = fixture.makeEventLoop(region.CreateNode)
        return protocol, connecting

    @defer.inlineCallbacks
    def test_calls_create_node_rpc(self):
        protocol, connecting = self.prepare_region_rpc()
        self.addCleanup((yield connecting))
        protocol.CreateNode.return_value = defer.succeed(
            {"system_id": factory.make_name("system-id")}
        )

        uuid = "node-" + factory.make_UUID()
        macs = sorted(factory.make_mac_address() for _ in range(3))
        arch = factory.make_name("architecture")
        hostname = factory.make_hostname()
        domain = factory.make_name("domain")

        power_type = factory.make_name("power_type")
        power_parameters = {
            "power_address": factory.make_ipv4_address(),
            "power_user": factory.make_name("power_user"),
            "power_pass": factory.make_name("power_pass"),
            "power_control": None,
            "system_id": uuid,
        }

        yield create_node(
            macs,
            arch,
            power_type,
            power_parameters,
            domain=domain,
            hostname=hostname,
        )
        self.assertThat(
            protocol.CreateNode,
            MockCalledOnceWith(
                protocol,
                architecture=arch,
                power_type=power_type,
                power_parameters=json.dumps(power_parameters),
                mac_addresses=macs,
                domain=domain,
                hostname=hostname,
            ),
        )

    @defer.inlineCallbacks
    def test_returns_system_id_of_new_node(self):
        protocol, connecting = self.prepare_region_rpc()
        self.addCleanup((yield connecting))
        system_id = factory.make_name("system-id")
        protocol.CreateNode.return_value = defer.succeed(
            {"system_id": system_id}
        )
        get_cluster_uuid = self.patch(
            provisioningserver.utils, "get_cluster_uuid"
        )
        get_cluster_uuid.return_value = "cluster-" + factory.make_UUID()

        uuid = "node-" + factory.make_UUID()
        macs = sorted(factory.make_mac_address() for _ in range(3))
        arch = factory.make_name("architecture")
        power_type = factory.make_name("power_type")
        power_parameters = {
            "power_address": factory.make_ipv4_address(),
            "power_user": factory.make_name("power_user"),
            "power_pass": factory.make_name("power_pass"),
            "power_control": None,
            "system_id": uuid,
        }
        new_system_id = yield create_node(
            macs, arch, power_type, power_parameters
        )
        self.assertEqual(system_id, new_system_id)

    @defer.inlineCallbacks
    def test_passes_on_no_duplicate_macs(self):
        protocol, connecting = self.prepare_region_rpc()
        self.addCleanup((yield connecting))
        system_id = factory.make_name("system-id")
        protocol.CreateNode.return_value = defer.succeed(
            {"system_id": system_id}
        )

        uuid = "node-" + factory.make_UUID()
        arch = factory.make_name("architecture")
        power_type = factory.make_name("power_type")
        power_parameters = {
            "power_address": factory.make_ipv4_address(),
            "power_user": factory.make_name("power_user"),
            "power_pass": factory.make_name("power_pass"),
            "power_control": None,
            "system_id": uuid,
        }

        # Create a list of MACs with one random duplicate.
        macs = sorted(factory.make_mac_address() for _ in range(3))
        macs_with_duplicate = macs + [choice(macs)]

        yield create_node(
            macs_with_duplicate, arch, power_type, power_parameters
        )
        self.assertThat(
            protocol.CreateNode,
            MockCalledOnceWith(
                protocol,
                architecture=arch,
                power_type=power_type,
                power_parameters=json.dumps(power_parameters),
                mac_addresses=macs,
                domain=None,
                hostname=None,
            ),
        )

    @defer.inlineCallbacks
    def test_logs_error_on_duplicate_macs(self):
        protocol, connecting = self.prepare_region_rpc()
        self.addCleanup((yield connecting))
        system_id = factory.make_name("system-id")
        maaslog = self.patch(provisioningserver.rpc.utils, "maaslog")

        uuid = "node-" + factory.make_UUID()
        macs = sorted(factory.make_mac_address() for _ in range(3))
        arch = factory.make_name("architecture")
        power_type = factory.make_name("power_type")
        power_parameters = {
            "power_address": factory.make_ipv4_address(),
            "power_user": factory.make_name("power_user"),
            "power_pass": factory.make_name("power_pass"),
            "power_control": None,
            "system_id": uuid,
        }

        protocol.CreateNode.side_effect = [
            defer.succeed({"system_id": system_id}),
            defer.fail(NodeAlreadyExists("Node already exists.")),
        ]

        yield create_node(macs, arch, power_type, power_parameters)
        yield create_node(macs, arch, power_type, power_parameters)
        self.assertThat(
            maaslog.error,
            MockCalledOnceWith(
                "A node with one of the mac addresses in %s already "
                "exists.",
                macs,
            ),
        )


class TestCommissionNode(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def prepare_region_rpc(self):
        fixture = self.useFixture(MockLiveClusterToRegionRPCFixture())
        protocol, connecting = fixture.makeEventLoop(region.CommissionNode)
        return protocol, connecting

    @defer.inlineCallbacks
    def test_calls_commission_node_rpc(self):
        protocol, connecting = self.prepare_region_rpc()
        self.addCleanup((yield connecting))
        protocol.CommissionNode.return_value = defer.succeed({})
        system_id = factory.make_name("system_id")
        user = factory.make_name("user")

        yield commission_node(system_id, user)
        self.assertThat(
            protocol.CommissionNode,
            MockCalledOnceWith(protocol, system_id=system_id, user=user),
        )

    @defer.inlineCallbacks
    def test_logs_error_when_not_able_to_commission(self):
        protocol, connecting = self.prepare_region_rpc()
        self.addCleanup((yield connecting))
        maaslog = self.patch(provisioningserver.rpc.utils, "maaslog")
        system_id = factory.make_name("system_id")
        user = factory.make_name("user")
        error = CommissionNodeFailed("error")

        protocol.CommissionNode.return_value = defer.fail(error)

        yield commission_node(system_id, user)
        self.assertThat(
            maaslog.error,
            MockCalledOnceWith(
                "Could not commission with system_id %s because %s.",
                system_id,
                error.args[0],
            ),
        )
