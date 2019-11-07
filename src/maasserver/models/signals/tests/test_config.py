# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test the behaviour of config signals."""

__all__ = []

from maasserver.models import domain as domain_module
from maasserver.models.config import Config
from maasserver.testing.testcase import MAASServerTestCase
from maastesting.matchers import MockCalledOnceWith


class TestConfigSignals(MAASServerTestCase):
    def test_changing_kms_host_triggers_update(self):
        dns_kms_setting_changed = self.patch_autospec(
            domain_module, "dns_kms_setting_changed"
        )
        Config.objects.set_config("windows_kms_host", "8.8.8.8")
        self.assertThat(dns_kms_setting_changed, MockCalledOnceWith())
