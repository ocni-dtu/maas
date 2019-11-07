# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for Twisted-specific logging stuff."""

__all__ = []

import io

from maastesting.factory import factory
from maastesting.testcase import MAASTestCase
from maastesting.twisted import TwistedLoggerFixture
from provisioningserver.logger import _twisted
from provisioningserver.logger._common import DEFAULT_LOG_FORMAT_DATE
from provisioningserver.logger._twisted import (
    _formatModernEvent,
    _getCommandName,
    _getSystemName,
    EventLogger,
    LegacyLogger,
    observe_twisted_internet_tcp,
    observe_twisted_internet_udp,
    observe_twisted_internet_unix,
)
from provisioningserver.logger.testing import (
    find_log_lines,
    make_event,
    pick_log_time,
)
from testtools.matchers import (
    AfterPreprocessing,
    Contains,
    ContainsDict,
    Equals,
    HasLength,
    Is,
    IsInstance,
    MatchesAll,
    MatchesDict,
    StartsWith,
)
from twisted import logger
from twisted.python.failure import Failure


def ContainsDictByEquality(expected):
    return ContainsDict(
        {key: Equals(value) for key, value in expected.items()}
    )


def formatTimeStatic(when):
    """Just return <when>."""
    return "<when>"


class TestLegacyLogger(MAASTestCase):
    def test__logs_messages(self):
        events = []
        namespace = factory.make_name("namespace")
        legacy_logger = LegacyLogger(namespace, self, events.append)
        message = factory.make_name("message")
        keywords = {
            factory.make_name("key"): factory.make_name("value")
            for _ in range(3)
        }

        legacy_logger.msg(message, **keywords)

        expected = {
            "log_format": Equals("{_message_0}"),
            "log_level": Equals(logger.LogLevel.info),
            "log_logger": Is(legacy_logger),
            "log_namespace": Equals(namespace),
            "log_source": Is(self),
            "log_time": IsInstance(float),
            "_message_0": Equals(message),
        }
        expected.update(
            {key: Equals(value) for key, value in keywords.items()}
        )
        self.assertThat(events, HasLength(1))
        self.assertThat(events[0], MatchesDict(expected))
        self.assertThat(
            logger.formatEventAsClassicLogText(events[0], formatTimeStatic),
            Equals("<when> [%s#info] %s\n" % (namespace, message)),
        )

    def test__logs_multiple_messages(self):
        events = []
        legacy_logger = LegacyLogger(observer=events.append)
        messages = [
            factory.make_name("message"),
            factory.make_name("message"),
            factory.make_name("message"),
        ]

        legacy_logger.msg(*messages)

        expected = {
            "_message_0": messages[0],
            "_message_1": messages[1],
            "_message_2": messages[2],
            "log_format": "{_message_0} {_message_1} {_message_2}",
        }
        self.assertThat(events, HasLength(1))
        self.assertThat(events[0], ContainsDictByEquality(expected))
        self.assertThat(
            logger.formatEventAsClassicLogText(events[0], formatTimeStatic),
            Equals("<when> [%s#info] %s\n" % (__name__, " ".join(messages))),
        )

    def test__logs_errors(self):
        events = []
        namespace = factory.make_name("namespace")
        legacy_logger = LegacyLogger(namespace, self, events.append)
        message = factory.make_name("message")
        exception_type = factory.make_exception_type()
        keywords = {
            factory.make_name("key"): factory.make_name("value")
            for _ in range(3)
        }

        try:
            raise exception_type()
        except exception_type:
            legacy_logger.err(None, message, **keywords)

        expected = {
            "log_failure": MatchesAll(
                IsInstance(Failure),
                AfterPreprocessing(
                    (lambda failure: failure.value), IsInstance(exception_type)
                ),
            ),
            "log_format": Equals("{_why}"),
            "log_level": Equals(logger.LogLevel.critical),
            "log_logger": Is(legacy_logger),
            "log_namespace": Equals(namespace),
            "log_source": Is(self),
            "log_time": IsInstance(float),
            "_why": Equals(message),
        }
        expected.update(
            {key: Equals(value) for key, value in keywords.items()}
        )
        self.assertThat(events, HasLength(1))
        self.assertThat(events[0], MatchesDict(expected))
        # Twisted 16.6.0 (see issue #8858) now includes a traceback,
        # so we only match on the beginning of the string.
        self.assertThat(
            logger.formatEventAsClassicLogText(events[0], formatTimeStatic),
            StartsWith("<when> [%s#critical] %s\n" % (namespace, message)),
        )


class TestObserveTwistedInternetTCP(MAASTestCase):
    """Tests for `observe_twisted_internet_tcp`."""

    def test__ignores_port_closed_events(self):
        event = make_event(
            "(%s Port %d Closed)"
            % (factory.make_name("port-name"), factory.pick_port())
        )
        with TwistedLoggerFixture() as logger:
            observe_twisted_internet_tcp(event)
        self.assertThat(logger.events, HasLength(0))

    def test__ignores_protocol_starting_on_events(self):
        event = make_event(
            "%s starting on %d"
            % (factory.make_name("protocol"), factory.pick_port())
        )
        with TwistedLoggerFixture() as logger:
            observe_twisted_internet_tcp(event)
        self.assertThat(logger.events, HasLength(0))

    def test__propagates_other_events(self):
        event = make_event(factory.make_name("something"))
        with TwistedLoggerFixture() as logger:
            observe_twisted_internet_tcp(event)
        self.assertThat(logger.events, Contains(event))


class TestObserveTwistedInternetUDP(MAASTestCase):
    """Tests for `observe_twisted_internet_udp`."""

    def test__ignores_port_closed_events(self):
        event = make_event(
            "(%s Port %d Closed)"
            % (factory.make_name("port-name"), factory.pick_port())
        )
        with TwistedLoggerFixture() as logger:
            observe_twisted_internet_udp(event)
        self.assertThat(logger.events, HasLength(0))

    def test__ignores_protocol_starting_on_events(self):
        event = make_event(
            "%s starting on %d"
            % (factory.make_name("protocol"), factory.pick_port())
        )
        with TwistedLoggerFixture() as logger:
            observe_twisted_internet_udp(event)
        self.assertThat(logger.events, HasLength(0))

    def test__propagates_other_events(self):
        event = make_event(factory.make_name("something"))
        with TwistedLoggerFixture() as logger:
            observe_twisted_internet_udp(event)
        self.assertThat(logger.events, Contains(event))


class TestObserveTwistedInternetUNIX(MAASTestCase):
    """Tests for `observe_twisted_internet_unix`."""

    def test__ignores_port_closed_events(self):
        event = make_event(
            "(%s Port %r Closed)"
            % (factory.make_name("port-name"), factory.make_name("port"))
        )
        with TwistedLoggerFixture() as logger:
            observe_twisted_internet_unix(event)
        self.assertThat(logger.events, HasLength(0))

    def test__ignores_protocol_starting_on_events(self):
        event = make_event(
            "%s starting on %d"
            % (factory.make_name("protocol"), factory.pick_port())
        )
        with TwistedLoggerFixture() as logger:
            observe_twisted_internet_unix(event)
        self.assertThat(logger.events, HasLength(0))

    def test__propagates_other_events(self):
        event = make_event(factory.make_name("something"))
        with TwistedLoggerFixture() as logger:
            observe_twisted_internet_unix(event)
        self.assertThat(logger.events, Contains(event))


class TestGetSystemName(MAASTestCase):
    """Tests for `_getSystemName`."""

    expectations = {
        "foo.bar.baz": "foo.bar.baz",
        "f_o.bar.baz": "f_o.bar.baz",
        "foo.b_r.baz": "foo.b_r.baz",
        "foo.bar.b_z": "foo.bar.b_z",
        "foo.bar._az": "foo.bar",
        "foo._ar.baz": "foo",
        "foo._ar._az": "foo",
        "_oo.bar.baz": None,
        "_": None,
        "": None,
        None: None,
    }

    scenarios = tuple(
        (
            "%s => %s"
            % (string_in or repr(string_in), string_out or repr(string_out)),
            {"string_in": string_in, "string_out": string_out},
        )
        for string_in, string_out in expectations.items()
    )

    def test(self):
        self.assertThat(
            _getSystemName(self.string_in), Equals(self.string_out)
        )


class TestGetCommandName(MAASTestCase):
    """Tests for `_getCommandName`."""

    expectations = {
        # maas-rackd
        (
            "/usr/bin/twistd3",
            "--nodaemon",
            "--pidfile=",
            "--logger=provisioningserver.logger.EventLogger",
            "maas-rackd",
        ): "rackd",
        # maas-regiond
        (
            "twistd3",
            "--nodaemon",
            "--pidfile=",
            "--logger=provisioningserver.logger.EventLogger",
            "maas-regiond",
        ): "regiond",
        # twistd running ...
        ("twistd3", "--an-option", "something-else"): "daemon",
        # command
        ("some-command",): "some-command",
        # command with .py suffix
        ("some-command-with-suffix.py",): "some-command-with-suffix",
        # command running under python
        ("python", "py-command"): "py-command",
        # command running under python with .py suffix
        ("python", "py-command-with-suffix.py"): "py-command-with-suffix",
        # something else
        ("python", "-c", "print('woo')"): "command",
    }

    scenarios = tuple(
        (expected, {"argv": argv, "expected": expected})
        for argv, expected in expectations.items()
    )

    def test(self):
        self.assertThat(_getCommandName(self.argv), Equals(self.expected))


class TestFormatModernEvent(MAASTestCase):
    """Tests for `_formatModernEvent`."""

    scenarios = tuple(
        (level.name, {"log_level": level})
        for level in logger.LogLevel.iterconstants()
    )

    def test_format_basics(self):
        thing1 = factory.make_name("thing")
        thing2 = factory.make_name("thing")
        log_system = factory.make_name("system")
        log_format = ">{thing1}< >{thing2}<"
        log_time = pick_log_time()
        self.assertThat(
            _formatModernEvent(
                {
                    "log_time": log_time,
                    "log_format": log_format,
                    "log_system": log_system,
                    "log_level": self.log_level,
                    "thing1": thing1,
                    "thing2": thing2,
                }
            ),
            Equals(
                "%s %s: [%s] >%s< >%s<\n"
                % (
                    logger.formatTime(log_time, DEFAULT_LOG_FORMAT_DATE),
                    log_system,
                    self.log_level.name,
                    thing1,
                    thing2,
                )
            ),
        )

    def test_format_failure(self):
        try:
            1 / 0
        except ZeroDivisionError:
            failure = Failure()
        else:
            raise RuntimeError("should have raised ZeroDivisionError")
        log_system = factory.make_name("system")
        log_time = pick_log_time()
        self.assertThat(
            _formatModernEvent(
                {
                    "log_time": log_time,
                    "log_system": log_system,
                    "log_level": self.log_level,
                    "log_failure": failure,
                }
            ),
            Equals(
                "%s %s: [%s] \n\t%s\n"
                % (
                    logger.formatTime(log_time, DEFAULT_LOG_FORMAT_DATE),
                    log_system,
                    self.log_level.name,
                    failure.getTraceback().replace("\n", "\n\t"),
                )
            ),
        )

    def test_formats_without_format(self):
        self.assertThat(
            _formatModernEvent({"log_level": self.log_level}),
            Equals("- -: [%s] \n" % self.log_level.name),
        )

    def test_formats_with_null_format(self):
        self.assertThat(
            _formatModernEvent(
                {"log_format": None, "log_level": self.log_level}
            ),
            Equals("- -: [%s] \n" % self.log_level.name),
        )

    def test_formats_without_time(self):
        self.assertThat(
            _formatModernEvent({"log_level": self.log_level}),
            Equals("- -: [%s] \n" % self.log_level.name),
        )

    def test_formats_with_null_time(self):
        self.assertThat(
            _formatModernEvent(
                {"log_time": None, "log_level": self.log_level}
            ),
            Equals("- -: [%s] \n" % self.log_level.name),
        )

    def test_uses_namespace_if_system_missing(self):
        log_namespace = factory.make_name("namespace")
        self.assertThat(
            _formatModernEvent(
                {"log_level": self.log_level, "log_namespace": log_namespace}
            ),
            Equals("- %s: [%s] \n" % (log_namespace, self.log_level.name)),
        )

    def test_uses_namespace_if_system_null(self):
        log_namespace = factory.make_name("namespace")
        self.assertThat(
            _formatModernEvent(
                {
                    "log_level": self.log_level,
                    "log_namespace": log_namespace,
                    "log_system": None,
                }
            ),
            Equals("- %s: [%s] \n" % (log_namespace, self.log_level.name)),
        )


class TestEventLogger(MAASTestCase):
    """Tests for `EventLogger`."""

    scenarios = tuple(
        (level.name, {"log_level": level})
        for level in logger.LogLevel.iterconstants()
    )

    def setUp(self):
        super(TestEventLogger, self).setUp()
        self.output = io.StringIO()
        self.log = EventLogger(self.output)
        self.get_logs = lambda: find_log_lines(self.output.getvalue())

    def setLogLevel(self, log_level):
        """Set the level at which events will be logged.

        This is not a minimum level, it is an absolute level.
        """
        self.patch(_twisted, "_filterByLevels", {self.log_level})

    def test_basics(self):
        self.setLogLevel(self.log_level)
        event = make_event(log_level=self.log_level)
        event["log_system"] = factory.make_name("system")
        self.log(event)
        self.assertSequenceEqual(
            [(event["log_system"], self.log_level.name, event["log_text"])],
            self.get_logs(),
        )

    def test_filters_by_level(self):
        self.setLogLevel(self.log_level)
        events = {
            log_level: make_event(log_level=log_level)
            for log_level in logger.LogLevel.iterconstants()
        }
        for event in events.values():
            self.log(event)
        # Only the log at the current level will get through.
        self.assertSequenceEqual(
            [("-", self.log_level.name, events[self.log_level]["log_text"])],
            self.get_logs(),
        )

    def test_filters_by_noise(self):
        self.setLogLevel(self.log_level)
        common = dict(log_namespace="log_legacy", log_system="-")
        noisy = [
            make_event("Log opened.", **common),
            make_event("Main loop terminated.", **common),
        ]
        for event in noisy:
            self.log(event)
        okay = [make_event(log_level=self.log_level, **common)]
        for event in okay:
            self.log(event)
        # Only the `okay` logs will get through.
        expected = [
            ("-", self.log_level.name, event["log_text"]) for event in okay
        ]
        self.assertSequenceEqual(expected, self.get_logs())
