# Copyright 2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Proxy config management module."""

__all__ = ["get_proxy_config_path", "is_config_present", "write_config"]

import datetime
import os
import socket
import sys
from urllib.parse import urlparse

from provisioningserver.logger import get_maas_logger
from provisioningserver.utils import locate_template, snappy
from provisioningserver.utils.fs import atomic_write
from provisioningserver.utils.twisted import synchronous
import tempita


maaslog = get_maas_logger("proxy")
MAAS_PROXY_CONF_NAME = "maas-proxy.conf"
MAAS_PROXY_CONF_TEMPLATE = "maas-proxy.conf.template"


class ProxyConfigFail(Exception):
    """Raised if there is a problem with the proxy configuration."""


def get_proxy_config_path():
    """Location of bind configuration files."""
    setting = os.getenv("MAAS_PROXY_CONFIG_DIR", "/var/lib/maas")
    if isinstance(setting, bytes):
        fsenc = sys.getfilesystemencoding()
        setting = setting.decode(fsenc)
    setting = os.sep.join([setting, MAAS_PROXY_CONF_NAME])
    return setting


def is_config_present():
    """Check if there is a configuration file for the proxy."""
    return os.access(get_proxy_config_path(), os.R_OK)


@synchronous
def write_config(
    allowed_cidrs,
    peer_proxies=None,
    prefer_v4_proxy=False,
    maas_proxy_port=8000,
):
    """Write the proxy configuration."""
    if peer_proxies is None:
        peer_proxies = []

    context = {
        "modified": str(datetime.date.today()),
        "fqdn": socket.getfqdn(),
        "cidrs": allowed_cidrs,
        "running_in_snap": snappy.running_in_snap(),
        "snap_path": snappy.get_snap_path(),
        "snap_data_path": snappy.get_snap_data_path(),
        "snap_common_path": snappy.get_snap_common_path(),
        "dns_v4_first": prefer_v4_proxy,
        "maas_proxy_port": maas_proxy_port,
    }

    formatted_peers = []
    for peer in peer_proxies:
        formatted_peers.append(
            {"address": urlparse(peer).hostname, "port": urlparse(peer).port}
        )
    context["peers"] = formatted_peers

    template_path = locate_template("proxy", MAAS_PROXY_CONF_TEMPLATE)
    template = tempita.Template.from_filename(template_path, encoding="UTF-8")
    try:
        content = template.substitute(context)
    except NameError as error:
        raise ProxyConfigFail(*error.args)

    # Squid prefers ascii.
    content = content.encode("ascii")
    target_path = get_proxy_config_path()
    atomic_write(content, target_path, overwrite=True, mode=0o644)
