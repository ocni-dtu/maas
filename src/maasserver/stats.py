# Copyright 2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Boot Resources."""

__all__ = [
    "get_subnets_utilisation_stats",
    "StatsService",
    "STATS_SERVICE_PERIOD",
]

from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Sum, Value
from django.db.models.functions import Coalesce
from maasserver.enum import IPRANGE_TYPE
from maasserver.models import Config
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
from provisioningserver.logger import LegacyLogger
from provisioningserver.utils.network import IPRangeStatistics
from twisted.application.internet import TimerService


log = LegacyLogger()

import base64
from collections import Counter
import json

from maasserver.enum import IPADDRESS_TYPE, NODE_TYPE, NODE_STATUS, BMC_TYPE
from maasserver.models import (
    Machine,
    Node,
    Fabric,
    VLAN,
    Space,
    StaticIPAddress,
    Subnet,
    BMC,
    Pod,
)
from maasserver.utils import get_maas_user_agent
import requests


def NotNullSum(column):
    """Like Sum, but returns 0 if the aggregate is None."""
    return Coalesce(Sum(column), Value(0))


def get_machine_stats():
    # Rather overall amount of stats for machines.
    return Machine.objects.aggregate(
        total_cpu=NotNullSum("cpu_count"),
        total_mem=NotNullSum("memory"),
        total_storage=NotNullSum("blockdevice__size"),
    )


def get_machine_state_stats():
    node_status = Node.objects.filter(node_type=NODE_TYPE.MACHINE)
    node_status = Counter(node_status.values_list("status", flat=True))

    return {
        # base status
        "new": node_status.get(NODE_STATUS.NEW, 0),
        "ready": node_status.get(NODE_STATUS.READY, 0),
        "allocated": node_status.get(NODE_STATUS.ALLOCATED, 0),
        "deployed": node_status.get(NODE_STATUS.DEPLOYED, 0),
        # in progress status
        "commissioning": node_status.get(NODE_STATUS.COMMISSIONING, 0),
        "testing": node_status.get(NODE_STATUS.TESTING, 0),
        "deploying": node_status.get(NODE_STATUS.DEPLOYING, 0),
        # failure status
        "failed_deployment": node_status.get(NODE_STATUS.FAILED_DEPLOYMENT, 0),
        "failed_commissioning": node_status.get(
            NODE_STATUS.FAILED_COMMISSIONING, 0
        ),
        "failed_testing": node_status.get(NODE_STATUS.FAILED_TESTING, 0),
        "broken": node_status.get(NODE_STATUS.BROKEN, 0),
    }


def get_machines_by_architecture():
    node_arches = Machine.objects.extra(
        dict(short_arch="SUBSTRING(architecture FROM '(.*)/')")
    ).values_list("short_arch", flat=True)
    return Counter(node_arches)


def get_kvm_pods_stats():
    pods = BMC.objects.filter(bmc_type=BMC_TYPE.POD, power_type="virsh")
    # Calculate available physical resources
    # total_mem is in MB
    # local_storage is in bytes
    available_resources = pods.aggregate(
        cores=NotNullSum("cores"),
        memory=NotNullSum("memory"),
        storage=NotNullSum("local_storage"),
    )

    # available resources with overcommit
    over_cores = over_memory = 0
    for pod in pods:
        over_cores += pod.cores * pod.cpu_over_commit_ratio
        over_memory += pod.memory * pod.memory_over_commit_ratio
    available_resources["over_cores"] = over_cores
    available_resources["over_memory"] = over_memory

    # Calculate utilization
    pod_machines = Pod.objects.all()
    machines = cores = memory = storage = 0
    for pod in pod_machines:
        machines += Node.objects.filter(bmc__id=pod.id).count()
        cores += pod.get_used_cores()
        memory += pod.get_used_memory()
        storage += pod.get_used_local_storage()

    return {
        "kvm_pods": len(pods),
        "kvm_machines": machines,
        "kvm_available_resources": available_resources,
        "kvm_utilized_resources": {
            "cores": cores,
            "memory": memory,
            "storage": storage,
        },
    }


def get_subnets_stats():
    subnets = Subnet.objects.all()
    v4 = [net for net in subnets if net.get_ip_version() == 4]
    v6 = [net for net in subnets if net.get_ip_version() == 6]
    return {
        "spaces": Space.objects.count(),
        "fabrics": Fabric.objects.count(),
        "vlans": VLAN.objects.count(),
        "subnets_v4": len(v4),
        "subnets_v6": len(v6),
    }


def get_subnets_utilisation_stats():
    """Return a dict mapping subnet CIDRs to their utilisation details."""
    ips_count = _get_subnets_ipaddress_count()

    stats = {}
    for subnet in Subnet.objects.all():
        full_range = subnet.get_iprange_usage()
        range_stats = IPRangeStatistics(subnet.get_iprange_usage())
        static = 0
        reserved_available = 0
        reserved_used = 0
        dynamic_available = 0
        dynamic_used = 0
        for rng in full_range.ranges:
            if IPRANGE_TYPE.DYNAMIC in rng.purpose:
                dynamic_available += rng.num_addresses
            elif IPRANGE_TYPE.RESERVED in rng.purpose:
                reserved_available += rng.num_addresses
            elif "assigned-ip" in rng.purpose:
                static += rng.num_addresses
        # allocated IPs
        subnet_ips = ips_count[subnet.id]
        reserved_used += subnet_ips[IPADDRESS_TYPE.USER_RESERVED]
        reserved_available -= reserved_used
        dynamic_used += (
            subnet_ips[IPADDRESS_TYPE.AUTO]
            + subnet_ips[IPADDRESS_TYPE.DHCP]
            + subnet_ips[IPADDRESS_TYPE.DISCOVERED]
        )
        dynamic_available -= dynamic_used
        stats[subnet.cidr] = {
            "available": range_stats.num_available,
            "unavailable": range_stats.num_unavailable,
            "dynamic_available": dynamic_available,
            "dynamic_used": dynamic_used,
            "static": static,
            "reserved_available": reserved_available,
            "reserved_used": reserved_used,
        }
    return stats


def _get_subnets_ipaddress_count():
    counts = defaultdict(lambda: defaultdict(int))
    rows = (
        StaticIPAddress.objects.values("subnet_id", "alloc_type")
        .filter(ip__isnull=False)
        .annotate(count=Count("ip"))
    )
    for row in rows:
        counts[row["subnet_id"]][row["alloc_type"]] = row["count"]
    return counts


def get_maas_stats():
    # TODO
    # - architectures
    # - resource pools
    # - pods
    # Get all node types to get count values
    node_types = Node.objects.values_list("node_type", flat=True)
    node_types = Counter(node_types)
    # get summary of machine resources, and its statuses.
    stats = get_machine_stats()
    machine_status = get_machine_state_stats()
    # get summary of network objects
    netstats = get_subnets_stats()

    return json.dumps(
        {
            "controllers": {
                "regionracks": node_types.get(
                    NODE_TYPE.REGION_AND_RACK_CONTROLLER, 0
                ),
                "regions": node_types.get(NODE_TYPE.REGION_CONTROLLER, 0),
                "racks": node_types.get(NODE_TYPE.RACK_CONTROLLER, 0),
            },
            "nodes": {
                "machines": node_types.get(NODE_TYPE.MACHINE, 0),
                "devices": node_types.get(NODE_TYPE.DEVICE, 0),
            },
            "machine_stats": stats,  # count of cpus, mem, storage
            "machine_status": machine_status,  # machines by status
            "network_stats": netstats,  # network status
        }
    )


def get_request_params():
    return {
        "data": base64.b64encode(
            json.dumps(get_maas_stats()).encode()
        ).decode()
    }


def make_maas_user_agent_request():
    headers = {"User-Agent": get_maas_user_agent()}
    params = get_request_params()
    try:
        requests.get(
            "https://stats.images.maas.io/", params=params, headers=headers
        )
    except Exception:
        # Do not fail if for any reason requests does.
        pass


# How often the import service runs.
STATS_SERVICE_PERIOD = timedelta(hours=24)


class StatsService(TimerService, object):
    """Service to periodically get stats.

    This will run immediately when it's started, then once again every
    24 hours, though the interval can be overridden by passing it to
    the constructor.
    """

    def __init__(self, interval=STATS_SERVICE_PERIOD):
        super(StatsService, self).__init__(
            interval.total_seconds(), self.maybe_make_stats_request
        )

    def maybe_make_stats_request(self):
        def determine_stats_request():
            if Config.objects.get_config("enable_analytics"):
                make_maas_user_agent_request()

        d = deferToDatabase(transactional(determine_stats_request))
        d.addErrback(log.err, "Failure performing user agent request.")
        return d
