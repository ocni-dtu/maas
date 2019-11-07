# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for node power status query when state changes."""

__all__ = []

from unittest.mock import ANY

from crochet import wait_for
from maasserver.enum import POWER_STATE
from maasserver.exceptions import PowerProblem
from maasserver.models.node import Node
from maasserver.models.signals import power
from maasserver.node_status import get_failed_status, NODE_STATUS
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.orm import post_commit_hooks, transactional
from maasserver.utils.threads import deferToDatabase
from maastesting.matchers import MockCalledOnceWith, MockNotCalled
from provisioningserver.rpc.exceptions import UnknownPowerType
from twisted.internet import defer
from twisted.internet.task import Clock


wait_for_reactor = wait_for(30)  # 30 seconds.


class TestStatusQueryEvent(MAASServerTestCase):
    def test_changing_status_of_node_emits_event(self):
        self.patch_autospec(power, "update_power_state_of_node_soon")
        old_status = NODE_STATUS.COMMISSIONING
        node = factory.make_Node(status=old_status, power_type="virsh")
        node.status = get_failed_status(old_status)

        with post_commit_hooks:
            node.save()
            # update_power_state_of_node_soon is registered as a post-commit
            # task, so it's not called immediately.
            self.expectThat(
                power.update_power_state_of_node_soon, MockNotCalled()
            )

        # One post-commit hooks have been fired, then it's called.
        post_commit_hooks.fire()
        self.assertThat(
            power.update_power_state_of_node_soon,
            MockCalledOnceWith(node.system_id),
        )

    def test_changing_not_tracked_status_of_node_doesnt_emit_event(self):
        self.patch_autospec(power, "update_power_state_of_node_soon")
        old_status = NODE_STATUS.ALLOCATED
        node = factory.make_Node(status=old_status, power_type="virsh")
        node.status = NODE_STATUS.DEPLOYING
        node.save()
        self.assertThat(power.update_power_state_of_node_soon, MockNotCalled())


class TestUpdatePowerStateOfNodeSoon(MAASServerTestCase):
    def test__calls_update_power_state_of_node_after_wait_time(self):
        self.patch_autospec(power, "update_power_state_of_node")
        node = factory.make_Node(power_type="virsh")
        clock = Clock()
        power.update_power_state_of_node_soon(node.system_id, clock=clock)
        self.assertThat(power.update_power_state_of_node, MockNotCalled())
        clock.advance(power.WAIT_TO_QUERY.total_seconds())
        self.assertThat(
            power.update_power_state_of_node,
            MockCalledOnceWith(node.system_id),
        )


class TestUpdatePowerStateOfNode(MAASTransactionServerTestCase):
    @wait_for_reactor
    @defer.inlineCallbacks
    def test__retrieves_power_state(self):
        node = yield deferToDatabase(transactional(factory.make_Node))
        mock_power_query = self.patch(Node, "power_query")
        mock_power_query.return_value = POWER_STATE.ON
        power_state = yield power.update_power_state_of_node(node.system_id)
        self.assertEqual(power_state, POWER_STATE.ON)

    def test__traps_failure_for_Node_DoesNotExist(self):
        self.assertIsNone(
            power.update_power_state_of_node(factory.make_name("system_id"))
        )

    @wait_for_reactor
    @defer.inlineCallbacks
    def test__traps_failure_for_UnknownPowerType(self):
        node = yield deferToDatabase(transactional(factory.make_Node))
        mock_power_query = self.patch(Node, "power_query")
        mock_power_query.side_effect = UnknownPowerType()
        power_state = yield power.update_power_state_of_node(node.system_id)
        self.assertIsNone(power_state)

    @wait_for_reactor
    @defer.inlineCallbacks
    def test__traps_failure_for_PowerProblem(self):
        node = yield deferToDatabase(transactional(factory.make_Node))
        mock_power_query = self.patch(Node, "power_query")
        mock_power_query.side_effect = PowerProblem()
        power_state = yield power.update_power_state_of_node(node.system_id)
        self.assertIsNone(power_state)

    @wait_for_reactor
    @defer.inlineCallbacks
    def test__logs_other_errors(self):
        node = yield deferToDatabase(transactional(factory.make_Node))
        mock_power_query = self.patch(Node, "power_query")
        mock_power_query.side_effect = factory.make_exception("Error")
        mock_log_err = self.patch(power.log, "err")
        yield power.update_power_state_of_node(node.system_id)
        self.assertThat(
            mock_log_err,
            MockCalledOnceWith(
                ANY,
                "Failed to update power state of machine after state "
                "transition.",
            ),
        )
