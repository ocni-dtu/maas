# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Helpers for operating systems in testing."""

__all__ = ["make_usable_osystem", "patch_usable_osystems"]

from random import randint

from maasserver.clusterrpc import boot_images
from maasserver.clusterrpc.testing.osystems import (
    make_rpc_osystem,
    make_rpc_release,
)
from maasserver.enum import BOOT_RESOURCE_TYPE
from maasserver.models import Node
from maasserver.testing.factory import factory
from provisioningserver.drivers.osystem import (
    CustomOS,
    OperatingSystemRegistry,
)


def make_osystem_with_releases(testcase, osystem_name=None, releases=None):
    """Generate an arbitrary operating system.

    :param osystem_name: The operating system name. Useful in cases where
        we need to test that not supplying an os works correctly.
    :param releases: The list of releases name. Useful in cases where
        we need to test that not supplying a release works correctly.
    """
    if osystem_name is None:
        osystem_name = factory.make_name("os")
    if releases is None:
        releases = [factory.make_name("release") for _ in range(3)]
    rpc_releases = [make_rpc_release(release) for release in releases]
    if osystem_name not in OperatingSystemRegistry:
        OperatingSystemRegistry.register_item(osystem_name, CustomOS())
        testcase.addCleanup(
            OperatingSystemRegistry.unregister_item, osystem_name
        )
    # Make sure the commissioning Ubuntu release and all created releases
    # are available to all architectures.
    architectures = [
        node.architecture for node in Node.objects.distinct("architecture")
    ]
    if len(architectures) == 0:
        architectures.append("%s/generic" % factory.make_name("arch"))
    for arch in architectures:
        factory.make_default_ubuntu_release_bootable(arch.split("/")[0])
        for release in releases:
            factory.make_BootResource(
                rtype=BOOT_RESOURCE_TYPE.UPLOADED,
                name=("%s/%s" % (osystem_name, release)),
                architecture=arch,
            )
    return make_rpc_osystem(osystem_name, releases=rpc_releases)


def patch_usable_osystems(testcase, osystems=None, allow_empty=True):
    """Set a fixed list of usable operating systems.

    A usable operating system is one for which boot images are available.

    :param testcase: A `TestCase` whose `patch` this function can use.
    :param osystems: Optional list of operating systems.  If omitted,
        defaults to a list (which may be empty) of random operating systems.
    """
    start = 0
    if allow_empty is False:
        start = 1
    if osystems is None:
        osystems = [
            make_osystem_with_releases(testcase)
            for _ in range(randint(start, 2))
        ]
    os_releases = []
    for osystem in osystems:
        for release in osystem["releases"]:
            os_releases.append(
                {
                    "osystem": osystem["name"],
                    "release": release["name"],
                    "purpose": "xinstall",
                }
            )
    # Make sure the Commissioning release is always available.
    ubuntu = factory.make_default_ubuntu_release_bootable()
    os_releases.append(
        {
            "osystem": ubuntu.name.split("/")[0],
            "release": ubuntu.name.split("/")[1],
            "purpose": "xinstall",
        }
    )
    testcase.patch(
        boot_images, "get_common_available_boot_images"
    ).return_value = os_releases


def make_usable_osystem(testcase, osystem_name=None, releases=None):
    """Return arbitrary operating system, and make it "usable."

    A usable operating system is one that is returned from the
    RPC call ListOperatingSystems.

    :param testcase: A `TestCase` whose `patch` this function can pass to
        `patch_usable_osystems`.
    :param osystem_name: The operating system name. Useful in cases where
        we need to test that not supplying an os works correctly.
    :param releases: The list of releases name. Useful in cases where
        we need to test that not supplying a release works correctly.
    """
    osystem = make_osystem_with_releases(
        testcase, osystem_name=osystem_name, releases=releases
    )
    patch_usable_osystems(testcase, [osystem])
    return osystem
