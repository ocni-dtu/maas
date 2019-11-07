# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test maasserver.bootresources."""

__all__ = []

from datetime import datetime
from email.utils import format_datetime
import http.client
from io import BytesIO
import json
import logging
import os
from os import environ
import random
from random import randint
from subprocess import CalledProcessError
from unittest import skip
from unittest.mock import ANY, call, MagicMock, Mock, sentinel
from urllib.parse import urljoin

from crochet import wait_for
from django.conf import settings
from django.db import connections, transaction
from django.http import StreamingHttpResponse
from fixtures import FakeLogger, Fixture
from maasserver import __version__, bootresources
from maasserver.bootresources import (
    BootResourceRepoWriter,
    BootResourceStore,
    download_all_boot_resources,
    download_boot_resources,
    get_simplestream_endpoint,
    set_global_default_releases,
    SimpleStreamsHandler,
)
from maasserver.clusterrpc.testing.boot_images import make_rpc_boot_image
from maasserver.components import (
    get_persistent_error,
    register_persistent_error,
)
from maasserver.enum import (
    BOOT_RESOURCE_FILE_TYPE,
    BOOT_RESOURCE_FILE_TYPE_CHOICES,
    BOOT_RESOURCE_TYPE,
    COMPONENT,
)
from maasserver.listener import PostgresListenerService
from maasserver.models import (
    BootResource,
    BootResourceFile,
    BootResourceSet,
    BootSource,
    Config,
    LargeFile,
    signals,
)
from maasserver.models.signals.testing import SignalsDisabled
from maasserver.rpc.testing.fixtures import MockLiveRegionToClusterRPCFixture
from maasserver.testing.config import RegionConfigurationFixture
from maasserver.testing.dblocks import lock_held_in_other_thread
from maasserver.testing.eventloop import (
    RegionEventLoopFixture,
    RunningEventLoopFixture,
)
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.testing.testclient import MAASSensibleClient
from maasserver.utils import absolute_reverse, get_maas_user_agent
from maasserver.utils.django_urls import reverse
from maasserver.utils.orm import (
    get_one,
    post_commit_hooks,
    reload_object,
    transactional,
)
from maasserver.utils.threads import deferToDatabase
from maastesting.matchers import (
    MockCalledOnce,
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase
from maastesting.twisted import extract_result, TwistedLoggerFixture
from provisioningserver.auth import get_maas_user_gpghome
from provisioningserver.import_images.product_mapping import ProductMapping
from provisioningserver.rpc.cluster import ListBootImages, ListBootImagesV2
from provisioningserver.utils.text import normalise_whitespace
from provisioningserver.utils.twisted import asynchronous, DeferredValue
from testtools.matchers import Contains, ContainsAll, Equals, HasLength, Not
from twisted.application.internet import TimerService
from twisted.internet.defer import Deferred, fail, inlineCallbacks, succeed
from twisted.protocols.amp import UnhandledCommand


wait_for_reactor = wait_for(30)  # 30 seconds.


def make_boot_resource_file_with_stream(size=None):
    resource = factory.make_usable_boot_resource(
        rtype=BOOT_RESOURCE_TYPE.SYNCED, size=size
    )
    rfile = resource.sets.first().files.first()
    with rfile.largefile.content.open("rb") as stream:
        content = stream.read()
    with rfile.largefile.content.open("wb") as stream:
        stream.truncate()
    return rfile, BytesIO(content), content


class TestHelpers(MAASServerTestCase):
    """Tests for `maasserver.bootresources` helpers."""

    def test_get_simplestreams_endpoint(self):
        endpoint = get_simplestream_endpoint()
        self.assertEqual(
            absolute_reverse(
                "simplestreams_stream_handler",
                kwargs={"filename": "index.json"},
            ),
            endpoint["url"],
        )
        self.assertEqual([], endpoint["selections"])


class SimplestreamsEnvFixture(Fixture):
    """Clears the env variables set by the methods that interact with
    simplestreams."""

    def setUp(self):
        super(SimplestreamsEnvFixture, self).setUp()
        prior_env = {}
        for key in ["GNUPGHOME", "http_proxy", "https_proxy"]:
            prior_env[key] = os.environ.get(key, "")
        self.addCleanup(os.environ.update, prior_env)


class TestSimpleStreamsHandler(MAASServerTestCase):
    """Tests for `maasserver.bootresources.SimpleStreamsHandler`."""

    def reverse_stream_handler(self, filename):
        return reverse(
            "simplestreams_stream_handler", kwargs={"filename": filename}
        )

    def reverse_file_handler(
        self, os, arch, subarch, series, version, filename
    ):
        return reverse(
            "simplestreams_file_handler",
            kwargs={
                "os": os,
                "arch": arch,
                "subarch": subarch,
                "series": series,
                "version": version,
                "filename": filename,
            },
        )

    def get_stream_client(self, filename):
        return self.client.get(self.reverse_stream_handler(filename))

    def get_file_client(self, os, arch, subarch, series, version, filename):
        return self.client.get(
            self.reverse_file_handler(
                os, arch, subarch, series, version, filename
            )
        )

    def get_product_name_for_resource(self, resource):
        arch, subarch = resource.architecture.split("/")
        if "/" in resource.name:
            os, series = resource.name.split("/")
        else:
            os = "custom"
            series = resource.name
        return "maas:boot:%s:%s:%s:%s" % (os, arch, subarch, series)

    def make_usable_product_boot_resource(
        self, kflavor=None, bootloader_type=None, rolling=False
    ):
        resource = factory.make_usable_boot_resource(
            kflavor=kflavor, bootloader_type=bootloader_type, rolling=rolling
        )
        return self.get_product_name_for_resource(resource), resource

    def test_streams_other_than_allowed_returns_404(self):
        allowed_paths = ["index.json", "maas:v2:download.json"]
        invalid_paths = [
            "%s.json" % factory.make_name("path") for _ in range(3)
        ]
        for path in allowed_paths:
            response = self.get_stream_client(path)
            self.assertEqual(http.client.OK, response.status_code)
        for path in invalid_paths:
            response = self.get_stream_client(path)
            self.assertEqual(http.client.NOT_FOUND, response.status_code)

    def test_streams_product_index_contains_keys(self):
        response = self.get_stream_client("index.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertThat(output, ContainsAll(["index", "updated", "format"]))

    def test_streams_product_index_format_is_index_1(self):
        response = self.get_stream_client("index.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertEqual("index:1.0", output["format"])

    def test_streams_product_index_index_has_maas_v2_download(self):
        response = self.get_stream_client("index.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertThat(output["index"], ContainsAll(["maas:v2:download"]))

    def test_streams_product_index_maas_v2_download_contains_keys(self):
        response = self.get_stream_client("index.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertThat(
            output["index"]["maas:v2:download"],
            ContainsAll(["datatype", "path", "updated", "products", "format"]),
        )

    def test_streams_product_index_maas_v2_download_has_valid_values(self):
        response = self.get_stream_client("index.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertEqual(
            "image-downloads", output["index"]["maas:v2:download"]["datatype"]
        )
        self.assertEqual(
            "streams/v1/maas:v2:download.json",
            output["index"]["maas:v2:download"]["path"],
        )
        self.assertEqual(
            "products:1.0", output["index"]["maas:v2:download"]["format"]
        )

    def test_streams_product_index_empty_products(self):
        response = self.get_stream_client("index.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertEqual([], output["index"]["maas:v2:download"]["products"])

    def test_streams_product_index_empty_with_incomplete_resource(self):
        resource = factory.make_BootResource()
        factory.make_BootResourceSet(resource)
        response = self.get_stream_client("index.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertEqual([], output["index"]["maas:v2:download"]["products"])

    def test_streams_product_index_with_resources(self):
        products = []
        for _ in range(3):
            product, _ = self.make_usable_product_boot_resource()
            products.append(product)
        response = self.get_stream_client("index.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        # Product listing should be the same as all of the completed
        # boot resources in the database.
        self.assertItemsEqual(
            products, output["index"]["maas:v2:download"]["products"]
        )

    def test_streams_product_download_contains_keys(self):
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertThat(
            output,
            ContainsAll(
                ["datatype", "updated", "content_id", "products", "format"]
            ),
        )

    def test_streams_product_download_has_valid_values(self):
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertEqual("image-downloads", output["datatype"])
        self.assertEqual("maas:v2:download", output["content_id"])
        self.assertEqual("products:1.0", output["format"])

    def test_streams_product_download_empty_products(self):
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertEqual({}, output["products"])

    def test_streams_product_download_empty_with_incomplete_resource(self):
        resource = factory.make_BootResource()
        factory.make_BootResourceSet(resource)
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertEqual({}, output["products"])

    def test_streams_product_download_has_valid_product_keys(self):
        products = []
        for _ in range(3):
            product, _ = self.make_usable_product_boot_resource()
            products.append(product)
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        # Product listing should be the same as all of the completed
        # boot resources in the database.
        self.assertThat(output["products"], ContainsAll(products))

    def test_streams_product_download_product_contains_keys(self):
        product, _ = self.make_usable_product_boot_resource()
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertThat(
            output["products"][product],
            ContainsAll(
                [
                    "versions",
                    "subarch",
                    "label",
                    "version",
                    "arch",
                    "release",
                    "os",
                ]
            ),
        )
        # Verify optional fields aren't added
        self.assertThat(
            output["products"][product],
            Not(ContainsAll(["kflavor", "bootloader_type"])),
        )

    def test_streams_product_download_product_adds_optional_fields(self):
        kflavor = factory.make_name("kflavor")
        bootloader_type = factory.make_name("bootloader_type")
        product, _ = self.make_usable_product_boot_resource(
            kflavor, bootloader_type, True
        )
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertEquals(kflavor, output["products"][product]["kflavor"])
        self.assertEquals(
            bootloader_type, output["products"][product]["bootloader-type"]
        )
        self.assertTrue(output["products"][product]["rolling"])

    def test_streams_product_download_product_has_valid_values(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(":")
        label = resource.get_latest_complete_set().label
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        output_product = output["products"][product]
        self.assertEqual(subarch, output_product["subarch"])
        self.assertEqual(label, output_product["label"])
        self.assertEqual(series, output_product["version"])
        self.assertEqual(arch, output_product["arch"])
        self.assertEqual(series, output_product["release"])
        self.assertEqual(os, output_product["os"])
        for key, value in resource.extra.items():
            self.assertEqual(value, output_product[key])

    def test_streams_product_download_product_uses_latest_complete_label(self):
        product, resource = self.make_usable_product_boot_resource()
        # Incomplete resource_set
        factory.make_BootResourceSet(resource)
        newest_set = factory.make_BootResourceSet(resource)
        factory.make_boot_resource_file_with_content(newest_set)
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        output_product = output["products"][product]
        self.assertEqual(newest_set.label, output_product["label"])

    def test_streams_product_download_product_contains_multiple_versions(self):
        resource = factory.make_BootResource()
        resource_sets = [
            factory.make_BootResourceSet(resource) for _ in range(3)
        ]
        versions = []
        for resource_set in resource_sets:
            factory.make_boot_resource_file_with_content(resource_set)
            versions.append(resource_set.version)
        product = self.get_product_name_for_resource(resource)
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        self.assertThat(
            output["products"][product]["versions"], ContainsAll(versions)
        )

    def test_streams_product_download_product_version_contains_items(self):
        product, resource = self.make_usable_product_boot_resource()
        resource_set = resource.get_latest_complete_set()
        items = [rfile.filename for rfile in resource_set.files.all()]
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        version = output["products"][product]["versions"][resource_set.version]
        self.assertThat(version["items"], ContainsAll(items))

    def test_streams_product_download_product_item_contains_keys(self):
        product, resource = self.make_usable_product_boot_resource()
        resource_set = resource.get_latest_complete_set()
        resource_file = resource_set.files.order_by("?")[0]
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        version = output["products"][product]["versions"][resource_set.version]
        self.assertThat(
            version["items"][resource_file.filename],
            ContainsAll(["path", "ftype", "sha256", "size"]),
        )

    def test_streams_product_download_product_item_has_valid_values(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(":")
        resource_set = resource.get_latest_complete_set()
        resource_file = resource_set.files.order_by("?")[0]
        path = "%s/%s/%s/%s/%s/%s" % (
            os,
            arch,
            subarch,
            series,
            resource_set.version,
            resource_file.filename,
        )
        response = self.get_stream_client("maas:v2:download.json")
        output = json.loads(response.content.decode(settings.DEFAULT_CHARSET))
        version = output["products"][product]["versions"][resource_set.version]
        item = version["items"][resource_file.filename]
        self.assertEqual(path, item["path"])
        self.assertEqual(resource_file.filetype, item["ftype"])
        self.assertEqual(resource_file.largefile.sha256, item["sha256"])
        self.assertEqual(resource_file.largefile.total_size, item["size"])
        for key, value in resource_file.extra.items():
            self.assertEqual(value, item[key])

    def test_download_invalid_boot_resource_returns_404(self):
        os = factory.make_name("os")
        series = factory.make_name("series")
        arch = factory.make_name("arch")
        subarch = factory.make_name("subarch")
        version = factory.make_name("version")
        filename = factory.make_name("filename")
        response = self.get_file_client(
            os, arch, subarch, series, version, filename
        )
        self.assertEqual(http.client.NOT_FOUND, response.status_code)

    def test_download_invalid_version_returns_404(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(":")
        version = factory.make_name("version")
        filename = factory.make_name("filename")
        response = self.get_file_client(
            os, arch, subarch, series, version, filename
        )
        self.assertEqual(http.client.NOT_FOUND, response.status_code)

    def test_download_invalid_filename_returns_404(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(":")
        resource_set = resource.get_latest_complete_set()
        version = resource_set.version
        filename = factory.make_name("filename")
        response = self.get_file_client(
            os, arch, subarch, series, version, filename
        )
        self.assertEqual(http.client.NOT_FOUND, response.status_code)

    def test_download_valid_path_returns_200(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(":")
        resource_set = resource.get_latest_complete_set()
        version = resource_set.version
        resource_file = resource_set.files.order_by("?")[0]
        filename = resource_file.filename
        response = self.get_file_client(
            os, arch, subarch, series, version, filename
        )
        self.assertEqual(http.client.OK, response.status_code)

    def test_download_returns_streaming_response(self):
        product, resource = self.make_usable_product_boot_resource()
        _, _, os, arch, subarch, series = product.split(":")
        resource_set = resource.get_latest_complete_set()
        version = resource_set.version
        resource_file = resource_set.files.order_by("?")[0]
        filename = resource_file.filename
        response = self.get_file_client(
            os, arch, subarch, series, version, filename
        )
        self.assertIsInstance(response, StreamingHttpResponse)


class TestConnectionWrapper(MAASTransactionServerTestCase):
    """Tests the use of StreamingHttpResponse(ConnectionWrapper(stream)).

    We do not run this inside of `MAASServerTestCase` as that wraps a
    transaction around each test. Since a new connection is created to return
    the actual content, the transaction to create the data needs be committed.
    """

    def make_file_for_client(self):
        # Set up the database information inside of a transaction. This is
        # done so the information is committed. As the new connection needs
        # to be able to access the data.
        with transaction.atomic():
            os = factory.make_name("os")
            series = factory.make_name("series")
            arch = factory.make_name("arch")
            subarch = factory.make_name("subarch")
            name = "%s/%s" % (os, series)
            architecture = "%s/%s" % (arch, subarch)
            version = factory.make_name("version")
            filetype = factory.pick_enum(BOOT_RESOURCE_FILE_TYPE)
            # We set the filename to the same value as filetype, as in most
            # cases this will always be true. The simplestreams content from
            # maas.io, is formatted this way.
            filename = filetype
            size = randint(1024, 2048)
            content = factory.make_bytes(size=size)
            resource = factory.make_BootResource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED,
                name=name,
                architecture=architecture,
            )
            resource_set = factory.make_BootResourceSet(
                resource, version=version
            )
            largefile = factory.make_LargeFile(content=content, size=size)
            factory.make_BootResourceFile(
                resource_set, largefile, filename=filename, filetype=filetype
            )
        return (
            content,
            reverse(
                "simplestreams_file_handler",
                kwargs={
                    "os": os,
                    "arch": arch,
                    "subarch": subarch,
                    "series": series,
                    "version": version,
                    "filename": filename,
                },
            ),
        )

    def read_response(self, response):
        """Read the streaming_content from the response.

        :rtype: bytes
        """
        return b"".join(response.streaming_content)

    def test_download_calls__get_new_connection(self):
        content, url = self.make_file_for_client()
        mock_get_new_connection = self.patch(
            bootresources.ConnectionWrapper, "_get_new_connection"
        )

        client = MAASSensibleClient()
        response = client.get(url)
        self.read_response(response)
        self.assertThat(mock_get_new_connection, MockCalledOnceWith())

    def test_download_connection_is_not_same_as_django_connections(self):
        content, url = self.make_file_for_client()

        class AssertConnectionWrapper(bootresources.ConnectionWrapper):
            def _set_up(self):
                super(AssertConnectionWrapper, self)._set_up()
                # Capture the created connection
                AssertConnectionWrapper.connection = self._connection

            def close(self):
                # Close the stream, but we don't want to close the
                # connection as the test is testing that the connection is
                # not the same as the connection django is using for other
                # webrequests.
                if self._stream is not None:
                    self._stream.close()
                    self._stream = None
                self._connection = None

        self.patch(bootresources, "ConnectionWrapper", AssertConnectionWrapper)

        client = MAASSensibleClient()
        response = client.get(url)
        self.read_response(response)

        # Add cleanup to close the connection, since this was removed from
        # AssertConnectionWrapper.close method.
        def close():
            conn = AssertConnectionWrapper.connection
            conn.in_atomic_block = False
            conn.commit()
            conn.set_autocommit(True)
            conn.close()

        self.addCleanup(close)

        # The connection that is used by the wrapper cannot be the same as the
        # connection be using for all other webrequests. Without this
        # seperate the transactional middleware will fail to initialize,
        # because the the connection will already be in a transaction.
        #
        # Note: cannot test if DatabaseWrapper != DatabaseWrapper, as it will
        # report true, because the __eq__ operator only checks if the aliases
        # are the same. This is checking the underlying connection is
        # different, which is the important part.
        self.assertNotEqual(
            connections["default"].connection,
            AssertConnectionWrapper.connection.connection,
        )


def make_product(ftype=None, kflavor=None, subarch=None):
    """Make product dictionary that is just like the one provided
    from simplsetreams."""
    if ftype is None:
        ftype = factory.pick_choice(BOOT_RESOURCE_FILE_TYPE_CHOICES)
    if kflavor is None:
        kflavor = "generic"
    if subarch is None:
        subarch = factory.make_name("subarch")
    subarches = [factory.make_name("subarch") for _ in range(3)]
    subarches.insert(0, subarch)
    subarches = ",".join(subarches)
    name = factory.make_name("name")
    product = {
        "os": factory.make_name("os"),
        "arch": factory.make_name("arch"),
        "subarch": subarch,
        "release": factory.make_name("release"),
        "kflavor": kflavor,
        "subarches": subarches,
        "version_name": factory.make_name("version"),
        "label": factory.make_name("label"),
        "ftype": ftype,
        "kpackage": factory.make_name("kpackage"),
        "item_name": name,
        "path": "/path/to/%s" % name,
        "rolling": factory.pick_bool(),
    }
    name = "%s/%s" % (product["os"], product["release"])
    if kflavor == "generic":
        subarch = product["subarch"]
    else:
        subarch = "%s-%s" % (product["subarch"], kflavor)
    architecture = "%s/%s" % (product["arch"], subarch)
    return name, architecture, product


def make_boot_resource_group(
    rtype=None,
    name=None,
    architecture=None,
    version=None,
    filename=None,
    filetype=None,
):
    """Make boot resource that contains one set and one file."""
    resource = factory.make_BootResource(
        rtype=rtype, name=name, architecture=architecture
    )
    resource_set = factory.make_BootResourceSet(resource, version=version)
    rfile = factory.make_boot_resource_file_with_content(
        resource_set, filename=filename, filetype=filetype
    )
    return resource, resource_set, rfile


def make_boot_resource_group_from_product(product):
    """Make boot resource that contains one set and one file, using the
    information from the given product.

    The product dictionary is also updated to include the sha256 and size
    for the created largefile. The calling function should use the returned
    product in place of the passed product.
    """
    name = "%s/%s" % (product["os"], product["release"])
    architecture = "%s/%s" % (product["arch"], product["subarch"])
    resource = factory.make_BootResource(
        rtype=BOOT_RESOURCE_TYPE.SYNCED, name=name, architecture=architecture
    )
    resource_set = factory.make_BootResourceSet(
        resource, version=product["version_name"]
    )
    rfile = factory.make_boot_resource_file_with_content(
        resource_set, filename=product["item_name"], filetype=product["ftype"]
    )
    product["sha256"] = rfile.largefile.sha256
    product["size"] = rfile.largefile.total_size
    return product, resource


class TestBootResourceStore(MAASServerTestCase):
    def make_boot_resources(self):
        resources = [
            factory.make_BootResource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
            for _ in range(3)
        ]
        resource_names = []
        for resource in resources:
            os, series = resource.name.split("/")
            arch, subarch = resource.split_arch()
            name = "%s/%s/%s/%s" % (os, arch, subarch, series)
            resource_names.append(name)
        return resources, resource_names

    def test_init_initializes_variables(self):
        _, resource_names = self.make_boot_resources()
        store = BootResourceStore()
        self.assertItemsEqual(resource_names, store._resources_to_delete)
        self.assertEqual({}, store._content_to_finalize)

    def test_prevent_resource_deletion_removes_resource(self):
        resources, resource_names = self.make_boot_resources()
        store = BootResourceStore()
        resource = resources.pop()
        resource_names.pop()
        store.prevent_resource_deletion(resource)
        self.assertItemsEqual(resource_names, store._resources_to_delete)

    def test_prevent_resource_deletion_doesnt_remove_unknown_resource(self):
        resources, resource_names = self.make_boot_resources()
        store = BootResourceStore()
        resource = factory.make_BootResource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
        store.prevent_resource_deletion(resource)
        self.assertItemsEqual(resource_names, store._resources_to_delete)

    def test_save_content_later_adds_to__content_to_finalize_var(self):
        _, _, rfile = make_boot_resource_group()
        store = BootResourceStore()
        store.save_content_later(rfile, sentinel.reader)
        self.assertEqual(
            {rfile.id: sentinel.reader}, store._content_to_finalize
        )

    def test_get_or_create_boot_resource_creates_resource(self):
        name, architecture, product = make_product()
        store = BootResourceStore()
        resource = store.get_or_create_boot_resource(product)
        self.assertEqual(BOOT_RESOURCE_TYPE.SYNCED, resource.rtype)
        self.assertEqual(name, resource.name)
        self.assertEqual(architecture, resource.architecture)
        self.assertEqual(product["kflavor"], resource.kflavor)
        self.assertEqual(product["subarches"], resource.extra["subarches"])
        self.assertEqual(product["rolling"], resource.rolling)

    def test_get_or_create_boot_resource_handles_bootloader(self):
        osystem = factory.make_name("os")
        product = {
            "os": osystem,
            "arch": factory.make_name("arch"),
            "bootloader-type": factory.make_name("bootloader-type"),
            "lablel": factory.make_name("label"),
            "ftype": factory.pick_choice(
                [
                    BOOT_RESOURCE_FILE_TYPE.BOOTLOADER,
                    BOOT_RESOURCE_FILE_TYPE.ARCHIVE_TAR_XZ,
                ]
            ),
            "path": "/path/to/%s" % osystem,
            "sha256": factory.make_name("sha256"),
            "src_package": factory.make_name("src_package"),
            "src_release": factory.make_name("src_release"),
            "src_version": factory.make_name("src_version"),
        }
        store = BootResourceStore()
        resource = store.get_or_create_boot_resource(product)
        self.assertEqual(BOOT_RESOURCE_TYPE.SYNCED, resource.rtype)
        self.assertEqual(
            "%s/%s" % (product["os"], product["bootloader-type"]),
            resource.name,
        )
        self.assertEqual("%s/generic" % product["arch"], resource.architecture)
        self.assertEqual(product["bootloader-type"], resource.bootloader_type)

    def test_get_or_create_boot_resource_gets_resource(self):
        name, architecture, product = make_product()
        expected = factory.make_BootResource(
            rtype=BOOT_RESOURCE_TYPE.SYNCED,
            name=name,
            architecture=architecture,
        )
        store = BootResourceStore()
        resource = store.get_or_create_boot_resource(product)
        self.assertEqual(expected, resource)
        self.assertEqual(product["kflavor"], resource.kflavor)
        self.assertEqual(product["subarches"], resource.extra["subarches"])

    def test_get_or_create_boot_resource_calls_prevent_resource_deletion(self):
        name, architecture, product = make_product()
        resource = factory.make_BootResource(
            rtype=BOOT_RESOURCE_TYPE.SYNCED,
            name=name,
            architecture=architecture,
        )
        store = BootResourceStore()
        mock_prevent = self.patch(store, "prevent_resource_deletion")
        store.get_or_create_boot_resource(product)
        self.assertThat(mock_prevent, MockCalledOnceWith(resource))

    def test_get_or_create_boot_resource_converts_generated_into_synced(self):
        name, architecture, product = make_product()
        resource = factory.make_BootResource(
            rtype=BOOT_RESOURCE_TYPE.GENERATED,
            name=name,
            architecture=architecture,
        )
        store = BootResourceStore()
        mock_prevent = self.patch(store, "prevent_resource_deletion")
        store.get_or_create_boot_resource(product)
        self.assertEqual(
            BOOT_RESOURCE_TYPE.SYNCED, reload_object(resource).rtype
        )
        self.assertThat(mock_prevent, MockNotCalled())

    def test_get_or_create_boot_resource_adds_kflavor_to_subarch(self):
        kflavor = factory.make_name("kflavor")
        _, architecture, product = make_product(
            kflavor=kflavor, subarch=random.choice(["hwe-16.04", "ga-16.04"])
        )
        store = BootResourceStore()
        resource = store.get_or_create_boot_resource(product)
        self.assertEqual(architecture, reload_object(resource).architecture)
        self.assertTrue(architecture.endswith(kflavor))

    def test_get_or_create_boot_resources_add_no_kflavor_for_generic(self):
        _, architecture, product = make_product(kflavor="generic")
        store = BootResourceStore()
        resource = store.get_or_create_boot_resource(product)
        resource = reload_object(resource)
        self.assertEqual(architecture, resource.architecture)
        self.assertNotIn("generic", resource.architecture)

    def test_get_or_create_boot_resource_handles_ubuntu_core(self):
        product = {
            "arch": "amd64",
            "gadget_snap": "pc",
            "gadget_title": "PC",
            "kernel_snap": "pc-kernel",
            "label": "daily",
            "maas_supported": "2.2",
            "os": "ubuntu-core",
            "os_title": "Ubuntu Core",
            "release": "16",
            "release_title": "16",
            "item_name": "root-dd.xz",
            "ftype": BOOT_RESOURCE_FILE_TYPE.ROOT_DDXZ,
            "path": "/path/to/root-dd.xz",
        }
        store = BootResourceStore()
        resource = store.get_or_create_boot_resource(product)
        self.assertEquals(BOOT_RESOURCE_TYPE.SYNCED, resource.rtype)
        self.assertEquals("ubuntu-core/16-pc", resource.name)
        self.assertEquals("amd64/generic", resource.architecture)
        self.assertIsNone(resource.bootloader_type)
        self.assertEquals("pc-kernel", resource.kflavor)
        self.assertDictEqual({"title": "Ubuntu Core 16 PC"}, resource.extra)

    def test_get_or_create_boot_resource_set_creates_resource_set(self):
        self.useFixture(SignalsDisabled("largefiles"))
        name, architecture, product = make_product()
        product, resource = make_boot_resource_group_from_product(product)
        with post_commit_hooks:
            resource.sets.all().delete()
        store = BootResourceStore()
        resource_set = store.get_or_create_boot_resource_set(resource, product)
        self.assertEqual(product["version_name"], resource_set.version)
        self.assertEqual(product["label"], resource_set.label)

    def test_get_or_create_boot_resource_set_gets_resource_set(self):
        name, architecture, product = make_product()
        product, resource = make_boot_resource_group_from_product(product)
        expected = resource.sets.first()
        store = BootResourceStore()
        resource_set = store.get_or_create_boot_resource_set(resource, product)
        self.assertEqual(expected, resource_set)
        self.assertEqual(product["label"], resource_set.label)

    def test_get_or_create_boot_resource_file_creates_resource_file(self):
        self.useFixture(SignalsDisabled("largefiles"))
        name, architecture, product = make_product()
        product, resource = make_boot_resource_group_from_product(product)
        resource_set = resource.sets.first()
        with post_commit_hooks:
            resource_set.files.all().delete()
        store = BootResourceStore()
        rfile = store.get_or_create_boot_resource_file(resource_set, product)
        self.assertEqual(os.path.basename(product["path"]), rfile.filename)
        self.assertEqual(product["ftype"], rfile.filetype)
        self.assertEqual(product["kpackage"], rfile.extra["kpackage"])

    def test_get_or_create_boot_resource_file_gets_resource_file(self):
        name, architecture, product = make_product()
        product, resource = make_boot_resource_group_from_product(product)
        resource_set = resource.sets.first()
        expected = resource_set.files.first()
        store = BootResourceStore()
        rfile = store.get_or_create_boot_resource_file(resource_set, product)
        self.assertEqual(expected, rfile)
        self.assertEqual(product["ftype"], rfile.filetype)
        self.assertEqual(product["kpackage"], rfile.extra["kpackage"])

    def test_get_or_create_boot_resource_file_captures_extra_fields(self):
        extra_fields = [
            "kpackage",
            "src_package",
            "src_release",
            "src_version",
        ]
        name, architecture, product = make_product()
        for extra_field in extra_fields:
            product[extra_field] = factory.make_name(extra_field)
        product, resource = make_boot_resource_group_from_product(product)
        resource_set = resource.sets.first()
        store = BootResourceStore()
        rfile = store.get_or_create_boot_resource_file(resource_set, product)
        for extra_field in extra_fields:
            self.assertEqual(product[extra_field], rfile.extra[extra_field])

    def test_get_or_create_boot_resources_can_handle_duplicate_ftypes(self):
        name, architecture, product = make_product()
        product, resource = make_boot_resource_group_from_product(product)
        resource_set = resource.sets.first()
        store = BootResourceStore()
        files = [resource_set.files.first().filename]
        with post_commit_hooks:
            for _ in range(3):
                item_name = factory.make_name("item_name")
                product["item_name"] = item_name
                files.append(item_name)
                rfile = store.get_or_create_boot_resource_file(
                    resource_set, product
                )
                rfile.largefile = factory.make_LargeFile()
                rfile.save()
            for rfile in resource_set.files.all():
                self.assertIn(rfile.filename, files)
                self.assertEquals(rfile.filetype, product["ftype"])

    def test_get_resource_file_log_identifier_returns_valid_ident(self):
        os = factory.make_name("os")
        series = factory.make_name("series")
        arch = factory.make_name("arch")
        subarch = factory.make_name("subarch")
        version = factory.make_name("version")
        filename = factory.make_name("filename")
        name = "%s/%s" % (os, series)
        architecture = "%s/%s" % (arch, subarch)
        resource = factory.make_BootResource(
            rtype=BOOT_RESOURCE_TYPE.SYNCED,
            name=name,
            architecture=architecture,
        )
        resource_set = factory.make_BootResourceSet(resource, version=version)
        rfile = factory.make_boot_resource_file_with_content(
            resource_set, filename=filename
        )
        store = BootResourceStore()
        self.assertEqual(
            "%s/%s/%s/%s/%s/%s"
            % (os, arch, subarch, series, version, filename),
            store.get_resource_file_log_identifier(rfile),
        )
        self.assertEqual(
            "%s/%s/%s/%s/%s/%s"
            % (os, arch, subarch, series, version, filename),
            store.get_resource_file_log_identifier(
                rfile, resource_set, resource
            ),
        )

    def test_write_content_thread_saves_data(self):
        store = BootResourceStore()
        # Make size bigger than the read size so multiple loops are performed
        # and the content is written correctly.
        size = int(2.5 * store.read_size)
        rfile, reader, content = make_boot_resource_file_with_stream(size=size)
        store.write_content_thread(rfile.id, reader)
        self.assertTrue(BootResourceFile.objects.filter(id=rfile.id).exists())
        with rfile.largefile.content.open("rb") as stream:
            written_data = stream.read()
        self.assertEqual(content, written_data)
        rfile.largefile = reload_object(rfile.largefile)
        self.assertEqual(rfile.largefile.size, len(written_data))
        self.assertEqual(rfile.largefile.size, rfile.largefile.total_size)

    def test_write_content_doesnt_write_if_cancel(self):
        store = BootResourceStore()
        size = int(2.5 * store.read_size)
        rfile, reader, content = make_boot_resource_file_with_stream(size=size)
        store._cancel_finalize = True
        store.write_content_thread(rfile.id, reader)
        self.assertTrue(BootResourceFile.objects.filter(id=rfile.id).exists())
        with rfile.largefile.content.open("rb") as stream:
            written_data = stream.read()
        self.assertEqual(b"", written_data)
        rfile.largefile = reload_object(rfile.largefile)
        self.assertEqual(rfile.largefile.size, 0)

    @skip(
        "XXX blake_r: Skipped because it causes the test that runs after this "
        "to fail. Because this test is not isolated and places a task in the "
        "reactor."
    )
    def test_write_content_thread_deletes_file_on_bad_checksum(self):
        rfile, _, _ = make_boot_resource_file_with_stream()
        reader = BytesIO(factory.make_bytes())
        store = BootResourceStore()
        with post_commit_hooks:
            store.write_content_thread(rfile.id, reader)
        self.assertFalse(BootResourceFile.objects.filter(id=rfile.id).exists())

    def test_delete_content_to_finalize_deletes_items(self):
        self.useFixture(SignalsDisabled("largefiles"))
        rfile_one, _, _ = make_boot_resource_file_with_stream()
        rfile_two, _, _ = make_boot_resource_file_with_stream()
        store = BootResourceStore()
        store._content_to_finalize = {
            rfile_one.id: rfile_one,
            rfile_two.id: rfile_two,
        }
        store.delete_content_to_finalize()
        self.assertIsNone(reload_object(rfile_one))
        self.assertIsNone(reload_object(rfile_two))
        self.assertEquals({}, store._content_to_finalize)

    def test_finalize_does_nothing_if_resources_to_delete_hasnt_changed(self):
        self.patch(bootresources.Event.objects, "create_region_event")
        factory.make_BootResource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
        store = BootResourceStore()
        mock_resource_cleaner = self.patch(store, "resource_cleaner")
        mock_perform_write = self.patch(store, "perform_write")
        mock_resource_set_cleaner = self.patch(store, "resource_set_cleaner")
        store.finalize()
        self.expectThat(mock_resource_cleaner, MockNotCalled())
        self.expectThat(mock_perform_write, MockNotCalled())
        self.expectThat(mock_resource_set_cleaner, MockNotCalled())

    def test_finalize_calls_methods_if_new_resources_need_to_be_saved(self):
        factory.make_BootResource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
        store = BootResourceStore()
        store._content_to_finalize = [sentinel.content]
        mock_resource_cleaner = self.patch(store, "resource_cleaner")
        mock_perform_write = self.patch(store, "perform_write")
        mock_resource_set_cleaner = self.patch(store, "resource_set_cleaner")
        store.finalize()
        self.assertTrue(store._finalizing)
        self.expectThat(mock_resource_cleaner, MockCalledOnceWith())
        self.expectThat(mock_perform_write, MockCalledOnceWith())
        self.expectThat(mock_resource_set_cleaner, MockCalledOnceWith())

    def test_finalize_calls_methods_if_resources_to_delete_has_changed(self):
        factory.make_BootResource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
        store = BootResourceStore()
        store._resources_to_delete = set()
        mock_resource_cleaner = self.patch(store, "resource_cleaner")
        mock_perform_write = self.patch(store, "perform_write")
        mock_resource_set_cleaner = self.patch(store, "resource_set_cleaner")
        store.finalize()
        self.expectThat(mock_resource_cleaner, MockCalledOnceWith())
        self.expectThat(mock_perform_write, MockCalledOnceWith())
        self.expectThat(mock_resource_set_cleaner, MockCalledOnceWith())

    def test_finalize_calls_methods_with_delete_if_cancel_finalize(self):
        factory.make_BootResource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
        store = BootResourceStore()
        store._content_to_finalize = [sentinel.content]
        mock_resource_cleaner = self.patch(store, "resource_cleaner")
        mock_delete = self.patch(store, "delete_content_to_finalize")
        mock_resource_set_cleaner = self.patch(store, "resource_set_cleaner")
        store._cancel_finalize = True
        store.finalize()
        self.assertFalse(store._finalizing)
        self.expectThat(mock_resource_cleaner, MockCalledOnceWith())
        self.expectThat(mock_delete, MockCalledOnceWith())
        self.expectThat(mock_resource_set_cleaner, MockCalledOnceWith())

    def test_finalize_calls_delete_after_write_if_cancel_finalize(self):
        factory.make_BootResource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
        store = BootResourceStore()
        store._content_to_finalize = [sentinel.content]
        mock_resource_cleaner = self.patch(store, "resource_cleaner")
        mock_perform_write = self.patch(store, "perform_write")
        mock_resource_set_cleaner = self.patch(store, "resource_set_cleaner")
        mock_delete = self.patch(store, "delete_content_to_finalize")

        def set_cancel():
            store._cancel_finalize = True

        mock_perform_write.side_effect = set_cancel
        store.finalize()
        self.assertTrue(store._finalizing)
        self.expectThat(mock_resource_cleaner, MockCalledOnceWith())
        self.expectThat(mock_perform_write, MockCalledOnceWith())
        self.expectThat(mock_delete, MockCalledOnceWith())
        self.expectThat(mock_resource_set_cleaner, MockCalledOnceWith())


class TestBootResourceTransactional(MAASTransactionServerTestCase):
    """Test methods on `BootResourceStore` that manage their own transactions.

    This is done using `MAASTransactionServerTestCase` so the database is
    flushed after each test run.
    """

    def test_insert_does_nothing_if_file_already_exists(self):
        name, architecture, product = make_product()
        with transaction.atomic():
            product, resource = make_boot_resource_group_from_product(product)
            rfile = resource.sets.first().files.first()
        largefile = rfile.largefile
        store = BootResourceStore()
        mock_save_later = self.patch(store, "save_content_later")
        store.insert(product, sentinel.reader)
        self.assertEqual(largefile, reload_object(rfile).largefile)
        self.assertThat(mock_save_later, MockNotCalled())

    def test_insert_uses_already_existing_largefile(self):
        name, architecture, product = make_product()
        with transaction.atomic():
            product, resource = make_boot_resource_group_from_product(product)
            resource_set = resource.sets.first()
            with post_commit_hooks:
                resource_set.files.all().delete()
            largefile = factory.make_LargeFile()
        product["sha256"] = largefile.sha256
        product["size"] = largefile.total_size
        store = BootResourceStore()
        mock_save_later = self.patch(store, "save_content_later")
        store.insert(product, sentinel.reader)
        self.assertEqual(
            largefile,
            get_one(reload_object(resource_set).files.all()).largefile,
        )
        self.assertThat(mock_save_later, MockNotCalled())

    def test_insert_deletes_mismatch_largefile(self):
        self.patch(bootresources.Event.objects, "create_region_event")
        self.useFixture(SignalsDisabled("largefiles"))
        name, architecture, product = make_product()
        with transaction.atomic():
            product, resource = make_boot_resource_group_from_product(product)
            rfile = resource.sets.first().files.first()
            delete_largefile = rfile.largefile
            largefile = factory.make_LargeFile()
        product["sha256"] = largefile.sha256
        product["size"] = largefile.total_size
        store = BootResourceStore()
        mock_save_later = self.patch(store, "save_content_later")
        store.insert(product, sentinel.reader)
        self.assertFalse(
            LargeFile.objects.filter(id=delete_largefile.id).exists()
        )
        self.assertEqual(largefile, reload_object(rfile).largefile)
        self.assertThat(mock_save_later, MockNotCalled())

    def test_insert_deletes_root_image_if_squashfs_available(self):
        self.useFixture(SignalsDisabled("largefiles"))
        name, architecture, product = make_product(
            BOOT_RESOURCE_FILE_TYPE.ROOT_IMAGE
        )
        with transaction.atomic():
            product, resource = make_boot_resource_group_from_product(product)
            squashfs = factory.make_LargeFile()
        product["ftype"] = BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE
        product["itemname"] = "squashfs"
        product["path"] = "/path/to/squashfs"
        product["sha256"] = squashfs.sha256
        product["size"] = squashfs.total_size
        store = BootResourceStore()
        mock_save_later = self.patch(store, "save_content_later")
        store.insert(product, sentinel.reader)
        brs = resource.get_latest_set()
        self.assertThat(
            brs.files.filter(
                filetype=BOOT_RESOURCE_FILE_TYPE.ROOT_IMAGE
            ).count(),
            Equals(0),
        )
        self.assertThat(
            brs.files.filter(
                filetype=BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE
            ).count(),
            Equals(1),
        )
        self.assertThat(mock_save_later, MockNotCalled())

    def test_insert_prints_warning_if_mismatch_largefile(self):
        self.patch(bootresources.Event.objects, "create_region_event")
        self.useFixture(SignalsDisabled("largefiles"))
        name, architecture, product = make_product()
        with transaction.atomic():
            product, resource = make_boot_resource_group_from_product(product)
            largefile = factory.make_LargeFile()
        product["sha256"] = largefile.sha256
        product["size"] = largefile.total_size
        store = BootResourceStore()
        with FakeLogger("maas", logging.WARNING) as logger:
            store.insert(product, sentinel.reader)
        self.assertDocTestMatches(
            "Hash mismatch for prev_file=...", logger.output
        )

    def test_insert_deletes_mismatch_largefile_keeps_other_resource_file(self):
        self.patch(bootresources.Event.objects, "create_region_event")
        name, architecture, product = make_product()
        with transaction.atomic():
            resource = factory.make_BootResource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED,
                name=name,
                architecture=architecture,
            )
            resource_set = factory.make_BootResourceSet(
                resource, version=product["version_name"]
            )
            other_type = factory.pick_enum(
                BOOT_RESOURCE_FILE_TYPE, but_not=product["ftype"]
            )
            other_file = factory.make_boot_resource_file_with_content(
                resource_set, filename=other_type, filetype=other_type
            )
            rfile = factory.make_BootResourceFile(
                resource_set,
                other_file.largefile,
                filename=product["item_name"],
                filetype=product["ftype"],
            )
            largefile = factory.make_LargeFile()
        product["sha256"] = largefile.sha256
        product["size"] = largefile.total_size
        store = BootResourceStore()
        mock_save_later = self.patch(store, "save_content_later")
        store.insert(product, sentinel.reader)
        self.assertEqual(largefile, reload_object(rfile).largefile)
        self.assertTrue(
            LargeFile.objects.filter(id=other_file.largefile.id).exists()
        )
        self.assertTrue(
            BootResourceFile.objects.filter(id=other_file.id).exists()
        )
        self.assertEqual(
            other_file.largefile, reload_object(other_file).largefile
        )
        self.assertThat(mock_save_later, MockNotCalled())

    def test_insert_creates_new_largefile(self):
        name, architecture, product = make_product()
        with transaction.atomic():
            resource = factory.make_BootResource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED,
                name=name,
                architecture=architecture,
            )
            resource_set = factory.make_BootResourceSet(
                resource, version=product["version_name"]
            )
        product["sha256"] = factory.make_string(size=64)
        product["size"] = randint(1024, 2048)
        store = BootResourceStore()
        mock_save_later = self.patch(store, "save_content_later")
        store.insert(product, sentinel.reader)
        rfile = get_one(reload_object(resource_set).files.all())
        self.assertEqual(product["sha256"], rfile.largefile.sha256)
        self.assertEqual(product["size"], rfile.largefile.total_size)
        self.assertThat(
            mock_save_later, MockCalledOnceWith(rfile, sentinel.reader)
        )

    def test_insert_prints_error_when_breaking_resources(self):
        # Test case for bug 1419041: if the call to insert() makes
        # an existing complete resource incomplete: print an error in the
        # log.
        self.useFixture(SignalsDisabled("largefiles"))
        self.patch(bootresources.Event.objects, "create_region_event")
        name, architecture, product = make_product()
        with transaction.atomic():
            resource = factory.make_BootResource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED,
                name=name,
                architecture=architecture,
                kflavor="generic",
            )
            release_name = resource.name.split("/")[1]
            resource_set = factory.make_BootResourceSet(
                resource, version=product["version_name"]
            )
            factory.make_boot_resource_file_with_content(
                resource_set,
                filename=product["ftype"],
                filetype=product["ftype"],
            )
            # The resource has a complete set.
            self.assertIsNotNone(resource.get_latest_complete_set())
            # The resource is references in the simplestreams endpoint.
            simplestreams_response = SimpleStreamsHandler().get_product_index()
            simplestreams_content = simplestreams_response.content.decode(
                settings.DEFAULT_CHARSET
            )
            self.assertThat(simplestreams_content, Contains(release_name))
        product["sha256"] = factory.make_string(size=64)
        product["size"] = randint(1024, 2048)
        store = BootResourceStore()

        with FakeLogger("maas", logging.ERROR) as logger:
            store.insert(product, sentinel.reader)

        self.assertDocTestMatches(
            "Resource %s has no complete resource set!" % resource,
            logger.output,
        )

    def test_insert_doesnt_print_error_when_first_import(self):
        name, architecture, product = make_product()
        with transaction.atomic():
            factory.make_BootResource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED,
                name=name,
                architecture=architecture,
            )
        product["sha256"] = factory.make_string(size=64)
        product["size"] = randint(1024, 2048)
        store = BootResourceStore()

        with FakeLogger("maas", logging.ERROR) as logger:
            store.insert(product, sentinel.reader)

        self.assertEqual("", logger.output)

    def test_resource_cleaner_removes_boot_resources_without_sets(self):
        with transaction.atomic():
            resources = [
                factory.make_BootResource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
                for _ in range(3)
            ]
        store = BootResourceStore()
        store.resource_cleaner()
        for resource in resources:
            os, series = resource.name.split("/")
            arch, subarch = resource.split_arch()
            self.assertFalse(
                BootResource.objects.has_synced_resource(
                    os, arch, subarch, series
                )
            )

    def test_resource_cleaner_removes_boot_resources_not_in_selections(self):
        self.useFixture(SignalsDisabled("bootsources"))
        self.useFixture(SignalsDisabled("largefiles"))
        with transaction.atomic():
            # Make random selection as one is required, and empty set of
            # selections will not delete anything.
            factory.make_BootSourceSelection()
            resources = [
                factory.make_usable_boot_resource(
                    rtype=BOOT_RESOURCE_TYPE.SYNCED
                )
                for _ in range(3)
            ]
        store = BootResourceStore()
        store.resource_cleaner()
        for resource in resources:
            os, series = resource.name.split("/")
            arch, subarch = resource.split_arch()
            self.assertFalse(
                BootResource.objects.has_synced_resource(
                    os, arch, subarch, series
                )
            )

    def test_resource_cleaner_removes_extra_subarch_boot_resource(self):
        self.useFixture(SignalsDisabled("bootsources"))
        self.useFixture(SignalsDisabled("largefiles"))
        with transaction.atomic():
            # Make selection that will keep both subarches.
            arch = factory.make_name("arch")
            selection = factory.make_BootSourceSelection(
                arches=[arch], subarches=["*"], labels=["*"]
            )
            # Create first subarch for selection.
            subarch_one = factory.make_name("subarch")
            factory.make_usable_boot_resource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED,
                name="%s/%s" % (selection.os, selection.release),
                architecture="%s/%s" % (arch, subarch_one),
            )
            # Create second subarch for selection.
            subarch_two = factory.make_name("subarch")
            factory.make_usable_boot_resource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED,
                name="%s/%s" % (selection.os, selection.release),
                architecture="%s/%s" % (arch, subarch_two),
            )
        store = BootResourceStore()
        store._resources_to_delete = [
            "%s/%s/%s/%s"
            % (selection.os, arch, subarch_two, selection.release)
        ]
        store.resource_cleaner()
        self.assertTrue(
            BootResource.objects.has_synced_resource(
                selection.os, arch, subarch_one, selection.release
            )
        )
        self.assertFalse(
            BootResource.objects.has_synced_resource(
                selection.os, arch, subarch_two, selection.release
            )
        )

    def test_resource_cleaner_keeps_boot_resources_in_selections(self):
        self.patch(bootresources.Event.objects, "create_region_event")
        self.useFixture(SignalsDisabled("bootsources"))
        with transaction.atomic():
            resources = [
                factory.make_usable_boot_resource(
                    rtype=BOOT_RESOURCE_TYPE.SYNCED
                )
                for _ in range(3)
            ]
            for resource in resources:
                os, series = resource.name.split("/")
                arch, subarch = resource.split_arch()
                resource_set = resource.get_latest_set()
                factory.make_BootSourceSelection(
                    os=os,
                    release=series,
                    arches=[arch],
                    subarches=[subarch],
                    labels=[resource_set.label],
                )
        store = BootResourceStore()
        store.resource_cleaner()
        for resource in resources:
            os, series = resource.name.split("/")
            arch, subarch = resource.split_arch()
            self.assertTrue(
                BootResource.objects.has_synced_resource(
                    os, arch, subarch, series
                )
            )

    def test_resource_set_cleaner_removes_incomplete_set(self):
        with transaction.atomic():
            resource = factory.make_usable_boot_resource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED
            )
            incomplete_set = factory.make_BootResourceSet(resource)
        store = BootResourceStore()
        store.resource_set_cleaner()
        self.assertFalse(
            BootResourceSet.objects.filter(id=incomplete_set.id).exists()
        )

    def test_resource_set_cleaner_keeps_only_newest_completed_set(self):
        self.useFixture(SignalsDisabled("largefiles"))
        with transaction.atomic():
            resource = factory.make_BootResource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED
            )
            old_complete_sets = []
            for _ in range(3):
                resource_set = factory.make_BootResourceSet(resource)
                factory.make_boot_resource_file_with_content(resource_set)
                old_complete_sets.append(resource_set)
            newest_set = factory.make_BootResourceSet(resource)
            factory.make_boot_resource_file_with_content(newest_set)
        store = BootResourceStore()
        store.resource_set_cleaner()
        self.assertItemsEqual([newest_set], resource.sets.all())
        for resource_set in old_complete_sets:
            self.assertFalse(
                BootResourceSet.objects.filter(id=resource_set.id).exists()
            )

    def test_resource_set_cleaner_removes_resources_with_empty_sets(self):
        with transaction.atomic():
            resource = factory.make_BootResource(
                rtype=BOOT_RESOURCE_TYPE.SYNCED
            )
        store = BootResourceStore()
        store.resource_set_cleaner()
        self.assertFalse(BootResource.objects.filter(id=resource.id).exists())

    def test_perform_writes_writes_all_content(self):
        with transaction.atomic():
            files = [make_boot_resource_file_with_stream() for _ in range(3)]
            store = BootResourceStore()
            for rfile, reader, content in files:
                store.save_content_later(rfile, reader)
        store.perform_write()
        with transaction.atomic():
            for rfile, reader, content in files:
                self.assertTrue(
                    BootResourceFile.objects.filter(id=rfile.id).exists()
                )
                with rfile.largefile.content.open("rb") as stream:
                    written_data = stream.read()
                self.assertEqual(content, written_data)

    @asynchronous(timeout=1)
    def test_finalize_calls_notify_errback(self):
        @transactional
        def create_store(testcase):
            factory.make_BootResource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
            store = BootResourceStore()
            testcase.patch(store, "resource_cleaner")
            testcase.patch(store, "perform_write")
            testcase.patch(store, "resource_set_cleaner")
            return store

        notify = Deferred()
        d = deferToDatabase(create_store, self)
        d.addCallback(lambda store: store.finalize(notify=notify))
        d.addCallback(lambda _: notify)
        d.addErrback(lambda failure: failure.trap(Exception))
        return d

    @asynchronous(timeout=1)
    def test_finalize_calls_notify_callback(self):
        @transactional
        def create_store(testcase):
            factory.make_BootResource(rtype=BOOT_RESOURCE_TYPE.SYNCED)
            store = BootResourceStore()
            store._content_to_finalize = [sentinel.content]
            testcase.patch(store, "resource_cleaner")
            testcase.patch(store, "perform_write")
            testcase.patch(store, "resource_set_cleaner")
            return store

        notify = Deferred()
        d = deferToDatabase(create_store, self)
        d.addCallback(lambda store: store.finalize(notify=notify))
        d.addCallback(lambda _: notify)
        return d


class TestSetGlobalDefaultReleases(MAASServerTestCase):
    def test__doesnt_change_anything(self):
        commissioning_release = factory.make_name("release")
        deploy_release = factory.make_name("release")
        Config.objects.set_config(
            "commissioning_distro_series", commissioning_release
        )
        Config.objects.set_config("default_distro_series", deploy_release)
        resource = factory.make_usable_boot_resource(
            rtype=BOOT_RESOURCE_TYPE.SYNCED
        )
        mock_available = self.patch(
            BootResource.objects, "get_available_commissioning_resources"
        )
        mock_available.return_value = [resource]
        set_global_default_releases()
        self.assertEqual(
            commissioning_release,
            Config.objects.get(name="commissioning_distro_series").value,
        )
        self.assertEqual(
            deploy_release,
            Config.objects.get(name="default_distro_series").value,
        )

    def test__sets_commissioning_release(self):
        os, release = factory.make_name("os"), factory.make_name("release")
        resource = factory.make_usable_boot_resource(
            rtype=BOOT_RESOURCE_TYPE.SYNCED, name="%s/%s" % (os, release)
        )
        mock_available = self.patch(
            BootResource.objects, "get_available_commissioning_resources"
        )
        mock_available.return_value = [resource]
        set_global_default_releases()
        self.assertEqual(
            os, Config.objects.get(name="commissioning_osystem").value
        )
        self.assertEqual(
            release,
            Config.objects.get(name="commissioning_distro_series").value,
        )

    def test__sets_both_commissioning_deploy_release(self):
        os, release = factory.make_name("os"), factory.make_name("release")
        resource = factory.make_usable_boot_resource(
            rtype=BOOT_RESOURCE_TYPE.SYNCED, name="%s/%s" % (os, release)
        )
        mock_available = self.patch(
            BootResource.objects, "get_available_commissioning_resources"
        )
        mock_available.return_value = [resource]
        set_global_default_releases()
        self.assertEqual(
            os, Config.objects.get(name="commissioning_osystem").value
        )
        self.assertEqual(
            release,
            Config.objects.get(name="commissioning_distro_series").value,
        )
        self.assertEqual(os, Config.objects.get(name="default_osystem").value)
        self.assertEqual(
            release, Config.objects.get(name="default_distro_series").value
        )


class TestImportImages(MAASTransactionServerTestCase):
    def setUp(self):
        super(TestImportImages, self).setUp()
        self.useFixture(SimplestreamsEnvFixture())
        # Don't create the gnupg home directory.
        self.patch_autospec(bootresources, "create_gnupg_home")
        # Don't actually create the sources as that will make the cache update.
        self.patch_autospec(
            bootresources, "ensure_boot_source_definition"
        ).return_value = False
        # We're not testing cache_boot_sources() here, so patch it out to
        # avoid inadvertently calling it and wondering why the test blocks.
        self.patch_autospec(bootresources, "cache_boot_sources")
        self.patch(bootresources.Event.objects, "create_region_event")

    def patch_and_capture_env_for_download_all_boot_resources(self):
        class CaptureEnv:
            """Fake function; records a copy of the environment."""

            def __call__(self, *args, **kwargs):
                self.args = args
                self.env = environ.copy()

        capture = self.patch(
            bootresources, "download_all_boot_resources", CaptureEnv()
        )
        return capture

    def test_download_boot_resources_syncs_repo(self):
        fake_sync = self.patch(bootresources.BootResourceRepoWriter, "sync")
        store = BootResourceStore()
        source_url = factory.make_url()
        download_boot_resources(source_url, store, None, None)
        self.assertEqual(1, len(fake_sync.mock_calls))

    def test_download_boot_resources_passes_user_agent(self):
        self.patch(bootresources.BootResourceRepoWriter, "sync")
        store = BootResourceStore()
        source_url = factory.make_url()
        mock_UrlMirrorReader = self.patch(bootresources, "UrlMirrorReader")
        download_boot_resources(source_url, store, None, None)
        self.assertThat(
            mock_UrlMirrorReader,
            MockCalledOnceWith(
                ANY, policy=ANY, user_agent=get_maas_user_agent()
            ),
        )

    def test_download_boot_resources_fallsback_to_no_user_agent(self):
        self.patch(bootresources.BootResourceRepoWriter, "sync")
        store = BootResourceStore()
        source_url = factory.make_url()
        mock_UrlMirrorReader = self.patch(bootresources, "UrlMirrorReader")
        mock_UrlMirrorReader.side_effect = [TypeError(), Mock()]
        download_boot_resources(source_url, store, None, None)
        self.assertThat(
            mock_UrlMirrorReader,
            MockCallsMatch(
                call(ANY, policy=ANY, user_agent=get_maas_user_agent()),
                call(ANY, policy=ANY),
            ),
        )

    def test_download_all_boot_resources_calls_download_boot_resources(self):
        source = {
            "url": factory.make_url(),
            "keyring": self.make_file("keyring"),
        }
        product_mapping = ProductMapping()
        store = BootResourceStore()
        self.patch(
            bootresources.services, "getServiceNamed"
        ).return_value = MagicMock()
        fake_download = self.patch(bootresources, "download_boot_resources")
        download_all_boot_resources(
            sources=[source], product_mapping=product_mapping, store=store
        )
        self.assertThat(
            fake_download,
            MockCalledOnceWith(
                source["url"],
                store,
                product_mapping,
                keyring_file=source["keyring"],
            ),
        )

    def test_download_all_boot_resources_calls_finalize_on_store(self):
        product_mapping = ProductMapping()
        store = BootResourceStore()
        self.patch(
            bootresources.services, "getServiceNamed"
        ).return_value = MagicMock()
        fake_finalize = self.patch(store, "finalize")
        success = download_all_boot_resources(
            sources=[], product_mapping=product_mapping, store=store
        )
        self.assertThat(fake_finalize, MockCalledOnceWith(notify=None))
        self.assertTrue(success)

    def test_download_all_boot_resources_registers_stop_handler(self):
        product_mapping = ProductMapping()
        store = BootResourceStore()
        listener = MagicMock()
        self.patch(
            bootresources.services, "getServiceNamed"
        ).return_value = listener
        self.patch(bootresources, "download_boot_resources")
        download_all_boot_resources(
            sources=[], product_mapping=product_mapping, store=store
        )
        self.assertThat(
            listener.register, MockCalledOnceWith("sys_stop_import", ANY)
        )

    def test_download_all_boot_resources_calls_cancel_finalize(self):
        product_mapping = ProductMapping()
        store = BootResourceStore()
        listener = MagicMock()
        self.patch(
            bootresources.services, "getServiceNamed"
        ).return_value = listener

        # Call the stop_import function register with the listener.
        def call_stop(*args, **kwargs):
            listener.register.call_args[0][1]("sys_stop_import", "")

        self.patch(
            bootresources, "download_boot_resources"
        ).side_effect = call_stop

        mock_cancel = self.patch(store, "cancel_finalize")
        mock_finalize = self.patch(store, "finalize")
        success = download_all_boot_resources(
            sources=[{"url": "", "keyring": ""}],
            product_mapping=product_mapping,
            store=store,
        )
        self.assertThat(mock_cancel, MockCalledOnce())
        self.assertThat(mock_finalize, Not(MockCalledOnce()))
        self.assertFalse(success)

    def test_download_all_boot_resources_calls_cancel_finalize_in_stop(self):
        product_mapping = ProductMapping()
        store = BootResourceStore()
        listener = MagicMock()
        self.patch(
            bootresources.services, "getServiceNamed"
        ).return_value = listener
        self.patch(bootresources, "download_boot_resources")

        # Call the stop_import function when finalize is called.
        def call_stop(*args, **kwargs):
            listener.register.call_args[0][1]("sys_stop_import", "")

        mock_finalize = self.patch(store, "finalize")
        mock_finalize.side_effect = call_stop
        mock_cancel = self.patch(store, "cancel_finalize")

        success = download_all_boot_resources(
            sources=[{"url": "", "keyring": ""}],
            product_mapping=product_mapping,
            store=store,
        )
        self.assertThat(mock_finalize, MockCalledOnce())
        self.assertThat(mock_cancel, MockCalledOnce())
        self.assertFalse(success)

    def test__import_resources_exits_early_if_lock_held(self):
        set_simplestreams_env = self.patch_autospec(
            bootresources, "set_simplestreams_env"
        )
        with lock_held_in_other_thread(bootresources.locks.import_images):
            bootresources._import_resources()
        # The test for set_simplestreams_env is not called if the
        # lock is already held.
        self.assertThat(set_simplestreams_env, MockNotCalled())

    def test__import_resources_holds_lock(self):
        fake_write_all_keyrings = self.patch(
            bootresources, "write_all_keyrings"
        )

        def test_for_held_lock(directory, sources):
            self.assertTrue(bootresources.locks.import_images.is_locked())
            return []

        fake_write_all_keyrings.side_effect = test_for_held_lock

        bootresources._import_resources()
        self.assertFalse(bootresources.locks.import_images.is_locked())

    def test__import_resources_calls_functions_with_correct_parameters(self):
        write_all_keyrings = self.patch(bootresources, "write_all_keyrings")
        write_all_keyrings.return_value = []
        image_descriptions = self.patch(
            bootresources, "download_all_image_descriptions"
        )
        descriptions = Mock()
        descriptions.is_empty.return_value = False
        image_descriptions.return_value = descriptions
        map_products = self.patch(bootresources, "map_products")
        map_products.return_value = sentinel.mapping
        download_all_boot_resources = self.patch(
            bootresources, "download_all_boot_resources"
        )
        set_global_default_releases = self.patch(
            bootresources, "set_global_default_releases"
        )

        bootresources._import_resources()

        self.expectThat(bootresources.create_gnupg_home, MockCalledOnceWith())
        self.expectThat(
            bootresources.ensure_boot_source_definition, MockCalledOnceWith()
        )
        self.expectThat(bootresources.cache_boot_sources, MockCalledOnceWith())
        self.expectThat(write_all_keyrings, MockCalledOnceWith(ANY, []))
        self.expectThat(
            image_descriptions, MockCalledOnceWith([], get_maas_user_agent())
        )
        self.expectThat(map_products, MockCalledOnceWith(descriptions))
        self.expectThat(
            download_all_boot_resources,
            MockCalledOnceWith([], sentinel.mapping, notify=None),
        )
        self.expectThat(set_global_default_releases, MockCalledOnceWith())

    def test__import_resources_has_env_GNUPGHOME_set(self):
        fake_image_descriptions = self.patch(
            bootresources, "download_all_image_descriptions"
        )
        descriptions = Mock()
        descriptions.is_empty.return_value = False
        fake_image_descriptions.return_value = descriptions
        self.patch(bootresources, "map_products")
        capture = self.patch_and_capture_env_for_download_all_boot_resources()

        bootresources._import_resources()
        self.assertEqual(get_maas_user_gpghome(), capture.env["GNUPGHOME"])

    def test__import_resources_has_env_http_and_https_proxy_set(self):
        proxy_address = factory.make_name("proxy")
        self.patch(signals.bootsources, "post_commit_do")
        Config.objects.set_config("http_proxy", proxy_address)

        fake_image_descriptions = self.patch(
            bootresources, "download_all_image_descriptions"
        )
        descriptions = Mock()
        descriptions.is_empty.return_value = False
        fake_image_descriptions.return_value = descriptions
        self.patch(bootresources, "map_products")
        capture = self.patch_and_capture_env_for_download_all_boot_resources()

        bootresources._import_resources()
        self.assertEqual(
            (proxy_address, proxy_address),
            (capture.env["http_proxy"], capture.env["http_proxy"]),
        )

    def test__import_resources_schedules_import_to_rack_controllers(self):
        from maasserver.clusterrpc import boot_images

        self.patch(boot_images.RackControllersImporter, "run")

        bootresources._import_resources()

        self.assertThat(
            boot_images.RackControllersImporter.run, MockCalledOnceWith()
        )

    def test__restarts_import_if_source_changed(self):
        # Regression test for LP:1766370
        self.patch(signals.bootsources, "post_commit_do")
        boot_source = factory.make_BootSource(
            keyring_data=factory.make_bytes()
        )
        factory.make_BootSourceSelection(boot_source=boot_source)

        def write_all_keyrings(directory, sources):
            for source in sources:
                source["keyring"] = factory.make_name("keyring")
            return sources

        mock_write_all_keyrings = self.patch(
            bootresources, "write_all_keyrings"
        )
        mock_write_all_keyrings.side_effect = write_all_keyrings

        def image_descriptions(*args, **kwargs):
            # Simulate user changing sources
            if not image_descriptions.called:
                BootSource.objects.all().delete()
                boot_source = factory.make_BootSource(
                    keyring_data=factory.make_bytes()
                )
                factory.make_BootSourceSelection(boot_source=boot_source)
                image_descriptions.called = True

            class Ret:
                def is_empty(self):
                    return False

            return Ret()

        image_descriptions.called = False
        mock_image_descriptions = self.patch(
            bootresources, "download_all_image_descriptions"
        )
        mock_image_descriptions.side_effect = image_descriptions
        descriptions = Mock()
        descriptions.is_empty.return_value = False
        image_descriptions.return_value = descriptions
        map_products = self.patch(bootresources, "map_products")
        map_products.return_value = sentinel.mapping
        self.patch(bootresources, "download_all_boot_resources")
        self.patch(bootresources, "set_global_default_releases")

        bootresources._import_resources()

        # write_all_keyrings is called once per
        self.assertEqual(2, mock_write_all_keyrings.call_count)

    def test__restarts_import_if_selection_changed(self):
        # Regression test for LP:1766370
        self.patch(signals.bootsources, "post_commit_do")
        boot_source = factory.make_BootSource(
            keyring_data=factory.make_bytes()
        )
        factory.make_BootSourceSelection(boot_source=boot_source)

        def write_all_keyrings(directory, sources):
            for source in sources:
                source["keyring"] = factory.make_name("keyring")
            return sources

        mock_write_all_keyrings = self.patch(
            bootresources, "write_all_keyrings"
        )
        mock_write_all_keyrings.side_effect = write_all_keyrings

        def image_descriptions(*args, **kwargs):
            # Simulate user adding a selection.
            if not image_descriptions.called:
                factory.make_BootSourceSelection(boot_source=boot_source)
                image_descriptions.called = True

            class Ret:
                def is_empty(self):
                    return False

            return Ret()

        image_descriptions.called = False
        mock_image_descriptions = self.patch(
            bootresources, "download_all_image_descriptions"
        )
        mock_image_descriptions.side_effect = image_descriptions
        descriptions = Mock()
        descriptions.is_empty.return_value = False
        image_descriptions.return_value = descriptions
        map_products = self.patch(bootresources, "map_products")
        map_products.return_value = sentinel.mapping
        self.patch(bootresources, "download_all_boot_resources")
        self.patch(bootresources, "set_global_default_releases")

        bootresources._import_resources()

        # write_all_keyrings is called once per
        self.assertEqual(2, mock_write_all_keyrings.call_count)


class TestImportResourcesInThread(MAASTestCase):
    """Tests for `_import_resources_in_thread`."""

    def test__defers__import_resources_to_thread(self):
        deferToDatabase = self.patch(bootresources, "deferToDatabase")
        bootresources._import_resources_in_thread()
        self.assertThat(
            deferToDatabase,
            MockCalledOnceWith(bootresources._import_resources, notify=None),
        )

    def tests__defaults_force_to_False(self):
        deferToDatabase = self.patch(bootresources, "deferToDatabase")
        bootresources._import_resources_in_thread()
        self.assertThat(
            deferToDatabase,
            MockCalledOnceWith(bootresources._import_resources, notify=None),
        )

    def test__logs_errors_and_does_not_errback(self):
        logger = self.useFixture(TwistedLoggerFixture())
        exception_type = factory.make_exception_type()
        deferToDatabase = self.patch(bootresources, "deferToDatabase")
        deferToDatabase.return_value = fail(exception_type())
        d = bootresources._import_resources_in_thread()
        self.assertIsNone(extract_result(d))
        self.assertDocTestMatches(
            """\
            Importing boot resources failed.
            Traceback (most recent call last):
            ...
            """,
            logger.output,
        )

    def test__logs_subprocess_output_on_error(self):
        logger = self.useFixture(TwistedLoggerFixture())
        exception = CalledProcessError(
            2, [factory.make_name("command")], factory.make_name("output")
        )
        deferToDatabase = self.patch(bootresources, "deferToDatabase")
        deferToDatabase.return_value = fail(exception)
        d = bootresources._import_resources_in_thread()
        self.assertIsNone(extract_result(d))
        self.assertDocTestMatches(
            """\
            Importing boot resources failed.
            Traceback (most recent call last):
            Failure: subprocess.CalledProcessError:
              Command `command-...` returned non-zero exit status 2:
            output-...
            """,
            logger.output,
        )


class TestStopImportResources(MAASTransactionServerTestCase):
    def make_listener_without_delay(self):
        listener = PostgresListenerService()
        self.patch(listener, "HANDLE_NOTIFY_DELAY", 0)
        return listener

    @wait_for_reactor
    @inlineCallbacks
    def test_does_nothing_if_import_not_running(self):
        mock_defer = self.patch(bootresources, "deferToDatabase")
        mock_defer.return_value = succeed(False)
        yield bootresources.stop_import_resources()
        self.assertThat(mock_defer, MockCalledOnce())

    @wait_for_reactor
    @inlineCallbacks
    def test_sends_stop_import_notification(self):
        mock_running = self.patch(bootresources, "is_import_resources_running")
        mock_running.side_effect = [True, True, False]
        dv = DeferredValue()
        listener = self.make_listener_without_delay()
        listener.register("sys_stop_import", lambda *args: dv.set(args))
        yield listener.startService()
        try:
            yield bootresources.stop_import_resources()
            yield dv.get(2)
        finally:
            yield listener.stopService()


class TestImportResourcesService(MAASTestCase):
    """Tests for `ImportResourcesService`."""

    def test__is_a_TimerService(self):
        service = bootresources.ImportResourcesService()
        self.assertIsInstance(service, TimerService)

    def test__runs_once_an_hour(self):
        service = bootresources.ImportResourcesService()
        self.assertEqual(3600, service.step)

    def test__calls__maybe_import_resources(self):
        service = bootresources.ImportResourcesService()
        self.assertEqual(
            (service.maybe_import_resources, (), {}), service.call
        )

    def test_maybe_import_resources_does_not_error(self):
        service = bootresources.ImportResourcesService()
        deferToDatabase = self.patch(bootresources, "deferToDatabase")
        exception_type = factory.make_exception_type()
        deferToDatabase.return_value = fail(exception_type())
        d = service.maybe_import_resources()
        self.assertIsNone(extract_result(d))


class TestImportResourcesServiceAsync(MAASTransactionServerTestCase):
    """Tests for the async parts of `ImportResourcesService`."""

    def test__imports_resources_in_thread_if_auto(self):
        self.patch(bootresources, "_import_resources_in_thread")
        self.patch(bootresources, "is_dev_environment").return_value = False

        with transaction.atomic():
            Config.objects.set_config("boot_images_auto_import", True)

        service = bootresources.ImportResourcesService()
        maybe_import_resources = asynchronous(service.maybe_import_resources)
        maybe_import_resources().wait(5)

        self.assertThat(
            bootresources._import_resources_in_thread, MockCalledOnceWith()
        )

    def test__no_auto_import_if_dev(self):
        self.patch(bootresources, "_import_resources_in_thread")

        with transaction.atomic():
            Config.objects.set_config("boot_images_auto_import", True)

        service = bootresources.ImportResourcesService()
        maybe_import_resources = asynchronous(service.maybe_import_resources)
        maybe_import_resources().wait(5)

        self.assertThat(
            bootresources._import_resources_in_thread, MockNotCalled()
        )

    def test__does_not_import_resources_in_thread_if_not_auto(self):
        self.patch(bootresources, "_import_resources_in_thread")

        with transaction.atomic():
            Config.objects.set_config("boot_images_auto_import", False)

        service = bootresources.ImportResourcesService()
        maybe_import_resources = asynchronous(service.maybe_import_resources)
        maybe_import_resources().wait(5)

        self.assertThat(
            bootresources._import_resources_in_thread, MockNotCalled()
        )


class TestImportResourcesProgressService(MAASServerTestCase):
    """Tests for `ImportResourcesProgressService`."""

    def test__is_a_TimerService(self):
        service = bootresources.ImportResourcesProgressService()
        self.assertIsInstance(service, TimerService)

    def test__runs_every_three_minutes(self):
        service = bootresources.ImportResourcesProgressService()
        self.assertEqual(180, service.step)

    def test__calls_try_check_boot_images(self):
        service = bootresources.ImportResourcesProgressService()
        func, args, kwargs = service.call
        self.expectThat(func, Equals(service.try_check_boot_images))
        self.expectThat(args, HasLength(0))
        self.expectThat(kwargs, HasLength(0))


class TestImportResourcesProgressServiceAsync(MAASTransactionServerTestCase):
    """Tests for the async parts of `ImportResourcesProgressService`."""

    def set_maas_url(self):
        maas_url_path = "/path/%s" % factory.make_string()
        maas_url = factory.make_simple_http_url(path=maas_url_path)
        self.useFixture(RegionConfigurationFixture(maas_url=maas_url))
        return maas_url, maas_url_path

    def patch_are_functions(self, service, region_answer, cluster_answer):
        # Patch the are_boot_images_available_* functions.
        are_region_func = self.patch_autospec(
            service, "are_boot_images_available_in_the_region"
        )
        are_region_func.return_value = region_answer
        are_cluster_func = self.patch_autospec(
            service, "are_boot_images_available_in_any_rack"
        )
        are_cluster_func.return_value = cluster_answer

    def test__adds_warning_if_boot_images_exists_on_cluster_not_region(self):
        maas_url, maas_url_path = self.set_maas_url()

        service = bootresources.ImportResourcesProgressService()
        self.patch_are_functions(service, False, True)

        check_boot_images = asynchronous(service.check_boot_images)
        check_boot_images().wait(5)

        error_observed = get_persistent_error(COMPONENT.IMPORT_PXE_FILES)
        error_expected = """\
        One or more of your rack controller(s) currently has boot images, but
        your region controller does not. Machines will not be able to provision
        until you import boot images into the region. Visit the
        <a href="%s">boot images</a> page to start the import.
        """
        images_link = maas_url + urljoin(maas_url_path, "/MAAS/#/images")
        self.assertEqual(
            normalise_whitespace(error_expected % images_link),
            normalise_whitespace(error_observed),
        )

    def test__adds_warning_if_boot_image_import_not_started(self):
        maas_url, maas_url_path = self.set_maas_url()

        service = bootresources.ImportResourcesProgressService()
        self.patch_are_functions(service, False, False)

        check_boot_images = asynchronous(service.check_boot_images)
        check_boot_images().wait(5)

        error_observed = get_persistent_error(COMPONENT.IMPORT_PXE_FILES)
        error_expected = """\
        Boot image import process not started. Machines will not be able to
        provision without boot images. Visit the <a href="%s">boot images</a>
        page to start the import.
        """
        images_link = maas_url + urljoin(maas_url_path, "/MAAS/#/images")
        self.assertEqual(
            normalise_whitespace(error_expected % images_link),
            normalise_whitespace(error_observed),
        )

    def test__removes_warning_if_boot_image_process_started(self):
        register_persistent_error(
            COMPONENT.IMPORT_PXE_FILES,
            "You rotten swine, you! You have deaded me!",
        )

        service = bootresources.ImportResourcesProgressService()
        self.patch_are_functions(service, True, False)

        check_boot_images = asynchronous(service.check_boot_images)
        check_boot_images().wait(5)

        error = get_persistent_error(COMPONENT.IMPORT_PXE_FILES)
        self.assertIsNone(error)

    def test__logs_all_errors(self):
        logger = self.useFixture(TwistedLoggerFixture())

        exception = factory.make_exception()
        service = bootresources.ImportResourcesProgressService()
        check_boot_images = self.patch_autospec(service, "check_boot_images")
        check_boot_images.return_value = fail(exception)
        try_check_boot_images = asynchronous(service.try_check_boot_images)
        try_check_boot_images().wait(5)

        self.assertDocTestMatches(
            """\
            Failure checking for boot images.
            Traceback (most recent call last):
            ...
            maastesting.factory.TestException#...:
            """,
            logger.output,
        )

    def test__are_boot_images_available_in_the_region(self):
        service = bootresources.ImportResourcesProgressService()
        self.assertFalse(service.are_boot_images_available_in_the_region())
        factory.make_BootResource()
        self.assertTrue(service.are_boot_images_available_in_the_region())

    def test__are_boot_images_available_in_any_rack_v2(self):
        # Import the websocket handlers now: merely defining DeviceHandler,
        # e.g., causes a database access, which will crash if it happens
        # inside the reactor thread where database access is forbidden and
        # prevented. My own opinion is that a class definition should not
        # cause a database access and we ought to fix that.
        import maasserver.websockets.handlers  # noqa

        rack_controller = factory.make_RackController()
        service = bootresources.ImportResourcesProgressService()

        self.useFixture(RegionEventLoopFixture("rpc"))
        self.useFixture(RunningEventLoopFixture())
        region_rpc = MockLiveRegionToClusterRPCFixture()
        self.useFixture(region_rpc)

        # are_boot_images_available_in_the_region() returns False when there
        # are no clusters connected.
        self.assertFalse(service.are_boot_images_available_in_any_rack())

        # Connect a rack controller to the region via RPC.
        cluster_rpc = region_rpc.makeCluster(rack_controller, ListBootImagesV2)

        # are_boot_images_available_in_the_region() returns False when none of
        # the clusters have any images.
        cluster_rpc.ListBootImagesV2.return_value = succeed({"images": []})
        self.assertFalse(service.are_boot_images_available_in_any_rack())

        # are_boot_images_available_in_the_region() returns True when a
        # cluster has an imported boot image.
        response = {"images": [make_rpc_boot_image()]}
        cluster_rpc.ListBootImagesV2.return_value = succeed(response)
        self.assertTrue(service.are_boot_images_available_in_any_rack())

    def test__are_boot_images_available_in_any_rack_v1(self):
        # Import the websocket handlers now: merely defining DeviceHandler,
        # e.g., causes a database access, which will crash if it happens
        # inside the reactor thread where database access is forbidden and
        # prevented. My own opinion is that a class definition should not
        # cause a database access and we ought to fix that.
        import maasserver.websockets.handlers  # noqa

        rack_controller = factory.make_RackController()
        service = bootresources.ImportResourcesProgressService()

        self.useFixture(RegionEventLoopFixture("rpc"))
        self.useFixture(RunningEventLoopFixture())
        region_rpc = MockLiveRegionToClusterRPCFixture()
        self.useFixture(region_rpc)

        # are_boot_images_available_in_the_region() returns False when there
        # are no clusters connected.
        self.assertFalse(service.are_boot_images_available_in_any_rack())

        # Connect a rack controller to the region via RPC.
        cluster_rpc = region_rpc.makeCluster(
            rack_controller, ListBootImagesV2, ListBootImages
        )

        # All calls to ListBootImagesV2 raises a UnhandledCommand.
        cluster_rpc.ListBootImagesV2.side_effect = UnhandledCommand

        # are_boot_images_available_in_the_region() returns False when none of
        # the clusters have any images.
        cluster_rpc.ListBootImages.return_value = succeed({"images": []})
        self.assertFalse(service.are_boot_images_available_in_any_rack())

        # are_boot_images_available_in_the_region() returns True when a
        # cluster has an imported boot image.
        response = {"images": [make_rpc_boot_image()]}
        cluster_rpc.ListBootImages.return_value = succeed(response)
        self.assertTrue(service.are_boot_images_available_in_any_rack())


class TestBootResourceRepoWriter(MAASServerTestCase):
    """Tests for `BootResourceRepoWriter`."""

    def create_ubuntu_simplestream(
        self, ftypes, stream_version=None, osystem=None, maas_supported=None
    ):
        version = "16.04"
        arch = "amd64"
        subarch = "hwe-x"
        if osystem is None:
            osystem = "ubuntu"
        if stream_version is None and osystem == "ubuntu-core":
            stream_version = "v4"
        elif stream_version is None:
            stream_version = random.choice(["v2", "v3"])
        if maas_supported is None:
            maas_supported = __version__
        product = "com.ubuntu.maas.daily:%s:boot:%s:%s:%s" % (
            stream_version,
            version,
            arch,
            subarch,
        )
        version = datetime.now().date().strftime("%Y%m%d.0")
        versions = {
            version: {
                "items": {
                    ftype: {
                        "sha256": factory.make_name("sha256"),
                        "path": factory.make_name("path"),
                        "ftype": ftype,
                        "size": random.randint(0, 2 ** 64),
                    }
                    for ftype in ftypes
                }
            }
        }
        products = {
            product: {
                "subarch": subarch,
                "label": "daily",
                "os": osystem,
                "arch": arch,
                "subarches": "generic,%s" % subarch,
                "kflavor": "generic",
                "version": version,
                "versions": versions,
                "maas_supported": maas_supported,
            }
        }
        src = {
            "datatype": "image-downloads",
            "format": "products:1.0",
            "updated": format_datetime(datetime.now()),
            "products": products,
            "content_id": "com.ubuntu.maas:daily:v2:download",
        }
        return src, product, version

    def create_bootloader_simplestream(self, stream_version=None):
        if stream_version is None:
            stream_version = "1"
        product = (
            "com.ubuntu.maas:daily:%s:bootloader-download" % stream_version
        )
        version = datetime.now().date().strftime("%Y%m%d.0")
        versions = {
            version: {
                "items": {
                    BOOT_RESOURCE_FILE_TYPE.BOOTLOADER: {
                        "sha256": factory.make_name("sha256"),
                        "path": factory.make_name("path"),
                        "ftype": BOOT_RESOURCE_FILE_TYPE.BOOTLOADER,
                        "size": random.randint(0, 2 ** 64),
                    }
                }
            }
        }
        products = {
            product: {
                "label": "daily",
                "os": "grub-efi-signed",
                "arch": "amd64",
                "bootloader-type": "uefi",
                "version": version,
                "versions": versions,
            }
        }
        src = {
            "datatype": "image-downloads",
            "format": "products:1.0",
            "updated": format_datetime(datetime.now()),
            "products": products,
            "content_id": "com.ubuntu.maas:daily:1:bootloader-download",
        }
        return src, product, version

    def test_insert_validates_maas_supported_if_available(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_ubuntu_simplestream(
            [BOOT_RESOURCE_FILE_TYPE.ROOT_DDXZ], maas_supported="999.999"
        )
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.ROOT_DDXZ
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.ROOT_DDXZ)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockNotCalled())

    def test_insert_prefers_squashfs_over_root_image(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_ubuntu_simplestream(
            [
                BOOT_RESOURCE_FILE_TYPE.ROOT_IMAGE,
                BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE,
            ]
        )
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.ROOT_IMAGE
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.ROOT_IMAGE)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockNotCalled())

    def test_insert_allows_squashfs(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_ubuntu_simplestream(
            [BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE]
        )
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockCalledOnce())

    def test_insert_allows_root_image(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_ubuntu_simplestream(
            [BOOT_RESOURCE_FILE_TYPE.ROOT_IMAGE]
        )
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.ROOT_IMAGE
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.ROOT_IMAGE)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockCalledOnce())

    def test_insert_allows_bootloader(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_bootloader_simplestream()
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.BOOTLOADER
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.BOOTLOADER)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockCalledOnce())

    def test_insert_allows_archive_tar_xz(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_ubuntu_simplestream(
            [BOOT_RESOURCE_FILE_TYPE.ARCHIVE_TAR_XZ]
        )
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.ARCHIVE_TAR_XZ
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.ARCHIVE_TAR_XZ)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockCalledOnce())

    def test_insert_ignores_unknown_ftypes(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        unknown_ftype = factory.make_name("ftype")
        src, product, version = self.create_ubuntu_simplestream(
            [unknown_ftype]
        )
        data = src["products"][product]["versions"][version]["items"][
            unknown_ftype
        ]
        pedigree = (product, version, unknown_ftype)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockNotCalled())

    def test_insert_validates_bootloader(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_bootloader_simplestream()
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.BOOTLOADER
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.BOOTLOADER)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockCalledOnce())

    def test_insert_validates_rejects_unknown_version(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_bootloader_simplestream(
            factory.make_name("stream_version")
        )
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.BOOTLOADER
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.BOOTLOADER)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockNotCalled())

    def test_insert_validates_ubuntu(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_ubuntu_simplestream(
            [BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE]
        )
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockCalledOnce())

    def test_validate_ubuntu_rejects_unknown_version(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_ubuntu_simplestream(
            [BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE],
            factory.make_name("stream_version"),
        )
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.SQUASHFS_IMAGE)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockNotCalled())

    def test_validates_ubuntu_core(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_ubuntu_simplestream(
            [BOOT_RESOURCE_FILE_TYPE.ROOT_DDXZ], osystem="ubuntu-core"
        )
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.ROOT_DDXZ
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.ROOT_DDXZ)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockCalledOnce())

    def test_validates_ubuntu_core_rejects_unknown_version(self):
        boot_resource_repo_writer = BootResourceRepoWriter(
            BootResourceStore(), None
        )
        src, product, version = self.create_ubuntu_simplestream(
            [BOOT_RESOURCE_FILE_TYPE.ROOT_DDXZ],
            factory.make_name("stream_version"),
            "ubuntu-core",
        )
        data = src["products"][product]["versions"][version]["items"][
            BOOT_RESOURCE_FILE_TYPE.ROOT_DDXZ
        ]
        pedigree = (product, version, BOOT_RESOURCE_FILE_TYPE.ROOT_DDXZ)
        mock_insert = self.patch(boot_resource_repo_writer.store, "insert")
        boot_resource_repo_writer.insert_item(data, src, None, pedigree, None)
        self.assertThat(mock_insert, MockNotCalled())
