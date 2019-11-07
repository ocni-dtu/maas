# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Windows Operating System."""

__all__ = ["WindowsOS"]

import os
import re

from provisioningserver.config import ClusterConfiguration
from provisioningserver.drivers.osystem import (
    BOOT_IMAGE_PURPOSE,
    OperatingSystem,
)


WINDOWS_CHOICES = {
    "win2008r2": 'Windows "Server 2008 R2"',
    "win2008hvr2": 'Windows "Hyper-V Server 2008 R2"',
    "win2012": 'Windows "Server 2012"',
    "win2012r2": 'Windows "Server 2012 R2"',
    "win2012hv": 'Windows "Hyper-V Server 2012"',
    "win2012hvr2": 'Windows "Hyper-V Server 2012 R2"',
    "win2016": 'Windows "Server 2016"',
    "win2016hv": 'Windows "Hyper-V Server 2016"',
    "win2016nano": 'Windows "Nano Server 2016"',
    "win2016-core": 'Windows "Server 2016 Core"',
    "win2016dc": 'Windows "Server 2016 Datacenter"',
    "win2016dc-core": 'Windows "Server 2016 Datacenter Core"',
    "win2019": 'Windows "Server 2019"',
    "win2019-core": 'Windows "Server 2019 Core"',
    "win2019dc": 'Windows "Server 2019 Datacenter"',
    "win2019dc-core": 'Windows "Server 2019 Datecenter Core"',
    "win10ent": 'Windows "10 Enterprise"',
}

WINDOWS_DEFAULT = "win2019"

REQUIRE_LICENSE_KEY = [
    "win2008r2",
    "win2012",
    "win2012r2",
    "win2016",
    "win2016nano",
    "win2016-core",
    "win2016dc",
    "win2016dc-core",
    "win2019",
    "win2019-core",
    "win2019dc",
    "win2019dc-core",
    "win10ent",
]


class WindowsOS(OperatingSystem):
    """Windows operating system."""

    name = "windows"
    title = "Windows"

    def get_boot_image_purposes(self, arch, subarch, release, label):
        """Gets the purpose of each boot image. Windows only allows install."""
        # Windows can support both xinstall and install, but the correct files
        # need to be available before it is enabled. This way if only xinstall
        # is available the node will boot correctly, even if fast-path
        # installer is not selected.
        purposes = []
        with ClusterConfiguration.open() as config:
            resources = config.tftp_root
        path = os.path.join(
            resources, "windows", arch, subarch, release, label
        )
        if os.path.exists(os.path.join(path, "root-dd")):
            purposes.append(BOOT_IMAGE_PURPOSE.XINSTALL)
        if os.path.exists(os.path.join(path, "pxeboot.0")):
            purposes.append(BOOT_IMAGE_PURPOSE.INSTALL)
        return purposes

    def get_default_release(self):
        """Gets the default release to use when a release is not
        explicit."""
        return WINDOWS_DEFAULT

    def get_release_title(self, release):
        """Return the title for the given release."""
        return WINDOWS_CHOICES.get(release, release)

    def requires_license_key(self, release):
        return release in REQUIRE_LICENSE_KEY

    def validate_license_key(self, release, key):
        r = re.compile("^([A-Za-z0-9]{5}-){4}[A-Za-z0-9]{5}$")
        return r.match(key)

    def compose_preseed(self, preseed_type, node, token, metadata_url):
        """Since this method exists in the WindowsOS class, it will be called
        to provide preseed to all booting Windows nodes.
        """
        # Don't override the curtin preseed.
        if preseed_type == "curtin":
            raise NotImplementedError()

        # Sets the hostname in the preseed. Using just the hostname
        # not the FQDN.
        hostname = node.hostname.split(".", 1)[0]
        # Windows max hostname length is 15 characters.
        if len(hostname) > 15:
            hostname = hostname[:15]

        credentials = {
            "maas_metadata_url": metadata_url,
            "maas_oauth_consumer_secret": "",
            "maas_oauth_consumer_key": token.consumer_key,
            "maas_oauth_token_key": token.token_key,
            "maas_oauth_token_secret": token.token_secret,
            "hostname": hostname,
        }
        return credentials

    def get_xinstall_parameters(self, arch, subarch, release, label):
        """Return the xinstall image name and type for this operating system.

        :param arch: Architecture of boot image.
        :param subarch: Sub-architecture of boot image.
        :param release: Release of boot image.
        :param label: Label of boot image.
        :return: tuple with name of root image and image type
        """
        return self._find_image(arch, subarch, release, label, dd=True)
