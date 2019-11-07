# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for DNSResource forms."""

__all__ = []

import random
from unittest.mock import Mock

from maasserver.forms.dnsresource import DNSResourceForm
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.orm import reload_object


class TestDNSResourceForm(MAASServerTestCase):
    def test__creates_dnsresource(self):
        name = factory.make_name("dnsresource")
        sip = factory.make_StaticIPAddress()
        domain = factory.make_Domain()
        request = Mock()
        request.user = factory.make_User()
        form = DNSResourceForm(
            {"name": name, "domain": domain.id, "ip_addresses": [sip.id]},
            request=request,
        )
        self.assertTrue(form.is_valid(), form.errors)
        dnsresource = form.save()
        self.assertEqual(name, dnsresource.name)
        self.assertEqual(domain.id, dnsresource.domain.id)
        self.assertEqual(sip.id, dnsresource.ip_addresses.first().id)

    def test__accepts_string_for_ip_addresses(self):
        name = factory.make_name("dnsresource")
        sip = factory.make_StaticIPAddress()
        domain = factory.make_Domain()
        form = DNSResourceForm(
            {"name": name, "domain": domain.id, "ip_addresses": str(sip.id)}
        )
        self.assertTrue(form.is_valid(), form.errors)
        dnsresource = form.save()
        self.assertEqual(name, dnsresource.name)
        self.assertEqual(domain.id, dnsresource.domain.id)
        self.assertEqual(sip.id, dnsresource.ip_addresses.first().id)

    def test__creates_staticipaddresses(self):
        name = factory.make_name("dnsresource")
        domain = factory.make_Domain()
        ips = [factory.make_ip_address() for _ in range(3)]
        request = Mock()
        request.user = factory.make_User()
        form = DNSResourceForm(
            {"name": name, "domain": domain.id, "ip_addresses": " ".join(ips)},
            request=request,
        )
        self.assertTrue(form.is_valid(), form.errors)
        dnsresource = form.save()
        self.assertEqual(name, dnsresource.name)
        self.assertEqual(domain.id, dnsresource.domain.id)
        actual_ips = dnsresource.ip_addresses.all()
        actual = {str(ip.ip) for ip in actual_ips}
        self.assertItemsEqual(set(ips), actual)
        actual_users = {ip.user_id for ip in actual_ips}
        self.assertItemsEqual({request.user.id}, actual_users)

    def test__accepts_mix_of_id_and_ipaddress(self):
        name = factory.make_name("dnsresource")
        domain = factory.make_Domain()
        ips = [factory.make_StaticIPAddress() for _ in range(6)]
        in_vals = [
            str(ip.id) if factory.pick_bool() else str(ip.ip) for ip in ips
        ]
        form = DNSResourceForm(
            {
                "name": name,
                "domain": domain.id,
                "ip_addresses": " ".join(in_vals),
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        dnsresource = form.save()
        self.assertEqual(name, dnsresource.name)
        self.assertEqual(domain.id, dnsresource.domain.id)
        actual = {ip.id for ip in dnsresource.ip_addresses.all()}
        self.assertItemsEqual(set(ip.id for ip in ips), actual)

    def test_does_not_require_ip_addresses(self):
        name = factory.make_name("dnsresource")
        domain = factory.make_Domain()
        form = DNSResourceForm({"name": name, "domain": domain.id})
        self.assertTrue(form.is_valid(), form.errors)
        dnsresource = form.save()
        self.assertEqual(name, dnsresource.name)
        self.assertEqual(domain.id, dnsresource.domain.id)

    def test_accepts_address_ttl(self):
        name = factory.make_name("dnsresource")
        domain = factory.make_Domain()
        ttl = random.randint(1, 1000)
        form = DNSResourceForm(
            {"name": name, "domain": domain.id, "address_ttl": ttl}
        )
        self.assertTrue(form.is_valid(), form.errors)
        dnsresource = form.save()
        self.assertEqual(name, dnsresource.name)
        self.assertEqual(domain.id, dnsresource.domain.id)
        self.assertEqual(ttl, dnsresource.address_ttl)

    def test_accepts_address_ttl_equals_none(self):
        name = factory.make_name("dnsresource")
        domain = factory.make_Domain()
        ttl = random.randint(1, 1000)
        form = DNSResourceForm(
            {"name": name, "domain": domain.id, "address_ttl": ttl}
        )
        self.assertTrue(form.is_valid(), form.errors)
        dnsresource = form.save()
        form = DNSResourceForm(
            instance=dnsresource, data={"address_ttl": None}
        )
        self.assertTrue(form.is_valid(), form.errors)
        dnsresource = form.save()
        self.assertEqual(name, dnsresource.name)
        self.assertEqual(domain.id, dnsresource.domain.id)
        self.assertEqual(None, dnsresource.address_ttl)

    def test__doesnt_require_name_on_update(self):
        dnsresource = factory.make_DNSResource()
        form = DNSResourceForm(instance=dnsresource, data={})
        self.assertTrue(form.is_valid(), form.errors)

    def test__updates_dnsresource(self):
        dnsresource = factory.make_DNSResource()
        new_name = factory.make_name("new")
        new_sip_ids = [factory.make_StaticIPAddress().id]
        new_ttl = random.randint(1, 1000)
        form = DNSResourceForm(
            instance=dnsresource,
            data={
                "name": new_name,
                "ip_addresses": new_sip_ids,
                "address_ttl": new_ttl,
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(new_name, reload_object(dnsresource).name)
        self.assertEqual(new_ttl, reload_object(dnsresource).address_ttl)
        self.assertItemsEqual(
            new_sip_ids,
            [ip.id for ip in reload_object(dnsresource).ip_addresses.all()],
        )

    def test__update_allows_multiple_ips(self):
        dnsresource = factory.make_DNSResource()
        new_name = factory.make_name("new")
        new_sip_ids = [factory.make_StaticIPAddress().id for _ in range(3)]
        form = DNSResourceForm(
            instance=dnsresource,
            data={
                "name": new_name,
                "ip_addresses": " ".join(str(id) for id in new_sip_ids),
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(new_name, reload_object(dnsresource).name)
        self.assertItemsEqual(
            new_sip_ids,
            [ip.id for ip in reload_object(dnsresource).ip_addresses.all()],
        )
