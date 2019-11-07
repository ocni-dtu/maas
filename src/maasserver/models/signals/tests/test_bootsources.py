# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test the behaviour of bootsource signals."""

__all__ = []

from django.db import connection
from maasserver.bootsources import cache_boot_sources
from maasserver.models import BootSource, signals
from maasserver.models.config import Config
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maastesting.matchers import (
    MockCalledOnceWith,
    MockCalledWith,
    MockNotCalled,
)
from twisted.internet import reactor


class TestBootSourceSignals(MAASServerTestCase):
    """Tests for the `BootSource` model's signals."""

    def test_doesnt_update_on_initial_BootSource_create(self):
        # The way MAAS detects if the BootSource is the initial creation is by
        # looking at its id. Since Postgres always increments the id only the
        # initial BootSource create(default) will have id=1. When running
        # multiple tests the database may be rolled back but Postgres still
        # increments ids as normal. This resets the sequence to 1.
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER SEQUENCE %s_id_seq RESTART WITH 1"
                % BootSource._meta.db_table
            )
        post_commit_do = self.patch(signals.bootsources, "post_commit_do")
        factory.make_BootSource(keyring_data=factory.make_bytes())
        self.assertThat(post_commit_do, MockNotCalled())

    def test_arranges_for_update_on_BootSource_create(self):
        post_commit_do = self.patch(signals.bootsources, "post_commit_do")
        factory.make_BootSource(keyring_data=factory.make_bytes())
        factory.make_BootSource(keyring_data=factory.make_bytes())
        self.assertThat(
            post_commit_do,
            MockCalledWith(reactor.callLater, 0, cache_boot_sources),
        )

    def test_arranges_for_update_always_when_empty(self):
        self.patch(signals.bootsources, "post_commit_do")
        # Create then delete a boot source cache to get over initial ignore
        # on create.
        boot_source = factory.make_BootSource(
            keyring_data=factory.make_bytes()
        )
        boot_source.delete()
        post_commit_do = self.patch(signals.bootsources, "post_commit_do")
        factory.make_BootSource(keyring_data=factory.make_bytes())
        self.assertThat(
            post_commit_do,
            MockCalledOnceWith(reactor.callLater, 0, cache_boot_sources),
        )

    def test_arranges_for_update_on_BootSource_update(self):
        self.patch(signals.bootsources, "post_commit_do")
        factory.make_BootSource(keyring_data=factory.make_bytes())
        boot_source = factory.make_BootSource(
            keyring_data=factory.make_bytes()
        )
        post_commit_do = self.patch(signals.bootsources, "post_commit_do")
        boot_source.keyring_data = factory.make_bytes()
        boot_source.save()
        self.assertThat(
            post_commit_do,
            MockCalledOnceWith(reactor.callLater, 0, cache_boot_sources),
        )

    def test_arranges_for_update_on_BootSource_delete(self):
        self.patch(signals.bootsources, "post_commit_do")
        factory.make_BootSource(keyring_data=factory.make_bytes())
        boot_source = factory.make_BootSource(
            keyring_data=factory.make_bytes()
        )
        post_commit_do = self.patch(signals.bootsources, "post_commit_do")
        boot_source.delete()
        self.assertThat(
            post_commit_do,
            MockCalledOnceWith(reactor.callLater, 0, cache_boot_sources),
        )

    def test_arranges_for_update_on_Config_http_proxy(self):
        post_commit_do = self.patch(signals.bootsources, "post_commit_do")
        Config.objects.set_config("http_proxy", factory.make_url())
        self.assertThat(
            post_commit_do,
            MockCalledOnceWith(reactor.callLater, 0, cache_boot_sources),
        )

    def test_arranges_for_update_on_Config_http_proxy_enable(self):
        post_commit_do = self.patch(signals.bootsources, "post_commit_do")
        Config.objects.set_config("enable_http_proxy", False)
        self.assertThat(
            post_commit_do,
            MockCalledOnceWith(reactor.callLater, 0, cache_boot_sources),
        )
