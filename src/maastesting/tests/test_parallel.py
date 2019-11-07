# Copyright 2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maastesting.parallel`."""

__all__ = []

import os
import random
from unittest.mock import ANY

import junitxml
from maastesting import parallel
from maastesting.fixtures import CaptureStandardIO
from maastesting.matchers import (
    DocTestMatches,
    MockCalledOnceWith,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase
import subunit
from testtools import (
    ExtendedToOriginalDecorator,
    MultiTestResult,
    TestByTestResult,
    TextTestResult,
)
from testtools.matchers import (
    AfterPreprocessing,
    Equals,
    Is,
    IsInstance,
    MatchesAll,
    MatchesListwise,
    MatchesSetwise,
    MatchesStructure,
)


class TestSelectorArguments(MAASTestCase):
    """Tests for arguments that select scripts."""

    def setUp(self):
        super(TestSelectorArguments, self).setUp()
        self.stdio = self.useFixture(CaptureStandardIO())
        self.patch_autospec(parallel, "test")
        parallel.test.return_value = True

    def assertScriptsMatch(self, *matchers):
        self.assertThat(parallel.test, MockCalledOnceWith(ANY, ANY, ANY))
        suite, results, processes = parallel.test.call_args[0]
        self.assertThat(
            suite, AfterPreprocessing(list, MatchesSetwise(*matchers))
        )

    def test__all_scripts_are_selected_when_no_selectors(self):
        sysexit = self.assertRaises(SystemExit, parallel.main, [])
        self.assertThat(sysexit.code, Equals(0))
        self.assertScriptsMatch(
            MatchesUnselectableScript("bin/test.region.legacy"),
            MatchesSelectableScript("cli"),
            MatchesSelectableScript("rack"),
            MatchesSelectableScript("region"),
            MatchesSelectableScript("testing"),
        )

    def test__scripts_can_be_selected_by_path(self):
        sysexit = self.assertRaises(
            SystemExit,
            parallel.main,
            [
                "src/maascli/001",
                "src/provisioningserver/002",
                "src/maasserver/003",
                "src/metadataserver/004",
                "src/maastesting/005",
            ],
        )
        self.assertThat(sysexit.code, Equals(0))
        self.assertScriptsMatch(
            MatchesSelectableScript("cli", "src/maascli/001"),
            MatchesSelectableScript("rack", "src/provisioningserver/002"),
            MatchesSelectableScript(
                "region", "src/maasserver/003", "src/metadataserver/004"
            ),
            MatchesSelectableScript("testing", "src/maastesting/005"),
        )

    def test__scripts_can_be_selected_by_module(self):
        sysexit = self.assertRaises(
            SystemExit,
            parallel.main,
            [
                "maascli.001",
                "provisioningserver.002",
                "maasserver.003",
                "metadataserver.004",
                "maastesting.005",
            ],
        )
        self.assertThat(sysexit.code, Equals(0))
        self.assertScriptsMatch(
            MatchesSelectableScript("cli", "maascli.001"),
            MatchesSelectableScript("rack", "provisioningserver.002"),
            MatchesSelectableScript(
                "region", "maasserver.003", "metadataserver.004"
            ),
            MatchesSelectableScript("testing", "maastesting.005"),
        )


def MatchesUnselectableScript(what, *selectors):
    return MatchesAll(
        IsInstance(parallel.TestScriptUnselectable),
        MatchesStructure.byEquality(script=what),
        first_only=True,
    )


def MatchesSelectableScript(what, *selectors):
    return MatchesAll(
        IsInstance(parallel.TestScriptSelectable),
        MatchesStructure.byEquality(
            script="bin/test.%s" % what, selectors=selectors
        ),
        first_only=True,
    )


class TestSubprocessArguments(MAASTestCase):
    """Tests for arguments that adjust subprocess behaviour."""

    def setUp(self):
        super(TestSubprocessArguments, self).setUp()
        self.stdio = self.useFixture(CaptureStandardIO())
        self.patch_autospec(parallel, "test")
        parallel.test.return_value = True

    def test__defaults(self):
        sysexit = self.assertRaises(SystemExit, parallel.main, [])
        self.assertThat(sysexit.code, Equals(0))
        self.assertThat(
            parallel.test,
            MockCalledOnceWith(ANY, ANY, max(os.cpu_count() - 2, 2)),
        )

    def test__subprocess_count_can_be_specified(self):
        count = random.randrange(100, 1000)
        sysexit = self.assertRaises(
            SystemExit, parallel.main, ["--subprocesses", str(count)]
        )
        self.assertThat(sysexit.code, Equals(0))
        self.assertThat(parallel.test, MockCalledOnceWith(ANY, ANY, count))

    def test__subprocess_count_of_less_than_1_is_rejected(self):
        sysexit = self.assertRaises(
            SystemExit, parallel.main, ["--subprocesses", "0"]
        )
        self.assertThat(sysexit.code, Equals(2))
        self.assertThat(parallel.test, MockNotCalled())
        self.assertThat(
            self.stdio.getError(),
            DocTestMatches(
                "usage: ... argument --subprocesses: 0 is not 1 or greater"
            ),
        )

    def test__subprocess_count_non_numeric_is_rejected(self):
        sysexit = self.assertRaises(
            SystemExit, parallel.main, ["--subprocesses", "foo"]
        )
        self.assertThat(sysexit.code, Equals(2))
        self.assertThat(parallel.test, MockNotCalled())
        self.assertThat(
            self.stdio.getError(),
            DocTestMatches(
                "usage: ... argument --subprocesses: 'foo' is not an integer"
            ),
        )

    def test__subprocess_per_core_can_be_specified(self):
        sysexit = self.assertRaises(
            SystemExit, parallel.main, ["--subprocess-per-core"]
        )
        self.assertThat(sysexit.code, Equals(0))
        self.assertThat(
            parallel.test, MockCalledOnceWith(ANY, ANY, os.cpu_count())
        )

    def test__subprocess_count_and_per_core_cannot_both_be_specified(self):
        sysexit = self.assertRaises(
            SystemExit,
            parallel.main,
            ["--subprocesses", "3", "--subprocess-per-core"],
        )
        self.assertThat(sysexit.code, Equals(2))
        self.assertThat(parallel.test, MockNotCalled())
        self.assertThat(
            self.stdio.getError(),
            DocTestMatches(
                "usage: ... argument --subprocess-per-core: not allowed with "
                "argument --subprocesses"
            ),
        )


class TestEmissionArguments(MAASTestCase):
    """Tests for arguments that adjust result emission behaviour."""

    def setUp(self):
        super(TestEmissionArguments, self).setUp()
        self.stdio = self.useFixture(CaptureStandardIO())
        self.patch_autospec(parallel, "test")
        parallel.test.return_value = True

    def test__results_are_human_readable_by_default(self):
        sysexit = self.assertRaises(SystemExit, parallel.main, [])
        self.assertThat(sysexit.code, Equals(0))
        self.assertThat(parallel.test, MockCalledOnceWith(ANY, ANY, ANY))
        _, result, _ = parallel.test.call_args[0]
        self.assertThat(
            result,
            IsMultiResultOf(
                IsInstance(TextTestResult), IsInstance(TestByTestResult)
            ),
        )

    def test__results_can_be_explicitly_specified_as_human_readable(self):
        sysexit = self.assertRaises(
            SystemExit, parallel.main, ["--emit-human"]
        )
        self.assertThat(sysexit.code, Equals(0))
        self.assertThat(parallel.test, MockCalledOnceWith(ANY, ANY, ANY))
        _, result, _ = parallel.test.call_args[0]
        self.assertThat(
            result,
            IsMultiResultOf(
                IsInstance(TextTestResult), IsInstance(TestByTestResult)
            ),
        )

    def test__results_can_be_specified_as_subunit(self):
        sysexit = self.assertRaises(
            SystemExit, parallel.main, ["--emit-subunit"]
        )
        self.assertThat(sysexit.code, Equals(0))
        self.assertThat(parallel.test, MockCalledOnceWith(ANY, ANY, ANY))
        _, result, _ = parallel.test.call_args[0]
        self.assertThat(result, IsInstance(subunit.TestProtocolClient))
        self.assertThat(
            result, MatchesStructure(_stream=Is(self.stdio.stdout.buffer))
        )

    def test__results_can_be_specified_as_junit(self):
        sysexit = self.assertRaises(
            SystemExit, parallel.main, ["--emit-junit"]
        )
        self.assertThat(sysexit.code, Equals(0))
        self.assertThat(parallel.test, MockCalledOnceWith(ANY, ANY, ANY))
        _, result, _ = parallel.test.call_args[0]
        self.assertThat(result, IsInstance(junitxml.JUnitXmlResult))
        self.assertThat(
            result, MatchesStructure(_stream=Is(self.stdio.stdout))
        )


def IsMultiResultOf(*results):
    """Match a `MultiTestResult` wrapping the given results."""
    return MatchesAll(
        IsInstance(MultiTestResult),
        MatchesStructure(
            _results=MatchesListwise(
                [
                    MatchesAll(
                        IsInstance(ExtendedToOriginalDecorator),
                        MatchesStructure(decorated=matcher),
                        first_only=True,
                    )
                    for matcher in results
                ]
            )
        ),
        first_only=True,
    )
