# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Ubuntu Operating System."""

__all__ = ["UbuntuOS"]

from distro_info import UbuntuDistroInfo
from provisioningserver.drivers.osystem import (
    BOOT_IMAGE_PURPOSE,
    OperatingSystem,
)


class UbuntuOS(OperatingSystem):
    """Ubuntu operating system."""

    name = "ubuntu"
    title = "Ubuntu"

    def __init__(self):
        self.ubuntu_distro_info = UbuntuDistroInfo()

    def get_boot_image_purposes(self, arch, subarch, release, label):
        """Gets the purpose of each boot image."""
        return [
            BOOT_IMAGE_PURPOSE.COMMISSIONING,
            BOOT_IMAGE_PURPOSE.INSTALL,
            BOOT_IMAGE_PURPOSE.XINSTALL,
            BOOT_IMAGE_PURPOSE.DISKLESS,
        ]

    def is_release_supported(self, release):
        """Return True when the release is supported, False otherwise."""
        row = self.get_distro_series_info_row(release)
        return row is not None

    def get_lts_release(self):
        """Return the latest Ubuntu LTS release."""
        # XXX ltrager 2018-01-08 - Force the default to be bionic before
        # bionic release for testing with MAAS 2.4.0.
        return "bionic"

    def get_default_release(self):
        """Gets the default release to use when a release is not
        explicit."""
        return self.get_lts_release()

    def get_supported_commissioning_releases(self):
        """Gets the supported commissioning releases for Ubuntu. This
        only exists on Ubuntu, because that is the only operating
        system that supports commissioning.
        """
        unsupported_releases = ["precise", "trusty"]
        return [
            name
            for name in self.ubuntu_distro_info.supported()
            if name not in unsupported_releases
            if self.ubuntu_distro_info.is_lts(name)
        ]

    def get_default_commissioning_release(self):
        """Gets the default commissioning release for Ubuntu. This only exists
        on Ubuntu, because that is the only operating system that supports
        commissioning.
        """
        return self.get_lts_release()

    def get_distro_series_info_row(self, release):
        """Returns the distro series row information from python-distro-info.
        """
        info = self.ubuntu_distro_info
        for row in info._avail(info._date):
            # LP: #1711191 - distro-info 0.16+ no longer returns dictionaries
            # or lists, and it now returns objects instead. In this case, we
            # return either the object or the dictionary so get_release_title
            # handles the formating correctly.
            row_dict = row
            if not isinstance(row, dict):
                row_dict = row.__dict__
            if row_dict["series"] == release:
                return row
        return None

    def get_release_title(self, release):
        """Return the title for the given release."""
        row = self.get_distro_series_info_row(release)
        if row is None:
            return None
        return self.ubuntu_distro_info._format("fullname", row)

    def get_xinstall_parameters(self, arch, subarch, release, label):
        """Return the xinstall image name and type for this operating system.

        :param arch: Architecture of boot image.
        :param subarch: Sub-architecture of boot image.
        :param release: Release of boot image.
        :param label: Label of boot image.
        :return: tuple with name of root image and image type
        """
        return self._find_image(
            arch, subarch, release, label, tgz=True, squashfs=True
        )
