# Copyright 2015-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maasserver.websockets.base`"""

__all__ = []

import random
from unittest.mock import ANY, MagicMock, sentinel

from django.db.models.query import QuerySet
from django.http import HttpRequest
from maasserver.forms import AdminMachineForm, AdminMachineWithMACAddressesForm
from maasserver.models.node import Device, Node
from maasserver.models.vlan import VLAN
from maasserver.models.zone import Zone
from maasserver.permissions import NodePermission
from maasserver.testing.architecture import make_usable_architecture
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.orm import reload_object
from maasserver.websockets import base
from maasserver.websockets.base import (
    Handler,
    HandlerDoesNotExistError,
    HandlerNoSuchMethodError,
    HandlerPermissionError,
    HandlerValidationError,
)
from maastesting.matchers import MockCalledOnceWith, MockNotCalled
from maastesting.testcase import MAASTestCase
from provisioningserver.prometheus.metrics import PROMETHEUS_METRICS
from provisioningserver.utils.twisted import asynchronous
from testtools.matchers import Equals, Is, IsInstance, MatchesStructure
from testtools.testcase import ExpectedException


def make_handler(name, **kwargs):
    meta = type("Meta", (object,), kwargs)
    return object.__new__(type(name, (Handler,), {"Meta": meta}))


class TestHandlerMeta(MAASTestCase):
    def test_creates_handler_with_default_meta(self):
        handler = Handler(None, {}, None)
        self.assertThat(
            handler._meta,
            MatchesStructure(
                abstract=Is(False),
                allowed_methods=Equals(
                    ["list", "get", "create", "update", "delete", "set_active"]
                ),
                handler_name=Equals(""),
                object_class=Is(None),
                queryset=Is(None),
                pk=Equals("id"),
                fields=Is(None),
                exclude=Is(None),
                list_fields=Is(None),
                list_exclude=Is(None),
                non_changeable=Is(None),
                form=Is(None),
            ),
        )

    def test_creates_handler_with_options(self):
        handler = make_handler(
            "TestHandler",
            abstract=True,
            allowed_methods=["list"],
            handler_name="testing",
            queryset=Node.objects.all(),
            pk="system_id",
            fields=["hostname", "distro_series"],
            exclude=["system_id"],
            list_fields=["hostname"],
            list_exclude=["hostname"],
            non_changeable=["system_id"],
            form=sentinel.form,
        )
        self.assertThat(
            handler._meta,
            MatchesStructure(
                abstract=Is(True),
                allowed_methods=Equals(["list"]),
                handler_name=Equals("testing"),
                object_class=Is(Node),
                queryset=IsInstance(QuerySet),
                pk=Equals("system_id"),
                fields=Equals(["hostname", "distro_series"]),
                exclude=Equals(["system_id"]),
                list_fields=Equals(["hostname"]),
                list_exclude=Equals(["hostname"]),
                non_changeable=Equals(["system_id"]),
                form=Is(sentinel.form),
            ),
        )

    def test_sets_handler_name_based_on_class_name(self):
        names = [
            ("TestHandler", "test"),
            ("TestHandlerNew", "testnew"),
            ("AlwaysLowerHandler", "alwayslower"),
        ]
        for class_name, handler_name in names:
            obj = make_handler(class_name)
            self.expectThat(obj._meta.handler_name, Equals(handler_name))

    def test_sets_object_class_based_on_queryset(self):
        handler = make_handler("TestHandler", queryset=Node.objects.all())
        self.assertIs(Node, handler._meta.object_class)

    def test_copy_fields_and_excludes_to_list_fields_and_list_excludes(self):
        fields = [factory.make_name("field") for _ in range(3)]
        exclude = [factory.make_name("field") for _ in range(3)]
        handler = make_handler("TestHandler", fields=fields, exclude=exclude)
        self.assertEqual(fields, handler._meta.list_fields)
        self.assertEqual(exclude, handler._meta.list_exclude)

    def test_copy_fields_and_excludes_doesnt_overwrite_lists_if_set(self):
        fields = [factory.make_name("field") for _ in range(3)]
        exclude = [factory.make_name("field") for _ in range(3)]
        list_fields = [factory.make_name("field") for _ in range(3)]
        list_exclude = [factory.make_name("field") for _ in range(3)]
        handler = make_handler(
            "TestHandler",
            fields=fields,
            exclude=exclude,
            list_fields=list_fields,
            list_exclude=list_exclude,
        )
        self.assertEqual(list_fields, handler._meta.list_fields)
        self.assertEqual(list_exclude, handler._meta.list_exclude)


class FakeNodesHandlerMixin:
    def make_nodes_handler(self, **kwargs):
        meta_args = {
            "queryset": Node.objects.all(),
            "object_class": Node,
            "pk": "system_id",
            "pk_type": str,
        }
        meta_args.update(kwargs)
        user = factory.make_User()
        request = HttpRequest()
        request.user = user
        handler = make_handler("TestNodesHandler", **meta_args)
        handler.__init__(user, {}, request)
        return handler

    def make_mock_node_with_fields(self, **kwargs):
        return object.__new__(type("MockNode", (object,), kwargs))


class TestHandler(MAASServerTestCase, FakeNodesHandlerMixin):
    def test_full_dehydrate_only_includes_allowed_fields(self):
        handler = self.make_nodes_handler(fields=["hostname", "cpu_count"])
        node = factory.make_Node()
        self.assertEqual(
            {"hostname": node.hostname, "cpu_count": node.cpu_count},
            handler.full_dehydrate(node),
        )

    def test_full_dehydrate_excludes_fields(self):
        handler = self.make_nodes_handler(
            fields=["hostname", "power_type"], exclude=["power_type"]
        )
        node = factory.make_Node()
        self.assertEqual(
            {"hostname": node.hostname}, handler.full_dehydrate(node)
        )

    def test_full_dehydrate_includes_permissions_when_defined(self):
        handler = self.make_nodes_handler(
            fields=["hostname"],
            edit_permission=NodePermission.admin,
            delete_permission=NodePermission.admin,
        )
        handler.user = factory.make_admin()
        node = factory.make_Node()
        self.assertEqual(
            {"hostname": node.hostname, "permissions": ["edit", "delete"]},
            handler.full_dehydrate(node),
        )

    def test_full_dehydrate_only_includes_list_fields_when_for_list(self):
        handler = self.make_nodes_handler(
            list_fields=["cpu_count", "power_state"]
        )
        node = factory.make_Node()
        self.assertEqual(
            {"cpu_count": node.cpu_count, "power_state": node.power_state},
            handler.full_dehydrate(node, for_list=True),
        )

    def test_full_dehydrate_excludes_list_fields_when_for_list(self):
        handler = self.make_nodes_handler(
            list_fields=["cpu_count", "power_state"],
            list_exclude=["cpu_count"],
        )
        node = factory.make_Node()
        self.assertEqual(
            {"power_state": node.power_state},
            handler.full_dehydrate(node, for_list=True),
        )

    def test_full_dehydrate_calls_field_dehydrate_method_if_exists(self):
        handler = self.make_nodes_handler(fields=["hostname"])
        mock_dehydrate_hostname = self.patch(handler, "dehydrate_hostname")
        mock_dehydrate_hostname.return_value = sentinel.hostname
        node = factory.make_Node()
        self.expectThat(
            {"hostname": sentinel.hostname},
            Equals(handler.full_dehydrate(node)),
        )
        self.expectThat(
            mock_dehydrate_hostname, MockCalledOnceWith(node.hostname)
        )

    def test_full_dehydrate_calls_final_dehydrate_method(self):
        handler = self.make_nodes_handler(fields=["hostname"])
        mock_dehydrate = self.patch_autospec(handler, "dehydrate")
        mock_dehydrate.return_value = sentinel.final_dehydrate
        node = factory.make_Node()
        self.expectThat(
            sentinel.final_dehydrate, Equals(handler.full_dehydrate(node))
        )
        self.expectThat(
            mock_dehydrate,
            MockCalledOnceWith(
                node, {"hostname": node.hostname}, for_list=False
            ),
        )

    def test_dehydrate_does_nothing(self):
        handler = self.make_nodes_handler()
        self.assertEqual(
            sentinel.nothing, handler.dehydrate(sentinel.obj, sentinel.nothing)
        )

    def test_full_hydrate_only_doesnt_set_primary_key_field(self):
        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        handler = self.make_nodes_handler(fields=["system_id", "hostname"])
        node = self.make_mock_node_with_fields(
            system_id=system_id, hostname=factory.make_name("hostname")
        )
        handler.full_hydrate(
            node,
            {
                "system_id": factory.make_name("system_id"),
                "hostname": hostname,
            },
        )
        self.expectThat(system_id, Equals(node.system_id))
        self.expectThat(hostname, Equals(node.hostname))

    def test_full_hydrate_only_sets_allowed_fields(self):
        hostname = factory.make_name("hostname")
        power_state = "on"
        handler = self.make_nodes_handler(fields=["hostname", "power_state"])
        node = self.make_mock_node_with_fields(
            hostname=factory.make_name("hostname"),
            power_state="off",
            power_type="ipmi",
        )
        handler.full_hydrate(
            node,
            {
                "hostname": hostname,
                "power_state": power_state,
                "power_type": "manual",
            },
        )
        self.expectThat(hostname, Equals(node.hostname))
        self.expectThat(power_state, Equals(node.power_state))
        self.expectThat("ipmi", Equals(node.power_type))

    def test_full_hydrate_only_sets_non_excluded_fields(self):
        hostname = factory.make_name("hostname")
        handler = self.make_nodes_handler(
            fields=["hostname", "power_state"], exclude=["power_state"]
        )
        node = self.make_mock_node_with_fields(
            hostname=factory.make_name("hostname"),
            power_state="off",
            power_type="ipmi",
        )
        handler.full_hydrate(
            node,
            {
                "hostname": hostname,
                "power_state": "on",
                "power_type": "manual",
            },
        )
        self.expectThat(hostname, Equals(node.hostname))
        self.expectThat("off", Equals(node.power_state))
        self.expectThat("ipmi", Equals(node.power_type))

    def test_full_hydrate_only_doesnt_set_fields_not_allowed_to_change(self):
        hostname = factory.make_name("hostname")
        handler = self.make_nodes_handler(
            fields=["hostname", "power_state"], non_changeable=["power_state"]
        )
        node = self.make_mock_node_with_fields(
            hostname=factory.make_name("hostname"),
            power_state="off",
            power_type="ipmi",
        )
        handler.full_hydrate(
            node,
            {
                "hostname": hostname,
                "power_state": "on",
                "power_type": "manual",
            },
        )
        self.expectThat(hostname, Equals(node.hostname))
        self.expectThat("off", Equals(node.power_state))
        self.expectThat("ipmi", Equals(node.power_type))

    def test_full_hydrate_calls_fields_hydrate_method_if_present(self):
        call_hostname = factory.make_name("hostname")
        hostname = factory.make_name("hostname")
        handler = self.make_nodes_handler(fields=["hostname"])
        node = self.make_mock_node_with_fields(
            hostname=factory.make_name("hostname")
        )
        mock_hydrate_hostname = self.patch(handler, "hydrate_hostname")
        mock_hydrate_hostname.return_value = hostname
        handler.full_hydrate(node, {"hostname": call_hostname})
        self.expectThat(hostname, Equals(node.hostname))
        self.expectThat(
            mock_hydrate_hostname, MockCalledOnceWith(call_hostname)
        )

    def test_full_hydrate_calls_final_hydrate_method(self):
        hostname = factory.make_name("hostname")
        handler = self.make_nodes_handler(fields=["hostname"])
        node = self.make_mock_node_with_fields(
            hostname=factory.make_name("hostname")
        )
        mock_hydrate = self.patch_autospec(handler, "hydrate")
        mock_hydrate.return_value = sentinel.final_hydrate
        self.expectThat(
            sentinel.final_hydrate,
            Equals(handler.full_hydrate(node, {"hostname": hostname})),
        )
        self.expectThat(
            mock_hydrate, MockCalledOnceWith(node, {"hostname": hostname})
        )

    def test_hydrate_does_nothing(self):
        handler = self.make_nodes_handler()
        self.assertEqual(
            sentinel.obj, handler.hydrate(sentinel.obj, sentinel.nothing)
        )

    def test_get_object_raises_HandlerValidationError(self):
        handler = self.make_nodes_handler()
        self.assertRaises(
            HandlerValidationError, handler.get_object, {"host": "test"}
        )

    def test_get_object_raises_HandlerDoesNotExistError(self):
        handler = self.make_nodes_handler()
        self.assertRaises(
            HandlerDoesNotExistError,
            handler.get_object,
            {"system_id": factory.make_name("system_id")},
        )

    def test_get_object_returns_object(self):
        handler = self.make_nodes_handler()
        node = factory.make_Node()
        self.assertEqual(
            node.hostname,
            handler.get_object({"system_id": node.system_id}).hostname,
        )

    def test_get_object_respects_queryset(self):
        handler = self.make_nodes_handler(queryset=Device.objects.all())
        machine = factory.make_Machine()
        device = factory.make_Device()
        returned_device = handler.get_object({"system_id": device.system_id})
        self.assertEqual(device.hostname, returned_device.hostname)
        self.assertRaises(
            HandlerDoesNotExistError,
            handler.get_object,
            {"system_id": machine.system_id},
        )

    def test_get_queryset(self):
        queryset = MagicMock()
        list_queryset = MagicMock()
        handler = make_handler(
            "TestHandler", queryset=queryset, list_queryset=list_queryset
        )
        self.assertEqual(queryset, handler.get_queryset())

    def test_get_queryset_list(self):
        queryset = MagicMock()
        list_queryset = MagicMock()
        handler = make_handler(
            "TestHandler", queryset=queryset, list_queryset=list_queryset
        )
        self.assertEqual(list_queryset, handler.get_queryset(for_list=True))

    def test_get_queryset_list_only_if_avail(self):
        queryset = MagicMock()
        handler = make_handler("TestHandler", queryset=queryset)
        self.assertEqual(queryset, handler.get_queryset(for_list=True))

    def test_execute_only_allows_meta_allowed_methods(self):
        handler = self.make_nodes_handler(allowed_methods=["list"])
        with ExpectedException(HandlerNoSuchMethodError):
            handler.execute("get", {}).wait(30)

    def test_execute_raises_HandlerNoSuchMethodError(self):
        handler = self.make_nodes_handler(allowed_methods=["extra_method"])
        with ExpectedException(HandlerNoSuchMethodError):
            handler.execute("extra_method", {}).wait(30)

    def test_execute_calls_in_database_thread_with_params(self):
        # Methods are assumed by default to be synchronous and are called in a
        # thread that originates from a specific threadpool.
        handler = self.make_nodes_handler()
        params = {"system_id": factory.make_name("system_id")}
        self.patch(base, "deferToDatabase").return_value = sentinel.thing
        result = handler.execute("get", params).wait(30)
        self.assertThat(result, Is(sentinel.thing))
        self.assertThat(base.deferToDatabase, MockCalledOnceWith(ANY, params))

    def test_execute_track_latency(self):
        mock_metrics = self.patch(PROMETHEUS_METRICS, "update")

        handler = self.make_nodes_handler()
        params = {"system_id": factory.make_name("system_id")}
        self.patch(base, "deferToDatabase").return_value = sentinel.thing
        result = handler.execute("get", params).wait(30)
        self.assertIs(result, sentinel.thing)
        mock_metrics.assert_called_with(
            "maas_websocket_call_latency",
            "observe",
            labels={"call": "testnodes.get"},
            value=ANY,
        )

    def test_list(self):
        output = [{"hostname": factory.make_Node().hostname} for _ in range(3)]
        handler = self.make_nodes_handler(fields=["hostname"])
        self.assertItemsEqual(output, handler.list({}))

    def test_list_start(self):
        nodes = [factory.make_Node() for _ in range(6)]
        output = [{"hostname": node.hostname} for node in nodes[3:]]
        handler = self.make_nodes_handler(fields=["hostname"])
        self.assertItemsEqual(output, handler.list({"start": nodes[2].id}))

    def test_list_limit(self):
        nodes = [factory.make_Node() for _ in range(6)]
        output = [{"hostname": node.hostname} for node in nodes[:3]]
        handler = self.make_nodes_handler(fields=["hostname"])
        self.assertItemsEqual(output, handler.list({"limit": 3}))

    def test_list_start_and_limit(self):
        nodes = [factory.make_Node() for _ in range(9)]
        output = [{"hostname": node.hostname} for node in nodes[3:6]]
        handler = self.make_nodes_handler(fields=["hostname"])
        self.assertItemsEqual(
            output, handler.list({"start": nodes[2].id, "limit": 3})
        )

    def test_list_adds_to_loaded_pks(self):
        pks = [factory.make_Node().system_id for _ in range(3)]
        handler = self.make_nodes_handler(fields=["hostname"])
        handler.list({})
        self.assertItemsEqual(pks, handler.cache["loaded_pks"])

    def test_list_unions_the_loaded_pks(self):
        nodes = [factory.make_Node() for _ in range(3)]
        pks = {node.system_id for node in nodes}
        handler = self.make_nodes_handler(fields=["hostname"])
        # Make two calls to list making sure the loaded_pks contains all of
        # the primary keys listed.
        handler.list({"limit": 1})
        # Nodes are little special: they are referred to by system_id, but
        # ordered by id. This is because system_id is no longer guaranteed to
        # sort from oldest node to newest.
        handler.list({"start": nodes[0].id})
        self.assertItemsEqual(pks, handler.cache["loaded_pks"])

    def test_get(self):
        node = factory.make_Node()
        handler = self.make_nodes_handler(fields=["hostname"])
        self.assertEqual(
            {"hostname": node.hostname},
            handler.get({"system_id": node.system_id}),
        )

    def test_get_raises_permission_error(self):
        node = factory.make_Node()
        # Set the permission to admin to force the error.
        handler = self.make_nodes_handler(
            fields=["hostname"], view_permission=NodePermission.admin
        )
        self.assertRaises(
            HandlerPermissionError, handler.get, {"system_id": node.system_id}
        )

    def test_create_without_form(self):
        # Use a zone as its simple and easy to create without a form, unlike
        # Node which requires a form.
        handler = make_handler(
            "TestZoneHandler",
            queryset=Zone.objects.all(),
            fields=["name", "description"],
        )
        name = factory.make_name("zone")
        json_obj = handler.create({"name": name})
        self.expectThat({"name": name, "description": ""}, Equals(json_obj))
        self.expectThat(name, Equals(Zone.objects.get(name=name).name))

    def test_create_without_form_uses_object_id(self):
        # Uses a VLAN, which only requires a Fabric.
        handler = make_handler(
            "TestVLANHandler",
            queryset=VLAN.objects.all(),
            fields=["fabric", "vid"],
        )
        fabric = factory.make_Fabric()
        vid = random.randint(1, 4094)
        json_obj = handler.create({"vid": vid, "fabric": fabric.id})
        self.expectThat({"vid": vid, "fabric": fabric.id}, Equals(json_obj))
        vlan = VLAN.objects.get(vid=vid)
        self.expectThat(vid, Equals(vlan.vid))
        self.expectThat(fabric, Equals(fabric))

    def test_create_with_form_creates_node(self):
        hostname = factory.make_name("hostname")
        arch = make_usable_architecture(self)
        handler = self.make_nodes_handler(
            fields=["hostname", "architecture"],
            form=AdminMachineWithMACAddressesForm,
        )
        handler.user = factory.make_admin()
        json_obj = handler.create(
            {
                "hostname": hostname,
                "architecture": arch,
                "mac_addresses": [factory.make_mac_address()],
            }
        )
        self.expectThat(
            {"hostname": hostname, "architecture": arch}, Equals(json_obj)
        )

    def test_create_with_form_uses_form_from_get_form_class(self):
        hostname = factory.make_name("hostname")
        arch = make_usable_architecture(self)
        handler = self.make_nodes_handler(fields=["hostname", "architecture"])
        handler.user = factory.make_admin()
        self.patch(
            handler, "get_form_class"
        ).return_value = AdminMachineWithMACAddressesForm
        json_obj = handler.create(
            {
                "hostname": hostname,
                "architecture": arch,
                "mac_addresses": [factory.make_mac_address()],
            }
        )
        self.expectThat(
            {"hostname": hostname, "architecture": arch}, Equals(json_obj)
        )

    def test_create_raised_permission_error(self):
        hostname = factory.make_name("hostname")
        arch = make_usable_architecture(self)
        handler = self.make_nodes_handler(fields=["hostname", "architecture"])
        self.patch(
            handler, "get_form_class"
        ).return_value = AdminMachineWithMACAddressesForm
        self.assertRaises(
            HandlerPermissionError,
            handler.create,
            {
                "hostname": hostname,
                "architecture": arch,
                "mac_addresses": [factory.make_mac_address()],
            },
        )

    def test_create_with_form_passes_request_with_user_set(self):
        hostname = factory.make_name("hostname")
        arch = make_usable_architecture(self)
        mock_form = MagicMock()
        mock_form.return_value.is_valid.return_value = True
        mock_form.return_value.save.return_value = factory.make_Node()
        handler = self.make_nodes_handler(fields=[], form=mock_form)
        handler.create({"hostname": hostname, "architecture": arch})
        # Extract the passed request.
        passed_request = mock_form.call_args_list[0][1]["request"]
        self.assertIs(handler.user, passed_request.user)

    def test_create_with_form_raises_HandlerValidationError(self):
        hostname = factory.make_name("hostname")
        arch = make_usable_architecture(self)
        handler = self.make_nodes_handler(
            fields=["hostname", "architecture"],
            form=AdminMachineWithMACAddressesForm,
        )
        handler.user = factory.make_admin()
        self.assertRaises(
            HandlerValidationError,
            handler.create,
            {"hostname": hostname, "architecture": arch},
        )

    def test_update_without_form(self):
        handler = self.make_nodes_handler(fields=["hostname"])
        node = factory.make_Node()
        hostname = factory.make_name("hostname")
        json_obj = handler.update(
            {"system_id": node.system_id, "hostname": hostname}
        )
        self.expectThat({"hostname": hostname}, Equals(json_obj))
        self.expectThat(reload_object(node).hostname, Equals(hostname))

    def test_update_with_form_updates_node(self):
        arch = make_usable_architecture(self)
        node = factory.make_Node(architecture=arch, power_type="manual")
        hostname = factory.make_name("hostname")
        handler = self.make_nodes_handler(
            fields=["hostname"], form=AdminMachineForm
        )
        handler.user = factory.make_admin()
        json_obj = handler.update(
            {"system_id": node.system_id, "hostname": hostname}
        )
        self.expectThat({"hostname": hostname}, Equals(json_obj))
        self.expectThat(reload_object(node).hostname, Equals(hostname))

    def test_update_with_form_uses_form_from_get_form_class(self):
        arch = make_usable_architecture(self)
        node = factory.make_Node(architecture=arch, power_type="manual")
        hostname = factory.make_name("hostname")
        handler = self.make_nodes_handler(fields=["hostname"])
        handler.user = factory.make_admin()
        self.patch(handler, "get_form_class").return_value = AdminMachineForm
        json_obj = handler.update(
            {"system_id": node.system_id, "hostname": hostname}
        )
        self.expectThat({"hostname": hostname}, Equals(json_obj))
        self.expectThat(reload_object(node).hostname, Equals(hostname))

    def test_update_with_form_raises_permission_error(self):
        arch = make_usable_architecture(self)
        node = factory.make_Node(architecture=arch, power_type="manual")
        hostname = factory.make_name("hostname")
        handler = self.make_nodes_handler(fields=["hostname"])
        self.patch(handler, "get_form_class").return_value = AdminMachineForm
        self.assertRaises(
            HandlerPermissionError,
            handler.update,
            {"system_id": node.system_id, "hostname": hostname},
        )

    def test_delete_deletes_object(self):
        node = factory.make_Node()
        handler = self.make_nodes_handler()
        handler.delete({"system_id": node.system_id})
        self.assertIsNone(reload_object(node))

    def test_delete_raises_permission_error(self):
        node = factory.make_Node()
        handler = self.make_nodes_handler(
            delete_permission=NodePermission.admin
        )
        self.assertRaises(
            HandlerPermissionError,
            handler.delete,
            {"system_id": node.system_id},
        )

    def test_set_active_does_nothing_if_no_active_obj_and_missing_pk(self):
        handler = self.make_nodes_handler()
        mock_get = self.patch(handler, "get")
        handler.set_active({})
        self.assertThat(mock_get, MockNotCalled())

    def test_set_active_clears_active_if_missing_pk(self):
        handler = self.make_nodes_handler()
        handler.cache["active_pk"] = factory.make_name("system_id")
        handler.set_active({})
        self.assertFalse("active_pk" in handler.cache)

    def test_set_active_returns_data_and_sets_active(self):
        node = factory.make_Node()
        handler = self.make_nodes_handler(fields=["system_id"])
        node_data = handler.set_active({"system_id": node.system_id})
        self.expectThat(node_data["system_id"], Equals(node.system_id))
        self.expectThat(handler.cache["active_pk"], Equals(node.system_id))

    def test_on_listen_calls_listen(self):
        handler = self.make_nodes_handler()
        pk = factory.make_name("system_id")
        mock_listen = self.patch(handler, "listen")
        mock_listen.side_effect = HandlerDoesNotExistError()
        handler.on_listen(sentinel.channel, sentinel.action, pk)
        self.assertThat(
            mock_listen,
            MockCalledOnceWith(sentinel.channel, sentinel.action, pk),
        )

    def test_on_listen_returns_None_if_unknown_action(self):
        handler = self.make_nodes_handler()
        mock_listen = self.patch(handler, "listen")
        mock_listen.side_effect = HandlerDoesNotExistError()
        self.assertIsNone(
            handler.on_listen(
                sentinel.channel, factory.make_name("action"), sentinel.pk
            )
        )

    def test_on_listen_delete_removes_pk_from_loaded(self):
        handler = self.make_nodes_handler()
        node = factory.make_Node()
        handler.cache["loaded_pks"].add(node.system_id)
        self.assertEqual(
            (handler._meta.handler_name, "delete", node.system_id),
            handler.on_listen(sentinel.channel, "delete", node.system_id),
        )
        self.assertTrue(
            node.system_id not in handler.cache["loaded_pks"],
            "on_listen delete did not remove system_id from loaded_pks",
        )

    def test_on_listen_delete_returns_None_if_pk_not_in_loaded(self):
        handler = self.make_nodes_handler()
        node = factory.make_Node()
        self.assertEqual(
            None, handler.on_listen(sentinel.channel, "delete", node.system_id)
        )

    def test_on_listen_create_adds_pk_to_loaded(self):
        handler = self.make_nodes_handler(fields=["hostname"])
        node = factory.make_Node(owner=handler.user)
        self.assertEqual(
            (
                handler._meta.handler_name,
                "create",
                {"hostname": node.hostname},
            ),
            handler.on_listen(sentinel.channel, "create", node.system_id),
        )
        self.assertTrue(
            node.system_id in handler.cache["loaded_pks"],
            "on_listen create did not add system_id to loaded_pks",
        )

    def test_on_listen_create_returns_update_if_pk_already_known(self):
        handler = self.make_nodes_handler(fields=["hostname"])
        node = factory.make_Node(owner=handler.user)
        handler.cache["loaded_pks"].add(node.system_id)
        self.assertEqual(
            (
                handler._meta.handler_name,
                "update",
                {"hostname": node.hostname},
            ),
            handler.on_listen(sentinel.channel, "create", node.system_id),
        )

    def test_on_listen_update_returns_delete_action_if_obj_is_None(self):
        handler = self.make_nodes_handler()
        node = factory.make_Node()
        handler.cache["loaded_pks"].add(node.system_id)
        self.patch(handler, "listen").return_value = None
        self.assertEqual(
            (handler._meta.handler_name, "delete", node.system_id),
            handler.on_listen(sentinel.channel, "update", node.system_id),
        )
        self.assertTrue(
            node.system_id not in handler.cache["loaded_pks"],
            "on_listen update did not remove system_id from loaded_pks",
        )

    def test_on_listen_update_returns_update_action_if_obj_not_None(self):
        handler = self.make_nodes_handler(fields=["hostname"])
        node = factory.make_Node()
        handler.cache["loaded_pks"].add(node.system_id)
        self.assertEqual(
            (
                handler._meta.handler_name,
                "update",
                {"hostname": node.hostname},
            ),
            handler.on_listen(sentinel.channel, "update", node.system_id),
        )
        self.assertTrue(
            node.system_id in handler.cache["loaded_pks"],
            "on_listen update removed system_id from loaded_pks",
        )

    def test_on_listen_update_returns_create_action_if_not_in_loaded(self):
        handler = self.make_nodes_handler(fields=["hostname"])
        node = factory.make_Node()
        self.assertEqual(
            (
                handler._meta.handler_name,
                "create",
                {"hostname": node.hostname},
            ),
            handler.on_listen(sentinel.channel, "update", node.system_id),
        )
        self.assertTrue(
            node.system_id in handler.cache["loaded_pks"],
            "on_listen update didnt add system_id to loaded_pks",
        )

    def test_on_listen_update_call_full_dehydrate_for_list_if_not_active(self):
        node = factory.make_Node()
        handler = self.make_nodes_handler()
        handler.cache["loaded_pks"].add(node.system_id)
        mock_dehydrate = self.patch(handler, "full_dehydrate")
        mock_dehydrate.return_value = sentinel.data
        self.expectThat(
            handler.on_listen(sentinel.channel, "update", node.system_id),
            Equals((handler._meta.handler_name, "update", sentinel.data)),
        )
        self.expectThat(
            mock_dehydrate, MockCalledOnceWith(node, for_list=True)
        )

    def test_on_listen_update_call_full_dehydrate_not_for_list_if_active(self):
        node = factory.make_Node()
        handler = self.make_nodes_handler()
        handler.cache["loaded_pks"].add(node.system_id)
        handler.cache["active_pk"] = node.system_id
        mock_dehydrate = self.patch(handler, "full_dehydrate")
        mock_dehydrate.return_value = sentinel.data
        self.expectThat(
            handler.on_listen(sentinel.channel, "update", node.system_id),
            Equals((handler._meta.handler_name, "update", sentinel.data)),
        )
        self.expectThat(
            mock_dehydrate, MockCalledOnceWith(node, for_list=False)
        )

    def test_listen_calls_get_object_with_pk_on_other_actions(self):
        handler = self.make_nodes_handler()
        mock_get_object = self.patch(handler, "get_object")
        mock_get_object.return_value = sentinel.obj
        self.expectThat(
            handler.listen(sentinel.channel, "update", sentinel.pk),
            Equals(sentinel.obj),
        )
        self.expectThat(
            mock_get_object,
            MockCalledOnceWith({handler._meta.pk: sentinel.pk}),
        )


class TestHandlerTransaction(
    MAASTransactionServerTestCase, FakeNodesHandlerMixin
):
    def test_execute_calls_asynchronous_method_with_params(self):
        # An asynchronous method -- decorated with @asynchronous -- is called
        # directly, not in a thread.
        handler = self.make_nodes_handler()
        handler.get = asynchronous(lambda params: sentinel.thing)
        params = {"system_id": factory.make_name("system_id")}
        result = handler.execute("get", params).wait(30)
        self.assertThat(result, Is(sentinel.thing))
