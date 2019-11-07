# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the `helpers` module."""

__all__ = []

from unittest import mock

from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase
from provisioningserver.import_images import helpers
from simplestreams.util import SignatureMissingException


class TestGetSigningPolicy(MAASTestCase):
    """Tests for `get_signing_policy`."""

    def test_picks_nonchecking_policy_for_json_index(self):
        path = "streams/v1/index.json"
        policy = helpers.get_signing_policy(path)
        content = factory.make_string()
        self.assertEqual(
            content, policy(content, path, factory.make_name("keyring"))
        )

    def test_picks_checking_policy_for_sjson_index(self):
        path = "streams/v1/index.sjson"
        content = factory.make_string()
        policy = helpers.get_signing_policy(path)
        self.assertRaises(
            SignatureMissingException,
            policy,
            content,
            path,
            factory.make_name("keyring"),
        )

    def test_picks_checking_policy_for_json_gpg_index(self):
        path = "streams/v1/index.json.gpg"
        content = factory.make_string()
        policy = helpers.get_signing_policy(path)
        self.assertRaises(
            SignatureMissingException,
            policy,
            content,
            path,
            factory.make_name("keyring"),
        )

    def test_injects_default_keyring_if_passed(self):
        path = "streams/v1/index.json.gpg"
        content = factory.make_string()
        keyring = factory.make_name("keyring")
        self.patch(helpers, "policy_read_signed")
        policy = helpers.get_signing_policy(path, keyring)
        policy(content, path)
        self.assertThat(
            helpers.policy_read_signed,
            MockCalledOnceWith(mock.ANY, mock.ANY, keyring=keyring),
        )


class TestGetOSFromProduct(MAASTestCase):
    """Tests for `get_os_from_product`."""

    def test_returns_os_from_product(self):
        os = factory.make_name("os")
        product = {"os": os}
        self.assertEqual(os, helpers.get_os_from_product(product))

    def test_returns_ubuntu_if_missing(self):
        self.assertEqual("ubuntu", helpers.get_os_from_product({}))
