# Copyright 2012-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Builtin script hooks, run upon receipt of ScriptResult"""

__all__ = [
    'NODE_INFO_SCRIPTS',
    'update_node_network_information',
    ]

import fnmatch
import json
import logging
import re

from lxml import etree
from maasserver.enum import NODE_METADATA
from maasserver.models import (
    Fabric,
    NUMANode,
    Subnet,
)
from maasserver.models.blockdevice import MIN_BLOCK_DEVICE_SIZE
from maasserver.models.interface import (
    Interface,
    PhysicalInterface,
)
from maasserver.models.nodemetadata import NodeMetadata
from maasserver.models.physicalblockdevice import PhysicalBlockDevice
from maasserver.models.switch import Switch
from maasserver.models.tag import Tag
from maasserver.utils.orm import get_one
from metadataserver.enum import SCRIPT_STATUS
from provisioningserver.refresh.node_info_scripts import (
    GET_FRUID_DATA_OUTPUT_NAME,
    IPADDR_OUTPUT_NAME,
    KERNEL_CMDLINE_OUTPUT_NAME,
    LIST_MODALIASES_OUTPUT_NAME,
    LSHW_OUTPUT_NAME,
    LXD_OUTPUT_NAME,
    NODE_INFO_SCRIPTS,
    VIRTUALITY_OUTPUT_NAME,
)
from provisioningserver.utils.ipaddr import parse_ip_addr


logger = logging.getLogger(__name__)


SWITCH_TAG_NAME = "switch"
SWITCH_HARDWARE = [
    # Seen on Facebook Wedge 40 switch:
    #     pci:v000014E4d0000B850sv000014E4sd0000B850bc02sc00i00
    #     (Broadcom Trident II ASIC)
    {
        'modaliases': [
            'pci:v000014E4d0000B850sv*sd*bc*sc*i*',
        ],
        'tag': 'bcm-trident2-asic',
        'comment':
            'Broadcom High-Capacity StrataXGS "Trident II" '
            'Ethernet Switch ASIC'
    },
    # Seen on Facebook Wedge 100 switch:
    #     pci:v000014E4d0000B960sv000014E4sd0000B960bc02sc00i00
    #     (Broadcom Tomahawk ASIC)
    {
        'modaliases': [
            'pci:v000014E4d0000B960sv*sd*bc*sc*i*',
        ],
        'tag': 'bcm-tomahawk-asic',
        'comment':
            'Broadcom High-Density 25/100 StrataXGS "Tomahawk" '
            'Ethernet Switch ASIC'
    },
]
SWITCH_OPENBMC_MAC = "02:00:00:00:00:02"


def _create_default_physical_interface(
        node, ifname, mac, link_connected, interface_speed,
        link_speed, numa_node, **kwargs):
    """Assigns the specified interface to the specified Node.

    Creates or updates a PhysicalInterface that corresponds to the given MAC.

    :param node: Node model object
    :param ifname: the interface name (for example, 'eth0')
    :param mac: the Interface to update and associate
    """
    # We don't yet have enough information to put this newly-created Interface
    # into the proper Fabric/VLAN. (We'll do this on a "best effort" basis
    # later, if we are able to determine that the interface is on a particular
    # subnet due to a DHCP reply during commissioning.)
    fabric = Fabric.objects.get_default_fabric()
    vlan = fabric.get_default_vlan()
    interface = PhysicalInterface.objects.create(
        mac_address=mac, name=ifname, node=node,
        numa_node=numa_node, vlan=vlan, **kwargs)

    return interface


def _parse_interface_speed(port):
    supported_modes = port.get('supported_modes')
    if supported_modes is not None:
        # Iterate over supported modes and choose the highest
        # supported speed.
        speeds = []
        for supported_mode in supported_modes:
            speeds.append(int(supported_mode.split('base')[0]))
        return max(speeds)


def _parse_interfaces(node, data):
    """Return a dict of interfaces keyed by MAC address."""
    interfaces = {}

    # Retrieve informaton from IPADDR_SCRIPT
    script_set = node.current_commissioning_script_set
    script_result = script_set.find_script_result(
        script_name=IPADDR_OUTPUT_NAME)
    if not script_result or script_result.status != SCRIPT_STATUS.PASSED:
        logger.error(
            '%s: Unable to discover NIC IP addresses due to missing '
            'passed output from %s' % (node.hostname, IPADDR_OUTPUT_NAME))
    assert isinstance(script_result.output, bytes)

    ip_addr_info = parse_ip_addr(script_result.output)
    network_cards = data.get('network', {}).get('cards', {})
    for card in network_cards:
        for port in card.get('ports', {}):
            mac = port.get('address')
            if mac in (None, SWITCH_OPENBMC_MAC):
                # Ignore loopback (with no MAC) and OpenBMC interfaces on
                # switches which all share the same, hard-coded OpenBMC MAC
                # address.
                continue

            interface = {
                'name': port.get('id'),
                'link_connected': port.get('link_detected'),
                'interface_speed': _parse_interface_speed(port),
                'link_speed': port.get('link_speed'),
                'numa_node': card.get('numa_node', 0),
                'vendor': card.get('vendor'),
                'product': card.get('product'),
                'firmware_version': card.get('firmware_version'),
                'sriov_max_vf': card.get('sriov', {}).get('maximum_vfs', 0),
            }
            # Assign the IP addresses to this interface
            link = ip_addr_info[interface['name']]
            interface['ips'] = link.get('inet', []) + link.get('inet6', [])

            interfaces[mac] = interface

    return interfaces


def parse_interfaces_details(node):
    """Get details for node interfaces from commissioning script results."""
    interfaces = {}

    script_set = node.current_commissioning_script_set
    if not script_set:
        return interfaces
    script_result = script_set.find_script_result(script_name=LXD_OUTPUT_NAME)
    if not script_result or script_result.status != SCRIPT_STATUS.PASSED:
        logger.error(
            f'{node.hostname}: Unable to discover NIC information due to '
            f'missing output from {LXD_OUTPUT_NAME}')
        return interfaces

    details = json.loads(script_result.stdout)
    return _parse_interfaces(node, details)


def update_interface_details(interface, details):
    """Update details for an existing interface from commissioning data.

    This should be passed details from the _parse_interfaces call.

    """
    iface_details = details.get(interface.mac_address)
    if not iface_details:
        return

    update_fields = []
    for field in ('name', 'vendor', 'product', 'firmware_version'):
        value = iface_details.get(field, '')
        if getattr(interface, field) != value:
            setattr(interface, field, value)
        update_fields.append(field)

    sriov_max_vf = iface_details.get('sriov_max_vf')
    if interface.sriov_max_vf != sriov_max_vf:
        interface.sriov_max_vf = sriov_max_vf
        update_fields.append('sriov_max_vf')
    if update_fields:
        interface.save(
            update_fields=['updated', *update_fields])


BOOTIF_RE = re.compile(r'BOOTIF=\d\d-([0-9a-f]{2}(?:-[0-9a-f]{2}){5})')


def parse_bootif_cmdline(cmdline):
    match = BOOTIF_RE.search(cmdline)
    if match:
        return match.group(1).replace('-', ':').lower()
    return None


def update_boot_interface(node, output, exit_status):
    """Update the boot interface from the kernel command line.

    If a BOOTIF parameter is present, that's the interface the machine
    booted off.
    """
    if exit_status != 0:
        logger.error(
            "%s: kernel-cmdline failed with status: "
            "%s." % (node.hostname, exit_status))
        return

    cmdline = output.decode('utf-8')
    boot_mac = parse_bootif_cmdline(cmdline)
    if boot_mac is None:
        # This is ok. For example, if a rack controller runs the
        # commissioning scripts, it won't have the BOOTIF parameter
        # there.
        return None

    try:
        node.boot_interface = node.interface_set.get(
            mac_address=boot_mac)
    except Interface.DoesNotExist:
        logger.error(
            f"'BOOTIF interface {boot_mac} doesn't exist for "
            f"{node.fqdn}")
    else:
        node.save(update_fields=['boot_interface'])


def update_node_network_information(node, data, numa_nodes):
    # Skip network configuration if set by the user.
    if node.skip_networking:
        # Turn off skip_networking now that the hook has been called.
        node.skip_networking = False
        node.save(update_fields=['skip_networking'])
        return

    interfaces_info = _parse_interfaces(node, data)
    current_interfaces = set()

    for mac, iface in interfaces_info.items():
        ifname = iface.get('name')
        link_connected = iface.get('link_connected')
        interface_speed = iface.get('interface_speed')
        link_speed = iface.get('link_speed')
        numa_index = iface.get('numa_node')
        vendor = iface.get('vendor')
        product = iface.get('product')
        firmware_version = iface.get('firmware_version')
        sriov_max_vf = iface.get('sriov_max_vf')
        try:
            interface = PhysicalInterface.objects.get(mac_address=mac)
            ifname = iface['name']
            if interface.node is not None and interface.node != node:
                logger.warning(
                    "Interface with MAC %s moved from node %s to %s. "
                    "(The existing interface will be deleted.)" %
                    (interface.mac_address, interface.node.fqdn,
                     node.fqdn))
                interface.delete()
                interface = _create_default_physical_interface(
                    node, ifname, mac, link_connected, interface_speed,
                    link_speed, numa_nodes[numa_index], vendor=vendor,
                    product=product, firmware_version=firmware_version,
                    sriov_max_vf=sriov_max_vf)
            else:
                # Interface already exists on this Node, so just update
                # the NIC info.
                update_interface_details(interface, interfaces_info)
        except PhysicalInterface.DoesNotExist:
            interface = _create_default_physical_interface(
                node, ifname, mac, link_connected, interface_speed,
                link_speed, numa_nodes[numa_index], vendor=vendor,
                product=product, firmware_version=firmware_version,
                sriov_max_vf=sriov_max_vf)

        current_interfaces.add(interface)
        interface.update_ip_addresses(iface.get('ips'))
        if sriov_max_vf > 0:
            interface.add_tag('sriov')
            interface.save(update_fields=['tags'])

        if not link_connected:
            # This interface is now disconnected.
            if interface.vlan is not None:
                interface.vlan = None
                interface.save(update_fields=['vlan', 'updated'])

    # If a machine boots by UUID before commissioning(s390x) no boot_interface
    # will be set as interfaces existed during boot. Set it using the
    # boot_cluster_ip now that the interfaces have been created.
    if node.boot_interface is None and node.boot_cluster_ip is not None:
        subnet = Subnet.objects.get_best_subnet_for_ip(node.boot_cluster_ip)
        if subnet:
            node.boot_interface = node.interface_set.filter(
                id__in=[interface.id for interface in current_interfaces],
                vlan=subnet.vlan).first()
            node.save(update_fields=['boot_interface'])

    # Only configured Interfaces are tested so configuration must be done
    # before regeneration.
    node.set_initial_networking_configuration()

    # XXX ltrager 11-16-2017 - Don't regenerate ScriptResults on controllers.
    # Currently this is not needed saving us 1 database query. However, if
    # commissioning is ever enabled for controllers regeneration will need
    # to be allowed on controllers otherwise network testing may break.
    if node.current_testing_script_set is not None and not node.is_controller:
        # LP: #1731353 - Regenerate ScriptResults before deleting Interfaces.
        # This creates a ScriptResult with proper parameters for each interface
        # on the system. Interfaces no long available will be deleted which
        # causes a casade delete on their assoicated ScriptResults.
        node.current_testing_script_set.regenerate(storage=False, network=True)

    Interface.objects.filter(node=node).exclude(
        id__in=[iface.id for iface in current_interfaces]).delete()


def get_xml_field_value(evaluator, expression):
    """Return an XML field or None if its not found."""
    field = evaluator(expression)
    # Supermicro uses 0123456789 as a place holder.
    if (isinstance(field, list) and len(field) > 0 and
            '0123456789' not in field[0].lower()):
        return field[0]
    else:
        return None


def update_hardware_details(node, output, exit_status):
    """Process the results of `LSHW_SCRIPT`.

    Updates `node.storage` fields, and also evaluates all tag
    expressions against the given ``lshw`` XML.

    If `exit_status` is non-zero, this function returns without doing
    anything.
    """
    if exit_status != 0:
        logger.error(
            "%s: lshw script failed with status: %s." % (
                node.hostname, exit_status))
        return
    assert isinstance(output, bytes)
    try:
        doc = etree.XML(output)
    except etree.XMLSyntaxError:
        logger.exception("Invalid lshw data.")
    else:
        # Same document, many queries: use XPathEvaluator.
        evaluator = etree.XPathEvaluator(doc)

        # Only one hardware UUID should be provided but lxml always returns a
        # list.
        for e in evaluator('//node/configuration/setting[@id="uuid"]'):
            value = e.get('value')
            if value:
                node.hardware_uuid = value

        node.save(update_fields=['hardware_uuid'])

        # This gathers the system vendor, product, version, and serial. Custom
        # built machines and some Supermicro servers do not provide this
        # information.
        for key in ["vendor", "product", "version", "serial"]:
            value = get_xml_field_value(
                evaluator, "//node[@class='system']/%s/text()" % key)
            if value:
                NodeMetadata.objects.update_or_create(
                    node=node, key="system_%s" % key,
                    defaults={"value": value})

        # Gather the mainboard information, all systems should have this.
        for key in ["vendor", "product"]:
            value = get_xml_field_value(
                evaluator, "//node[@id='core']/%s/text()" % key)
            if value:
                NodeMetadata.objects.update_or_create(
                    node=node, key="mainboard_%s" % key,
                    defaults={"value": value})

        for key in ["version", "date"]:
            value = get_xml_field_value(
                evaluator,
                "//node[@id='core']/node[@id='firmware']/%s/text()" % key)
            if value:
                NodeMetadata.objects.update_or_create(
                    node=node, key="mainboard_firmware_%s" % key,
                    defaults={'value': value})


def process_lxd_results(node, output, exit_status):
    """Process the results of `LXD_SCRIPT`.

    If `exit_status` is non-zero, this function returns without doing
    anything.
    """
    if exit_status != 0:
        logger.error(
            "%s: lxd script failed with status: "
            "%s." % (node.hostname, exit_status))
        return
    assert isinstance(output, bytes)
    try:
        data = json.loads(output.decode('utf-8'))
    except ValueError as e:
        raise ValueError(e.message + ': ' + output)

    # Update CPU details.
    node.cpu_count, node.cpu_speed, cpu_model, numa_nodes = (
        _parse_cpuinfo(data))
    # Update memory.
    node.memory, numa_nodes = _parse_memory(data, numa_nodes)

    # Create or update NUMA nodes.
    numa_nodes = [
        NUMANode.objects.update_or_create(
            node=node, index=numa_index, defaults={
                'memory': numa_data['memory'],
                'cores': numa_data['cores']}
        )[0] for numa_index, numa_data in numa_nodes.items()]

    # Network interfaces.
    update_node_network_information(node, data, numa_nodes)
    # Storage.
    update_node_physical_block_devices(node, data, numa_nodes)

    if cpu_model:
        NodeMetadata.objects.update_or_create(
            node=node, key='cpu_model', defaults={'value': cpu_model})

    node.save(update_fields=['cpu_count', 'cpu_speed', 'memory'])


def _parse_cpuinfo(data):
    """Retrieve cpu_count, cpu_speed, and cpu_model."""
    cpu_speed = 0
    cpu_model = None
    cpu_count = data.get('cpu', {}).get('total', 0)
    # Only update the cpu_model if all the socket names match.
    sockets = data.get('cpu', {}).get('sockets', [])
    names = []
    numa_nodes = {}
    for socket in sockets:
        name = socket.get('name')
        if name is not None:
            names.append(name)
        for core in socket.get('cores', []):
            numa_node = core.get('numa_node')
            if numa_node not in numa_nodes:
                numa_nodes[numa_node] = {
                    "cores": [core.get('core')]
                    }
            else:
                numa_nodes[numa_node]['cores'].append(
                    core.get('core'))
    if len(names) > 0 and all(name == names[0] for name in names):
        cpu = names[0]
        m = re.search(
            r'(?P<model_name>.+)', cpu, re.MULTILINE)
        if m is not None:
            cpu_model = m.group('model_name')
            if '@' in cpu_model:
                cpu_model = cpu_model.split(' @')[0]

        # Some CPU vendors include the speed in the model. If so use
        # that for the CPU speed as the other speeds are effected by
        # CPU scaling.
        m = re.search(
            r'(\s@\s(?P<ghz>\d+\.\d+)GHz)$', cpu, re.MULTILINE)
        if m is not None:
            cpu_speed = int(float(m.group('ghz')) * 1000)
    # When socket names don't match or cpu_speed couldn't be retrieved,
    # use the max frequency among all the sockets if before
    # resulting to average current frequency of all the sockets.
    if not cpu_speed:
        max_frequency = 0
        for socket in sockets:
            frequency_turbo = socket.get('frequency_turbo', 0)
            if frequency_turbo > max_frequency:
                max_frequency = frequency_turbo
        if max_frequency:
            cpu_speed = max_frequency
        else:
            current_average = 0
            for socket in sockets:
                current_average += socket.get('frequency', 0)
            current_average /= len(sockets)
            if current_average:
                # Fall back on the current speed, round it to
                # the nearest hundredth as the number may be
                # effected by CPU scaling.
                cpu_speed = round(current_average / 100) * 100

    return cpu_count, cpu_speed, cpu_model, numa_nodes


def _parse_memory(data, numa_nodes):

    total_memory = data.get('memory', {}).get('total', 0) / 1024 / 1024
    for memory_node in data.get('memory', {}).get('nodes', []):
        numa_nodes[memory_node['numa_node']]['memory'] = (
            memory_node['total'] / 1024 / 1024)

    return total_memory, numa_nodes


def set_virtual_tag(node, output, exit_status):
    """Process the results of `VIRTUALITY_SCRIPT`.

    This adds or removes the *virtual* tag from the node, depending on
    whether a virtualization type is listed.

    If `exit_status` is non-zero, this function returns without doing
    anything.
    """
    if exit_status != 0:
        logger.error(
            "%s: virtual machine detection script failed with status: %s." % (
                node.hostname, exit_status))
        return
    assert isinstance(output, bytes)
    decoded_output = output.decode('ascii').strip()
    tag, _ = Tag.objects.get_or_create(name='virtual')
    if 'none' in decoded_output:
        node.tags.remove(tag)
    elif decoded_output == '':
        logger.warning(
            "No virtual type reported in VIRTUALITY_SCRIPT output for node "
            "%s", node.system_id)
    else:
        node.tags.add(tag)


_xpath_routers = "/lldp//id[@type='mac']/text()"


def extract_router_mac_addresses(raw_content):
    """Extract the routers' MAC Addresses from raw LLDP information."""
    if not raw_content:
        return None
    assert isinstance(raw_content, bytes)
    parser = etree.XMLParser()
    doc = etree.XML(raw_content.strip(), parser)
    return doc.xpath(_xpath_routers)


def get_tags_from_block_info(block_info):
    """Return array of tags that will populate the `PhysicalBlockDevice`.

    Tags block devices for:
        rotary: Storage device with a spinning disk.
        ssd: Storage device with flash storage.
        removable: Storage device that can be easily removed like a USB
            flash drive.
        sata: Storage device that is connected over SATA.
    """
    tags = []
    if block_info['rpm'] > 0:
        tags.append('rotary')
        tags.append("%srpm" % block_info['rpm'])
    else:
        tags.append('ssd')
    if block_info['removable']:
        tags.append('removable')
    if block_info['type'] == 'sata':
        tags.append('sata')
    return tags


def get_matching_block_device(block_devices, serial=None, id_path=None):
    """Return the matching block device based on `serial` or `id_path` from
    the provided list of `block_devices`."""
    if serial:
        for block_device in block_devices:
            if block_device.serial == serial:
                return block_device
    elif id_path:
        for block_device in block_devices:
            if block_device.id_path == id_path:
                return block_device
    return None


def update_node_physical_block_devices(node, data, numa_nodes):
    # Skip storage configuration if set by the user.
    if node.skip_storage:
        # Turn off skip_storage now that the hook has been called.
        node.skip_storage = False
        node.save(update_fields=['skip_storage'])
        return

    blockdevs = data.get('storage', {}).get('disks', [])
    previous_block_devices = list(
        PhysicalBlockDevice.objects.filter(node=node).all())
    for block_info in blockdevs:
        # Skip the read-only devices. We keep them in the output for
        # the user to view but they do not get an entry in the database.
        if block_info['read_only']:
            continue
        name = block_info['id']
        model = block_info.get('model', '')
        serial = block_info.get('serial', '')
        id_path = block_info.get('device_path', '')
        if not id_path or not serial:
            # Fallback to the dev path if device_path missing or there is
            # no serial number. (No serial number is a strong indicator that
            # this is a virtual disk, so it's unlikely that the device_path
            # would work.)
            id_path = '/dev/' + block_info.get('id')
        size = block_info.get('size', 0)
        block_size = block_info.get('block_size', 0)
        firmware_version = block_info.get('firmware_version')
        numa_index = block_info.get('numa_node')
        tags = get_tags_from_block_info(block_info)

        # First check if there is an existing device with the same name.
        # If so, we need to rename it. Its name will be changed back later,
        # when we loop around to it.
        existing = PhysicalBlockDevice.objects.filter(
            node=node, name=name).all()
        for device in existing:
            # Use the device ID to ensure a unique temporary name.
            device.name = "%s.%d" % (device.name, device.id)
            device.save(update_fields=['name'])

        block_device = get_matching_block_device(
            previous_block_devices, serial, id_path)
        if block_device is not None:
            # Refresh, since it might have been temporarily renamed
            # above.
            block_device.refresh_from_db()
            # Already exists for the node. Keep the original object so the
            # ID doesn't change and if its set to the boot_disk that FK will
            # not need to be updated.
            previous_block_devices.remove(block_device)
            block_device.name = name
            block_device.model = model
            block_device.serial = serial
            block_device.id_path = id_path
            block_device.size = size
            block_device.block_size = block_size
            block_device.firmware_version = firmware_version
            block_device.tags = tags
            block_device.save()
        else:
            # MAAS doesn't allow disks smaller than 4MiB so skip them
            if size <= MIN_BLOCK_DEVICE_SIZE:
                continue
            # Skip loopback devices as they won't be available on next boot
            if id_path.startswith('/dev/loop'):
                continue

            # New block device. Create it on the node.
            PhysicalBlockDevice.objects.create(
                numa_node=numa_nodes[numa_index],
                name=name,
                id_path=id_path,
                size=size,
                block_size=block_size,
                tags=tags,
                model=model,
                serial=serial,
                firmware_version=firmware_version,
                )

    # Clear boot_disk if it is being removed.
    boot_disk = node.boot_disk
    if boot_disk is not None and boot_disk in previous_block_devices:
        boot_disk = None
    if node.boot_disk != boot_disk:
        node.boot_disk = boot_disk
        node.save(update_fields=['boot_disk'])

    # XXX ltrager 11-16-2017 - Don't regenerate ScriptResults on controllers.
    # Currently this is not needed saving us 1 database query. However, if
    # commissioning is ever enabled for controllers regeneration will need
    # to be allowed on controllers otherwise storage testing may break.
    if node.current_testing_script_set is not None and not node.is_controller:
        # LP: #1731353 - Regenerate ScriptResults before deleting
        # PhyscalBlockDevices. This creates a ScriptResult with proper
        # parameters for each storage device on the system. Storage devices no
        # long available will be deleted which causes a casade delete on their
        # assoicated ScriptResults.
        node.current_testing_script_set.regenerate(storage=True, network=False)

    # Delete all the previous block devices that are no longer present
    # on the commissioned node.
    delete_block_device_ids = [
        bd.id
        for bd in previous_block_devices
    ]
    if len(delete_block_device_ids) > 0:
        PhysicalBlockDevice.objects.filter(
            id__in=delete_block_device_ids).delete()

    # Layout needs to be set last so removed disks aren't included in the
    # applied layout.
    node.set_default_storage_layout()


def create_metadata_by_modalias(node, output: bytes, exit_status):
    """Tags the node based on discovered hardware, determined by modaliases.
    If nodes are detected as supported switches, they also get Switch objects.

    :param node: The node whose tags to set.
    :param output: Output from the LIST_MODALIASES_SCRIPT
        (one modalias per line).
    :param exit_status: The exit status of the commissioning script.
    """
    if exit_status != 0:
        logger.error("%s: modalias discovery script failed with status: %s" % (
            node.hostname, exit_status))
        return
    assert isinstance(output, bytes)
    modaliases = output.decode('utf-8').splitlines()
    switch_tags_added, _ = retag_node_for_hardware_by_modalias(
        node, modaliases, SWITCH_TAG_NAME, SWITCH_HARDWARE)
    if len(switch_tags_added) > 0:
        dmi_data = get_dmi_data(modaliases)
        vendor, model = detect_switch_vendor_model(dmi_data)
        add_switch_vendor_model_tags(node, vendor, model)
        add_switch(node, vendor, model)


def add_switch_vendor_model_tags(node, vendor, model):
    if vendor is not None:
        vendor_tag, _ = Tag.objects.get_or_create(name=vendor)
        node.tags.add(vendor_tag)
        logger.info(
            "%s: Added vendor tag '%s' for detected switch hardware." % (
                node.hostname, vendor))
    if model is not None:
        kernel_opts = None
        if model == "wedge40":
            kernel_opts = "console=tty0 console=ttyS1,57600n8"
        elif model == "wedge100":
            kernel_opts = "console=tty0 console=ttyS4,57600n8"
        model_tag, _ = Tag.objects.get_or_create(
            name=model, defaults={
                'kernel_opts': kernel_opts
            })
        node.tags.add(model_tag)
        logger.info(
            "%s: Added model tag '%s' for detected switch hardware." % (
                node.hostname, model))


def add_switch(node, vendor, model):
    """Add Switch object representing the switch hardware."""
    switch, created = Switch.objects.get_or_create(node=node)
    logger.info("%s: detected as a switch." % node.hostname)
    NodeMetadata.objects.update_or_create(
        node=node, key=NODE_METADATA.VENDOR_NAME, defaults={"value": vendor})
    NodeMetadata.objects.update_or_create(
        node=node, key=NODE_METADATA.PHYSICAL_MODEL_NAME,
        defaults={"value": model})
    return switch


def update_node_fruid_metadata(node, output: bytes, exit_status):
    try:
        data = json.loads(output.decode("utf-8"))
    except json.decoder.JSONDecodeError:
        return

    # Attempt to map metadata provided by Facebook Wedge 100 FRUID API
    # to SNMP OID-like metadata describing physical nodes (see
    # http://www.ietf.org/rfc/rfc2737.txt).
    key_name_map = {
        "Product Name": NODE_METADATA.PHYSICAL_MODEL_NAME,
        "Product Serial Number": NODE_METADATA.PHYSICAL_SERIAL_NUM,
        "Product Version": NODE_METADATA.PHYSICAL_HARDWARE_REV,
        "System Manufacturer": NODE_METADATA.PHYSICAL_MFG_NAME,
    }
    info = data.get("Information", {})
    for fruid_key, node_key in key_name_map.items():
        if fruid_key in info:
            NodeMetadata.objects.update_or_create(
                node=node, key=node_key, defaults={"value": info[fruid_key]})


def detect_switch_vendor_model(dmi_data):
    # This is based on:
    #    https://github.com/lool/sonic-snap/blob/master/common/id-switch
    vendor = None
    if "svnIntel" in dmi_data and "pnEPGSVR" in dmi_data:
        # XXX this seems like a suspicious assumption.
        vendor = "accton"
    elif "svnJoytech" in dmi_data and "pnWedge-AC-F20-001329" in dmi_data:
        vendor = "accton"
    elif "svnMellanoxTechnologiesLtd." in dmi_data:
        vendor = "mellanox"
    elif "svnTobefilledbyO.E.M." in dmi_data:
        if "rnPCOM-B632VG-ECC-FB-ACCTON-D" in dmi_data:
            vendor = "accton"
    # Now that we know the manufacturer, see if we can identify the model.
    model = None
    if vendor == "mellanox":
        if 'pn"MSN2100-CB2FO"' in dmi_data:
            model = "sn2100"
    elif vendor == "accton":
        if 'pnEPGSVR' in dmi_data:
            model = "wedge40"
        elif 'pnWedge-AC-F20-001329' in dmi_data:
            model = "wedge40"
        elif 'pnTobefilledbyO.E.M.' in dmi_data:
            if 'rnPCOM-B632VG-ECC-FB-ACCTON-D' in dmi_data:
                model = "wedge100"
    return vendor, model


def get_dmi_data(modaliases):
    """Given the list of modaliases, returns the set of DMI data.

    An empty set will be returned if no DMI data could be found.

    The DMI data will be stripped of whitespace and have a prefix indicating
    what value they represent. Prefixes can be found in
    drivers/firmware/dmi-id.c in the Linux source:

        { "bvn", DMI_BIOS_VENDOR },
        { "bvr", DMI_BIOS_VERSION },
        { "bd",  DMI_BIOS_DATE },
        { "svn", DMI_SYS_VENDOR },
        { "pn",  DMI_PRODUCT_NAME },
        { "pvr", DMI_PRODUCT_VERSION },
        { "rvn", DMI_BOARD_VENDOR },
        { "rn",  DMI_BOARD_NAME },
        { "rvr", DMI_BOARD_VERSION },
        { "cvn", DMI_CHASSIS_VENDOR },
        { "ct",  DMI_CHASSIS_TYPE },
        { "cvr", DMI_CHASSIS_VERSION },

    The following is an example of what the set might look like:

        {'bd09/18/2014',
         'bvnAmericanMegatrendsInc.',
         'bvrMF1_2A04',
         'ct0',
         'cvnIntel',
         'cvrTobefilledbyO.E.M.',
         'pnEPGSVR',
         'pvrTobefilledbyO.E.M.',
         'rnTobefilledbyO.E.M.',
         'rvnTobefilledbyO.E.M.',
         'rvrTobefilledbyO.E.M.',
         'svnIntel'}

    :return: set
    """
    for modalias in modaliases:
        if modalias.startswith("dmi:"):
            return frozenset(
                [data for data in modalias.split(':')[1:] if len(data) > 0])
    return frozenset()


def filter_modaliases(
        modaliases_discovered, modaliases=None, pci=None, usb=None):
    """Determines which candidate modaliases match what was discovered.

    :param modaliases_discovered: The list of modaliases found on the node.
    :param modaliases: The candidate modaliases to match against. This
        parameter must be iterable. Wildcards are accepted.
    :param pci: A list of strings in the format <vendor>:<device>. May include
        wildcards.
    :param usb: A list of strings in the format <vendor>:<product>. May include
        wildcards.
    :return: The list of modaliases on the node matching the candidate(s).
    """
    patterns = []
    if modaliases is not None:
        patterns.extend(modaliases)
    if pci is not None:
        for pattern in pci:
            try:
                vendor, device = pattern.split(':')
            except ValueError:
                # Ignore malformed patterns.
                continue
            vendor = vendor.upper()
            device = device.upper()
            # v: vendor
            # d: device
            # sv: subvendor
            # sd: subdevice
            # bc: bus class
            # sc: bus subclass
            # i: interface
            patterns.append(
                "pci:v0000{vendor}d0000{device}sv*sd*bc*sc*i*".format(
                    vendor=vendor, device=device))
    if usb is not None:
        for pattern in usb:
            try:
                vendor, product = pattern.split(':')
            except ValueError:
                # Ignore malformed patterns.
                continue
            vendor = vendor.upper()
            product = product.upper()
            # v: vendor
            # p: product
            # d: bcdDevice (device release number)
            # dc: device class
            # dsc: device subclass
            # dp: device protocol
            # ic: interface class
            # isc: interface subclass
            # ip: interface protocol
            patterns.append(
                "usb:v{vendor}p{product}d*dc*dsc*dp*ic*isc*ip*".format(
                    vendor=vendor, product=product))
    matches = []
    for pattern in patterns:
        new_matches = fnmatch.filter(modaliases_discovered, pattern)
        for match in new_matches:
            if match not in matches:
                matches.append(match)
    return matches


def determine_hardware_matches(modaliases, hardware_descriptors):
    """Determines which hardware descriptors match the given modaliases.

    :param modaliases: List of modaliases found on the node.
    :param hardware_descriptors: Dictionary of information about each hardware
        component that can be discovered. This method requires a 'modaliases'
        entry to be present (with a list of modalias globs that might match
        the hardware on the node).
    :returns: A tuple whose first element contains the list of discovered
        hardware descriptors (with an added 'matches' element to specify which
        modaliases matched), and whose second element the list of any hardware
        that has been ruled out (so that the caller may remove those tags).
    """
    discovered_hardware = []
    ruled_out_hardware = []
    for candidate in hardware_descriptors:
        matches = filter_modaliases(modaliases, candidate['modaliases'])
        if len(matches) > 0:
            candidate = candidate.copy()
            candidate['matches'] = matches
            discovered_hardware.append(candidate)
        else:
            ruled_out_hardware.append(candidate)
    return discovered_hardware, ruled_out_hardware


def retag_node_for_hardware_by_modalias(
        node, modaliases, parent_tag_name, hardware_descriptors):
    """Adds or removes tags on a node based on its modaliases.

    Returns the Tag model objects added and removed, respectively.

    :param node: The node whose tags to modify.
    :param modaliases: The modaliases discovered on the node.
    :param parent_tag_name: The tag name for the hardware type given in the
        `hardware_descriptors` list. For example, if switch ASICs are being
        discovered, the string "switch" might be appropriate. Then, if switch
        hardware is found, the node will be tagged with the matching
        descriptors' tag(s), *and* with the more general "switch" tag.
    :param hardware_descriptors: A list of hardware descriptor dictionaries.

    :returns: tuple of (tags_added, tags_removed)
    """
    # Don't unconditionally create the tag. Check for it with a filter first.
    parent_tag = get_one(Tag.objects.filter(name=parent_tag_name))
    tags_added = set()
    tags_removed = set()
    discovered_hardware, ruled_out_hardware = determine_hardware_matches(
        modaliases, hardware_descriptors)
    if len(discovered_hardware) > 0:
        if parent_tag is None:
            # Create the tag "just in time" if we found matching hardware, and
            # we hadn't created the tag yet.
            parent_tag = Tag(name=parent_tag_name)
            parent_tag.save()
        node.tags.add(parent_tag)
        tags_added.add(parent_tag)
        logger.info(
            "%s: Added tag '%s' for detected hardware type." % (
                node.hostname, parent_tag_name))
        for descriptor in discovered_hardware:
            tag = descriptor['tag']
            comment = descriptor['comment']
            matches = descriptor['matches']
            hw_tag, _ = Tag.objects.get_or_create(name=tag, defaults={
                'comment': comment
            })
            node.tags.add(hw_tag)
            tags_added.add(hw_tag)
            logger.info(
                "%s: Added tag '%s' for detected hardware: %s "
                "(Matched: %s)." % (node.hostname, tag, comment, matches))
    else:
        if parent_tag is not None:
            node.tags.remove(parent_tag)
            tags_removed.add(parent_tag)
            logger.info(
                "%s: Removed tag '%s'; machine does not match hardware "
                "description." % (node.hostname, parent_tag_name))
    for descriptor in ruled_out_hardware:
        tag_name = descriptor['tag']
        existing_tag = get_one(node.tags.filter(name=tag_name))
        if existing_tag is not None:
            node.tags.remove(existing_tag)
            tags_removed.add(existing_tag)
            logger.info(
                "%s: Removed tag '%s'; hardware is missing." % (
                    node.hostname, tag_name))
    return tags_added, tags_removed


# Register the post processing hooks.
NODE_INFO_SCRIPTS[LSHW_OUTPUT_NAME]['hook'] = update_hardware_details
NODE_INFO_SCRIPTS[VIRTUALITY_OUTPUT_NAME]['hook'] = set_virtual_tag
NODE_INFO_SCRIPTS[GET_FRUID_DATA_OUTPUT_NAME]['hook'] = (
    update_node_fruid_metadata)
NODE_INFO_SCRIPTS[LIST_MODALIASES_OUTPUT_NAME]['hook'] = (
    create_metadata_by_modalias)
NODE_INFO_SCRIPTS[LXD_OUTPUT_NAME]['hook'] = process_lxd_results
NODE_INFO_SCRIPTS[KERNEL_CMDLINE_OUTPUT_NAME]['hook'] = update_boot_interface
