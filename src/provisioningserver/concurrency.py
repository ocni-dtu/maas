# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Configuration relating to concurrency in the cluster controller.

This module is intended as a place to define concurrency policies for code
running in the cluster controller. Typically this will take the form of a
Twisted concurrency primative, like `DeferredLock` or `DeferredSemaphore`.

"""

__all__ = ["boot_images", "dhcpv4", "dhcpv6"]

from twisted.internet.defer import DeferredLock

# Limit boot image imports to one at a time.
boot_images = DeferredLock()

# Limit DHCPv4 changes to one at a time.
dhcpv4 = DeferredLock()

# Limit DHCPv6 changes to one at a time.
dhcpv6 = DeferredLock()
