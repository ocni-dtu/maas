# Copyright 2014-2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Support for testing with `crochet`."""

__all__ = ["EventualResultCatchingMixin"]

import crochet
from testtools.content import Content, UTF8_TEXT
from testtools.matchers import Equals


class EventualResultCatchingMixin:
    """A mix-in for tests that checks for unfired/unhandled `EventualResults`.

    It reports about all py:class:`crochet.EventualResults` that are unfired
    or whose results have not been retrieved. A test detail is recorded for
    each, then the test is force-failed at the last moment.
    """

    def setUp(self):
        super(EventualResultCatchingMixin, self).setUp()
        try:
            # Every EventualResult that crochet creates is registered into
            # this registry. We'll check it after the test has finished.
            registry = crochet._main._registry
        except AttributeError:
            # Crochet has not started, so we have nothing to check right now.
            pass
        else:
            # The registry stores EventualResults in a WeakSet, which means
            # that unfired and unhandled results can be garbage collected
            # before we get to see them. Here we patch in a regular set so
            # that nothing gets garbage collected until we've been able to
            # check the results.
            results = set()
            self.addCleanup(
                self.__patchResults,
                registry,
                self.__patchResults(registry, results),
            )
            # While unravelling clean-ups is a good time to check the results.
            # Any meaningful work represented by an EventualResult should have
            # done should been done by now.
            self.addCleanup(self.__checkResults, results)

    def __patchResults(self, registry, results):
        with registry._lock:
            originals = registry._results
            registry._results = set()
            return originals

    def __checkResults(self, eventual_results):
        fail_count = 0

        # Go through all the EventualResults created in this test.
        for eventual_result in eventual_results:
            # If the result has been retrieved, fine, otherwise look closer.
            if not eventual_result._result_retrieved:
                fail_count += 1

                try:
                    # Is there a result waiting to be retrieved?
                    result = eventual_result.wait(timeout=0)
                except crochet.TimeoutError:
                    # No result yet. This could be because the result is wired
                    # up to a Deferred that hasn't fired yet, or because it
                    # hasn't yet been connected.
                    if eventual_result._deferred is None:
                        message = [
                            "*** EventualResult has not fired:\n",
                            "%r\n" % (eventual_result,),
                            "*** It was not connected to a Deferred.\n",
                        ]
                    else:
                        message = [
                            "*** EventualResult has not fired:\n",
                            "%r\n" % (eventual_result,),
                            "*** It was connected to a Deferred:\n",
                            "%r\n" % (eventual_result._deferred,),
                        ]
                else:
                    # A result, but nothing has collected it. This can be
                    # caused by forgetting to call wait().
                    message = [
                        "*** EventualResult has fired:\n",
                        "%r\n" % (eventual_result,),
                        "*** It contained the following result:\n",
                        "%r\n" % (result,),
                        "*** but it was not collected.\n",
                        "*** Was result.wait() called?\n",
                    ]

                # Record the details with a unique name.
                message = [block.encode("utf-8") for block in message]
                self.addDetail(
                    "Unfired/unhandled EventualResult #%d" % fail_count,
                    Content(UTF8_TEXT, lambda: message),
                )

        # Use expectThat() so that other clean-up tasks run to completion
        # before, at the last moment, the test is failed.
        self.expectThat(
            fail_count,
            Equals(0),
            "Unfired and/or unhandled " "EventualResult(s); see test details.",
        )
