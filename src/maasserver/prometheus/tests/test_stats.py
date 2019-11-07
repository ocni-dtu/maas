# Copyright 2014-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test maasserver.prometheus.stats."""

__all__ = []

import http.client
import json
from unittest import mock

from django.db import transaction
from maasserver.enum import IPADDRESS_TYPE, IPRANGE_TYPE
from maasserver.models import Config
from maasserver.prometheus import stats
from maasserver.prometheus.stats import (
    push_stats_to_prometheus,
    STATS_DEFINITIONS,
    update_prometheus_stats,
)
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.django_urls import reverse
from maastesting.matchers import (
    MockCalledOnce,
    MockCalledOnceWith,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase
from maastesting.twisted import extract_result
import prometheus_client
from provisioningserver.prometheus.utils import create_metrics
from provisioningserver.utils.twisted import asynchronous
from twisted.application.internet import TimerService
from twisted.internet.defer import fail


class TestPrometheusHandler(MAASServerTestCase):
    def test_prometheus_stats_handler_not_found_disabled(self):
        Config.objects.set_config("prometheus_enabled", False)
        self.patch(stats, "PROMETHEUS_SUPPORTED", True)
        response = self.client.get(reverse("metrics"))
        self.assertEqual("text/html; charset=utf-8", response["Content-Type"])
        self.assertEquals(response.status_code, http.client.NOT_FOUND)

    def test_prometheus_stats_handler_not_found_not_supported(self):
        Config.objects.set_config("prometheus_enabled", True)
        self.patch(stats, "PROMETHEUS_SUPPORTED", False)
        response = self.client.get(reverse("metrics"))
        self.assertEqual("text/html; charset=utf-8", response["Content-Type"])
        self.assertEquals(response.status_code, http.client.NOT_FOUND)

    def test_prometheus_stats_handler_returns_success(self):
        Config.objects.set_config("prometheus_enabled", True)
        mock_prom_cli = self.patch(stats, "prom_cli")
        mock_prom_cli.generate_latest.return_value = {}
        response = self.client.get(reverse("metrics"))
        self.assertEqual("text/plain", response["Content-Type"])
        self.assertEquals(response.status_code, http.client.OK)

    def test_prometheus_stats_handler_returns_metrics(self):
        Config.objects.set_config("prometheus_enabled", True)
        response = self.client.get(reverse("metrics"))
        content = response.content.decode("utf-8")
        metrics = (
            "maas_machines",
            "maas_nodes",
            "maas_net_spaces",
            "maas_net_fabrics",
            "maas_net_vlans",
            "maas_net_subnets_v4",
            "maas_net_subnets_v6",
            "maas_machines_total_mem",
            "maas_machines_total_cpu",
            "maas_machines_total_storage",
            "maas_kvm_pods",
            "maas_kvm_machines",
            "maas_kvm_cores",
            "maas_kvm_memory",
            "maas_kvm_storage",
            "maas_kvm_overcommit_cores",
            "maas_kvm_overcommit_memory",
            "maas_machine_arches",
        )
        for metric in metrics:
            self.assertIn("TYPE {} gauge".format(metric), content)

    def test_prometheus_stats_handler_include_maas_id_label(self):
        self.patch(stats, "get_machines_by_architecture").return_value = {
            "amd64": 2,
            "i386": 1,
        }
        Config.objects.set_config("uuid", "abcde")
        Config.objects.set_config("prometheus_enabled", True)
        response = self.client.get(reverse("metrics"))
        content = response.content.decode("utf-8")
        metrics = (
            "maas_machines",
            "maas_nodes",
            "maas_net_spaces",
            "maas_net_fabrics",
            "maas_net_vlans",
            "maas_net_subnets_v4",
            "maas_net_subnets_v6",
            "maas_machines_total_mem",
            "maas_machines_total_cpu",
            "maas_machines_total_storage",
            "maas_kvm_pods",
            "maas_kvm_machines",
            "maas_kvm_cores",
            "maas_kvm_memory",
            "maas_kvm_storage",
            "maas_kvm_overcommit_cores",
            "maas_kvm_overcommit_memory",
            "maas_machine_arches",
        )
        for metric in metrics:
            for line in content.splitlines():
                if line.startswith("maas_"):
                    self.assertIn('maas_id="abcde"'.format(metric), line)


class TestPrometheus(MAASServerTestCase):
    def test_update_prometheus_stats(self):
        self.patch(stats, "prom_cli")
        # general values
        values = {
            "machine_status": {"random_status": 0},
            "controllers": {"regions": 0},
            "nodes": {"machines": 0},
            "network_stats": {"spaces": 0},
            "machine_stats": {"total_cpu": 0},
        }
        mock = self.patch(stats, "get_maas_stats")
        mock.return_value = json.dumps(values)
        # architecture
        arches = {"amd64": 0, "i386": 0}
        mock_arches = self.patch(stats, "get_machines_by_architecture")
        mock_arches.return_value = arches
        # pods
        pods = {
            "kvm_pods": 0,
            "kvm_machines": 0,
            "kvm_available_resources": {
                "cores": 10,
                "memory": 20,
                "storage": 30,
                "over_cores": 100,
                "over_memory": 200,
            },
            "kvm_utilized_resources": {
                "cores": 5,
                "memory": 10,
                "storage": 15,
            },
        }
        mock_pods = self.patch(stats, "get_kvm_pods_stats")
        mock_pods.return_value = pods
        subnet_stats = {
            "1.2.0.0/16": {
                "available": 2 ** 16 - 3,
                "dynamic_available": 0,
                "dynamic_used": 0,
                "reserved_available": 0,
                "reserved_used": 0,
                "static": 0,
                "unavailable": 1,
            },
            "::1/128": {
                "available": 1,
                "dynamic_available": 0,
                "dynamic_used": 0,
                "reserved_available": 0,
                "reserved_used": 0,
                "static": 0,
                "unavailable": 0,
            },
        }
        mock_subnet_stats = self.patch(stats, "get_subnets_utilisation_stats")
        mock_subnet_stats.return_value = subnet_stats
        metrics = create_metrics(
            STATS_DEFINITIONS, registry=prometheus_client.CollectorRegistry()
        )
        update_prometheus_stats(metrics)
        self.assertThat(mock, MockCalledOnce())
        self.assertThat(mock_arches, MockCalledOnce())
        self.assertThat(mock_pods, MockCalledOnce())
        self.assertThat(mock_subnet_stats, MockCalledOnce())

    def test_push_stats_to_prometheus(self):
        factory.make_RegionRackController()
        maas_name = "random.maas"
        push_gateway = "127.0.0.1:2000"
        mock_prom_cli = self.patch(stats, "prom_cli")
        push_stats_to_prometheus(maas_name, push_gateway)
        self.assertThat(
            mock_prom_cli.push_to_gateway,
            MockCalledOnceWith(
                push_gateway, job="stats_for_%s" % maas_name, registry=mock.ANY
            ),
        )

    def test_subnet_stats(self):
        subnet = factory.make_Subnet(cidr="1.2.0.0/16", gateway_ip="1.2.0.254")
        factory.make_IPRange(
            subnet=subnet,
            start_ip="1.2.0.11",
            end_ip="1.2.0.20",
            alloc_type=IPRANGE_TYPE.DYNAMIC,
        )
        factory.make_IPRange(
            subnet=subnet,
            start_ip="1.2.0.51",
            end_ip="1.2.0.70",
            alloc_type=IPRANGE_TYPE.RESERVED,
        )
        factory.make_StaticIPAddress(
            ip="1.2.0.12", alloc_type=IPADDRESS_TYPE.DHCP, subnet=subnet
        )
        for n in (60, 61):
            factory.make_StaticIPAddress(
                ip="1.2.0.{}".format(n),
                alloc_type=IPADDRESS_TYPE.USER_RESERVED,
                subnet=subnet,
            )
        for n in (80, 90, 100):
            factory.make_StaticIPAddress(
                ip="1.2.0.{}".format(n),
                alloc_type=IPADDRESS_TYPE.STICKY,
                subnet=subnet,
            )
        metrics = create_metrics(
            STATS_DEFINITIONS, registry=prometheus_client.CollectorRegistry()
        )
        update_prometheus_stats(metrics)
        output = metrics.generate_latest().decode("ascii")
        self.assertIn(
            "maas_net_subnet_ip_count"
            '{cidr="1.2.0.0/16",status="available"} 65500.0',
            output,
        )
        self.assertIn(
            "maas_net_subnet_ip_count"
            '{cidr="1.2.0.0/16",status="unavailable"} 34.0',
            output,
        )
        self.assertIn(
            "maas_net_subnet_ip_dynamic"
            '{cidr="1.2.0.0/16",status="available"} 9.0',
            output,
        )
        self.assertIn(
            'maas_net_subnet_ip_dynamic{cidr="1.2.0.0/16",status="used"} 1.0',
            output,
        )
        self.assertIn(
            "maas_net_subnet_ip_reserved"
            '{cidr="1.2.0.0/16",status="available"} 18.0',
            output,
        )
        self.assertIn(
            'maas_net_subnet_ip_reserved{cidr="1.2.0.0/16",status="used"} 2.0',
            output,
        )
        self.assertIn(
            'maas_net_subnet_ip_static{cidr="1.2.0.0/16"} 3.0', output
        )


class TestPrometheusService(MAASTestCase):
    """Tests for `ImportPrometheusService`."""

    def test__is_a_TimerService(self):
        service = stats.PrometheusService()
        self.assertIsInstance(service, TimerService)

    def test__runs_once_an_hour_by_default(self):
        service = stats.PrometheusService()
        self.assertEqual(3600, service.step)

    def test__calls__maybe_make_stats_request(self):
        service = stats.PrometheusService()
        self.assertEqual(
            (service.maybe_push_prometheus_stats, (), {}), service.call
        )

    def test_maybe_make_stats_request_does_not_error(self):
        service = stats.PrometheusService()
        deferToDatabase = self.patch(stats, "deferToDatabase")
        exception_type = factory.make_exception_type()
        deferToDatabase.return_value = fail(exception_type())
        d = service.maybe_push_prometheus_stats()
        self.assertIsNone(extract_result(d))


class TestPrometheusServiceAsync(MAASTransactionServerTestCase):
    """Tests for the async parts of `PrometheusService`."""

    def test_maybe_make_stats_request_makes_request(self):
        mock_call = self.patch(stats, "push_stats_to_prometheus")
        self.patch(stats, "PROMETHEUS_SUPPORTED", True)

        with transaction.atomic():
            Config.objects.set_config("prometheus_enabled", True)
            Config.objects.set_config(
                "prometheus_push_gateway", "192.168.1.1:8081"
            )

        service = stats.PrometheusService()
        maybe_push_prometheus_stats = asynchronous(
            service.maybe_push_prometheus_stats
        )
        maybe_push_prometheus_stats().wait(5)

        self.assertThat(mock_call, MockCalledOnce())

    def test_maybe_make_stats_request_doesnt_make_request(self):
        mock_prom_cli = self.patch(stats, "prom_cli")

        with transaction.atomic():
            Config.objects.set_config("enable_analytics", False)

        service = stats.PrometheusService()
        maybe_push_prometheus_stats = asynchronous(
            service.maybe_push_prometheus_stats
        )
        maybe_push_prometheus_stats().wait(5)

        self.assertThat(
            mock_prom_cli.push_stats_to_prometheus, MockNotCalled()
        )
