# Copyright 2017-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = []

from datetime import datetime, timedelta
import random
from unittest.mock import MagicMock

from django.core.exceptions import ValidationError
from maasserver.enum import NODE_TYPE
from maasserver.models import Event, EventType
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.orm import reload_object
from maastesting.djangotestcase import CountQueries
from maastesting.matchers import DocTestMatches, MockCalledOnceWith
from metadataserver.builtin_scripts.hooks import NODE_INFO_SCRIPTS
from metadataserver.enum import (
    RESULT_TYPE,
    SCRIPT_STATUS,
    SCRIPT_STATUS_CHOICES,
    SCRIPT_STATUS_RUNNING_OR_PENDING,
    SCRIPT_TYPE,
)
from metadataserver.models import (
    ScriptResult,
    scriptresult as scriptresult_module,
)
from provisioningserver.events import EVENT_TYPES
import yaml


class TestScriptResult(MAASServerTestCase):
    """Test the ScriptResult model."""

    def test__name_returns_script_name(self):
        script_result = factory.make_ScriptResult()
        self.assertEquals(script_result.script.name, script_result.name)

    def test__name_returns_model_script_name_when_no_script(self):
        script_result = factory.make_ScriptResult()
        script_result.script = None
        script_name = factory.make_name("script_name")
        script_result.script_name = script_name
        self.assertEquals(script_name, script_result.name)

    def test__name_returns_unknown_when_no_script_or_model_script_name(self):
        script_result = factory.make_ScriptResult()
        script_result.script = None
        script_result.script_name = None
        self.assertEquals("Unknown", script_result.name)

    def test_store_result_only_allows_status_running(self):
        # XXX ltrager 2016-12-07 - Only allow SCRIPT_STATUS.RUNNING once
        # status tracking is implemented.
        script_result = factory.make_ScriptResult(
            status=factory.pick_choice(
                SCRIPT_STATUS_CHOICES, SCRIPT_STATUS_RUNNING_OR_PENDING
            )
        )
        self.assertRaises(
            AssertionError, script_result.store_result, random.randint(0, 255)
        )

    def test_store_result_allows_controllers_to_overwrite(self):
        node = factory.make_Node(
            node_type=random.choice(
                [
                    NODE_TYPE.REGION_AND_RACK_CONTROLLER,
                    NODE_TYPE.REGION_CONTROLLER,
                    NODE_TYPE.RACK_CONTROLLER,
                ]
            )
        )
        script_set = factory.make_ScriptSet(node=node)
        script_result = factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.PASSED
        )
        exit_status = random.randint(0, 255)
        output = factory.make_bytes()
        stdout = factory.make_bytes()
        stderr = factory.make_bytes()
        result = factory.make_bytes()

        script_result.store_result(
            random.randint(0, 255),
            factory.make_bytes(),
            factory.make_bytes(),
            factory.make_bytes(),
            factory.make_bytes(),
        )
        script_result.store_result(exit_status, output, stdout, stderr, result)

        self.assertEquals(exit_status, script_result.exit_status)
        self.assertEquals(output, script_result.output)
        self.assertEquals(stdout, script_result.stdout)
        self.assertEquals(stderr, script_result.stderr)
        self.assertEquals(result, script_result.result)

    def test_store_result_sets_status_to_timedout_with_timedout_true(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        script_result.store_result(random.randint(0, 255), timedout=True)
        self.assertEquals(SCRIPT_STATUS.TIMEDOUT, script_result.status)

    def test_store_result_sets_status_to_passed_with_exit_code_zero(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        script_result.store_result(0)
        self.assertEquals(SCRIPT_STATUS.PASSED, script_result.status)
        self.assertEquals(0, script_result.exit_status)

    def test_store_result_sets_status_to_failed_with_exit_code_non_zero(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        exit_status = random.randint(1, 255)
        script_result.store_result(exit_status)
        self.assertEquals(SCRIPT_STATUS.FAILED, script_result.status)
        self.assertEquals(exit_status, script_result.exit_status)

    def test_store_result_sets_status_to_install_failed_when_install(self):
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.INSTALLING
        )
        exit_status = random.randint(1, 255)
        script_result.store_result(exit_status)
        self.assertEquals(
            SCRIPT_STATUS.FAILED_INSTALLING, script_result.status
        )
        self.assertEquals(exit_status, script_result.exit_status)

    def test_store_result_sets_status_to_netconf_failed_when_netconf(self):
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.APPLYING_NETCONF
        )
        exit_status = random.randint(1, 255)
        script_result.store_result(exit_status)
        self.assertEquals(
            SCRIPT_STATUS.FAILED_APPLYING_NETCONF, script_result.status
        )
        self.assertEquals(exit_status, script_result.exit_status)

    def test_store_result_stores_output(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        exit_status = random.randint(0, 255)
        output = factory.make_bytes()

        script_result.store_result(exit_status, output=output)

        self.assertEquals(exit_status, script_result.exit_status)
        self.assertEquals(output, script_result.output)

    def test_store_result_stores_stdout(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        exit_status = random.randint(0, 255)
        stdout = factory.make_bytes()

        script_result.store_result(exit_status, stdout=stdout)

        self.assertEquals(exit_status, script_result.exit_status)
        self.assertEquals(stdout, script_result.stdout)

    def test_store_result_stores_stderr(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        exit_status = random.randint(0, 255)
        stderr = factory.make_bytes()

        script_result.store_result(exit_status, stderr=stderr)

        self.assertEquals(exit_status, script_result.exit_status)
        self.assertEquals(stderr, script_result.stderr)

    def test_store_result_stores_result(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        exit_status = random.randint(0, 255)
        result = {
            "results": {
                factory.make_name("key"): factory.make_name("value")
                for _ in range(3)
            }
        }

        script_result.store_result(
            exit_status, result=yaml.safe_dump(result).encode()
        )

        self.assertEquals(exit_status, script_result.exit_status)
        self.assertDictEqual(result, script_result.read_results())

    def test_store_result_logs_invalid_result_yaml(self):
        mock_logger = self.patch(scriptresult_module.logger, "error")
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        result = factory.make_name("invalid").encode()

        script_result.store_result(0, result=result)

        expected_msg = (
            "%s(%s) sent a script result with invalid YAML: "
            "YAML must be a dictionary."
            % (
                script_result.script_set.node.fqdn,
                script_result.script_set.node.system_id,
            )
        )
        event_type = EventType.objects.get(
            name=EVENT_TYPES.SCRIPT_RESULT_ERROR
        )
        event = Event.objects.get(
            node=script_result.script_set.node, type_id=event_type.id
        )
        self.assertEquals(expected_msg, event.description)
        self.assertThat(mock_logger, MockCalledOnceWith(expected_msg))
        self.assertEquals(SCRIPT_STATUS.PASSED, script_result.status)
        self.assertEquals(result, script_result.result)

    def test_store_result_yaml_can_set_script_status(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        status_choices = {
            "passed": SCRIPT_STATUS.PASSED,
            "failed": SCRIPT_STATUS.FAILED,
            "degraded": SCRIPT_STATUS.DEGRADED,
            "timedout": SCRIPT_STATUS.TIMEDOUT,
            "skipped": SCRIPT_STATUS.SKIPPED,
        }
        status = random.choice(list(status_choices.keys()))
        status_yaml = {"status": status}
        exit_status = random.randint(0, 255)

        script_result.store_result(
            exit_status, result=yaml.safe_dump(status_yaml).encode()
        )

        self.assertEquals(status_choices[status], script_result.status)
        self.assertEquals(exit_status, script_result.exit_status)
        self.assertDictEqual(status_yaml, script_result.read_results())

    def test_store_result_yaml_can_set_interface_link_connected(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        script_set = factory.make_ScriptSet(node=node)
        script = factory.make_Script(
            parameters={"interface": {"type": "interface"}}
        )
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.RUNNING,
            script=script,
            script_set=script_set,
            interface=node.boot_interface,
        )

        script_result.store_result(
            0, result=yaml.safe_dump({"link_connected": False}).encode()
        )

        self.assertFalse(reload_object(node.boot_interface).link_connected)

    def test_store_result_stores_script_version(self):
        script = factory.make_Script()
        old_version = script.script
        script.script = script.script.update(factory.make_string())
        script.save()
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.RUNNING, script=script
        )
        exit_status = random.randint(0, 255)

        script_result.store_result(
            exit_status, script_version_id=old_version.id
        )

        self.assertEquals(exit_status, script_result.exit_status)
        self.assertEquals(old_version, script_result.script_version)

    def test_store_result_sets_script_version_to_latest_when_not_given(self):
        script = factory.make_Script()
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.RUNNING, script=script
        )
        exit_status = random.randint(0, 255)

        script_result.store_result(exit_status)

        self.assertEquals(exit_status, script_result.exit_status)
        self.assertEquals(script.script, script_result.script_version)

    def test_store_result_logs_missing_script_version(self):
        mock_logger = self.patch(scriptresult_module.logger, "error")
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.RUNNING)
        exit_status = random.randint(0, 255)

        script_result.store_result(exit_status, script_version_id=-1)

        expected_msg = (
            "%s(%s) sent a script result for %s(%d) with an unknown "
            "script version(-1)."
            % (
                script_result.script_set.node.fqdn,
                script_result.script_set.node.system_id,
                script_result.script.name,
                script_result.script.id,
            )
        )
        event_type = EventType.objects.get(
            name=EVENT_TYPES.SCRIPT_RESULT_ERROR
        )
        event = Event.objects.get(
            node=script_result.script_set.node, type_id=event_type.id
        )
        self.assertEquals(expected_msg, event.description)
        self.assertThat(mock_logger, MockCalledOnceWith(expected_msg))

    def test_store_result_runs_builtin_commissioning_hooks(self):
        script_set = factory.make_ScriptSet(
            result_type=RESULT_TYPE.COMMISSIONING
        )
        script_result = factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.RUNNING
        )
        exit_status = random.randint(0, 255)
        stdout = factory.make_name("stdout").encode()
        mock_hook = MagicMock()
        scriptresult_module.NODE_INFO_SCRIPTS[script_result.name] = {
            "hook": mock_hook
        }
        self.addCleanup(
            scriptresult_module.NODE_INFO_SCRIPTS.pop, script_result.name
        )

        script_result.store_result(exit_status, stdout=stdout)

        self.assertThat(
            mock_hook,
            MockCalledOnceWith(
                node=script_set.node, output=stdout, exit_status=exit_status
            ),
        )

    def test_store_result_logs_event_upon_hook_failure(self):
        script_set = factory.make_ScriptSet(
            result_type=RESULT_TYPE.COMMISSIONING
        )
        script_result = factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.RUNNING
        )
        exit_status = random.randint(0, 255)

        def _raise():
            raise Exception()

        scriptresult_module.NODE_INFO_SCRIPTS[script_result.name] = {
            "hook": _raise
        }
        self.addCleanup(
            scriptresult_module.NODE_INFO_SCRIPTS.pop, script_result.name
        )
        script_result.store_result(exit_status, stdout=b"")
        expected_event = Event.objects.first()
        self.assertThat(
            expected_event.description,
            DocTestMatches("...failed during post-processing."),
        )

    def test_store_result_on_recommission_script_resets_builtin_commiss(self):
        script_set = factory.make_ScriptSet(
            result_type=RESULT_TYPE.COMMISSIONING
        )
        for script_name in NODE_INFO_SCRIPTS.keys():
            factory.make_ScriptResult(
                script_name=script_name,
                script_set=script_set,
                status=SCRIPT_STATUS.PENDING,
            )
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING, recommission=True
        )
        script_result = factory.make_ScriptResult(
            script=script, script_set=script_set, status=SCRIPT_STATUS.PENDING
        )

        script_result.store_result(0)

        for script_result in script_set:
            if script_result.name in NODE_INFO_SCRIPTS:
                self.assertEquals(SCRIPT_STATUS.PENDING, script_result.status)
                self.assertIsNone(script_result.started)
                self.assertIsNone(script_result.ended)
            else:
                self.assertEquals(SCRIPT_STATUS.PASSED, script_result.status)

    def test_store_result_on_recommission_script_failure_does_nothing(self):
        script_set = factory.make_ScriptSet(
            result_type=RESULT_TYPE.COMMISSIONING
        )
        for script_name in NODE_INFO_SCRIPTS.keys():
            factory.make_ScriptResult(
                script_name=script_name,
                script_set=script_set,
                status=SCRIPT_STATUS.PASSED,
            )
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING, recommission=True
        )
        script_result = factory.make_ScriptResult(
            script=script, script_set=script_set, status=SCRIPT_STATUS.PENDING
        )

        script_result.store_result(1)

        for script_result in script_set:
            if script_result.name in NODE_INFO_SCRIPTS:
                self.assertEquals(SCRIPT_STATUS.PASSED, script_result.status)
                self.assertIsNotNone(script_result.started)
                self.assertIsNotNone(script_result.ended)
            else:
                self.assertEquals(SCRIPT_STATUS.FAILED, script_result.status)

    def test_save_stores_start_time(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.PENDING)
        script_result.status = SCRIPT_STATUS.RUNNING
        script_result.save(update_fields=["status"])
        self.assertIsNotNone(reload_object(script_result).started)

    def test_save_stores_end_time(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.PENDING)
        script_result.status = random.choice(
            [
                SCRIPT_STATUS.PASSED,
                SCRIPT_STATUS.FAILED,
                SCRIPT_STATUS.TIMEDOUT,
                SCRIPT_STATUS.ABORTED,
                SCRIPT_STATUS.DEGRADED,
                SCRIPT_STATUS.FAILED_INSTALLING,
                SCRIPT_STATUS.SKIPPED,
                SCRIPT_STATUS.FAILED_APPLYING_NETCONF,
            ]
        )
        script_result.save(update_fields=["status"])
        self.assertIsNotNone(reload_object(script_result).ended)

    def test_save_stores_start_time_with_end_time_when_not_set(self):
        # Regression test for LP:1730799
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.PENDING)
        script_result.started = None
        script_result.status = random.choice(
            [
                SCRIPT_STATUS.PASSED,
                SCRIPT_STATUS.FAILED,
                SCRIPT_STATUS.TIMEDOUT,
                SCRIPT_STATUS.ABORTED,
                SCRIPT_STATUS.DEGRADED,
                SCRIPT_STATUS.FAILED_INSTALLING,
                SCRIPT_STATUS.SKIPPED,
            ]
        )
        script_result.save(update_fields=["status"])
        script_result = reload_object(script_result)
        self.assertIsNotNone(script_result.started)
        self.assertIsNotNone(script_result.ended)
        self.assertEquals(script_result.started, script_result.ended)

    def test_save_sets_physical_blockdevice_from_parameters(self):
        node = factory.make_Machine()
        script_set = factory.make_ScriptSet(node=node)
        physical_blockdevice = node.physicalblockdevice_set.first()
        script_result = factory.make_ScriptResult(
            script_set=script_set,
            parameters={
                "storage": {
                    "type": "storage",
                    "value": {
                        "name": physical_blockdevice.name,
                        "id_path": physical_blockdevice.id_path,
                        "model": physical_blockdevice.model,
                        "serial": physical_blockdevice.serial,
                        "physical_blockdevice": physical_blockdevice,
                    },
                }
            },
        )
        self.assertEquals(
            physical_blockdevice, script_result.physical_blockdevice
        )
        self.assertDictEqual(
            {
                "storage": {
                    "type": "storage",
                    "value": {
                        "name": physical_blockdevice.name,
                        "id_path": physical_blockdevice.id_path,
                        "model": physical_blockdevice.model,
                        "serial": physical_blockdevice.serial,
                        "physical_blockdevice_id": physical_blockdevice.id,
                    },
                }
            },
            script_result.parameters,
        )

    def test_save_sets_interface_from_parameters(self):
        node = factory.make_Machine()
        script_set = factory.make_ScriptSet(node=node)
        interface = factory.make_Interface(node=node)
        script_result = factory.make_ScriptResult(
            script_set=script_set,
            parameters={
                "interface": {
                    "type": "interface",
                    "value": {
                        "name": interface.name,
                        "mac_address": str(interface.mac_address),
                        "vendor": interface.vendor,
                        "product": interface.product,
                        "interface": interface,
                    },
                }
            },
        )
        self.assertEquals(interface, script_result.interface)
        self.assertIsNone(script_result.physical_blockdevice)
        self.assertDictEqual(
            {
                "interface": {
                    "type": "interface",
                    "value": {
                        "name": interface.name,
                        "mac_address": str(interface.mac_address),
                        "vendor": interface.vendor,
                        "product": interface.product,
                        "interface_id": interface.id,
                    },
                }
            },
            script_result.parameters,
        )

    def test_get_runtime(self):
        runtime_seconds = random.randint(1, 59)
        now = datetime.now()
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.PASSED,
            started=now - timedelta(seconds=runtime_seconds),
            ended=now,
        )
        if runtime_seconds < 10:
            text_seconds = "0%d" % runtime_seconds
        else:
            text_seconds = "%d" % runtime_seconds
        self.assertEquals("0:00:%s" % text_seconds, script_result.runtime)

    def test_get_runtime_blank_when_missing(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.PENDING)
        self.assertEquals("", script_result.runtime)

    def test_get_starttime(self):
        now = datetime.now()
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.PASSED, started=now, ended=now
        )
        self.assertEquals(now.timestamp(), script_result.starttime)

    def test_get_starttime_None(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.PENDING)
        self.assertEquals("", script_result.starttime)

    def test_get_endtime(self):
        now = datetime.now()
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.PASSED, started=now, ended=now
        )
        self.assertEquals(now.timestamp(), script_result.endtime)

    def test_get_endtime_None(self):
        script_result = factory.make_ScriptResult(status=SCRIPT_STATUS.PENDING)
        self.assertEquals("", script_result.endtime)

    def test_estimated_runtime_returns_set_runtime(self):
        now = datetime.now()
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.PENDING,
            started=now,
            ended=(now + factory.make_timedelta()),
        )
        self.assertEquals(
            script_result.runtime, script_result.estimated_runtime
        )

    def test_estimated_runtime_returns_average_of_previous(self):
        script = factory.make_Script()
        script_set = factory.make_ScriptSet()
        old_results = [
            factory.make_ScriptResult(
                status=SCRIPT_STATUS.PASSED,
                script=script,
                script_set=script_set,
            )
            for _ in range(10)
        ]
        factory.make_ScriptResult(
            status=SCRIPT_STATUS.FAILED, script=script, script_set=script_set
        )
        average_runtime = old_results[9].ended - old_results[9].started
        for result in reversed(old_results[:-1]):
            average_runtime += result.ended - result.started
            average_runtime = average_runtime / 2
        now = datetime.now()
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.RUNNING,
            started=now,
            script=script,
            script_set=script_set,
        )
        expected = str(
            average_runtime
            - timedelta(microseconds=average_runtime.microseconds)
        )
        self.assertEquals(expected, script_result.estimated_runtime)

    def test_estimated_runtime_sets_start_time_when_unavailable(self):
        # Regression test for LP:1730799
        script = factory.make_Script()
        script_set = factory.make_ScriptSet()
        no_started_result = factory.make_ScriptResult(
            script=script, script_set=script_set
        )
        no_started_result.status = SCRIPT_STATUS.PASSED
        no_started_result.started = None
        no_started_result.save()
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.RUNNING,
            started=datetime.now(),
            script=script,
            script_set=script_set,
        )
        self.assertEquals("0:00:00", script_result.estimated_runtime)

    def test_estimated_runtime_uses_timeout(self):
        now = datetime.now()
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.RUNNING, started=now
        )
        expected = str(
            script_result.script.timeout
            - timedelta(microseconds=script_result.script.timeout.microseconds)
        )
        self.assertEquals(expected, script_result.estimated_runtime)

    def test_estimated_runtime_returns_Unknown(self):
        now = datetime.now()
        script_result = factory.make_ScriptResult(
            status=SCRIPT_STATUS.RUNNING, started=now
        )
        script_result.script.timeout = timedelta(0)
        script_result.script.save()
        self.assertEquals("Unknown", script_result.estimated_runtime)

    def test_read_results(self):
        results = {
            "status": random.choice(
                ["passed", "failed", "degraded", "timedout", "skipped"]
            ),
            "results": {
                factory.make_name("key"): factory.make_name("value"),
                factory.make_name("key"): [
                    factory.make_name("value"),
                    random.uniform(1, 2),
                    random.randint(0, 1000),
                    factory.pick_bool(),
                ],
            },
        }
        script_result = factory.make_ScriptResult(
            result=yaml.safe_dump(results).encode()
        )

        self.assertDictEqual(results, script_result.read_results())

    def test_read_results_ignores_empty(self):
        script_result = factory.make_ScriptResult(result=b"")
        self.assertDictEqual({}, script_result.read_results())

    def test_read_results_does_not_require_results(self):
        result = {
            "status": random.choice(
                ["passed", "failed", "degraded", "timedout", "skipped"]
            )
        }
        script_result = factory.make_ScriptResult(
            result=yaml.safe_dump(result).encode()
        )
        self.assertDictEqual(result, script_result.read_results())

    def test_read_results_errors_when_invalid_yaml(self):
        script_result = factory.make_ScriptResult(result=b"{")

        self.assertRaises(ValidationError, script_result.read_results)

    def test_read_results_errors_when_not_dict(self):
        script_result = factory.make_ScriptResult(
            result=factory.make_name("invalid").encode()
        )
        with self.assertRaisesRegex(
            ValidationError, "YAML must be a dictionary."
        ):
            script_result.read_results()

    def test_read_results_errors_with_invalid_status(self):
        result = {"status": factory.make_name("status")}
        script_result = factory.make_ScriptResult(
            result=yaml.safe_dump(result).encode()
        )
        with self.assertRaisesRegex(
            ValidationError,
            'status must be "passed", "failed", "degraded", '
            '"timedout", or "skipped".',
        ):
            script_result.read_results()

    def test_read_results_errors_with_link_connected_and_no_interface(self):
        result = {"link_connected": factory.pick_bool()}
        script_result = factory.make_ScriptResult(
            result=yaml.safe_dump(result).encode()
        )
        with self.assertRaisesRegex(
            ValidationError,
            "link_connected may only be specified if the Script accepts "
            "an interface parameter.",
        ):
            script_result.read_results()

    def test_read_results_errors_with_link_connected_non_boolean(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        script_set = factory.make_ScriptSet(node=node)
        script = factory.make_Script(
            parameters={"interface": {"type": "interface"}}
        )
        result = {"link_connected": factory.make_name("link_connected")}
        script_result = factory.make_ScriptResult(
            script=script,
            script_set=script_set,
            interface=node.boot_interface,
            result=yaml.safe_dump(result).encode(),
        )
        with self.assertRaisesRegex(
            ValidationError, "link_connected must be a boolean"
        ):
            script_result.read_results()

    def test_read_results_errors_when_dict_keys_not_str(self):
        result = {
            "results": {random.randint(0, 1000): factory.make_name("value")}
        }
        script_result = factory.make_ScriptResult(
            result=yaml.safe_dump(result).encode()
        )
        with self.assertRaisesRegex(
            ValidationError,
            "All keys in the results dictionary must be strings.",
        ):
            script_result.read_results()

    def test_read_results_errors_when_dict_values_invalid(self):
        result = {"results": {factory.make_name("key"): {}}}
        script_result = factory.make_ScriptResult(
            result=yaml.safe_dump(result).encode()
        )
        with self.assertRaisesRegex(
            ValidationError,
            "All values in the results dictionary must be a string, "
            "float, int, or bool.",
        ):
            script_result.read_results()

    def test_read_results_requires_results_to_be_a_dict(self):
        result = {"results": factory.make_name("invalid")}
        script_result = factory.make_ScriptResult(
            result=yaml.safe_dump(result).encode()
        )
        with self.assertRaisesRegex(
            ValidationError, "results must be a dictionary."
        ):
            script_result.read_results()

    def test_history_with_script(self):
        script = factory.make_Script()
        script_set = factory.make_ScriptSet()
        script_results = [
            factory.make_ScriptResult(script=script, script_set=script_set)
            for _ in range(10)
        ]
        # Script result from another node
        factory.make_ScriptResult(script=script)
        script_result = script_results[-1]
        self.assertItemsEqual(script_results, script_result.history)

    def test_history_without_script(self):
        script_name = factory.make_name("script_name")
        script_set = factory.make_ScriptSet()
        script_results = [
            factory.make_ScriptResult(
                script_name=script_name, script_set=script_set
            )
            for _ in range(10)
        ]
        # Script result from another node
        factory.make_ScriptResult(script_name=script_name)
        script_result = script_results[-1]
        self.assertItemsEqual(script_results, script_result.history)

    def test_history_query_count(self):
        script_name = factory.make_name("script_name")
        script_set = factory.make_ScriptSet()
        script_results = [
            factory.make_ScriptResult(
                script_name=script_name, script_set=script_set
            )
            for _ in range(10)
        ]
        queries_one = CountQueries()
        script_result_one = ScriptResult.objects.get(id=script_results[0].id)
        with queries_one:
            script_result_one.history
        queries_many = CountQueries()
        script_result_many = ScriptResult.objects.get(id=script_results[-1].id)
        with queries_many:
            script_result_many.history
        self.assertEquals(1, queries_one.num_queries)
        self.assertEquals(1, queries_many.num_queries)

    def test_history_storage_device(self):
        # Regression test for LP: #1721524
        script = factory.make_Script()
        script_set = factory.make_ScriptSet()
        physical_blockdevice = factory.make_PhysicalBlockDevice()
        # Scripts without associated block devices
        script_results = [
            factory.make_ScriptResult(script=script, script_set=script_set)
            for _ in range(10)
        ]
        # Scripts with associated block devices
        script_results += [
            factory.make_ScriptResult(
                script=script,
                script_set=script_set,
                physical_blockdevice=physical_blockdevice,
            )
            for _ in range(10)
        ]
        # Script from another node
        factory.make_ScriptResult(script=script)
        script_result = script_results[-1]
        self.assertItemsEqual(script_results, script_result.history)

    def test_not_suppressed_by_default(self):
        script_result = factory.make_ScriptResult()
        self.assertFalse(script_result.suppressed)

    def test_suppressed(self):
        script_result = factory.make_ScriptResult(suppressed=True)
        self.assertTrue(script_result.suppressed)
