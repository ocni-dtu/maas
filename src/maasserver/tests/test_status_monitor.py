# Copyright 2016-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the status monitor module."""

__all__ = []


from datetime import timedelta
from unittest.mock import call

from maasserver import status_monitor
from maasserver.enum import NODE_STATUS
from maasserver.models import Config, Node
from maasserver.models.signals.testing import SignalsDisabled
from maasserver.models.timestampedmodel import now
from maasserver.node_status import (
    get_node_timeout,
    NODE_FAILURE_MONITORED_STATUS_TRANSITIONS,
)
from maasserver.status_monitor import (
    mark_nodes_failed_after_expiring,
    mark_nodes_failed_after_missing_script_timeout,
    StatusMonitorService,
)
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.orm import reload_object
from maastesting.djangotestcase import CountQueries
from maastesting.matchers import (
    MockCalledOnce,
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from metadataserver.enum import SCRIPT_STATUS, SCRIPT_TYPE
from metadataserver.models import ScriptSet
from provisioningserver.refresh.node_info_scripts import NODE_INFO_SCRIPTS
from twisted.internet.defer import maybeDeferred
from twisted.internet.task import Clock


class TestMarkNodesFailedAfterExpiring(MAASServerTestCase):
    def test__marks_all_possible_failed_status_as_failed(self):
        maaslog = self.patch(status_monitor.maaslog, "info")
        self.useFixture(SignalsDisabled("power"))
        current_time = now()
        expired_time = current_time - timedelta(minutes=1)
        nodes = [
            factory.make_Node(status=status, status_expires=expired_time)
            for status in NODE_FAILURE_MONITORED_STATUS_TRANSITIONS.keys()
        ]
        mark_nodes_failed_after_expiring(current_time, 20)
        failed_statuses = [reload_object(node).status for node in nodes]
        self.assertItemsEqual(
            NODE_FAILURE_MONITORED_STATUS_TRANSITIONS.values(), failed_statuses
        )
        # MAAS logs in the status_monitor that the timeout was detected. It
        # then logs the transisition in the node signal handler.
        self.assertEquals(
            len(NODE_FAILURE_MONITORED_STATUS_TRANSITIONS),
            len(maaslog.call_args_list) / 2,
        )

    def test__skips_those_that_have_not_expired(self):
        maaslog = self.patch(status_monitor.maaslog, "info")
        self.useFixture(SignalsDisabled("power"))
        current_time = now()
        expired_time = current_time + timedelta(minutes=1)
        nodes = [
            factory.make_Node(status=status, status_expires=expired_time)
            for status in NODE_FAILURE_MONITORED_STATUS_TRANSITIONS.keys()
        ]
        mark_nodes_failed_after_expiring(current_time, 20)
        failed_statuses = [reload_object(node).status for node in nodes]
        self.assertItemsEqual(
            NODE_FAILURE_MONITORED_STATUS_TRANSITIONS.keys(), failed_statuses
        )
        self.assertThat(maaslog, MockNotCalled())


class TestMarkNodesFailedAfterMissingScriptTimeout(MAASServerTestCase):

    scenarios = (
        (
            "commissioning",
            {
                "status": NODE_STATUS.COMMISSIONING,
                "failed_status": NODE_STATUS.FAILED_COMMISSIONING,
            },
        ),
        (
            "testing",
            {
                "status": NODE_STATUS.TESTING,
                "failed_status": NODE_STATUS.FAILED_TESTING,
            },
        ),
    )

    def setUp(self):
        super().setUp()
        self.useFixture(SignalsDisabled("power"))
        self.mock_stop = self.patch(Node, "stop")
        self.maaslog = self.patch(status_monitor.maaslog, "info")

    def make_node(self):
        user = factory.make_admin()
        node = factory.make_Node(
            status=self.status,
            with_empty_script_sets=True,
            owner=user,
            enable_ssh=factory.pick_bool(),
        )
        if self.status == NODE_STATUS.COMMISSIONING:
            script_set = node.current_commissioning_script_set
        elif self.status == NODE_STATUS.TESTING:
            script_set = node.current_testing_script_set
        return node, script_set

    def test_mark_nodes_handled_last_ping_None(self):
        node, script_set = self.make_node()
        script_set.last_ping = None
        script_set.save()
        for _ in range(3):
            factory.make_ScriptResult(
                script_set=script_set, status=SCRIPT_STATUS.PENDING
            )

        # No exception should be raised.
        mark_nodes_failed_after_missing_script_timeout(now(), 20)
        node = reload_object(node)
        self.assertEquals(self.status, node.status)
        self.assertThat(self.maaslog, MockNotCalled())

    def test_mark_nodes_failed_after_missing_timeout_heartbeat(self):
        node, script_set = self.make_node()
        current_time = now()
        node_timeout = Config.objects.get_config("node_timeout")
        script_set.last_ping = current_time - timedelta(
            minutes=(node_timeout + 1)
        )
        script_set.save()
        script_results = [
            factory.make_ScriptResult(
                script_set=script_set, status=SCRIPT_STATUS.PENDING
            )
            for _ in range(3)
        ]

        mark_nodes_failed_after_missing_script_timeout(
            current_time, node_timeout
        )
        node = reload_object(node)

        self.assertEquals(self.failed_status, node.status)
        self.assertEquals(
            "Node has not been heard from for the last %s minutes"
            % node_timeout,
            node.error_description,
        )
        self.assertIn(
            call(
                "%s: Has not been heard from for the last %s minutes"
                % (node.hostname, node_timeout)
            ),
            self.maaslog.call_args_list,
        )
        if node.enable_ssh:
            self.assertThat(self.mock_stop, MockNotCalled())
        else:
            self.assertThat(self.mock_stop, MockCalledOnce())
            self.assertIn(
                call("%s: Stopped because SSH is disabled" % node.hostname),
                self.maaslog.call_args_list,
            )
        for script_result in script_results:
            self.assertEquals(
                SCRIPT_STATUS.TIMEDOUT, reload_object(script_result).status
            )

    def test_sets_status_expires_when_flatlined_with_may_reboot_script(self):
        node, script_set = self.make_node()
        current_time = now()
        if self.status == NODE_STATUS.COMMISSIONING:
            script_type = SCRIPT_TYPE.COMMISSIONING
        else:
            script_type = SCRIPT_TYPE.TESTING
        script = factory.make_Script(script_type=script_type, may_reboot=True)
        factory.make_ScriptResult(
            script=script, script_set=script_set, status=SCRIPT_STATUS.RUNNING
        )
        script_set.last_ping = current_time - timedelta(11)
        script_set.save()

        mark_nodes_failed_after_missing_script_timeout(current_time, 20)
        node = reload_object(node)

        self.assertEquals(
            current_time
            - (current_time - script_set.last_ping)
            + timedelta(minutes=get_node_timeout(self.status, 20)),
            node.status_expires,
        )

    def test_mark_nodes_failed_after_script_overrun(self):
        node, script_set = self.make_node()
        current_time = now()
        script_set.last_ping = current_time
        script_set.save()
        passed_script_result = factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.PASSED
        )
        failed_script_result = factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.FAILED
        )
        pending_script_result = factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.PENDING
        )
        script = factory.make_Script(timeout=timedelta(seconds=60))
        running_script_result = factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.RUNNING,
            script=script,
            started=current_time - timedelta(minutes=10),
        )

        mark_nodes_failed_after_missing_script_timeout(current_time, 20)
        node = reload_object(node)

        self.assertEquals(self.failed_status, node.status)
        self.assertEquals(
            "%s has run past it's timeout(%s)"
            % (
                running_script_result.name,
                str(running_script_result.script.timeout),
            ),
            node.error_description,
        )
        self.assertIn(
            call(
                "%s: %s has run past it's timeout(%s)"
                % (
                    node.hostname,
                    running_script_result.name,
                    str(running_script_result.script.timeout),
                )
            ),
            self.maaslog.call_args_list,
        )
        if node.enable_ssh:
            self.assertThat(self.mock_stop, MockNotCalled())
        else:
            self.assertThat(self.mock_stop, MockCalledOnce())
            self.assertIn(
                call("%s: Stopped because SSH is disabled" % node.hostname),
                self.maaslog.call_args_list,
            )
        self.assertEquals(
            SCRIPT_STATUS.PASSED, reload_object(passed_script_result).status
        )
        self.assertEquals(
            SCRIPT_STATUS.FAILED, reload_object(failed_script_result).status
        )
        self.assertEquals(
            SCRIPT_STATUS.ABORTED, reload_object(pending_script_result).status
        )
        self.assertEquals(
            SCRIPT_STATUS.TIMEDOUT, reload_object(running_script_result).status
        )

    def test_mark_nodes_failed_after_builtin_commiss_script_overrun(self):
        user = factory.make_admin()
        node = factory.make_Node(status=NODE_STATUS.COMMISSIONING, owner=user)
        script_set = ScriptSet.objects.create_commissioning_script_set(node)
        node.current_commissioning_script_set = script_set
        node.save()
        current_time = now()
        script_set.last_ping = current_time
        script_set.save()
        pending_script_results = list(script_set.scriptresult_set.all())
        passed_script_result = pending_script_results.pop()
        passed_script_result.status = SCRIPT_STATUS.PASSED
        passed_script_result.save()
        failed_script_result = pending_script_results.pop()
        failed_script_result.status = SCRIPT_STATUS.FAILED
        failed_script_result.save()
        running_script_result = pending_script_results.pop()
        running_script_result.status = SCRIPT_STATUS.RUNNING
        running_script_result.started = current_time - timedelta(minutes=10)
        running_script_result.save()

        mark_nodes_failed_after_missing_script_timeout(current_time, 20)
        node = reload_object(node)

        self.assertEquals(NODE_STATUS.FAILED_COMMISSIONING, node.status)
        self.assertEquals(
            "%s has run past it's timeout(%s)"
            % (
                running_script_result.name,
                str(NODE_INFO_SCRIPTS[running_script_result.name]["timeout"]),
            ),
            node.error_description,
        )
        self.assertIn(
            call(
                "%s: %s has run past it's timeout(%s)"
                % (
                    node.hostname,
                    running_script_result.name,
                    str(
                        NODE_INFO_SCRIPTS[running_script_result.name][
                            "timeout"
                        ]
                    ),
                )
            ),
            self.maaslog.call_args_list,
        )
        if node.enable_ssh:
            self.assertThat(self.mock_stop, MockNotCalled())
        else:
            self.assertThat(self.mock_stop, MockCalledOnce())
            self.assertIn(
                call("%s: Stopped because SSH is disabled" % node.hostname),
                self.maaslog.call_args_list,
            )
        self.assertEquals(
            SCRIPT_STATUS.PASSED, reload_object(passed_script_result).status
        )
        self.assertEquals(
            SCRIPT_STATUS.FAILED, reload_object(failed_script_result).status
        )
        self.assertEquals(
            SCRIPT_STATUS.TIMEDOUT, reload_object(running_script_result).status
        )
        for script_result in pending_script_results:
            self.assertEquals(
                SCRIPT_STATUS.ABORTED, reload_object(script_result).status
            )

    def test_uses_param_runtime(self):
        node, script_set = self.make_node()
        current_time = now()
        script_set.last_ping = current_time
        script_set.save()
        passed_script_result = factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.PASSED
        )
        failed_script_result = factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.FAILED
        )
        pending_script_result = factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.PENDING
        )
        script = factory.make_Script(timeout=timedelta(minutes=2))
        running_script_result = factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.RUNNING,
            script=script,
            started=current_time - timedelta(minutes=50),
            parameters={"runtime": {"type": "runtime", "value": 60 * 60}},
        )

        mark_nodes_failed_after_missing_script_timeout(current_time, 20)
        node = reload_object(node)

        self.assertEquals(self.status, node.status)
        self.assertThat(self.mock_stop, MockNotCalled())
        self.assertEquals(
            SCRIPT_STATUS.PASSED, reload_object(passed_script_result).status
        )
        self.assertEquals(
            SCRIPT_STATUS.FAILED, reload_object(failed_script_result).status
        )
        self.assertEquals(
            SCRIPT_STATUS.PENDING, reload_object(pending_script_result).status
        )
        self.assertEquals(
            SCRIPT_STATUS.RUNNING, reload_object(running_script_result).status
        )

    def test_mark_nodes_failed_after_missing_timeout_prefetches(self):
        self.patch(Node, "mark_failed")
        current_time = now()
        node, script_set = self.make_node()
        script_set.last_ping = current_time
        script_set.save()
        script = factory.make_Script(timeout=timedelta(seconds=60))
        factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.RUNNING,
            script=script,
            started=current_time - timedelta(minutes=3),
        )

        counter_one = CountQueries()
        with counter_one:
            mark_nodes_failed_after_missing_script_timeout(current_time, 20)

        nodes = []
        for _ in range(6):
            node, script_set = self.make_node()
            script_set.last_ping = current_time
            script_set.save()
            script = factory.make_Script(timeout=timedelta(seconds=60))
            factory.make_ScriptResult(
                script_set=script_set,
                status=SCRIPT_STATUS.RUNNING,
                script=script,
                started=current_time - timedelta(minutes=3),
            )
            nodes.append(node)

        counter_many = CountQueries()
        with counter_many:
            mark_nodes_failed_after_missing_script_timeout(current_time, 20)

        # Lookup takes 7 queries no matter the amount of Nodes
        # 1. Get all Nodes in commissioning or testing
        # 2. Get all commissioning ScriptSets
        # 3. Get all testing ScriptSets
        # 4. Get all commissioning ScriptResults
        # 5. Get all testing ScriptResults
        # 6. Get all commissioning Scripts
        # 7. Get all testing Scripts
        self.assertEquals(7, counter_one.num_queries)
        self.assertEquals(7, counter_many.num_queries)


class TestStatusMonitorService(MAASServerTestCase):
    def test_init_with_default_interval(self):
        # The service itself calls `check_status` in a thread, via a couple of
        # decorators. This indirection makes it clearer to mock
        # `cleanup_old_nonces` here and track calls to it.
        mock_check_status = self.patch(status_monitor, "check_status")
        # Making `deferToDatabase` use the current thread helps testing.
        self.patch(status_monitor, "deferToDatabase", maybeDeferred)

        service = StatusMonitorService()
        # Use a deterministic clock instead of the reactor for testing.
        service.clock = Clock()

        # The interval is stored as `step` by TimerService,
        # StatusMonitorService's parent class.
        interval = 60  # seconds.
        self.assertEqual(service.step, interval)

        # `check_status` is not called before the service is started.
        self.assertThat(mock_check_status, MockNotCalled())
        # `check_status` is called the moment the service is started.
        service.startService()
        self.assertThat(mock_check_status, MockCalledOnceWith())
        # Advancing the clock by `interval - 1` means that
        # `mark_nodes_failed_after_expiring` has still only been called once.
        service.clock.advance(interval - 1)
        self.assertThat(mock_check_status, MockCalledOnceWith())
        # Advancing the clock one more second causes another call to
        # `check_status`.
        service.clock.advance(1)
        self.assertThat(mock_check_status, MockCallsMatch(call(), call()))

    def test_interval_can_be_set(self):
        interval = self.getUniqueInteger()
        service = StatusMonitorService(interval)
        self.assertEqual(interval, service.step)
