# Copyright 2012-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Testing helpers for `provisioningserver.logger`."""

__all__ = [
    "find_log_lines",
    "make_event",
    "make_log_text",
    "pick_log_level",
    "pick_log_time",
]

import random
import re
import time

from maastesting.factory import factory
from twisted.logger import LogLevel


def make_event(log_text=None, log_level=None, log_time=None, **other):
    """Make a minimal event dict for use with Twisted."""
    event = {
        "log_format": "{log_text}",
        "log_level": pick_log_level() if log_level is None else log_level,
        "log_text": make_log_text() if log_text is None else log_text,
        "log_time": pick_log_time() if log_time is None else log_time,
    }
    event.update(other)
    return event


def make_log_text():
    """Make some random log text."""
    return factory.make_unicode_string(size=50, spaces=True)


_log_levels = tuple(LogLevel.iterconstants())


def pick_log_level():
    """Pick a random `LogLevel`."""
    return random.choice(_log_levels)


def pick_log_time(noise=float(60 * 60)):
    """Pick a random time based on now, but with some noise."""
    return time.time() + (random.random() * noise) - (noise / 2)


# Matches lines like: 2016-10-18 14:23:55 namespace: [level] message
find_log_lines_re = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*?): [[](.*)[]] (.*)$",
    re.MULTILINE,
)


def find_log_lines(text):
    """Find logs in `text` that match `find_log_lines_re`.

    Checks for well-formed date/times but throws them away.
    """
    return [
        (ns, level, line)
        for (ts, ns, level, line) in find_log_lines_re.findall(text)
    ]
