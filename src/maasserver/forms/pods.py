# Copyright 2017-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Pod forms."""

__all__ = ["PodForm"]

from functools import partial

import crochet
from django import forms
from django.core.exceptions import ValidationError
from django.forms import (
    BooleanField,
    CharField,
    ChoiceField,
    IntegerField,
    ModelChoiceField,
)
from maasserver.clusterrpc import driver_parameters
from maasserver.clusterrpc.driver_parameters import (
    get_driver_parameters_from_json,
)
from maasserver.clusterrpc.pods import (
    compose_machine,
    discover_pod,
    get_best_discovered_result,
)
from maasserver.enum import BMC_TYPE, INTERFACE_TYPE, NODE_CREATION_TYPE
from maasserver.exceptions import PodProblem
from maasserver.forms import MAASModelForm
from maasserver.models import (
    BMC,
    BMCRoutableRackControllerRelationship,
    Domain,
    Interface,
    Machine,
    Node,
    Pod,
    PodStoragePool,
    RackController,
    ResourcePool,
    Tag,
    Zone,
)
from maasserver.node_constraint_filter_forms import (
    get_storage_constraints_from_string,
    interfaces_validator,
    LabeledConstraintMapField,
    nodes_by_interface,
    storage_validator,
)
from maasserver.rpc import getClientFromIdentifiers
from maasserver.utils.dns import validate_hostname
from maasserver.utils.forms import set_form_error
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
import petname
from provisioningserver.drivers import SETTING_SCOPE
from provisioningserver.drivers.pod import (
    Capabilities,
    InterfaceAttachType,
    KnownHostInterface,
    RequestedMachine,
    RequestedMachineBlockDevice,
    RequestedMachineInterface,
)
from provisioningserver.enum import MACVLAN_MODE, MACVLAN_MODE_CHOICES
from provisioningserver.logger import LegacyLogger
from provisioningserver.utils.network import get_ifname_for_label
from provisioningserver.utils.twisted import asynchronous
from twisted.internet.defer import inlineCallbacks
from twisted.python.threadable import isInIOThread


log = LegacyLogger()


def make_unique_hostname():
    """Returns a unique machine hostname."""
    while True:
        hostname = petname.Generate(2, "-")
        if Machine.objects.filter(hostname=hostname).exists():
            continue
        else:
            return hostname


class PodForm(MAASModelForm):
    class Meta:
        model = Pod
        fields = [
            "name",
            "tags",
            "zone",
            "pool",
            "cpu_over_commit_ratio",
            "memory_over_commit_ratio",
            "default_storage_pool",
            "default_macvlan_mode",
        ]

    name = forms.CharField(
        label="Name", required=False, help_text="The name of the pod"
    )

    zone = forms.ModelChoiceField(
        label="Physical zone",
        required=False,
        initial=Zone.objects.get_default_zone,
        queryset=Zone.objects.all(),
        to_field_name="name",
    )

    pool = forms.ModelChoiceField(
        label="Default pool of created machines",
        required=False,
        initial=lambda: ResourcePool.objects.get_default_resource_pool().name,
        queryset=ResourcePool.objects.all(),
        to_field_name="name",
    )

    cpu_over_commit_ratio = forms.FloatField(
        label="CPU overcommit ratio",
        initial=1,
        required=False,
        min_value=0,
        max_value=10,
    )

    memory_over_commit_ratio = forms.FloatField(
        label="Memory overcommit ratio",
        initial=1,
        required=False,
        min_value=0,
        max_value=10,
    )

    default_storage_pool = forms.ModelChoiceField(
        label="Default storage pool",
        required=False,
        queryset=PodStoragePool.objects.none(),
        to_field_name="pool_id",
    )

    default_macvlan_mode = forms.ChoiceField(
        label="Default MACVLAN mode",
        required=False,
        choices=MACVLAN_MODE_CHOICES,
        initial=MACVLAN_MODE_CHOICES[0],
    )

    def __init__(
        self, data=None, instance=None, request=None, user=None, **kwargs
    ):
        self.is_new = instance is None
        self.request = request
        self.user = user
        super(PodForm, self).__init__(data=data, instance=instance, **kwargs)
        if data is None:
            data = {}
        type_value = data.get("type", self.initial.get("type"))

        self.drivers_orig = driver_parameters.get_all_power_types()
        self.drivers = {
            driver["name"]: driver
            for driver in self.drivers_orig
            if driver["driver_type"] == "pod"
        }
        if len(self.drivers) == 0:
            type_value = ""
        elif type_value not in self.drivers:
            type_value = (
                "" if self.instance is None else self.instance.power_type
            )
        choices = [
            (name, driver["description"])
            for name, driver in self.drivers.items()
        ]
        self.fields["type"] = forms.ChoiceField(
            required=True, choices=choices, initial=type_value
        )
        if not self.is_new:
            if self.instance.power_type != "":
                self.initial["type"] = self.instance.power_type
        if instance is not None:
            self.initial["zone"] = instance.zone.name
            self.initial["pool"] = instance.pool.name
            self.fields[
                "default_storage_pool"
            ].queryset = instance.storage_pools.all()
            if instance.default_storage_pool:
                self.initial[
                    "default_storage_pool"
                ] = instance.default_storage_pool.pool_id

    def _clean_fields(self):
        """Override to dynamically add fields based on the value of `type`
        field."""
        # Process the built-in fields first.
        super(PodForm, self)._clean_fields()
        # If no errors then we re-process with the fields required by the
        # selected type for the pod.
        if len(self.errors) == 0:
            driver_fields = get_driver_parameters_from_json(
                self.drivers_orig, scope=SETTING_SCOPE.BMC
            )
            self.param_fields = driver_fields[
                self.cleaned_data["type"]
            ].field_dict
            self.fields.update(self.param_fields)
            if not self.is_new:
                for key, value in self.instance.power_parameters.items():
                    if key not in self.data:
                        self.data[key] = value
            super(PodForm, self)._clean_fields()

    def clean(self):
        cleaned_data = super(PodForm, self).clean()
        if len(self.drivers) == 0:
            set_form_error(
                self,
                "type",
                "No rack controllers are connected, unable to validate.",
            )
        elif (
            not self.is_new
            and self.instance.power_type != self.cleaned_data.get("type")
        ):
            set_form_error(
                self,
                "type",
                "Cannot change the type of a pod. Delete and re-create the "
                "pod with a different type.",
            )
        return cleaned_data

    def save(self, *args, **kwargs):
        """Persist the pod into the database."""

        def check_for_duplicate(power_type, power_parameters):
            # When the Pod is new try to get a BMC of the same type and
            # parameters to convert the BMC to a new Pod. When the Pod is not
            # new the form will use the already existing pod instance to update
            # those fields. If updating the fields causes a duplicate BMC then
            # a validation erorr will be raised from the model level.
            if self.is_new:
                bmc = BMC.objects.filter(
                    power_type=power_type, power_parameters=power_parameters
                ).first()
                if bmc is not None:
                    if bmc.bmc_type == BMC_TYPE.BMC:
                        # Convert the BMC to a Pod and set as the instance for
                        # the PodForm.
                        bmc.bmc_type = BMC_TYPE.POD
                        bmc.pool = (
                            ResourcePool.objects.get_default_resource_pool()
                        )
                        return bmc.as_pod()
                    else:
                        # Pod already exists with the same power_type and
                        # parameters.
                        raise ValidationError(
                            "Pod %s with type and "
                            "parameters already exist." % bmc.name
                        )

        def update_obj(existing_obj):
            if existing_obj is not None:
                self.instance = existing_obj
            self.instance = super(PodForm, self).save(commit=False)
            self.instance.power_type = power_type
            self.instance.power_parameters = power_parameters
            # Add tag for pod console logging with
            # appropriate kernel parameters.
            tag, _ = Tag.objects.get_or_create(
                name="pod-console-logging",
                kernel_opts="console=tty1 console=ttyS0",
            )
            # Add this tag to the pod.  This only adds the tag
            # if it is not present on the pod.
            self.instance.add_tag(tag.name)
            return self.instance

        power_type = self.cleaned_data["type"]
        # Set power_parameters to the generated param_fields.
        power_parameters = {
            param_name: self.cleaned_data[param_name]
            for param_name in self.param_fields.keys()
            if param_name in self.cleaned_data
        }

        if isInIOThread():
            # Running in twisted reactor, do the work inside the reactor.
            d = deferToDatabase(
                transactional(check_for_duplicate),
                power_type,
                power_parameters,
            )
            d.addCallback(partial(deferToDatabase, transactional(update_obj)))
            d.addCallback(lambda _: self.discover_and_sync_pod())
            return d
        else:
            # Perform the actions inside the executing thread.
            existing_obj = check_for_duplicate(power_type, power_parameters)
            if existing_obj is not None:
                self.instance = existing_obj
            self.instance = update_obj(self.instance)
            return self.discover_and_sync_pod()

    def discover_and_sync_pod(self):
        """Discover and sync the pod information."""

        def update_db(result):
            discovered_pod, discovered = result

            # When called with an instance that has no name, be sure to set
            # it before going any further. If this is a new instance this will
            # also create it in the database.
            if not self.instance.name:
                self.instance.set_random_name()
            if self.request is not None:
                user = self.request.user
            else:
                user = self.user
            self.instance.sync(discovered_pod, user)

            # Save which rack controllers can route and which cannot.
            discovered_rack_ids = [
                rack_id for rack_id, _ in discovered[0].items()
            ]
            for rack_controller in RackController.objects.all():
                routable = rack_controller.system_id in discovered_rack_ids
                bmc_route_model = BMCRoutableRackControllerRelationship
                relation, created = bmc_route_model.objects.get_or_create(
                    bmc=self.instance.as_bmc(),
                    rack_controller=rack_controller,
                    defaults={"routable": routable},
                )
                if not created and relation.routable != routable:
                    relation.routable = routable
                    relation.save()
            return self.instance

        if isInIOThread():
            # Running in twisted reactor, do the work inside the reactor.
            d = discover_pod(
                self.instance.power_type,
                self.instance.power_parameters,
                pod_id=self.instance.id,
                name=self.instance.name,
            )
            d.addCallback(
                lambda discovered: (
                    get_best_discovered_result(discovered),
                    discovered,
                )
            )

            def catch_no_racks(result):
                discovered_pod, discovered = result
                if discovered_pod is None:
                    raise PodProblem(
                        "Unable to start the pod discovery process. "
                        "No rack controllers connected."
                    )
                return discovered_pod, discovered

            def wrap_errors(failure):
                if failure.check(PodProblem):
                    return failure
                else:
                    log.err(failure, "Failed to discover pod.")
                    raise PodProblem(str(failure.value))

            d.addCallback(catch_no_racks)
            d.addCallback(partial(deferToDatabase, transactional(update_db)))
            d.addErrback(wrap_errors)
            return d
        else:
            # Perform the actions inside the executing thread.
            try:
                discovered = discover_pod(
                    self.instance.power_type,
                    self.instance.power_parameters,
                    pod_id=self.instance.id,
                    name=self.instance.name,
                )
            except Exception as exc:
                raise PodProblem(str(exc)) from exc

            # Use the first discovered pod object. All other objects are
            # ignored. The other rack controllers that also provided a result
            # can route to the pod.
            try:
                discovered_pod = get_best_discovered_result(discovered)
            except Exception as error:
                raise PodProblem(str(error))
            if discovered_pod is None:
                raise PodProblem(
                    "Unable to start the pod discovery process. "
                    "No rack controllers connected."
                )
            return update_db((discovered_pod, discovered))


def get_known_host_interfaces(host: Node) -> list:
    """Given the specified host node, calculates each KnownHostInterface.

    :return: a list of KnownHostInterface objects for the specified Node.
    """
    interfaces = host.interface_set.all()
    result = []
    for interface in interfaces:
        ifname = interface.name
        if interface.type == INTERFACE_TYPE.BRIDGE:
            attach_type = InterfaceAttachType.BRIDGE
        else:
            attach_type = InterfaceAttachType.MACVLAN
        dhcp_enabled = False
        vlan = interface.vlan
        if vlan is not None:
            if vlan.dhcp_on:
                dhcp_enabled = True
            elif vlan.relay_vlan is not None:
                if vlan.relay_vlan.dhcp_on:
                    dhcp_enabled = True
        result.append(
            KnownHostInterface(
                ifname=ifname,
                attach_type=attach_type,
                dhcp_enabled=dhcp_enabled,
            )
        )
    return result


class ComposeMachineForm(forms.Form):
    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        if self.request is None:
            raise ValueError("'request' kwargs is required.")
        self.pod = kwargs.pop("pod", None)
        if self.pod is None:
            raise ValueError("'pod' kwargs is required.")
        super(ComposeMachineForm, self).__init__(*args, **kwargs)

        # Build the fields based on the pod and current pod hints.
        self.fields["cores"] = IntegerField(
            min_value=1, max_value=self.pod.hints.cores, required=False
        )
        self.initial["cores"] = 1
        self.fields["memory"] = IntegerField(
            min_value=1024, max_value=self.pod.hints.memory, required=False
        )
        self.initial["memory"] = 1024
        self.fields["architecture"] = ChoiceField(
            choices=[(arch, arch) for arch in self.pod.architectures],
            required=False,
        )
        self.initial["architecture"] = self.pod.architectures[0]
        if self.pod.hints.cpu_speed > 0:
            self.fields["cpu_speed"] = IntegerField(
                min_value=300,
                max_value=self.pod.hints.cpu_speed,
                required=False,
            )
        else:
            self.fields["cpu_speed"] = IntegerField(
                min_value=300, required=False
            )

        def duplicated_hostname(hostname):
            if Node.objects.filter(hostname=hostname).exists():
                raise ValidationError(
                    'Node with hostname "%s" already exists' % hostname
                )

        self.fields["hostname"] = CharField(
            required=False, validators=[duplicated_hostname, validate_hostname]
        )
        self.initial["hostname"] = make_unique_hostname()
        self.fields["domain"] = ModelChoiceField(
            required=False, queryset=Domain.objects.all()
        )
        self.initial["domain"] = Domain.objects.get_default_domain()
        self.fields["zone"] = ModelChoiceField(
            required=False, queryset=Zone.objects.all()
        )
        self.initial["zone"] = Zone.objects.get_default_zone()
        self.fields["pool"] = ModelChoiceField(
            required=False, queryset=ResourcePool.objects.all()
        )
        self.initial["pool"] = self.pod.pool
        self.fields["storage"] = CharField(
            validators=[storage_validator], required=False
        )
        self.initial["storage"] = "root:8(local)"
        self.fields["interfaces"] = LabeledConstraintMapField(
            validators=[interfaces_validator],
            label="Interface constraints",
            required=False,
        )
        self.initial["interfaces"] = None
        self.fields["skip_commissioning"] = BooleanField(required=False)
        self.initial["skip_commissioning"] = False
        self.allocated_ips = {}

    def get_value_for(self, field):
        """Get the value for `field`. Use initial data if missing or set to
        `None` in the cleaned_data."""
        value = self.cleaned_data.get(field)
        if value:
            return value
        elif field in self.initial:
            return self.initial[field]
        else:
            return None

    def get_requested_machine(self, known_host_interfaces):
        """Return the `RequestedMachine`."""
        block_devices = []

        storage_constraints = get_storage_constraints_from_string(
            self.get_value_for("storage")
        )
        for _, size, tags in storage_constraints:
            if tags is None:
                tags = []
            block_devices.append(
                RequestedMachineBlockDevice(size=size, tags=tags)
            )
        interfaces_label_map = self.get_value_for("interfaces")
        if interfaces_label_map is not None:
            requested_machine_interfaces = self._get_requested_machine_interfaces_via_constraints(
                interfaces_label_map
            )
        else:
            requested_machine_interfaces = [RequestedMachineInterface()]

        return RequestedMachine(
            hostname=self.get_value_for("hostname"),
            architecture=self.get_value_for("architecture"),
            cores=self.get_value_for("cores"),
            memory=self.get_value_for("memory"),
            cpu_speed=self.get_value_for("cpu_speed"),
            block_devices=block_devices,
            interfaces=requested_machine_interfaces,
            known_host_interfaces=known_host_interfaces,
        )

    def _pick_interface(self, interfaces):
        bridge_interfaces = interfaces.filter(
            type=INTERFACE_TYPE.BRIDGE
        ).order_by("-id")
        bond_interfaces = interfaces.filter(type=INTERFACE_TYPE.BOND).order_by(
            "-id"
        )
        bridge_interface = list(bridge_interfaces[:1])
        if bridge_interface:
            return bridge_interface[0]
        bond_interface = list(bond_interfaces[:1])
        if bond_interface:
            return bond_interface[0]
        return interfaces[0]

    def _get_requested_machine_interfaces_via_constraints(
        self, interfaces_label_map
    ):
        requested_machine_interfaces = []
        if self.pod.host is None:
            raise ValidationError(
                "Pod must be on a known host if interfaces are specified."
            )
        result = nodes_by_interface(
            interfaces_label_map,
            preconfigured=False,
            include_filter=dict(node__id=self.pod.host.id),
        )
        pod_node_id = self.pod.host.id
        if pod_node_id not in result.node_ids:
            raise ValidationError(
                "This pod does not match the specified networks."
            )
        has_bootable_vlan = False
        for label in result.label_map.keys():
            # XXX: We might want to use the "deepest" interface in the
            # heirarchy, to ensure we get a bridge or bond (if configured)
            # rather than its parent. Since child interfaces will be created
            # after their parents, this is a good approximation.
            # Search for bridges, followed by bonds, and finally with the
            # remaining interfaces.
            interface_ids = result.label_map[label][pod_node_id]
            interfaces = Interface.objects.filter(
                id__in=interface_ids
            ).order_by("-id")
            interface = self._pick_interface(interfaces)

            # Check to see if we have a bootable VLAN,
            # we need at least one.
            if interface.has_bootable_vlan():
                has_bootable_vlan = True
            rmi = self.get_requested_machine_interface_by_interface(interface)
            # Set the requested interface name and IP addresses.
            rmi.ifname = get_ifname_for_label(label)
            rmi.requested_ips = result.allocated_ips.get(label, [])
            rmi.ip_mode = result.ip_modes.get(label, None)
            requested_machine_interfaces.append(rmi)
        if not has_bootable_vlan:
            raise ValidationError(
                "MAAS DHCP must be enabled on at least one VLAN attached "
                "to the specified interfaces."
            )
        return requested_machine_interfaces

    def get_requested_machine_interface_by_interface(self, interface):
        if interface.type == INTERFACE_TYPE.BRIDGE:
            rmi = RequestedMachineInterface(
                attach_name=interface.name,
                attach_type=InterfaceAttachType.BRIDGE,
            )
        else:
            attach_options = self.pod.default_macvlan_mode
            if not attach_options:
                # Default macvlan mode is 'bridge' if not specified, since that
                # provides the best chance for connectivity.
                attach_options = MACVLAN_MODE.BRIDGE
            rmi = RequestedMachineInterface(
                attach_name=interface.name,
                attach_type=InterfaceAttachType.MACVLAN,
                attach_options=attach_options,
            )
        return rmi

    def save(self):
        """Prevent from usage."""
        raise AttributeError("Use `compose` instead of `save`.")

    def compose(
        self,
        timeout=120,
        creation_type=NODE_CREATION_TYPE.MANUAL,
        skip_commissioning=None,
    ):
        """Compose the machine.

        Internal operation of this form is asynchronous. It will block the
        calling thread until the asynchronous operation is complete. Adjust
        `timeout` to minimize the maximum wait for the asynchronous operation.
        """

        if skip_commissioning is None:
            skip_commissioning = self.get_value_for("skip_commissioning")

        def db_work(client):
            # Check overcommit ratios.
            over_commit_message = self.pod.check_over_commit_ratios(
                requested_cores=self.get_value_for("cores"),
                requested_memory=self.get_value_for("memory"),
            )
            if over_commit_message:
                raise PodProblem(
                    "Unable to compose KVM instance in '%s'. %s"
                    % (self.pod.name, over_commit_message)
                )

            # Update the default storage pool.
            if self.pod.default_storage_pool is not None:
                power_parameters[
                    "default_storage_pool_id"
                ] = self.pod.default_storage_pool.pool_id

            # Find the pod's known host interfaces.
            if self.pod.host is not None:
                interfaces = get_known_host_interfaces(self.pod.host)
            else:
                interfaces = []

            return client, interfaces

        def create_and_sync(result):
            requested_machine, result = result
            discovered_machine, pod_hints = result
            created_machine = self.pod.create_machine(
                discovered_machine,
                self.request.user,
                skip_commissioning=skip_commissioning,
                creation_type=creation_type,
                interfaces=self.get_value_for("interfaces"),
                requested_machine=requested_machine,
                domain=self.get_value_for("domain"),
                pool=self.get_value_for("pool"),
                zone=self.get_value_for("zone"),
            )
            self.pod.sync_hints(pod_hints)
            return created_machine

        @inlineCallbacks
        def async_compose_machine(
            result, power_type, power_paramaters, **kwargs
        ):
            client, result = result
            requested_machine = yield deferToDatabase(
                self.get_requested_machine, result
            )
            result = yield compose_machine(
                client,
                power_type,
                power_paramaters,
                requested_machine,
                **kwargs
            )
            return requested_machine, result

        power_parameters = self.pod.power_parameters.copy()

        if isInIOThread():
            # Running under the twisted reactor, before the work from inside.
            d = deferToDatabase(transactional(self.pod.get_client_identifiers))
            d.addCallback(getClientFromIdentifiers)
            d.addCallback(partial(deferToDatabase, transactional(db_work)))
            d.addCallback(
                async_compose_machine,
                self.pod.power_type,
                power_parameters,
                pod_id=self.pod.id,
                name=self.pod.name,
            )
            d.addCallback(
                partial(deferToDatabase, transactional(create_and_sync))
            )
            return d
        else:
            # Running outside of reactor. Do the work inside and then finish
            # the work outside.
            @asynchronous
            def wrap_compose_machine(
                client_idents, pod_type, parameters, request, pod_id, name
            ):
                """Wrapper to get the client."""
                d = getClientFromIdentifiers(client_idents)
                d.addCallback(
                    compose_machine,
                    pod_type,
                    parameters,
                    request,
                    pod_id=pod_id,
                    name=name,
                )
                return d

            _, result = db_work(None)
            try:
                requested_machine = self.get_requested_machine(result)
                result = wrap_compose_machine(
                    self.pod.get_client_identifiers(),
                    self.pod.power_type,
                    power_parameters,
                    requested_machine,
                    pod_id=self.pod.id,
                    name=self.pod.name,
                ).wait(timeout)
            except crochet.TimeoutError:
                raise PodProblem(
                    "Unable to compose a machine because '%s' driver "
                    "timed out after %d seconds."
                    % (self.pod.power_type, timeout)
                )
            return create_and_sync((requested_machine, result))


class ComposeMachineForPodsForm(forms.Form):
    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        if self.request is None:
            raise ValueError("'request' kwargs is required.")
        self.pods = kwargs.pop("pods", None)
        if self.pods is None:
            raise ValueError("'pods' kwargs is required.")
        super(ComposeMachineForPodsForm, self).__init__(*args, **kwargs)
        self.pod_forms = [
            ComposeMachineForm(request=self.request, data=self.data, pod=pod)
            for pod in self.pods
        ]

    def save(self):
        """Prevent from usage."""
        raise AttributeError("Use `compose` instead of `save`.")

    def compose(self):
        """Composed machine from available pod."""
        non_commit_forms = [
            form
            for form in self.valid_pod_forms
            if Capabilities.OVER_COMMIT not in form.pod.capabilities
        ]
        commit_forms = [
            form
            for form in self.valid_pod_forms
            if Capabilities.OVER_COMMIT in form.pod.capabilities
        ]
        # XXX - we need a way to get errors back from these forms for debugging
        # First, try to compose a machine from non-commitable pods.
        for form in non_commit_forms:
            try:
                return form.compose(
                    skip_commissioning=True,
                    creation_type=NODE_CREATION_TYPE.DYNAMIC,
                )
            except Exception:
                continue
        # Second, try to compose a machine from commitable pods
        for form in commit_forms:
            try:
                return form.compose(
                    skip_commissioning=True,
                    creation_type=NODE_CREATION_TYPE.DYNAMIC,
                )
            except Exception:
                continue
        # No machine found.
        return None

    def clean(self):
        self.valid_pod_forms = [
            pod_form for pod_form in self.pod_forms if pod_form.is_valid()
        ]
        if len(self.valid_pod_forms) == 0:
            self.add_error(
                "__all__", "No current pod resources match constraints."
            )
