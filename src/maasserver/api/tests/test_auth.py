# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test `api.auth` module."""

__all__ = []

from datetime import datetime, timedelta
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from maasserver.api import auth as api_auth
from maasserver.api.auth import MAASAPIAuthentication, OAuthUnauthorized
from maasserver.middleware import ExternalAuthInfo
from maasserver.models import Config
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maastesting.testcase import MAASTestCase
from metadataserver.nodeinituser import get_node_init_user
from oauth import oauth
from testtools.matchers import Contains


class TestMAASAPIAuthentication(MAASServerTestCase):
    def setUp(self):
        super().setUp()
        Config.objects.set_config("external_auth_url", "https://example.com")

    def make_request(self, user=None):
        request = factory.make_fake_request("/")
        request.user = user or AnonymousUser()

        auth_url = Config.objects.get_config("external_auth_url")
        if auth_url:
            request.external_auth_info = ExternalAuthInfo(
                type="candid",
                url="https://example.com",
                domain="domain",
                admin_group="admins",
            )
        else:
            request.external_auth_info = None
        return request

    def test_is_authenticated(self):
        Config.objects.set_config("external_auth_url", "")
        user = factory.make_User()
        request = self.make_request(user=user)
        auth = MAASAPIAuthentication()
        self.assertTrue(auth.is_authenticated(request))

    def test_is_authenticated_external_auth(self):
        user = factory.make_User()
        request = self.make_request(user=user)
        auth = MAASAPIAuthentication()
        self.assertTrue(auth.is_authenticated(request))

    def test_is_authenticated_external_auth_validate_user(self):
        mock_validate = self.patch(api_auth, "validate_user_external_auth")
        mock_validate.return_value = True

        auth = MAASAPIAuthentication()
        user = factory.make_User()
        user.userprofile.auth_last_check = datetime.utcnow() - timedelta(
            days=1
        )
        mock_token = mock.Mock(user=user)
        request = self.make_request()

        auth.is_valid_request = lambda request: True
        auth.validate_token = lambda request: (mock.Mock(), mock_token, None)
        self.assertTrue(auth.is_authenticated(request))
        mock_validate.assert_called_with(
            user,
            ExternalAuthInfo(
                "candid", "https://example.com", "domain", "admins"
            ),
        )

    def test_is_authenticated_external_auth_validate_fail(self):
        mock_validate = self.patch(api_auth, "validate_user_external_auth")
        mock_validate.return_value = False

        auth = MAASAPIAuthentication()
        user = factory.make_User()
        user.userprofile.auth_last_check = datetime.utcnow() - timedelta(
            days=1
        )
        mock_token = mock.Mock(user=user)
        request = self.make_request()
        auth.is_valid_request = lambda request: True
        auth.validate_token = lambda request: (mock.Mock(), mock_token, None)
        self.assertFalse(auth.is_authenticated(request))
        # check interval not expired, the user isn't checked
        mock_validate.assert_called_with(
            user,
            ExternalAuthInfo(
                type="candid",
                url="https://example.com",
                domain="domain",
                admin_group="admins",
            ),
        )

    def test_is_authenticated_external_auth_user_local(self):
        mock_validate = self.patch(api_auth, "validate_user_external_auth")
        mock_validate.return_value = True

        auth = MAASAPIAuthentication()
        user = factory.make_User()
        user.userprofile.is_local = True
        user.userprofile.save()
        request = self.make_request(user=user)
        self.assertFalse(auth.is_authenticated(request))
        mock_validate.assert_not_called()

    def test_is_authenticated_external_auth_system_user(self):
        mock_validate = self.patch(api_auth, "validate_user_external_auth")
        mock_validate.return_value = True

        auth = MAASAPIAuthentication()
        user = get_node_init_user()
        request = self.make_request()
        mock_token = mock.Mock(user=user)
        auth.is_valid_request = lambda request: True
        auth.validate_token = lambda request: (mock.Mock(), mock_token, None)
        self.assertTrue(auth.is_authenticated(request))
        mock_validate.assert_not_called()

    def test_is_authenticated_false_external_user_no_external_auth(self):
        Config.objects.set_config("external_auth_url", "")
        user = factory.make_User()
        user.userprofile.is_local = False
        user.userprofile.save()
        mock_token = mock.Mock(user=user)
        request = self.make_request()
        auth = MAASAPIAuthentication()
        auth.is_valid_request = lambda request: True
        auth.validate_token = lambda request: (mock.Mock(), mock_token, None)
        self.assertFalse(auth.is_authenticated(request))


class TestOAuthUnauthorized(MAASTestCase):
    def test_exception_unicode_includes_original_failure_message(self):
        error_msg = factory.make_name("error-message")
        original_exception = oauth.OAuthError(error_msg)
        maas_exception = OAuthUnauthorized(original_exception)
        self.assertThat(
            str(maas_exception),
            Contains("Authorization Error: %r" % error_msg),
        )

    def test_exception_unicode_includes_user_friendly_message(self):
        # When the error is an authentication error, the message is more
        # user-friendly than the default 'Invalid consumer.'.
        original_exception = oauth.OAuthError("Invalid consumer.")
        maas_exception = OAuthUnauthorized(original_exception)
        self.assertThat(
            str(maas_exception),
            Contains("Authorization Error: Invalid API key."),
        )
