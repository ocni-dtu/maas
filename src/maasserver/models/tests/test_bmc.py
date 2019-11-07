# Copyright 2016-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test maasserver models."""

__all__ = []

import random
from unittest.mock import Mock, sentinel

from crochet import wait_for
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.deletion import ProtectedError
from django.http import Http404
from maasserver.enum import (
    INTERFACE_TYPE,
    IPADDRESS_TYPE,
    NODE_CREATION_TYPE,
    NODE_TYPE,
    POWER_STATE,
)
from maasserver.exceptions import PodProblem
from maasserver.models import bmc as bmc_module
from maasserver.models.blockdevice import BlockDevice
from maasserver.models.bmc import (
    BMC,
    BMCRoutableRackControllerRelationship,
    get_requested_ips,
    Pod,
)
from maasserver.models.fabric import Fabric
from maasserver.models.interface import Interface
from maasserver.models.iscsiblockdevice import (
    get_iscsi_target,
    ISCSIBlockDevice,
)
from maasserver.models.node import Machine
from maasserver.models.physicalblockdevice import PhysicalBlockDevice
from maasserver.models.resourcepool import ResourcePool
from maasserver.models.staticipaddress import StaticIPAddress
from maasserver.permissions import PodPermission
from maasserver.testing.factory import factory
from maasserver.testing.fixtures import RBACEnabled
from maasserver.testing.matchers import MatchesSetwiseWithAll
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.orm import reload_object
from maasserver.utils.threads import deferToDatabase
from maastesting.matchers import MockCalledOnceWith
from provisioningserver.drivers.pod import (
    BlockDeviceType,
    DiscoveredMachine,
    DiscoveredMachineBlockDevice,
    DiscoveredMachineInterface,
    DiscoveredPod,
    DiscoveredPodHints,
    DiscoveredPodStoragePool,
    InterfaceAttachType,
    RequestedMachine,
    RequestedMachineInterface,
)
from provisioningserver.rpc.cluster import DecomposeMachine
from provisioningserver.utils.constraints import LabeledConstraintMap
from testtools import ExpectedException
from testtools.matchers import (
    Equals,
    HasLength,
    Is,
    IsInstance,
    MatchesSetwise,
    MatchesStructure,
)
from twisted.internet.defer import fail, inlineCallbacks, succeed


wait_for_reactor = wait_for(30)  # 30 seconds.


class TestBMC(MAASServerTestCase):
    @staticmethod
    def get_machine_ip_address(machine):
        return machine.interface_set.all()[0].ip_addresses.all()[0]

    def make_machine_and_bmc_with_shared_ip(self):
        machine = factory.make_Node(interface=False)
        machine.interface_set.clear()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=machine
        )
        machine_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=subnet,
            interface=interface,
        )
        self.assertEqual(1, machine.interface_set.count())

        bmc = factory.make_BMC(
            power_type="virsh",
            power_parameters={
                "power_address": "protocol://%s:8080/path/to/thing#tag"
                % (factory.ip_to_url_format(machine_ip.ip))
            },
        )
        # Make sure they're sharing an IP.
        machine = reload_object(machine)
        machine_ip_addr = TestBMC.get_machine_ip_address(machine)
        self.assertEqual(machine_ip_addr.id, bmc.ip_address.id)
        return machine, bmc, machine_ip

    def make_machine_and_bmc_differing_ips(self):
        machine = factory.make_Node(interface=False)
        machine.interface_set.clear()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=machine
        )
        machine_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=subnet,
            interface=interface,
        )
        self.assertEqual(1, machine.interface_set.count())

        ip_address = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, subnet=subnet
        )
        bmc_ip = ip_address.ip
        ip_address.delete()
        bmc = factory.make_BMC(
            power_type="virsh",
            power_parameters={
                "power_address": "protocol://%s:8080/path/to/thing#tag"
                % (factory.ip_to_url_format(bmc_ip))
            },
        )
        # Make sure they're not sharing an IP.
        machine = reload_object(machine)
        machine_ip_addr = TestBMC.get_machine_ip_address(machine)
        self.assertNotEqual(machine_ip_addr.id, bmc.ip_address.id)
        return machine, bmc, machine_ip

    def test_make_machine_and_bmc_discovered_ip(self):
        # Regression test for LP:1816651
        subnet = factory.make_Subnet()
        discovered_ip = factory.make_StaticIPAddress(
            subnet=subnet, alloc_type=IPADDRESS_TYPE.DISCOVERED
        )
        sticky_ip = factory.make_StaticIPAddress(
            ip=discovered_ip.ip,
            subnet=subnet,
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        bmc = factory.make_BMC(
            power_type="virsh",
            power_parameters={
                "power_address": "protocol://%s:8080/path/to/thing#tag"
                % (factory.ip_to_url_format(discovered_ip.ip))
            },
        )
        self.assertItemsEqual(
            [discovered_ip, sticky_ip],
            StaticIPAddress.objects.filter(ip=discovered_ip.ip),
        )
        self.assertEqual(sticky_ip, bmc.ip_address)

    def test_bmc_save_extracts_ip_address(self):
        subnet = factory.make_Subnet()
        sticky_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, subnet=subnet
        )
        power_parameters = {
            "power_address": "protocol://%s:8080/path/to/thing#tag"
            % (factory.ip_to_url_format(sticky_ip.ip))
        }
        bmc = factory.make_BMC(
            power_type="virsh", power_parameters=power_parameters
        )
        self.assertEqual(sticky_ip.ip, bmc.ip_address.ip)
        self.assertEqual(subnet, bmc.ip_address.subnet)

    def test_bmc_save_accepts_naked_ipv6_address(self):
        subnet = factory.make_Subnet(cidr=factory.make_ipv6_network())
        sticky_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, subnet=subnet
        )
        power_parameters = {"power_address": "%s" % sticky_ip.ip}
        bmc = factory.make_BMC(
            power_type="ipmi", power_parameters=power_parameters
        )
        self.assertEqual(sticky_ip.ip, bmc.ip_address.ip)
        self.assertEqual(subnet, bmc.ip_address.subnet)

    def test_bmc_save_accepts_bracketed_ipv6_address(self):
        subnet = factory.make_Subnet(cidr=factory.make_ipv6_network())
        sticky_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, subnet=subnet
        )
        power_parameters = {"power_address": "[%s]" % sticky_ip.ip}
        bmc = factory.make_BMC(
            power_type="ipmi", power_parameters=power_parameters
        )
        self.assertEqual(sticky_ip.ip, bmc.ip_address.ip)
        self.assertEqual(subnet, bmc.ip_address.subnet)

    def test_bmc_changing_power_parameters_changes_ip(self):
        ip = factory.make_ipv4_address()
        power_parameters = {
            "power_address": "protocol://%s:8080/path#tag"
            % factory.ip_to_url_format(ip)
        }
        bmc = factory.make_BMC(
            power_type="virsh", power_parameters=power_parameters
        )
        self.assertEqual(ip, bmc.ip_address.ip)
        self.assertIsNone(bmc.ip_address.subnet)

        subnet = factory.make_Subnet()
        sticky_ip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, subnet=subnet
        )
        bmc.power_parameters = {
            "power_address": "protocol://%s:8080/path/to/thing#tag"
            % (factory.ip_to_url_format(sticky_ip.ip))
        }
        bmc.save()
        self.assertEqual(sticky_ip.ip, bmc.ip_address.ip)
        self.assertEqual(subnet, bmc.ip_address.subnet)

    def test_bmc_changing_power_parameters_ipmi_errors_if_invalid(self):
        ip = factory.make_ipv4_address()
        power_parameters = {
            "power_address": "protocol://%s" % factory.ip_to_url_format(ip)
        }
        with ExpectedException(ValueError):
            factory.make_BMC(
                power_type="ipmi", power_parameters=power_parameters
            )

    def test_deleting_machine_ip_when_shared_with_bmc(self):
        machine, bmc, machine_ip = self.make_machine_and_bmc_with_shared_ip()

        # Now delete the machine.
        old_ip = machine_ip.ip
        machine.delete()

        # Check BMC still has old IP.
        bmc = reload_object(bmc)
        self.assertIsNotNone(bmc.ip_address)
        self.assertEqual(old_ip, bmc.ip_address.ip)

        # Make sure DB ID's of StaticIPAddress instances differ.
        self.assertNotEqual(machine_ip.id, bmc.ip_address.id)

    def test_removing_bmc_ip_when_shared_with_bmc(self):
        machine, bmc, machine_ip = self.make_machine_and_bmc_with_shared_ip()

        # Clear the BMC IP.
        old_ip = bmc.ip_address.ip
        bmc.power_type = "manual"
        bmc.save()
        self.assertIsNone(bmc.ip_address)

        # Check Machine still has same IP address.
        machine = reload_object(machine)
        machine_ip_addr = TestBMC.get_machine_ip_address(machine)
        self.assertEqual(old_ip, machine_ip_addr.ip)
        self.assertEqual(machine_ip.id, machine_ip_addr.id)

    def test_changing_machine_ip_when_shared_with_bmc_keeps_both(self):
        machine, bmc, machine_ip = self.make_machine_and_bmc_with_shared_ip()

        # Now change the Machine's IP to a new address on same subnet.
        old_ip = machine_ip.ip
        new_ip_address = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, subnet=machine_ip.subnet
        )
        new_ip = new_ip_address.ip
        # Remove IP so we can set machine_ip to its address.
        new_ip_address.delete()
        self.assertNotEqual(new_ip, old_ip)
        machine_ip.ip = new_ip
        machine_ip.save()

        # Check Machine has new IP address but kept same instance: machine_ip.
        machine = reload_object(machine)
        machine_ip_addr = TestBMC.get_machine_ip_address(machine)
        self.assertEqual(new_ip, machine_ip_addr.ip)
        self.assertEqual(machine_ip.id, machine_ip_addr.id)

        # Check BMC still has old IP.
        bmc = reload_object(bmc)
        self.assertEqual(old_ip, bmc.ip_address.ip)

        # Make sure DB ID's of StaticIPAddress instances differ.
        self.assertNotEqual(machine_ip_addr.id, bmc.ip_address.id)

    def test_changing_bmc_ip_when_shared_with_machine_keeps_both(self):
        machine, bmc, machine_ip = self.make_machine_and_bmc_with_shared_ip()

        # Now change the BMC's IP to a new address on same subnet.
        old_ip = machine_ip.ip
        new_ip_address = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, subnet=machine_ip.subnet
        )
        new_ip = new_ip_address.ip
        # Remove IP so we can set machine_ip to its address.
        new_ip_address.delete()
        self.assertNotEqual(new_ip, old_ip)

        bmc.power_parameters = {
            "power_address": "protocol://%s:8080/path/to/thing#tag"
            % (factory.ip_to_url_format(new_ip))
        }
        bmc.save()

        # Check Machine has old IP address and kept same instance: machine_ip.
        machine = reload_object(machine)
        machine_ip_addr = TestBMC.get_machine_ip_address(machine)
        self.assertEqual(old_ip, machine_ip_addr.ip)
        self.assertEqual(machine_ip.id, machine_ip_addr.id)

        # Check BMC has new IP.
        bmc = reload_object(bmc)
        self.assertEqual(new_ip, bmc.ip_address.ip)

        # Make sure DB ID's of StaticIPAddress instances differ.
        self.assertNotEqual(machine_ip_addr.id, bmc.ip_address.id)

    def test_merging_machine_into_bmc_ip(self):
        machine, bmc, _ = self.make_machine_and_bmc_differing_ips()

        # Now change the machine's address to match bmc's.
        machine_ip_addr = TestBMC.get_machine_ip_address(machine)
        machine_ip_addr.ip = bmc.ip_address.ip
        machine_ip_addr.save()

        # Make sure BMC and Machine now using same StaticIPAddress instance.
        machine = reload_object(machine)
        machine_ip_addr = TestBMC.get_machine_ip_address(machine)
        self.assertEqual(machine_ip_addr.id, reload_object(bmc).ip_address.id)

    def test_merging_bmc_into_machine_ip(self):
        machine, bmc, machine_ip = self.make_machine_and_bmc_differing_ips()

        # Now change the BMC's address to match machine's.
        bmc.power_parameters = {
            "power_address": "protocol://%s:8080/path/to/thing#tag"
            % (factory.ip_to_url_format(machine_ip.ip))
        }
        bmc.save()

        # Make sure BMC and Machine are using same StaticIPAddress instance.
        machine = reload_object(machine)
        machine_ip_addr = TestBMC.get_machine_ip_address(machine)
        self.assertEqual(machine_ip_addr.id, bmc.ip_address.id)

    def test_delete_bmc_deletes_orphaned_ip_address(self):
        bmc = factory.make_BMC(
            power_type="virsh",
            power_parameters={
                "power_address": "protocol://%s:8080/path/to/thing#tag"
                % (factory.make_ipv4_address())
            },
        )
        ip = bmc.ip_address
        bmc.delete()
        self.assertEqual(0, StaticIPAddress.objects.filter(id=ip.id).count())

    def test_delete_bmc_spares_non_orphaned_ip_address(self):
        machine, bmc, machine_ip = self.make_machine_and_bmc_with_shared_ip()
        bmc.delete()
        self.assertEqual(
            1, StaticIPAddress.objects.filter(id=machine_ip.id).count()
        )

    def test_scope_power_parameters(self):
        bmc_parameters = dict(
            power_address=factory.make_string(),
            power_pass=factory.make_string(),
        )
        node_parameters = dict(
            power_vm_name=factory.make_string(),
            power_uuid=factory.make_string(),
        )
        parameters = {**bmc_parameters, **node_parameters}
        result = BMC.scope_power_parameters("vmware", parameters)
        self.assertTrue(result[0])
        self.assertEqual(bmc_parameters, result[1])
        self.assertEqual(node_parameters, result[2])

    def test_scope_power_parameters_unknown_parameter(self):
        bmc_parameters = dict(power_address=factory.make_string())
        node_parameters = dict(server_name=factory.make_string())
        # This random parameter should be stored on the node instance.
        node_parameters[factory.make_string()] = factory.make_string()
        parameters = {**bmc_parameters, **node_parameters}
        result = BMC.scope_power_parameters("hmc", parameters)
        self.assertTrue(result[0])
        self.assertEqual(bmc_parameters, result[1])
        self.assertEqual(node_parameters, result[2])

    def test_bmc_extract_ip_address_whole_value(self):
        power_parameters = {"power_address": "192.168.1.1"}
        self.assertEqual(
            "192.168.1.1", BMC.extract_ip_address("hmc", power_parameters)
        )

    def test_bmc_extract_ip_address_empty_power_type_gives_none(self):
        power_parameters = {"power_address": "192.168.1.1"}
        self.assertEqual(None, BMC.extract_ip_address("", power_parameters))
        self.assertEqual(None, BMC.extract_ip_address(None, power_parameters))

    def test_bmc_extract_ip_address_blank_gives_none(self):
        self.assertEqual(None, BMC.extract_ip_address("hmc", None))
        self.assertEqual(None, BMC.extract_ip_address("hmc", {}))

        power_parameters = {"power_address": ""}
        self.assertEqual(None, BMC.extract_ip_address("hmc", power_parameters))

        power_parameters = {"power_address": None}
        self.assertEqual(None, BMC.extract_ip_address("hmc", power_parameters))

    def test_bmc_extract_ip_address_from_url(self):
        power_parameters = {
            "power_address": "protocol://somehost:8080/path/to/thing#tag"
        }
        self.assertEqual(
            "somehost", BMC.extract_ip_address("virsh", power_parameters)
        )

    def test_bmc_extract_ip_address_from_url_blank_gives_none(self):
        self.assertEqual(None, BMC.extract_ip_address("virsh", None))
        self.assertEqual(None, BMC.extract_ip_address("virsh", {}))

        power_parameters = {"power_address": ""}
        self.assertEqual(
            None, BMC.extract_ip_address("virsh", power_parameters)
        )

        power_parameters = {"power_address": None}
        self.assertEqual(
            None, BMC.extract_ip_address("virsh", power_parameters)
        )

    def test_bmc_extract_ip_address_from_url_empty_host(self):
        power_parameters = {"power_address": "http://:8080/foo/#baz"}
        self.assertEqual("", BMC.extract_ip_address("virsh", power_parameters))

    def test_get_usable_rack_controllers_returns_empty_when_none(self):
        bmc = factory.make_BMC()
        self.assertThat(bmc.get_usable_rack_controllers(), HasLength(0))

    def test_get_usable_rack_controllers_returns_routable_racks(self):
        bmc = factory.make_BMC()
        routable_racks = [factory.make_RackController() for _ in range(3)]
        not_routable_racks = [factory.make_RackController() for _ in range(3)]
        for rack in routable_racks:
            BMCRoutableRackControllerRelationship(
                bmc=bmc, rack_controller=rack, routable=True
            ).save()
        for rack in not_routable_racks:
            BMCRoutableRackControllerRelationship(
                bmc=bmc, rack_controller=rack, routable=False
            ).save()
        self.assertItemsEqual(
            routable_racks,
            bmc.get_usable_rack_controllers(with_connection=False),
        )

    def test_get_usable_rack_controllers_returns_routable_racks_conn(self):
        bmc = factory.make_BMC()
        routable_racks = [factory.make_RackController() for _ in range(3)]
        not_routable_racks = [factory.make_RackController() for _ in range(3)]
        for rack in routable_racks:
            BMCRoutableRackControllerRelationship(
                bmc=bmc, rack_controller=rack, routable=True
            ).save()
        for rack in not_routable_racks:
            BMCRoutableRackControllerRelationship(
                bmc=bmc, rack_controller=rack, routable=False
            ).save()
        connected_rack = random.choice(routable_racks)
        client = Mock()
        client.ident = connected_rack.system_id
        self.patch(bmc_module, "getAllClients").return_value = [client]
        self.assertItemsEqual(
            [connected_rack],
            bmc.get_usable_rack_controllers(with_connection=True),
        )

    def test_get_usable_rack_controllers_updates_subnet_on_sip(self):
        network = factory.make_ipv4_network()
        subnet = factory.make_Subnet(cidr=str(network.cidr))
        ip = factory.pick_ip_in_Subnet(subnet)
        sip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=ip, subnet=subnet
        )
        sip.subnet = None
        sip.save()
        bmc = factory.make_BMC(
            power_type="virsh",
            power_parameters={
                "power_address": "qemu+ssh://user@%s/system" % ip
            },
            ip_address=sip,
        )
        bmc.get_usable_rack_controllers()
        self.assertEqual(subnet, reload_object(sip).subnet)

    def test_get_usable_rack_controllers_updates_handles_unknown_subnet(self):
        network = factory.make_ipv4_network()
        ip = factory.pick_ip_in_network(network)
        sip = StaticIPAddress.objects.create(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=ip
        )
        bmc = factory.make_BMC(
            power_type="virsh",
            power_parameters={
                "power_address": "qemu+ssh://user@%s/system" % ip
            },
            ip_address=sip,
        )
        bmc.get_usable_rack_controllers()
        self.assertIsNone(reload_object(sip).subnet)

    def test_get_usable_rack_controllers_returns_rack_controllers(self):
        rack_controller = factory.make_RackController()
        machine = factory.make_Node(bmc_connected_to=rack_controller)
        self.assertItemsEqual(
            [rack_controller],
            machine.bmc.get_usable_rack_controllers(with_connection=False),
        )

    def test_get_client_identifiers_returns_rack_controller_system_ids(self):
        rack_controllers = [factory.make_RackController() for _ in range(3)]
        bmc = factory.make_BMC()
        self.patch(
            bmc, "get_usable_rack_controllers"
        ).return_value = rack_controllers
        expected_system_ids = [rack.system_id for rack in rack_controllers]
        self.assertItemsEqual(
            expected_system_ids, bmc.get_client_identifiers()
        )

    def test_is_accessible_calls_get_usable_rack_controllers(self):
        bmc = factory.make_BMC()
        mock_get_usable_rack_controllers = self.patch(
            bmc, "get_usable_rack_controllers"
        )
        bmc.is_accessible()
        self.assertThat(
            mock_get_usable_rack_controllers,
            MockCalledOnceWith(with_connection=False),
        )

    def test_is_accessible_returns_true(self):
        bmc = factory.make_BMC()
        mock_get_usable_rack_controllers = self.patch(
            bmc, "get_usable_rack_controllers"
        )
        mock_get_usable_rack_controllers.return_value = [
            factory.make_RackController()
        ]
        self.assertTrue(bmc.is_accessible())

    def test_is_accessible_returns_false(self):
        bmc = factory.make_BMC()
        mock_get_usable_rack_controllers = self.patch(
            bmc, "get_usable_rack_controllers"
        )
        mock_get_usable_rack_controllers.return_value = []
        self.assertFalse(bmc.is_accessible())

    def test_update_routable_racks_updates_rack_relationship(self):
        node = factory.make_Node(power_type="virsh")

        # Create old relationships that should be removed.
        old_relationship_ids = [
            BMCRoutableRackControllerRelationship.objects.create(
                bmc=node.bmc,
                rack_controller=factory.make_RackController(),
                routable=True,
            ).id
            for _ in range(3)
        ]

        routable_racks = [factory.make_RackController() for _ in range(3)]
        non_routable_racks = [factory.make_RackController() for _ in range(3)]

        node.bmc.update_routable_racks(
            [rack.system_id for rack in routable_racks],
            [rack.system_id for rack in non_routable_racks],
        )

        self.assertThat(
            BMCRoutableRackControllerRelationship.objects.filter(
                id__in=old_relationship_ids
            ),
            HasLength(0),
        )
        self.assertThat(
            BMCRoutableRackControllerRelationship.objects.filter(
                rack_controller__in=routable_racks, routable=True
            ),
            HasLength(len(routable_racks)),
        )
        self.assertThat(
            BMCRoutableRackControllerRelationship.objects.filter(
                rack_controller__in=non_routable_racks, routable=False
            ),
            HasLength(len(non_routable_racks)),
        )


class TestPodManager(MAASServerTestCase):
    def enable_rbac(self):
        rbac = self.useFixture(RBACEnabled())
        self.store = rbac.store

    def test_get_pods_no_rbac_always_all(self):
        pods = [factory.make_Pod() for _ in range(3)]
        for perm in PodPermission:
            self.assertItemsEqual(
                pods, Pod.objects.get_pods(factory.make_User(), perm)
            )

    def test_get_pods_view_rbac_returns_view_rights(self):
        self.enable_rbac()
        user = factory.make_User()
        view_pool = factory.make_ResourcePool()
        view = factory.make_Pod(pool=view_pool)
        self.store.add_pool(view_pool)
        self.store.allow(user.username, view_pool, "view")
        view_all_pool = factory.make_ResourcePool()
        view_all = factory.make_Pod(pool=view_all_pool)
        self.store.add_pool(view_all_pool)
        self.store.allow(user.username, view_all_pool, "view-all")

        # others not shown
        for _ in range(3):
            factory.make_Pod()

        self.assertItemsEqual(
            [view, view_all], Pod.objects.get_pods(user, PodPermission.view)
        )

    def test_get_pods_edit_compose_rbac_returns_admin_rights(self):
        self.enable_rbac()
        user = factory.make_User()
        view_pool = factory.make_ResourcePool()
        factory.make_Pod(pool=view_pool)
        self.store.add_pool(view_pool)
        self.store.allow(user.username, view_pool, "view")
        deploy_pool = factory.make_ResourcePool()
        factory.make_Pod(pool=deploy_pool)
        self.store.add_pool(deploy_pool)
        self.store.allow(user.username, deploy_pool, "deploy-machines")
        admin_pool = factory.make_ResourcePool()
        admin_pod = factory.make_Pod(pool=admin_pool)
        self.store.add_pool(admin_pool)
        self.store.allow(user.username, admin_pool, "admin-machines")

        # others not shown
        for _ in range(3):
            factory.make_Pod()

        self.assertItemsEqual(
            [admin_pod], Pod.objects.get_pods(user, PodPermission.edit)
        )
        self.assertItemsEqual(
            [admin_pod], Pod.objects.get_pods(user, PodPermission.compose)
        )

    def test_get_pods_dynamic_compose_rbac_returns_deploy_admin_rights(self):
        self.enable_rbac()
        user = factory.make_User()
        view_pool = factory.make_ResourcePool()
        factory.make_Pod(pool=view_pool)
        self.store.add_pool(view_pool)
        self.store.allow(user.username, view_pool, "view")
        deploy_pool = factory.make_ResourcePool()
        deploy_pod = factory.make_Pod(pool=deploy_pool)
        self.store.add_pool(deploy_pool)
        self.store.allow(user.username, deploy_pool, "deploy-machines")
        admin_pool = factory.make_ResourcePool()
        admin_pod = factory.make_Pod(pool=admin_pool)
        self.store.add_pool(admin_pool)
        self.store.allow(user.username, admin_pool, "admin-machines")

        # others not shown
        for _ in range(3):
            factory.make_Pod()

        self.assertItemsEqual(
            [deploy_pod, admin_pod],
            Pod.objects.get_pods(user, PodPermission.dynamic_compose),
        )

    def test_get_pod_or_404_raises_404(self):
        user = factory.make_User()
        self.patch(user, "has_perm").return_value = False
        self.assertRaises(
            Http404,
            Pod.objects.get_pod_or_404,
            random.randint(10, 20),
            user,
            PodPermission.view,
        )

    def test_get_pod_or_404_checks_permissions(self):
        pod = factory.make_Pod()
        user = factory.make_User()
        self.patch(user, "has_perm").return_value = False
        self.assertRaises(
            PermissionDenied,
            Pod.objects.get_pod_or_404,
            pod.id,
            user,
            PodPermission.view,
        )

    def test_get_pod_or_404_returns_pod(self):
        pod = factory.make_Pod()
        user = factory.make_User()
        self.patch(user, "has_perm").return_value = True
        self.assertEqual(
            pod, Pod.objects.get_pod_or_404(pod.id, user, PodPermission.view)
        )

    def test_have_rsd_with_no_rsd(self):
        factory.make_Pod()
        user = factory.make_User()
        self.patch(user, "has_perm").return_value = True
        self.assertFalse(Pod.objects.have_rsd(user, PodPermission.view))

    def test_have_rsd_with_rsd(self):
        factory.make_Pod(pod_type="rsd")
        user = factory.make_User()
        self.patch(user, "has_perm").return_value = True
        self.assertTrue(Pod.objects.have_rsd(user, PodPermission.view))

    def test_have_rsd_with_forbidden_rsd(self):
        self.enable_rbac()
        factory.make_Pod(pod_type="rsd")
        user = factory.make_User()
        self.patch(user, "has_perm").return_value = False
        self.assertFalse(Pod.objects.have_rsd(user, PodPermission.view))


class TestPod(MAASServerTestCase):
    def make_discovered_block_device(
        self,
        model=None,
        serial=None,
        id_path=None,
        target=None,
        block_type=BlockDeviceType.PHYSICAL,
        storage_pools=None,
    ):
        if block_type == BlockDeviceType.PHYSICAL:
            if id_path is None:
                if model is None:
                    model = factory.make_name("model")
                if serial is None:
                    serial = factory.make_name("serial")
            else:
                model = None
                serial = None
        elif block_type == BlockDeviceType.ISCSI:
            if target is None:
                target = "%s::::%s" % (
                    factory.make_name("host"),
                    factory.make_name("target"),
                )
        else:
            raise ValueError("Unknown block_type: %s" % block_type)
        storage_pool = None
        if storage_pools is not None:
            storage_pool = random.choice(storage_pools).id
        return DiscoveredMachineBlockDevice(
            model=model,
            serial=serial,
            size=random.randint(1024 ** 3, 1024 ** 4),
            block_size=random.choice([512, 4096]),
            tags=[factory.make_name("tag") for _ in range(3)],
            id_path=id_path,
            type=block_type,
            iscsi_target=target,
            storage_pool=storage_pool,
        )

    def make_discovered_interface(self, mac_address=None):
        if mac_address is None:
            mac_address = factory.make_mac_address()
        return DiscoveredMachineInterface(
            mac_address=mac_address,
            tags=[factory.make_name("tag") for _ in range(3)],
        )

    def make_discovered_machine(
        self, block_devices=None, interfaces=None, storage_pools=None
    ):
        if block_devices is None:
            block_devices = [
                self.make_discovered_block_device(
                    block_type=BlockDeviceType.PHYSICAL,
                    storage_pools=storage_pools,
                )
                for _ in range(3)
            ] + [
                self.make_discovered_block_device(
                    block_type=BlockDeviceType.ISCSI
                )
                for _ in range(3)
            ]
        if interfaces is None:
            interfaces = [self.make_discovered_interface() for _ in range(3)]
            interfaces[0].boot = True
        return DiscoveredMachine(
            hostname=factory.make_name("hostname"),
            architecture="amd64/generic",
            cores=random.randint(8, 120),
            cpu_speed=random.randint(2000, 4000),
            memory=random.randint(8192, 8192 * 8),
            interfaces=interfaces,
            block_devices=block_devices,
            power_state=random.choice([POWER_STATE.ON, POWER_STATE.OFF]),
            power_parameters={
                factory.make_name("key"): factory.make_name("value")
            },
            tags=[factory.make_name("tag") for _ in range(3)],
        )

    def make_discovered_storage_pool(self):
        name = factory.make_name("name")
        return DiscoveredPodStoragePool(
            id=factory.make_name("id"),
            name=name,
            storage=random.randint(10 * 1024 ** 3, 100 * 1024 ** 3),
            type=factory.make_name("type"),
            path="/var/lib/%s" % name,
        )

    def make_discovered_pod(self, machines=None, storage_pools=None):
        if storage_pools is None:
            storage_pools = [
                self.make_discovered_storage_pool() for _ in range(3)
            ]
        if machines is None:
            machines = [
                self.make_discovered_machine(storage_pools=storage_pools)
                for _ in range(3)
            ]
        return DiscoveredPod(
            architectures=["amd64/generic"],
            cores=random.randint(8, 120),
            cpu_speed=random.randint(2000, 4000),
            memory=random.randint(8192, 8192 * 8),
            local_storage=random.randint(20000, 40000),
            hints=DiscoveredPodHints(
                cores=random.randint(8, 16),
                cpu_speed=random.randint(2000, 4000),
                memory=random.randint(8192, 8192 * 2),
                local_storage=random.randint(10000, 20000),
            ),
            storage_pools=storage_pools,
            machines=machines,
        )

    def test_create_with_pool(self):
        pool = ResourcePool.objects.get_default_resource_pool()
        pod = Pod(power_type="virsh", power_parameters={}, pool=pool)
        pod.save()
        self.assertEqual(pool, pod.pool)

    def test_create_with_no_pool(self):
        pod = Pod(power_type="virsh", power_parameters={})
        pod.save()
        self.assertEqual(
            ResourcePool.objects.get_default_resource_pool(), pod.pool
        )

    def test_save_with_no_pool(self):
        pod = Pod(power_type="virsh", power_parameters={})
        pod.pool = None
        self.assertRaises(ValidationError, pod.save)

    def test_no_delete_pod_pool(self):
        pool = factory.make_ResourcePool()
        pod = Pod(power_type="virsh", power_parameters={}, pool=pool)
        pod.save()
        self.assertRaises(ProtectedError, pool.delete)

    def test_create_with_over_commit_ratios(self):
        cpu_over_commit_ratio = random.uniform(0.1, 2.0)
        memory_over_commit_ratio = random.uniform(0.1, 2.0)
        pod = Pod(
            power_type="virsh",
            power_parameters={},
            cpu_over_commit_ratio=cpu_over_commit_ratio,
            memory_over_commit_ratio=memory_over_commit_ratio,
        )
        pod.save()
        self.assertEqual(cpu_over_commit_ratio, pod.cpu_over_commit_ratio)
        self.assertEqual(
            memory_over_commit_ratio, pod.memory_over_commit_ratio
        )

    def test_create_with_no_over_commit_ratios(self):
        pod = Pod(power_type="virsh", power_parameters={})
        pod.save()
        self.assertEqual(1, pod.cpu_over_commit_ratio)
        self.assertEqual(1, pod.memory_over_commit_ratio)

    def test_sync_pod_properties_and_hints(self):
        discovered = self.make_discovered_pod()
        discovered.tags = [factory.make_name("tag") for _ in range(3)]
        # Create a subset of the discovered pod's tags
        # to make sure no duplicates are added on sync.
        pod = factory.make_Pod(tags=[discovered.tags[0]])
        self.patch(pod, "sync_machines")
        pod.sync(discovered, factory.make_User())
        self.assertThat(
            pod,
            MatchesStructure(
                architectures=Equals(discovered.architectures),
                cores=Equals(discovered.cores),
                cpu_speed=Equals(discovered.cpu_speed),
                memory=Equals(discovered.memory),
                local_storage=Equals(discovered.local_storage),
                local_disks=Equals(discovered.local_disks),
                iscsi_storage=Equals(discovered.iscsi_storage),
                capabilities=Equals(discovered.capabilities),
                tags=MatchesSetwise(*[Equals(tag) for tag in discovered.tags]),
                hints=MatchesStructure(
                    cores=Equals(discovered.hints.cores),
                    cpu_speed=Equals(discovered.hints.cpu_speed),
                    memory=Equals(discovered.hints.memory),
                    local_storage=Equals(discovered.hints.local_storage),
                    local_disks=Equals(discovered.hints.local_disks),
                ),
                default_storage_pool=MatchesStructure(
                    pool_id=Equals(discovered.storage_pools[0].id)
                ),
                storage_pools=MatchesSetwiseWithAll(
                    *[
                        MatchesStructure(
                            name=Equals(pool.name),
                            pool_id=Equals(pool.id),
                            pool_type=Equals(pool.type),
                            path=Equals(pool.path),
                            storage=Equals(pool.storage),
                        )
                        for pool in discovered.storage_pools
                    ]
                ),
            ),
        )

    def test_sync_pod_creates_new_machines_connected_to_default_vlan(self):
        discovered = self.make_discovered_pod()
        # Set one of the discovered machine's hostnames to something illegal.
        machine = discovered.machines[0]
        machine.hostname = "This is not legal #$%*^@!"
        mock_set_default_storage_layout = self.patch(
            Machine, "set_default_storage_layout"
        )
        mock_set_initial_networking_configuration = self.patch(
            Machine, "set_initial_networking_configuration"
        )
        mock_start_commissioning = self.patch(Machine, "start_commissioning")
        pod = factory.make_Pod()
        pod.sync(discovered, factory.make_User())
        machine_macs = [
            machine.interfaces[0].mac_address
            for machine in discovered.machines
        ]
        created_machines = Machine.objects.filter(
            interface__mac_address__in=machine_macs
        ).distinct()
        default_vlan = Fabric.objects.get_default_fabric().get_default_vlan()
        self.assertThat(
            created_machines,
            MatchesSetwise(
                *[
                    MatchesStructure(
                        architecture=Equals(machine.architecture),
                        bmc=Equals(pod),
                        cpu_count=Equals(machine.cores),
                        cpu_speed=Equals(machine.cpu_speed),
                        memory=Equals(machine.memory),
                        power_state=Equals(machine.power_state),
                        instance_power_parameters=Equals(
                            machine.power_parameters
                        ),
                        creation_type=Equals(NODE_CREATION_TYPE.PRE_EXISTING),
                        tags=MatchesSetwiseWithAll(
                            *[
                                MatchesStructure(name=Equals(tag))
                                for tag in machine.tags
                            ]
                        ),
                        physicalblockdevice_set=MatchesSetwiseWithAll(
                            *[
                                MatchesStructure(
                                    name=Equals(
                                        BlockDevice._get_block_name_from_idx(
                                            idx
                                        )
                                    ),
                                    id_path=Equals(bd.id_path),
                                    model=Equals(bd.model),
                                    serial=Equals(bd.serial),
                                    size=Equals(bd.size),
                                    block_size=Equals(bd.block_size),
                                    tags=MatchesSetwise(
                                        *[Equals(tag) for tag in bd.tags]
                                    ),
                                    storage_pool=Equals(
                                        pod._get_storage_pool_by_id(
                                            bd.storage_pool
                                        )
                                    ),
                                )
                                for idx, bd in enumerate(machine.block_devices)
                                if bd.type == BlockDeviceType.PHYSICAL
                            ]
                        ),
                        iscsiblockdevice_set=MatchesSetwiseWithAll(
                            *[
                                MatchesStructure(
                                    name=Equals(
                                        BlockDevice._get_block_name_from_idx(
                                            idx
                                        )
                                    ),
                                    target=Equals(
                                        get_iscsi_target(bd.iscsi_target)
                                    ),
                                    size=Equals(bd.size),
                                    block_size=Equals(bd.block_size),
                                    tags=MatchesSetwise(
                                        *[Equals(tag) for tag in bd.tags]
                                    ),
                                )
                                for idx, bd in enumerate(machine.block_devices)
                                if bd.type == BlockDeviceType.ISCSI
                            ]
                        ),
                        boot_interface=IsInstance(Interface),
                        interface_set=MatchesSetwiseWithAll(
                            *[
                                MatchesStructure(
                                    name=Equals("eth%d" % idx),
                                    mac_address=Equals(nic.mac_address),
                                    vlan=Equals(default_vlan),
                                    tags=MatchesSetwise(
                                        *[Equals(tag) for tag in nic.tags]
                                    ),
                                )
                                for idx, nic in enumerate(machine.interfaces)
                                if nic.boot
                            ]
                            + [
                                MatchesStructure(
                                    name=Equals("eth%d" % idx),
                                    mac_address=Equals(nic.mac_address),
                                    vlan=Is(None),
                                    tags=MatchesSetwise(
                                        *[Equals(tag) for tag in nic.tags]
                                    ),
                                )
                                for idx, nic in enumerate(machine.interfaces)
                                if not nic.boot
                            ]
                        ),
                    )
                    for machine in discovered.machines
                ]
            ),
        )
        self.assertThat(mock_set_default_storage_layout.call_count, Equals(0))
        self.assertThat(
            mock_set_initial_networking_configuration.call_count, Equals(0)
        )
        self.assertThat(
            mock_start_commissioning.call_count,
            Equals(len(discovered.machines)),
        )

    def test_sync_pod_upgrades_default_storage_pool(self):
        discovered = self.make_discovered_pod(machines=[])
        discovered_default = discovered.storage_pools[2]
        pod = factory.make_Pod(
            parameters={"default_storage_pool": discovered_default.name}
        )
        pod.sync(discovered, factory.make_User())
        self.assertThat(
            pod.default_storage_pool.name, Equals(discovered_default.name)
        )
        self.assertThat(pod.power_parameters, Equals({}))

    def test_sync_pod_sets_default_numanode(self):
        discovered_bdev = self.make_discovered_block_device()
        discovered_iface = self.make_discovered_interface()
        discovered_machine = self.make_discovered_machine(
            block_devices=[discovered_bdev], interfaces=[discovered_iface]
        )
        discovered_pod = self.make_discovered_pod(
            machines=[discovered_machine]
        )
        self.patch(Machine, "start_commissioning")
        pod = factory.make_Pod()
        pod.sync(discovered_pod, factory.make_User())
        [machine] = Machine.objects.all()
        self.assertIsNotNone(machine.default_numanode)
        [bdev] = machine.physicalblockdevice_set.all()
        [iface] = machine.interface_set.all()
        self.assertEqual(bdev.numa_node, machine.default_numanode)
        self.assertEqual(iface.numa_node, machine.default_numanode)

    def test_create_machine_ensures_unique_hostname(self):
        existing_machine = factory.make_Node()
        discovered_machine = self.make_discovered_machine()
        discovered_machine.hostname = existing_machine.hostname
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        pod = factory.make_Pod()
        # Doesn't raise an exception ensures that the hostname is unique as
        # that will cause a database exception.
        pod.create_machine(discovered_machine, factory.make_User())

    def test_create_machine_invalid_hostname(self):
        discovered_machine = self.make_discovered_machine()
        discovered_machine.hostname = "invalid_name"
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        pod = factory.make_Pod()
        # Doesn't raise an exception ensures that the hostname is unique as
        # that will cause a database exception.
        machine = pod.create_machine(discovered_machine, factory.make_User())
        self.assertNotEqual(machine.hostname, "invalid_name")

    def test_create_machine_pod_pool(self):
        discovered_machine = self.make_discovered_machine()
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        pool = factory.make_ResourcePool()
        pod = factory.make_Pod(pool=pool)
        machine = pod.create_machine(discovered_machine, factory.make_User())
        self.assertEqual(pool, machine.pool)

    def test_create_machine_sets_zone(self):
        discovered_machine = self.make_discovered_machine()
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        zone = factory.make_Zone()
        pod = factory.make_Pod(zone=zone)
        machine = pod.create_machine(discovered_machine, factory.make_User())
        self.assertEqual(zone, machine.zone)

    def test_create_machine_sets_pod_tags_on_machine(self):
        discovered_machine = self.make_discovered_machine()
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        pod = factory.make_Pod()
        tag = factory.make_Tag()
        pod.add_tag(tag.name)
        machine = pod.create_machine(discovered_machine, factory.make_User())
        self.assertTrue(tag in machine.tags.all())

    def test_create_machine_sets_interface_names_using_constraint_labels(self):
        discovered_machine = self.make_discovered_machine()
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        vlan = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        vlan2 = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=False,
            primary_rack=factory.make_RackController(),
        )
        vlan3 = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=False,
            primary_rack=factory.make_RackController(),
        )
        pod = factory.make_Pod()
        machine = pod.create_machine(
            discovered_machine,
            factory.make_User(),
            interfaces=LabeledConstraintMap(
                "maas0:vlan=id:%d;maas1:vlan=id:%d;maas2:vlan=id:%d"
                % (vlan.id, vlan2.id, vlan3.id)
            ),
        )
        # Check that the interface names match the labels provided in the
        # constraints string.
        self.assertItemsEqual(
            ["maas0", "maas1", "maas2"],
            list(
                machine.interface_set.order_by("id").values_list(
                    "name", flat=True
                )
            ),
        )

    def test_create_machine_allocates_requested_ip_addresses(self):
        discovered_machine = self.make_discovered_machine()
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        vlan = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        subnet = factory.make_Subnet(vlan=vlan)
        ip = factory.pick_ip_in_Subnet(subnet)
        vlan2 = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=False,
            primary_rack=factory.make_RackController(),
        )
        subnet2 = factory.make_Subnet(vlan=vlan2)
        ip2 = factory.pick_ip_in_Subnet(subnet2)
        vlan3 = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=False,
            primary_rack=factory.make_RackController(),
        )
        subnet3 = factory.make_Subnet(vlan=vlan3)
        ip3 = factory.pick_ip_in_Subnet(subnet3)
        pod = factory.make_Pod()
        rmi = RequestedMachineInterface(ifname="maas0", requested_ips=[ip])
        rmi2 = RequestedMachineInterface(ifname="maas1", requested_ips=[ip2])
        rmi3 = RequestedMachineInterface(ifname="maas2", requested_ips=[ip3])
        requested_machine = RequestedMachine(
            hostname="foo",
            architecture="amd64",
            cores=1,
            memory=1024,
            block_devices=[],
            interfaces=[rmi, rmi2, rmi3],
        )
        machine = pod.create_machine(
            discovered_machine,
            factory.make_User(),
            interfaces=LabeledConstraintMap(
                "maas0:vlan=id:%d;maas1:vlan=id:%d;maas2:ip=%s"
                % (vlan.id, vlan2.id, ip3)
            ),
            requested_machine=requested_machine,
        )
        sip = StaticIPAddress.objects.filter(ip=ip).first()
        self.assertThat(sip.get_interface().node, Equals(machine))
        sip2 = StaticIPAddress.objects.filter(ip=ip2).first()
        self.assertThat(sip2.get_interface().node, Equals(machine))
        sip3 = StaticIPAddress.objects.filter(ip=ip3).first()
        self.assertThat(sip3.get_interface().node, Equals(machine))

    def test_create_machine_unconfigures_ips_upon_request(self):
        discovered_machine = self.make_discovered_machine()
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        vlan = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        subnet = factory.make_Subnet(vlan=vlan)
        ip = factory.pick_ip_in_Subnet(subnet)
        vlan2 = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=False,
            primary_rack=factory.make_RackController(),
        )
        subnet2 = factory.make_Subnet(vlan=vlan2)
        ip2 = factory.pick_ip_in_Subnet(subnet2)
        vlan3 = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=False,
            primary_rack=factory.make_RackController(),
        )
        subnet3 = factory.make_Subnet(vlan=vlan3)
        ip3 = factory.pick_ip_in_Subnet(subnet3)
        pod = factory.make_Pod()
        rmi = RequestedMachineInterface(
            ifname="maas0", requested_ips=[ip], ip_mode="unconfigured"
        )
        rmi2 = RequestedMachineInterface(
            ifname="maas1", requested_ips=[ip2], ip_mode="unconfigured"
        )
        rmi3 = RequestedMachineInterface(
            ifname="maas2", requested_ips=[ip3], ip_mode="unconfigured"
        )
        requested_machine = RequestedMachine(
            hostname="foo",
            architecture="amd64",
            cores=1,
            memory=1024,
            block_devices=[],
            interfaces=[rmi, rmi2, rmi3],
        )
        machine = pod.create_machine(
            discovered_machine,
            factory.make_User(),
            interfaces=LabeledConstraintMap(
                "maas0:vlan=id:%d,mode=unconfigured;"
                "maas1:vlan=id:%d,mode=unconfigured;"
                "maas2:ip=%s,mode=unconfigured" % (vlan.id, vlan2.id, ip3)
            ),
            requested_machine=requested_machine,
        )
        sip = StaticIPAddress.objects.filter(
            interface__name=rmi.ifname
        ).first()
        self.assertThat(sip.get_interface().node, Equals(machine))
        self.assertThat(sip.ip, Is(None))
        sip2 = StaticIPAddress.objects.filter(
            interface__name=rmi2.ifname
        ).first()
        self.assertThat(sip2.get_interface().node, Equals(machine))
        self.assertThat(sip2.ip, Is(None))
        sip3 = StaticIPAddress.objects.filter(
            interface__name=rmi3.ifname
        ).first()
        self.assertThat(sip3.get_interface().node, Equals(machine))
        self.assertThat(sip3.ip, Is(None))

    def test_create_machine_sets_up_interface_vlans_correctly(self):
        discovered_machine = self.make_discovered_machine()
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        controller = factory.make_RackController()
        vlan = factory.make_VLAN(
            fabric=fabric, dhcp_on=True, primary_rack=controller
        )
        vlan2 = factory.make_VLAN(
            fabric=fabric, dhcp_on=False, primary_rack=controller
        )
        vlan3 = factory.make_VLAN(
            fabric=fabric, dhcp_on=False, primary_rack=controller
        )
        # Create subnets, so we can test to ensure they get linked up.
        subnet = factory.make_Subnet(vlan=vlan)
        factory.make_Subnet(vlan=vlan2)
        factory.make_Subnet(vlan=vlan3)
        eth0 = factory.make_Interface(node=controller, vlan=vlan)
        eth1 = factory.make_Interface(node=controller, vlan=vlan2)
        eth2 = factory.make_Interface(node=controller, vlan=vlan3)
        br0 = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE,
            node=controller,
            vlan=vlan,
            parents=[eth0],
        )
        ip = factory.make_StaticIPAddress(subnet=subnet, interface=br0)
        discovered_machine.interfaces[
            0
        ].attach_type = InterfaceAttachType.BRIDGE
        discovered_machine.interfaces[0].attach_name = br0.name
        discovered_machine.interfaces[
            1
        ].attach_type = InterfaceAttachType.MACVLAN
        discovered_machine.interfaces[1].attach_name = eth1.name
        discovered_machine.interfaces[
            2
        ].attach_type = InterfaceAttachType.MACVLAN
        discovered_machine.interfaces[2].attach_name = eth2.name
        pod = factory.make_Pod(ip_address=ip)
        # Skip commissioning on creation so that we can test that VLANs
        # are properly set based on the interface constraint.
        machine = pod.create_machine(
            discovered_machine,
            factory.make_User(),
            # Use numeric names to mimic what Juju will do.
            interfaces=LabeledConstraintMap(
                "0:vlan=id:%d;1:vlan=id:%d;2:vlan=id:%d"
                % (vlan.id, vlan2.id, vlan3.id)
            ),
        )
        interfaces = {
            interface.name: interface
            for interface in machine.interface_set.all()
        }
        self.assertThat(interfaces["eth0"].vlan, Equals(vlan))
        self.assertThat(interfaces["eth1"].vlan, Equals(vlan2))
        self.assertThat(interfaces["eth2"].vlan, Equals(vlan3))
        # Make sure all interfaces also have a subnet link.
        self.assertThat(interfaces["eth0"].ip_addresses.count(), Equals(1))
        self.assertThat(interfaces["eth1"].ip_addresses.count(), Equals(1))
        self.assertThat(interfaces["eth2"].ip_addresses.count(), Equals(1))

    def test_create_machine_uses_default_ifnames_if_discovered_mismatch(self):
        """This makes sure that if the discovered machine comes back with
        a different number of interfaces than the constraint string, the
        default (ethX) names are used.
        """
        discovered_machine = self.make_discovered_machine()
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        vlan = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        vlan2 = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=False,
            primary_rack=factory.make_RackController(),
        )
        pod = factory.make_Pod()
        # The constraint here as two labels, but the discovered machine will
        # have three interfaces.
        machine = pod.create_machine(
            discovered_machine,
            factory.make_User(),
            interfaces=LabeledConstraintMap(
                "maas0:vlan=id:%d;maas1:vlan=id:%d" % (vlan.id, vlan2.id)
            ),
        )
        # Check that the interface names use the ethX numbering, since the
        # provided constraints won't match the number of interfaces that were
        # returned.
        self.assertItemsEqual(
            ["eth0", "eth1", "eth2"],
            list(
                machine.interface_set.order_by("id").values_list(
                    "name", flat=True
                )
            ),
        )

    def test_create_machine_uses_default_names_if_no_interfaces(self):
        discovered_machine = self.make_discovered_machine()
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        pod = factory.make_Pod()
        machine = pod.create_machine(discovered_machine, factory.make_User())
        # Check that the interface names match the labels provided in the
        # constraints string.
        self.assertItemsEqual(
            ["eth0", "eth1", "eth2"],
            list(
                machine.interface_set.order_by("id").values_list(
                    "name", flat=True
                )
            ),
        )

    def test_sync_pod_creates_new_machines_connected_to_dhcp_vlan(self):
        discovered = self.make_discovered_pod()
        mock_set_default_storage_layout = self.patch(
            Machine, "set_default_storage_layout"
        )
        mock_set_initial_networking_configuration = self.patch(
            Machine, "set_initial_networking_configuration"
        )
        mock_start_commissioning = self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        vlan = factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        pod = factory.make_Pod()
        pod.sync(discovered, factory.make_User())
        machine_macs = [
            machine.interfaces[0].mac_address
            for machine in discovered.machines
        ]
        created_machines = Machine.objects.filter(
            interface__mac_address__in=machine_macs
        ).distinct()
        self.assertThat(
            created_machines,
            MatchesSetwise(
                *[
                    MatchesStructure(
                        architecture=Equals(machine.architecture),
                        bmc=Equals(pod),
                        cpu_count=Equals(machine.cores),
                        cpu_speed=Equals(machine.cpu_speed),
                        memory=Equals(machine.memory),
                        power_state=Equals(machine.power_state),
                        instance_power_parameters=Equals(
                            machine.power_parameters
                        ),
                        creation_type=Equals(NODE_CREATION_TYPE.PRE_EXISTING),
                        tags=MatchesSetwiseWithAll(
                            *[
                                MatchesStructure(name=Equals(tag))
                                for tag in machine.tags
                            ]
                        ),
                        physicalblockdevice_set=MatchesSetwiseWithAll(
                            *[
                                MatchesStructure(
                                    name=Equals(
                                        BlockDevice._get_block_name_from_idx(
                                            idx
                                        )
                                    ),
                                    id_path=Equals(bd.id_path),
                                    model=Equals(bd.model),
                                    serial=Equals(bd.serial),
                                    size=Equals(bd.size),
                                    block_size=Equals(bd.block_size),
                                    tags=MatchesSetwise(
                                        *[Equals(tag) for tag in bd.tags]
                                    ),
                                    storage_pool=Equals(
                                        pod._get_storage_pool_by_id(
                                            bd.storage_pool
                                        )
                                    ),
                                )
                                for idx, bd in enumerate(machine.block_devices)
                                if bd.type == BlockDeviceType.PHYSICAL
                            ]
                        ),
                        iscsiblockdevice_set=MatchesSetwiseWithAll(
                            *[
                                MatchesStructure(
                                    name=Equals(
                                        BlockDevice._get_block_name_from_idx(
                                            idx
                                        )
                                    ),
                                    target=Equals(
                                        get_iscsi_target(bd.iscsi_target)
                                    ),
                                    size=Equals(bd.size),
                                    block_size=Equals(bd.block_size),
                                    tags=MatchesSetwise(
                                        *[Equals(tag) for tag in bd.tags]
                                    ),
                                )
                                for idx, bd in enumerate(machine.block_devices)
                                if bd.type == BlockDeviceType.ISCSI
                            ]
                        ),
                        boot_interface=IsInstance(Interface),
                        interface_set=MatchesSetwiseWithAll(
                            *[
                                MatchesStructure(
                                    name=Equals("eth%d" % idx),
                                    mac_address=Equals(nic.mac_address),
                                    vlan=Equals(vlan),
                                    tags=MatchesSetwise(
                                        *[Equals(tag) for tag in nic.tags]
                                    ),
                                )
                                for idx, nic in enumerate(machine.interfaces)
                                if nic.boot
                            ]
                            + [
                                MatchesStructure(
                                    name=Equals("eth%d" % idx),
                                    mac_address=Equals(nic.mac_address),
                                    vlan=Is(None),
                                    tags=MatchesSetwise(
                                        *[Equals(tag) for tag in nic.tags]
                                    ),
                                )
                                for idx, nic in enumerate(machine.interfaces)
                                if not nic.boot
                            ]
                        ),
                    )
                    for machine in discovered.machines
                ]
            ),
        )
        self.assertThat(mock_set_default_storage_layout.call_count, Equals(0))
        self.assertThat(
            mock_set_initial_networking_configuration.call_count, Equals(0)
        )
        self.assertThat(
            mock_start_commissioning.call_count,
            Equals(len(discovered.machines)),
        )

    def test_create_machine_with_bad_physical_block_device(self):
        block_device = self.make_discovered_block_device()
        block_device.serial = None
        block_device.id_path = None
        machine = self.make_discovered_machine(block_devices=[block_device])
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        pod = factory.make_Pod()
        pod.create_machine(machine, factory.make_User())
        created_machine = Machine.objects.get(
            interface__mac_address=machine.interfaces[0].mac_address
        )
        self.assertThat(
            created_machine,
            MatchesStructure(
                architecture=Equals(machine.architecture),
                bmc=Equals(pod),
                cpu_count=Equals(machine.cores),
                cpu_speed=Equals(machine.cpu_speed),
                memory=Equals(machine.memory),
                power_state=Equals(machine.power_state),
                instance_power_parameters=Equals(machine.power_parameters),
                creation_type=Equals(NODE_CREATION_TYPE.PRE_EXISTING),
                tags=MatchesSetwiseWithAll(
                    *[
                        MatchesStructure(name=Equals(tag))
                        for tag in machine.tags
                    ]
                ),
                physicalblockdevice_set=MatchesSetwiseWithAll(),
            ),
        )

    def test_create_machine_doesnt_allow_bad_physical_block_device(self):
        block_device = self.make_discovered_block_device()
        block_device.serial = None
        block_device.id_path = None
        machine = self.make_discovered_machine(block_devices=[block_device])
        self.patch(Machine, "set_default_storage_layout")
        self.patch(Machine, "set_initial_networking_configuration")
        self.patch(Machine, "start_commissioning")
        fabric = factory.make_Fabric()
        factory.make_VLAN(
            fabric=fabric,
            dhcp_on=True,
            primary_rack=factory.make_RackController(),
        )
        pod = factory.make_Pod()
        self.assertRaises(
            ValidationError,
            pod.create_machine,
            machine,
            factory.make_User(),
            skip_commissioning=True,
        )

    def test_sync_pod_deletes_missing_machines(self):
        pod = factory.make_Pod()
        machine = factory.make_Node()
        machine.bmc = pod
        machine.save()
        discovered = self.make_discovered_pod(machines=[])
        pod.sync(discovered, factory.make_User())
        self.assertIsNone(reload_object(machine))

    def test_sync_moves_machine_under_pod(self):
        pod = factory.make_Pod()
        machine = factory.make_Node(interface=True)
        discovered_interface = self.make_discovered_interface(
            mac_address=machine.interface_set.first().mac_address
        )
        discovered_machine = self.make_discovered_machine(
            interfaces=[discovered_interface]
        )
        discovered_pod = self.make_discovered_pod(
            machines=[discovered_machine]
        )
        pod.sync(discovered_pod, factory.make_User())
        machine = reload_object(machine)
        self.assertThat(machine.bmc.id, Equals(pod.id))

    def test_sync_keeps_rack_controller_pod_nodes(self):
        pod = factory.make_Pod()
        controller = factory.make_RackController(interface=True)
        discovered_interface = self.make_discovered_interface(
            mac_address=controller.interface_set.first().mac_address
        )
        discovered_machine = self.make_discovered_machine(
            interfaces=[discovered_interface]
        )
        discovered_pod = self.make_discovered_pod(
            machines=[discovered_machine]
        )
        pod.sync(discovered_pod, factory.make_User())
        controller = reload_object(controller)
        self.assertThat(
            controller.node_type, Equals(NODE_TYPE.RACK_CONTROLLER)
        )

    def test_sync_updates_machine_properties_for_dynamic(self):
        pod = factory.make_Pod()
        machine = factory.make_Node(
            interface=True, creation_type=NODE_CREATION_TYPE.DYNAMIC
        )
        discovered_interface = self.make_discovered_interface(
            mac_address=machine.interface_set.first().mac_address
        )
        discovered_machine = self.make_discovered_machine(
            interfaces=[discovered_interface]
        )
        discovered_pod = self.make_discovered_pod(
            machines=[discovered_machine]
        )
        pod.sync(discovered_pod, factory.make_User())
        machine = reload_object(machine)
        self.assertThat(
            machine,
            MatchesStructure(
                architecture=Equals(discovered_machine.architecture),
                cpu_count=Equals(discovered_machine.cores),
                cpu_speed=Equals(discovered_machine.cpu_speed),
                memory=Equals(discovered_machine.memory),
                power_state=Equals(discovered_machine.power_state),
                instance_power_parameters=Equals(
                    discovered_machine.power_parameters
                ),
                tags=MatchesSetwiseWithAll(
                    *[
                        MatchesStructure(name=Equals(tag))
                        for tag in discovered_machine.tags
                    ]
                ),
            ),
        )

    def test_sync_updates_machine_properties_for_not_dynamic(self):
        pod = factory.make_Pod()
        machine = factory.make_Node(
            interface=True,
            creation_type=random.choice(
                [NODE_CREATION_TYPE.PRE_EXISTING, NODE_CREATION_TYPE.MANUAL]
            ),
        )
        discovered_interface = self.make_discovered_interface(
            mac_address=machine.interface_set.first().mac_address
        )
        discovered_machine = self.make_discovered_machine(
            interfaces=[discovered_interface]
        )
        discovered_pod = self.make_discovered_pod(
            machines=[discovered_machine]
        )
        pod.sync(discovered_pod, factory.make_User())
        machine = reload_object(machine)
        self.assertThat(
            machine,
            MatchesStructure(
                architecture=Equals(machine.architecture),
                cpu_count=Equals(machine.cpu_count),
                cpu_speed=Equals(machine.cpu_speed),
                memory=Equals(machine.memory),
                power_state=Equals(discovered_machine.power_state),
                instance_power_parameters=Equals(
                    discovered_machine.power_parameters
                ),
                tags=MatchesSetwiseWithAll(
                    *[
                        MatchesStructure(name=Equals(tag))
                        for tag in machine.tags.all()
                    ]
                ),
            ),
        )

    def test_sync_updates_machine_bmc_deletes_old_bmc(self):
        pod = factory.make_Pod()
        machine = factory.make_Node(interface=True)
        old_bmc = factory.make_BMC()
        machine.bmc = old_bmc
        machine.save()
        discovered_interface = self.make_discovered_interface(
            mac_address=machine.interface_set.first().mac_address
        )
        discovered_machine = self.make_discovered_machine(
            interfaces=[discovered_interface]
        )
        discovered_pod = self.make_discovered_pod(
            machines=[discovered_machine]
        )
        pod.sync(discovered_pod, factory.make_User())
        machine = reload_object(machine)
        old_bmc = reload_object(old_bmc)
        self.assertIsNone(old_bmc)
        self.assertThat(machine.bmc, Equals(pod))

    def test_sync_updates_machine_bmc_keeps_old_bmc(self):
        pod = factory.make_Pod()
        rack_controller = factory.make_RackController()
        machine = factory.make_Node(
            interface=True,
            power_type="virsh",
            bmc_connected_to=rack_controller,
        )
        old_bmc = machine.bmc

        # Create another machine sharing the BMC. This should prevent the
        # BMC from being deleted.
        other_machine = factory.make_Node(interface=True)
        other_machine.bmc = machine.bmc
        other_machine.instance_power_parameter = {
            "power_id": factory.make_name("power_id")
        }
        other_machine.save()

        discovered_interface = self.make_discovered_interface(
            mac_address=machine.interface_set.first().mac_address
        )
        discovered_machine = self.make_discovered_machine(
            interfaces=[discovered_interface]
        )
        discovered_pod = self.make_discovered_pod(
            machines=[discovered_machine]
        )
        pod.sync(discovered_pod, factory.make_User())
        machine = reload_object(machine)
        old_bmc = reload_object(old_bmc)
        self.assertIsNotNone(old_bmc)
        self.assertThat(machine.bmc.as_self(), Equals(pod))

    def test_sync_updates_existing_machine_block_devices_for_dynamic(self):
        pod = factory.make_Pod()
        machine = factory.make_Node(
            with_boot_disk=False, creation_type=NODE_CREATION_TYPE.DYNAMIC
        )
        boot_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=machine
        )
        keep_model_bd = factory.make_PhysicalBlockDevice(node=machine)
        keep_path_bd = factory.make_PhysicalBlockDevice(
            node=machine, id_path=factory.make_name("id_path")
        )
        keep_iscsi_bd = factory.make_ISCSIBlockDevice(node=machine)
        # ISCIBlockDevice that exists on another machine. It should be
        # moved to this machine.
        other_iscsi_bd = factory.make_ISCSIBlockDevice()
        delete_model_bd = factory.make_PhysicalBlockDevice(node=machine)
        delete_path_bd = factory.make_PhysicalBlockDevice(
            node=machine, id_path=factory.make_name("id_path")
        )
        delete_iscsi_bd = factory.make_ISCSIBlockDevice(node=machine)
        dkeep_model_bd = self.make_discovered_block_device(
            model=keep_model_bd.model, serial=keep_model_bd.serial
        )
        dkeep_path_bd = self.make_discovered_block_device(
            id_path=keep_path_bd.id_path
        )
        dkeep_iscsi_bd = self.make_discovered_block_device(
            target=keep_iscsi_bd.target, block_type=BlockDeviceType.ISCSI
        )
        dother_iscsi_bd = self.make_discovered_block_device(
            target=other_iscsi_bd.target, block_type=BlockDeviceType.ISCSI
        )
        dnew_model_bd = self.make_discovered_block_device()
        dnew_path_bd = self.make_discovered_block_device(
            id_path=factory.make_name("id_path")
        )
        dnew_iscsi_bd = self.make_discovered_block_device(
            block_type=BlockDeviceType.ISCSI
        )
        discovered_machine = self.make_discovered_machine(
            block_devices=[
                dkeep_model_bd,
                dkeep_path_bd,
                dkeep_iscsi_bd,
                dother_iscsi_bd,
                dnew_model_bd,
                dnew_path_bd,
                dnew_iscsi_bd,
            ],
            interfaces=[
                self.make_discovered_interface(
                    mac_address=boot_interface.mac_address
                )
            ],
        )
        discovered_pod = self.make_discovered_pod(
            machines=[discovered_machine]
        )
        pod.sync(discovered_pod, factory.make_User())
        machine = reload_object(machine)
        keep_model_bd = reload_object(keep_model_bd)
        keep_path_bd = reload_object(keep_path_bd)
        keep_iscsi_bd = reload_object(keep_iscsi_bd)
        other_iscsi_bd = reload_object(other_iscsi_bd)
        delete_model_bd = reload_object(delete_model_bd)
        delete_path_bd = reload_object(delete_path_bd)
        delete_iscsi_bd = reload_object(delete_iscsi_bd)
        new_model_bd = PhysicalBlockDevice.objects.filter(
            node=machine,
            model=dnew_model_bd.model,
            serial=dnew_model_bd.serial,
        ).first()
        new_path_bd = PhysicalBlockDevice.objects.filter(
            node=machine, id_path=dnew_path_bd.id_path
        ).first()
        new_iscsi_bd = ISCSIBlockDevice.objects.filter(
            node=machine, target=get_iscsi_target(dnew_iscsi_bd.iscsi_target)
        ).first()
        self.assertIsNone(delete_model_bd)
        self.assertIsNone(delete_path_bd)
        self.assertIsNone(delete_iscsi_bd)
        self.assertThat(
            keep_model_bd,
            MatchesStructure(
                size=Equals(dkeep_model_bd.size),
                block_size=Equals(dkeep_model_bd.block_size),
                tags=MatchesSetwise(
                    *[Equals(tag) for tag in dkeep_model_bd.tags]
                ),
            ),
        )
        self.assertThat(
            keep_path_bd,
            MatchesStructure(
                size=Equals(dkeep_path_bd.size),
                block_size=Equals(dkeep_path_bd.block_size),
                tags=MatchesSetwise(
                    *[Equals(tag) for tag in dkeep_path_bd.tags]
                ),
            ),
        )
        self.assertThat(
            keep_iscsi_bd,
            MatchesStructure(
                size=Equals(dkeep_iscsi_bd.size),
                block_size=Equals(dkeep_iscsi_bd.block_size),
                tags=MatchesSetwise(
                    *[Equals(tag) for tag in dkeep_iscsi_bd.tags]
                ),
            ),
        )
        self.assertThat(
            other_iscsi_bd,
            MatchesStructure(
                node=Equals(machine),
                size=Equals(dother_iscsi_bd.size),
                block_size=Equals(dother_iscsi_bd.block_size),
                tags=MatchesSetwise(
                    *[Equals(tag) for tag in dother_iscsi_bd.tags]
                ),
            ),
        )
        self.assertThat(
            new_model_bd,
            MatchesStructure(
                size=Equals(dnew_model_bd.size),
                block_size=Equals(dnew_model_bd.block_size),
                tags=MatchesSetwise(
                    *[Equals(tag) for tag in dnew_model_bd.tags]
                ),
            ),
        )
        self.assertThat(
            new_path_bd,
            MatchesStructure(
                size=Equals(dnew_path_bd.size),
                block_size=Equals(dnew_path_bd.block_size),
                tags=MatchesSetwise(
                    *[Equals(tag) for tag in dnew_path_bd.tags]
                ),
            ),
        )
        self.assertThat(
            new_iscsi_bd,
            MatchesStructure(
                size=Equals(dnew_iscsi_bd.size),
                block_size=Equals(dnew_iscsi_bd.block_size),
                tags=MatchesSetwise(
                    *[Equals(tag) for tag in dnew_iscsi_bd.tags]
                ),
            ),
        )

    def test_sync_updates_existing_machine_interfaces_for_dynamic(self):
        pod = factory.make_Pod()
        machine = factory.make_Node(creation_type=NODE_CREATION_TYPE.DYNAMIC)
        other_vlan = factory.make_Fabric().get_default_vlan()
        keep_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=machine, vlan=other_vlan
        )
        delete_interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=machine
        )
        dkeep_interface = self.make_discovered_interface(
            mac_address=keep_interface.mac_address
        )
        dnew_interface = self.make_discovered_interface()
        dnew_interface.boot = True
        discovered_machine = self.make_discovered_machine(
            interfaces=[dkeep_interface, dnew_interface]
        )
        discovered_pod = self.make_discovered_pod(
            machines=[discovered_machine]
        )
        pod.sync(discovered_pod, factory.make_User())
        machine = reload_object(machine)
        keep_interface = reload_object(keep_interface)
        delete_interface = reload_object(delete_interface)
        new_interface = machine.interface_set.filter(
            mac_address=dnew_interface.mac_address
        ).first()
        self.assertIsNone(delete_interface)
        self.assertThat(
            keep_interface,
            MatchesStructure(
                vlan=Equals(other_vlan),
                tags=MatchesSetwise(
                    *[Equals(tag) for tag in dkeep_interface.tags]
                ),
            ),
        )
        self.assertThat(
            new_interface,
            MatchesStructure(
                vlan=Equals(
                    Fabric.objects.get_default_fabric().get_default_vlan()
                ),
                tags=MatchesSetwise(
                    *[Equals(tag) for tag in dnew_interface.tags]
                ),
            ),
        )
        self.assertEqual(new_interface, machine.boot_interface)

    def test_get_used_cores(self):
        pod = factory.make_Pod()
        total_cores = 0
        for _ in range(3):
            cores = random.randint(1, 4)
            total_cores += cores
            factory.make_Node(bmc=pod, cpu_count=cores)
        self.assertEquals(total_cores, pod.get_used_cores())

    def test_get_used_memory(self):
        pod = factory.make_Pod()
        total_memory = 0
        for _ in range(3):
            memory = random.randint(1, 4)
            total_memory += memory
            factory.make_Node(bmc=pod, memory=memory)
        self.assertEquals(total_memory, pod.get_used_memory())

    def test_get_used_local_storage(self):
        pod = factory.make_Pod()
        total_storage = 0
        for _ in range(3):
            storage = random.randint(1024 ** 3, 4 * (1024 ** 3))
            total_storage += storage
            node = factory.make_Node(bmc=pod, with_boot_disk=False)
            factory.make_PhysicalBlockDevice(node=node, size=storage)
        self.assertEquals(total_storage, pod.get_used_local_storage())

    def test_get_used_local_disks(self):
        pod = factory.make_Pod()
        for _ in range(3):
            node = factory.make_Node(bmc=pod, with_boot_disk=False)
            for _ in range(3):
                factory.make_PhysicalBlockDevice(node=node)
        self.assertEquals(9, pod.get_used_local_disks())

    def test_get_used_iscsi_storage(self):
        pod = factory.make_Pod()
        total_storage = 0
        for _ in range(3):
            storage = random.randint(1024 ** 3, 4 * (1024 ** 3))
            total_storage += storage
            node = factory.make_Node(bmc=pod, with_boot_disk=False)
            factory.make_ISCSIBlockDevice(node=node, size=storage)
        self.assertEquals(total_storage, pod.get_used_iscsi_storage())


class TestPodDelete(MAASTransactionServerTestCase):
    def test_delete_is_not_allowed(self):
        pod = factory.make_Pod()
        self.assertRaises(AttributeError, pod.delete)

    @wait_for_reactor
    @inlineCallbacks
    def test_delete_async_simply_deletes_empty_pod(self):
        pod = yield deferToDatabase(factory.make_Pod)
        yield pod.async_delete()
        pod = yield deferToDatabase(reload_object, pod)
        self.assertIsNone(pod)

    @wait_for_reactor
    @inlineCallbacks
    def test_decomposes_and_deletes_machines_and_pod(self):
        pod = yield deferToDatabase(factory.make_Pod)
        decomposable_machine = yield deferToDatabase(
            factory.make_Machine,
            bmc=pod,
            creation_type=NODE_CREATION_TYPE.MANUAL,
        )
        delete_machine = yield deferToDatabase(factory.make_Machine, bmc=pod)
        client = Mock()
        client.return_value = succeed({"hints": None})
        self.patch(
            bmc_module, "getClientFromIdentifiers"
        ).return_value = client
        yield pod.async_delete()
        self.assertThat(
            client,
            MockCalledOnceWith(
                DecomposeMachine,
                type=pod.power_type,
                context=pod.power_parameters,
                pod_id=pod.id,
                name=pod.name,
            ),
        )
        decomposable_machine = yield deferToDatabase(
            reload_object, decomposable_machine
        )
        delete_machine = yield deferToDatabase(reload_object, delete_machine)
        pod = yield deferToDatabase(reload_object, pod)
        self.assertIsNone(decomposable_machine)
        self.assertIsNone(delete_machine)
        self.assertIsNone(pod)

    @wait_for_reactor
    @inlineCallbacks
    def test_decomposes_handles_failure_after_one_successful(self):
        pod = yield deferToDatabase(factory.make_Pod)
        decomposable_machine_one = yield deferToDatabase(
            factory.make_Machine,
            bmc=pod,
            creation_type=NODE_CREATION_TYPE.MANUAL,
        )
        decomposable_machine_two = yield deferToDatabase(
            factory.make_Machine,
            bmc=pod,
            creation_type=NODE_CREATION_TYPE.MANUAL,
        )
        delete_machine = yield deferToDatabase(factory.make_Machine, bmc=pod)
        client = Mock()
        client.side_effect = [
            succeed({"hints": sentinel.hints}),
            fail(PodProblem()),
        ]
        self.patch(
            bmc_module, "getClientFromIdentifiers"
        ).return_value = client
        yield pod.async_delete()
        # All the machines should have been deleted.
        decomposable_machine_one = yield deferToDatabase(
            reload_object, decomposable_machine_one
        )
        decomposable_machine_two = yield deferToDatabase(
            reload_object, decomposable_machine_two
        )
        delete_machine = yield deferToDatabase(reload_object, delete_machine)
        pod = yield deferToDatabase(reload_object, pod)
        self.assertIsNone(decomposable_machine_one)
        self.assertIsNone(decomposable_machine_two)
        self.assertIsNone(delete_machine)
        self.assertIsNone(pod)


class TestPodDefaultMACVlanMode(MAASServerTestCase):
    def test_allows_default_macvlan_mode(self):
        pod = factory.make_Pod()
        default_macvlan_mode = random.choice(
            ["bridge", "private", "vepa", "passthru"]
        )
        pod.default_macvlan_mode = default_macvlan_mode
        pod.save()
        self.assertThat(pod.default_macvlan_mode, Equals(default_macvlan_mode))

    def test_default_default_macvlan_mode_is_None(self):
        pod = factory.make_Pod()
        self.assertThat(pod.default_macvlan_mode, Equals(None))


class TestGetRequestedIPs(MAASServerTestCase):
    def test__returns_empty_dict_if_no_requested_machine(self):
        self.assertThat(get_requested_ips(None), Equals({}))

    def test__returns_empty_dict_if_no_interfaces_are_named(self):
        interface = RequestedMachineInterface()
        interface2 = RequestedMachineInterface()
        interfaces = [interface, interface2]
        requested_machine = RequestedMachine(
            factory.make_hostname(), "amd64", 1, 1024, [], interfaces
        )
        self.assertThat(get_requested_ips(requested_machine), Equals({}))

    def test__returns_ifname_to_ip_list_dict_if_specified(self):
        interface = RequestedMachineInterface(
            ifname="eth0", requested_ips=["10.0.0.1", "2001:db8::1"]
        )
        interface2 = RequestedMachineInterface(
            ifname="eth1", requested_ips=["10.0.0.2", "2001:db8::2"]
        )
        interfaces = [interface, interface2]
        requested_machine = RequestedMachine(
            factory.make_hostname(), "amd64", 1, 1024, [], interfaces
        )
        self.assertThat(
            get_requested_ips(requested_machine),
            Equals(
                {
                    "eth0": ["10.0.0.1", "2001:db8::1"],
                    "eth1": ["10.0.0.2", "2001:db8::2"],
                }
            ),
        )

    def test__leaves_out_keys_with_no_assigned_ips(self):
        interface = RequestedMachineInterface(
            ifname="eth0", requested_ips=["10.0.0.1", "2001:db8::1"]
        )
        interface2 = RequestedMachineInterface(ifname="eth1", requested_ips=[])
        interfaces = [interface, interface2]
        requested_machine = RequestedMachine(
            factory.make_hostname(), "amd64", 1, 1024, [], interfaces
        )
        self.assertThat(
            get_requested_ips(requested_machine),
            Equals({"eth0": ["10.0.0.1", "2001:db8::1"]}),
        )
