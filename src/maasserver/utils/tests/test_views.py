# Copyright 2014-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for :py:module:`maasserver.utils.views`."""

__all__ = []

import http.client
import io
from itertools import count
import logging
from random import randint, random
import sys
from unittest.mock import ANY, call, sentinel
from weakref import WeakSet

from apiclient.multipart import encode_multipart_data
from django.core import signals
from django.core.handlers.wsgi import WSGIHandler, WSGIRequest
from django.db import connection
from django.http import HttpRequest, HttpResponse
from fixtures import FakeLogger
from maasserver.exceptions import MAASAPIException
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    SerializationFailureTestCase,
)
from maasserver.utils import views
from maasserver.utils.django_urls import get_resolver
from maasserver.utils.orm import (
    make_deadlock_failure,
    post_commit_hooks,
    request_transaction_retry,
    retry_context,
    validate_in_transaction,
)
from maasserver.utils.views import HttpResponseConflict
from maastesting.matchers import MockCalledOnceWith, MockCallsMatch
from maastesting.testcase import MAASTestCase
from maastesting.utils import sample_binary_data
from piston3.authentication import initialize_server_request
from piston3.models import Nonce
from testtools.matchers import Contains, Equals, HasLength, Is, IsInstance, Not
from testtools.testcase import ExpectedException
from twisted.internet.task import Clock
from twisted.web import wsgi


def make_request(env=None, oauth_env=None, missing_oauth_param=None):
    # Return a minimal WSGIRequest.
    if oauth_env is None:
        oauth_env = {}
    base_env = {
        "REQUEST_METHOD": "GET",
        "wsgi.input": wsgi._InputStream(io.BytesIO()),
        "SERVER_NAME": "server",
        "SERVER_PORT": 80,
        "HTTP_AUTHORIZATION": factory.make_oauth_header(
            missing_param=missing_oauth_param, **oauth_env
        ),
    }
    if env is not None:
        base_env.update(env)
    request = WSGIRequest(base_env)
    return request


class TestLogFunctions(MAASTestCase):
    """Tests for `log_failed_attempt` and `log_final_failed_attempt`."""

    def capture_logs(self):
        return FakeLogger(
            views.__name__,
            level=logging.DEBUG,
            format="%(levelname)s: %(message)s",
        )

    def test_log_failed_attempt_logs_warning(self):
        request = make_request()
        request.path = factory.make_name("path")
        attempt = randint(1, 10)
        elapsed = random() * 10
        remaining = random() * 10
        pause = random()

        with self.capture_logs() as logger:
            views.log_failed_attempt(
                request, attempt, elapsed, remaining, pause
            )

        self.assertEqual(
            "debug: Attempt #%d for %s failed; will retry in %.0fms (%.1fs "
            "now elapsed, %.1fs remaining)\n"
            % (attempt, request.path, pause * 1000.0, elapsed, remaining),
            logger.output,
        )

    def test_log_final_failed_attempt_logs_error(self):
        request = make_request()
        request.path = factory.make_name("path")
        attempt = randint(1, 10)
        elapsed = random() * 10

        with self.capture_logs() as logger:
            views.log_final_failed_attempt(request, attempt, elapsed)

        self.assertEqual(
            "error: Attempt #%d for %s failed; giving up (%.1fs elapsed in "
            "total)\n" % (attempt, request.path, elapsed),
            logger.output,
        )


class TestResetRequest(MAASTestCase):
    """Tests for :py:func:`maasserver.utils.views.reset_request`."""

    def test__clears_messages_from_cookies(self):
        request = make_request()
        request.COOKIES["messages"] = sentinel.messages
        request = views.reset_request(request)
        self.assertEqual({}, request.COOKIES)


class TestDeleteOAuthNonce(MAASServerTestCase):
    """Tests for :py:func:`maasserver.utils.views.delete_oauth_nonce`."""

    def test__deletes_nonce(self):
        oauth_consumer_key = factory.make_string(18)
        oauth_token = factory.make_string(18)
        oauth_nonce = randint(0, 99999)
        Nonce.objects.create(
            consumer_key=oauth_consumer_key,
            token_key=oauth_token,
            key=oauth_nonce,
        )
        oauth_env = {
            "oauth_consumer_key": oauth_consumer_key,
            "oauth_token": oauth_token,
            "oauth_nonce": oauth_nonce,
        }
        request = make_request(oauth_env=oauth_env)
        views.delete_oauth_nonce(request)
        with ExpectedException(Nonce.DoesNotExist):
            Nonce.objects.get(
                consumer_key=oauth_consumer_key,
                token_key=oauth_token,
                key=oauth_nonce,
            )

    def test__skips_missing_nonce(self):
        oauth_consumer_key = factory.make_string(18)
        oauth_token = factory.make_string(18)
        oauth_nonce = randint(0, 99999)
        oauth_env = {
            "oauth_consumer_key": oauth_consumer_key,
            "oauth_token": oauth_token,
            "oauth_nonce": oauth_nonce,
        }
        request = make_request(oauth_env=oauth_env)
        # No exception is raised.
        self.assertIsNone(views.delete_oauth_nonce(request))

    def test__skips_non_oauth_request(self):
        request = make_request(env={"HTTP_AUTHORIZATION": ""})
        # No exception is raised.
        self.assertIsNone(views.delete_oauth_nonce(request))

    def test__skips_oauth_request_with_missing_param(self):
        missing_params = ("oauth_consumer_key", "oauth_token", "oauth_nonce")
        for missing_param in missing_params:
            request = make_request(missing_oauth_param=missing_param)
            # No exception is raised.
            self.assertIsNone(views.delete_oauth_nonce(request))


class TestWebApplicationHandler(SerializationFailureTestCase):
    def setUp(self):
        super(TestWebApplicationHandler, self).setUp()
        # Wire time.sleep() directly up to clock.advance() to avoid needless
        # sleeps, and to simulate the march of time without intervention.
        clock = self.patch(views, "clock", Clock())
        self.patch(views, "sleep", clock.advance)

    def test__init_defaults(self):
        handler = views.WebApplicationHandler()
        self.expectThat(
            handler._WebApplicationHandler__retry_attempts, Equals(10)
        )
        self.expectThat(
            handler._WebApplicationHandler__retry_timeout, Equals(90)
        )
        self.expectThat(
            handler._WebApplicationHandler__retry, IsInstance(WeakSet)
        )
        self.expectThat(handler._WebApplicationHandler__retry, HasLength(0))

    def test__init_attempts_can_be_set(self):
        attempts = randint(1, 100)
        handler = views.WebApplicationHandler(attempts)
        self.expectThat(
            handler._WebApplicationHandler__retry_attempts, Equals(attempts)
        )

    def test__init_timeout_can_be_set(self):
        handler = views.WebApplicationHandler(timeout=sentinel.timeout)
        self.expectThat(
            handler._WebApplicationHandler__retry_timeout, Is(sentinel.timeout)
        )

    def test__handle_uncaught_exception_notes_serialization_failure(self):
        handler = views.WebApplicationHandler()
        request = make_request()
        request.path = factory.make_name("path")
        failure = self.capture_serialization_failure()
        response = handler.handle_uncaught_exception(
            request=request,
            resolver=get_resolver(None),
            exc_info=failure,
            reraise=False,
        )
        # HTTP 409 is returned...
        self.expectThat(response.status_code, Equals(http.client.CONFLICT))
        # ... and the response is recorded as needing a retry.
        self.expectThat(
            handler._WebApplicationHandler__retry, Contains(response)
        )

    def test__handle_uncaught_exception_does_not_note_other_failure(self):
        handler = views.WebApplicationHandler()
        request = make_request()
        request.path = factory.make_name("path")
        failure_type = factory.make_exception_type()
        failure = failure_type, failure_type(), None
        response = handler.handle_uncaught_exception(
            request=request,
            resolver=get_resolver(None),
            exc_info=failure,
            reraise=False,
        )
        # HTTP 500 is returned...
        self.expectThat(
            response.status_code, Equals(http.client.INTERNAL_SERVER_ERROR)
        )
        # ... but the response is NOT recorded as needing a retry.
        self.expectThat(
            handler._WebApplicationHandler__retry, Not(Contains(response))
        )

    def test__handle_uncaught_exception_raises_error_on_api_exception(self):
        handler = views.WebApplicationHandler()
        request = make_request()
        request.path = factory.make_name("path")

        # Capture an exc_info tuple with traceback.
        exc_type = MAASAPIException
        exc_msg = factory.make_name("message")
        try:
            raise exc_type(exc_msg)
        except exc_type:
            exc_info = sys.exc_info()

        response = handler.handle_uncaught_exception(
            request=request,
            resolver=get_resolver(None),
            exc_info=exc_info,
            reraise=False,
        )
        self.assertThat(
            response.status_code, Equals(http.client.INTERNAL_SERVER_ERROR)
        )

    def test__get_response_catches_serialization_failures(self):
        get_response = self.patch(WSGIHandler, "get_response")
        get_response.side_effect = (
            lambda request: self.cause_serialization_failure()
        )

        handler = views.WebApplicationHandler(1)
        request = make_request()
        request.path = factory.make_name("path")
        response = handler.get_response(request)

        self.assertThat(get_response, MockCalledOnceWith(request))
        self.assertThat(response, IsInstance(HttpResponseConflict))

    def test__get_response_catches_deadlock_failures(self):
        get_response = self.patch(WSGIHandler, "get_response")
        get_response.side_effect = make_deadlock_failure()

        handler = views.WebApplicationHandler(1)
        request = make_request()
        request.path = factory.make_name("path")
        response = handler.get_response(request)

        self.assertThat(get_response, MockCalledOnceWith(request))
        self.assertThat(response, IsInstance(HttpResponseConflict))

    def test__get_response_sends_signal_on_serialization_failures(self):
        get_response = self.patch(WSGIHandler, "get_response")
        get_response.side_effect = (
            lambda request: self.cause_serialization_failure()
        )

        send_request_exception = self.patch_autospec(
            signals.got_request_exception, "send"
        )

        handler = views.WebApplicationHandler(1)
        request = make_request()
        request.path = factory.make_name("path")
        handler.get_response(request)

        self.assertThat(
            send_request_exception,
            MockCalledOnceWith(
                sender=views.WebApplicationHandler, request=request
            ),
        )

    def test__get_response_sends_signal_on_deadlock_failures(self):
        get_response = self.patch(WSGIHandler, "get_response")
        get_response.side_effect = make_deadlock_failure()

        send_request_exception = self.patch_autospec(
            signals.got_request_exception, "send"
        )

        handler = views.WebApplicationHandler(1)
        request = make_request()
        request.path = factory.make_name("path")
        handler.get_response(request)

        self.assertThat(
            send_request_exception,
            MockCalledOnceWith(
                sender=views.WebApplicationHandler, request=request
            ),
        )

    def test__get_response_tries_only_once(self):
        response = HttpResponse(status=200)
        get_response = self.patch(WSGIHandler, "get_response")
        get_response.return_value = response

        handler = views.WebApplicationHandler()
        request = make_request()
        request.path = factory.make_name("path")
        observed_response = handler.get_response(request)

        self.assertThat(get_response, MockCalledOnceWith(request))
        self.assertThat(observed_response, Is(response))

    def test__get_response_tries_multiple_times(self):
        handler = views.WebApplicationHandler(3)
        # An iterable of responses, the last of which will be
        # an HttpResponseConflict (HTTP 409 - Conflict) error
        # indicating that the request reached its maximum
        # number of retry attempts.
        responses = iter(
            (
                HttpResponse(status=200),
                HttpResponse(status=200),
                HttpResponse(status=200),
            )
        )

        def set_retry(request):
            response = next(responses)
            handler._WebApplicationHandler__retry.add(response)
            return response

        get_response = self.patch(WSGIHandler, "get_response")
        get_response.side_effect = set_retry

        reset_request = self.patch_autospec(views, "reset_request")
        reset_request.side_effect = lambda request: request

        request = make_request()
        request.path = factory.make_name("path")
        response = handler.get_response(request)

        self.assertThat(
            get_response,
            MockCallsMatch(call(request), call(request), call(request)),
        )
        self.assertThat(response, IsInstance(HttpResponseConflict))
        self.expectThat(response.status_code, Equals(http.client.CONFLICT))
        self.expectThat(
            response.reason_phrase,
            Equals(http.client.responses[http.client.CONFLICT]),
        )

    def test__get_response_prepare_retry_context_before_each_try(self):
        class ObserveContext:
            def __init__(self, callback, name):
                self.callback = callback
                self.name = name

            def __enter__(self):
                self.callback("%s:enter" % self.name)

            def __exit__(self, *exc_info):
                self.callback("%s:exit" % self.name)

        counter = count(1)
        calls = []

        def get_response_and_retry(request):
            calls.append("get_response")
            request_transaction_retry(
                ObserveContext(calls.append, "context#%d" % next(counter))
            )

        get_response = self.patch(WSGIHandler, "get_response")
        get_response.side_effect = get_response_and_retry

        reset_request = self.patch_autospec(views, "reset_request")
        reset_request.side_effect = lambda request: request

        request = make_request()
        request.path = factory.make_name("path")
        handler = views.WebApplicationHandler(3)
        handler.get_response(request)

        self.assertThat(
            calls,
            Equals(
                [
                    "get_response",  # 1st attempt.
                    "context#1:enter",  # Retry requested, enter 1st new context.
                    "get_response",  # 2nd attempt.
                    "context#2:enter",  # Retry requested, enter 2nd new context.
                    "get_response",  # 3rd attempt, and last.
                    "context#2:exit",  # Exit 2nd context.
                    "context#1:exit",  # Exit 1st context.
                ]
            ),
        )

    def test__get_response_logs_retry_and_resets_request(self):
        timeout = 1.0 + (random() * 99)
        handler = views.WebApplicationHandler(2, timeout)

        def set_retry(request):
            response = HttpResponse(status=200)
            handler._WebApplicationHandler__retry.add(response)
            return response

        get_response = self.patch(WSGIHandler, "get_response")
        get_response.side_effect = set_retry

        self.patch_autospec(views, "log_failed_attempt")
        self.patch_autospec(views, "log_final_failed_attempt")
        reset_request = self.patch_autospec(views, "reset_request")
        reset_request.side_effect = lambda request: request

        request = make_request()
        request.path = factory.make_name("path")
        handler.get_response(request)

        self.expectThat(
            views.log_failed_attempt,
            MockCalledOnceWith(request, 1, ANY, ANY, ANY),
        )
        self.expectThat(
            views.log_final_failed_attempt, MockCalledOnceWith(request, 2, ANY)
        )
        self.expectThat(reset_request, MockCalledOnceWith(request))

    def test__get_response_up_calls_in_transaction(self):
        handler = views.WebApplicationHandler(2)

        def check_in_transaction(request):
            validate_in_transaction(connection)

        get_response = self.patch(WSGIHandler, "get_response")
        get_response.side_effect = check_in_transaction

        request = make_request()
        request.path = factory.make_name("path")
        handler.get_response(request)

        self.assertThat(get_response, MockCalledOnceWith(request))

    def test__get_response_is_in_retry_context_in_transaction(self):
        handler = views.WebApplicationHandler(2)

        def check_retry_context_active(request):
            self.assertThat(retry_context.active, Is(True))

        get_response = self.patch(WSGIHandler, "get_response")
        get_response.side_effect = check_retry_context_active

        request = make_request()
        request.path = factory.make_name("path")

        self.assertThat(retry_context.active, Is(False))
        handler.get_response(request)
        self.assertThat(retry_context.active, Is(False))
        self.assertThat(get_response, MockCalledOnceWith(request))

    def test__get_response_restores_files_across_requests(self):
        handler = views.WebApplicationHandler(3)
        file_content = sample_binary_data
        file_name = "content"

        recorder = []

        def get_response_read_content_files(self, request):
            # Simple get_response method which returns the 'file_name' file
            # from the request in the response.
            content = request.FILES[file_name].read()
            # Record calls.
            recorder.append(content)
            response = HttpResponse(
                content=content,
                status=200,
                content_type=b"text/plain; charset=utf-8",
            )
            handler._WebApplicationHandler__retry.add(response)
            return response

        self.patch(
            WSGIHandler, "get_response", get_response_read_content_files
        )

        body, headers = encode_multipart_data(
            [], [[file_name, io.BytesIO(file_content)]]
        )
        env = {
            "REQUEST_METHOD": "POST",
            "wsgi.input": wsgi._InputStream(io.BytesIO(body.encode("utf-8"))),
            "CONTENT_TYPE": headers["Content-Type"],
            "CONTENT_LENGTH": headers["Content-Length"],
            "HTTP_MIME_VERSION": headers["MIME-Version"],
        }
        request = make_request(env)

        response = handler.get_response(request)
        self.assertEqual(file_content, response.content)
        self.assertEqual(recorder, [file_content] * 3)

    def test__get_response_deleted_nonces_across_requests(self):
        handler = views.WebApplicationHandler(3)
        user = factory.make_User()
        token = user.userprofile.get_authorisation_tokens()[0]

        recorder = []

        def get_response_check_nonce(self, request):
            _, oauth_req = initialize_server_request(request)
            # get_or _create the Nonce object like the authentication
            # mechanism does.
            nonce_obj, created = Nonce.objects.get_or_create(
                consumer_key=token.consumer.key,
                token_key=token.key,
                key=oauth_req.get_parameter("oauth_nonce"),
            )

            # Record calls.
            recorder.append(created)
            response = HttpResponse(
                content="",
                status=200,
                content_type=b"text/plain; charset=utf-8",
            )
            handler._WebApplicationHandler__retry.add(response)
            return response

        self.patch(WSGIHandler, "get_response", get_response_check_nonce)

        oauth_env = {
            "oauth_consumer_key": token.consumer.key,
            "oauth_token": token.key,
        }
        request = make_request(oauth_env=oauth_env)

        handler.get_response(request)
        self.assertEqual(recorder, [True] * 3, "Nonce hasn't been cleaned up!")


class TestWebApplicationHandlerAtomicViews(MAASServerTestCase):
    def test__make_view_atomic_wraps_view_with_post_commit_savepoint(self):
        hooks = post_commit_hooks.hooks
        savepoint_level = len(connection.savepoint_ids)

        def view(*args, **kwargs):
            # We're one more savepoint in.
            self.assertThat(
                connection.savepoint_ids, HasLength(savepoint_level + 1)
            )
            # Post-commit hooks have been saved.
            self.assertThat(post_commit_hooks.hooks, Not(Is(hooks)))
            # Return the args we were given.
            return args, kwargs

        handler = views.WebApplicationHandler()
        view_atomic = handler.make_view_atomic(view)

        self.assertThat(post_commit_hooks.hooks, Is(hooks))
        self.assertThat(
            view_atomic(sentinel.arg, kwarg=sentinel.kwarg),
            Equals(((sentinel.arg,), {"kwarg": sentinel.kwarg})),
        )
        self.assertThat(post_commit_hooks.hooks, Is(hooks))


class TestRequestHeaders(MAASTestCase):
    def test_headers(self):
        request = HttpRequest()
        request.META.update(
            {"HTTP_HOST": "www.example.com", "HTTP_CONTENT_TYPE": "text/plain"}
        )
        self.assertEqual(
            views.request_headers(request),
            {"host": "www.example.com", "content-type": "text/plain"},
        )

    def test_non_http_headers_ignored(self):
        request = HttpRequest()
        request.META.update(
            {"HTTP_HOST": "www.example.com", "SERVER_NAME": "myserver"}
        )
        self.assertEqual(
            views.request_headers(request), {"host": "www.example.com"}
        )

    def test_case_insensitive(self):
        request = HttpRequest()
        request.META["HTTP_CONTENT_TYPE"] = "text/plain"
        headers = views.request_headers(request)
        self.assertEqual(headers["Content-type"], "text/plain")
