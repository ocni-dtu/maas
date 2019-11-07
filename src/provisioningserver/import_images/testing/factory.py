# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Factory helpers for the `import_images` package."""

__all__ = [
    "make_boot_resource",
    "make_image_spec",
    "make_maas_meta",
    "make_maas_meta_without_os",
    "set_resource",
]

from textwrap import dedent

from maastesting.factory import factory
from provisioningserver.import_images.boot_image_mapping import (
    BootImageMapping,
)
from provisioningserver.import_images.helpers import ImageSpec


def make_maas_meta():
    """Return fake maas.meta data."""
    return dedent(
        """\
        {"ubuntu": {"amd64": {"generic": {"generic": {"precise": {"release": {"content_id": "com.ubuntu.maas:v2:download", "path": "precise/amd64/20140410/raring/generic/boot-kernel", "product_name": "com.ubuntu.maas:v2:boot:12.04:amd64:hwe-r", "subarches": "generic,hwe-p,hwe-q,hwe-r", "version_name": "20140410"}}, "trusty": {"release": {"content_id": "com.ubuntu.maas:v2:download", "path": "trusty/amd64/20140416.1/root-image.gz", "product_name": "com.ubuntu.maas:v2:boot:14.04:amd64:hwe-t", "subarches": "generic,hwe-p,hwe-q,hwe-r,hwe-s,hwe-t", "version_name": "20140416.1"}}}}, "hwe-s": {"generic": {"precise": {"release": {"content_id": "com.ubuntu.maas:v2:download", "path": "precise/amd64/20140410/saucy/generic/boot-kernel", "product_name": "com.ubuntu.maas:v2:boot:12.04:amd64:hwe-s", "subarches": "generic,hwe-p,hwe-q,hwe-r,hwe-s", "version_name": "20140410"}}}}}}}"""
    )  # NOQA


def make_maas_meta_legacy():
    """Return fake maas.meta data from < 2.1."""
    return dedent(
        """\
        {"ubuntu": {"amd64": {"generic": {"precise": {"release": {"content_id": "com.ubuntu.maas:v2:download", "path": "precise/amd64/20140410/raring/generic/boot-kernel", "product_name": "com.ubuntu.maas:v2:boot:12.04:amd64:hwe-r", "subarches": "generic,hwe-p,hwe-q,hwe-r", "version_name": "20140410"}}, "trusty": {"release": {"content_id": "com.ubuntu.maas:v2:download", "path": "trusty/amd64/20140416.1/root-image.gz", "product_name": "com.ubuntu.maas:v2:boot:14.04:amd64:hwe-t", "subarches": "generic,hwe-p,hwe-q,hwe-r,hwe-s,hwe-t", "version_name": "20140416.1"}}}, "hwe-s": {"precise": {"release": {"content_id": "com.ubuntu.maas:v2:download", "path": "precise/amd64/20140410/saucy/generic/boot-kernel", "product_name": "com.ubuntu.maas:v2:boot:12.04:amd64:hwe-s", "subarches": "generic,hwe-p,hwe-q,hwe-r,hwe-s", "version_name": "20140410"}}}}}}"""
    )  # NOQA


def make_maas_meta_without_os():
    """Return fake maas.meta data, without the os field."""
    return dedent(
        """\
        {"amd64": {"generic": {"precise": {"release": {"content_id": "com.ubuntu.maas:v2:download", "path": "precise/amd64/20140410/raring/generic/boot-kernel", "product_name": "com.ubuntu.maas:v2:boot:12.04:amd64:hwe-r", "subarches": "generic,hwe-p,hwe-q,hwe-r", "version_name": "20140410"}}, "trusty": {"release": {"content_id": "com.ubuntu.maas:v2:download", "path": "trusty/amd64/20140416.1/root-image.gz", "product_name": "com.ubuntu.maas:v2:boot:14.04:amd64:hwe-t", "subarches": "generic,hwe-p,hwe-q,hwe-r,hwe-s,hwe-t", "version_name": "20140416.1"}}}, "hwe-s": {"precise": {"release": {"content_id": "com.ubuntu.maas:v2:download", "path": "precise/amd64/20140410/saucy/generic/boot-kernel", "product_name": "com.ubuntu.maas:v2:boot:12.04:amd64:hwe-s", "subarches": "generic,hwe-p,hwe-q,hwe-r,hwe-s", "version_name": "20140410"}}}}}"""
    )  # NOQA


def make_boot_resource():
    """Create a fake resource dict."""
    return {
        "content_id": factory.make_name("content_id"),
        "product_name": factory.make_name("product_name"),
        "version_name": factory.make_name("version_name"),
    }


def make_image_spec(
    os=None, arch=None, subarch=None, release=None, kflavor=None, label=None
):
    """Return an `ImageSpec` with random values."""
    if os is None:
        os = factory.make_name("os")
    if arch is None:
        arch = factory.make_name("arch")
    if subarch is None:
        subarch = factory.make_name("subarch")
    if kflavor is None:
        kflavor = "generic"
    if release is None:
        release = factory.make_name("release")
    if label is None:
        label = factory.make_name("label")
    return ImageSpec(os, arch, subarch, kflavor, release, label)


def set_resource(boot_dict=None, image_spec=None, resource=None):
    """Add boot resource to a `BootImageMapping`, creating it if necessary."""
    if boot_dict is None:
        boot_dict = BootImageMapping()
    if image_spec is None:
        image_spec = make_image_spec()
    if resource is None:
        resource = factory.make_name("boot-resource")
    boot_dict.mapping[image_spec] = resource
    return boot_dict
