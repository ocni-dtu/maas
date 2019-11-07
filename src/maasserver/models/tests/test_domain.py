# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the Domain model."""

__all__ = []


import random

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import ProtectedError
from maasserver.dns.zonegenerator import get_hostname_dnsdata_mapping, lazydict
from maasserver.models.config import Config
from maasserver.models.dnsdata import DNSData, HostnameRRsetMapping
from maasserver.models.dnsresource import DNSResource
from maasserver.models.domain import DEFAULT_DOMAIN_NAME, Domain
from maasserver.models.staticipaddress import StaticIPAddress
from maasserver.permissions import NodePermission
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from netaddr import IPAddress
from testtools.matchers import Equals, HasLength, MatchesStructure
from testtools.testcase import ExpectedException


class TestDomainManagerGetDomainOr404(MAASServerTestCase):
    def test__user_view_returns_domain(self):
        user = factory.make_User()
        domain = factory.make_Domain()
        self.assertEqual(
            domain,
            Domain.objects.get_domain_or_404(
                domain.id, user, NodePermission.view
            ),
        )

    def test__user_view_returns_domain_by_name(self):
        user = factory.make_User()
        domain = factory.make_Domain()
        self.assertEqual(
            domain,
            Domain.objects.get_domain_or_404(
                "name:%s" % domain.name, user, NodePermission.view
            ),
        )

    def test__user_edit_raises_PermissionError(self):
        user = factory.make_User()
        domain = factory.make_Domain()
        self.assertRaises(
            PermissionDenied,
            Domain.objects.get_domain_or_404,
            domain.id,
            user,
            NodePermission.edit,
        )

    def test__user_admin_raises_PermissionError(self):
        user = factory.make_User()
        domain = factory.make_Domain()
        self.assertRaises(
            PermissionDenied,
            Domain.objects.get_domain_or_404,
            domain.id,
            user,
            NodePermission.admin,
        )

    def test__admin_view_returns_domain(self):
        admin = factory.make_admin()
        domain = factory.make_Domain()
        self.assertEqual(
            domain,
            Domain.objects.get_domain_or_404(
                domain.id, admin, NodePermission.view
            ),
        )

    def test__admin_view_returns_domain_by_name(self):
        admin = factory.make_admin()
        domain = factory.make_Domain()
        self.assertEqual(
            domain,
            Domain.objects.get_domain_or_404(
                "name:%s" % domain.name, admin, NodePermission.view
            ),
        )

    def test__admin_edit_returns_domain(self):
        admin = factory.make_admin()
        domain = factory.make_Domain()
        self.assertEqual(
            domain,
            Domain.objects.get_domain_or_404(
                domain.id, admin, NodePermission.edit
            ),
        )

    def test__admin_admin_returns_domain(self):
        admin = factory.make_admin()
        domain = factory.make_Domain()
        self.assertEqual(
            domain,
            Domain.objects.get_domain_or_404(
                domain.id, admin, NodePermission.admin
            ),
        )


class TestDomainManager(MAASServerTestCase):
    def test__default_specifier_matches_id(self):
        factory.make_Domain()
        domain = factory.make_Domain()
        factory.make_Domain()
        id = domain.id
        self.assertItemsEqual(
            Domain.objects.filter_by_specifiers("%s" % id), [domain]
        )

    def test__default_specifier_matches_name(self):
        factory.make_Domain()
        name = factory.make_name("domain-")
        domain = factory.make_Domain(name=name)
        factory.make_Domain()
        self.assertItemsEqual(
            Domain.objects.filter_by_specifiers(name), [domain]
        )

    def test__name_specifier_matches_name(self):
        factory.make_Domain()
        name = factory.make_name("domain-")
        domain = factory.make_Domain(name=name)
        factory.make_Domain()
        self.assertItemsEqual(
            Domain.objects.filter_by_specifiers("name:%s" % name), [domain]
        )


class DomainTest(MAASServerTestCase):
    def test_creates_domain(self):
        name = factory.make_name("name")
        domain = Domain(name=name)
        domain.save()
        domain_from_db = Domain.objects.get(name=name)
        self.assertThat(domain_from_db, MatchesStructure.byEquality(name=name))

    def test_create_strips_trailing_dot(self):
        name = factory.make_name("name")
        domain = Domain(name=name + ".")
        domain.save()
        domain_from_db = Domain.objects.get(name=name)
        self.assertThat(domain_from_db, MatchesStructure.byEquality(name=name))

    def test_get_default_domain_creates_default_domain(self):
        default_domain = Domain.objects.get_default_domain()
        self.assertEqual(0, default_domain.id)
        self.assertEqual(DEFAULT_DOMAIN_NAME, default_domain.get_name())

    def test_invalid_name_raises_exception(self):
        self.assertRaises(
            ValidationError, factory.make_Domain, name="invalid*name"
        )

    def test_get_default_domain_is_idempotent(self):
        default_domain = Domain.objects.get_default_domain()
        default_domain2 = Domain.objects.get_default_domain()
        self.assertEqual(default_domain.id, default_domain2.id)

    def test_is_default_detects_default_domain(self):
        default_domain = Domain.objects.get_default_domain()
        self.assertTrue(default_domain.is_default())

    def test_is_default_detects_non_default_domain(self):
        name = factory.make_name("name")
        domain = factory.make_Domain(name=name)
        self.assertFalse(domain.is_default())

    def test_can_be_deleted_if_does_not_contain_resources(self):
        name = factory.make_name("name")
        domain = factory.make_Domain(name=name)
        domain.delete()
        self.assertItemsEqual([], Domain.objects.filter(name=name))

    def test_cant_be_deleted_if_contains_resources(self):
        domain = factory.make_Domain()
        factory.make_DNSResource(domain=domain)
        with ExpectedException(ProtectedError):
            domain.delete()

    def test_add_delegations_may_do_nothing(self):
        domain = factory.make_Domain()
        mapping = {}
        domain.add_delegations(
            mapping,
            Domain.objects.get_default_domain().name,
            [IPAddress("::1")],
            30,
        )
        self.assertEqual({}, mapping)

    def test_add_delegations_adds_delegation(self):
        parent = factory.make_Domain()
        name = factory.make_name()
        default_name = Domain.objects.get_default_domain().name
        factory.make_Domain(name="%s.%s" % (name, parent.name))
        mappings = lazydict(get_hostname_dnsdata_mapping)
        mapping = mappings[parent]
        parent.add_delegations(mapping, default_name, [IPAddress("::1")], 30)
        expected_map = HostnameRRsetMapping(rrset={(30, "NS", default_name)})
        self.assertEqual(expected_map, mapping[name])

    def test_add_delegations_adds_nsrrset_and_glue(self):
        parent = factory.make_Domain()
        name = factory.make_name()
        child = factory.make_Domain(name="%s.%s" % (name, parent.name))
        default_name = Domain.objects.get_default_domain().name
        dnsrr = factory.make_DNSResource(name="@", domain=child)
        nsname = factory.make_name()
        factory.make_DNSData(
            dnsresource=dnsrr,
            rrtype="NS",
            rrdata="%s.%s." % (nsname, child.name),
        )
        nsrr = factory.make_DNSResource(name=nsname, domain=child)
        other_name = factory.make_name()
        factory.make_DNSResource(name=other_name, domain=parent)
        factory.make_DNSData(
            dnsresource=dnsrr,
            rrtype="NS",
            rrdata="%s.%s." % (other_name, parent.name),
        )
        mappings = lazydict(get_hostname_dnsdata_mapping)
        mapping = mappings[parent]
        parent.add_delegations(mapping, default_name, [IPAddress("::1")], 30)
        expected_map = {
            name: HostnameRRsetMapping(
                rrset={
                    (30, "NS", default_name),
                    (30, "NS", "%s.%s." % (nsname, child.name)),
                    (30, "NS", "%s.%s." % (other_name, parent.name)),
                }
            )
        }
        for sip in nsrr.ip_addresses.all():
            if IPAddress(sip.ip).version == 6:
                expected_map[nsname] = HostnameRRsetMapping(
                    rrset={(30, "AAAA", sip.ip)}
                )
            else:
                expected_map[nsname] = HostnameRRsetMapping(
                    rrset={(30, "A", sip.ip)}
                )
        self.assertEqual(expected_map, mapping)

    def test_add_delegations_adds_nsrrset_and_glue_in_depth(self):
        parent = factory.make_Domain()
        name = factory.make_name()
        child = factory.make_Domain(name="%s.%s" % (name, parent.name))
        default_name = Domain.objects.get_default_domain().name
        g_name = factory.make_name()
        grandchild = factory.make_Domain(name="%s.%s" % (g_name, child.name))
        dnsrr = factory.make_DNSResource(name="@", domain=child)
        nsname = factory.make_name()
        factory.make_DNSData(
            dnsresource=dnsrr,
            rrtype="NS",
            rrdata="%s.%s." % (nsname, grandchild.name),
        )
        nsrr = factory.make_DNSResource(name=nsname, domain=grandchild)
        other_name = factory.make_name()
        factory.make_DNSResource(name=other_name, domain=parent)
        factory.make_DNSData(
            dnsresource=dnsrr,
            rrtype="NS",
            rrdata="%s.%s." % (other_name, parent.name),
        )
        mappings = lazydict(get_hostname_dnsdata_mapping)
        mapping = mappings[parent]
        expected_map = {
            name: HostnameRRsetMapping(
                rrset={
                    (30, "NS", default_name),
                    (30, "NS", "%s.%s." % (nsname, grandchild.name)),
                    (30, "NS", "%s.%s." % (other_name, parent.name)),
                }
            )
        }
        ns_part = "%s.%s" % (nsname, g_name)
        for sip in nsrr.ip_addresses.all():
            if IPAddress(sip.ip).version == 6:
                expected_map[ns_part] = HostnameRRsetMapping(
                    rrset={(30, "AAAA", sip.ip)}
                )
            else:
                expected_map[ns_part] = HostnameRRsetMapping(
                    rrset={(30, "A", sip.ip)}
                )
        parent.add_delegations(mapping, default_name, [IPAddress("::1")], 30)
        self.assertEqual(expected_map, mapping)

    def test_add_delegations_allows_dots(self):
        parent = factory.make_Domain()
        name = "%s.%s" % (factory.make_name(), factory.make_name())
        factory.make_Domain(name="%s.%s" % (name, parent.name))
        default_name = Domain.objects.get_default_domain().name
        mappings = lazydict(get_hostname_dnsdata_mapping)
        mapping = mappings[parent]
        parent.add_delegations(mapping, default_name, [IPAddress("::1")], 30)
        expected_map = HostnameRRsetMapping(rrset={(30, "NS", default_name)})
        self.assertEqual(expected_map, mapping[name])

    def test_add_delegations_stops_at_one_deep(self):
        parent = factory.make_Domain()
        name = factory.make_name()
        child = factory.make_Domain(name="%s.%s" % (name, parent.name))
        default_name = Domain.objects.get_default_domain().name
        factory.make_Domain(name="%s.%s" % (factory.make_name(), child.name))
        mappings = lazydict(get_hostname_dnsdata_mapping)
        mapping = mappings[parent]
        parent.add_delegations(mapping, default_name, [IPAddress("::1")], 30)
        expected_map = HostnameRRsetMapping(rrset={(30, "NS", default_name)})
        self.assertEqual(expected_map, mapping[name])

    def test_add_delegations_does_not_list_region_for_non_auth(self):
        parent = factory.make_Domain()
        name = factory.make_name()
        child = factory.make_Domain(
            name="%s.%s" % (name, parent.name), authoritative=False
        )
        default_name = Domain.objects.get_default_domain().name
        ns_name = "%s.%s." % (factory.make_name("h"), factory.make_name("d"))
        factory.make_DNSData(
            name="@", domain=child, rrtype="NS", rrdata=ns_name
        )
        mappings = lazydict(get_hostname_dnsdata_mapping)
        mapping = mappings[parent]
        parent.add_delegations(mapping, default_name, [IPAddress("::1")], 30)
        expected_map = HostnameRRsetMapping(rrset={(30, "NS", ns_name)})
        self.assertEqual(expected_map, mapping[name])

    def test_save_migrates_dnsresource(self):
        p_name = "%s.%s" % (factory.make_name(), factory.make_name())
        c_name = factory.make_name()
        parent = factory.make_Domain(name=p_name)
        dnsrr = factory.make_DNSResource(name=c_name, domain=parent)
        child = factory.make_Domain(name="%s.%s" % (c_name, p_name))
        dnsrr_from_db = DNSResource.objects.get(id=dnsrr.id)
        self.assertEqual("@", dnsrr_from_db.name)
        self.assertEqual(child, dnsrr_from_db.domain)
        self.assertItemsEqual(
            [], DNSResource.objects.filter(name=c_name, domain=parent)
        )

    def test_update_kms_srv_deletes_srv_records(self):
        domain = factory.make_Domain()
        target = "%s.%s" % (factory.make_name(), factory.make_name())
        factory.make_DNSData(
            domain=domain,
            name="_vlmcs._tcp",
            rrtype="SRV",
            rrdata="0 0 1688 %s." % target,
        )
        domain.update_kms_srv("")
        # We would restrict it more, but we just deleted it...
        rrset = DNSData.objects.filter(rrtype="SRV")
        self.assertEqual(0, rrset.count())

    def test_update_kms_srv_creates_srv_records(self):
        domain = factory.make_Domain()
        target = "%s.%s" % (factory.make_name(), factory.make_name())
        domain.update_kms_srv(target)
        srvrr = DNSData.objects.get(
            rrtype="SRV",
            dnsresource__name="_vlmcs._tcp",
            dnsresource__domain_id=domain.id,
        )
        self.assertEqual("0 0 1688 %s." % target, srvrr.rrdata)

    def test_update_kms_srv_creates_srv_records_on_all_domains(self):
        domains = [factory.make_Domain() for _ in range(random.randint(1, 10))]
        target = "%s.%s" % (factory.make_name(), factory.make_name())
        Config.objects.set_config("windows_kms_host", target)
        for domain in domains:
            srvrr = DNSData.objects.get(
                rrtype="SRV",
                dnsresource__name="_vlmcs._tcp",
                dnsresource__domain_id=domain.id,
            )
            self.assertEqual("0 0 1688 %s." % target, srvrr.rrdata)


class TestRenderRRData(MAASServerTestCase):
    def render_rrdata(self, domain, for_list=False):
        rr_map = DNSData.objects.get_hostname_dnsdata_mapping(
            domain, raw_ttl=True
        )
        ip_map = StaticIPAddress.objects.get_hostname_ip_mapping(
            domain, raw_ttl=True
        )
        for hostname, info in ip_map.items():
            hostname = hostname[: -len(domain.name) - 1]
            if info.system_id is not None:
                rr_map[hostname].system_id = info.system_id
            if info.user_id is not None:
                rr_map[hostname].user_id = info.user_id
            for ip in info.ips:
                if IPAddress(ip).version == 4:
                    rr_map[hostname].rrset.add((info.ttl, "A", ip, None))
                else:
                    rr_map[hostname].rrset.add((info.ttl, "AAAA", ip, None))
                rr_map[hostname].dnsresource_id = info.dnsresource_id
        rrsets = [
            {
                "name": name,
                "system_id": info.system_id,
                "node_type": info.node_type,
                "user_id": info.user_id,
                "dnsresource_id": info.dnsresource_id,
                "ttl": ttl,
                "rrtype": rrtype,
                "rrdata": rrdata,
                "dnsdata_id": dnsdata_id,
            }
            for name, info in rr_map.items()
            for ttl, rrtype, rrdata, dnsdata_id in info.rrset
        ]
        return rrsets

    def test_render_json_for_related_rrdata_returns_correct_values(self):
        domain = factory.make_Domain()
        factory.make_DNSData(domain=domain, rrtype="NS")
        dnsdata = factory.make_DNSData(domain=domain, rrtype="MX")
        factory.make_DNSData(dnsresource=dnsdata.dnsresource, rrtype="MX")
        factory.make_DNSResource(domain=domain)
        node = factory.make_Node_with_Interface_on_Subnet(domain=domain)
        factory.make_DNSResource(name=node.hostname, domain=domain)
        expected = self.render_rrdata(domain, for_list=True)
        actual = domain.render_json_for_related_rrdata(for_list=True)
        self.assertItemsEqual(expected, actual)
        expected = self.render_rrdata(domain, for_list=False)
        actual = domain.render_json_for_related_rrdata(for_list=False)
        self.assertItemsEqual(expected, actual)

    def test_render_json_for_related_rrdata_includes_user_id(self):
        domain = factory.make_Domain()
        node_name = factory.make_name("node")
        user = factory.make_User()
        factory.make_Node_with_Interface_on_Subnet(
            hostname=node_name, domain=domain, owner=user
        )
        dnsrr = factory.make_DNSResource(domain=domain, name=node_name)
        factory.make_DNSData(dnsresource=dnsrr, ip_addresses=True)
        expected = self.render_rrdata(domain, for_list=False)
        actual = domain.render_json_for_related_rrdata(for_list=True)
        self.assertEqual(actual, expected)
        for record in actual:
            self.assertEqual(record["user_id"], user.id)

    def test_renders_as_dictionary(self):
        domain = factory.make_Domain()
        name1 = factory.make_name(prefix="a")
        name2 = factory.make_name(prefix="b")
        factory.make_DNSData(name=name1, domain=domain, rrtype="MX")
        rrdata_list = domain.render_json_for_related_rrdata(as_dict=False)
        rrdata_dict = domain.render_json_for_related_rrdata(as_dict=True)
        self.assertThat(rrdata_dict[name1], Equals([rrdata_list[0]]))
        self.assertThat(rrdata_dict[name1], HasLength(1))
        factory.make_DNSData(name=name1, domain=domain, rrtype="MX")
        factory.make_DNSData(name=name2, domain=domain, rrtype="NS")
        rrdata_dict = domain.render_json_for_related_rrdata(as_dict=True)
        self.assertThat(rrdata_dict[name1], HasLength(2))
        self.assertThat(rrdata_dict[name2], HasLength(1))
