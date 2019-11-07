# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Boot Resource."""

__all__ = ["BootResource"]

from operator import attrgetter

from django.core.exceptions import ValidationError
from django.db.models import (
    BooleanField,
    CharField,
    Count,
    IntegerField,
    Manager,
    Prefetch,
    Sum,
)
from maasserver import DefaultMeta
from maasserver.enum import (
    BOOT_RESOURCE_FILE_TYPE,
    BOOT_RESOURCE_TYPE,
    BOOT_RESOURCE_TYPE_CHOICES,
    BOOT_RESOURCE_TYPE_CHOICES_DICT,
)
from maasserver.fields import JSONObjectField
from maasserver.models.bootresourceset import BootResourceSet
from maasserver.models.bootsourcecache import BootSourceCache
from maasserver.models.cleansave import CleanSave
from maasserver.models.timestampedmodel import now, TimestampedModel
from maasserver.utils.orm import get_first, get_one
from provisioningserver.drivers.osystem import OperatingSystemRegistry
from provisioningserver.utils.twisted import undefined

# Names on boot resources have a specific meaning depending on the type
# of boot resource. If its a synced or generated image then the name must
# be in the format os/series.
RTYPE_REQUIRING_OS_SERIES_NAME = (
    BOOT_RESOURCE_TYPE.SYNCED,
    BOOT_RESOURCE_TYPE.GENERATED,
)


class BootResourceManager(Manager):
    def _has_resource(self, rtype, name, architecture, subarchitecture):
        """Return True if `BootResource` exists with given rtype, name,
        architecture, and subarchitecture."""
        arch = "%s/%s" % (architecture, subarchitecture)
        return self.filter(rtype=rtype, name=name, architecture=arch).exists()

    def _get_resource(self, rtype, name, architecture, subarchitecture):
        """Return `BootResource` with given rtype, name, architecture, and
        subarchitecture."""
        arch = "%s/%s" % (architecture, subarchitecture)
        return get_one(self.filter(rtype=rtype, name=name, architecture=arch))

    def has_synced_resource(
        self, osystem, architecture, subarchitecture, series
    ):
        """Return True if `BootResource` exists with type of SYNCED, and given
        osystem, architecture, subarchitecture, and series."""
        name = "%s/%s" % (osystem, series)
        return self._has_resource(
            BOOT_RESOURCE_TYPE.SYNCED, name, architecture, subarchitecture
        )

    def get_synced_resource(
        self, osystem, architecture, subarchitecture, series
    ):
        """Return `BootResource` with type of SYNCED, and given
        osystem, architecture, subarchitecture, and series."""
        name = "%s/%s" % (osystem, series)
        return self._get_resource(
            BOOT_RESOURCE_TYPE.SYNCED, name, architecture, subarchitecture
        )

    def has_generated_resource(
        self, osystem, architecture, subarchitecture, series
    ):
        """Return True if `BootResource` exists with type of GENERATED, and
        given osystem, architecture, subarchitecture, and series."""
        name = "%s/%s" % (osystem, series)
        return self._has_resource(
            BOOT_RESOURCE_TYPE.GENERATED, name, architecture, subarchitecture
        )

    def get_generated_resource(
        self, osystem, architecture, subarchitecture, series
    ):
        """Return `BootResource` with type of GENERATED, and given
        osystem, architecture, subarchitecture, and series."""
        name = "%s/%s" % (osystem, series)
        return self._get_resource(
            BOOT_RESOURCE_TYPE.GENERATED, name, architecture, subarchitecture
        )

    def has_uploaded_resource(self, name, architecture, subarchitecture):
        """Return True if `BootResource` exists with type of UPLOADED, and
        given name, architecture, and subarchitecture."""
        return self._has_resource(
            BOOT_RESOURCE_TYPE.UPLOADED, name, architecture, subarchitecture
        )

    def get_uploaded_resource(self, name, architecture, subarchitecture):
        """Return `BootResource` with type of UPLOADED, and given
        name, architecture, and subarchitecture."""
        return self._get_resource(
            BOOT_RESOURCE_TYPE.UPLOADED, name, architecture, subarchitecture
        )

    def get_usable_architectures(self):
        """Return the set of usable architectures.

        Return the architectures for which the has at least one
        commissioning image and at least one install image.
        """
        arches = set()
        for resource in self.all():
            resource_set = resource.get_latest_complete_set()
            if (
                resource_set is not None
                and resource_set.commissionable
                and resource_set.xinstallable
            ):
                if (
                    "hwe-" not in resource.architecture
                    and "ga-" not in resource.architecture
                ):
                    arches.add(resource.architecture)
                if "subarches" in resource.extra:
                    arch, _ = resource.split_arch()
                    for subarch in resource.extra["subarches"].split(","):
                        if "hwe-" not in subarch and "ga-" not in subarch:
                            arches.add("%s/%s" % (arch, subarch.strip()))
        return sorted(arches)

    def get_commissionable_resource(self, osystem, series):
        """Return generator for all commissionable resources for the
        given osystem and series."""
        name = "%s/%s" % (osystem, series)
        resources = self.filter(name=name).order_by("architecture")
        for resource in resources:
            resource_set = resource.get_latest_complete_set()
            if resource_set is not None and resource_set.commissionable:
                yield resource

    def get_default_commissioning_resource(self, osystem, series):
        """Return best guess `BootResource` for the given osystem and series.

        Prefers `i386` then `amd64` resources if available.  Returns `None`
        if none match requirements.
        """
        commissionable = list(
            self.get_commissionable_resource(osystem, series)
        )
        for resource in commissionable:
            # Prefer i386. It will work for most cases where we don't
            # know the actual architecture.
            arch, subarch = resource.split_arch()
            if arch == "i386":
                return resource
        for resource in commissionable:
            # Prefer amd64. It has a much better chance of working than
            # say arm or ppc.
            arch, subarch = resource.split_arch()
            if arch == "amd64":
                return resource
        return get_first(commissionable)

    def get_resource_for(self, osystem, architecture, subarchitecture, series):
        """Return resource that support the given osystem, architecture,
        subarchitecture, and series."""
        name = "%s/%s" % (osystem, series)
        resources = BootResource.objects.filter(
            rtype__in=RTYPE_REQUIRING_OS_SERIES_NAME,
            name=name,
            architecture__startswith=architecture,
        )
        for resource in resources:
            if resource.supports_subarch(subarchitecture):
                return resource
        return None

    def get_resources_matching_boot_images(self, images):
        """Return `BootResource` that match the given images."""
        resources = BootResource.objects.all()
        matched_resources = set()
        for image in images:
            if image["osystem"] == "bootloader":
                matching_resources = resources.filter(
                    rtype=BOOT_RESOURCE_TYPE.SYNCED,
                    bootloader_type=image["release"],
                    architecture__startswith=image["architecture"],
                )
            else:
                if image["osystem"] != "custom":
                    rtypes = [
                        BOOT_RESOURCE_TYPE.SYNCED,
                        BOOT_RESOURCE_TYPE.GENERATED,
                        BOOT_RESOURCE_TYPE.UPLOADED,
                    ]
                    name = "%s/%s" % (image["osystem"], image["release"])
                else:
                    rtypes = [BOOT_RESOURCE_TYPE.UPLOADED]
                    name = image["release"]
                matching_resources = resources.filter(
                    rtype__in=rtypes,
                    name=name,
                    architecture__startswith=image["architecture"],
                )
            for resource in matching_resources:
                if resource is None:
                    # This shouldn't happen at all, but just to be sure.
                    continue
                if not resource.supports_subarch(image["subarchitecture"]):
                    # This matching resource doesn't support the images
                    # subarchitecture, so its not a matching resource.
                    continue
                resource_set = resource.get_latest_complete_set()
                if resource_set is None:
                    # Possible that the import just started, and there is no
                    # set. Making it not a matching resource, as it cannot
                    # exist on the cluster unless it has a set.
                    continue
                if (
                    resource_set.label != image["label"]
                    and image["label"] != "*"
                ):
                    # The label is different so the cluster has a different
                    # version of this set.
                    continue
                matched_resources.add(resource)
        return list(matched_resources)

    def boot_images_are_in_sync(self, images):
        """Return True if the given images match items in the `BootResource`
        table."""
        resources = BootResource.objects.all()
        matched_resources = self.get_resources_matching_boot_images(images)
        if len(matched_resources) == 0 and len(images) > 0:
            # If there are images, but no resources then there is a mismatch.
            return False
        if len(matched_resources) != resources.count():
            # If not all resources have been matched then there is a mismatch.
            return False
        return True

    def get_hwe_kernels(
        self,
        name=None,
        architecture=None,
        kflavor=None,
        include_subarches=False,
    ):
        """Return the set of kernels."""
        from maasserver.utils.osystems import get_release_version_from_string

        if not name:
            name = ""
        if not architecture:
            architecture = ""

        sets_prefetch = BootResourceSet.objects.annotate(
            files_count=Count("files__id"),
            files_size=Sum("files__largefile__size"),
            files_total_size=Sum("files__largefile__total_size"),
        )
        sets_prefetch = sets_prefetch.prefetch_related("files")
        sets_prefetch = sets_prefetch.order_by("id")
        query = self.filter(
            architecture__startswith=architecture, name__startswith=name
        )
        query = query.prefetch_related(Prefetch("sets", sets_prefetch))

        kernels = set()
        for resource in query:
            if kflavor is not None and resource.kflavor != kflavor:
                continue
            resource_set = resource.get_latest_complete_set()
            if (
                resource_set is None
                or not resource_set.commissionable
                or not resource_set.xinstallable
            ):
                continue
            subarch = resource.split_arch()[1]
            if subarch.startswith("hwe-") or subarch.startswith("ga-"):
                kernels.add(subarch)
                if resource.rolling:
                    subarch_parts = subarch.split("-")
                    subarch_parts[1] = "rolling"
                    kernels.add("-".join(subarch_parts))

            if include_subarches and "subarches" in resource.extra:
                for subarch in resource.extra["subarches"].split(","):
                    if subarch.startswith("hwe-") or subarch.startswith("ga-"):
                        if kflavor is None:
                            kernels.add(subarch)
                        else:
                            # generic kflavors are not included in the subarch.
                            if kflavor == "generic":
                                kparts = subarch.split("-")
                                if len(kparts) == 2:
                                    kernels.add(subarch)
                            else:
                                if kflavor in subarch:
                                    kernels.add(subarch)
        # Make sure kernels named with a version come after the kernels named
        # with the first letter of release. This switched in Xenial so this
        # preserves the chronological order of the kernels.
        return sorted(
            kernels, key=lambda k: get_release_version_from_string(k)
        )

    def get_usable_hwe_kernels(
        self, name=None, architecture=None, kflavor=None
    ):
        """Return the set of usable kernels for the given name, arch, kflavor.

        Returns only the list of kernels which MAAS has downloaded. For example
        if Trusty and Xenial have been downloaded this will return hwe-t,
        ga-16.04, hwe-16.04, hwe-16.04-edge, hwe-16.04-lowlatency, and
        hwe-16.04-lowlatency-edge."""
        return self.get_hwe_kernels(name, architecture, kflavor, False)

    def get_supported_hwe_kernels(
        self, name=None, architecture=None, kflavor=None
    ):
        """Return the set of supported kernels for the given name, arch,
        kflavor.

        Returns the list of kernels downloaded by MAAS and the subarches each
        of those kernels support. For example if Trusty and Xenial have been
        downloaded this will return hwe-p, hwe-q, hwe-r, hwe-s, hwe-t, hwe-u,
        hwe-v, hwe-w, ga-16.04, hwe-16.04, hwe-16.04-edge,
        hwe-16.04-lowlatency, and hwe-16.04-lowlatency-edge."""
        return self.get_hwe_kernels(name, architecture, kflavor, True)

    def get_kpackage_for_node(self, node):
        """Return the kernel package name for the kernel specified."""
        if not node.hwe_kernel:
            return None
        elif "hwe-rolling" in node.hwe_kernel:
            kparts = node.hwe_kernel.split("-")
            if kparts[-1] == "edge":
                if len(kparts) == 3:
                    kflavor = "generic"
                else:
                    kflavor = kparts[-2]
                return "linux-%s-hwe-rolling-edge" % kflavor
            else:
                if len(kparts) == 2:
                    kflavor = "generic"
                else:
                    kflavor = kparts[-1]
                return "linux-%s-hwe-rolling" % kflavor

        arch = node.split_arch()[0]
        os_release = node.get_osystem() + "/" + node.get_distro_series()
        # Before hwe_kernel was introduced the subarchitecture was the
        # hwe_kernel simple stream still uses this convention
        hwe_arch = arch + "/" + node.hwe_kernel

        resource = self.filter(name=os_release, architecture=hwe_arch).first()
        if resource:
            latest_set = resource.get_latest_complete_set()
            if latest_set:
                kernel = latest_set.files.filter(
                    filetype=BOOT_RESOURCE_FILE_TYPE.BOOT_KERNEL
                ).first()
                if kernel and "kpackage" in kernel.extra:
                    return kernel.extra["kpackage"]
        return None

    def get_kparams_for_node(
        self, node, default_osystem=undefined, default_distro_series=undefined
    ):
        """Return the kernel package name for the kernel specified."""
        arch = node.split_arch()[0]
        os_release = (
            node.get_osystem(default=default_osystem)
            + "/"
            + node.get_distro_series(default=default_distro_series)
        )

        # Before hwe_kernel was introduced the subarchitecture was the
        # hwe_kernel simple stream still uses this convention
        if node.hwe_kernel is None or node.hwe_kernel == "":
            hwe_arch = arch + "/generic"
        else:
            hwe_arch = arch + "/" + node.hwe_kernel

        resource = self.filter(name=os_release, architecture=hwe_arch).first()
        if resource:
            latest_set = resource.get_latest_set()
            if latest_set:
                kernel = latest_set.files.filter(
                    filetype=BOOT_RESOURCE_FILE_TYPE.BOOT_KERNEL
                ).first()
                if kernel and "kparams" in kernel.extra:
                    return kernel.extra["kparams"]
        return None

    def get_available_commissioning_resources(self):
        """Return list of Ubuntu boot resources that can be used for
        commissioning.

        Only return's LTS releases that have been fully imported.
        """
        # Get the LTS releases placing the release with the longest support
        # window first.
        lts_releases = BootSourceCache.objects.filter(
            os="ubuntu", release_title__endswith="LTS"
        )
        lts_releases = lts_releases.exclude(support_eol__isnull=True)
        lts_releases = lts_releases.order_by("-support_eol")
        lts_releases = lts_releases.values("release").distinct()
        lts_releases = [
            "ubuntu/%s" % release["release"] for release in lts_releases
        ]

        # Filter the completed and commissionable resources. The operation
        # loses the ordering of the releases.
        resources = []
        for resource in self.filter(
            rtype=BOOT_RESOURCE_TYPE.SYNCED, name__in=lts_releases
        ):
            resource_set = resource.get_latest_complete_set()
            if resource_set is not None and resource_set.commissionable:
                resources.append(resource)

        # Re-order placing the resource with the longest support window first.
        return sorted(
            resources, key=lambda resource: lts_releases.index(resource.name)
        )


def validate_architecture(value):
    """Validates that architecture value contains a subarchitecture."""
    if "/" not in value:
        raise ValidationError("Invalid architecture, missing subarchitecture.")


class BootResource(CleanSave, TimestampedModel):
    """Boot resource.

    Each `BootResource` represents a os/series combination or custom uploaded
    image that maps to a specific architecture that a node can use to
    commission or install.

    `BootResource` can have multiple `BootResourceSet` corresponding to
    different versions of this `BootResource`. When a node selects this
    `BootResource` the newest `BootResourceSet` is used to deploy to the node.

    :ivar rtype: Type of `BootResource`. See the vocabulary
        :class:`BOOT_RESOURCE_TYPE`.
    :ivar name: Name of the `BootResource`. If its BOOT_RESOURCE_TYPE.UPLOADED
        then `name` is used to reference this image. If its
        BOOT_RESOURCE_TYPE.SYCNED or BOOT_RESOURCE_TYPE.GENERATED then its
        in the format of os/series.
    :ivar architecture: Architecture of the `BootResource`. It must be in
        the format arch/subarch.
    :ivar extra: Extra information about the file. This is only used
        for synced Ubuntu images.
    """

    class Meta(DefaultMeta):
        unique_together = (("name", "architecture"),)

    objects = BootResourceManager()

    rtype = IntegerField(choices=BOOT_RESOURCE_TYPE_CHOICES, editable=False)

    name = CharField(max_length=255, blank=False)

    architecture = CharField(
        max_length=255, blank=False, validators=[validate_architecture]
    )

    bootloader_type = CharField(max_length=32, blank=True, null=True)

    kflavor = CharField(max_length=32, blank=True, null=True)

    # The hwe-rolling kernel is a meta-package which depends on the latest
    # kernel available. Instead of placing a duplicate kernel in the stream
    # SimpleStreams adds a boolean field to indicate that the hwe-rolling
    # kernel meta-package points to this kernel. When the rolling field is set
    # true MAAS allows users to deploy the hwe-rolling kernel by using this
    # BootResource kernel and instructs Curtin to install the meta-package.
    rolling = BooleanField(blank=False, null=False, default=False)

    extra = JSONObjectField(blank=True, default="", editable=False)

    def __str__(self):
        return "<BootResource name=%s, arch=%s, kflavor=%s>" % (
            self.name,
            self.architecture,
            self.kflavor,
        )

    @property
    def display_rtype(self):
        """Return rtype text as displayed to the user."""
        return BOOT_RESOURCE_TYPE_CHOICES_DICT[self.rtype]

    def clean(self):
        """Validate the model.

        Checks that the name is in a valid format, for its type.
        """
        if self.rtype == BOOT_RESOURCE_TYPE.UPLOADED:
            if "/" in self.name:
                os_name = self.name.split("/")[0]
                osystem = OperatingSystemRegistry.get_item(os_name)
                if osystem is None:
                    raise ValidationError(
                        "%s boot resource cannot contain a '/' in it's name "
                        "unless it starts with a supported operating system."
                        % (self.display_rtype)
                    )
        elif self.rtype in RTYPE_REQUIRING_OS_SERIES_NAME:
            if "/" not in self.name:
                raise ValidationError(
                    "%s boot resource must contain a '/' in it's name."
                    % (self.display_rtype)
                )

    def unique_error_message(self, model_class, unique_check):
        if unique_check == ("name", "architecture"):
            return "Boot resource of name, and architecture already " "exists."
        return super(BootResource, self).unique_error_message(
            model_class, unique_check
        )

    def get_latest_set(self):
        """Return latest `BootResourceSet`."""
        if (
            not hasattr(self, "_prefetched_objects_cache")
            or "sets" not in self._prefetched_objects_cache
        ):
            return self.sets.order_by("id").last()
        elif self.sets.all():
            return sorted(self.sets.all(), key=attrgetter("id"), reverse=True)[
                0
            ]
        else:
            return None

    def get_latest_complete_set(self):
        """Return latest `BootResourceSet` where all `BootResouceFile`'s
        are complete."""
        if (
            not hasattr(self, "_prefetched_objects_cache")
            or "sets" not in self._prefetched_objects_cache
        ):
            resource_sets = self.sets.order_by("-id").annotate(
                files_count=Count("files__id"),
                files_size=Sum("files__largefile__size"),
                files_total_size=Sum("files__largefile__total_size"),
            )
        else:
            resource_sets = sorted(
                self.sets.all(), key=attrgetter("id"), reverse=True
            )
        for resource_set in resource_sets:
            if (
                resource_set.files_count > 0
                and resource_set.files_size == resource_set.files_total_size
            ):
                return resource_set
        return None

    def split_arch(self):
        return self.architecture.split("/")

    def get_next_version_name(self):
        """Return the version a `BootResourceSet` should use when adding to
        this resource.

        The version naming is specific to how the resource sets will be sorted
        by simplestreams. The version name is YYYYmmdd, with an optional
        revision index. (e.g. 20140822.1)

        This method gets the current date, and checks if a revision already
        exists in the database. If it doesn't then just the current date is
        returned. If it does exists then the next revision in the set for that
        date will be returned.

        :return: Name of version to use for a new set on this `BootResource`.
        :rtype: string
        """
        version_name = now().strftime("%Y%m%d")
        sets = self.sets.filter(version__startswith=version_name).order_by(
            "version"
        )
        if not sets.exists():
            return version_name
        max_idx = 0
        for resource_set in sets:
            if "." in resource_set.version:
                _, set_idx = resource_set.version.split(".")
                set_idx = int(set_idx)
                if set_idx > max_idx:
                    max_idx = set_idx
        return "%s.%d" % (version_name, max_idx + 1)

    def supports_subarch(self, subarch):
        """Return True if the resource supports the given subarch."""
        _, self_subarch = self.split_arch()
        if subarch == self_subarch:
            return True
        if "subarches" not in self.extra:
            return False
        subarches = self.extra["subarches"].split(",")
        return subarch in subarches
