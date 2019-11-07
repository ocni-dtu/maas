# Copyright 2016-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""The BootResource handler for the WebSocket connection."""

__all__ = ["BootResourceHandler"]

from collections import defaultdict
import json

from distro_info import UbuntuDistroInfo
from django.core.exceptions import ValidationError
from django.db.models import Q
from maasserver.bootresources import (
    import_resources,
    is_import_resources_running,
    stop_import_resources,
)
from maasserver.bootsources import (
    get_os_info_from_boot_sources,
    set_simplestreams_env,
)
from maasserver.clusterrpc.boot_images import (
    get_common_available_boot_images,
    is_import_boot_images_running,
)
from maasserver.enum import BOOT_RESOURCE_TYPE, NODE_STATUS
from maasserver.models import (
    BootResource,
    BootSource,
    BootSourceCache,
    BootSourceSelection,
    Config,
    LargeFile,
    Node,
)
from maasserver.utils import get_maas_user_agent
from maasserver.utils.converters import human_readable_bytes
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
from maasserver.websockets.base import (
    Handler,
    HandlerError,
    HandlerValidationError,
)
from provisioningserver.config import DEFAULT_IMAGES_URL, DEFAULT_KEYRINGS_PATH
from provisioningserver.drivers.osystem import OperatingSystemRegistry
from provisioningserver.import_images.download_descriptions import (
    download_all_image_descriptions,
    image_passes_filter,
)
from provisioningserver.import_images.keyrings import write_all_keyrings
from provisioningserver.logger import LegacyLogger
from provisioningserver.utils.fs import tempdir
from provisioningserver.utils.twisted import asynchronous, callOut, FOREVER
from twisted.internet.defer import Deferred


log = LegacyLogger()


def get_distro_series_info_row(series):
    """Returns the distro series row information from python-distro-info.
    """
    info = UbuntuDistroInfo()
    for row in info._avail(info._date):
        # LP: #1711191 - distro-info 0.16+ no longer returns dictionaries or
        # lists, and it now returns objects instead. As such, we need to
        # handle both cases for backwards compatibility.
        if not isinstance(row, dict):
            row = row.__dict__
        if row["series"] == series:
            return row
    return None


def format_ubuntu_distro_series(series):
    """Formats the Ubuntu distro series into a version name."""
    row = get_distro_series_info_row(series)
    if row is None:
        return series
    return row["version"]


class BootResourceHandler(Handler):
    class Meta:
        allowed_methods = [
            "poll",
            "stop_import",
            "save_ubuntu",
            "save_ubuntu_core",
            "save_other",
            "fetch",
            "delete_image",
        ]

    def format_ubuntu_sources(self):
        """Return formatted Ubuntu sources."""
        sources = []
        for source in self.ubuntu_sources:
            source_type = "custom"
            if (
                source.url == DEFAULT_IMAGES_URL
                and source.keyring_filename == DEFAULT_KEYRINGS_PATH
            ):
                source_type = "maas.io"
            sources.append(
                {
                    "source_type": source_type,
                    "url": source.url,
                    "keyring_filename": source.keyring_filename,
                    "keyring_data": bytes(source.keyring_data).decode("ascii"),
                }
            )
        return sources

    def get_ubuntu_release_selections(self):
        """Return list of all selected releases for Ubuntu. If first item in
        tuple is true, then all releases are selected by wildcard."""
        all_selected = False
        releases = set()
        for selection in BootSourceSelection.objects.all():
            if selection.os == "ubuntu":
                if selection.release == "*":
                    all_selected = True
                else:
                    releases.add(selection.release)
        return all_selected, releases

    def format_ubuntu_releases(self):
        """Return formatted Ubuntu release selections for the template."""
        releases = []
        all_releases, selected_releases = self.get_ubuntu_release_selections()
        for release in sorted(list(self.ubuntu_releases), reverse=True):
            checked = False
            if release in selected_releases:
                checked = True
                selected_releases.remove(release)
            if not checked and all_releases:
                checked = True
            releases.append(
                {
                    "name": release,
                    "title": format_ubuntu_distro_series(release),
                    "checked": checked,
                    "deleted": False,
                }
            )
        # If any selections still exist then they have been removed from the
        # stream but the selection still exists.
        for release in selected_releases:
            releases.append(
                {
                    "name": release,
                    "title": format_ubuntu_distro_series(release),
                    "checked": True,
                    "deleted": True,
                }
            )
        return releases

    def get_ubuntu_arch_selections(self):
        """Return list of all selected arches for Ubuntu. If first item in
        tuple is true, then all arches are selected by wildcard."""
        all_selected = False
        arches = set()
        for selection in BootSourceSelection.objects.all():
            if selection.os == "ubuntu":
                for arch in selection.arches:
                    if arch == "*":
                        all_selected = True
                    else:
                        arches.add(arch)
        return all_selected, arches

    def format_ubuntu_arches(self):
        """Return formatted Ubuntu architecture selections for the template."""
        arches = []
        all_arches, selected_arches = self.get_ubuntu_arch_selections()
        for arch in sorted(list(self.ubuntu_arches)):
            checked = False
            if arch in selected_arches:
                checked = True
                selected_arches.remove(arch)
            if not checked and all_arches:
                checked = True
            arches.append(
                {
                    "name": arch,
                    "title": arch,
                    "checked": checked,
                    "deleted": False,
                }
            )
        # If any selections still exist then they have been removed from the
        # stream but the selection still exists.
        for arch in selected_arches:
            arches.append(
                {"name": arch, "title": arch, "checked": True, "deleted": True}
            )
        return arches

    def check_if_image_matches_resource(self, resource, image):
        """Return True if the resource matches the image."""
        os, series = resource.name.split("/")
        arch, subarch = resource.split_arch()
        if os != image.os or series != image.release or arch != image.arch:
            return False
        if not resource.supports_subarch(subarch):
            return False
        return True

    def get_matching_resource_for_image(self, resources, image):
        """Return True if the image matches one of the resources."""
        for resource in resources:
            if self.check_if_image_matches_resource(resource, image):
                return resource
        return None

    def get_other_synced_resources(self):
        """Return all synced resources that are not Ubuntu."""
        resources = list(
            BootResource.objects.filter(rtype=BOOT_RESOURCE_TYPE.SYNCED)
            .exclude(name__startswith="ubuntu/")
            .order_by("-name", "architecture")
        )
        for resource in resources:
            self.add_resource_template_attributes(resource)
        return resources

    def add_resource_template_attributes(self, resource):
        """Adds helper attributes to the resource."""
        resource.title = self.get_resource_title(resource)
        resource.arch, resource.subarch = resource.split_arch()
        resource.number_of_nodes = self.get_number_of_nodes_deployed_for(
            resource
        )
        resource_set = resource.get_latest_set()
        if resource_set is None:
            resource.size = human_readable_bytes(0)
            resource.last_update = resource.updated
            resource.complete = False
            resource.status = "Queued for download"
            resource.downloading = False
        else:
            resource.size = human_readable_bytes(resource_set.total_size)
            resource.last_update = resource_set.updated
            resource.complete = resource_set.complete
            if not resource.complete:
                progress = resource_set.progress
                if progress > 0:
                    resource.status = "Downloading %3.0f%%" % progress
                    resource.downloading = True
                    resource.icon = "in-progress"
                else:
                    resource.status = "Queued for download"
                    resource.downloading = False
                    resource.icon = "queued"
            else:
                # See if the resource also exists on all the clusters.
                if resource in self.rack_resources:
                    resource.status = "Synced"
                    resource.downloading = False
                    resource.icon = "succeeded"
                else:
                    resource.complete = False
                    if self.racks_syncing:
                        resource.status = "Syncing to rack controller(s)"
                        resource.downloading = True
                        resource.icon = "in-progress"
                    else:
                        resource.status = (
                            "Waiting for rack controller(s) to sync"
                        )
                        resource.downloading = False
                        resource.icon = "waiting"

    def format_ubuntu_core_images(self):
        """Return formatted other images for selection."""
        resources = self.get_other_synced_resources()
        images = []
        for image in BootSourceCache.objects.filter(os="ubuntu-core"):
            resource = self.get_matching_resource_for_image(resources, image)
            if "title" in image.extra and image.extra != "":
                title = image.extra["title"]
            else:
                osystem = OperatingSystemRegistry["ubuntu-core"]
                title = osystem.get_release_title(image.release)
            if title is None:
                title = "%s/%s" % (image.os, image.release)
            images.append(
                {
                    "name": "%s/%s/%s/%s"
                    % (image.os, image.arch, image.subarch, image.release),
                    "title": title,
                    "checked": True if resource else False,
                    "deleted": False,
                }
            )
        return images

    def format_other_images(self):
        """Return formatted other images for selection."""
        resources = self.get_other_synced_resources()
        images = []
        qs = BootSourceCache.objects.exclude(
            Q(os="ubuntu") | Q(os="ubuntu-core")
        ).filter(bootloader_type=None)
        for image in qs:
            resource = self.get_matching_resource_for_image(resources, image)
            title = None
            if "title" in image.extra and image.extra != "":
                title = image.extra["title"]
            elif image.os in OperatingSystemRegistry:
                osystem = OperatingSystemRegistry[image.os]
                title = osystem.get_release_title(image.release)
            if not title:
                title = "%s/%s" % (image.os, image.release)
            images.append(
                {
                    "name": "%s/%s/%s/%s"
                    % (image.os, image.arch, image.subarch, image.release),
                    "title": title,
                    "checked": True if resource else False,
                    "deleted": False,
                }
            )
        return images

    def node_has_architecture_for_resource(self, node, resource):
        """Return True if node is the same architecture as resource."""
        arch, _ = resource.split_arch()
        node_arch, node_subarch = node.split_arch()
        return arch == node_arch and resource.supports_subarch(node_subarch)

    def get_number_of_nodes_deployed_for(self, resource):
        """Return number of nodes that are deploying the given
        os, series, and architecture."""
        if resource.rtype == BOOT_RESOURCE_TYPE.UPLOADED:
            osystem = "custom"
            distro_series = resource.name
        else:
            osystem, distro_series = resource.name.split("/")

        # Count the number of nodes with same os/release and architecture.
        count = 0
        for node in self.nodes.filter(
            osystem=osystem, distro_series=distro_series
        ):
            if self.node_has_architecture_for_resource(node, resource):
                count += 1

        # Any node that is deployed without osystem and distro_series,
        # will be using the defaults.
        if (
            self.default_osystem == osystem
            and self.default_distro_series == distro_series
        ):
            for node in self.nodes.filter(osystem="", distro_series=""):
                if self.node_has_architecture_for_resource(node, resource):
                    count += 1
        return count

    def pick_latest_datetime(self, time, other_time):
        """Return the datetime that is the latest."""
        if time is None:
            return other_time
        return max([time, other_time])

    def calculate_unique_size_for_resources(self, resources):
        """Return size of all unique largefiles for the given resources."""
        shas = set()
        size = 0
        for resource in resources:
            resource_set = resource.get_latest_set()
            if resource_set is None:
                continue
            for rfile in resource_set.files.all():
                try:
                    largefile = rfile.largefile
                except LargeFile.DoesNotExist:
                    continue
                if largefile.sha256 not in shas:
                    size += largefile.total_size
                    shas.add(largefile.sha256)
        return size

    def are_all_resources_complete(self, resources):
        """Return the complete status for all the given resources."""
        for resource in resources:
            resource_set = resource.get_latest_set()
            if resource_set is None:
                return False
            if not resource_set.complete:
                return False
        return True

    def get_last_update_for_resources(self, resources):
        """Return the latest updated time for all resources."""
        last_update = None
        for resource in resources:
            last_update = self.pick_latest_datetime(
                last_update, resource.updated
            )
            resource_set = resource.get_latest_set()
            if resource_set is not None:
                last_update = self.pick_latest_datetime(
                    last_update, resource_set.updated
                )
        return last_update

    def get_number_of_nodes_for_resources(self, resources):
        """Return the number of nodes used by all resources."""
        return sum(
            [
                self.get_number_of_nodes_deployed_for(resource)
                for resource in resources
            ]
        )

    def get_progress_for_resources(self, resources):
        """Return the overall progress for all resources."""
        size = 0
        total_size = 0
        for resource in resources:
            resource_set = resource.get_latest_set()
            if resource_set is not None:
                size += resource_set.size
                total_size += resource_set.total_size
        if size <= 0:
            # Handle division by zero
            return 0
        return 100.0 * (size / float(total_size))

    def get_resource_title(self, resource):
        """Return the title for the resource based on the type and name."""
        rtypes_with_split_names = [
            BOOT_RESOURCE_TYPE.SYNCED,
            BOOT_RESOURCE_TYPE.GENERATED,
        ]
        if "title" in resource.extra and len(resource.extra["title"]) > 0:
            return resource.extra["title"]
        elif resource.rtype in rtypes_with_split_names:
            os, series = resource.name.split("/")
            if resource.name.startswith("ubuntu/"):
                return format_ubuntu_distro_series(series)
            else:
                title = None
                if os in OperatingSystemRegistry:
                    osystem = OperatingSystemRegistry[os]
                    title = osystem.get_release_title(series)
                if not title:
                    return resource.name
                else:
                    return title
        else:
            return resource.name

    def resource_group_to_resource(self, group):
        """Convert the list of resources into one resource to be used in
        the UI."""
        # Calculate all of the values using all of the resources for
        # this combination.
        last_update = self.get_last_update_for_resources(group)
        unique_size = self.calculate_unique_size_for_resources(group)
        number_of_nodes = self.get_number_of_nodes_for_resources(group)
        complete = self.are_all_resources_complete(group)
        progress = self.get_progress_for_resources(group)

        # Set the computed attributes on the first resource as that will
        # be the only one returned to the UI.
        resource = group[0]
        resource.arch, resource.subarch = resource.split_arch()
        resource.title = self.get_resource_title(resource)
        resource.complete = complete
        resource.size = human_readable_bytes(unique_size)
        resource.last_update = last_update
        resource.number_of_nodes = number_of_nodes
        resource.complete = complete
        if not complete:
            if progress > 0:
                resource.status = "Downloading %3.0f%%" % progress
                resource.downloading = True
                resource.icon = "in-progress"
            else:
                resource.status = "Queued for download"
                resource.downloading = False
                resource.icon = "queued"
        else:
            # See if all the resources exist on all the racks.
            rack_has_resources = any(
                res in group for res in self.rack_resources
            )
            if rack_has_resources:
                resource.status = "Synced"
                resource.downloading = False
                resource.icon = "succeeded"
            else:
                resource.complete = False
                if self.racks_syncing:
                    resource.status = "Syncing to rack controller(s)"
                    resource.downloading = True
                    resource.icon = "in-progress"
                else:
                    resource.status = "Waiting for rack controller(s) to sync"
                    resource.downloading = False
                    resource.icon = "waiting"
        return resource

    def combine_resources(self, resources):
        """Return a list of resources combining all of subarchitecture
        resources into one resource."""
        resource_group = defaultdict(list)
        for resource in resources:
            arch = resource.split_arch()[0]
            key = "%s/%s" % (resource.name, arch)
            resource_group[key].append(resource)
        return [
            self.resource_group_to_resource(group)
            for _, group in resource_group.items()
        ]

    def poll(self, params):
        """Polling method that the websocket client calls.

        Since boot resource information is split between the region and the
        rack controllers the websocket uses a polling method to get the
        updated information. Pushing the information at the moment is not
        possible as no rack information is cached on the region side.
        """
        try:
            sources, releases, arches = get_os_info_from_boot_sources("ubuntu")
            self.connection_error = False
            self.ubuntu_sources = sources
            self.ubuntu_releases = releases
            self.ubuntu_arches = arches
        except ConnectionError:
            self.connection_error = True
            self.ubuntu_sources = []
            self.ubuntu_releases = set()
            self.ubuntu_arches = set()

        # Load all the nodes, so its not done on every call
        # to the method get_number_of_nodes_deployed_for.
        # XXX Danilo 2017-06-29: this used to also do
        #   .only('osystem', 'distro_series')
        # but that caused "RecursionError: maximum recursion depth exceeded
        # while calling a Python object" errors with Django 1.11 in
        #   maasserver.websockets.handlers.tests.test_bootresource
        # tests.  Since we are filtering on status field which is not in
        # "only" anyway, I don't think we ever had the benefit of prefetching
        # only those two fields.
        self.nodes = Node.objects.filter(
            status__in=[NODE_STATUS.DEPLOYED, NODE_STATUS.DEPLOYING]
        )
        self.default_osystem = Config.objects.get_config("default_osystem")
        self.default_distro_series = Config.objects.get_config(
            "default_distro_series"
        )

        # Load list of boot resources that currently exist on all racks.
        rack_images = get_common_available_boot_images()
        self.racks_syncing = is_import_boot_images_running()
        self.rack_resources = BootResource.objects.get_resources_matching_boot_images(
            rack_images
        )

        # Load all the resources and generate the JSON result.
        resources = self.combine_resources(
            BootResource.objects.filter(bootloader_type=None)
        )
        json_resources = [
            dict(
                id=resource.id,
                rtype=resource.rtype,
                name=resource.name,
                title=resource.title,
                arch=resource.arch,
                size=resource.size,
                complete=resource.complete,
                status=resource.status,
                icon=resource.icon,
                downloading=resource.downloading,
                numberOfNodes=resource.number_of_nodes,
                lastUpdate=resource.last_update.strftime(
                    "%a, %d %b. %Y %H:%M:%S"
                ),
            )
            for resource in resources
        ]
        commissioning_series = Config.objects.get_config(
            name="commissioning_distro_series"
        )
        json_ubuntu = dict(
            sources=self.format_ubuntu_sources(),
            releases=self.format_ubuntu_releases(),
            arches=self.format_ubuntu_arches(),
            commissioning_series=commissioning_series,
        )
        data = dict(
            connection_error=self.connection_error,
            region_import_running=is_import_resources_running(),
            rack_import_running=self.racks_syncing,
            resources=json_resources,
            ubuntu=json_ubuntu,
            ubuntu_core_images=self.format_ubuntu_core_images(),
            other_images=self.format_other_images(),
        )
        return json.dumps(data)

    def get_bootsource(self, params, from_db=False):
        source_type = params.get("source_type", "custom")
        if source_type == "maas.io":
            url = DEFAULT_IMAGES_URL
            keyring_filename = DEFAULT_KEYRINGS_PATH
            keyring_data = b""
        elif source_type == "custom":
            url = params["url"]
            keyring_filename = params.get("keyring_filename", "")
            keyring_data = params.get("keyring_data", "").encode("utf-8")
            if keyring_filename == "" and keyring_data == b"":
                keyring_filename = DEFAULT_KEYRINGS_PATH
        else:
            raise HandlerError("Unknown source_type: %s" % source_type)

        if from_db:
            source, created = BootSource.objects.get_or_create(
                url=url,
                defaults={
                    "keyring_filename": keyring_filename,
                    "keyring_data": keyring_data,
                },
            )
            if not created:
                source.keyring_filename = keyring_filename
                source.keyring_data = keyring_data
                source.save()
            else:
                # This was a new source, make sure its the only source in the
                # database. This is because the UI only supports handling one
                # source at a time.
                BootSource.objects.exclude(id=source.id).delete()
            return source
        else:
            return BootSource(
                url=url,
                keyring_filename=keyring_filename,
                keyring_data=keyring_data,
            )

    @asynchronous(timeout=FOREVER)
    def stop_import(self, params):
        """Called to stop the current import process."""
        d = stop_import_resources()
        d.addCallback(lambda _: deferToDatabase(transactional(self.poll), {}))
        d.addErrback(log.err, "Failed to stop the image import process.")
        return d

    @asynchronous(timeout=FOREVER)
    def save_ubuntu(self, params):
        """Called to save the Ubuntu section of the websocket."""
        # Must be administrator.
        assert self.user.is_superuser, "Permission denied."

        @transactional
        def update_source(params):
            os = "ubuntu"
            releases = params["releases"]
            arches = params["arches"]
            boot_source = self.get_bootsource(params, from_db=True)

            # Remove all selections, that are not of release.
            BootSourceSelection.objects.filter(
                boot_source=boot_source, os=os
            ).exclude(release__in=releases).delete()

            if len(releases) > 0:
                # Create or update the selections.
                for release in releases:
                    selection, _ = BootSourceSelection.objects.get_or_create(
                        boot_source=boot_source, os=os, release=release
                    )
                    selection.arches = arches
                    selection.subarches = ["*"]
                    selection.labels = ["*"]
                    selection.save()
            else:
                # Create a selection that will cause nothing to be downloaded,
                # since no releases are selected.
                selection, _ = BootSourceSelection.objects.get_or_create(
                    boot_source=boot_source, os=os, release=""
                )
                selection.arches = arches
                selection.subarches = ["*"]
                selection.labels = ["*"]
                selection.save()

        notify = Deferred()
        d = stop_import_resources()
        d.addCallback(lambda _: deferToDatabase(update_source, params))
        d.addCallback(callOut, import_resources, notify=notify)
        d.addCallback(lambda _: notify)
        d.addCallback(lambda _: deferToDatabase(transactional(self.poll), {}))
        d.addErrback(
            log.err,
            "Failed to start the image import. Unable to save the Ubuntu "
            "image(s) source information.",
        )
        return d

    @asynchronous(timeout=FOREVER)
    def save_ubuntu_core(self, params):
        """Update `BootSourceSelection`'s to only include the selected
        images."""
        # Must be administrator.
        assert self.user.is_superuser, "Permission denied."

        @transactional
        def update_selections(params):
            # Remove all Ubuntu Core selections.
            BootSourceSelection.objects.filter(os="ubuntu-core").delete()

            # Break down the images into os/release with multiple arches.
            selections = defaultdict(list)
            for image in params["images"]:
                os, arch, _, release = image.split("/", 4)
                name = "%s/%s" % (os, release)
                selections[name].append(arch)

            # Create each selection for the source.
            for name, arches in selections.items():
                os, release = name.split("/")
                cache = BootSourceCache.objects.filter(
                    os=os, arch=arch, release=release
                ).first()
                if cache is None:
                    # It is possible the cache changed while waiting for the
                    # user to perform an action. Ignore the selection as its
                    # no longer available.
                    continue
                # Create the selection for the source.
                BootSourceSelection.objects.create(
                    boot_source=cache.boot_source,
                    os=os,
                    release=release,
                    arches=arches,
                    subarches=["*"],
                    labels=["*"],
                )

        notify = Deferred()
        d = stop_import_resources()
        d.addCallback(lambda _: deferToDatabase(update_selections, params))
        d.addCallback(callOut, import_resources, notify=notify)
        d.addCallback(lambda _: notify)
        d.addCallback(lambda _: deferToDatabase(transactional(self.poll), {}))
        d.addErrback(
            log.err,
            "Failed to start the image import. Unable to save the Ubuntu Core "
            "image(s) source information",
        )
        return d

    @asynchronous(timeout=FOREVER)
    def save_other(self, params):
        """Update `BootSourceSelection`'s to only include the selected
        images."""
        # Must be administrator.
        assert self.user.is_superuser, "Permission denied."

        @transactional
        def update_selections(params):
            # Remove all selections that are not Ubuntu.
            BootSourceSelection.objects.exclude(
                Q(os="ubuntu") | Q(os="ubuntu-core")
            ).delete()

            # Break down the images into os/release with multiple arches.
            selections = defaultdict(list)
            for image in params["images"]:
                os, arch, _, release = image.split("/", 4)
                name = "%s/%s" % (os, release)
                selections[name].append(arch)

            # Create each selection for the source.
            for name, arches in selections.items():
                os, release = name.split("/")
                cache = BootSourceCache.objects.filter(
                    os=os, arch=arch, release=release
                ).first()
                if cache is None:
                    # It is possible the cache changed while waiting for the
                    # user to perform an action. Ignore the selection as its
                    # no longer available.
                    continue
                # Create the selection for the source.
                BootSourceSelection.objects.create(
                    boot_source=cache.boot_source,
                    os=os,
                    release=release,
                    arches=arches,
                    subarches=["*"],
                    labels=["*"],
                )

        notify = Deferred()
        d = stop_import_resources()
        d.addCallback(lambda _: deferToDatabase(update_selections, params))
        d.addCallback(callOut, import_resources, notify=notify)
        d.addCallback(lambda _: notify)
        d.addCallback(lambda _: deferToDatabase(transactional(self.poll), {}))
        d.addErrback(
            log.err,
            "Failed to start the image import. Unable to save the non-Ubuntu "
            "image(s) source information",
        )
        return d

    def fetch(self, params):
        """Fetch the releases and the arches from the provided source."""
        # Must be administrator.
        assert self.user.is_superuser, "Permission denied."
        # Build a source, but its not saved into the database.
        boot_source = self.get_bootsource(params, from_db=False)
        try:
            # Validate the boot source fields without committing it.
            boot_source.clean_fields()
        except ValidationError as error:
            raise HandlerValidationError(error)
        source = boot_source.to_dict_without_selections()

        # FIXME: This modifies the environment of the entire process, which is
        # Not Cool. We should integrate with simplestreams in a more
        # Pythonic manner.
        set_simplestreams_env()
        with tempdir("keyrings") as keyrings_path:
            [source] = write_all_keyrings(keyrings_path, [source])
            try:
                descriptions = download_all_image_descriptions(
                    [source], user_agent=get_maas_user_agent()
                )
            except Exception as error:
                raise HandlerError(str(error))
        items = list(descriptions.items())
        err_msg = "Mirror provides no Ubuntu images."
        if len(items) == 0:
            raise HandlerError(err_msg)
        releases = {}
        arches = {}
        for image_spec, product_info in items:
            # Only care about Ubuntu images.
            if image_spec.os != "ubuntu":
                continue
            releases[image_spec.release] = {
                "name": image_spec.release,
                "title": product_info.get(
                    "release_title",
                    format_ubuntu_distro_series(image_spec.release),
                ),
                "checked": False,
                "deleted": False,
            }
            arches[image_spec.arch] = {
                "name": image_spec.arch,
                "title": image_spec.arch,
                "checked": False,
                "deleted": False,
            }
        if len(releases) == 0 or len(arches) == 0:
            raise HandlerError(err_msg)
        return json.dumps(
            {
                "releases": list(releases.values()),
                "arches": list(arches.values()),
            }
        )

    def delete_image(self, params):
        """Delete `BootResource` by its ID."""
        # Must be administrator.
        assert self.user.is_superuser, "Permission denied."
        if "id" not in params:
            raise HandlerValidationError({"id": ["This field is required."]})
        # Convert resource into a set of resources that make up this image.
        # An image in UI is a set of resource each with different subarches
        # and kflavor.
        resource = BootResource.objects.get(id=params["id"])
        if resource.rtype == BOOT_RESOURCE_TYPE.SYNCED:
            os, release = resource.name.split("/")
            arch, subarch = resource.architecture.split("/")
            resources = BootResource.objects.filter(
                name=resource.name, architecture__startswith=arch
            )
            # Remove the selection that provides the initial resource. All
            # other resources will come from the same selection.
            for selection in BootSourceSelection.objects.all():
                if image_passes_filter(
                    [selection.to_dict()], os, arch, subarch, release, "*"
                ):
                    # This selection provided this image, remove it.
                    selection.delete()

            # Remove the whole set of resources.
            resources.delete()
        else:
            # Delete just this resource.
            resource.delete()
        return self.poll({})
