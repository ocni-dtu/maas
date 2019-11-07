# Copyright 2014-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test maasserver.bootsources."""

__all__ = []

import html
from os import environ
import random
from unittest import skip
from unittest.mock import ANY, MagicMock

from maasserver import bootsources
from maasserver.bootsources import (
    cache_boot_sources,
    ensure_boot_source_definition,
    get_boot_sources,
    get_os_info_from_boot_sources,
    get_product_title,
)
from maasserver.components import (
    get_persistent_error,
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
from maasserver.models.signals.bootsources import (
    signals as bootsources_signals,
)
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.tests.test_bootresources import SimplestreamsEnvFixture
from maasserver.utils import get_maas_user_agent
from maastesting.djangotestcase import count_queries
from maastesting.matchers import MockCalledOnceWith
from provisioningserver.config import DEFAULT_IMAGES_URL
from provisioningserver.import_images import (
    download_descriptions as download_descriptions_module,
)
from provisioningserver.import_images.boot_image_mapping import (
    BootImageMapping,
)
from provisioningserver.import_images.helpers import ImageSpec
from requests.exceptions import ConnectionError
from testtools.matchers import HasLength


def patch_and_capture_env_for_download_all_image_descriptions(testcase):
    class CaptureEnv:
        """Fake function; records a copy of the environment."""

        def __call__(self, *args, **kwargs):
            self.args = args
            self.env = environ.copy()
            return MagicMock()

    capture = testcase.patch(
        bootsources, "download_all_image_descriptions", CaptureEnv()
    )
    return capture


def make_image_spec(
    os=None, arch=None, subarch=None, kflavor=None, release=None, label=None
):
    if os is None:
        os = factory.make_name("os")
    if arch is None:
        arch = factory.make_name("arch")
    if subarch is None:
        subarch = factory.make_name("subarch")
    if kflavor is None:
        kflavor = factory.make_name("kflavor")
    if release is None:
        release = factory.make_name("release")
    if label is None:
        label = factory.make_name("label")
    return ImageSpec(os, arch, subarch, kflavor, release, label)


def make_boot_image_mapping(image_specs=None):
    mapping = BootImageMapping()
    if image_specs is not None:
        for image_spec in image_specs:
            mapping.setdefault(image_spec, {})
    return mapping


class TestHelpers(MAASServerTestCase):
    def setUp(self):
        super(TestHelpers, self).setUp()
        # Disable boot source cache signals.
        self.addCleanup(bootsources_signals.enable)
        bootsources_signals.disable()

    def test_ensure_boot_source_definition_creates_default_source(self):
        BootSource.objects.all().delete()
        arch = factory.make_name("arch")
        architecture = "%s/%s" % (arch, factory.make_name("subarch"))
        mock_get_architecture = self.patch(bootsources, "get_architecture")
        mock_get_architecture.return_value = architecture
        created = ensure_boot_source_definition()
        self.assertTrue(
            created,
            "Should have returned True signaling that the "
            "sources where added.",
        )
        sources = BootSource.objects.all()
        self.assertThat(sources, HasLength(1))
        [source] = sources
        self.assertAttributes(
            source,
            {
                "url": DEFAULT_IMAGES_URL,
                "keyring_filename": (
                    "/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg"
                ),
            },
        )
        selections = BootSourceSelection.objects.filter(boot_source=source)
        by_release = {selection.release: selection for selection in selections}
        self.assertItemsEqual(["bionic"], by_release.keys())
        self.assertAttributes(
            by_release["bionic"],
            {
                "release": "bionic",
                "arches": [arch, "amd64"],
                "subarches": ["*"],
                "labels": ["*"],
            },
        )

    def test_ensure_boot_source_definition_creates_with_default_arch(self):
        BootSource.objects.all().delete()
        mock_get_architecture = self.patch(bootsources, "get_architecture")
        mock_get_architecture.return_value = ""
        created = ensure_boot_source_definition()
        self.assertTrue(
            created,
            "Should have returned True signaling that the "
            "sources where added.",
        )
        sources = BootSource.objects.all()
        self.assertThat(sources, HasLength(1))
        [source] = sources
        self.assertAttributes(
            source,
            {
                "url": DEFAULT_IMAGES_URL,
                "keyring_filename": (
                    "/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg"
                ),
            },
        )
        selections = BootSourceSelection.objects.filter(boot_source=source)
        by_release = {selection.release: selection for selection in selections}
        self.assertItemsEqual(["bionic"], by_release.keys())
        self.assertAttributes(
            by_release["bionic"],
            {
                "release": "bionic",
                "arches": ["amd64"],
                "subarches": ["*"],
                "labels": ["*"],
            },
        )

    def test_ensure_boot_source_definition_skips_if_already_present(self):
        sources = [factory.make_BootSource() for _ in range(3)]
        created = ensure_boot_source_definition()
        self.assertFalse(
            created,
            "Should have returned False signaling that the "
            "sources where not added.",
        )
        self.assertItemsEqual(sources, BootSource.objects.all())

    def test_get_boot_sources(self):
        sources = [
            factory.make_BootSource(keyring_data="data").to_dict()
            for _ in range(3)
        ]
        self.assertItemsEqual(sources, get_boot_sources())

    def test_get_product_title_with_os_release_and_gadget_titles(self):
        os_title = factory.make_name("os_title")
        release_title = factory.make_name("release_title")
        gadget_title = factory.make_name("gadget_title")
        self.assertEquals(
            "%s %s %s" % (os_title, release_title, gadget_title),
            get_product_title(
                {
                    "os_title": os_title,
                    "release_title": release_title,
                    "gadget_title": gadget_title,
                }
            ),
        )

    def test_get_product_title_with_os_and_release_titles(self):
        os_title = factory.make_name("os_title")
        release_title = factory.make_name("release_title")
        self.assertEquals(
            "%s %s" % (os_title, release_title),
            get_product_title(
                {"os_title": os_title, "release_title": release_title}
            ),
        )

    def test_get_product_title_without_titles(self):
        self.assertIsNone(get_product_title({}))


class TestGetOSInfoFromBootSources(MAASServerTestCase):
    def setUp(self):
        super(TestGetOSInfoFromBootSources, self).setUp()
        # Disable boot source cache signals.
        self.addCleanup(bootsources_signals.enable)
        bootsources_signals.disable()

    def test__returns_empty_sources_and_sets_when_cache_empty(self):
        self.assertEqual(
            ([], set(), set()),
            get_os_info_from_boot_sources(factory.make_name("os")),
        )

    def test__returns_empty_sources_and_sets_when_no_os(self):
        factory.make_BootSourceCache()
        self.assertEqual(
            ([], set(), set()),
            get_os_info_from_boot_sources(factory.make_name("os")),
        )

    def test__returns_sources_and_sets_of_releases_and_architectures(self):
        os = factory.make_name("os")
        sources = [
            factory.make_BootSource(keyring_data="1234") for _ in range(2)
        ]
        releases = set()
        arches = set()
        for source in sources:
            for _ in range(3):
                release = factory.make_name("release")
                arch = factory.make_name("arch")
                factory.make_BootSourceCache(
                    source, os=os, release=release, arch=arch
                )
                releases.add(release)
                arches.add(arch)
        self.assertEqual(
            (sources, releases, arches), get_os_info_from_boot_sources(os)
        )


class TestPrivateUpdateCache(MAASServerTestCase):
    def setUp(self):
        super(TestPrivateUpdateCache, self).setUp()
        self.useFixture(SimplestreamsEnvFixture())
        # Disable boot source cache signals.
        self.addCleanup(bootsources_signals.enable)
        bootsources_signals.disable()

    def make_release(self, image_mapping):
        os = factory.make_name("os")
        release = factory.make_name("release")
        release_codename = factory.make_name("codename")
        release_title = factory.make_name("title")
        support_eol = factory.make_date().strftime("%Y-%m-%d")
        image_spec = make_image_spec(os=os, release=release)
        image_mapping.setdefault(
            image_spec,
            {
                "release_codename": release_codename,
                "release_title": release_title,
                "support_eol": support_eol,
            },
        )

    def test__adds_release_to_cache(self):
        source = factory.make_BootSource(keyring_data=b"1234")
        os = factory.make_name("os")
        release = factory.make_name("release")
        release_codename = factory.make_name("codename")
        release_title = factory.make_name("title")
        support_eol = factory.make_date().strftime("%Y-%m-%d")
        image_spec = make_image_spec(os=os, release=release)
        image_mapping = BootImageMapping()
        image_mapping.setdefault(
            image_spec,
            {
                "release_codename": release_codename,
                "release_title": release_title,
                "support_eol": support_eol,
            },
        )
        bootsources._update_cache(
            source.to_dict_without_selections(), image_mapping
        )
        cached = BootSourceCache.objects.filter(boot_source=source).first()
        self.assertEqual(release_codename, cached.release_codename)
        self.assertEqual(release_title, cached.release_title)
        self.assertEqual(support_eol, cached.support_eol.strftime("%Y-%m-%d"))

    def test__consistent_query_count(self):
        source = factory.make_BootSource(keyring_data=b"1234")
        image_mapping = BootImageMapping()
        for _ in range(random.randint(20, 50)):
            self.make_release(image_mapping)
        # Add all the items to the cache, always 5.
        queries, _ = count_queries(
            bootsources._update_cache,
            source.to_dict_without_selections(),
            image_mapping,
        )
        self.assertEquals(5, queries)

        # Now that they all already exist, it should only be 4 queries.
        queries, _ = count_queries(
            bootsources._update_cache,
            source.to_dict_without_selections(),
            image_mapping,
        )
        self.assertEquals(4, queries)

        # Do it again just to be sure.
        queries, _ = count_queries(
            bootsources._update_cache,
            source.to_dict_without_selections(),
            image_mapping,
        )
        self.assertEquals(4, queries)


class TestPrivateCacheBootSources(MAASTransactionServerTestCase):
    def setUp(self):
        super(TestPrivateCacheBootSources, self).setUp()
        self.useFixture(SimplestreamsEnvFixture())
        # Disable boot source cache signals.
        self.addCleanup(bootsources_signals.enable)
        bootsources_signals.disable()

    def test__has_env_GNUPGHOME_set(self):
        capture = patch_and_capture_env_for_download_all_image_descriptions(
            self
        )
        factory.make_BootSource(keyring_data=b"1234")
        cache_boot_sources()
        self.assertEqual(
            bootsources.get_maas_user_gpghome(), capture.env["GNUPGHOME"]
        )

    def test__has_env_http_and_https_proxy_set(self):
        proxy_address = factory.make_name("proxy")
        Config.objects.set_config("http_proxy", proxy_address)
        capture = patch_and_capture_env_for_download_all_image_descriptions(
            self
        )
        factory.make_BootSource(keyring_data=b"1234")
        cache_boot_sources()
        self.assertEqual(
            (proxy_address, proxy_address, "127.0.0.1,localhost"),
            (
                capture.env["http_proxy"],
                capture.env["https_proxy"],
                capture.env["no_proxy"],
            ),
        )

    def test__has_env_http_and_https_proxy_set_with_custom_no_proxy(self):
        proxy_address = factory.make_name("proxy")
        Config.objects.set_config("http_proxy", proxy_address)
        Config.objects.set_config("boot_images_no_proxy", True)
        capture = patch_and_capture_env_for_download_all_image_descriptions(
            self
        )
        factory.make_BootSource(
            keyring_data=b"1234",
            url=b"http://192.168.1.100:8080/ephemeral-v3/",
        )
        cache_boot_sources()
        no_proxy_hosts = "127.0.0.1,localhost,192.168.1.100"
        self.assertEqual(
            (proxy_address, proxy_address, no_proxy_hosts),
            (
                capture.env["http_proxy"],
                capture.env["https_proxy"],
                capture.env["no_proxy"],
            ),
        )

    def test__passes_user_agent_with_maas_version(self):
        mock_download = self.patch(
            bootsources, "download_all_image_descriptions"
        )
        factory.make_BootSource(keyring_data=b"1234")
        cache_boot_sources()
        self.assertThat(
            mock_download,
            MockCalledOnceWith(ANY, user_agent=get_maas_user_agent()),
        )

    @skip("XXX: GavinPanella 2015-12-04 bug=1546235: Fails spuriously.")
    def test__doesnt_have_env_http_and_https_proxy_set_if_disabled(self):
        proxy_address = factory.make_name("proxy")
        Config.objects.set_config("http_proxy", proxy_address)
        Config.objects.set_config("enable_http_proxy", False)
        capture = patch_and_capture_env_for_download_all_image_descriptions(
            self
        )
        factory.make_BootSource(keyring_data=b"1234")
        cache_boot_sources()
        self.assertEqual(
            ("", ""),
            (
                capture.env.get("http_proxy", ""),
                capture.env.get("https_proxy", ""),
            ),
        )

    def test__returns_clears_entire_cache(self):
        source = factory.make_BootSource(keyring_data=b"1234")
        factory.make_BootSourceCache(source)
        mock_download = self.patch(
            bootsources, "download_all_image_descriptions"
        )
        mock_download.return_value = make_boot_image_mapping()
        cache_boot_sources()
        self.assertEqual(0, BootSourceCache.objects.all().count())

    def test__returns_adds_entries_to_cache_for_source(self):
        source = factory.make_BootSource(keyring_data=b"1234")
        os = factory.make_name("os")
        releases = [factory.make_name("release") for _ in range(3)]
        image_specs = [
            make_image_spec(os=os, release=release) for release in releases
        ]
        mock_download = self.patch(
            bootsources, "download_all_image_descriptions"
        )
        mock_download.return_value = make_boot_image_mapping(image_specs)

        cache_boot_sources()
        cached_releases = [
            cache.release
            for cache in BootSourceCache.objects.filter(boot_source=source)
            if cache.os == os
        ]
        self.assertItemsEqual(releases, cached_releases)

    def test__adds_release_codename_title_and_support_eol(self):
        source = factory.make_BootSource(keyring_data=b"1234")
        os = factory.make_name("os")
        release = factory.make_name("release")
        release_codename = factory.make_name("codename")
        release_title = factory.make_name("title")
        support_eol = factory.make_date().strftime("%Y-%m-%d")
        image_spec = make_image_spec(os=os, release=release)
        image_mapping = BootImageMapping()
        image_mapping.setdefault(
            image_spec,
            {
                "release_codename": release_codename,
                "release_title": release_title,
                "support_eol": support_eol,
            },
        )
        mock_download = self.patch(
            bootsources, "download_all_image_descriptions"
        )
        mock_download.return_value = image_mapping

        cache_boot_sources()
        cached = BootSourceCache.objects.filter(boot_source=source).first()
        self.assertEqual(release_codename, cached.release_codename)
        self.assertEqual(release_title, cached.release_title)
        self.assertEqual(support_eol, cached.support_eol.strftime("%Y-%m-%d"))

    def test__adds_title_to_extra(self):
        source = factory.make_BootSource(keyring_data=b"1234")
        os = factory.make_name("os")
        release = factory.make_name("release")
        os_title = factory.make_name("os_title")
        release_title = factory.make_name("release_title")
        image_spec = make_image_spec(os=os, release=release)
        image_mapping = BootImageMapping()
        image_mapping.setdefault(
            image_spec, {"os_title": os_title, "release_title": release_title}
        )
        mock_download = self.patch(
            bootsources, "download_all_image_descriptions"
        )
        mock_download.return_value = image_mapping

        cache_boot_sources()
        cached = BootSourceCache.objects.filter(boot_source=source).first()
        self.assertDictEqual(
            {"title": "%s %s" % (os_title, release_title)}, cached.extra
        )

    def test__adds_title_with_gadget_to_extra(self):
        source = factory.make_BootSource(keyring_data=b"1234")
        os = factory.make_name("os")
        release = factory.make_name("release")
        os_title = factory.make_name("os_title")
        release_title = factory.make_name("release_title")
        gadget_title = factory.make_name("gadget_title")
        image_spec = make_image_spec(os=os, release=release)
        image_mapping = BootImageMapping()
        image_mapping.setdefault(
            image_spec,
            {
                "os_title": os_title,
                "release_title": release_title,
                "gadget_title": gadget_title,
            },
        )
        mock_download = self.patch(
            bootsources, "download_all_image_descriptions"
        )
        mock_download.return_value = image_mapping

        cache_boot_sources()
        cached = BootSourceCache.objects.filter(boot_source=source).first()
        self.assertDictEqual(
            {"title": "%s %s %s" % (os_title, release_title, gadget_title)},
            cached.extra,
        )

    def test__notifies_missing_commissioning_os(self):
        cache_boot_sources()
        self.assertTrue(
            Notification.objects.filter(
                ident="commissioning_series_unselected"
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                ident="commissioning_series_unavailable"
            ).exists()
        )


class TestBadConnectionHandling(MAASTransactionServerTestCase):
    def setUp(self):
        super(TestBadConnectionHandling, self).setUp()
        self.useFixture(SimplestreamsEnvFixture())
        # Disable boot source cache signals.
        self.addCleanup(bootsources_signals.enable)
        bootsources_signals.disable()

    def test__catches_connection_errors_and_sets_component_error(self):
        sources = [
            factory.make_BootSource(keyring_data=b"1234") for _ in range(3)
        ]
        download_image_descriptions = self.patch(
            download_descriptions_module, "download_image_descriptions"
        )
        error_text_one = factory.make_name("<error1>")
        error_text_two = factory.make_name("<error2>")
        # Make two of the downloads fail.
        download_image_descriptions.side_effect = [
            ConnectionError(error_text_one),
            BootImageMapping(),
            IOError(error_text_two),
        ]
        cache_boot_sources()
        base_error = "Failed to import images from {url}: {err}"
        error_part_one = base_error.format(
            url=sources[0].url, err=html.escape(error_text_one)
        )
        error_part_two = base_error.format(
            url=sources[2].url, err=html.escape(error_text_two)
        )
        expected_error = error_part_one + "<br>" + error_part_two
        actual_error = get_persistent_error(COMPONENT.REGION_IMAGE_IMPORT)
        self.assertEqual(expected_error, actual_error)

    def test__clears_component_error_when_successful(self):
        register_persistent_error(
            COMPONENT.REGION_IMAGE_IMPORT, factory.make_string()
        )
        [factory.make_BootSource(keyring_data=b"1234") for _ in range(3)]
        download_image_descriptions = self.patch(
            download_descriptions_module, "download_image_descriptions"
        )
        # Make all of the downloads successful.
        download_image_descriptions.return_value = BootImageMapping()
        cache_boot_sources()
        self.assertIsNone(get_persistent_error(COMPONENT.REGION_IMAGE_IMPORT))
