# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maascli.utils`."""

__all__ = []

import collections
import http.client
import io
import random
from unittest.mock import sentinel

import httplib2
from maascli import utils
from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase
from testtools.matchers import AfterPreprocessing, Equals, MatchesListwise


class TestDocstringParsing(MAASTestCase):
    """Tests for docstring parsing in `maascli.utils`."""

    def test_basic(self):
        self.assertEqual(
            ("Title", "Body"), utils.parse_docstring("Title\n\nBody")
        )
        self.assertEqual(
            ("A longer title", "A longer body"),
            utils.parse_docstring("A longer title\n\nA longer body"),
        )

    def test_no_body(self):
        # parse_docstring returns an empty string when there's no body.
        self.assertEqual(("Title", ""), utils.parse_docstring("Title\n\n"))
        self.assertEqual(("Title", ""), utils.parse_docstring("Title"))

    def test_unwrapping(self):
        # parse_docstring unwraps the title paragraph, and dedents the body
        # paragraphs.
        self.assertEqual(
            (
                "Title over two lines",
                "Paragraph over\ntwo lines\n\n"
                "Another paragraph\nover two lines",
            ),
            utils.parse_docstring(
                """
                Title over
                two lines

                Paragraph over
                two lines

                Another paragraph
                over two lines
                """
            ),
        )

    def test_gets_docstring_from_function(self):
        # parse_docstring can extract the docstring when the argument passed
        # is not a string type.
        def example():
            """Title.

            Body.
            """

        self.assertEqual(("Title.", "Body."), utils.parse_docstring(example))

    def test_normalises_whitespace(self):
        # parse_docstring can parse CRLF/CR/LF text, but always emits LF (\n,
        # new-line) separated text.
        self.assertEqual(
            ("long title", ""), utils.parse_docstring("long\r\ntitle")
        )
        self.assertEqual(
            ("title", "body1\n\nbody2"),
            utils.parse_docstring("title\n\nbody1\r\rbody2"),
        )


class TestFunctions(MAASTestCase):
    """Tests for miscellaneous functions in `maascli.utils`."""

    def test_safe_name(self):
        # safe_name attempts to discriminate parts of a vaguely camel-cased
        # string, and rejoins them using a hyphen.
        expected = {
            "NodeHandler": "Node-Handler",
            "SpadeDiggingHandler": "Spade-Digging-Handler",
            "SPADE_Digging_Handler": "SPADE-Digging-Handler",
            "SpadeHandlerForDigging": "Spade-Handler-For-Digging",
            "JamesBond007": "James-Bond007",
            "JamesBOND": "James-BOND",
            "James-BOND-007": "James-BOND-007",
        }
        observed = {name_in: utils.safe_name(name_in) for name_in in expected}
        self.assertItemsEqual(expected.items(), observed.items())

    def test_safe_name_non_ASCII(self):
        # safe_name will not break if passed a string with non-ASCII
        # characters. However, those characters will not be present in the
        # returned name.
        self.assertEqual("a-b-c", utils.safe_name("a\u1234_b\u5432_c\u9876"))

    def test_handler_command_name(self):
        # handler_command_name attempts to discriminate parts of a vaguely
        # camel-cased string, removes any "handler" parts, joins again with
        # hyphens, and returns the whole lot in lower case.
        expected = {
            "NodeHandler": "node",
            "SpadeDiggingHandler": "spade-digging",
            "SPADE_Digging_Handler": "spade-digging",
            "SpadeHandlerForDigging": "spade-for-digging",
            "JamesBond007": "james-bond007",
            "JamesBOND": "james-bond",
            "James-BOND-007": "james-bond-007",
        }
        observed = {
            name_in: utils.handler_command_name(name_in)
            for name_in in expected
        }
        self.assertItemsEqual(expected.items(), observed.items())

    def test_handler_command_name_non_ASCII(self):
        # handler_command_name will not break if passed a string with
        # non-ASCII characters. However, those characters will not be present
        # in the returned name.
        self.assertEqual(
            "a-b-c", utils.handler_command_name("a\u1234_b\u5432_c\u9876")
        )

    def test_ensure_trailing_slash(self):
        # ensure_trailing_slash ensures that the given string - typically a
        # URL or path - has a trailing forward slash.
        self.assertEqual("fred/", utils.ensure_trailing_slash("fred"))
        self.assertEqual("fred/", utils.ensure_trailing_slash("fred/"))

    def test_ensure_trailing_slash_string_type(self):
        # Given a unicode string, ensure_trailing_slash will always return a
        # unicode string, and given a byte string it will always return a byte
        # string.
        self.assertIsInstance(utils.ensure_trailing_slash("fred"), str)
        self.assertIsInstance(utils.ensure_trailing_slash(b"fred"), bytes)

    def test_api_url(self):
        transformations = list(
            {
                "http://example.com/": "http://example.com/api/2.0/",
                "http://example.com/foo": "http://example.com/foo/api/2.0/",
                "http://example.com/foo/": "http://example.com/foo/api/2.0/",
                "http://example.com/api/7.9": "http://example.com/api/7.9/",
                "http://example.com/api/7.9/": "http://example.com/api/7.9/",
            }.items()
        )
        urls = [url for url, url_out in transformations]
        urls_out = [url_out for url, url_out in transformations]
        expected = [
            AfterPreprocessing(utils.api_url, Equals(url_out))
            for url_out in urls_out
        ]
        self.assertThat(urls, MatchesListwise(expected))


class TestGetResponseContentType(MAASTestCase):
    """Tests for `get_response_content_type`."""

    def test_get_response_content_type_returns_content_type_header(self):
        response = httplib2.Response({"content-type": "application/json"})
        self.assertEqual(
            "application/json", utils.get_response_content_type(response)
        )

    def test_get_response_content_type_omits_parameters(self):
        response = httplib2.Response(
            {"content-type": "application/json; charset=utf-8"}
        )
        self.assertEqual(
            "application/json", utils.get_response_content_type(response)
        )

    def test_get_response_content_type_return_None_when_type_not_found(self):
        response = httplib2.Response({})
        self.assertIsNone(utils.get_response_content_type(response))


class TestIsResponseTextual(MAASTestCase):
    """Tests for `is_response_textual`."""

    content_types_textual_map = {
        "text/plain": True,
        "text/yaml": True,
        "text/foobar": True,
        "application/json": True,
        "image/png": False,
        "video/webm": False,
    }

    scenarios = sorted(
        (ctype, {"content_type": ctype, "is_textual": is_textual})
        for ctype, is_textual in content_types_textual_map.items()
    )

    def test_type(self):
        grct = self.patch(utils, "get_response_content_type")
        grct.return_value = self.content_type
        self.assertEqual(
            self.is_textual, utils.is_response_textual(sentinel.response)
        )
        self.assertThat(grct, MockCalledOnceWith(sentinel.response))


class TestPrintResponseHeaders(MAASTestCase):
    """Tests for `print_response_headers`."""

    def test__prints_http_headers_in_order(self):
        # print_response_headers() prints the given headers, in order, with
        # each hyphen-delimited part of the header name capitalised, to the
        # given file, with the names right aligned, and with a 2 space left
        # margin.
        headers = collections.OrderedDict()
        headers["two-two"] = factory.make_name("two")
        headers["one"] = factory.make_name("one")
        buf = io.StringIO()
        utils.print_response_headers(headers, buf)
        self.assertEqual(
            ("      One: %(one)s\n" "  Two-Two: %(two-two)s\n") % headers,
            buf.getvalue(),
        )


class TestPrintResponseContent(MAASTestCase):
    """Tests for `print_response_content`."""

    def test__prints_textual_response_with_newline(self):
        # If the response content is textual and sys.stdout is connected to a
        # TTY, print_response_content() prints the response with a trailing
        # newline.
        response = httplib2.Response(
            {
                "status": http.client.NOT_FOUND,
                "content": b"Lorem ipsum dolor sit amet.",
                "content-type": "text/unicode",
            }
        )
        buf = io.BytesIO()
        self.patch(buf, "isatty").return_value = True
        utils.print_response_content(response, response["content"], buf)
        self.assertEqual(response["content"] + b"\n", buf.getvalue())

    def test__prints_textual_response_when_redirected(self):
        # If the response content is textual and sys.stdout is not connected
        # to a TTY, print_response_content() prints the response without a
        # trailing newline.
        response = httplib2.Response(
            {
                "status": http.client.FOUND,
                "content": b"Lorem ipsum dolor sit amet.",
                "content-type": "text/unicode",
            }
        )
        buf = io.BytesIO()
        utils.print_response_content(response, response["content"], buf)
        self.assertEqual(response["content"], buf.getvalue())

    def test__writes_binary_response(self):
        # Non-textual response content is written to the output stream
        # using write(), so it carries no trailing newline, even if
        # stdout is connected to a tty
        response = httplib2.Response(
            {
                "content": b"Lorem ipsum dolor sit amet.",
                "content-type": "image/jpeg",
            }
        )
        buf = io.BytesIO()
        self.patch(buf, "isatty").return_value = True
        utils.print_response_content(response, response["content"], buf)
        self.assertEqual(response["content"], buf.getvalue())

    def test__prints_textual_response_with_success_msg(self):
        # When the response has a status code of 2XX, and the response body is
        # textual print_response_content() will print a success message to the
        # TTY.
        status_code = random.randrange(200, 300)
        response = httplib2.Response(
            {
                "status": status_code,
                "content": b"Lorem ipsum dolor sit amet.",
                "content-type": "text/unicode",
            }
        )
        buf = io.BytesIO()
        self.patch(buf, "isatty").return_value = True
        utils.print_response_content(response, response["content"], buf)
        self.assertEqual(
            b"Success.\n"
            b"Machine-readable output follows:\n"
            + response["content"]
            + b"\n",
            buf.getvalue(),
        )
