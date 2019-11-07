# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test :class:`Sequence`."""

__all__ = []

from itertools import islice
import random

from django.db import connection, transaction
from django.db.utils import DatabaseError, DataError, ProgrammingError
from maasserver.sequence import Sequence
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from testtools import ExpectedException


class TestSequence(MAASServerTestCase):
    def query_seq(self, name):
        with connection.cursor() as cursor:
            cursor.execute("SELECT nextval(%s)", [name])
            return cursor.fetchone()[0]

    def test_create_sequence(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        seq.create()
        val = self.query_seq(seq.name)
        self.assertEqual(1, val)

    def test_create_sequence_fails_if_sequence_exists(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        seq.create()
        self.assertRaisesRegex(ProgrammingError, "already exists", seq.create)

    def test_create_if_not_exists_does_not_fail_if_sequence_exists(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        seq.create()
        seq.create_if_not_exists()
        self.assertEqual(1, next(seq))

    def test_sequence_respects_minvalue(self):
        name = factory.make_name("seq", sep="")
        minvalue = random.randint(1, 50)
        seq = Sequence(name, minvalue=minvalue)
        seq.create()
        val = self.query_seq(seq.name)
        self.assertEqual(minvalue, val)

    def test_sequence_respects_start(self):
        name = factory.make_name("seq", sep="")
        start = random.randint(5, 50)
        seq = Sequence(name, start=start)
        seq.create()
        val = self.query_seq(seq.name)
        self.assertEqual(start, val)

    def test_sequence_respects_increment(self):
        name = factory.make_name("seq", sep="")
        increment = random.randint(1, 50)
        seq = Sequence(name, increment=increment)
        seq.create()
        val = self.query_seq(seq.name)
        val = self.query_seq(seq.name)
        self.assertEqual(1 + increment, val)

    def test_sequence_respects_maxvalue_and_cycles(self):
        name = factory.make_name("seq", sep="")
        maxvalue = random.randint(10, 50)
        seq = Sequence(name, maxvalue=maxvalue)
        seq.create()
        cursor = connection.cursor()
        query = "ALTER SEQUENCE %s" % seq.name
        cursor.execute(query + " RESTART WITH %s", [maxvalue])
        val = self.query_seq(seq.name)
        val = self.query_seq(seq.name)
        self.assertEqual(1, val)

    def test_sequence_cycling_can_be_prevented(self):
        seq = Sequence("alice", maxvalue=2, cycle=False)
        seq.create()
        self.assertSequenceEqual([1, 2], [next(seq), next(seq)])
        self.assertRaisesRegex(
            DataError, "nextval: reached maximum value of sequence", next, seq
        )

    def test_sequence_can_be_owned(self):
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE alice (bob INT)")
        seq = Sequence("carol", owner="alice.bob")
        seq.create()
        self.assertEqual(1, next(seq))
        # Dropping the table drops the sequence too.
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE alice")
        self.assertRaisesRegex(
            ProgrammingError,
            'relation "carol" does not exist',
            self.query_seq,
            seq.name,
        )

    def test_sequence_will_be_created_automatically_on_first_access(self):
        seq = Sequence("dave")
        # Accessing the sequence directly in the database we find that it's
        # not there.
        with ExpectedException(ProgrammingError, ".* does not exist"):
            with transaction.atomic():
                self.query_seq(seq.name)
        # Iterating via `Sequence` automatically vivifies it.
        self.assertEqual(1, next(seq))
        self.assertEqual(2, self.query_seq(seq.name))

    def test_drop_sequence(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        seq.create()
        seq.drop()
        self.assertRaisesRegex(
            DatabaseError, "does not exist", self.query_seq, seq.name
        )

    def test_drop_sequence_fails_if_sequence_does_not_exist(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        self.assertRaisesRegex(ProgrammingError, "does not exist", seq.drop)

    def test_drop_if_exists_does_not_fail_if_sequence_does_not_exist(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        seq.drop_if_exists()

    def test_next_returns_sequential_values(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        seq.create()
        self.assertSequenceEqual(
            list(range(1, 11)), [next(seq) for _ in range(10)]
        )

    def test_iteration_returns_sequential_values(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        seq.create()
        self.assertSequenceEqual(list(range(1, 11)), list(islice(seq, 10)))

    def test_current_returns_none_when_table_does_not_exist(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        self.assertIsNone(seq.current())

    def test_current_returns_none_when_no_current_value(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        seq.create()
        self.assertIsNone(seq.current())

    def test_current_returns_current_value(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        seq.create()
        expected = next(seq)
        self.assertEqual(expected, seq.current())
        self.assertEqual(expected, seq.current())

    def test_current_returns_correct_value(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        seq.create()
        expected = next(seq)
        self.assertEqual(expected, seq.current())
        self.assertEqual(expected, seq.current())
        expected = next(seq)
        self.assertEqual(expected, seq.current())
        self.assertEqual(expected, seq.current())

    def test_set_value_sets_value(self):
        name = factory.make_name("seq", sep="")
        seq = Sequence(name)
        seq.create()
        expected = random.randint(2000, 9999)
        seq.set_value(expected)
        self.assertEqual(expected, next(seq))
