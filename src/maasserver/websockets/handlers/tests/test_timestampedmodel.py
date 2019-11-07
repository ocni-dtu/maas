# Copyright 2015-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maasserver.websockets.handlers.timestampedmodel`"""

__all__ = []

from datetime import datetime

from maasserver.websockets.handlers.timestampedmodel import (
    TimestampedModelHandler,
)
from maastesting.testcase import MAASTestCase


class TestTimeStampedModelHandler(MAASTestCase):
    def test_has_abstract_set_to_true(self):
        handler = TimestampedModelHandler(None, {}, None)
        self.assertTrue(handler._meta.abstract)

    def test_adds_created_and_updated_to_non_changeable(self):
        handler = TimestampedModelHandler(None, {}, None)
        self.assertItemsEqual(
            ["created", "updated"], handler._meta.non_changeable
        )

    def test_doesnt_overwrite_other_non_changeable_fields(self):
        class TestHandler(TimestampedModelHandler):
            class Meta:
                non_changeable = ["other", "extra"]

        handler = TestHandler(None, {}, None)
        self.assertItemsEqual(
            ["other", "extra", "created", "updated"],
            handler._meta.non_changeable,
        )

    def test_dehydrate_created_converts_datetime_to_string(self):
        now = datetime.now()
        handler = TimestampedModelHandler(None, {}, None)
        self.assertEqual(
            now.strftime("%a, %d %b. %Y %H:%M:%S"),
            handler.dehydrate_created(now),
        )

    def test_dehydrate_updated_converts_datetime_to_string(self):
        now = datetime.now()
        handler = TimestampedModelHandler(None, {}, None)
        self.assertEqual(
            now.strftime("%a, %d %b. %Y %H:%M:%S"),
            handler.dehydrate_updated(now),
        )
