# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""PowerNV Boot Method"""

__all__ = ["PowerNVBootMethod"]

import re

from provisioningserver.boot import (
    BootMethod,
    BytesReader,
    get_parameters,
    get_remote_mac,
)
from provisioningserver.boot.pxe import ARP_HTYPE, re_mac_address
from provisioningserver.kernel_opts import compose_kernel_command_line
from provisioningserver.utils import typed
from tftp.backend import FilesystemReader

# The pxelinux.cfg path is prefixed with the architecture for the
# PowerNV nodes. This prefix is set by the path-prefix dhcpd option.
# We assume that the ARP HTYPE (hardware type) that PXELINUX sends is
# always Ethernet.
re_config_file = r"""
    # Optional leading slash(es).
    ^/*
    ppc64el           # PowerNV pxe prefix, set by dhcpd
    /
    pxelinux[.]cfg    # PXELINUX expects this.
    /
    (?: # either a MAC
        {htype:02x}    # ARP HTYPE.
        -
        (?P<mac>{re_mac_address.pattern})    # Capture MAC.
    | # or "default"
        default
    )
    $
"""

re_config_file = re_config_file.format(
    htype=ARP_HTYPE.ETHERNET, re_mac_address=re_mac_address
)
re_config_file = re_config_file.encode("ascii")
re_config_file = re.compile(re_config_file, re.VERBOSE)

# Due to the "ppc64el" prefix all files requested from the client using
# relative paths will have that prefix. Capturing the path after that prefix
# will give us the correct path in the local tftp root on disk.
re_other_file = r"""
    # Optional leading slash(es).
    ^/*
    ppc64el           # PowerNV PXE prefix, set by dhcpd.
    /
    (?P<path>.+)      # Capture path.
    $
"""
re_other_file = re_other_file.encode("ascii")
re_other_file = re.compile(re_other_file, re.VERBOSE)


def format_bootif(mac):
    """Formats a mac address into the BOOTIF format, expected by
    the linux kernel."""
    mac = mac.replace(":", "-")
    mac = mac.lower()
    return "%02x-%s" % (ARP_HTYPE.ETHERNET, mac)


class PowerNVBootMethod(BootMethod):

    name = "powernv"
    bios_boot_method = "powernv"
    template_subdir = "pxe"
    bootloader_path = "pxelinux.0"
    arch_octet = "00:0E"
    user_class = None
    path_prefix = "ppc64el/"

    def get_params(self, backend, path):
        """Gets the matching parameters from the requested path."""
        match = re_config_file.match(path)
        if match is not None:
            return get_parameters(match)
        match = re_other_file.match(path)
        if match is not None:
            return get_parameters(match)
        return None

    def match_path(self, backend, path):
        """Checks path for the configuration file that needs to be
        generated.

        :param backend: requesting backend
        :param path: requested path
        :return: dict of match params from path, None if no match
        """
        params = self.get_params(backend, path)
        if params is None:
            return None
        params["arch"] = "ppc64el"
        if "mac" not in params:
            mac = get_remote_mac()
            if mac is not None:
                params["mac"] = mac
        return params

    def get_reader(self, backend, kernel_params, mac=None, path=None, **extra):
        """Render a configuration file as a unicode string.

        :param backend: requesting backend
        :param kernel_params: An instance of `KernelParameters`.
        :param path: Optional MAC address discovered by `match_path`.
        :param path: Optional path discovered by `match_path`.
        :param extra: Allow for other arguments. This is a safety valve;
            parameters generated in another component (for example, see
            `TFTPBackend.get_config_reader`) won't cause this to break.
        """
        if path is not None:
            # This is a request for a static file, not a configuration file.
            # The prefix was already trimmed by `match_path` so we need only
            # return a FilesystemReader for `path` beneath the backend's base.
            target_path = backend.base.descendant(path.split("/"))
            return FilesystemReader(target_path)

        # Return empty config for PowerNV local. PowerNV fails to
        # support the LOCALBOOT flag. Empty config will allow it
        # to select the first device.
        if kernel_params.purpose == "local":
            return BytesReader("".encode("utf-8"))

        template = self.get_template(
            kernel_params.purpose, kernel_params.arch, kernel_params.subarch
        )
        namespace = self.compose_template_namespace(kernel_params)

        # Modify the kernel_command to inject the BOOTIF. PowerNV fails to
        # support the IPAPPEND pxelinux flag.
        def kernel_command(params):
            cmd_line = compose_kernel_command_line(params)
            if mac is not None:
                return "%s BOOTIF=%s" % (cmd_line, format_bootif(mac))
            return cmd_line

        namespace["kernel_command"] = kernel_command
        return BytesReader(template.substitute(namespace).encode("utf-8"))

    @typed
    def link_bootloader(self, destination: str):
        """Does nothing. No extra boot files are required."""
        # PowerNV doesn't actually use the provided pxelinux.0. It emulates
        # pxelinux behaviour using its own bootloader.
