# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `provisioningserver.drivers.hardware.vmware`.
"""

__all__ = []


import random

from maastesting.factory import factory
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
from provisioningserver.drivers.hardware import vmware
from provisioningserver.drivers.hardware.vmware import (
    VMwarePyvmomiAPI,
    VMwareVMNotFound,
)
from provisioningserver.utils.twisted import asynchronous
from testtools import ExpectedException
from testtools.matchers import Equals, Is, IsInstance, Not
from twisted.internet.defer import inlineCallbacks
from twisted.internet.threads import deferToThread


class FakeVmomiVMSummaryConfig(object):
    def __init__(self, name, has_instance_uuid=None, has_uuid=None):
        self.name = name
        self.guestId = random.choice(["otherLinux64Guest", "otherLinuxGuest"])
        if has_instance_uuid is None:
            has_instance_uuid = random.choice([True, False])
        if has_instance_uuid:
            self.instanceUuid = factory.make_UUID()
        if has_uuid is None:
            has_uuid = random.choice([True, False])
        if has_uuid:
            self.uuid = factory.make_UUID()


class FakeVmomiVMSummary(object):
    def __init__(self, name, has_instance_uuid=None, has_uuid=None):
        self.config = FakeVmomiVMSummaryConfig(
            name, has_instance_uuid=has_instance_uuid, has_uuid=has_uuid
        )


class FakeVmomiVMRuntime(object):
    def __init__(self):
        # add an invalid power state into the mix
        self.powerState = random.choice(
            ["poweredOn", "poweredOff", "suspended", "warp9"]
        )


class FakeVmomiVMConfigHardwareDevice(object):
    def __init__(self):
        pass


class FakeVmomiNic(FakeVmomiVMConfigHardwareDevice):
    def __init__(self):
        super(FakeVmomiNic, self).__init__()
        self.macAddress = factory.make_mac_address()

    @property
    def key(self):
        return id(self)


class FakeVmomiVMConfigHardware(object):
    def __init__(self, nics=None):
        self.device = []

        if nics is None:
            nics = random.choice([1, 1, 1, 2, 2, 3])

        for i in range(0, nics):
            self.device.append(FakeVmomiNic())

        # add a few random non-NICs into the mix
        for i in range(0, random.choice([0, 1, 3, 5, 15])):
            self.device.append(FakeVmomiVMConfigHardwareDevice())

        random.shuffle(self.device)


class FakeVmomiVMConfig(object):
    def __init__(self, nics=None):
        self.hardware = FakeVmomiVMConfigHardware(nics=nics)


class FakeVmomiVM(object):
    def __init__(
        self, name=None, nics=None, has_instance_uuid=None, has_uuid=None
    ):

        if name is None:
            self._name = factory.make_hostname()
        else:
            self._name = name

        self.summary = FakeVmomiVMSummary(
            self._name, has_instance_uuid=has_instance_uuid, has_uuid=has_uuid
        )
        self.runtime = FakeVmomiVMRuntime()
        self.config = FakeVmomiVMConfig(nics=nics)

    def PowerOn(self):
        self.runtime.powerState = "poweredOn"

    def PowerOff(self):
        self.runtime.powerState = "poweredOff"

    def ReconfigVM_Task(self, vmconf):
        pass


class FakeVmomiVmFolder(object):
    def __init__(self, servers=0, has_instance_uuid=None, has_uuid=None):
        self.childEntity = []
        for i in range(0, servers):
            vm = FakeVmomiVM(
                has_instance_uuid=has_instance_uuid, has_uuid=has_uuid
            )
            self.childEntity.append(vm)


class FakeVmomiDatacenter(object):
    def __init__(self, servers=0, has_instance_uuid=None, has_uuid=None):
        self.vmFolder = FakeVmomiVmFolder(
            servers=servers,
            has_instance_uuid=has_instance_uuid,
            has_uuid=has_uuid,
        )


class FakeVmomiRootFolder(object):
    def __init__(self, servers=0, has_instance_uuid=None, has_uuid=None):
        self.childEntity = [
            FakeVmomiDatacenter(
                servers=servers,
                has_instance_uuid=has_instance_uuid,
                has_uuid=has_uuid,
            )
        ]


class FakeVmomiSearchIndex(object):
    def __init__(self, content):
        self.vms_by_instance_uuid = {}
        self.vms_by_uuid = {}

        for child in content.rootFolder.childEntity:
            if hasattr(child, "vmFolder"):
                datacenter = child
                vm_folder = datacenter.vmFolder
                vm_list = vm_folder.childEntity
                for vm in vm_list:
                    if (
                        hasattr(vm.summary.config, "instanceUuid")
                        and vm.summary.config.instanceUuid is not None
                    ):
                        self.vms_by_instance_uuid[
                            vm.summary.config.instanceUuid
                        ] = vm
                    if (
                        hasattr(vm.summary.config, "uuid")
                        and vm.summary.config.uuid is not None
                    ):
                        self.vms_by_uuid[vm.summary.config.uuid] = vm

    def FindByUuid(
        self, datacenter, uuid, search_vms, search_by_instance_uuid
    ):
        assert datacenter is None
        assert uuid is not None
        assert search_vms is True
        if search_by_instance_uuid:
            if uuid not in self.vms_by_instance_uuid:
                return None
            return self.vms_by_instance_uuid[uuid]
        else:
            if uuid not in self.vms_by_uuid:
                return None
            return self.vms_by_uuid[uuid]


class FakeVmomiContent(object):
    def __init__(self, servers=0, has_instance_uuid=None, has_uuid=None):
        self.rootFolder = FakeVmomiRootFolder(
            servers=servers,
            has_instance_uuid=has_instance_uuid,
            has_uuid=has_uuid,
        )
        self.searchIndex = FakeVmomiSearchIndex(self)


class FakeVmomiServiceInstance(object):
    def __init__(self, servers=0, has_instance_uuid=None, has_uuid=None):
        self.content = FakeVmomiContent(
            servers=servers,
            has_instance_uuid=has_instance_uuid,
            has_uuid=has_uuid,
        )

    def RetrieveContent(self):
        return self.content


class TestVMwarePyvmomi(MAASTestCase):
    """Tests for VMware probe-and-enlist, and power query/control using
    the python3-pyvmomi API."""

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def configure_vmomi_api(
        self, servers=10, has_instance_uuid=None, has_uuid=None
    ):
        mock_vmomi_api = self.patch(vmware, "vmomi_api")
        mock_vmomi_api.SmartConnect.return_value = FakeVmomiServiceInstance(
            servers=servers,
            has_instance_uuid=has_instance_uuid,
            has_uuid=has_uuid,
        )
        is_datacenter = self.patch(vmware.VMwarePyvmomiAPI, "is_datacenter")
        is_datacenter.side_effect = lambda x: isinstance(
            x, FakeVmomiDatacenter
        )
        is_datacenter = self.patch(vmware.VMwarePyvmomiAPI, "is_vm")
        is_datacenter.side_effect = lambda x: isinstance(x, FakeVmomiVM)
        is_folder = self.patch(vmware.VMwarePyvmomiAPI, "is_folder")
        is_folder.side_effect = lambda x: isinstance(
            x, (FakeVmomiVmFolder, FakeVmomiRootFolder)
        )
        has_children = self.patch(vmware.VMwarePyvmomiAPI, "has_children")
        has_children.side_effect = lambda x: isinstance(
            x, (FakeVmomiVmFolder, FakeVmomiDatacenter)
        )
        return mock_vmomi_api

    def setUp(self):
        super(TestVMwarePyvmomi, self).setUp()
        if vmware.try_pyvmomi_import() is False:
            self.skipTest("cannot test VMware without python3-pyvmomi")

    def test_api_connection(self):
        mock_vmomi_api = self.configure_vmomi_api(servers=0)
        api = VMwarePyvmomiAPI(
            factory.make_hostname(),
            factory.make_username(),
            factory.make_username(),
        )
        api.connect()
        self.expectThat(
            api.service_instance, IsInstance(FakeVmomiServiceInstance)
        )
        self.expectThat(api.is_connected(), Equals(True))
        api.disconnect()
        self.expectThat(mock_vmomi_api.SmartConnect.called, Equals(True))
        self.expectThat(mock_vmomi_api.Disconnect.called, Equals(True))

    def test_api_failed_connection(self):
        mock_vmomi_api = self.patch(vmware, "vmomi_api")
        mock_vmomi_api.SmartConnect.return_value = None
        api = VMwarePyvmomiAPI(
            factory.make_hostname(),
            factory.make_username(),
            factory.make_username(),
        )
        with ExpectedException(vmware.VMwareAPIConnectionFailed):
            api.connect()
        self.expectThat(api.service_instance, Is(None))
        self.expectThat(api.is_connected(), Equals(False))
        api.disconnect()
        self.expectThat(mock_vmomi_api.SmartConnect.called, Equals(True))
        self.expectThat(mock_vmomi_api.Disconnect.called, Equals(True))

    def test_get_vmware_servers_empty(self):
        self.configure_vmomi_api(servers=0)
        servers = vmware.get_vmware_servers(
            factory.make_hostname(),
            factory.make_username(),
            factory.make_username(),
            port=8443,
            protocol="https",
        )
        self.expectThat(servers, Equals({}))

    def test_get_vmware_servers(self):
        self.configure_vmomi_api(servers=10)
        servers = vmware.get_vmware_servers(
            factory.make_hostname(),
            factory.make_username(),
            factory.make_username(),
        )
        self.expectThat(servers, Not(Equals({})))

    def test_get_server_by_instance_uuid(self):
        mock_vmomi_api = self.configure_vmomi_api(
            servers=1, has_instance_uuid=True, has_uuid=False
        )
        search_index = (
            mock_vmomi_api.SmartConnect.return_value.content.searchIndex
        )
        instance_uuids = search_index.vms_by_instance_uuid.keys()
        for uuid in instance_uuids:
            vm = vmware._find_vm_by_uuid_or_name(mock_vmomi_api, uuid, None)
            self.assertIsNotNone(vm)

    def test_get_server_by_uuid(self):
        mock_vmomi_api = self.configure_vmomi_api(
            servers=1, has_instance_uuid=True, has_uuid=False
        )
        search_index = (
            mock_vmomi_api.SmartConnect.return_value.content.searchIndex
        )
        uuids = search_index.vms_by_uuid.keys()
        for uuid in uuids:
            vm = vmware._find_vm_by_uuid_or_name(mock_vmomi_api, uuid, None)
            self.assertIsNotNone(vm)

    def test_get_server_by_name(self):
        mock_vmomi_api = self.configure_vmomi_api(
            servers=1, has_instance_uuid=False, has_uuid=True
        )
        host = factory.make_hostname()
        username = factory.make_username()
        password = factory.make_username()
        servers = vmware.get_vmware_servers(host, username, password)
        for vm_name in servers.keys():
            vm = vmware._find_vm_by_uuid_or_name(mock_vmomi_api, None, vm_name)
            self.assertIsNotNone(vm)

    def test_get_missing_server_raises_VMwareVMNotFound(self):
        mock_vmomi_api = self.configure_vmomi_api(
            servers=1, has_instance_uuid=True, has_uuid=True
        )
        with ExpectedException(VMwareVMNotFound):
            vmware._find_vm_by_uuid_or_name(mock_vmomi_api, None, None)

    def test_power_control_missing_server_raises_VMwareVMNotFound(self):
        self.configure_vmomi_api(
            servers=1, has_instance_uuid=True, has_uuid=True
        )
        host = factory.make_hostname()
        username = factory.make_username()
        password = factory.make_username()
        with ExpectedException(VMwareVMNotFound):
            vmware.power_control_vmware(
                host, username, password, None, None, "on"
            )

    def test_power_query_missing_server_raises_VMwareVMNotFound(self):
        self.configure_vmomi_api(
            servers=1, has_instance_uuid=True, has_uuid=True
        )
        host = factory.make_hostname()
        username = factory.make_username()
        password = factory.make_username()
        with ExpectedException(VMwareVMNotFound):
            vmware.power_query_vmware(host, username, password, None, None)

    def test_power_control(self):
        mock_vmomi_api = self.configure_vmomi_api(servers=100)

        host = factory.make_hostname()
        username = factory.make_username()
        password = factory.make_username()

        servers = vmware.get_vmware_servers(host, username, password)

        # here we're grabbing indexes only available in the private mock object
        search_index = (
            mock_vmomi_api.SmartConnect.return_value.content.searchIndex
        )

        bios_uuids = list(search_index.vms_by_uuid)
        instance_uuids = list(search_index.vms_by_instance_uuid)

        # at least one should have a randomly-invalid state (just checking
        # for coverage, but since it's random, don't want to assert)
        vm_name = None

        for uuid in bios_uuids:
            vmware.power_query_vmware(host, username, password, vm_name, uuid)
        for uuid in instance_uuids:
            vmware.power_query_vmware(host, username, password, vm_name, uuid)
        for vm_name in servers:
            vmware.power_query_vmware(host, username, password, vm_name, None)

        # turn on a set of VMs, then verify they are on
        for uuid in bios_uuids:
            vmware.power_control_vmware(
                host, username, password, vm_name, uuid, "on"
            )

        for uuid in bios_uuids:
            state = vmware.power_query_vmware(
                host, username, password, vm_name, uuid
            )
            self.expectThat(state, Equals("on"))

        # turn off a set of VMs, then verify they are off
        for uuid in instance_uuids:
            vmware.power_control_vmware(
                host, username, password, vm_name, uuid, "off"
            )
        for uuid in instance_uuids:
            state = vmware.power_query_vmware(
                host, username, password, vm_name, uuid
            )
            self.expectThat(state, Equals("off"))

        self.expectThat(servers, Not(Equals({})))

    @inlineCallbacks
    def test_probe_and_enlist(self):
        num_servers = 100
        self.configure_vmomi_api(servers=num_servers)
        mock_create_node = self.patch(vmware, "create_node")
        system_id = factory.make_name("system_id")
        mock_create_node.side_effect = asynchronous(
            lambda *args, **kwargs: system_id
        )
        mock_commission_node = self.patch(vmware, "commission_node")

        host = factory.make_hostname()
        username = factory.make_username()
        password = factory.make_username()

        yield deferToThread(
            vmware.probe_vmware_and_enlist,
            factory.make_username(),
            host,
            username,
            password,
            accept_all=True,
        )

        self.assertEqual(mock_create_node.call_count, num_servers)
        self.assertEqual(mock_commission_node.call_count, num_servers)

    @inlineCallbacks
    def test_probe_and_enlist_reconfigures_boot_order_if_create_node_ok(self):
        num_servers = 1
        self.configure_vmomi_api(servers=num_servers)
        mock_create_node = self.patch(vmware, "create_node")
        system_id = factory.make_name("system_id")
        mock_create_node.side_effect = asynchronous(
            lambda *args, **kwargs: system_id
        )
        mock_reconfigure_vm = self.patch(FakeVmomiVM, "ReconfigVM_Task")

        # We need to not actually try to commission any nodes...
        self.patch(vmware, "commission_node")

        host = factory.make_hostname()
        username = factory.make_username()
        password = factory.make_username()

        yield deferToThread(
            vmware.probe_vmware_and_enlist,
            factory.make_username(),
            host,
            username,
            password,
            accept_all=True,
        )

        self.assertEqual(mock_reconfigure_vm.call_count, num_servers)

    @inlineCallbacks
    def test_probe_and_enlist_skips_pxe_config_if_create_node_failed(self):
        num_servers = 1
        self.configure_vmomi_api(servers=num_servers)
        mock_create_node = self.patch(vmware, "create_node")
        mock_create_node.side_effect = asynchronous(
            lambda *args, **kwargs: None
        )
        mock_reconfigure_vm = self.patch(FakeVmomiVM, "ReconfigVM_Task")

        # We need to not actually try to commission any nodes...
        self.patch(vmware, "commission_node")

        host = factory.make_hostname()
        username = factory.make_username()
        password = factory.make_username()

        yield deferToThread(
            vmware.probe_vmware_and_enlist,
            factory.make_username(),
            host,
            username,
            password,
            accept_all=True,
        )

        self.assertEqual(mock_reconfigure_vm.call_count, 0)
