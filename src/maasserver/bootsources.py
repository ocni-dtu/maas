# Copyright 2014-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Boot Sources."""

__all__ = [
    "cache_boot_sources",
    "ensure_boot_source_definition",
    "get_boot_sources",
    "get_os_info_from_boot_sources",
]

import datetime
import html
import os
from urllib.parse import urlparse

from maasserver.components import (
    discard_persistent_error,
    register_persistent_error,
)
from maasserver.enum import COMPONENT
from maasserver.models import (
    BootSource,
    BootSourceCache,
    BootSourceSelection,
    Config,
    Notification,
)
from maasserver.models.timestampedmodel import now
from maasserver.utils import get_maas_user_agent
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
from provisioningserver.auth import get_maas_user_gpghome
from provisioningserver.config import DEFAULT_IMAGES_URL, DEFAULT_KEYRINGS_PATH
from provisioningserver.drivers.osystem.ubuntu import UbuntuOS
from provisioningserver.import_images.download_descriptions import (
    download_all_image_descriptions,
)
from provisioningserver.import_images.keyrings import write_all_keyrings
from provisioningserver.logger import get_maas_logger, LegacyLogger
from provisioningserver.refresh import get_architecture
from provisioningserver.utils.fs import tempdir
from provisioningserver.utils.twisted import asynchronous, FOREVER
from requests.exceptions import ConnectionError
from simplestreams import util as sutil
from twisted.internet.defer import inlineCallbacks


log = LegacyLogger()
maaslog = get_maas_logger("bootsources")


@transactional
def ensure_boot_source_definition():
    """Set default boot source if none is currently defined."""
    if not BootSource.objects.exists():
        source = BootSource.objects.create(
            url=DEFAULT_IMAGES_URL, keyring_filename=DEFAULT_KEYRINGS_PATH
        )
        # Default is to import newest Ubuntu LTS release, for the current
        # architecture.
        arch = get_architecture().split("/")[0]
        # amd64 is the primary architecture for MAAS uses. Make sure its always
        # selected. If MAAS is running on another architecture select that as
        # well.
        if arch in ("", "amd64"):
            arches = ["amd64"]
        else:
            arches = [arch, "amd64"]
        ubuntu = UbuntuOS()
        BootSourceSelection.objects.create(
            boot_source=source,
            os=ubuntu.name,
            release=ubuntu.get_default_commissioning_release(),
            arches=arches,
            subarches=["*"],
            labels=["*"],
        )
        return True
    else:
        return False


@transactional
def get_boot_sources():
    """Return list of boot sources for the region to import from."""
    return [source.to_dict() for source in BootSource.objects.all()]


@transactional
def get_simplestreams_env():
    """Get environment that should be used with simplestreams."""
    env = {"GNUPGHOME": get_maas_user_gpghome()}
    if Config.objects.get_config("enable_http_proxy"):
        http_proxy = Config.objects.get_config("http_proxy")
        if http_proxy is not None:
            env["http_proxy"] = http_proxy
            env["https_proxy"] = http_proxy
            # When the proxy environment variables are set they effect the
            # entire process, including controller refresh. When the region
            # needs to refresh itself it sends itself results over HTTP to
            # 127.0.0.1.
            no_proxy_hosts = "127.0.0.1,localhost"
            # When using a proxy and using an image mirror, we may not want
            # to use the proxy to download the images, as they could be
            # localted in the local network, hence it makes no sense to use
            # it. With this, we add the image mirror localtion(s) to the
            # no proxy variable, which ensures MAAS contacts the mirror
            # directly instead of through the proxy.
            no_proxy = Config.objects.get_config("boot_images_no_proxy")
            if no_proxy:
                sources = get_boot_sources()
                for source in sources:
                    host = urlparse(source["url"]).netloc.split(":")[0]
                    no_proxy_hosts = ",".join((no_proxy_hosts, host))
            env["no_proxy"] = no_proxy_hosts
    return env


def set_simplestreams_env():
    """Set the environment that simplestreams needs."""
    # We simply set the env variables here as another simplestreams-based
    # import might be running concurrently (bootresources._import_resources)
    # and we don't want to use the environment_variables context manager that
    # would reset the environment variables (they are global to the entire
    # process) while the other import is still running.
    os.environ.update(get_simplestreams_env())


def get_os_info_from_boot_sources(os):
    """Return sources, list of releases, and list of architectures that exists
    for the given operating system from the `BootSource`'s.

    This pulls the information for BootSourceCache.
    """
    os_sources = []
    releases = set()
    arches = set()
    for source in BootSource.objects.all():
        for cache_item in BootSourceCache.objects.filter(
            boot_source=source, os=os
        ):
            if source not in os_sources:
                os_sources.append(source)
            releases.add(cache_item.release)
            arches.add(cache_item.arch)
    return os_sources, releases, arches


def get_product_title(item):
    os_title = item.get("os_title")
    release_title = item.get("release_title")
    gadget_title = item.get("gadget_title")
    if None not in (os_title, release_title, gadget_title):
        return "%s %s %s" % (os_title, release_title, gadget_title)
    elif None not in (os_title, release_title):
        return "%s %s" % (os_title, release_title)
    else:
        return None


@transactional
def _update_cache(source, descriptions):
    try:
        bootsource = BootSource.objects.get(url=source["url"])
    except BootSource.DoesNotExist:
        # The record was deleted while we were fetching the description.
        log.debug(
            "Image descriptions at {url} are no longer needed; discarding.",
            url=source["url"],
        )
    else:
        if bootsource.compare_dict_without_selections(source):
            if descriptions.is_empty():
                # No images for this source, so clear the cache.
                BootSourceCache.objects.filter(boot_source=bootsource).delete()
            else:

                def make_image_tuple(image):
                    return (
                        image.os,
                        image.arch,
                        image.subarch,
                        image.release,
                        image.kflavor,
                        image.label,
                    )

                # Get current images for the source.
                current_images = {
                    make_image_tuple(image): image
                    for image in BootSourceCache.objects.filter(
                        boot_source=bootsource
                    )
                }
                bulk_create = []
                for spec, item in descriptions.mapping.items():
                    title = get_product_title(item)
                    if title is None:
                        extra = {}
                    else:
                        extra = {"title": title}
                    current = current_images.pop(make_image_tuple(spec), None)
                    if current is not None:
                        current.release_codename = item.get("release_codename")
                        current.release_title = item.get("release_title")
                        current.support_eol = item.get("support_eol")
                        current.bootloader_type = item.get("bootloader-type")
                        current.extra = extra

                        # Support EOL needs to be a datetime so it will only
                        # be marked updated if actually different.
                        support_eol = item.get("support_eol")
                        if support_eol:
                            current.support_eol = datetime.datetime.strptime(
                                support_eol, "%Y-%m-%d"
                            ).date()
                        else:
                            current.support_eol = support_eol

                        # Will do nothing if nothing has changed.
                        current.save()
                    else:
                        created = now()
                        bulk_create.append(
                            BootSourceCache(
                                boot_source=bootsource,
                                os=spec.os,
                                arch=spec.arch,
                                subarch=spec.subarch,
                                kflavor=spec.kflavor,
                                release=spec.release,
                                label=spec.label,
                                release_codename=item.get("release_codename"),
                                release_title=item.get("release_title"),
                                support_eol=item.get("support_eol"),
                                bootloader_type=item.get("bootloader-type"),
                                extra=extra,
                                created=created,
                                updated=created,
                            )
                        )
                if bulk_create:
                    # Insert all cache items in 1 query.
                    BootSourceCache.objects.bulk_create(bulk_create)
                if current_images:
                    image_ids = {
                        image.id for _, image in current_images.items()
                    }
                    BootSourceCache.objects.filter(id__in=image_ids).delete()
            log.debug(
                "Image descriptions for {url} have been updated.",
                url=source["url"],
            )
        else:
            log.debug(
                "Image descriptions for {url} are outdated; discarding.",
                url=source["url"],
            )


@asynchronous(timeout=FOREVER)
@inlineCallbacks
def cache_boot_sources():
    """Cache all image information in boot sources.

    Called from *outside* of a transaction this will:

    1. Retrieve information about all boot sources from the database. The
       transaction is committed before proceeding.

    2. The boot sources are consulted (i.e. there's network IO now) and image
       descriptions downloaded.

    3. Update the boot source cache with the fetched information. If the boot
       source has been modified or deleted during #2 then the results are
       discarded.

    This approach does not require an exclusive lock.

    """
    # Nomenclature herein: `bootsource` is an ORM record for BootSource;
    # `source` is one of those converted to a dict. The former ought not to be
    # used outside of a transactional context.

    @transactional
    def get_sources():
        return list(
            bootsource.to_dict_without_selections()
            for bootsource in BootSource.objects.all()
            # TODO: Only where there are no corresponding BootSourceCache
            # records or the BootSource's updated timestamp is later than any
            # of the BootSourceCache records' timestamps.
        )

    @transactional
    def check_commissioning_series_selected():
        commissioning_osystem = Config.objects.get_config(
            name="commissioning_osystem"
        )
        commissioning_series = Config.objects.get_config(
            name="commissioning_distro_series"
        )
        qs = BootSourceSelection.objects.filter(
            os=commissioning_osystem, release=commissioning_series
        )
        if not qs.exists():
            if not Notification.objects.filter(
                ident="commissioning_series_unselected"
            ).exists():
                Notification.objects.create_error_for_users(
                    "%s %s is configured as the commissioning release but it "
                    "is not selected for download!"
                    % (commissioning_osystem, commissioning_series),
                    ident="commissioning_series_unselected",
                )
        qs = BootSourceCache.objects.filter(
            os=commissioning_osystem, release=commissioning_series
        )
        if not qs.exists():
            if not Notification.objects.filter(
                ident="commissioning_series_unavailable"
            ).exists():
                Notification.objects.create_error_for_users(
                    "%s %s is configured as the commissioning release but it "
                    "is unavailable in the configured streams!"
                    % (commissioning_osystem, commissioning_series),
                    ident="commissioning_series_unavailable",
                )

    @transactional
    def get_proxy():
        enabled = Config.objects.get_config("enable_http_proxy")
        proxy = Config.objects.get_config("http_proxy")
        if enabled and proxy:
            return proxy
        return False

    # FIXME: This modifies the environment of the entire process, which is Not
    # Cool. We should integrate with simplestreams in a more Pythonic manner.
    yield deferToDatabase(set_simplestreams_env)

    errors = []
    sources = yield deferToDatabase(get_sources)
    for source in sources:
        with tempdir("keyrings") as keyrings_path:
            [source] = write_all_keyrings(keyrings_path, [source])
            try:
                user_agent = yield deferToDatabase(get_maas_user_agent)
                descriptions = download_all_image_descriptions(
                    [source], user_agent=user_agent
                )
            except (IOError, ConnectionError) as error:
                msg = "Failed to import images from " "%s: %s" % (
                    source["url"],
                    error,
                )
                errors.append(msg)
                maaslog.error(msg)
            except sutil.SignatureMissingException as error:
                # Raise an error to the UI.
                proxy = yield deferToDatabase(get_proxy)
                if not proxy:
                    msg = (
                        "Failed to import images from %s (%s). Verify "
                        "network connectivity and try again."
                        % (source["url"], error)
                    )
                else:
                    msg = (
                        "Failed to import images from %s (%s). Verify "
                        "network connectivity via your external "
                        "proxy (%s) and try again."
                        % (source["url"], error, proxy)
                    )
                errors.append(msg)
            else:
                yield deferToDatabase(_update_cache, source, descriptions)

    yield deferToDatabase(check_commissioning_series_selected)

    component = COMPONENT.REGION_IMAGE_IMPORT
    if len(errors) > 0:
        maaslog.error("Unable to update boot sources cache.")
        yield deferToDatabase(
            register_persistent_error,
            component,
            "<br>".join(map(html.escape, errors)),
        )
    else:
        maaslog.info("Updated boot sources cache.")
        yield deferToDatabase(discard_persistent_error, component)
