# Copyright 2017-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = []

from datetime import datetime, timedelta
import random
from unittest import mock
from unittest.mock import PropertyMock

from django.core.exceptions import ValidationError
from maasserver.enum import NODE_TYPE
from maasserver.exceptions import NoScriptsFound
from maasserver.models import Config, Event, EventType, Node
from maasserver.preseed import CURTIN_INSTALL_LOG
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.orm import reload_object
from maastesting.matchers import MockCalledOnceWith
from metadataserver.enum import (
    RESULT_TYPE,
    SCRIPT_STATUS,
    SCRIPT_STATUS_RUNNING_OR_PENDING,
    SCRIPT_TYPE,
)
from metadataserver.models import (
    ScriptResult,
    ScriptSet,
    scriptset as scriptset_module,
)
from metadataserver.models.scriptset import translate_result_type
from provisioningserver.events import EVENT_TYPES
from provisioningserver.refresh.node_info_scripts import NODE_INFO_SCRIPTS


class TestTranslateResultType(MAASServerTestCase):
    """Test translate_result_type."""

    scenarios = [
        (
            "numeric testing",
            {
                "value": RESULT_TYPE.TESTING,
                "return_value": RESULT_TYPE.TESTING,
            },
        ),
        (
            "numeric commissioning",
            {
                "value": RESULT_TYPE.COMMISSIONING,
                "return_value": RESULT_TYPE.COMMISSIONING,
            },
        ),
        (
            "numeric installation",
            {
                "value": RESULT_TYPE.INSTALLATION,
                "return_value": RESULT_TYPE.INSTALLATION,
            },
        ),
        (
            "numeric string testing",
            {
                "value": str(RESULT_TYPE.TESTING),
                "return_value": RESULT_TYPE.TESTING,
            },
        ),
        (
            "numeric string commissioning",
            {
                "value": str(RESULT_TYPE.COMMISSIONING),
                "return_value": RESULT_TYPE.COMMISSIONING,
            },
        ),
        (
            "numeric string installation",
            {
                "value": str(RESULT_TYPE.INSTALLATION),
                "return_value": RESULT_TYPE.INSTALLATION,
            },
        ),
        (
            "invalid id",
            {
                "value": random.randint(100, 1000),
                "exception": "Invalid result type numeric value.",
            },
        ),
        ("test", {"value": "test", "return_value": RESULT_TYPE.TESTING}),
        ("testing", {"value": "testing", "return_value": RESULT_TYPE.TESTING}),
        (
            "commission",
            {"value": "commission", "return_value": RESULT_TYPE.COMMISSIONING},
        ),
        (
            "commissioning",
            {
                "value": "commissioning",
                "return_value": RESULT_TYPE.COMMISSIONING,
            },
        ),
        (
            "install",
            {"value": "install", "return_value": RESULT_TYPE.INSTALLATION},
        ),
        (
            "installation",
            {
                "value": "installation",
                "return_value": RESULT_TYPE.INSTALLATION,
            },
        ),
        (
            "invalid value",
            {
                "value": factory.make_name("value"),
                "exception": "Result type must be commissioning, testing, or installation.",
            },
        ),
    ]

    def test_translate_result_type(self):
        if hasattr(self, "exception"):
            with self.assertRaisesRegex(ValidationError, self.exception):
                translate_result_type(self.value)
        else:
            self.assertEquals(
                self.return_value, translate_result_type(self.value)
            )


class TestScriptSetManager(MAASServerTestCase):
    """Test the ScriptSet manager."""

    def test_create_commissioning_script_set(self):
        custom_scripts = [
            factory.make_Script(script_type=SCRIPT_TYPE.COMMISSIONING)
            for _ in range(3)
        ]
        node = factory.make_Node()

        script_set = ScriptSet.objects.create_commissioning_script_set(node)

        expected_scripts = list(NODE_INFO_SCRIPTS)
        expected_scripts += [script.name for script in custom_scripts]
        self.assertItemsEqual(
            expected_scripts,
            [script_result.name for script_result in script_set],
        )
        self.assertEquals(RESULT_TYPE.COMMISSIONING, script_set.result_type)
        self.assertEquals(
            node.power_state, script_set.power_state_before_transition
        )

    def test_create_commissioning_script_set_for_controller(self):
        for _ in range(3):
            factory.make_Script(script_type=SCRIPT_TYPE.COMMISSIONING)
        node = factory.make_Node(
            node_type=random.choice(
                [
                    NODE_TYPE.RACK_CONTROLLER,
                    NODE_TYPE.REGION_CONTROLLER,
                    NODE_TYPE.REGION_AND_RACK_CONTROLLER,
                ]
            )
        )

        script_set = ScriptSet.objects.create_commissioning_script_set(node)

        expected_scripts = [
            script_name
            for script_name, data in NODE_INFO_SCRIPTS.items()
            if data["run_on_controller"]
        ]
        self.assertItemsEqual(
            expected_scripts,
            [script_result.name for script_result in script_set],
        )
        self.assertEquals(RESULT_TYPE.COMMISSIONING, script_set.result_type)
        self.assertEquals(
            node.power_state, script_set.power_state_before_transition
        )

    def test_create_commissioning_script_set_adds_all_user_scripts(self):
        script = factory.make_Script(script_type=SCRIPT_TYPE.COMMISSIONING)
        node = factory.make_Node()
        expected_scripts = list(NODE_INFO_SCRIPTS)
        expected_scripts.append(script.name)

        script_set = ScriptSet.objects.create_commissioning_script_set(node)

        self.assertItemsEqual(
            expected_scripts,
            [script_result.name for script_result in script_set],
        )
        self.assertEquals(RESULT_TYPE.COMMISSIONING, script_set.result_type)
        self.assertEquals(
            node.power_state, script_set.power_state_before_transition
        )

    def test_create_commissioning_script_set_adds_for_hardware_matches_tag(
        self,
    ):
        node = factory.make_Node()
        system_vendor = factory.make_NodeMetadata(
            node=node, key="system_vendor"
        )
        system_product = factory.make_NodeMetadata(
            node=node, key="system_product"
        )
        system_version = factory.make_NodeMetadata(
            node=node, key="system_version"
        )
        mainboard_vendor = factory.make_NodeMetadata(
            node=node, key="mainboard_vendor"
        )
        mainboard_product = factory.make_NodeMetadata(
            node=node, key="mainboard_product"
        )
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING,
            for_hardware=[
                random.choice(
                    [
                        "usb:174c:07d1",
                        "pci:8086:1918",
                        "modalias:pci:v00001A03d00001150sv000015D9*",
                        "system_vendor:%s" % system_vendor.value,
                        "system_product:%s" % system_product.value,
                        "system_version:%s" % system_version.value,
                        "mainboard_vendor:%s" % mainboard_vendor.value,
                        "mainboard_product:%s" % mainboard_product.value,
                    ]
                )
            ],
        )
        to_mock = (
            node.__module__ + "." + node.__class__.__qualname__ + ".modaliases"
        )
        with mock.patch(to_mock, new_callable=PropertyMock) as mods:
            mods.return_value = [
                "pci:v00008086d00001918sv000015D9sd00000888bc06sc00i00",
                "usb:v174Cp07D1d1000dc00dsc00dp00ic08isc06ip50in00",
                "pci:v00001A03d00001150sv000015D9sd00000888bc06sc04i00",
            ]
            expected_scripts = list(NODE_INFO_SCRIPTS)
            expected_scripts.append(script.name)

            script_set = ScriptSet.objects.create_commissioning_script_set(
                node, script.tags
            )

            self.assertItemsEqual(
                expected_scripts,
                [script_result.name for script_result in script_set],
            )
            self.assertEquals(
                RESULT_TYPE.COMMISSIONING, script_set.result_type
            )
            self.assertEquals(
                node.power_state, script_set.power_state_before_transition
            )

    def test_create_commissioning_scripts_with_for_hardware_ignores_wo_tag(
        self,
    ):
        node = factory.make_Node()
        system_vendor = factory.make_NodeMetadata(
            node=node, key="system_vendor"
        )
        system_product = factory.make_NodeMetadata(
            node=node, key="system_product"
        )
        system_version = factory.make_NodeMetadata(
            node=node, key="system_version"
        )
        mainboard_vendor = factory.make_NodeMetadata(
            node=node, key="mainboard_vendor"
        )
        mainboard_product = factory.make_NodeMetadata(
            node=node, key="mainboard_product"
        )
        factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING,
            for_hardware=[
                random.choice(
                    [
                        "usb:174c:07d1",
                        "pci:8086:1918",
                        "modalias:pci:v00001A03d00001150sv000015D9*",
                        "system_vendor:%s" % system_vendor.value,
                        "system_product:%s" % system_product.value,
                        "system_version:%s" % system_version.value,
                        "mainboard_vendor:%s" % mainboard_vendor.value,
                        "mainboard_product:%s" % mainboard_product.value,
                    ]
                )
            ],
        )
        to_mock = (
            node.__module__ + "." + node.__class__.__qualname__ + ".modaliases"
        )
        with mock.patch(to_mock, new_callable=PropertyMock) as mods:
            mods.return_value = [
                "pci:v00008086d00001918sv000015D9sd00000888bc06sc00i00",
                "usb:v174Cp07D1d1000dc00dsc00dp00ic08isc06ip50in00",
                "pci:v00001A03d00001150sv000015D9sd00000888bc06sc04i00",
            ]
            expected_scripts = list(NODE_INFO_SCRIPTS)

            other_script = factory.make_Script(
                script_type=SCRIPT_TYPE.COMMISSIONING
            )
            expected_scripts.append(other_script.name)
            script_set = ScriptSet.objects.create_commissioning_script_set(
                node, [other_script.name]
            )

            self.assertItemsEqual(
                expected_scripts,
                [script_result.name for script_result in script_set],
            )
            self.assertEquals(
                RESULT_TYPE.COMMISSIONING, script_set.result_type
            )
            self.assertEquals(
                node.power_state, script_set.power_state_before_transition
            )

    def test_create_commissioning_script_set_skips_non_matching_for_hardware(
        self,
    ):
        factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING,
            for_hardware=[
                random.choice(
                    [
                        "usb:174c:07d1",
                        "pci:8086:1918",
                        "modalias:pci:v00001A03d00001150sv000015D9*",
                    ]
                )
            ],
        )
        node = factory.make_Node()
        expected_scripts = list(NODE_INFO_SCRIPTS)

        script_set = ScriptSet.objects.create_commissioning_script_set(node)

        self.assertItemsEqual(
            expected_scripts,
            [script_result.name for script_result in script_set],
        )
        self.assertEquals(RESULT_TYPE.COMMISSIONING, script_set.result_type)
        self.assertEquals(
            node.power_state, script_set.power_state_before_transition
        )

    def test_create_commissioning_script_set_adds_selected_scripts(self):
        scripts = [
            factory.make_Script(script_type=SCRIPT_TYPE.COMMISSIONING)
            for _ in range(10)
        ]
        node = factory.make_Node()
        script_selected_by_tag = random.choice(scripts)
        script_selected_by_name = random.choice(scripts)
        script_selected_by_id = random.choice(scripts)
        expected_scripts = list(NODE_INFO_SCRIPTS)
        expected_scripts.append(script_selected_by_tag.name)
        expected_scripts.append(script_selected_by_name.name)
        expected_scripts.append(script_selected_by_id.name)

        script_set = ScriptSet.objects.create_commissioning_script_set(
            node,
            scripts=[
                random.choice(
                    [
                        tag
                        for tag in script_selected_by_tag.tags
                        if "tag" in tag
                    ]
                ),
                script_selected_by_name.name,
                script_selected_by_id.id,
            ],
        )
        self.assertItemsEqual(
            set(expected_scripts),
            [script_result.name for script_result in script_set],
        )
        self.assertEquals(RESULT_TYPE.COMMISSIONING, script_set.result_type)
        self.assertEquals(
            node.power_state, script_set.power_state_before_transition
        )

    def test_create_commissioning_script_set_cleans_up_past_limit(self):
        limit = Config.objects.get_config("max_node_commissioning_results")
        node = factory.make_Node()
        for i in range(limit + 2):
            ScriptSet.objects.create_commissioning_script_set(node)

        for script_name in NODE_INFO_SCRIPTS:
            self.assertEqual(
                limit,
                ScriptResult.objects.filter(script_name=script_name).count(),
            )

    def test_create_commissioning_script_set_cleans_up_by_node(self):
        limit = Config.objects.get_config("max_node_commissioning_results")
        node1 = factory.make_Node()
        node2 = factory.make_Node()
        for i in range(limit + 2):
            ScriptSet.objects.create_commissioning_script_set(node1)
            ScriptSet.objects.create_commissioning_script_set(node2)

        for script_name in NODE_INFO_SCRIPTS:
            self.assertEqual(
                limit,
                ScriptResult.objects.filter(
                    script_name=script_name,
                    script_set__in=ScriptSet.objects.filter(node=node1),
                ).count(),
            )
            self.assertEqual(
                limit,
                ScriptResult.objects.filter(
                    script_name=script_name,
                    script_set__in=ScriptSet.objects.filter(node=node2),
                ).count(),
            )

    def test_create_commissioning_script_set_cleans_up_current(self):
        Config.objects.set_config("max_node_commissioning_results", 1)
        node = factory.make_Node()

        node.current_commissioning_script_set = ScriptSet.objects.create_commissioning_script_set(
            node
        )
        node.save()

        ScriptSet.objects.create_commissioning_script_set(node)

        for script_name in NODE_INFO_SCRIPTS:
            self.assertEqual(
                1, ScriptResult.objects.filter(script_name=script_name).count()
            )

    def test_create_commissioning_script_set_only_cleans_same_type(self):
        # Regression test for LP:1751946
        node = Node.objects.create()
        script_set = ScriptSet.objects.create_commissioning_script_set(
            node=node
        )
        script_set.scriptresult_set.update(status=SCRIPT_STATUS.ABORTED)
        node.current_commissioning_script_set = script_set
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.TESTING,
            parameters={"storage": {"type": "storage"}},
        )
        testing_script_set = ScriptSet.objects.create_testing_script_set(
            node=node, scripts=[script.id]
        )
        testing_script_set.scriptresult_set.update(
            status=SCRIPT_STATUS.ABORTED
        )
        node.current_testing_script_set = testing_script_set
        node.save()
        new_script_set = ScriptSet.objects.create_commissioning_script_set(
            node=node
        )
        node.current_commissioning_script_set = new_script_set
        node.save()
        self.assertIsNotNone(ScriptSet.objects.get(id=testing_script_set.id))

    def test_create_commissioning_script_set_removes_previous_placeholder(
        self,
    ):
        # Regression test for LP:1731075
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING,
            parameters={"storage": {"type": "storage"}},
        )
        node = factory.make_Node(with_boot_disk=False)
        previous_script_set = factory.make_ScriptSet(
            node=node, result_type=RESULT_TYPE.COMMISSIONING
        )
        previous_script_result = factory.make_ScriptResult(
            script_set=previous_script_set, status=SCRIPT_STATUS.PENDING
        )
        previous_script_result.parameters = {
            "storage": {"type": "storage", "value": "all"}
        }
        previous_script_result.save()

        script_set = ScriptSet.objects.create_commissioning_script_set(
            node, scripts=[script.name]
        )
        script_result = script_set.scriptresult_set.get(script=script)

        # Verify the new place holder ScriptResult was created
        self.assertIsNotNone(reload_object(script_set))
        self.assertDictEqual(
            {"storage": {"type": "storage", "value": "all"}},
            script_result.parameters,
        )
        # Verify the old place holder ScriptResult was deleted
        self.assertIsNone(reload_object(previous_script_set))
        self.assertIsNone(reload_object(previous_script_result))

    def test_create_commissioning_script_set_cleans_up_empty_sets(self):
        Config.objects.set_config("max_node_commissioning_results", 1)
        node = factory.make_Node()
        ScriptSet.objects.create_commissioning_script_set(node)
        script_set = ScriptSet.objects.create_commissioning_script_set(node)

        # the first set is removed since it's empty
        self.assertCountEqual(
            [script_set],
            ScriptSet.objects.filter(
                result_type=RESULT_TYPE.COMMISSIONING
            ).all(),
        )

    def test_create_commissioning_script_set_cleans_up_per_node(self):
        Config.objects.set_config("max_node_commissioning_results", 1)
        node1 = factory.make_Node()
        node2 = factory.make_Node()
        ScriptSet.objects.create_commissioning_script_set(node1)
        script_set1 = ScriptSet.objects.create_commissioning_script_set(node1)
        ScriptSet.objects.create_commissioning_script_set(node2)
        script_set2 = ScriptSet.objects.create_commissioning_script_set(node2)

        # older sets for each node are removed
        self.assertCountEqual(
            [script_set1, script_set2],
            ScriptSet.objects.filter(
                result_type=RESULT_TYPE.COMMISSIONING
            ).all(),
        )

    def test_create_commissioning_script_set_aborts_old_running_scripts(self):
        node = factory.make_Node()
        script = factory.make_Script(script_type=SCRIPT_TYPE.COMMISSIONING)
        # Generate old ScriptResults
        for _ in range(3):
            script_set = factory.make_ScriptSet(
                node=node, result_type=RESULT_TYPE.COMMISSIONING
            )
            for status in SCRIPT_STATUS_RUNNING_OR_PENDING:
                factory.make_ScriptResult(script_set=script_set, status=status)

        script_set = ScriptSet.objects.create_commissioning_script_set(
            node, scripts=[script.name]
        )

        for script_result in ScriptResult.objects.all():
            if script_result.script_set == script_set:
                self.assertEqual(SCRIPT_STATUS.PENDING, script_result.status)
            else:
                self.assertEqual(SCRIPT_STATUS.ABORTED, script_result.status)

    def test_create_commissioning_script_set_accepts_params(self):
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING,
            parameters={"storage": {"type": "storage"}},
        )
        node = factory.make_Node()
        for _ in range(3):
            factory.make_PhysicalBlockDevice(node=node)

        script_set = ScriptSet.objects.create_commissioning_script_set(
            node, [script.name], {script.name: {"storage": "all"}}
        )

        self.assertItemsEqual(
            [bd.name for bd in node.physicalblockdevice_set],
            [
                script_result.parameters["storage"]["value"]["name"]
                for script_result in script_set
                if script_result.script == script
            ],
        )

    def test_create_commissioning_script_set_errors_params(self):
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING,
            parameters={"storage": {"type": "storage"}},
        )
        node = factory.make_Node()

        self.assertRaises(
            ValidationError,
            ScriptSet.objects.create_commissioning_script_set,
            node,
            [script.name],
            {script.name: factory.make_name("unknown")},
        )
        self.assertFalse(ScriptSet.objects.all().exists())

    def test_create_testing_script_set(self):
        node = factory.make_Node()
        expected_scripts = [
            factory.make_Script(
                script_type=SCRIPT_TYPE.TESTING, tags=["commissioning"]
            ).name
            for _ in range(3)
        ]

        script_set = ScriptSet.objects.create_testing_script_set(node)

        self.assertItemsEqual(
            expected_scripts,
            [script_result.name for script_result in script_set],
        )
        self.assertEquals(RESULT_TYPE.TESTING, script_set.result_type)
        self.assertEquals(
            node.power_state, script_set.power_state_before_transition
        )

    def test_create_testing_script_set_adds_selected_scripts(self):
        scripts = [
            factory.make_Script(script_type=SCRIPT_TYPE.TESTING)
            for _ in range(10)
        ]
        script_selected_by_tag = random.choice(scripts)
        script_selected_by_name = random.choice(scripts)
        script_selected_by_id = random.choice(scripts)
        node = factory.make_Node()
        expected_scripts = [
            script_selected_by_tag.name,
            script_selected_by_name.name,
            script_selected_by_id.name,
        ]

        script_set = ScriptSet.objects.create_testing_script_set(
            node,
            scripts=[
                random.choice(
                    [
                        tag
                        for tag in script_selected_by_tag.tags
                        if "tag" in tag
                    ]
                ),
                script_selected_by_name.name,
                script_selected_by_id.id,
            ],
        )

        self.assertItemsEqual(
            set(expected_scripts),
            [script_result.name for script_result in script_set],
        )
        self.assertEquals(RESULT_TYPE.TESTING, script_set.result_type)
        self.assertEquals(
            node.power_state, script_set.power_state_before_transition
        )

    def test_create_testing_script_raises_exception_when_none_found(self):
        node = factory.make_Node()
        self.assertRaises(
            NoScriptsFound, ScriptSet.objects.create_testing_script_set, node
        )

    def test_create_testing_script_set_cleans_up_past_limit(self):
        limit = Config.objects.get_config("max_node_testing_results")
        node = factory.make_Node()
        script = factory.make_Script(script_type=SCRIPT_TYPE.TESTING)
        for _ in range(limit + 2):
            ScriptSet.objects.create_testing_script_set(
                node, scripts=[script.name]
            )
        self.assertEqual(
            limit, ScriptResult.objects.filter(script_name=script.name).count()
        )

    def test_create_testing_script_set_cleans_up_by_node(self):
        limit = Config.objects.get_config("max_node_testing_results")
        node1 = factory.make_Node()
        node2 = factory.make_Node()
        script = factory.make_Script(script_type=SCRIPT_TYPE.TESTING)
        for _ in range(limit + 2):
            ScriptSet.objects.create_testing_script_set(
                node1, scripts=[script.name]
            )
            ScriptSet.objects.create_testing_script_set(
                node2, scripts=[script.name]
            )

        self.assertEqual(
            limit,
            ScriptResult.objects.filter(
                script_set__in=ScriptSet.objects.filter(node=node1)
            ).count(),
        )
        self.assertEqual(
            limit,
            ScriptResult.objects.filter(
                script_set__in=ScriptSet.objects.filter(node=node2)
            ).count(),
        )

    def test_create_testing_script_set_cleans_up_by_blockdevice(self):
        Config.objects.set_config("max_node_testing_results", 1)
        node = factory.make_Node()
        for _ in range(2):
            factory.make_PhysicalBlockDevice(node=node)

        script = factory.make_Script(
            script_type=SCRIPT_TYPE.TESTING,
            parameters={"storage": {"type": "storage"}},
        )
        ScriptSet.objects.create_testing_script_set(
            node, [script.name], {script.name: {"storage": "all"}}
        )
        ScriptSet.objects.create_testing_script_set(
            node, [script.name], {script.name: {"storage": "all"}}
        )

        # one result is kept for each block device
        self.assertEqual(
            3,
            ScriptResult.objects.filter(
                script_set__in=ScriptSet.objects.filter(node=node)
            ).count(),
        )

    def test_create_testing_script_set_cleans_up_current(self):
        Config.objects.set_config("max_node_testing_results", 1)
        script = factory.make_Script(script_type=SCRIPT_TYPE.TESTING)
        node = factory.make_Node()
        node.current_testing_script_set = ScriptSet.objects.create_testing_script_set(
            node, scripts=[script.name]
        )
        node.save()

        ScriptSet.objects.create_testing_script_set(
            node, scripts=[script.name]
        )

        self.assertEqual(
            1, ScriptResult.objects.filter(script_name=script.name).count()
        )

    def test_create_testing_script_set_removes_previous_placeholder(self):
        # Regression test for LP:1731075
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.TESTING,
            parameters={"storage": {"type": "storage"}},
        )
        node = factory.make_Node(with_boot_disk=False)
        previous_script_set = factory.make_ScriptSet(
            node=node, result_type=RESULT_TYPE.TESTING
        )
        previous_script_result = factory.make_ScriptResult(
            script_set=previous_script_set, status=SCRIPT_STATUS.PENDING
        )
        previous_script_result.parameters = {
            "storage": {"type": "storage", "value": "all"}
        }
        previous_script_result.save()

        script_set = ScriptSet.objects.create_testing_script_set(
            node, scripts=[script.name]
        )
        script_result = script_set.scriptresult_set.get(script=script)

        # Verify the new place holder ScriptResult was created
        self.assertIsNotNone(reload_object(script_set))
        self.assertDictEqual(
            {"storage": {"type": "storage", "value": "all"}},
            script_result.parameters,
        )
        # Verify the old place holder ScriptResult was deleted
        self.assertIsNone(reload_object(previous_script_set))
        self.assertIsNone(reload_object(previous_script_result))

    def test_create_testing_script_set_cleans_up_empty_sets(self):
        Config.objects.set_config("max_node_testing_results", 1)
        script = factory.make_Script(script_type=SCRIPT_TYPE.TESTING)
        node = factory.make_Node()
        ScriptSet.objects.create_testing_script_set(
            node, scripts=[script.name]
        )
        script_set = ScriptSet.objects.create_testing_script_set(
            node, scripts=[script.name]
        )
        # the first set is removed since it's empty
        self.assertCountEqual(
            [script_set],
            ScriptSet.objects.filter(result_type=RESULT_TYPE.TESTING).all(),
        )

    def test_create_testing_script_set_cleans_up_per_node(self):
        Config.objects.set_config("max_node_testing_results", 1)
        script = factory.make_Script(script_type=SCRIPT_TYPE.TESTING)
        node1 = factory.make_Node()
        node2 = factory.make_Node()
        ScriptSet.objects.create_testing_script_set(
            node1, scripts=[script.name]
        )
        script_set1 = ScriptSet.objects.create_testing_script_set(
            node1, scripts=[script.name]
        )
        ScriptSet.objects.create_testing_script_set(
            node2, scripts=[script.name]
        )
        script_set2 = ScriptSet.objects.create_testing_script_set(
            node2, scripts=[script.name]
        )
        # older sets are removed for each node
        self.assertCountEqual(
            [script_set1, script_set2],
            ScriptSet.objects.filter(result_type=RESULT_TYPE.TESTING).all(),
        )

    def test_create_testing_script_set_aborts_old_running_scripts(self):
        node = factory.make_Node()
        script = factory.make_Script(script_type=SCRIPT_TYPE.TESTING)
        # Generate old ScriptResults
        for _ in range(3):
            script_set = factory.make_ScriptSet(
                node=node, result_type=RESULT_TYPE.TESTING
            )
            for status in SCRIPT_STATUS_RUNNING_OR_PENDING:
                factory.make_ScriptResult(script_set=script_set, status=status)

        script_set = ScriptSet.objects.create_testing_script_set(
            node, scripts=[script.name]
        )

        for script_result in ScriptResult.objects.all():
            if script_result.script_set == script_set:
                self.assertEqual(SCRIPT_STATUS.PENDING, script_result.status)
            else:
                self.assertEqual(SCRIPT_STATUS.ABORTED, script_result.status)

    def test_create_testing_script_set_accepts_params(self):
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.TESTING,
            parameters={"storage": {"type": "storage"}},
        )
        node = factory.make_Node()
        for _ in range(3):
            factory.make_PhysicalBlockDevice(node=node)

        script_set = ScriptSet.objects.create_testing_script_set(
            node, [script.name], {script.name: {"storage": "all"}}
        )

        self.assertItemsEqual(
            [bd.name for bd in node.physicalblockdevice_set],
            [
                script_result.parameters["storage"]["value"]["name"]
                for script_result in script_set
                if script_result.script == script
            ],
        )

    def test_create_testing_script_set_errors_params(self):
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.TESTING,
            parameters={"storage": {"type": "storage"}},
        )
        node = factory.make_Node()

        self.assertRaises(
            ValidationError,
            ScriptSet.objects.create_testing_script_set,
            node,
            [script.name],
            {script.name: factory.make_name("unknown")},
        )
        self.assertFalse(ScriptSet.objects.all().exists())

    def test_create_installation_script_set(self):
        node = factory.make_Node()

        script_set = ScriptSet.objects.create_installation_script_set(node)
        self.assertItemsEqual(
            [CURTIN_INSTALL_LOG],
            [script_result.name for script_result in script_set],
        )
        self.assertEquals(RESULT_TYPE.INSTALLATION, script_set.result_type)
        self.assertEquals(
            node.power_state, script_set.power_state_before_transition
        )

    def test_create_installation_script_set_cleans_up_past_limit(self):
        limit = Config.objects.get_config("max_node_installation_results")
        node = factory.make_Node()
        for _ in range(limit + 2):
            ScriptSet.objects.create_installation_script_set(node)

        self.assertEqual(
            limit,
            ScriptResult.objects.filter(
                script_name=CURTIN_INSTALL_LOG
            ).count(),
        )

    def test_create_installation_script_set_cleans_up_by_node(self):
        limit = Config.objects.get_config("max_node_installation_results")
        node1 = factory.make_Node()
        node2 = factory.make_Node()
        for _ in range(limit + 2):
            ScriptSet.objects.create_installation_script_set(node1)
            ScriptSet.objects.create_installation_script_set(node2)

        self.assertEqual(
            limit,
            ScriptResult.objects.filter(
                script_name=CURTIN_INSTALL_LOG,
                script_set__in=ScriptSet.objects.filter(node=node1),
            ).count(),
        )
        self.assertEqual(
            limit,
            ScriptResult.objects.filter(
                script_name=CURTIN_INSTALL_LOG,
                script_set__in=ScriptSet.objects.filter(node=node2),
            ).count(),
        )

    def test_create_installation_script_set_cleans_up_current(self):
        Config.objects.set_config("max_node_installation_results", 1)
        node = factory.make_Node()
        node.current_installation_script_set = ScriptSet.objects.create_installation_script_set(
            node
        )
        node.save()

        ScriptSet.objects.create_installation_script_set(node)

        self.assertEqual(
            1,
            ScriptResult.objects.filter(
                script_name=CURTIN_INSTALL_LOG
            ).count(),
        )

    def test_create_installation_script_set_cleans_up_empty_sets(self):
        Config.objects.set_config("max_node_installation_results", 1)
        node = factory.make_Node()
        ScriptSet.objects.create_installation_script_set(node)
        script_set = ScriptSet.objects.create_installation_script_set(node)
        # the first set is removed since it's empty
        self.assertCountEqual(
            [script_set],
            ScriptSet.objects.filter(
                result_type=RESULT_TYPE.INSTALLATION
            ).all(),
        )

    def test_create_installation_script_set_cleans_up_per_node(self):
        Config.objects.set_config("max_node_installation_results", 1)
        node1 = factory.make_Node()
        node2 = factory.make_Node()
        ScriptSet.objects.create_installation_script_set(node1)
        script_set1 = ScriptSet.objects.create_installation_script_set(node1)
        ScriptSet.objects.create_installation_script_set(node2)
        script_set2 = ScriptSet.objects.create_installation_script_set(node2)
        # older results are deleted by node
        self.assertCountEqual(
            [script_set1, script_set2],
            ScriptSet.objects.filter(
                result_type=RESULT_TYPE.INSTALLATION
            ).all(),
        )


class TestScriptSet(MAASServerTestCase):
    """Test the ScriptSet model."""

    def test_find_script_result_by_id(self):
        script_set = factory.make_ScriptSet()
        script_results = [
            factory.make_ScriptResult(script_set=script_set) for _ in range(3)
        ]
        script_result = random.choice(script_results)
        self.assertEquals(
            script_result,
            script_set.find_script_result(script_result_id=script_result.id),
        )

    def test_find_script_result_by_name(self):
        script_set = factory.make_ScriptSet()
        script_results = [
            factory.make_ScriptResult(script_set=script_set) for _ in range(3)
        ]
        script_result = random.choice(script_results)
        self.assertEquals(
            script_result,
            script_set.find_script_result(script_name=script_result.name),
        )

    def test_find_script_result_returns_none_when_not_found(self):
        script_set = factory.make_ScriptSet()
        self.assertIsNone(script_set.find_script_result())

    def test_status(self):
        statuses = {
            SCRIPT_STATUS.RUNNING: (
                SCRIPT_STATUS.APPLYING_NETCONF,
                SCRIPT_STATUS.INSTALLING,
                SCRIPT_STATUS.PENDING,
                SCRIPT_STATUS.ABORTED,
                SCRIPT_STATUS.FAILED,
                SCRIPT_STATUS.FAILED_APPLYING_NETCONF,
                SCRIPT_STATUS.FAILED_INSTALLING,
                SCRIPT_STATUS.TIMEDOUT,
                SCRIPT_STATUS.PENDING,
                SCRIPT_STATUS.DEGRADED,
                SCRIPT_STATUS.PASSED,
            ),
            SCRIPT_STATUS.PENDING: (
                SCRIPT_STATUS.ABORTED,
                SCRIPT_STATUS.FAILED,
                SCRIPT_STATUS.FAILED_APPLYING_NETCONF,
                SCRIPT_STATUS.FAILED_INSTALLING,
                SCRIPT_STATUS.TIMEDOUT,
                SCRIPT_STATUS.DEGRADED,
                SCRIPT_STATUS.PASSED,
            ),
            SCRIPT_STATUS.ABORTED: (
                SCRIPT_STATUS.FAILED,
                SCRIPT_STATUS.FAILED_APPLYING_NETCONF,
                SCRIPT_STATUS.FAILED_INSTALLING,
                SCRIPT_STATUS.TIMEDOUT,
                SCRIPT_STATUS.PASSED,
                SCRIPT_STATUS.DEGRADED,
            ),
            SCRIPT_STATUS.FAILED: (
                SCRIPT_STATUS.FAILED_APPLYING_NETCONF,
                SCRIPT_STATUS.FAILED_INSTALLING,
                SCRIPT_STATUS.TIMEDOUT,
                SCRIPT_STATUS.DEGRADED,
                SCRIPT_STATUS.PASSED,
            ),
            SCRIPT_STATUS.TIMEDOUT: (
                SCRIPT_STATUS.DEGRADED,
                SCRIPT_STATUS.PASSED,
            ),
            SCRIPT_STATUS.DEGRADED: (SCRIPT_STATUS.PASSED,),
            SCRIPT_STATUS.PASSED: (SCRIPT_STATUS.PASSED,),
        }
        for status, other_statuses in statuses.items():
            script_set = factory.make_ScriptSet()
            factory.make_ScriptResult(script_set=script_set, status=status)
            for _ in range(3):
                factory.make_ScriptResult(
                    script_set=script_set, status=random.choice(other_statuses)
                )
            if status == SCRIPT_STATUS.TIMEDOUT:
                status = SCRIPT_STATUS.FAILED
            self.assertEquals(status, script_set.status)

    def test_status_with_suppressed(self):
        script_set = factory.make_ScriptSet()
        factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.PASSED
        )
        factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.FAILED, suppressed=True
        )
        factory.make_ScriptResult(
            status=SCRIPT_STATUS.TIMEDOUT, suppressed=True
        )
        factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.FAILED_INSTALLING,
            suppressed=True,
        )
        factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.SKIPPED
        )
        self.assertEquals(SCRIPT_STATUS.PASSED, script_set.status)

    def test_empty_scriptset_has_no_status(self):
        script_set = factory.make_ScriptSet()
        self.assertEquals(-1, script_set.status)

    def test_started(self):
        script_set = factory.make_ScriptSet()
        now = datetime.now()
        started = now - timedelta(seconds=random.randint(1, 500))
        factory.make_ScriptResult(script_set=script_set, started=now)
        factory.make_ScriptResult(script_set=script_set, started=started)
        self.assertEquals(started, script_set.started)

    def test_ended(self):
        script_set = factory.make_ScriptSet()
        ended = datetime.now() + timedelta(seconds=random.randint(1, 500))
        factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.PASSED
        )
        factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.PASSED, ended=ended
        )
        self.assertEquals(ended, script_set.ended)

    def test_ended_returns_none_when_not_all_results_finished(self):
        script_set = factory.make_ScriptSet()
        factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.PASSED
        )
        factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.RUNNING
        )
        self.assertIsNone(script_set.ended)

    def test_get_runtime(self):
        script_set = factory.make_ScriptSet()
        runtime_seconds = random.randint(1, 59)
        now = datetime.now()
        factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.PASSED,
            started=now - timedelta(seconds=runtime_seconds),
            ended=now,
        )
        if runtime_seconds < 10:
            text_seconds = "0%d" % runtime_seconds
        else:
            text_seconds = "%d" % runtime_seconds
        self.assertEquals("0:00:%s" % text_seconds, script_set.runtime)

    def test_get_runtime_blank_when_missing(self):
        script_set = factory.make_ScriptSet()
        factory.make_ScriptResult(
            script_set=script_set, status=SCRIPT_STATUS.PENDING
        )
        self.assertEquals("", script_set.runtime)

    def test_select_for_hardware_scripts_removes_if_not_selected(self):
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING,
            for_hardware=[
                random.choice(
                    [
                        "usb:174c:07d1",
                        "pci:8086:1918",
                        "modalias:pci:v00001A03d00001150sv000015D9*",
                    ]
                )
            ],
        )
        node = factory.make_Node()
        script_set = ScriptSet.objects.create_commissioning_script_set(node)
        script_set.add_pending_script(script)

        script_set.select_for_hardware_scripts()

        self.assertItemsEqual(
            list(NODE_INFO_SCRIPTS),
            [script_result.name for script_result in script_set],
        )

    def test_select_for_hardware_scripts_adds_modalias(self):
        node = factory.make_Node()
        system_vendor = factory.make_NodeMetadata(
            node=node, key="system_vendor"
        )
        system_product = factory.make_NodeMetadata(
            node=node, key="system_product"
        )
        system_version = factory.make_NodeMetadata(
            node=node, key="system_version"
        )
        mainboard_vendor = factory.make_NodeMetadata(
            node=node, key="mainboard_vendor"
        )
        mainboard_product = factory.make_NodeMetadata(
            node=node, key="mainboard_product"
        )
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING,
            for_hardware=[
                random.choice(
                    [
                        "usb:174c:07d1",
                        "pci:8086:1918",
                        "modalias:pci:v00001A03d00001150sv000015D9*",
                        "system_vendor:%s" % system_vendor.value,
                        "system_product:%s" % system_product.value,
                        "system_version:%s" % system_version.value,
                        "mainboard_vendor:%s" % mainboard_vendor.value,
                        "mainboard_product:%s" % mainboard_product.value,
                    ]
                )
            ],
        )
        script_set = ScriptSet.objects.create_commissioning_script_set(
            node, [random.choice(script.tags)]
        )

        to_mock = (
            node.__module__ + "." + node.__class__.__qualname__ + ".modaliases"
        )
        with mock.patch(to_mock, new_callable=PropertyMock) as mods:
            mods.return_value = [
                "pci:v00008086d00001918sv000015D9sd00000888bc06sc00i00",
                "usb:v174Cp07D1d1000dc00dsc00dp00ic08isc06ip50in00",
                "pci:v00001A03d00001150sv000015D9sd00000888bc06sc04i00",
            ]
            script_set.select_for_hardware_scripts()

        self.assertItemsEqual(
            list(NODE_INFO_SCRIPTS) + [script.name],
            [script_result.name for script_result in script_set],
        )

    def test_select_for_hardware_scripts_adds_system_mainboard(self):
        node = factory.make_Node()
        system_vendor = factory.make_NodeMetadata(
            node=node, key="system_vendor"
        )
        system_product = factory.make_NodeMetadata(
            node=node, key="system_product"
        )
        system_version = factory.make_NodeMetadata(
            node=node, key="system_version"
        )
        mainboard_vendor = factory.make_NodeMetadata(
            node=node, key="mainboard_vendor"
        )
        mainboard_product = factory.make_NodeMetadata(
            node=node, key="mainboard_product"
        )
        script = factory.make_Script(
            script_type=SCRIPT_TYPE.COMMISSIONING,
            for_hardware=[
                random.choice(
                    [
                        "system_vendor:%s" % system_vendor.value,
                        "system_product:%s" % system_product.value,
                        "system_version:%s" % system_version.value,
                        "mainboard_vendor:%s" % mainboard_vendor.value,
                        "mainboard_product:%s" % mainboard_product.value,
                    ]
                )
            ],
        )
        script_set = ScriptSet.objects.create_commissioning_script_set(
            node, [random.choice(script.tags)]
        )

        script_set.select_for_hardware_scripts()

        self.assertItemsEqual(
            list(NODE_INFO_SCRIPTS) + [script.name],
            [script_result.name for script_result in script_set],
        )

    def test_regenerate_storage(self):
        node = factory.make_Node(interface=True)
        script_set = factory.make_ScriptSet(node=node)

        passed_storage_script = factory.make_Script(
            parameters={"storage": {"type": "storage"}}
        )
        passed_storage_parameters = {
            "storage": {
                "type": "storage",
                "value": {
                    "name": factory.make_name("name"),
                    "model": factory.make_name("model"),
                    "serial": factory.make_name("serial"),
                    "id_path": "/dev/%s" % factory.make_name("id_path"),
                },
            }
        }
        passed_storage_script_result = factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.PASSED,
            script=passed_storage_script,
            parameters=passed_storage_parameters,
        )

        pending_storage_script = factory.make_Script(
            parameters={"storage": {"type": "storage"}}
        )
        pending_storage_parameters = {
            "storage": {
                "type": "storage",
                "value": {
                    "name": factory.make_name("name"),
                    "model": factory.make_name("model"),
                    "serial": factory.make_name("serial"),
                    "id_path": "/dev/%s" % factory.make_name("id_path"),
                },
            }
        }
        pending_storage_script_result = factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.PENDING,
            script=pending_storage_script,
            parameters=pending_storage_parameters,
        )

        pending_network_script = factory.make_Script(
            parameters={"interface": {"type": "interface"}}
        )
        pending_network_parameters = {
            "interface": {"type": "interface", "value": "all"}
        }
        factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.PENDING,
            script=pending_network_script,
            parameters=pending_network_parameters,
        )

        pending_other_script = factory.make_ScriptResult(script_set=script_set)

        script_set.regenerate()

        passed_storage_script_result = reload_object(
            passed_storage_script_result
        )
        self.assertIsNotNone(passed_storage_script_result)
        self.assertDictEqual(
            passed_storage_parameters, passed_storage_script_result.parameters
        )
        self.assertIsNone(reload_object(pending_storage_script_result))
        self.assertIsNotNone(reload_object(pending_other_script))

        new_storage_script_result = script_set.scriptresult_set.get(
            script=pending_storage_script
        )
        bd = node.physicalblockdevice_set.first()
        self.assertDictEqual(
            {
                "storage": {
                    "type": "storage",
                    "value": {
                        "name": bd.name,
                        "model": bd.model,
                        "serial": bd.serial,
                        "id_path": bd.id_path,
                        "physical_blockdevice_id": bd.id,
                    },
                }
            },
            new_storage_script_result.parameters,
        )
        new_network_script_result = script_set.scriptresult_set.get(
            script=pending_network_script
        )
        self.assertDictEqual(
            {"interface": {"type": "interface", "value": "all"}},
            new_network_script_result.parameters,
        )

    def test_regenerate_network(self):
        node = factory.make_Node()
        interface = factory.make_Interface(node=node)
        interface.ip_addresses.all().delete()
        interface.ip_addresses.add(factory.make_StaticIPAddress())
        script_set = factory.make_ScriptSet(node=node)

        passed_network_script = factory.make_Script(
            parameters={"interface": {"type": "interface"}}
        )
        passed_network_parameters = {
            "interface": {
                "type": "interface",
                "value": {
                    "name": factory.make_name("name"),
                    "mac_address": factory.make_mac_address(),
                    "vendor": factory.make_name("vendor"),
                    "product": factory.make_name("product"),
                },
            }
        }
        passed_network_script_result = factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.PASSED,
            script=passed_network_script,
            parameters=passed_network_parameters,
        )

        pending_network_script = factory.make_Script(
            parameters={"interface": {"type": "interface"}}
        )
        pending_network_parameters = {
            "interface": {
                "type": "interface",
                "value": {
                    "name": factory.make_name("name"),
                    "mac_address": factory.make_mac_address(),
                    "vendor": factory.make_name("vendor"),
                    "product": factory.make_name("product"),
                },
            }
        }
        pending_network_script_result = factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.PENDING,
            script=pending_network_script,
            parameters=pending_network_parameters,
        )

        pending_storage_script = factory.make_Script(
            parameters={"storage": {"type": "storage"}}
        )
        pending_storage_parameters = {
            "storage": {"type": "storage", "value": "all"}
        }
        factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.PENDING,
            script=pending_storage_script,
            parameters=pending_storage_parameters,
        )

        pending_other_script = factory.make_ScriptResult(script_set=script_set)

        script_set.regenerate(storage=False, network=True)

        passed_network_script_result = reload_object(
            passed_network_script_result
        )
        self.assertIsNotNone(passed_network_script_result)
        self.assertDictEqual(
            passed_network_parameters, passed_network_script_result.parameters
        )
        self.assertIsNone(reload_object(pending_network_script_result))
        self.assertIsNotNone(reload_object(pending_other_script))

        new_network_script_result = script_set.scriptresult_set.get(
            script=pending_network_script
        )
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
            new_network_script_result.parameters,
        )
        new_storage_script_result = script_set.scriptresult_set.get(
            script=pending_storage_script
        )
        self.assertDictEqual(
            {"storage": {"type": "storage", "value": "all"}},
            new_storage_script_result.parameters,
        )

    def test_regenerate_network_with_url_param(self):
        node = factory.make_Node()
        interface = factory.make_Interface(node=node)
        interface.ip_addresses.all().delete()
        interface.ip_addresses.add(factory.make_StaticIPAddress())
        url = factory.make_url(scheme="http")
        default_url = factory.make_url(scheme="http")
        script_set = factory.make_ScriptSet(node=node)

        pending_network_script = factory.make_Script(
            parameters={
                "interface": {"type": "interface"},
                "url": {
                    "type": "url",
                    "required": True,
                    "default": default_url,
                },
            }
        )
        factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.PENDING,
            script=pending_network_script,
            parameters={
                "interface": {
                    "type": "interface",
                    "value": {
                        "name": factory.make_name("name"),
                        "mac_address": factory.make_mac_address(),
                        "vendor": factory.make_name("vendor"),
                        "product": factory.make_name("product"),
                    },
                },
                "url": {
                    "type": "url",
                    "required": True,
                    "default": default_url,
                    "value": url,
                },
            },
        )

        script_set.regenerate(storage=False, network=True)

        new_network_script_result = script_set.scriptresult_set.get(
            script=pending_network_script
        )
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
                },
                "url": {
                    "type": "url",
                    "required": True,
                    "default": default_url,
                    "value": url,
                },
            },
            new_network_script_result.parameters,
        )

    def test_regenerate_logs_failure(self):
        mock_logger = self.patch(scriptset_module.logger, "error")
        node = factory.make_Node()
        script_set = factory.make_ScriptSet(node=node)

        pending_storage_script = factory.make_Script(
            parameters={
                "storage": {"type": "storage"},
                "runtime": {"type": "runtime"},
            }
        )
        pending_storage_parameters = {
            "storage": {
                "type": "storage",
                "value": {
                    "name": factory.make_name("name"),
                    "model": factory.make_name("model"),
                    "serial": factory.make_name("serial"),
                    "id_path": "/dev/%s" % factory.make_name("id_path"),
                },
            },
            "runtime": {
                "type": "runtime",
                "value": factory.make_name("invalid_value"),
            },
        }
        pending_storage_script_result = factory.make_ScriptResult(
            script_set=script_set,
            status=SCRIPT_STATUS.PENDING,
            script=pending_storage_script,
            parameters=pending_storage_parameters,
        )

        script_set.regenerate()

        self.assertIsNone(reload_object(pending_storage_script_result))
        self.assertItemsEqual([], list(script_set))
        expected_msg = (
            "Removing Script %s from ScriptSet due to regeneration "
            "error - {'runtime': ['Must be an int']}"
            % pending_storage_script.name
        )
        event_type = EventType.objects.get(
            name=EVENT_TYPES.SCRIPT_RESULT_ERROR
        )
        event = Event.objects.get(node=node, type_id=event_type.id)
        self.assertEquals(expected_msg, event.description)
        self.assertThat(mock_logger, MockCalledOnceWith(expected_msg))

    def test_delete(self):
        node = factory.make_Node(with_empty_script_sets=True)
        orig_commissioning_script_set = node.current_commissioning_script_set
        orig_testing_script_set = node.current_testing_script_set
        orig_installation_script_set = node.current_installation_script_set
        script_set = factory.make_ScriptSet(node=node)

        script_set.delete()

        node = reload_object(node)
        self.assertIsNone(reload_object(script_set))
        self.assertEquals(
            orig_commissioning_script_set,
            node.current_commissioning_script_set,
        )
        self.assertEquals(
            orig_testing_script_set, node.current_testing_script_set
        )
        self.assertEquals(
            orig_installation_script_set, node.current_installation_script_set
        )

    def test_delete_prevents_del_of_current_commissioning_script_set(self):
        node = factory.make_Node(with_empty_script_sets=True)
        self.assertRaises(
            ValidationError, node.current_commissioning_script_set.delete
        )

    def test_delete_prevents_del_of_current_installation_script_set(self):
        node = factory.make_Node(with_empty_script_sets=True)
        self.assertRaises(
            ValidationError, node.current_installation_script_set.delete
        )

    def test_delete_sets_current_testing_script_set_to_older_version(self):
        node = factory.make_Node(with_empty_script_sets=True)
        previous_script_set = factory.make_ScriptSet(
            node=node, result_type=RESULT_TYPE.TESTING
        )
        node.current_testing_script_set = factory.make_ScriptSet(
            node=node, result_type=RESULT_TYPE.TESTING
        )
        node.save()

        node.current_testing_script_set.delete()
        self.assertEquals(
            previous_script_set, reload_object(node).current_testing_script_set
        )
