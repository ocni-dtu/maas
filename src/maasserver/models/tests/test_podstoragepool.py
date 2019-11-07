# Copyright 2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the PodStoragePool model."""

__all__ = []

from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase


class TestPodStoragePool(MAASServerTestCase):
    def test_get_used_storage(self):
        pool = factory.make_PodStoragePool()
        size = 0
        for _ in range(3):
            bd = factory.make_PhysicalBlockDevice(storage_pool=pool)
            size += bd.size
        self.assertEquals(size, pool.get_used_storage())

    def test_get_used_storage_returns_zero(self):
        pool = factory.make_PodStoragePool()
        self.assertEquals(0, pool.get_used_storage())
