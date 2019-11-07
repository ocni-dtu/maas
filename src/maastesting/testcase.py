# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test related classes and functions for MAAS and its applications."""

__all__ = ["MAASRunTest", "MAASTestCase", "MAASTwistedRunTest"]

import abc
from collections import Mapping, Sequence
from contextlib import contextmanager
from functools import wraps
from importlib import import_module
import os
from unittest import mock
from unittest.mock import MagicMock

import crochet
from maastesting.crochet import EventualResultCatchingMixin
from maastesting.factory import factory
from maastesting.fixtures import MAASRootFixture, TempDirectory
from maastesting.matchers import DocTestMatches
from maastesting.runtest import (
    MAASCrochetRunTest,
    MAASRunTest,
    MAASTwistedRunTest,
)
from maastesting.scenarios import WithScenarios
from maastesting.twisted import TwistedLoggerFixture
from nose.proxy import ResultProxy
from nose.tools import nottest
import testresources
import testtools
import testtools.matchers
from testtools.matchers import AllMatch, IsInstance, Not


@nottest
@contextmanager
def active_test(result, test):
    """Force nose to report for the test that's running.

    Nose presents a proxy result and passes on results using only the
    top-level test, rather than the actual running test. This attempts to undo
    this dubious choice.

    If the result is not a nose proxy then this is a no-op.
    """
    if isinstance(result, ResultProxy):
        orig = result.test.test
        result.test.test = test
        try:
            yield
        finally:
            result.test.test = orig
    else:
        yield


class MAASTestType(abc.ABCMeta):
    """Base type for MAAS's test cases.

    Its only task at present is to ensure that `scenarios` is defined as a
    sequence. If not, for example it might be defined using a generator, it is
    coerced into a sequence.

    No attempt is made to suppress exceptions arising from this coercion, so
    failures are early and loud.

    Coercing generators is valuable because the use of these for scenarios can
    result in strange behaviour that doesn't obviously point to the cause.

    An alternative might be to reject non-sequences, but it seems we can
    safely handle them here just as well. Now that the issue is known, and a
    mechanism is in place to deal with it, we can easily change the policy
    later on.

    """

    def __new__(meta, name, bases, attrs):
        scenarios = attrs.get("scenarios")
        if scenarios is not None:
            if not isinstance(scenarios, Sequence):
                scenarios = attrs["scenarios"] = tuple(scenarios)
            if len(scenarios) == 0:
                scenarios = attrs["scenarios"] = None
        return super(MAASTestType, meta).__new__(meta, name, bases, attrs)


class MAASTestCase(
    WithScenarios,
    EventualResultCatchingMixin,
    testtools.TestCase,
    metaclass=MAASTestType,
):
    """Base `TestCase` for MAAS.

    Supports `test resources`_, `test scenarios`_, and `fixtures`_.

    .. _test resources: https://launchpad.net/testresources

    .. _test scenarios: https://launchpad.net/testscenarios

    .. _fixtures: https://launchpad.net/python-fixtures

    """

    # Allow testtools to generate longer diffs when tests fail.
    maxDiff = testtools.TestCase.maxDiff * 3

    # testresources.ResourcedTestCase does something similar to this class
    # (with respect to setUpResources and tearDownResources) but it explicitly
    # up-calls to unittest.TestCase instead of using super() even though it is
    # not guaranteed that the next class in the inheritance chain is
    # unittest.TestCase.

    resources = (
        # (resource-name, resource),
    )

    scenarios = (
        # (scenario-name, {instance-attribute-name: value, ...}),
    )

    # The database may NOT be used in tests. See `checkDatabaseUse`. Use a
    # subclass like `MAASServerTestCase` or `MAASTransactionalServerTestCase`
    # instead, which will manage the database and transactions correctly.
    database_use_possible = "DJANGO_SETTINGS_MODULE" in os.environ
    database_use_permitted = False

    @property
    def run_tests_with(self):
        """Use a customised executor.

        If crochet is managing the Twisted reactor, use `MAASCrochetRunTest`,
        otherwise default to `MAASRunTest`.
        """
        try:
            watchdog = crochet._watchdog
        except AttributeError:
            return MAASRunTest
        else:
            if watchdog.is_alive():
                return MAASCrochetRunTest
            else:
                return MAASRunTest

    def setUp(self):
        # Every test gets a pristine MAAS_ROOT, when it is defined.
        if "MAAS_ROOT" in os.environ:
            self.useFixture(MAASRootFixture())

        # Capture Twisted logs and add them as a test detail.
        twistedLog = self.useFixture(TwistedLoggerFixture())
        self.addDetail("Twisted logs", twistedLog.getContent())

        self.maybeCloseDatabaseConnections()
        super(MAASTestCase, self).setUp()
        self.setUpResources()
        self.addCleanup(self.tearDownResources)

    def setUpResources(self):
        testresources.setUpResources(
            self, self.resources, testresources._get_result()
        )

    def tearDown(self):
        super(MAASTestCase, self).tearDown()
        self.checkDatabaseUse()

    def maybeCloseDatabaseConnections(self):
        """Close database connections if their use is not permitted."""
        if self.database_use_possible and not self.database_use_permitted:
            from django.db import connection

            connection.close()

    def checkDatabaseUse(self):
        """Enforce `database_use_permitted`."""
        if self.database_use_possible and not self.database_use_permitted:
            from django.db import connection

            self.expectThat(
                connection.connection,
                testtools.matchers.Is(None),
                "Test policy forbids use of the database.",
            )
            connection.close()

    def tearDownResources(self):
        testresources.tearDownResources(
            self, self.resources, testresources._get_result()
        )

    def make_dir(self):
        """Create a temporary directory.

        This is a convenience wrapper around a fixture incantation.  That's
        the only reason why it's on the test case and not in a factory.
        """
        return self.useFixture(TempDirectory()).path

    def make_file(self, name=None, contents=None):
        """Create, and write to, a file.

        This is a convenience wrapper around `make_dir` and a factory
        call.  It ensures that the file is in a directory that will be
        cleaned up at the end of the test.
        """
        return factory.make_file(self.make_dir(), name, contents)

    @wraps(testtools.TestCase.assertItemsEqual)
    def assertItemsEqual(self, expected_seq, actual_seq, msg=None):
        """Override testtools' version to prevent use of mappings."""
        self.assertThat(
            (expected_seq, actual_seq),
            AllMatch(Not(IsInstance(Mapping))),
            "Mappings cannot be compared with assertItemsEqual",
        )
        return super().assertItemsEqual(expected_seq, actual_seq, msg)

    @wraps(testtools.TestCase.assertSequenceEqual)
    def assertSequenceEqual(self, seq1, seq2, msg=None, seq_type=None):
        """Override testtools' version to prevent use of mappings."""
        if seq_type is None:
            self.assertThat(
                (seq1, seq2),
                AllMatch(Not(IsInstance(Mapping))),
                "Mappings cannot be compared with assertSequenceEqual",
            )
        return super().assertSequenceEqual(seq1, seq2, msg, seq_type)

    def assertAttributes(self, tested_object, attributes):
        """Check multiple attributes of `tested_object` against a dict.

        :param tested_object: Any object whose attributes should be checked.
        :param attributes: A dict of attributes to test, and their expected
            values.  Only these attributes will be checked.
        """
        matcher = testtools.matchers.MatchesStructure.byEquality(**attributes)
        self.assertThat(tested_object, matcher)

    def assertDocTestMatches(
        self, expected, observed, flags=DocTestMatches.DEFAULT_FLAGS
    ):
        """See if `observed` matches `expected`, a doctest sample.

        By default uses the doctest flags `NORMALIZE_WHITESPACE` and
        `ELLIPSIS`.
        """
        self.assertThat(observed, DocTestMatches(expected, flags))

    def assertIdentical(self, expected, observed, msg=None):
        """Check if `expected` is `observed`.

        This is an obect-identity-equality test, not an object equality
        (i.e. __eq__) test.
        """
        if expected is not observed:
            raise self.failureException(
                msg or "%r is not %r" % (expected, observed)
            )

    def assertNotIdentical(self, expected, observed, msg=None):
        """Check if `expected` is not `observed`.

        This is an obect-identity-equality test, not an object equality
        (i.e. __eq__) test.
        """
        if expected is observed:
            raise self.failureException(
                msg or "%r is %r" % (expected, observed)
            )

    def run(self, result=None):
        with active_test(result, self):
            super(MAASTestCase, self).run(result)

    def __call__(self, result=None):
        with active_test(result, self):
            super(MAASTestCase, self).__call__(result)

    def patch(
        self, obj, attribute=None, value=mock.sentinel.unset
    ) -> MagicMock:
        """Patch `obj.attribute` with `value`.

        If `value` is unspecified, a new `MagicMock` will be created and
        patched-in instead. Its ``__name__`` attribute will be set to
        `attribute` or the ``__name__`` of the replaced object if `attribute`
        is not given.

        This is a thin customisation of `testtools.TestCase.patch`, so refer
        to that in case of doubt.

        :return: The patched-in object.
        """
        # If 'attribute' is None, assume 'obj' is a 'fully-qualified' object,
        # and assume that its __module__ is what we want to patch. For more
        # complex use cases, the two-parameter 'patch' will still need to
        # be used.
        if attribute is None:
            attribute = obj.__name__
            obj = import_module(obj.__module__)
        if value is mock.sentinel.unset:
            value = MagicMock(__name__=attribute)
        super(MAASTestCase, self).patch(obj, attribute, value)
        return value

    def patch_autospec(
        self, obj, attribute, spec_set=False, instance=False
    ) -> MagicMock:
        """Patch `obj.attribute` with an auto-spec of itself.

        See `mock.create_autospec` and `patch`.

        :return: The patched-in object.
        """
        spec = getattr(obj, attribute)
        if isinstance(spec, mock.Base):
            raise TypeError(
                "Cannot use a mock object as a specification: %s.%s = %r"
                % (_get_name(obj), attribute, spec)
            )
        value = mock.create_autospec(spec, spec_set, instance)
        super(MAASTestCase, self).patch(obj, attribute, value)
        return value


def _get_name(thing):
    """Return a "nice" name for `thing`."""
    try:
        return thing.__qualname__
    except AttributeError:
        try:
            return thing.__name__
        except AttributeError:
            return repr(thing)
