# Copyright 2016-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""The controller handler for the WebSocket connection."""

__all__ = ["ControllerHandler"]

import logging

from django.db.models import OuterRef, Subquery
from maasserver.config import RegionConfiguration
from maasserver.forms import ControllerForm
from maasserver.models.config import Config
from maasserver.models.event import Event
from maasserver.models.node import Controller, RackController
from maasserver.permissions import NodePermission
from maasserver.websockets.base import HandlerError, HandlerPermissionError
from maasserver.websockets.handlers.machine import MachineHandler
from maasserver.websockets.handlers.node import node_prefetch
from provisioningserver.utils.version import get_version_tuple


class ControllerHandler(MachineHandler):
    class Meta(MachineHandler.Meta):
        abstract = False
        queryset = node_prefetch(
            Controller.controllers.all().prefetch_related("service_set"),
            "controllerinfo",
        )
        list_queryset = (
            Controller.controllers.all()
            .select_related("controllerinfo", "domain", "bmc")
            .prefetch_related("service_set")
            .prefetch_related("tags")
            .annotate(
                status_event_type_description=Subquery(
                    Event.objects.filter(
                        node=OuterRef("pk"), type__level__gte=logging.INFO
                    )
                    .order_by("-created", "-id")
                    .values("type__description")[:1]
                ),
                status_event_description=Subquery(
                    Event.objects.filter(
                        node=OuterRef("pk"), type__level__gte=logging.INFO
                    )
                    .order_by("-created", "-id")
                    .values("description")[:1]
                ),
            )
        )
        allowed_methods = [
            "list",
            "get",
            "create",
            "update",
            "action",
            "set_active",
            "check_power",
            "check_images",
            "create_physical",
            "create_vlan",
            "create_bond",
            "update_interface",
            "delete_interface",
            "link_subnet",
            "unlink_subnet",
            "get_summary_xml",
            "get_summary_yaml",
            "set_script_result_suppressed",
            "set_script_result_unsuppressed",
            "get_suppressible_script_results",
            "get_latest_failed_testing_script_results",
        ]
        form = ControllerForm
        exclude = [
            "status_expires",
            "parent",
            "boot_interface",
            "boot_cluster_ip",
            "token",
            "netboot",
            "agent_name",
            "power_state_queried",
            "power_state_updated",
            "gateway_link_ipv4",
            "gateway_link_ipv6",
            "enable_ssh",
            "skip_bmc_config",
            "skip_networking",
            "skip_storage",
            "instance_power_parameters",
            "address_ttl",
            "url",
            "dns_process",
            "managing_process",
        ]
        list_fields = [
            "id",
            "system_id",
            "hostname",
            "domain",
            "node_type",
            "status",
            "last_image_sync",
            "cpu_count",
            "cpu_speed",
        ]
        listen_channels = ["controller"]

    def get_form_class(self, action):
        """Return the form class used for `action`."""
        if action in ("create", "update"):
            return ControllerForm
        else:
            raise HandlerError("Unknown action: %s" % action)

    def get_queryset(self, for_list=False):
        """Return `QuerySet` for controllers only viewable by `user`."""
        if for_list:
            qs = self._meta.list_queryset
        else:
            qs = self._meta.queryset
        return Controller.controllers.get_nodes(
            self.user, NodePermission.view, from_nodes=qs
        )

    def dehydrate(self, obj, data, for_list=False):
        obj = obj.as_self()
        data = super().dehydrate(obj, data, for_list=for_list)
        data["version"] = obj.version
        if obj.version is not None and len(obj.version) > 0:
            version = get_version_tuple(obj.version)
            data["version__short"] = version.short_version
            long_version = version.short_version
            if len(version.extended_info) > 0:
                long_version += " (%s)" % version.extended_info
            if version.is_snap:
                long_version += " (snap)"
            data["version__long"] = long_version
        data["service_ids"] = [service.id for service in obj.service_set.all()]
        if not for_list:
            data["vlan_ids"] = [
                interface.vlan_id for interface in obj.interface_set.all()
            ]
        return data

    def check_images(self, params):
        """Get the image sync statuses of requested controllers."""
        result = {}
        for node in [self.get_object(param) for param in params]:
            # We use a RackController method; without the cast, it's a Node.
            node = node.as_rack_controller()
            if isinstance(node, RackController):
                result[node.system_id] = (
                    node.get_image_sync_status().replace("-", " ").title()
                )
        return result

    def dehydrate_show_os_info(self, obj):
        """Always show the OS information for controllers in the UI."""
        return True

    def register_info(self, params):
        """Return the registration info for a new controller.

        User must be a superuser to perform this action.
        """
        if not self.user.is_superuser:
            raise HandlerPermissionError()

        rpc_shared_secret = Config.objects.get_config("rpc_shared_secret")
        with RegionConfiguration.open() as config:
            maas_url = config.maas_url

        return {"url": maas_url, "secret": rpc_shared_secret}
