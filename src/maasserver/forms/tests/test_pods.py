# Copyright 2017-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for pod forms."""

__all__ = []

import random
from unittest.mock import ANY, call, MagicMock

import crochet
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from maasserver.enum import (
    BMC_TYPE,
    INTERFACE_TYPE,
    NODE_CREATION_TYPE,
    NODE_STATUS,
)
from maasserver.exceptions import PodProblem, StaticIPAddressUnavailable
from maasserver.forms import pods as pods_module
from maasserver.forms.pods import (
    ComposeMachineForm,
    ComposeMachineForPodsForm,
    get_known_host_interfaces,
    PodForm,
)
from maasserver.models import StaticIPAddress
from maasserver.models.bmc import Pod
from maasserver.models.node import Machine
from maasserver.models.resourcepool import ResourcePool
from maasserver.models.zone import Zone
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.orm import reload_object
from maasserver.utils.threads import deferToDatabase
from maastesting.matchers import (
    MockCalledOnce,
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from provisioningserver.drivers.pod import (
    Capabilities,
    DiscoveredMachine,
    DiscoveredMachineInterface,
    DiscoveredPod,
    DiscoveredPodHints,
    DiscoveredPodStoragePool,
    InterfaceAttachType,
    KnownHostInterface,
    RequestedMachine,
    RequestedMachineBlockDevice,
    RequestedMachineInterface,
)
from provisioningserver.enum import MACVLAN_MODE, MACVLAN_MODE_CHOICES
from testtools import ExpectedException
from testtools.matchers import (
    Equals,
    HasLength,
    Is,
    IsInstance,
    MatchesAll,
    MatchesListwise,
    MatchesSetwise,
    MatchesStructure,
    Not,
)
from twisted.internet.defer import fail, inlineCallbacks, succeed


wait_for_reactor = crochet.wait_for(30)  # 30 seconds.


def make_known_host_interfaces(pod):
    return [
        KnownHostInterface(
            ifname=interface.name,
            attach_type=(
                InterfaceAttachType.BRIDGE
                if interface.type == INTERFACE_TYPE.BRIDGE
                else InterfaceAttachType.MACVLAN
            ),
            dhcp_enabled=(
                True
                if (interface.vlan is not None and interface.vlan.dhcp_on)
                or (
                    interface.vlan.relay_vlan is not None
                    and interface.vlan.relay_vlan.dhcp_on
                )
                else False
            ),
        )
        for interface in (
            pod.host.interface_set.all() if pod.host is not None else []
        )
    ]


def make_pod_with_hints(with_host=False, host=None):
    architectures = [
        "amd64/generic",
        "i386/generic",
        "arm64/generic",
        "armhf/generic",
    ]
    if with_host and host is None:
        host = factory.make_Machine_with_Interface_on_Subnet()

    if host is None:
        ip = factory.make_StaticIPAddress()
    else:
        ip = factory.make_StaticIPAddress(interface=host.boot_interface)
    pod = factory.make_Pod(
        architectures=architectures,
        cores=random.randint(8, 16),
        memory=random.randint(4096, 8192),
        ip_address=ip,
        cpu_speed=random.randint(2000, 3000),
    )
    for _ in range(3):
        pool = factory.make_PodStoragePool(pod)
    pod.default_storage_pool = pool
    pod.save()
    pod.hints.cores = pod.cores
    pod.hints.memory = pod.memory
    pod.hints.cpu_speed = pod.cpu_speed
    pod.hints.save()
    return pod


class TestPodForm(MAASTransactionServerTestCase):
    def setUp(self):
        super().setUp()
        self.request = MagicMock()
        self.request.user = factory.make_User()

    def make_pod_info(self):
        # Use virsh pod type as the required fields are specific to the
        # type of pod being created.
        pod_type = "virsh"
        pod_ip_adddress = factory.make_ipv4_address()
        pod_power_address = "qemu+ssh://user@%s/system" % pod_ip_adddress
        return {
            "type": pod_type,
            "power_address": pod_power_address,
            "ip_address": pod_ip_adddress,
        }

    def fake_pod_discovery(self):
        discovered_pod = DiscoveredPod(
            architectures=["amd64/generic"],
            cores=random.randint(2, 4),
            memory=random.randint(1024, 4096),
            local_storage=random.randint(1024, 1024 * 1024),
            cpu_speed=random.randint(2048, 4048),
            hints=DiscoveredPodHints(
                cores=random.randint(2, 4),
                memory=random.randint(1024, 4096),
                local_storage=random.randint(1024, 1024 * 1024),
                cpu_speed=random.randint(2048, 4048),
            ),
            storage_pools=[
                DiscoveredPodStoragePool(
                    id=factory.make_name("pool_id"),
                    name=factory.make_name("name"),
                    type=factory.make_name("type"),
                    path="/var/lib/path/%s" % factory.make_name("path"),
                    storage=random.randint(1024, 1024 * 1024),
                )
                for _ in range(3)
            ],
        )
        discovered_rack_1 = factory.make_RackController()
        discovered_rack_2 = factory.make_RackController()
        failed_rack = factory.make_RackController()
        self.patch(pods_module, "discover_pod").return_value = (
            {
                discovered_rack_1.system_id: discovered_pod,
                discovered_rack_2.system_id: discovered_pod,
            },
            {failed_rack.system_id: factory.make_exception()},
        )
        return (
            discovered_pod,
            [discovered_rack_1, discovered_rack_2],
            [failed_rack],
        )

    def test_contains_limited_set_of_fields(self):
        form = PodForm()
        self.assertItemsEqual(
            [
                "name",
                "tags",
                "type",
                "zone",
                "pool",
                "cpu_over_commit_ratio",
                "memory_over_commit_ratio",
                "default_storage_pool",
                "default_macvlan_mode",
            ],
            list(form.fields),
        )

    def test_creates_pod_with_discovered_information(self):
        (
            discovered_pod,
            discovered_racks,
            failed_racks,
        ) = self.fake_pod_discovery()
        pod_info = self.make_pod_info()
        form = PodForm(data=pod_info, request=self.request)
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertEqual(discovered_pod.cores, pod.cores)
        self.assertEqual(discovered_pod.memory, pod.memory)
        self.assertEqual(discovered_pod.cpu_speed, pod.cpu_speed)
        routable_racks = [
            relation.rack_controller
            for relation in pod.routable_rack_relationships.all()
            if relation.routable
        ]
        not_routable_racks = [
            relation.rack_controller
            for relation in pod.routable_rack_relationships.all()
            if not relation.routable
        ]
        self.assertItemsEqual(routable_racks, discovered_racks)
        self.assertItemsEqual(not_routable_racks, failed_racks)

    def test_creates_pod_with_only_required(self):
        (
            discovered_pod,
            discovered_racks,
            failed_racks,
        ) = self.fake_pod_discovery()
        pod_info = self.make_pod_info()
        form = PodForm(data=pod_info, request=self.request)
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertThat(
            pod,
            MatchesStructure(
                architectures=Equals(["amd64/generic"]),
                name=MatchesAll(Not(Is(None)), Not(Equals(""))),
                cores=Equals(discovered_pod.cores),
                memory=Equals(discovered_pod.memory),
                cpu_speed=Equals(discovered_pod.cpu_speed),
                zone=Equals(Zone.objects.get_default_zone()),
                pool=Equals(ResourcePool.objects.get_default_resource_pool()),
                power_type=Equals(pod_info["type"]),
                power_parameters=Equals(
                    {
                        "power_address": pod_info["power_address"],
                        "power_pass": "",
                    }
                ),
                ip_address=MatchesStructure(ip=Equals(pod_info["ip_address"])),
            ),
        )

    def test_creates_pod_with_name(self):
        self.fake_pod_discovery()
        pod_info = self.make_pod_info()
        pod_name = factory.make_name("pod")
        pod_info["name"] = pod_name
        form = PodForm(data=pod_info, request=self.request)
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertEqual(pod_name, pod.name)

    def test_creates_pod_with_power_parameters(self):
        self.fake_pod_discovery()
        pod_info = self.make_pod_info()
        pod_info["power_pass"] = factory.make_name("pass")
        form = PodForm(data=pod_info, request=self.request)
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertEqual(
            pod_info["power_address"], pod.power_parameters["power_address"]
        )
        self.assertEqual(
            pod_info["power_pass"], pod.power_parameters["power_pass"]
        )

    def test_creates_pod_with_overcommit(self):
        self.fake_pod_discovery()
        pod_info = self.make_pod_info()
        pod_info["cpu_over_commit_ratio"] = random.randint(0, 10)
        pod_info["memory_over_commit_ratio"] = random.randint(0, 10)
        form = PodForm(data=pod_info, request=self.request)
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertEqual(
            pod_info["cpu_over_commit_ratio"], pod.cpu_over_commit_ratio
        )
        self.assertEqual(
            pod_info["memory_over_commit_ratio"], pod.memory_over_commit_ratio
        )

    def test_creates_pod_with_tags(self):
        self.fake_pod_discovery()
        pod_info = self.make_pod_info()
        tags = [
            factory.make_name("tag"),
            factory.make_name("tag"),
            "pod-console-logging",
        ]
        pod_info["tags"] = ",".join(tags)
        form = PodForm(data=pod_info, request=self.request)
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertItemsEqual(tags, pod.tags)

    def test_creates_pod_with_zone(self):
        self.fake_pod_discovery()
        pod_info = self.make_pod_info()
        zone = factory.make_Zone()
        pod_info["zone"] = zone.name
        form = PodForm(data=pod_info, request=self.request)
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertEqual(zone.id, pod.zone.id)

    def test_creates_pod_with_pool(self):
        self.fake_pod_discovery()
        pod_info = self.make_pod_info()
        pool = factory.make_ResourcePool()
        pod_info["pool"] = pool.name
        form = PodForm(data=pod_info, request=self.request)
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertEqual(pool.id, pod.pool.id)

    @wait_for_reactor
    @inlineCallbacks
    def test_creates_pod_with_in_twisted(self):
        discovered_pod, discovered_racks, failed_racks = yield deferToDatabase(
            self.fake_pod_discovery
        )
        pods_module.discover_pod.return_value = succeed(
            pods_module.discover_pod.return_value
        )
        zone = yield deferToDatabase(factory.make_Zone)
        pod_info = yield deferToDatabase(self.make_pod_info)
        pod_info["zone"] = zone.name
        pod_info["name"] = factory.make_name("pod")
        form = yield deferToDatabase(
            PodForm, data=pod_info, request=self.request
        )
        is_valid = yield deferToDatabase(form.is_valid)
        self.assertTrue(is_valid, form._errors)
        pod = yield form.save()
        self.assertThat(
            pod,
            MatchesStructure(
                architectures=Equals(["amd64/generic"]),
                name=Equals(pod_info["name"]),
                cores=Equals(discovered_pod.cores),
                memory=Equals(discovered_pod.memory),
                cpu_speed=Equals(discovered_pod.cpu_speed),
                zone=Equals(zone),
                power_type=Equals(pod_info["type"]),
                power_parameters=Equals(
                    {
                        "power_address": pod_info["power_address"],
                        "power_pass": "",
                    }
                ),
                ip_address=MatchesStructure(ip=Equals(pod_info["ip_address"])),
            ),
        )

        def validate_rack_routes():
            routable_racks = [
                relation.rack_controller
                for relation in pod.routable_rack_relationships.all()
                if relation.routable
            ]
            not_routable_racks = [
                relation.rack_controller
                for relation in pod.routable_rack_relationships.all()
                if not relation.routable
            ]
            self.assertItemsEqual(routable_racks, discovered_racks)
            self.assertItemsEqual(not_routable_racks, failed_racks)

        yield deferToDatabase(validate_rack_routes)

    @wait_for_reactor
    @inlineCallbacks
    def test_doesnt_create_pod_when_discovery_fails_in_twisted(self):
        discovered_pod, discovered_racks, failed_racks = yield deferToDatabase(
            self.fake_pod_discovery
        )
        pods_module.discover_pod.return_value = fail(factory.make_exception())
        pod_info = yield deferToDatabase(self.make_pod_info)
        form = yield deferToDatabase(
            PodForm, data=pod_info, request=self.request
        )
        is_valid = yield deferToDatabase(form.is_valid)
        self.assertTrue(is_valid, form._errors)
        with ExpectedException(PodProblem):
            yield form.save()

        def validate_no_pods():
            self.assertItemsEqual([], Pod.objects.all())

        yield deferToDatabase(validate_no_pods)

    def test_prevents_duplicate_pod(self):
        discovered_pod, _, _ = self.fake_pod_discovery()
        pod_info = self.make_pod_info()
        form = PodForm(data=pod_info, request=self.request)
        self.assertTrue(form.is_valid(), form._errors)
        form.save()
        new_form = PodForm(data=pod_info)
        self.assertTrue(new_form.is_valid(), form._errors)
        self.assertRaises(ValidationError, new_form.save)

    def test_takes_over_bmc_with_pod(self):
        discovered_pod, _, _ = self.fake_pod_discovery()
        pod_info = self.make_pod_info()
        bmc = factory.make_BMC(
            power_type=pod_info["type"],
            power_parameters={
                "power_address": pod_info["power_address"],
                "power_pass": "",
            },
        )
        form = PodForm(data=pod_info, request=self.request)
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertEquals(bmc.id, pod.id)
        self.assertEquals(BMC_TYPE.POD, reload_object(bmc).bmc_type)

    def test_updates_existing_pod_minimal(self):
        self.fake_pod_discovery()
        zone = factory.make_Zone()
        pool = factory.make_ResourcePool()
        cpu_over_commit = random.randint(0, 10)
        memory_over_commit = random.randint(0, 10)
        power_parameters = {
            "power_address": "qemu+ssh://1.2.3.4/system",
            "power_pass": "pass",
        }
        orig_pod = factory.make_Pod(
            pod_type="virsh",
            zone=zone,
            pool=pool,
            cpu_over_commit_ratio=cpu_over_commit,
            memory_over_commit_ratio=memory_over_commit,
            parameters=power_parameters,
        )
        new_name = factory.make_name("pod")
        form = PodForm(
            data={"name": new_name}, request=self.request, instance=orig_pod
        )
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertEqual(new_name, pod.name)
        self.assertEqual(zone, pod.zone)
        self.assertEqual(pool, pod.pool)
        self.assertEqual(cpu_over_commit, pod.cpu_over_commit_ratio)
        self.assertEqual(memory_over_commit, pod.memory_over_commit_ratio)
        self.assertEqual(memory_over_commit, pod.memory_over_commit_ratio)
        self.assertEqual(power_parameters, pod.power_parameters)

    def test_updates_existing_pod(self):
        (
            discovered_pod,
            discovered_racks,
            failed_racks,
        ) = self.fake_pod_discovery()
        zone = factory.make_Zone()
        pool = factory.make_ResourcePool()
        pod_info = self.make_pod_info()
        pod_info["zone"] = zone.name
        pod_info["pool"] = pool.name
        orig_pod = factory.make_Pod(pod_type=pod_info["type"])
        new_name = factory.make_name("pod")
        pod_info["name"] = new_name
        form = PodForm(data=pod_info, request=self.request, instance=orig_pod)
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertThat(
            pod,
            MatchesStructure(
                id=Equals(orig_pod.id),
                bmc_type=Equals(BMC_TYPE.POD),
                architectures=Equals(["amd64/generic"]),
                name=Equals(new_name),
                cores=Equals(discovered_pod.cores),
                memory=Equals(discovered_pod.memory),
                cpu_speed=Equals(discovered_pod.cpu_speed),
                zone=Equals(zone),
                pool=Equals(pool),
                power_type=Equals(pod_info["type"]),
                power_parameters=Equals(
                    {
                        "power_address": pod_info["power_address"],
                        "power_pass": "",
                    }
                ),
                ip_address=MatchesStructure(ip=Equals(pod_info["ip_address"])),
            ),
        )
        routable_racks = [
            relation.rack_controller
            for relation in pod.routable_rack_relationships.all()
            if relation.routable
        ]
        not_routable_racks = [
            relation.rack_controller
            for relation in pod.routable_rack_relationships.all()
            if not relation.routable
        ]
        self.assertItemsEqual(routable_racks, discovered_racks)
        self.assertItemsEqual(not_routable_racks, failed_racks)

    def test_updates_default_storage_pool(self):
        discovered_pod, _, _ = self.fake_pod_discovery()
        default_storage_pool = random.choice(discovered_pod.storage_pools)
        pod = factory.make_Pod(pod_type="virsh")
        pod.sync(discovered_pod, self.request.user)
        form = PodForm(
            data={
                "default_storage_pool": default_storage_pool.id,
                "power_address": "qemu:///system",
            },
            request=self.request,
            instance=pod,
        )
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertThat(
            pod,
            MatchesStructure(
                default_storage_pool=MatchesStructure(
                    pool_id=Equals(default_storage_pool.id)
                )
            ),
        )

    def test_updates_default_macvlan_mode(self):
        discovered_pod, _, _ = self.fake_pod_discovery()
        default_macvlan_mode = factory.pick_choice(MACVLAN_MODE_CHOICES)
        pod = factory.make_Pod(pod_type="virsh")
        pod.sync(discovered_pod, self.request.user)
        form = PodForm(
            data={"default_macvlan_mode": default_macvlan_mode},
            request=self.request,
            instance=pod,
        )
        self.assertTrue(form.is_valid(), form._errors)
        pod = form.save()
        self.assertThat(
            pod,
            MatchesStructure(
                default_macvlan_mode=Equals(default_macvlan_mode)
            ),
        )

    @wait_for_reactor
    @inlineCallbacks
    def test_updates_existing_pod_in_twisted(self):
        discovered_pod, discovered_racks, failed_racks = yield deferToDatabase(
            self.fake_pod_discovery
        )
        pods_module.discover_pod.return_value = succeed(
            pods_module.discover_pod.return_value
        )
        zone = yield deferToDatabase(factory.make_Zone)
        pool = yield deferToDatabase(factory.make_ResourcePool)
        pod_info = yield deferToDatabase(self.make_pod_info)
        pod_info["zone"] = zone.name
        pod_info["pool"] = pool.name
        orig_pod = yield deferToDatabase(
            factory.make_Pod, pod_type=pod_info["type"]
        )
        new_name = factory.make_name("pod")
        pod_info["name"] = new_name
        form = yield deferToDatabase(
            PodForm, data=pod_info, request=self.request, instance=orig_pod
        )
        is_valid = yield deferToDatabase(form.is_valid)
        self.assertTrue(is_valid, form._errors)
        pod = yield form.save()
        self.assertThat(
            pod,
            MatchesStructure(
                id=Equals(orig_pod.id),
                bmc_type=Equals(BMC_TYPE.POD),
                architectures=Equals(["amd64/generic"]),
                name=Equals(new_name),
                cores=Equals(discovered_pod.cores),
                memory=Equals(discovered_pod.memory),
                cpu_speed=Equals(discovered_pod.cpu_speed),
                zone=Equals(zone),
                power_type=Equals(pod_info["type"]),
                power_parameters=Equals(
                    {
                        "power_address": pod_info["power_address"],
                        "power_pass": "",
                    }
                ),
                ip_address=MatchesStructure(ip=Equals(pod_info["ip_address"])),
            ),
        )

        def validate_rack_routes():
            routable_racks = [
                relation.rack_controller
                for relation in pod.routable_rack_relationships.all()
                if relation.routable
            ]
            not_routable_racks = [
                relation.rack_controller
                for relation in pod.routable_rack_relationships.all()
                if not relation.routable
            ]
            self.assertItemsEqual(routable_racks, discovered_racks)
            self.assertItemsEqual(not_routable_racks, failed_racks)

        yield deferToDatabase(validate_rack_routes)

    @wait_for_reactor
    @inlineCallbacks
    def test_updates_default_storage_pool_in_twisted(self):
        discovered_pod, _, _ = yield deferToDatabase(self.fake_pod_discovery)
        pods_module.discover_pod.return_value = succeed(
            pods_module.discover_pod.return_value
        )
        default_storage_pool = random.choice(discovered_pod.storage_pools)
        pod = yield deferToDatabase(factory.make_Pod, pod_type="virsh")
        yield deferToDatabase(pod.sync, discovered_pod, self.request.user)
        form = yield deferToDatabase(
            PodForm,
            data={
                "default_storage_pool": default_storage_pool.id,
                "power_address": "qemu:///system",
            },
            request=self.request,
            instance=pod,
        )
        is_valid = yield deferToDatabase(form.is_valid)
        self.assertTrue(is_valid, form._errors)
        pod = yield form.save()
        self.assertThat(
            pod,
            MatchesStructure(
                default_storage_pool=MatchesStructure(
                    pool_id=Equals(default_storage_pool.id)
                )
            ),
        )

    @wait_for_reactor
    @inlineCallbacks
    def test_updates_default_macvlan_mode_in_twisted(self):
        discovered_pod, _, _ = yield deferToDatabase(self.fake_pod_discovery)
        pods_module.discover_pod.return_value = succeed(
            pods_module.discover_pod.return_value
        )
        default_macvlan_mode = factory.pick_choice(MACVLAN_MODE_CHOICES)
        pod = yield deferToDatabase(factory.make_Pod, pod_type="virsh")
        yield deferToDatabase(pod.sync, discovered_pod, self.request.user)
        form = yield deferToDatabase(
            PodForm,
            data={"default_macvlan_mode": default_macvlan_mode},
            request=self.request,
            instance=pod,
        )
        is_valid = yield deferToDatabase(form.is_valid)
        self.assertTrue(is_valid, form._errors)
        pod = yield form.save()
        self.assertThat(
            pod,
            MatchesStructure(
                default_macvlan_mode=Equals(default_macvlan_mode)
            ),
        )

    def test_discover_and_sync_existing_pod(self):
        (
            discovered_pod,
            discovered_racks,
            failed_racks,
        ) = self.fake_pod_discovery()
        zone = factory.make_Zone()
        pod_info = self.make_pod_info()
        power_parameters = {"power_address": pod_info["power_address"]}
        orig_pod = factory.make_Pod(
            zone=zone, pod_type=pod_info["type"], parameters=power_parameters
        )
        form = PodForm(data=pod_info, request=self.request, instance=orig_pod)
        pod = form.discover_and_sync_pod()
        self.assertThat(
            pod,
            MatchesStructure(
                id=Equals(orig_pod.id),
                bmc_type=Equals(BMC_TYPE.POD),
                architectures=Equals(["amd64/generic"]),
                name=Equals(orig_pod.name),
                cores=Equals(discovered_pod.cores),
                memory=Equals(discovered_pod.memory),
                cpu_speed=Equals(discovered_pod.cpu_speed),
                zone=Equals(zone),
                power_type=Equals(pod_info["type"]),
                power_parameters=Equals(power_parameters),
                ip_address=MatchesStructure(ip=Equals(pod_info["ip_address"])),
            ),
        )
        routable_racks = [
            relation.rack_controller
            for relation in pod.routable_rack_relationships.all()
            if relation.routable
        ]
        not_routable_racks = [
            relation.rack_controller
            for relation in pod.routable_rack_relationships.all()
            if not relation.routable
        ]
        self.assertItemsEqual(routable_racks, discovered_racks)
        self.assertItemsEqual(not_routable_racks, failed_racks)

    @wait_for_reactor
    @inlineCallbacks
    def test_discover_and_sync_existing_pod_in_twisted(self):
        discovered_pod, discovered_racks, failed_racks = yield deferToDatabase(
            self.fake_pod_discovery
        )
        pods_module.discover_pod.return_value = succeed(
            pods_module.discover_pod.return_value
        )
        zone = yield deferToDatabase(factory.make_Zone)
        pod_info = yield deferToDatabase(self.make_pod_info)
        power_parameters = {"power_address": pod_info["power_address"]}
        orig_pod = yield deferToDatabase(
            factory.make_Pod,
            zone=zone,
            pod_type=pod_info["type"],
            parameters=power_parameters,
        )
        form = yield deferToDatabase(
            PodForm, data=pod_info, request=self.request, instance=orig_pod
        )
        pod = yield form.discover_and_sync_pod()
        self.assertThat(
            pod,
            MatchesStructure(
                id=Equals(orig_pod.id),
                bmc_type=Equals(BMC_TYPE.POD),
                architectures=Equals(["amd64/generic"]),
                name=Equals(orig_pod.name),
                cores=Equals(discovered_pod.cores),
                memory=Equals(discovered_pod.memory),
                cpu_speed=Equals(discovered_pod.cpu_speed),
                zone=Equals(zone),
                power_type=Equals(pod_info["type"]),
                power_parameters=Equals(power_parameters),
                ip_address=MatchesStructure(ip=Equals(pod_info["ip_address"])),
            ),
        )

        def validate_rack_routes():
            routable_racks = [
                relation.rack_controller
                for relation in pod.routable_rack_relationships.all()
                if relation.routable
            ]
            not_routable_racks = [
                relation.rack_controller
                for relation in pod.routable_rack_relationships.all()
                if not relation.routable
            ]
            self.assertItemsEqual(routable_racks, discovered_racks)
            self.assertItemsEqual(not_routable_racks, failed_racks)

        yield deferToDatabase(validate_rack_routes)

    def test_raises_unable_to_discover_because_no_racks(self):
        self.patch(pods_module, "discover_pod").return_value = ({}, {})
        pod_info = self.make_pod_info()
        form = PodForm(data=pod_info)
        self.assertTrue(form.is_valid(), form._errors)
        error = self.assertRaises(PodProblem, form.save)
        self.assertEquals(
            "Unable to start the pod discovery process. "
            "No rack controllers connected.",
            str(error),
        )

    @wait_for_reactor
    @inlineCallbacks
    def test_raises_unable_to_discover_because_no_racks_in_twisted(self):
        self.patch(pods_module, "discover_pod").return_value = succeed(
            ({}, {})
        )
        pod_info = yield deferToDatabase(self.make_pod_info)
        form = yield deferToDatabase(PodForm, data=pod_info)
        is_valid = yield deferToDatabase(form.is_valid)
        self.assertTrue(is_valid, form._errors)

        def validate_error(failure):
            self.assertIsInstance(failure.value, PodProblem)
            self.assertEquals(
                "Unable to start the pod discovery process. "
                "No rack controllers connected.",
                str(failure.value),
            )

        d = form.save()
        d.addErrback(validate_error)
        yield d

    def test_raises_exception_from_rack_controller(self):
        failed_rack = factory.make_RackController()
        exc = factory.make_exception()
        self.patch(pods_module, "discover_pod").return_value = (
            {},
            {failed_rack.system_id: exc},
        )
        pod_info = self.make_pod_info()
        form = PodForm(data=pod_info)
        self.assertTrue(form.is_valid(), form._errors)
        error = self.assertRaises(PodProblem, form.save)
        self.assertEquals(str(exc), str(error))

    @wait_for_reactor
    @inlineCallbacks
    def test_raises_exception_from_rack_controller_in_twisted(self):
        failed_rack = yield deferToDatabase(factory.make_RackController)
        exc = factory.make_exception()
        self.patch(pods_module, "discover_pod").return_value = succeed(
            ({}, {failed_rack.system_id: exc})
        )
        pod_info = yield deferToDatabase(self.make_pod_info)
        form = yield deferToDatabase(PodForm, data=pod_info)
        is_valid = yield deferToDatabase(form.is_valid)
        self.assertTrue(is_valid, form._errors)

        def validate_error(failure):
            self.assertIsInstance(failure.value, PodProblem)
            self.assertEquals(str(exc), str(failure.value))

        d = form.save()
        d.addErrback(validate_error)
        yield d


class TestComposeMachineForm(MAASTransactionServerTestCase):
    def make_requested_machine_result(self, pod):
        return RequestedMachine(
            hostname=factory.make_name("hostname"),
            architecture=factory.make_name("architecture"),
            cores=random.randint(1, 8),
            memory=random.randint(1024, 4096),
            block_devices=[RequestedMachineBlockDevice(size=4096)],
            interfaces=[RequestedMachineInterface()],
            known_host_interfaces=make_known_host_interfaces(pod),
        )

    def make_compose_machine_result(self, pod):
        composed_machine = DiscoveredMachine(
            hostname=factory.make_name("hostname"),
            architecture=pod.architectures[0],
            cores=1,
            memory=1024,
            cpu_speed=300,
            block_devices=[],
            interfaces=[],
        )
        pod_hints = DiscoveredPodHints(
            cores=random.randint(0, 10),
            memory=random.randint(1024, 4096),
            cpu_speed=random.randint(1000, 3000),
            local_storage=0,
        )
        return composed_machine, pod_hints

    def test__requires_request_kwarg(self):
        error = self.assertRaises(ValueError, ComposeMachineForm)
        self.assertEqual("'request' kwargs is required.", str(error))

    def test__requires_pod_kwarg(self):
        request = MagicMock()
        error = self.assertRaises(
            ValueError, ComposeMachineForm, request=request
        )
        self.assertEqual("'pod' kwargs is required.", str(error))

    def test__sets_up_fields_based_on_pod(self):
        request = MagicMock()
        pod = make_pod_with_hints()
        form = ComposeMachineForm(request=request, pod=pod)
        self.assertThat(
            form.fields["cores"],
            MatchesStructure(
                required=Equals(False),
                validators=MatchesSetwise(
                    MatchesAll(
                        IsInstance(MaxValueValidator),
                        MatchesStructure(limit_value=Equals(pod.hints.cores)),
                    ),
                    MatchesAll(
                        IsInstance(MinValueValidator),
                        MatchesStructure(limit_value=Equals(1)),
                    ),
                ),
            ),
        )
        self.assertThat(
            form.fields["memory"],
            MatchesStructure(
                required=Equals(False),
                validators=MatchesSetwise(
                    MatchesAll(
                        IsInstance(MaxValueValidator),
                        MatchesStructure(limit_value=Equals(pod.hints.memory)),
                    ),
                    MatchesAll(
                        IsInstance(MinValueValidator),
                        MatchesStructure(limit_value=Equals(1024)),
                    ),
                ),
            ),
        )
        self.assertThat(
            form.fields["architecture"],
            MatchesStructure(
                required=Equals(False),
                choices=MatchesSetwise(
                    *[
                        Equals((architecture, architecture))
                        for architecture in pod.architectures
                    ]
                ),
            ),
        )
        self.assertThat(
            form.fields["cpu_speed"],
            MatchesStructure(
                required=Equals(False),
                validators=MatchesSetwise(
                    MatchesAll(
                        IsInstance(MaxValueValidator),
                        MatchesStructure(
                            limit_value=Equals(pod.hints.cpu_speed)
                        ),
                    ),
                    MatchesAll(
                        IsInstance(MinValueValidator),
                        MatchesStructure(limit_value=Equals(300)),
                    ),
                ),
            ),
        )

    def test__sets_up_fields_based_on_pod_no_max_cpu_speed(self):
        request = MagicMock()
        pod = make_pod_with_hints()
        pod.hints.cpu_speed = 0
        pod.save()
        form = ComposeMachineForm(request=request, pod=pod)
        self.assertThat(
            form.fields["cpu_speed"],
            MatchesStructure(
                required=Equals(False),
                validators=MatchesSetwise(
                    MatchesAll(
                        IsInstance(MinValueValidator),
                        MatchesStructure(limit_value=Equals(300)),
                    )
                ),
            ),
        )

    def test__sets_up_pool_default(self):
        request = MagicMock()
        pod = make_pod_with_hints()
        pool = factory.make_ResourcePool()
        pod.pool = pool
        pod.save()
        form = ComposeMachineForm(request=request, pod=pod)
        self.assertEqual(pool, form.initial["pool"])

    def test__get_machine_uses_all_initial_values(self):
        request = MagicMock()
        pod = make_pod_with_hints()
        form = ComposeMachineForm(data={}, request=request, pod=pod)
        self.assertTrue(form.is_valid())
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    architecture=Equals(pod.architectures[0]),
                    cores=Equals(1),
                    memory=Equals(1024),
                    cpu_speed=Is(None),
                    block_devices=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineBlockDevice),
                                MatchesStructure(size=Equals(8 * (1000 ** 3))),
                            )
                        ]
                    ),
                    interfaces=MatchesListwise(
                        [IsInstance(RequestedMachineInterface)]
                    ),
                    known_host_interfaces=MatchesListwise([]),
                ),
            ),
        )

    def test__get_machine_uses_passed_values(self):
        request = MagicMock()
        pod = make_pod_with_hints()
        architecture = random.choice(pod.architectures)
        cores = random.randint(1, pod.hints.cores)
        memory = random.randint(1024, pod.hints.memory)
        cpu_speed = random.randint(300, pod.hints.cpu_speed)
        disk_1 = random.randint(8, 16) * (1000 ** 3)
        disk_1_tags = [factory.make_name("tag") for _ in range(3)]
        disk_2 = random.randint(8, 16) * (1000 ** 3)
        disk_2_tags = [factory.make_name("tag") for _ in range(3)]
        storage = "root:%d(%s),extra:%d(%s)" % (
            disk_1 // (1000 ** 3),
            ",".join(disk_1_tags),
            disk_2 // (1000 ** 3),
            ",".join(disk_2_tags),
        )
        form = ComposeMachineForm(
            data={
                "architecture": architecture,
                "cores": cores,
                "memory": memory,
                "cpu_speed": cpu_speed,
                "storage": storage,
            },
            request=request,
            pod=pod,
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    architecture=Equals(architecture),
                    cores=Equals(cores),
                    memory=Equals(memory),
                    cpu_speed=Equals(cpu_speed),
                    block_devices=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineBlockDevice),
                                MatchesStructure(
                                    size=Equals(disk_1),
                                    tags=Equals(disk_1_tags),
                                ),
                            ),
                            MatchesAll(
                                IsInstance(RequestedMachineBlockDevice),
                                MatchesStructure(
                                    size=Equals(disk_2),
                                    tags=Equals(disk_2_tags),
                                ),
                            ),
                        ]
                    ),
                    interfaces=MatchesListwise(
                        [IsInstance(RequestedMachineInterface)]
                    ),
                    known_host_interfaces=MatchesListwise([]),
                ),
            ),
        )

    def test__get_machine_handles_no_tags_in_storage(self):
        request = MagicMock()
        pod = make_pod_with_hints()
        disk_1 = random.randint(8, 16) * (1000 ** 3)
        disk_2 = random.randint(8, 16) * (1000 ** 3)
        storage = "root:%d,extra:%d" % (
            disk_1 // (1000 ** 3),
            disk_2 // (1000 ** 3),
        )
        form = ComposeMachineForm(
            data={"storage": storage}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    block_devices=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineBlockDevice),
                                MatchesStructure(
                                    size=Equals(disk_1), tags=Equals([])
                                ),
                            ),
                            MatchesAll(
                                IsInstance(RequestedMachineBlockDevice),
                                MatchesStructure(
                                    size=Equals(disk_2), tags=Equals([])
                                ),
                            ),
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_interfaces_fails_no_dhcp_for_vlan(self):
        request = MagicMock()
        pod_host = factory.make_Machine_with_Interface_on_Subnet()
        pod_host.boot_interface.vlan.dhcp_on = False
        pod_host.boot_interface.vlan.save()
        pod = make_pod_with_hints(host=pod_host)
        interfaces = "eth0:subnet=%s" % (
            pod_host.boot_interface.vlan.subnet_set.first().cidr
        )
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        with ExpectedException(
            ValidationError, ".*DHCP must be enabled on at least one VLAN*"
        ):
            form.get_requested_machine(make_known_host_interfaces(pod))

    def test__get_machine_with_interfaces_fails_for_no_matching_network(self):
        request = MagicMock()
        pod = make_pod_with_hints(with_host=True)
        # Make a subnet that won't match the host via the constraint.
        subnet = factory.make_Subnet()
        interfaces = "eth0:subnet=%s" % (subnet.cidr)
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        with ExpectedException(
            ValidationError, ".*does not match the specified network.*"
        ):
            form.get_requested_machine(make_known_host_interfaces(pod))

    def test__get_machine_with_interfaces_by_subnet(self):
        request = MagicMock()
        pod_host = factory.make_Machine_with_Interface_on_Subnet()
        space = factory.make_Space("dmz")
        pod_host.boot_interface.vlan.space = space
        pod_host.boot_interface.vlan.save()
        pod = make_pod_with_hints(host=pod_host)
        # Test with a numeric label, since that's what Juju will pass in.
        interfaces = "0:subnet=%s" % (
            pod_host.boot_interface.vlan.subnet_set.first().cidr
        )
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    # Make sure the numeric label gets converted
                                    # to a sane interface name.
                                    ifname=Equals("eth0"),
                                    attach_name=Equals(
                                        pod_host.boot_interface.name
                                    ),
                                    attach_type=Equals(
                                        InterfaceAttachType.MACVLAN
                                    ),
                                    attach_options=Equals(MACVLAN_MODE.BRIDGE),
                                ),
                            )
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_interfaces_with_empty_interfaces_input(self):
        request = MagicMock()
        host = factory.make_Machine_with_Interface_on_Subnet()
        space = factory.make_Space("dmz")
        host.boot_interface.vlan.space = space
        host.boot_interface.vlan.save()
        pod = make_pod_with_hints()
        pod.ip_address = host.boot_interface.ip_addresses.first()
        pod.save()
        form = ComposeMachineForm(
            data=dict(interfaces=""), request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Is(None),
                                    attach_type=Is(None),
                                    attach_options=Is(None),
                                ),
                            )
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_interfaces_with_unreserved_ip(self):
        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        subnet = factory.make_Subnet()
        host = factory.make_Machine_with_Interface_on_Subnet(subnet=subnet)
        space = factory.make_Space("dmz")
        host.boot_interface.vlan.dhcp_on = True
        host.boot_interface.vlan.space = space
        host.boot_interface.vlan.save()
        pod = make_pod_with_hints(host=host)

        # Mock start_commissioning so it doesn't use post commit hooks.
        self.patch(Machine, "start_commissioning")

        # Mock the result of the composed machine.
        composed_machine, pod_hints = self.make_compose_machine_result(pod)
        composed_machine.interfaces = [
            DiscoveredMachineInterface(
                mac_address="00:01:02:03:04:05", boot=True
            )
        ]
        mock_compose_machine = self.patch(pods_module, "compose_machine")
        mock_compose_machine.return_value = succeed(
            (composed_machine, pod_hints)
        )
        request = MagicMock()
        ip = factory.make_StaticIPAddress(
            interface=host.get_boot_interface(), subnet=subnet
        )
        expected_ip = str(ip.ip)
        ip.delete()
        interfaces = "eth0:ip=%s" % expected_ip
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    requested_ips=Equals(["%s" % expected_ip]),
                                    ifname=Equals("eth0"),
                                    attach_name=Equals(
                                        host.boot_interface.name
                                    ),
                                    attach_type=Equals(
                                        InterfaceAttachType.MACVLAN
                                    ),
                                    attach_options=Equals(MACVLAN_MODE.BRIDGE),
                                ),
                            )
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_interfaces_by_subnet_with_default_mode(self):
        request = MagicMock()
        pod_host = factory.make_Machine_with_Interface_on_Subnet()
        space = factory.make_Space("dmz")
        pod_host.boot_interface.vlan.space = space
        pod_host.boot_interface.vlan.save()
        pod = make_pod_with_hints(host=pod_host)
        attach_mode = factory.pick_choice(MACVLAN_MODE_CHOICES)
        pod.default_macvlan_mode = attach_mode
        pod.save()
        interfaces = "eth0:subnet=%s" % (
            pod_host.boot_interface.vlan.subnet_set.first().cidr
        )
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(
                                        pod_host.boot_interface.name
                                    ),
                                    attach_type=Equals(
                                        InterfaceAttachType.MACVLAN
                                    ),
                                    attach_options=Equals(attach_mode),
                                ),
                            )
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_interfaces_by_subnet_with_empty_mode(self):
        request = MagicMock()
        pod_host = factory.make_Machine_with_Interface_on_Subnet()
        space = factory.make_Space("dmz")
        pod_host.boot_interface.vlan.space = space
        pod_host.boot_interface.vlan.save()
        pod = make_pod_with_hints(host=pod_host)
        # We expect the macvlan mode to be the default choice...
        attach_mode = MACVLAN_MODE_CHOICES[0][1]
        # ... when the macvlan mode is set to the empty string.
        pod.default_macvlan_mode = ""
        pod.save()
        interfaces = "eth0:subnet=%s" % (
            pod_host.boot_interface.vlan.subnet_set.first().cidr
        )
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(
                                        pod_host.boot_interface.name
                                    ),
                                    attach_type=Equals(
                                        InterfaceAttachType.MACVLAN
                                    ),
                                    attach_options=Equals(attach_mode),
                                ),
                            )
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_interfaces_by_space(self):
        request = MagicMock()
        pod_host = factory.make_Machine_with_Interface_on_Subnet()
        space = factory.make_Space("dmz")
        pod_host.boot_interface.vlan.space = space
        pod_host.boot_interface.vlan.save()
        pod = make_pod_with_hints(host=pod_host)
        interfaces = "eth0:space=dmz"
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(
                                        pod.host.boot_interface.name
                                    ),
                                    attach_type=Equals(
                                        InterfaceAttachType.MACVLAN
                                    ),
                                    attach_options=Equals(MACVLAN_MODE.BRIDGE),
                                ),
                            )
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_interfaces_by_spaces(self):
        request = MagicMock()
        pod_host = factory.make_Machine_with_Interface_on_Subnet()
        dmz_space = factory.make_Space("dmz")
        storage_space = factory.make_Space("storage")
        # Because PXE booting from the DMZ is /always/ a great idea. ;-)
        pod_host.boot_interface.vlan.space = dmz_space
        pod_host.boot_interface.vlan.save()
        storage_vlan = factory.make_VLAN(space=storage_space, dhcp_on=True)
        storage_if = factory.make_Interface(node=pod_host, vlan=storage_vlan)
        pod = make_pod_with_hints(host=pod_host)
        interfaces = "eth0:space=dmz;eth1:space=storage"
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(
                                        pod_host.boot_interface.name
                                    ),
                                    attach_type=Equals(
                                        InterfaceAttachType.MACVLAN
                                    ),
                                    attach_options=Equals(MACVLAN_MODE.BRIDGE),
                                ),
                            ),
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(storage_if.name),
                                    attach_type=Equals(
                                        InterfaceAttachType.MACVLAN
                                    ),
                                    attach_options=Equals(MACVLAN_MODE.BRIDGE),
                                ),
                            ),
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_interfaces_by_subnets_bridge(self):
        request = MagicMock()
        cidr1 = "10.0.0.0/24"
        cidr2 = "192.168.100.0/24"
        vlan = factory.make_VLAN(dhcp_on=True)
        subnet = factory.make_Subnet(cidr=cidr2, vlan=vlan)
        pod_host = factory.make_Machine_with_Interface_on_Subnet(cidr=cidr1)
        space = factory.make_Space("dmz")
        pod_host.boot_interface.vlan.space = space
        pod_host.boot_interface.vlan.save()

        # Create a bridge and non-bridge on the pod_host
        bridge = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE,
            node=pod_host,
            subnet=pod_host.boot_interface.vlan.subnet_set.first(),
        )
        non_bridge = factory.make_Interface(node=pod_host, subnet=subnet)

        pod = make_pod_with_hints(host=pod_host)
        interfaces = "eth0:subnet=%s;eth1:subnet=%s" % (cidr1, cidr2)
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(bridge.name),
                                    attach_type=Equals(
                                        InterfaceAttachType.BRIDGE
                                    ),
                                    attach_options=Equals(None),
                                ),
                            ),
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(non_bridge.name),
                                    attach_type=Equals(
                                        InterfaceAttachType.MACVLAN
                                    ),
                                    attach_options=Equals(MACVLAN_MODE.BRIDGE),
                                ),
                            ),
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_interfaces_by_subnets_bond(self):
        request = MagicMock()
        cidr1 = "10.0.0.0/24"
        cidr2 = "192.168.100.0/24"
        vlan = factory.make_VLAN(dhcp_on=True)
        subnet = factory.make_Subnet(cidr=cidr2, vlan=vlan)
        pod_host = factory.make_Machine_with_Interface_on_Subnet(cidr=cidr1)
        space = factory.make_Space("dmz")
        pod_host.boot_interface.vlan.space = space
        pod_host.boot_interface.vlan.save()

        # Create a bond and non-bond on the pod_host
        bond_if = factory.make_Interface(
            node=pod_host,
            subnet=pod_host.boot_interface.vlan.subnet_set.first(),
        )
        bond = factory.make_Interface(
            iftype=INTERFACE_TYPE.BOND,
            node=pod_host,
            parents=[pod_host.boot_interface, bond_if],
            subnet=pod_host.boot_interface.vlan.subnet_set.first(),
        )
        non_bond = factory.make_Interface(node=pod_host, subnet=subnet)

        pod = make_pod_with_hints(host=pod_host)
        interfaces = "eth0:subnet=%s;eth1:subnet=%s" % (cidr1, cidr2)
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(bond.name),
                                    attach_type=Equals(
                                        InterfaceAttachType.MACVLAN
                                    ),
                                    attach_options=Equals(MACVLAN_MODE.BRIDGE),
                                ),
                            ),
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(non_bond.name),
                                    attach_type=Equals(
                                        InterfaceAttachType.MACVLAN
                                    ),
                                    attach_options=Equals(MACVLAN_MODE.BRIDGE),
                                ),
                            ),
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_interfaces_by_subnets_bond_inside_bridge(self):
        request = MagicMock()
        cidr1 = "10.0.0.0/24"
        cidr2 = "192.168.100.0/24"
        vlan = factory.make_VLAN(dhcp_on=True)
        subnet = factory.make_Subnet(cidr=cidr2, vlan=vlan)
        pod_host = factory.make_Machine_with_Interface_on_Subnet(cidr=cidr1)
        space = factory.make_Space("dmz")
        pod_host.boot_interface.vlan.space = space
        pod_host.boot_interface.vlan.save()

        # Create a bond and non-bond on the pod_host
        bond_if = factory.make_Interface(
            node=pod_host,
            subnet=pod_host.boot_interface.vlan.subnet_set.first(),
        )
        bond = factory.make_Interface(
            iftype=INTERFACE_TYPE.BOND,
            node=pod_host,
            parents=[pod_host.boot_interface, bond_if],
            subnet=pod_host.boot_interface.vlan.subnet_set.first(),
        )
        non_bond = factory.make_Interface(node=pod_host, subnet=subnet)
        # Create bridge using the bond
        bridge = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE,
            node=pod_host,
            parents=[bond],
            subnet=pod_host.boot_interface.vlan.subnet_set.first(),
        )

        pod = make_pod_with_hints(host=pod_host)
        interfaces = "eth0:subnet=%s;eth1:subnet=%s" % (cidr1, cidr2)
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(bridge.name),
                                    attach_type=Equals(
                                        InterfaceAttachType.BRIDGE
                                    ),
                                    attach_options=Equals(None),
                                ),
                            ),
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(non_bond.name),
                                    attach_type=Equals(
                                        InterfaceAttachType.MACVLAN
                                    ),
                                    attach_options=Equals(MACVLAN_MODE.BRIDGE),
                                ),
                            ),
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_interfaces_by_space_as_bridge(self):
        request = MagicMock()
        pod_host = factory.make_Machine_with_Interface_on_Subnet(
            status=NODE_STATUS.READY
        )
        space = factory.make_Space("dmz")
        pod_host.boot_interface.vlan.space = space
        pod_host.boot_interface.vlan.save()
        # This is just to make sure a bridge will be available for attachment.
        # We expect bridge mode to be preferred, when available.
        pod_host.acquire(factory.make_User(), bridge_all=True)
        pod = make_pod_with_hints(host=pod_host)
        interfaces = "eth0:space=dmz"
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(RequestedMachineInterface),
                                MatchesStructure(
                                    attach_name=Equals(
                                        "br-" + pod_host.boot_interface.name
                                    ),
                                    attach_type=Equals(
                                        InterfaceAttachType.BRIDGE
                                    ),
                                    attach_options=Is(None),
                                ),
                            )
                        ]
                    )
                ),
            ),
        )

    def test__get_machine_with_known_host_interfaces(self):
        # Need to test that it can actually find the pod host's data correctly
        # and that this matches what is expected.
        request = MagicMock()
        pod_host = factory.make_Machine_with_Interface_on_Subnet(
            interface_count=3
        )
        pod = make_pod_with_hints(host=pod_host)
        form = ComposeMachineForm(data={}, request=request, pod=pod)
        self.assertTrue(form.is_valid(), form.errors)
        request_machine = form.get_requested_machine(
            make_known_host_interfaces(pod)
        )
        self.assertThat(
            request_machine,
            MatchesAll(
                IsInstance(RequestedMachine),
                MatchesStructure(
                    known_host_interfaces=MatchesListwise(
                        [
                            MatchesAll(
                                IsInstance(KnownHostInterface),
                                MatchesStructure(
                                    ifname=Equals(interface.name),
                                    attach_type=Equals(
                                        InterfaceAttachType.BRIDGE
                                        if interface.type
                                        == INTERFACE_TYPE.BRIDGE
                                        else InterfaceAttachType.MACVLAN
                                    ),
                                    dhcp_enabled=Equals(
                                        interface.vlan.dhcp_on
                                        or interface.vlan.relay_vlan.dhcp_on
                                    ),
                                ),
                            )
                            for interface in pod.host.interface_set.all()
                        ]
                    )
                ),
            ),
        )

    def test__compose_with_interfaces_with_reserved_ip_fails(self):
        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        pod = make_pod_with_hints(with_host=True)

        # Mock start_commissioning so it doesn't use post commit hooks.
        self.patch(Machine, "start_commissioning")

        # Mock the result of the composed machine.
        composed_machine, pod_hints = self.make_compose_machine_result(pod)
        composed_machine.interfaces = [
            DiscoveredMachineInterface(mac_address="00:01:02:03:04:05")
        ]
        mock_compose_machine = self.patch(pods_module, "compose_machine")
        mock_compose_machine.return_value = succeed(
            (composed_machine, pod_hints)
        )

        request = MagicMock()
        subnet = pod.host.boot_interface.ip_addresses.first().subnet
        host = factory.make_Machine_with_Interface_on_Subnet(subnet=subnet)
        space = factory.make_Space("dmz")
        host.boot_interface.vlan.dhcp_on = True
        host.boot_interface.vlan.space = space
        host.boot_interface.vlan.save()
        ip = factory.make_StaticIPAddress(
            interface=host.get_boot_interface(), subnet=subnet
        )
        pod.ip_address = host.boot_interface.ip_addresses.first()
        pod.save()
        interfaces = "eth0:ip=%s" % str(ip.ip)
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        with ExpectedException(StaticIPAddressUnavailable):
            form.compose()

    def test__compose_with_interfaces_with_unreserved_ip(self):
        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        pod = make_pod_with_hints(with_host=True)

        # Mock start_commissioning so it doesn't use post commit hooks.
        self.patch(Machine, "start_commissioning")

        # Mock the result of the composed machine.
        composed_machine, pod_hints = self.make_compose_machine_result(pod)
        composed_machine.interfaces = [
            DiscoveredMachineInterface(
                mac_address="00:01:02:03:04:05", boot=True
            )
        ]
        mock_compose_machine = self.patch(pods_module, "compose_machine")
        mock_compose_machine.return_value = succeed(
            (composed_machine, pod_hints)
        )
        request = MagicMock()
        subnet = pod.host.boot_interface.ip_addresses.first().subnet
        host = factory.make_Machine_with_Interface_on_Subnet(subnet=subnet)
        space = factory.make_Space("dmz")
        host.boot_interface.vlan.dhcp_on = True
        host.boot_interface.vlan.space = space
        host.boot_interface.vlan.save()
        ip = factory.make_StaticIPAddress(
            interface=host.get_boot_interface(), subnet=subnet
        )
        expected_ip = str(ip.ip)
        ip.delete()
        pod.ip_address = host.boot_interface.ip_addresses.first()
        pod.save()
        interfaces = "eth0:ip=%s" % expected_ip
        form = ComposeMachineForm(
            data={"interfaces": interfaces}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid(), form.errors)
        machine = form.compose()
        ip = StaticIPAddress.objects.filter(ip=expected_ip).first()
        self.assertThat(ip.get_interface().node, Equals(machine))

    def test__compose_with_commissioning(self):
        request = MagicMock()
        pod = make_pod_with_hints()

        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        # Mock the result of the composed machine.
        composed_machine, pod_hints = self.make_compose_machine_result(pod)
        mock_compose_machine = self.patch(pods_module, "compose_machine")
        mock_compose_machine.return_value = succeed(
            (composed_machine, pod_hints)
        )

        # Mock start_commissioning so it doesn't use post commit hooks.
        mock_commissioning = self.patch(Machine, "start_commissioning")

        form = ComposeMachineForm(data={}, request=request, pod=pod)
        self.assertTrue(form.is_valid())
        created_machine = form.compose()
        self.assertThat(
            created_machine,
            MatchesAll(
                IsInstance(Machine),
                MatchesStructure(
                    cpu_count=Equals(1),
                    memory=Equals(1024),
                    cpu_speed=Equals(300),
                ),
            ),
        )
        self.assertThat(mock_commissioning, MockCalledOnce())

    def test__compose_sends_default_storage_pool_id(self):
        request = MagicMock()
        pod = make_pod_with_hints()

        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        # Mock the result of the composed machine.
        composed_machine, pod_hints = self.make_compose_machine_result(pod)
        mock_compose_machine = self.patch(pods_module, "compose_machine")
        mock_compose_machine.return_value = succeed(
            (composed_machine, pod_hints)
        )

        # Mock start_commissioning so it doesn't use post commit hooks.
        self.patch(Machine, "start_commissioning")

        form = ComposeMachineForm(data={}, request=request, pod=pod)
        self.assertTrue(form.is_valid())
        form.compose()
        self.assertThat(
            mock_compose_machine,
            MockCalledOnceWith(
                ANY,
                pod.power_type,
                {
                    "power_address": ANY,
                    "default_storage_pool_id": pod.default_storage_pool.pool_id,
                },
                form.get_requested_machine(make_known_host_interfaces(pod)),
                pod_id=pod.id,
                name=pod.name,
            ),
        )

    def test__compose_duplicated_hostname(self):
        factory.make_Node(hostname="test")

        request = MagicMock()
        pod = make_pod_with_hints()

        form = ComposeMachineForm(
            data={"hostname": "test"}, request=request, pod=pod
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(
            {"hostname": ['Node with hostname "test" already exists']},
            form.errors,
        )

    def test__compose_hostname_with_underscore(self):
        request = MagicMock()
        pod = make_pod_with_hints()

        form = ComposeMachineForm(
            data={"hostname": "testing_hostname"}, request=request, pod=pod
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(
            {
                "hostname": [
                    "Host label cannot contain underscore: 'testing_hostname'."
                ]
            },
            form.errors,
        )

    def test__compose_without_commissioning(self):
        request = MagicMock()
        pod = make_pod_with_hints()

        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        # Mock the result of the composed machine.
        composed_machine, pod_hints = self.make_compose_machine_result(pod)
        mock_compose_machine = self.patch(pods_module, "compose_machine")
        mock_compose_machine.return_value = succeed(
            (composed_machine, pod_hints)
        )

        # Mock start_commissioning so it doesn't use post commit hooks.
        mock_commissioning = self.patch(Machine, "start_commissioning")

        form = ComposeMachineForm(
            data={"skip_commissioning": "true"}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid())
        created_machine = form.compose()
        self.assertThat(
            created_machine,
            MatchesAll(
                IsInstance(Machine),
                MatchesStructure(
                    cpu_count=Equals(1),
                    memory=Equals(1024),
                    cpu_speed=Equals(300),
                ),
            ),
        )
        self.assertThat(mock_commissioning, MockNotCalled())

    def test__compose_with_skip_commissioning_passed(self):
        request = MagicMock()
        pod = make_pod_with_hints()

        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        # Mock the result of the composed machine.
        composed_machine, pod_hints = self.make_compose_machine_result(pod)
        mock_compose_machine = self.patch(pods_module, "compose_machine")
        mock_compose_machine.return_value = succeed(
            (composed_machine, pod_hints)
        )

        # Mock start_commissioning so it doesn't use post commit hooks.
        mock_commissioning = self.patch(Machine, "start_commissioning")

        form = ComposeMachineForm(data={}, request=request, pod=pod)
        self.assertTrue(form.is_valid())
        created_machine = form.compose(skip_commissioning=True)
        self.assertThat(
            created_machine,
            MatchesAll(
                IsInstance(Machine),
                MatchesStructure(
                    cpu_count=Equals(1),
                    memory=Equals(1024),
                    cpu_speed=Equals(300),
                ),
            ),
        )
        self.assertThat(mock_commissioning, MockNotCalled())

    def test__compose_sets_domain_and_zone(self):
        request = MagicMock()
        pod = make_pod_with_hints()

        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        # Mock the result of the composed machine.
        composed_machine, pod_hints = self.make_compose_machine_result(pod)
        mock_compose_machine = self.patch(pods_module, "compose_machine")
        mock_compose_machine.return_value = succeed(
            (composed_machine, pod_hints)
        )

        domain = factory.make_Domain()
        zone = factory.make_Zone()
        form = ComposeMachineForm(
            data={
                "domain": domain.id,
                "zone": zone.id,
                "skip_commissioning": "true",
            },
            request=request,
            pod=pod,
        )
        self.assertTrue(form.is_valid())
        created_machine = form.compose()
        self.assertThat(
            created_machine,
            MatchesAll(
                IsInstance(Machine),
                MatchesStructure(domain=Equals(domain), zone=Equals(zone)),
            ),
        )

    def test__compose_sets_resource_pool(self):
        request = MagicMock()
        pod = make_pod_with_hints()

        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        # Mock the result of the composed machine.
        composed_machine, pod_hints = self.make_compose_machine_result(pod)
        mock_compose_machine = self.patch(pods_module, "compose_machine")
        mock_compose_machine.return_value = succeed(
            (composed_machine, pod_hints)
        )

        pool = factory.make_ResourcePool()
        form = ComposeMachineForm(
            data={"pool": pool.id, "skip_commissioning": "true"},
            request=request,
            pod=pod,
        )
        self.assertTrue(form.is_valid())
        created_machine = form.compose()
        self.assertEqual(pool, created_machine.pool)

    def test__compose_uses_pod_pool(self):
        request = MagicMock()
        pod = make_pod_with_hints()
        pod.pool = factory.make_ResourcePool()
        pod.save()

        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        # Mock the result of the composed machine.
        composed_machine, pod_hints = self.make_compose_machine_result(pod)
        mock_compose_machine = self.patch(pods_module, "compose_machine")
        mock_compose_machine.return_value = succeed(
            (composed_machine, pod_hints)
        )

        form = ComposeMachineForm(
            data={"skip_commissioning": "true"}, request=request, pod=pod
        )
        self.assertTrue(form.is_valid())
        created_machine = form.compose()
        self.assertEqual(pod.pool, created_machine.pool)

    def test__compose_check_over_commit_ratios_raises_error_for_cores(self):
        request = MagicMock()
        pod = make_pod_with_hints()
        pod.cores = 0
        pod.save()

        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        form = ComposeMachineForm(data={}, request=request, pod=pod)
        self.assertTrue(form.is_valid())
        error = self.assertRaises(PodProblem, form.compose)
        self.assertEqual(
            "Unable to compose KVM instance in '%s'. "
            "CPU overcommit ratio is %s and there are %s "
            "available resources; %s requested."
            % (pod.name, pod.cpu_over_commit_ratio, pod.cores, 1),
            str(error),
        )

    def test__compose_check_over_commit_ratios_raises_error_for_memory(self):
        request = MagicMock()
        pod = make_pod_with_hints()
        pod.memory = 0
        pod.save()

        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        form = ComposeMachineForm(data={}, request=request, pod=pod)
        self.assertTrue(form.is_valid())
        error = self.assertRaises(PodProblem, form.compose)
        self.assertEqual(
            "Unable to compose KVM instance in '%s'. "
            "Memory overcommit ratio is %s and there are %s "
            "available resources; %s requested."
            % (pod.name, pod.memory_over_commit_ratio, pod.memory, 1024),
            str(error),
        )

    def test__compose_handles_timeout_error(self):
        request = MagicMock()
        pod = make_pod_with_hints()

        # Mock the RPC client.
        client = MagicMock()
        client.side_effect = crochet.TimeoutError()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        form = ComposeMachineForm(data={}, request=request, pod=pod)
        self.assertTrue(form.is_valid())
        error = self.assertRaises(PodProblem, form.compose)
        self.assertEquals(
            "Unable to compose a machine because '%s' driver timed out "
            "after 120 seconds." % pod.power_type,
            str(error),
        )

    @wait_for_reactor
    @inlineCallbacks
    def test__compose_with_commissioning_in_reactor(self):
        request = MagicMock()
        pod = yield deferToDatabase(make_pod_with_hints, with_host=True)

        # Mock the RPC client.
        client = MagicMock()
        mock_getClient = self.patch(pods_module, "getClientFromIdentifiers")
        mock_getClient.return_value = succeed(client)

        # Mock the result of the composed machine.
        composed_machine, pod_hints = self.make_compose_machine_result(pod)
        mock_compose_machine = self.patch(pods_module, "compose_machine")
        mock_compose_machine.return_value = succeed(
            (composed_machine, pod_hints)
        )

        # Mock start_commissioning so it doesn't use post commit hooks.
        mock_commissioning = self.patch(Machine, "start_commissioning")

        def get_free_ip():
            free_ip = factory.make_StaticIPAddress(
                interface=pod.host.boot_interface
            )
            requested_ip = free_ip.ip
            free_ip.delete()
            return requested_ip

        ip = yield deferToDatabase(get_free_ip)

        # Make sure to pass in 'interfaces' so that we fully test the functions
        # that must be deferred to the database.
        form = yield deferToDatabase(
            ComposeMachineForm,
            data=dict(interfaces="eth0:ip=%s" % ip),
            request=request,
            pod=pod,
        )
        is_valid = yield deferToDatabase(form.is_valid)
        self.assertTrue(is_valid)
        created_machine = yield form.compose()
        self.assertThat(
            created_machine,
            MatchesAll(
                IsInstance(Machine),
                MatchesStructure(
                    cpu_count=Equals(1),
                    memory=Equals(1024),
                    cpu_speed=Equals(300),
                ),
            ),
        )
        self.assertThat(mock_commissioning, MockCalledOnce())

    def test__save_raises_AttributeError(self):
        request = MagicMock()
        pod = make_pod_with_hints()
        form = ComposeMachineForm(data={}, request=request, pod=pod)
        self.assertTrue(form.is_valid())
        self.assertRaises(AttributeError, form.save)


class TestComposeMachineForPodsForm(MAASServerTestCase):
    def make_data(self, pods):
        return {
            "cores": random.randint(1, min([pod.hints.cores for pod in pods])),
            "memory": random.randint(
                1024, min([pod.hints.memory for pod in pods])
            ),
            "architecture": random.choice(
                [
                    "amd64/generic",
                    "i386/generic",
                    "arm64/generic",
                    "armhf/generic",
                ]
            ),
        }

    def make_pods(self):
        return [make_pod_with_hints() for _ in range(3)]

    def test__requires_request_kwarg(self):
        error = self.assertRaises(ValueError, ComposeMachineForPodsForm)
        self.assertEqual("'request' kwargs is required.", str(error))

    def test__requires_pods_kwarg(self):
        request = MagicMock()
        error = self.assertRaises(
            ValueError, ComposeMachineForPodsForm, request=request
        )
        self.assertEqual("'pods' kwargs is required.", str(error))

    def test__sets_up_pod_forms_based_on_pods(self):
        request = MagicMock()
        pods = self.make_pods()
        data = self.make_data(pods)
        form = ComposeMachineForPodsForm(request=request, data=data, pods=pods)
        self.assertTrue(form.is_valid())
        self.assertThat(
            form.pod_forms,
            MatchesListwise(
                [
                    MatchesAll(
                        IsInstance(ComposeMachineForm),
                        MatchesStructure(
                            request=Equals(request),
                            data=Equals(data),
                            pod=Equals(pod),
                        ),
                    )
                    for pod in pods
                ]
            ),
        )

    def test__save_raises_AttributeError(self):
        request = MagicMock()
        pods = self.make_pods()
        data = self.make_data(pods)
        form = ComposeMachineForPodsForm(request=request, data=data, pods=pods)
        self.assertTrue(form.is_valid())
        self.assertRaises(AttributeError, form.save)

    def test_compose_uses_non_commit_forms_first(self):
        request = MagicMock()
        pods = self.make_pods()
        # Make it skip the first overcommitable pod
        pods[1].capabilities = [Capabilities.OVER_COMMIT]
        pods[1].save()
        data = self.make_data(pods)
        form = ComposeMachineForPodsForm(request=request, data=data, pods=pods)
        mock_form_compose = self.patch(ComposeMachineForm, "compose")
        mock_form_compose.side_effect = [factory.make_exception(), None]
        self.assertTrue(form.is_valid())

        form.compose()
        self.assertThat(
            mock_form_compose,
            MockCallsMatch(
                call(
                    skip_commissioning=True,
                    creation_type=NODE_CREATION_TYPE.DYNAMIC,
                ),
                call(
                    skip_commissioning=True,
                    creation_type=NODE_CREATION_TYPE.DYNAMIC,
                ),
            ),
        )

    def test_compose_uses_commit_forms_second(self):
        request = MagicMock()
        pods = self.make_pods()
        # Make it skip all pods.
        for pod in pods:
            pod.capabilities = [Capabilities.OVER_COMMIT]
            pod.save()
        data = self.make_data(pods)
        form = ComposeMachineForPodsForm(request=request, data=data, pods=pods)
        mock_form_compose = self.patch(ComposeMachineForm, "compose")
        mock_form_compose.side_effect = [
            factory.make_exception(),
            factory.make_exception(),
            factory.make_exception(),
            None,
        ]
        self.assertTrue(form.is_valid())

        form.compose()
        self.assertThat(
            mock_form_compose,
            MockCallsMatch(
                call(
                    skip_commissioning=True,
                    creation_type=NODE_CREATION_TYPE.DYNAMIC,
                ),
                call(
                    skip_commissioning=True,
                    creation_type=NODE_CREATION_TYPE.DYNAMIC,
                ),
                call(
                    skip_commissioning=True,
                    creation_type=NODE_CREATION_TYPE.DYNAMIC,
                ),
            ),
        )

    def test_clean_adds_error_for_no_matching_constraints(self):
        request = MagicMock()
        pods = self.make_pods()
        for pod in pods:
            pod.architectures = ["Not vaild architecture"]
            pod.save()
        data = self.make_data(pods)
        form = ComposeMachineForPodsForm(request=request, data=data, pods=pods)
        self.assertFalse(form.is_valid())


class TestGetKnownHostInterfaces(MAASServerTestCase):
    def test__returns_empty_list_if_no_interfaces(self):
        node = factory.make_Machine_with_Interface_on_Subnet()
        node.interface_set.all().delete()
        interfaces = get_known_host_interfaces(node)
        self.assertThat(interfaces, HasLength(0))

    def test__returns_appropriate_attach_type(self):
        node = factory.make_Machine_with_Interface_on_Subnet()
        vlan = factory.make_VLAN(dhcp_on=False)
        node.interface_set.all().delete()
        bridge = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE, node=node, vlan=vlan
        )
        physical = factory.make_Interface(
            iftype=INTERFACE_TYPE.PHYSICAL, node=node, vlan=vlan
        )
        interfaces = get_known_host_interfaces(node)
        self.assertItemsEqual(
            interfaces,
            [
                KnownHostInterface(
                    ifname=bridge.name,
                    attach_type=InterfaceAttachType.BRIDGE,
                    dhcp_enabled=False,
                ),
                KnownHostInterface(
                    ifname=physical.name,
                    attach_type=InterfaceAttachType.MACVLAN,
                    dhcp_enabled=False,
                ),
            ],
        )

    def test__behaves_correctly_when_vlan_is_none(self):
        node = factory.make_Machine_with_Interface_on_Subnet()
        node.interface_set.all().delete()
        bridge = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE, node=node, link_connected=False
        )
        physical = factory.make_Interface(
            iftype=INTERFACE_TYPE.PHYSICAL, node=node, link_connected=False
        )
        interfaces = get_known_host_interfaces(node)
        self.assertItemsEqual(
            interfaces,
            [
                KnownHostInterface(
                    ifname=bridge.name,
                    attach_type=InterfaceAttachType.BRIDGE,
                    dhcp_enabled=False,
                ),
                KnownHostInterface(
                    ifname=physical.name,
                    attach_type=InterfaceAttachType.MACVLAN,
                    dhcp_enabled=False,
                ),
            ],
        )

    def test__gets_dhcp_status_for_directly_enabled_vlan(self):
        node = factory.make_Machine_with_Interface_on_Subnet()
        vlan = factory.make_VLAN(dhcp_on=True)
        node.interface_set.all().delete()
        bridge = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE, node=node, vlan=vlan
        )
        physical = factory.make_Interface(
            iftype=INTERFACE_TYPE.PHYSICAL, node=node, vlan=vlan
        )
        interfaces = get_known_host_interfaces(node)
        self.assertItemsEqual(
            interfaces,
            [
                KnownHostInterface(
                    ifname=bridge.name,
                    attach_type=InterfaceAttachType.BRIDGE,
                    dhcp_enabled=True,
                ),
                KnownHostInterface(
                    ifname=physical.name,
                    attach_type=InterfaceAttachType.MACVLAN,
                    dhcp_enabled=True,
                ),
            ],
        )

    def test__gets_dhcp_status_for_indirectly_enabled_vlan(self):
        node = factory.make_Machine_with_Interface_on_Subnet()
        relay_vlan = factory.make_VLAN(dhcp_on=True)
        vlan = factory.make_VLAN(dhcp_on=False, relay_vlan=relay_vlan)
        node.interface_set.all().delete()
        bridge = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE, node=node, vlan=vlan
        )
        physical = factory.make_Interface(
            iftype=INTERFACE_TYPE.PHYSICAL, node=node, vlan=vlan
        )
        interfaces = get_known_host_interfaces(node)
        self.assertItemsEqual(
            interfaces,
            [
                KnownHostInterface(
                    ifname=bridge.name,
                    attach_type=InterfaceAttachType.BRIDGE,
                    dhcp_enabled=True,
                ),
                KnownHostInterface(
                    ifname=physical.name,
                    attach_type=InterfaceAttachType.MACVLAN,
                    dhcp_enabled=True,
                ),
            ],
        )
