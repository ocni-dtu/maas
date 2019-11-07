# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test ORM utilities."""

__all__ = []

from contextlib import contextmanager
from itertools import islice, repeat
from random import randint
import unittest
from unittest.mock import ANY, call, Mock, sentinel

from django.core.exceptions import MultipleObjectsReturned
from django.db import connection, connections, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.transaction import TransactionManagementError
from django.db.utils import IntegrityError, OperationalError
from maasserver.models import Node
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
    SerializationFailureTestCase,
    UniqueViolationTestCase,
)
from maasserver.utils import orm
from maasserver.utils.orm import (
    count_queries,
    disable_all_database_connections,
    DisabledDatabaseConnection,
    enable_all_database_connections,
    ExclusivelyConnected,
    FullyConnected,
    get_first,
    get_model_object_name,
    get_one,
    get_psycopg2_deadlock_exception,
    get_psycopg2_exception,
    get_psycopg2_foreign_key_violation_exception,
    get_psycopg2_serialization_exception,
    get_psycopg2_unique_violation_exception,
    in_transaction,
    is_deadlock_failure,
    is_foreign_key_violation,
    is_retryable_failure,
    is_serialization_failure,
    is_unique_violation,
    log_sql_calls,
    post_commit,
    post_commit_do,
    post_commit_hooks,
    psql_array,
    request_transaction_retry,
    retry_on_retryable_failure,
    savepoint,
    TotallyDisconnected,
    validate_in_transaction,
)
from maastesting.doubles import StubContext
from maastesting.factory import factory
from maastesting.matchers import (
    HasLength,
    IsFiredDeferred,
    LessThanOrEqual,
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase
from maastesting.twisted import extract_result
from provisioningserver.utils.twisted import callOut, DeferredValue
import psycopg2
from psycopg2.errorcodes import (
    DEADLOCK_DETECTED,
    FOREIGN_KEY_VIOLATION,
    SERIALIZATION_FAILURE,
    UNIQUE_VIOLATION,
)
from testtools import ExpectedException
from testtools.matchers import (
    AllMatch,
    Equals,
    Is,
    IsInstance,
    MatchesPredicate,
    Not,
)
from twisted.internet.defer import CancelledError, Deferred, passthru
from twisted.python.failure import Failure


class NoSleepMixin(unittest.TestCase):
    """Test case mix-in to prevent real sleeps in the `orm` module."""

    def setUp(self):
        super(NoSleepMixin, self).setUp()
        self.patch(orm, "sleep", lambda _: None)


class FakeModel:
    class MultipleObjectsReturned(MultipleObjectsReturned):
        pass

    def __init__(self, name):
        self.name == name

    def __repr__(self):
        return self.name


class FakeQueryResult:
    """Something that looks, to `get_one`, close enough to a Django model."""

    def __init__(self, model, items):
        self.model = model
        self.items = items

    def __iter__(self):
        return self.items.__iter__()

    def __repr__(self):
        return "<FakeQueryResult: %r>" % self.items


class TestGetOne(MAASTestCase):
    def test_get_one_returns_None_for_empty_list(self):
        self.assertIsNone(get_one([]))

    def test_get_one_returns_single_list_item(self):
        item = factory.make_string()
        self.assertEqual(item, get_one([item]))

    def test_get_one_returns_None_from_any_empty_sequence(self):
        self.assertIsNone(get_one("no item" for counter in range(0)))

    def test_get_one_returns_item_from_any_sequence_of_length_one(self):
        item = factory.make_string()
        self.assertEqual(item, get_one(item for counter in range(1)))

    def test_get_one_does_not_trigger_database_counting(self):
        # Avoid typical performance pitfall of querying objects *and*
        # the number of objects.
        item = factory.make_string()
        sequence = FakeQueryResult(type(item), [item])
        sequence.__len__ = Mock(side_effect=Exception("len() was called"))
        self.assertEqual(item, get_one(sequence))

    def test_get_one_does_not_iterate_long_sequence_indefinitely(self):
        # Avoid typical performance pitfall of retrieving all objects.
        # In rare failure cases, there may be large numbers.  Fail fast.

        class InfinityException(Exception):
            """Iteration went on indefinitely."""

        def infinite_sequence():
            """Generator: count to infinity (more or less), then fail."""
            for counter in range(3):
                yield counter
            raise InfinityException()

        # Raises MultipleObjectsReturned as spec'ed.  It does not
        # iterate to infinity first!
        self.assertRaises(
            MultipleObjectsReturned, get_one, infinite_sequence()
        )

    def test_get_one_raises_model_error_if_query_result_is_too_big(self):
        self.assertRaises(
            FakeModel.MultipleObjectsReturned,
            get_one,
            FakeQueryResult(FakeModel, list(range(2))),
        )

    def test_get_one_raises_generic_error_if_other_sequence_is_too_big(self):
        self.assertRaises(MultipleObjectsReturned, get_one, list(range(2)))


class TestGetFirst(MAASTestCase):
    def test_get_first_returns_None_for_empty_list(self):
        self.assertIsNone(get_first([]))

    def test_get_first_returns_first_item(self):
        items = [factory.make_string() for counter in range(10)]
        self.assertEqual(items[0], get_first(items))

    def test_get_first_accepts_any_sequence(self):
        item = factory.make_string()
        self.assertEqual(item, get_first(repeat(item)))

    def test_get_first_does_not_retrieve_beyond_first_item(self):
        class SecondItemRetrieved(Exception):
            """Second item as retrieved.  It shouldn't be."""

        def multiple_items():
            yield "Item 1"
            raise SecondItemRetrieved()

        self.assertEqual("Item 1", get_first(multiple_items()))


class TestSerializationFailure(SerializationFailureTestCase):
    """Detecting SERIALIZABLE isolation failures."""

    def test_serialization_failure_detectable_via_error_cause(self):
        error = self.assertRaises(
            OperationalError, self.cause_serialization_failure
        )
        self.assertEqual(SERIALIZATION_FAILURE, error.__cause__.pgcode)


class TestUniqueViolation(UniqueViolationTestCase):
    """Detecting UNIQUE_VIOLATION failures."""

    def test_unique_violation_detectable_via_error_cause(self):
        error = self.assertRaises(IntegrityError, self.cause_unique_violation)
        self.assertEqual(UNIQUE_VIOLATION, error.__cause__.pgcode)


class TestGetPsycopg2Exception(MAASTestCase):
    """Tests for `get_psycopg2_exception`."""

    def test__returns_psycopg2_error(self):
        exception = psycopg2.Error()
        self.assertIs(exception, get_psycopg2_exception(exception))

    def test__returns_None_for_other_error(self):
        exception = factory.make_exception()
        self.assertIsNone(get_psycopg2_serialization_exception(exception))

    def test__returns_psycopg2_error_root_cause_for_serialization(self):
        exception = Exception()
        exception.__cause__ = orm.SerializationFailure()
        self.assertIs(exception.__cause__, get_psycopg2_exception(exception))

    def test__returns_psycopg2_error_root_cause_for_deadlock(self):
        exception = Exception()
        exception.__cause__ = orm.DeadlockFailure()
        self.assertIs(exception.__cause__, get_psycopg2_exception(exception))

    def test__returns_psycopg2_error_root_cause_for_foreign_key(self):
        exception = Exception()
        exception.__cause__ = orm.ForeignKeyViolation()
        self.assertIs(exception.__cause__, get_psycopg2_exception(exception))


class TestGetPsycopg2SerializationException(MAASTestCase):
    """Tests for `get_psycopg2_serialization_exception`."""

    def test__returns_None_for_plain_psycopg2_error(self):
        exception = psycopg2.Error()
        self.assertIsNone(get_psycopg2_serialization_exception(exception))

    def test__returns_None_for_other_error(self):
        exception = factory.make_exception()
        self.assertIsNone(get_psycopg2_serialization_exception(exception))

    def test__returns_psycopg2_error_root_cause(self):
        exception = Exception()
        exception.__cause__ = orm.SerializationFailure()
        self.assertIs(
            exception.__cause__,
            get_psycopg2_serialization_exception(exception),
        )


class TestGetPsycopg2DeadlockException(MAASTestCase):
    """Tests for `get_psycopg2_deadlock_exception`."""

    def test__returns_None_for_plain_psycopg2_error(self):
        exception = psycopg2.Error()
        self.assertIsNone(get_psycopg2_deadlock_exception(exception))

    def test__returns_None_for_other_error(self):
        exception = factory.make_exception()
        self.assertIsNone(get_psycopg2_deadlock_exception(exception))

    def test__returns_psycopg2_error_root_cause(self):
        exception = Exception()
        exception.__cause__ = orm.DeadlockFailure()
        self.assertIs(
            exception.__cause__, get_psycopg2_deadlock_exception(exception)
        )


class TestGetPsycopg2UniqueViolationException(MAASTestCase):
    """Tests for `get_psycopg2_unique_violation_exception`."""

    def test__returns_None_for_plain_psycopg2_error(self):
        exception = psycopg2.Error()
        self.assertIsNone(get_psycopg2_unique_violation_exception(exception))

    def test__returns_None_for_other_error(self):
        exception = factory.make_exception()
        self.assertIsNone(get_psycopg2_unique_violation_exception(exception))

    def test__returns_psycopg2_error_root_cause(self):
        exception = Exception()
        exception.__cause__ = orm.UniqueViolation()
        self.assertIs(
            exception.__cause__,
            get_psycopg2_unique_violation_exception(exception),
        )


class TestGetPsycopg2ForeignKeyException(MAASTestCase):
    """Tests for `get_psycopg2_foreign_key_violation_exception`."""

    def test__returns_None_for_plain_psycopg2_error(self):
        exception = psycopg2.Error()
        self.assertIsNone(
            get_psycopg2_foreign_key_violation_exception(exception)
        )

    def test__returns_None_for_other_error(self):
        exception = factory.make_exception()
        self.assertIsNone(
            get_psycopg2_foreign_key_violation_exception(exception)
        )

    def test__returns_psycopg2_error_root_cause(self):
        exception = Exception()
        exception.__cause__ = orm.ForeignKeyViolation()
        self.assertIs(
            exception.__cause__,
            get_psycopg2_foreign_key_violation_exception(exception),
        )


class TestIsSerializationFailure(SerializationFailureTestCase):
    """Tests relating to MAAS's use of SERIALIZABLE isolation."""

    def test_detects_operational_error_with_matching_cause(self):
        error = self.assertRaises(
            OperationalError, self.cause_serialization_failure
        )
        self.assertTrue(is_serialization_failure(error))

    def test_rejects_operational_error_without_matching_cause(self):
        error = OperationalError()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = factory.make_name("pgcode")
        self.assertFalse(is_serialization_failure(error))

    def test_rejects_operational_error_with_unrelated_cause(self):
        error = OperationalError()
        error.__cause__ = Exception()
        self.assertFalse(is_serialization_failure(error))

    def test_rejects_operational_error_without_cause(self):
        error = OperationalError()
        self.assertFalse(is_serialization_failure(error))

    def test_rejects_non_operational_error_with_matching_cause(self):
        error = factory.make_exception()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = SERIALIZATION_FAILURE
        self.assertFalse(is_serialization_failure(error))


class TestIsDeadlockFailure(MAASTestCase):
    """Tests relating to MAAS's use of catching deadlock failures."""

    def test_detects_operational_error_with_matching_cause(self):
        error = orm.make_deadlock_failure()
        self.assertTrue(is_deadlock_failure(error))

    def test_rejects_operational_error_without_matching_cause(self):
        error = OperationalError()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = factory.make_name("pgcode")
        self.assertFalse(is_deadlock_failure(error))

    def test_rejects_operational_error_with_unrelated_cause(self):
        error = OperationalError()
        error.__cause__ = Exception()
        self.assertFalse(is_deadlock_failure(error))

    def test_rejects_operational_error_without_cause(self):
        error = OperationalError()
        self.assertFalse(is_deadlock_failure(error))

    def test_rejects_non_operational_error_with_matching_cause(self):
        error = factory.make_exception()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = DEADLOCK_DETECTED
        self.assertFalse(is_deadlock_failure(error))


class TestIsUniqueViolation(UniqueViolationTestCase):
    """Tests relating to MAAS's identification of unique violations."""

    def test_detects_integrity_error_with_matching_cause(self):
        error = self.assertRaises(IntegrityError, self.cause_unique_violation)
        self.assertTrue(is_unique_violation(error))

    def test_rejects_integrity_error_without_matching_cause(self):
        error = IntegrityError()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = factory.make_name("pgcode")
        self.assertFalse(is_unique_violation(error))

    def test_rejects_integrity_error_with_unrelated_cause(self):
        error = IntegrityError()
        error.__cause__ = Exception()
        self.assertFalse(is_unique_violation(error))

    def test_rejects_integrity_error_without_cause(self):
        error = IntegrityError()
        self.assertFalse(is_unique_violation(error))

    def test_rejects_non_integrity_error_with_matching_cause(self):
        error = factory.make_exception()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = UNIQUE_VIOLATION
        self.assertFalse(is_unique_violation(error))


class TestIsForeignKeyViolation(MAASTestCase):
    """Tests relating to MAAS's use of catching foreign key violations."""

    def test_detects_violation_with_matching_cause(self):
        error = orm.make_foreign_key_violation()
        self.assertTrue(is_foreign_key_violation(error))

    def test_rejects_violation_without_matching_cause(self):
        error = OperationalError()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = factory.make_name("pgcode")
        self.assertFalse(is_foreign_key_violation(error))

    def test_rejects_violation_with_unrelated_cause(self):
        error = OperationalError()
        error.__cause__ = Exception()
        self.assertFalse(is_foreign_key_violation(error))

    def test_rejects_violation_without_cause(self):
        error = OperationalError()
        self.assertFalse(is_foreign_key_violation(error))

    def test_rejects_non_violation_with_matching_cause(self):
        error = factory.make_exception()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = FOREIGN_KEY_VIOLATION
        self.assertFalse(is_foreign_key_violation(error))


class TestIsRetryableFailure(MAASTestCase):
    """Tests relating to MAAS's use of catching retryable failures."""

    def test_detects_serialization_failure(self):
        error = orm.make_serialization_failure()
        self.assertTrue(is_retryable_failure(error))

    def test_detects_deadlock_failure(self):
        error = orm.make_deadlock_failure()
        self.assertTrue(is_retryable_failure(error))

    def test_detects_unique_violation(self):
        error = orm.make_unique_violation()
        self.assertTrue(is_retryable_failure(error))

    def test_detects_foreign_key_violation(self):
        error = orm.make_foreign_key_violation()
        self.assertTrue(is_retryable_failure(error))

    def test_rejects_operational_error_without_matching_cause(self):
        error = OperationalError()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = factory.make_name("pgcode")
        self.assertFalse(is_retryable_failure(error))

    def test_rejects_integrity_error_without_matching_cause(self):
        error = IntegrityError()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = factory.make_name("pgcode")
        self.assertFalse(is_retryable_failure(error))

    def test_rejects_operational_error_with_unrelated_cause(self):
        error = OperationalError()
        error.__cause__ = Exception()
        self.assertFalse(is_retryable_failure(error))

    def test_rejects_integrity_error_with_unrelated_cause(self):
        error = IntegrityError()
        error.__cause__ = Exception()
        self.assertFalse(is_retryable_failure(error))

    def test_rejects_operational_error_without_cause(self):
        error = OperationalError()
        self.assertFalse(is_retryable_failure(error))

    def test_rejects_integrity_error_without_cause(self):
        error = IntegrityError()
        self.assertFalse(is_retryable_failure(error))

    def test_rejects_non_database_error_with_cause_serialization(self):
        error = factory.make_exception()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = SERIALIZATION_FAILURE
        self.assertFalse(is_retryable_failure(error))

    def test_rejects_non_database_error_with_cause_deadlock(self):
        error = factory.make_exception()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = DEADLOCK_DETECTED
        self.assertFalse(is_retryable_failure(error))

    def test_rejects_non_database_error_with_cause_unique_violation(self):
        error = factory.make_exception()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = UNIQUE_VIOLATION
        self.assertFalse(is_retryable_failure(error))

    def test_rejects_non_database_error_with_cause_foreign_key_violation(self):
        error = factory.make_exception()
        cause = self.patch(error, "__cause__", Exception())
        cause.pgcode = FOREIGN_KEY_VIOLATION
        self.assertFalse(is_retryable_failure(error))


class TestRetryOnRetryableFailure(SerializationFailureTestCase, NoSleepMixin):
    def make_mock_function(self):
        function_name = factory.make_name("function")
        function = Mock(__name__=function_name)
        return function

    def test_retries_on_serialization_failure(self):
        function = self.make_mock_function()
        function.side_effect = self.cause_serialization_failure
        function_wrapped = retry_on_retryable_failure(function)
        self.assertRaises(OperationalError, function_wrapped)
        expected_calls = [call()] * 10
        self.assertThat(function, MockCallsMatch(*expected_calls))

    def test_retries_on_serialization_failure_until_successful(self):
        serialization_error = self.assertRaises(
            OperationalError, self.cause_serialization_failure
        )
        function = self.make_mock_function()
        function.side_effect = [serialization_error, sentinel.result]
        function_wrapped = retry_on_retryable_failure(function)
        self.assertEqual(sentinel.result, function_wrapped())
        self.assertThat(function, MockCallsMatch(call(), call()))

    def test_retries_on_deadlock_failure(self):
        function = self.make_mock_function()
        function.side_effect = orm.make_deadlock_failure()
        function_wrapped = retry_on_retryable_failure(function)
        self.assertRaises(OperationalError, function_wrapped)
        expected_calls = [call()] * 10
        self.assertThat(function, MockCallsMatch(*expected_calls))

    def test_retries_on_deadlock_failure_until_successful(self):
        function = self.make_mock_function()
        function.side_effect = [orm.make_deadlock_failure(), sentinel.result]
        function_wrapped = retry_on_retryable_failure(function)
        self.assertEqual(sentinel.result, function_wrapped())
        self.assertThat(function, MockCallsMatch(call(), call()))

    def test_retries_on_unique_violation(self):
        function = self.make_mock_function()
        function.side_effect = orm.make_unique_violation()
        function_wrapped = retry_on_retryable_failure(function)
        self.assertRaises(IntegrityError, function_wrapped)
        expected_calls = [call()] * 10
        self.assertThat(function, MockCallsMatch(*expected_calls))

    def test_retries_on_unique_violation_until_successful(self):
        function = self.make_mock_function()
        function.side_effect = [orm.make_unique_violation(), sentinel.result]
        function_wrapped = retry_on_retryable_failure(function)
        self.assertEqual(sentinel.result, function_wrapped())
        self.assertThat(function, MockCallsMatch(call(), call()))

    def test_retries_on_foreign_key_violation(self):
        function = self.make_mock_function()
        function.side_effect = orm.make_foreign_key_violation()
        function_wrapped = retry_on_retryable_failure(function)
        self.assertRaises(IntegrityError, function_wrapped)
        expected_calls = [call()] * 10
        self.assertThat(function, MockCallsMatch(*expected_calls))

    def test_retries_on_foreign_key_violation_until_successful(self):
        function = self.make_mock_function()
        function.side_effect = [
            orm.make_foreign_key_violation(),
            sentinel.result,
        ]
        function_wrapped = retry_on_retryable_failure(function)
        self.assertEqual(sentinel.result, function_wrapped())
        self.assertThat(function, MockCallsMatch(call(), call()))

    def test_retries_on_retry_transaction(self):
        function = self.make_mock_function()
        function.side_effect = orm.RetryTransaction()
        function_wrapped = retry_on_retryable_failure(function)
        self.assertRaises(orm.TooManyRetries, function_wrapped)
        expected_calls = [call()] * 10
        self.assertThat(function, MockCallsMatch(*expected_calls))

    def test_retries_on_retry_transaction_until_successful(self):
        function = self.make_mock_function()
        function.side_effect = [orm.RetryTransaction(), sentinel.result]
        function_wrapped = retry_on_retryable_failure(function)
        self.assertEqual(sentinel.result, function_wrapped())
        self.assertThat(function, MockCallsMatch(call(), call()))

    def test_passes_args_to_wrapped_function(self):
        function = lambda a, b: (a, b)
        function_wrapped = retry_on_retryable_failure(function)
        self.assertEqual(
            (sentinel.a, sentinel.b),
            function_wrapped(sentinel.a, b=sentinel.b),
        )

    def test_calls_reset_between_retries(self):
        reset = Mock()
        function = self.make_mock_function()
        function.side_effect = self.cause_serialization_failure
        function_wrapped = retry_on_retryable_failure(function, reset)
        self.assertRaises(OperationalError, function_wrapped)
        expected_function_calls = [call()] * 10
        self.expectThat(function, MockCallsMatch(*expected_function_calls))
        # There's one fewer reset than calls to the function.
        expected_reset_calls = expected_function_calls[:-1]
        self.expectThat(reset, MockCallsMatch(*expected_reset_calls))

    def test_does_not_call_reset_before_first_attempt(self):
        reset = Mock()
        function = self.make_mock_function()
        function.return_value = sentinel.all_is_okay
        function_wrapped = retry_on_retryable_failure(function, reset)
        function_wrapped()
        self.assertThat(reset, MockNotCalled())

    def test_uses_retry_context(self):
        @retry_on_retryable_failure
        def function_that_will_be_retried():
            self.assertThat(orm.retry_context.active, Is(True))

        self.assertThat(orm.retry_context.active, Is(False))
        function_that_will_be_retried()
        self.assertThat(orm.retry_context.active, Is(False))

    def test_retry_contexts_accumulate(self):
        accumulated, entered, exited = [], [], []

        @contextmanager
        def observe(thing):
            # Record `thing` on entry.
            entered.append(thing)
            try:
                yield sentinel.ignored
            finally:
                # Record `thing` on exit.
                exited.append(thing)

        @retry_on_retryable_failure
        def accumulate_and_observe():
            # `accumulated` is a record of every time this function is called.
            # `entered` and `exited` record what's going on with the contexts
            # we're throwing up into the retry machinery.
            count = len(accumulated)
            accumulated.append(count)
            request_transaction_retry(observe(count))

        self.assertRaises(orm.TooManyRetries, accumulate_and_observe)
        # `entered` and `exited` will contain all but the highest element in
        # `accumulated` because the final context is not entered nor exited.
        expected = accumulated[:-1]
        self.assertThat(entered, Equals(expected))
        expected.reverse()
        self.assertThat(exited, Equals(expected))


class TestMakeSerializationFailure(MAASTestCase):
    """Tests for `make_serialization_failure`."""

    def test__makes_a_serialization_failure(self):
        exception = orm.make_serialization_failure()
        self.assertThat(
            exception,
            MatchesPredicate(
                is_serialization_failure, "%r is not a serialization failure."
            ),
        )


class TestMakeDeadlockFailure(MAASTestCase):
    """Tests for `make_deadlock_failure`."""

    def test__makes_a_deadlock_failure(self):
        exception = orm.make_deadlock_failure()
        self.assertThat(
            exception,
            MatchesPredicate(
                is_deadlock_failure, "%r is not a deadlock failure."
            ),
        )


class TestMakeUniqueViolation(MAASTestCase):
    """Tests for `make_unique_violation`."""

    def test__makes_a_unique_violation(self):
        exception = orm.make_unique_violation()
        self.assertThat(
            exception,
            MatchesPredicate(
                is_unique_violation, "%r is not a unique violation."
            ),
        )


class TestMakeForeignKeyViolation(MAASTestCase):
    """Tests for `make_foreign_key_violation`."""

    def test__makes_a_foreign_key_violation(self):
        exception = orm.make_foreign_key_violation()
        self.assertThat(
            exception,
            MatchesPredicate(
                is_foreign_key_violation, "%r is not a foreign key violation."
            ),
        )


class PopulateContext:
    """A simple context manager that puts `thing` in `alist`."""

    def __init__(self, alist, thing):
        self.alist, self.thing = alist, thing

    def __enter__(self):
        self.alist.append(self.thing)
        return sentinel.irrelevant

    def __exit__(self, *exc_info):
        assert self.alist.pop() is self.thing


class CrashEntryContext:
    """A simple context manager that crashes on entry."""

    def __enter__(self):
        0 / 0  # Divide by zero and break everything.

    def __exit__(self, *exc_info):
        pass  # Nothing left to break.


class CrashExitContext:
    """A simple context manager that crashes on exit."""

    def __enter__(self):
        pass  # What a lovely day this is...

    def __exit__(self, *exc_info):
        0 / 0  # Nah, destroy everything.


class TestRetryStack(MAASTestCase):
    """Tests for `RetryStack`."""

    def test_add_and_enter_pending_contexts(self):
        names = []
        with orm.RetryStack() as stack:
            stack.add_pending_contexts(
                [
                    PopulateContext(names, "alice"),
                    PopulateContext(names, "bob"),
                    PopulateContext(names, "carol"),
                ]
            )
            # These contexts haven't been entered yet.
            self.assertThat(names, Equals([]))
            # They're entered when `enter_pending_contexts` is called.
            stack.enter_pending_contexts()
            # The contexts are entered in the order specified.
            self.assertThat(names, Equals(["alice", "bob", "carol"]))
        # These contexts have been exited again.
        self.assertThat(names, Equals([]))

    def test_each_context_entered_only_once_even_if_added_twice(self):
        names = []
        with orm.RetryStack() as stack:
            dave = PopulateContext(names, "dave")
            stack.add_pending_contexts([dave, dave])
            stack.enter_pending_contexts()
            # The `dave` context is entered only once.
            self.assertThat(names, Equals(["dave"]))
        # The `dave` context has been exited.
        self.assertThat(names, Equals([]))

    def test_each_context_entered_only_once_even_if_enter_called_twice(self):
        names = []
        with orm.RetryStack() as stack:
            dave = PopulateContext(names, "dave")
            stack.add_pending_contexts([dave])
            stack.enter_pending_contexts()
            stack.enter_pending_contexts()
            # The `dave` context is entered only once.
            self.assertThat(names, Equals(["dave"]))
        # The `dave` context has been exited.
        self.assertThat(names, Equals([]))

    def test_crash_entering_context_is_propagated(self):
        with orm.RetryStack() as stack:
            stack.add_pending_contexts([CrashEntryContext()])
            self.assertRaises(ZeroDivisionError, stack.enter_pending_contexts)

    def test_crash_exiting_context_is_propagated(self):
        with orm.RetryStack() as stack:
            stack.add_pending_contexts([CrashExitContext()])
            stack.enter_pending_contexts()
            # Use `ExitStack.close` here to elicit the crash.
            self.assertRaises(ZeroDivisionError, stack.close)


class TestRetryContext(MAASTestCase):
    """Tests for `RetryContext`."""

    def test_starts_off_with_nothing(self):
        context = orm.RetryContext()
        self.assertThat(context.active, Is(False))
        self.assertThat(context.stack, Is(None))

    def test_creates_stack_on_entry(self):
        context = orm.RetryContext()
        with context:
            self.assertThat(context.active, Is(True))
            self.assertThat(context.stack, IsInstance(orm.RetryStack))

    def test_prepare_enters_pending_contexts(self):
        context = orm.RetryContext()
        with context:
            context.stack.add_pending_contexts([CrashEntryContext()])
            self.assertRaises(ZeroDivisionError, context.prepare)

    def test_destroys_stack_on_exit(self):
        names = []
        context = orm.RetryContext()
        with context:
            self.assertThat(context.active, Is(True))
            context.stack.add_pending_contexts(
                [
                    PopulateContext(names, "alice"),
                    PopulateContext(names, "bob"),
                ]
            )
            context.prepare()
            self.assertThat(names, Equals(["alice", "bob"]))
        self.assertThat(context.active, Is(False))
        self.assertThat(context.stack, Is(None))
        self.assertThat(names, Equals([]))

    def test_destroys_stack_on_exit_even_when_there_is_a_crash(self):
        names = []
        context = orm.RetryContext()
        with ExpectedException(ZeroDivisionError):
            with context:
                context.stack.add_pending_contexts(
                    [
                        PopulateContext(names, "alice"),
                        CrashEntryContext(),
                        PopulateContext(names, "bob"),
                    ]
                )
                context.prepare()
                self.assertThat(names, Equals(["alice", "bob"]))
        self.assertThat(context.active, Is(False))
        self.assertThat(context.stack, Is(None))
        self.assertThat(names, Equals([]))


class TestRequestTransactionRetry(MAASTestCase):
    """Tests for `request_transaction_retry`."""

    def test__raises_a_retry_transaction_exception(self):
        with orm.retry_context:
            self.assertRaises(orm.RetryTransaction, request_transaction_retry)

    def test__adds_additional_contexts_to_retry_context(self):
        contexts = []
        with orm.retry_context:
            self.assertRaises(
                orm.RetryTransaction,
                request_transaction_retry,
                PopulateContext(contexts, "alice"),
                PopulateContext(contexts, "bob"),
                PopulateContext(contexts, "carol"),
            )
            # These contexts are added as "pending"...
            self.assertThat(contexts, Equals([]))
            # They're entered when `prepare` is called (which is done by the
            # retry machinery; normal application code doesn't do this).
            orm.retry_context.prepare()
            # The contexts are entered in the order specified.
            self.assertThat(contexts, Equals(["alice", "bob", "carol"]))
        # The contexts are exited in the order specified.
        self.assertThat(contexts, Equals([]))


class TestGenRetryIntervals(MAASTestCase):
    """Tests for `orm.gen_retry_intervals`."""

    def remove_jitter(self):
        # Remove the effect of randomness.
        full_jitter = self.patch(orm, "full_jitter")
        full_jitter.side_effect = lambda thing: thing

    def test__unjittered_series_begins(self):
        self.remove_jitter()
        # Get the first 10 intervals, without jitter.
        intervals = islice(orm.gen_retry_intervals(), 10)
        # Convert from seconds to milliseconds, and round.
        intervals = [int(interval * 1000) for interval in intervals]
        # They start off small, but grow rapidly to the maximum.
        self.assertThat(
            intervals,
            Equals([25, 62, 156, 390, 976, 2441, 6103, 10000, 10000, 10000]),
        )

    def test__pulls_from_exponential_series_until_maximum_is_reached(self):
        self.remove_jitter()
        # repeat() is the tail-end of the interval series.
        repeat = self.patch(orm, "repeat")
        repeat.return_value = [sentinel.end]
        maximum = randint(10, 100)
        intervals = list(orm.gen_retry_intervals(maximum=maximum))
        self.assertThat(intervals[-1], Is(sentinel.end))
        self.assertThat(intervals[:-1], AllMatch(LessThanOrEqual(maximum)))


class TestPostCommitHooks(MAASTestCase):
    """Tests for the `post_commit_hooks` singleton."""

    def test__crashes_on_enter_if_hooks_exist(self):
        hook = Deferred()
        post_commit_hooks.add(hook)
        with ExpectedException(TransactionManagementError):
            with post_commit_hooks:
                pass
        # The hook has been cancelled, but CancelledError is suppressed in
        # hooks, so we don't see it here.
        self.assertThat(hook, IsFiredDeferred())
        # The hook list is cleared so that the exception is raised only once.
        self.assertThat(post_commit_hooks.hooks, HasLength(0))

    def test__fires_hooks_on_exit_if_no_exception(self):
        self.addCleanup(post_commit_hooks.reset)
        hooks_fire = self.patch_autospec(post_commit_hooks, "fire")
        with post_commit_hooks:
            post_commit_hooks.add(Deferred())
        # Hooks are fired.
        self.assertThat(hooks_fire, MockCalledOnceWith())

    def test__resets_hooks_on_exit_if_exception(self):
        self.addCleanup(post_commit_hooks.reset)
        hooks_fire = self.patch_autospec(post_commit_hooks, "fire")
        hooks_reset = self.patch_autospec(post_commit_hooks, "reset")
        exception_type = factory.make_exception_type()
        with ExpectedException(exception_type):
            with post_commit_hooks:
                post_commit_hooks.add(Deferred())
                raise exception_type()
        # No hooks were fired; they were reset immediately.
        self.assertThat(hooks_fire, MockNotCalled())
        self.assertThat(hooks_reset, MockCalledOnceWith())


class TestPostCommit(MAASTestCase):
    """Tests for the `post_commit` function."""

    def setUp(self):
        super(TestPostCommit, self).setUp()
        self.addCleanup(post_commit_hooks.reset)

    def test__adds_Deferred_as_hook(self):
        hook = Deferred()
        hook_added = post_commit(hook)
        self.assertEqual([hook], list(post_commit_hooks.hooks))
        self.assertThat(hook_added, Is(hook))

    def test__adds_new_Deferred_as_hook_when_called_without_args(self):
        hook_added = post_commit()
        self.assertEqual([hook_added], list(post_commit_hooks.hooks))
        self.assertThat(hook_added, IsInstance(Deferred))

    def test__adds_callable_as_hook(self):
        hook = lambda arg: None
        hook_added = post_commit(hook)
        self.assertThat(post_commit_hooks.hooks, HasLength(1))
        self.assertThat(hook_added, IsInstance(Deferred))

    def test__fire_calls_back_with_None_to_Deferred_hook(self):
        hook = Deferred()
        spy = DeferredValue()
        spy.observe(hook)
        post_commit(hook)
        post_commit_hooks.fire()
        self.assertIsNone(extract_result(spy.get()))

    def test__fire_calls_back_with_None_to_new_Deferred_hook(self):
        hook_added = post_commit()
        spy = DeferredValue()
        spy.observe(hook_added)
        post_commit_hooks.fire()
        self.assertIsNone(extract_result(spy.get()))

    def test__reset_cancels_Deferred_hook(self):
        hook = Deferred()
        spy = DeferredValue()
        spy.observe(hook)
        post_commit(hook)
        post_commit_hooks.reset()
        self.assertRaises(CancelledError, extract_result, spy.get())

    def test__reset_cancels_new_Deferred_hook(self):
        hook_added = post_commit()
        spy = DeferredValue()
        spy.observe(hook_added)
        post_commit_hooks.reset()
        self.assertRaises(CancelledError, extract_result, spy.get())

    def test__fire_passes_None_to_callable_hook(self):
        hook = Mock()
        post_commit(hook)
        post_commit_hooks.fire()
        self.assertThat(hook, MockCalledOnceWith(None))

    def test__reset_passes_Failure_to_callable_hook(self):
        hook = Mock()
        post_commit(hook)
        post_commit_hooks.reset()
        self.assertThat(hook, MockCalledOnceWith(ANY))
        arg = hook.call_args[0][0]
        self.assertThat(arg, IsInstance(Failure))
        self.assertThat(arg.value, IsInstance(CancelledError))

    def test__rejects_other_hook_types(self):
        self.assertRaises(AssertionError, post_commit, sentinel.hook)


class TestPostCommitDo(MAASTestCase):
    """Tests for the `post_commit_do` function."""

    def setUp(self):
        super(TestPostCommitDo, self).setUp()
        self.addCleanup(post_commit_hooks.reset)

    def test__adds_callable_as_hook(self):
        hook = lambda arg: None
        post_commit_do(hook)
        self.assertThat(post_commit_hooks.hooks, HasLength(1))

    def test__returns_actual_hook(self):
        hook = Mock()
        hook_added = post_commit_do(hook, sentinel.foo, bar=sentinel.bar)
        self.assertThat(hook_added, IsInstance(Deferred))
        callback, errback = hook_added.callbacks.pop(0)
        # Errors are passed through; they're not passed to our hook.
        self.expectThat(errback, Equals((passthru, None, None)))
        # Our hook is set to be called via callOut.
        self.expectThat(
            callback,
            Equals((callOut, (hook, sentinel.foo), {"bar": sentinel.bar})),
        )

    def test__fire_passes_only_args_to_hook(self):
        hook = Mock()
        post_commit_do(hook, sentinel.arg, foo=sentinel.bar)
        post_commit_hooks.fire()
        self.assertThat(
            hook, MockCalledOnceWith(sentinel.arg, foo=sentinel.bar)
        )

    def test__reset_does_not_call_hook(self):
        hook = Mock()
        post_commit_do(hook)
        post_commit_hooks.reset()
        self.assertThat(hook, MockNotCalled())

    def test__rejects_other_hook_types(self):
        self.assertRaises(AssertionError, post_commit_do, sentinel.hook)


class TestConnected(MAASTransactionServerTestCase):
    """Tests for the `orm.connected` context manager."""

    def test__ensures_connection(self):
        with orm.connected():
            self.assertThat(connection.connection, Not(Is(None)))

    def test__opens_and_closes_connection_when_no_preexisting_connection(self):
        connection.close()

        self.assertThat(connection.connection, Is(None))
        with orm.connected():
            self.assertThat(connection.connection, Not(Is(None)))
        self.assertThat(connection.connection, Is(None))

    def test__leaves_preexisting_connections_alone(self):
        connection.ensure_connection()
        preexisting_connection = connection.connection

        self.assertThat(connection.connection, Not(Is(None)))
        with orm.connected():
            self.assertThat(connection.connection, Is(preexisting_connection))
        self.assertThat(connection.connection, Is(preexisting_connection))

    def test__disconnects_and_reconnects_if_not_usable(self):
        connection.ensure_connection()
        preexisting_connection = connection.connection

        connection.errors_occurred = True
        self.patch(connection, "is_usable").return_value = False

        self.assertThat(connection.connection, Not(Is(None)))
        with orm.connected():
            self.assertThat(
                connection.connection, Not(Is(preexisting_connection))
            )
            self.assertThat(connection.connection, Not(Is(None)))

        self.assertThat(connection.connection, Not(Is(preexisting_connection)))
        self.assertThat(connection.connection, Not(Is(None)))


class TestWithConnection(MAASTransactionServerTestCase):
    """Tests for the `orm.with_connection` decorator."""

    def test__exposes_original_function(self):
        function = Mock(__name__=self.getUniqueString())
        self.assertThat(orm.with_connection(function).func, Is(function))

    def test__ensures_function_is_called_within_connected_context(self):
        context = self.patch(orm, "connected").return_value = StubContext()

        @orm.with_connection
        def function(arg, kwarg):
            self.assertThat(arg, Is(sentinel.arg))
            self.assertThat(kwarg, Is(sentinel.kwarg))
            self.assertTrue(context.active)
            return sentinel.result

        self.assertTrue(context.unused)
        self.assertThat(
            function(sentinel.arg, kwarg=sentinel.kwarg), Is(sentinel.result)
        )
        self.assertTrue(context.used)


class TestTransactional(MAASTransactionServerTestCase):
    def test__exposes_original_function(self):
        function = Mock(__name__=self.getUniqueString())
        self.assertThat(orm.transactional(function).func, Is(function))

    def test__calls_function_within_transaction_then_closes_connections(self):
        # Close the database connection to begin with.
        connection.close()

        # No transaction has been entered (what Django calls an atomic block),
        # and the connection has not yet been established.
        self.assertFalse(connection.in_atomic_block)
        self.expectThat(connection.connection, Is(None))

        def check_inner(*args, **kwargs):
            # In here, the transaction (`atomic`) has been started but is not
            # over, and the connection to the database is open.
            self.assertTrue(connection.in_atomic_block)
            self.expectThat(connection.connection, Not(Is(None)))

        function = Mock()
        function.__name__ = self.getUniqueString()
        function.side_effect = check_inner

        # Call `function` via the `transactional` decorator.
        decorated_function = orm.transactional(function)
        decorated_function(sentinel.arg, kwarg=sentinel.kwarg)

        # `function` was called -- and therefore `check_inner` too --
        # and the arguments passed correctly.
        self.assertThat(
            function, MockCalledOnceWith(sentinel.arg, kwarg=sentinel.kwarg)
        )

        # After the decorated function has returned the transaction has
        # been exited, and the connection has been closed.
        self.assertFalse(connection.in_atomic_block)
        self.expectThat(connection.connection, Is(None))

    def test__leaves_preexisting_connections_open(self):
        # Ensure there's a database connection to begin with.
        connection.ensure_connection()

        # No transaction has been entered (what Django calls an atomic block),
        # but the connection has been established.
        self.assertFalse(connection.in_atomic_block)
        self.expectThat(connection.connection, Not(Is(None)))

        # Call a function via the `transactional` decorator.
        decorated_function = orm.transactional(lambda: None)
        decorated_function()

        # After the decorated function has returned the transaction has
        # been exited, but the preexisting connection remains open.
        self.assertFalse(connection.in_atomic_block)
        self.expectThat(connection.connection, Not(Is(None)))

    def test__closes_connections_only_when_leaving_atomic_block(self):
        # Close the database connection to begin with.
        connection.close()
        self.expectThat(connection.connection, Is(None))

        @orm.transactional
        def inner():
            # We're inside a `transactional` context here.
            self.expectThat(connection.connection, Not(Is(None)))
            return "inner"

        @orm.transactional
        def outer():
            # We're inside a `transactional` context here too.
            self.expectThat(connection.connection, Not(Is(None)))
            # Call `inner`, thus nesting `transactional` contexts.
            return "outer > " + inner()

        self.assertEqual("outer > inner", outer())
        # The connection has been closed.
        self.expectThat(connection.connection, Is(None))

    def test__fires_post_commit_hooks_when_done(self):
        fire = self.patch(orm.post_commit_hooks, "fire")
        function = lambda: sentinel.something
        decorated_function = orm.transactional(function)
        self.assertIs(sentinel.something, decorated_function())
        self.assertThat(fire, MockCalledOnceWith())

    def test__crashes_if_hooks_exist_before_entering_transaction(self):
        post_commit(lambda failure: None)
        decorated_function = orm.transactional(lambda: None)
        self.assertRaises(TransactionManagementError, decorated_function)
        # The hook list is cleared so that the exception is raised only once.
        self.assertThat(post_commit_hooks.hooks, HasLength(0))

    def test__creates_post_commit_hook_savepoint_on_inner_block(self):
        hooks = post_commit_hooks.hooks

        @orm.transactional
        def inner():
            # We're inside a savepoint context here.
            self.assertThat(post_commit_hooks.hooks, Not(Is(hooks)))
            return "inner"

        @orm.transactional
        def outer():
            # We're inside a transaction here, but not yet a savepoint.
            self.assertThat(post_commit_hooks.hooks, Is(hooks))
            return "outer > " + inner()

        self.assertEqual("outer > inner", outer())


class TestTransactionalRetries(SerializationFailureTestCase, NoSleepMixin):
    def test__retries_upon_serialization_failures(self):
        function = Mock()
        function.__name__ = self.getUniqueString()
        function.side_effect = self.cause_serialization_failure
        decorated_function = orm.transactional(function)

        self.assertRaises(OperationalError, decorated_function)
        expected_calls = [call()] * 10
        self.assertThat(function, MockCallsMatch(*expected_calls))

    def test__resets_post_commit_hooks_when_retrying(self):
        reset = self.patch(orm.post_commit_hooks, "reset")

        function = Mock()
        function.__name__ = self.getUniqueString()
        function.side_effect = self.cause_serialization_failure
        decorated_function = orm.transactional(function)

        self.assertRaises(OperationalError, decorated_function)
        # reset() is called 9 times by retry_on_serialization_failure() then
        # once more by transactional().
        expected_reset_calls = [call()] * 10
        self.assertThat(reset, MockCallsMatch(*expected_reset_calls))


class TestSavepoint(MAASTransactionServerTestCase):
    """Tests for `savepoint`."""

    def test__crashes_if_not_already_within_transaction(self):
        with ExpectedException(TransactionManagementError):
            with savepoint():
                pass

    def test__creates_savepoint_for_transaction_and_post_commit_hooks(self):
        hooks = post_commit_hooks.hooks
        with transaction.atomic():
            self.expectThat(connection.savepoint_ids, HasLength(0))
            with savepoint():
                # We're one savepoint in.
                self.assertThat(connection.savepoint_ids, HasLength(1))
                # Post-commit hooks have been saved.
                self.assertThat(post_commit_hooks.hooks, Not(Is(hooks)))
            self.expectThat(connection.savepoint_ids, HasLength(0))


class TestInTransaction(MAASTransactionServerTestCase):
    """Tests for `in_transaction`."""

    def test__true_within_atomic_block(self):
        with transaction.atomic():
            self.assertTrue(in_transaction())

    def test__false_when_no_transaction_is_active(self):
        self.assertFalse(in_transaction())


class TestValidateInTransaction(MAASTransactionServerTestCase):
    """Tests for `validate_in_transaction`."""

    def test__does_nothing_within_atomic_block(self):
        with transaction.atomic():
            validate_in_transaction(connection)

    def test__explodes_when_no_transaction_is_active(self):
        self.assertRaises(
            TransactionManagementError, validate_in_transaction, connection
        )


class TestPsqlArray(MAASTestCase):
    def test__returns_empty_array(self):
        self.assertEqual(("ARRAY[]", []), psql_array([]))

    def test__returns_params_in_array(self):
        self.assertEqual("ARRAY[%s,%s,%s]", psql_array(["a", "a", "a"])[0])

    def test__returns_params_in_tuple(self):
        params = [factory.make_name("param") for _ in range(3)]
        self.assertEqual(params, psql_array(params)[1])

    def test__returns_cast_to_type(self):
        self.assertEqual(
            ("ARRAY[]::integer[]", []), psql_array([], sql_type="integer")
        )


class TestDisablingDatabaseConnections(MAASTransactionServerTestCase):
    def assertConnectionsEnabled(self):
        for alias in connections:
            self.assertThat(
                connections[alias], IsInstance(BaseDatabaseWrapper)
            )

    def assertConnectionsDisabled(self):
        for alias in connections:
            self.assertEqual(
                DisabledDatabaseConnection, type(connections[alias])
            )

    def test_disable_and_enable_connections(self):
        self.addCleanup(enable_all_database_connections)

        # By default connections are enabled.
        self.assertConnectionsEnabled()

        # Disable all connections.
        disable_all_database_connections()
        self.assertConnectionsDisabled()

        # Back to the start again.
        enable_all_database_connections()
        self.assertConnectionsEnabled()

    def test_disable_can_be_called_multiple_times(self):
        self.addCleanup(enable_all_database_connections)
        disable_all_database_connections()
        self.assertConnectionsDisabled()
        disable_all_database_connections()
        self.assertConnectionsDisabled()

    def test_DisabledDatabaseConnection(self):
        connection = DisabledDatabaseConnection()
        self.assertRaises(RuntimeError, getattr, connection, "connect")
        self.assertRaises(RuntimeError, getattr, connection, "__call__")
        self.assertRaises(RuntimeError, setattr, connection, "foo", "bar")
        self.assertRaises(RuntimeError, delattr, connection, "baz")


class TestTotallyDisconnected(MAASTransactionServerTestCase):
    """Tests for `TotallyDisconnected`."""

    def test__enter_closes_open_connections_and_disables_new_ones(self):
        self.addCleanup(connection.close)
        connection.ensure_connection()
        with TotallyDisconnected():
            self.assertRaises(RuntimeError, getattr, connection, "connect")
        connection.ensure_connection()

    def test__exit_removes_block_on_database_connections(self):
        with TotallyDisconnected():
            self.assertRaises(RuntimeError, getattr, connection, "connect")
        connection.ensure_connection()


class TestExclusivelyConnected(MAASTransactionServerTestCase):
    """Tests for `ExclusivelyConnected`."""

    def test__enter_blows_up_if_there_are_open_connections(self):
        self.addCleanup(connection.close)
        connection.ensure_connection()
        context = ExclusivelyConnected()
        self.assertRaises(AssertionError, context.__enter__)

    def test__enter_does_nothing_if_there_are_no_open_connections(self):
        connection.close()
        context = ExclusivelyConnected()
        context.__enter__()

    def test__exit_closes_open_connections(self):
        self.addCleanup(connection.close)
        connection.ensure_connection()
        self.assertThat(connection.connection, Not(Is(None)))
        context = ExclusivelyConnected()
        context.__exit__()
        self.assertThat(connection.connection, Is(None))


class TestFullyConnected(MAASTransactionServerTestCase):
    """Tests for `FullyConnected`."""

    def assertOpen(self, alias):
        self.assertThat(connections[alias].connection, Not(Is(None)))

    def assertClosed(self, alias):
        self.assertThat(connections[alias].connection, Is(None))

    def test__opens_and_closes_connections(self):
        for alias in connections:
            connections[alias].close()
        for alias in connections:
            self.assertClosed(alias)
        with FullyConnected():
            for alias in connections:
                self.assertOpen(alias)
        for alias in connections:
            self.assertClosed(alias)

    def test__closes_connections_even_if_open_on_entry(self):
        for alias in connections:
            connections[alias].ensure_connection()
        for alias in connections:
            self.assertOpen(alias)
        with FullyConnected():
            for alias in connections:
                self.assertOpen(alias)
        for alias in connections:
            self.assertClosed(alias)


class TestGetModelObjectName(MAASServerTestCase):
    def test__gets_model_object_name_from_manager(self):
        self.assertThat(get_model_object_name(Node.objects), Equals("Node"))

    def test__gets_model_object_name_from_queryset(self):
        self.assertThat(
            get_model_object_name(Node.objects.all()), Equals("Node")
        )

    def test__gets_model_object_name_returns_none_if_not_found(self):
        self.assertThat(get_model_object_name("crazytalk"), Is(None))


class TestCountQueries(MAASServerTestCase):
    def test__logs_all_queries_made_by_func(self):
        def query_func():
            return list(Node.objects.all())

        mock_print = Mock()
        wrapped = count_queries(mock_print)(query_func)
        wrapped()

        query_time = sum(
            [float(query.get("time", 0)) for query in connection.queries]
        )
        self.assertThat(
            mock_print,
            MockCalledOnceWith(
                "[QUERIES] query_func executed 1 queries in %s seconds"
                % query_time
            ),
        )

    def test__resets_queries_between_calls(self):
        def query_func():
            return list(Node.objects.all())

        mock_print = Mock()
        wrapped = count_queries(mock_print)(query_func)

        # First call.
        wrapped()
        query_time_one = sum(
            [float(query.get("time", 0)) for query in connection.queries]
        )

        # Second call.
        wrapped()
        query_time_two = sum(
            [float(query.get("time", 0)) for query in connection.queries]
        )

        # Print called twice.
        self.assertThat(
            mock_print,
            MockCallsMatch(
                call(
                    "[QUERIES] query_func executed 1 queries in %s seconds"
                    % query_time_one
                ),
                call(
                    "[QUERIES] query_func executed 1 queries in %s seconds"
                    % query_time_two
                ),
            ),
        )

    def test__logs_all_queries_made(self):
        def query_func():
            return list(Node.objects.all())

        log_sql_calls(query_func)

        mock_print = Mock()
        wrapped = count_queries(mock_print)(query_func)
        wrapped()
        query_time = sum(
            [float(query.get("time", 0)) for query in connection.queries]
        )

        # Print called twice.
        self.assertThat(
            mock_print,
            MockCallsMatch(
                call(
                    "[QUERIES] query_func executed 1 queries in %s seconds"
                    % query_time
                ),
                call("[QUERIES] === Start SQL Log: query_func ==="),
                call("[QUERIES] %s" % connection.queries[0]["sql"]),
                call("[QUERIES] === End SQL Log: query_func ==="),
            ),
        )
