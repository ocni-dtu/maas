# Copyright 2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Permission enumerations."""

__all__ = ["NodePermission", "ResourcePoolPermission"]

import enum


class NodePermission(enum.Enum):
    """Permissions relating to nodes."""

    view = "view"
    edit = "edit"
    lock = "lock"
    admin = "admin"


class PodPermission(enum.Enum):
    """Permissions relating to pods."""

    view = "view"
    edit = "edit"
    create = "create"

    #: Composed machine will exist until deleted.
    compose = "compose"

    #: Composed machine will be removed once released.
    dynamic_compose = "dynamic-compose"


class ResourcePoolPermission(enum.Enum):
    """Permissions for `ResourcePool`."""

    view = "view"
    edit = "edit"
    create = "create"
    delete = "delete"
