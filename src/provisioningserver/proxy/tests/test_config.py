# Copyright 2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `provisioningserver.proxy.config`."""

__all__ = []

import os
from pathlib import Path
import random

from crochet import wait_for
from fixtures import EnvironmentVariableFixture
from maastesting.factory import factory
from maastesting.testcase import MAASTestCase
from provisioningserver.proxy import config
from provisioningserver.utils import snappy
from testtools.matchers import Contains, FileContains


wait_for_reactor = wait_for(30)  # 30 seconds.


class TestGetConfigDir(MAASTestCase):
    """Tests for `get_proxy_config_path`."""

    def test_returns_default(self):
        self.assertEquals(
            "/var/lib/maas/maas-proxy.conf", config.get_proxy_config_path()
        )

    def test_env_overrides_default(self):
        os.environ["MAAS_PROXY_CONFIG_DIR"] = factory.make_name("env")
        self.assertEquals(
            os.sep.join(
                [
                    os.environ["MAAS_PROXY_CONFIG_DIR"],
                    config.MAAS_PROXY_CONF_NAME,
                ]
            ),
            config.get_proxy_config_path(),
        )
        del os.environ["MAAS_PROXY_CONFIG_DIR"]


class TestWriteConfig(MAASTestCase):
    """Tests for `write_config`."""

    def setUp(self):
        super(TestWriteConfig, self).setUp()
        self.tmpdir = self.make_dir()
        self.proxy_path = Path(self.tmpdir) / config.MAAS_PROXY_CONF_NAME
        self.useFixture(
            EnvironmentVariableFixture("MAAS_PROXY_CONFIG_DIR", self.tmpdir)
        )

    def test__adds_cidr(self):
        cidr = factory.make_ipv4_network()
        config.write_config([cidr])
        matcher = Contains("acl localnet src %s" % cidr)
        self.assertThat(
            "%s/%s" % (self.tmpdir, config.MAAS_PROXY_CONF_NAME),
            FileContains(matcher=matcher),
        )

    def test__peer_proxies(self):
        cidr = factory.make_ipv4_network()
        peer_proxies = ["http://example.com:8000/", "http://other.com:8001/"]
        config.write_config([cidr], peer_proxies=peer_proxies)
        cache_peer1_line = (
            "cache_peer example.com parent 8000 0 no-query default"
        )
        cache_peer2_line = (
            "cache_peer other.com parent 8001 0 no-query default"
        )
        with self.proxy_path.open() as proxy_file:
            lines = [line.strip() for line in proxy_file.readlines()]
            self.assertIn("never_direct allow all", lines)
            self.assertIn(cache_peer1_line, lines)
            self.assertIn(cache_peer2_line, lines)

    def test__without_use_peer_proxy(self):
        cidr = factory.make_ipv4_network()
        config.write_config([cidr])
        with self.proxy_path.open() as proxy_file:
            lines = [line.strip() for line in proxy_file.readlines()]
            self.assertNotIn("never_direct allow all", lines)
            self.assertNotIn("cache_peer", lines)

    def test__with_prefer_v4_proxy_False(self):
        cidr = factory.make_ipv4_network()
        config.write_config([cidr], prefer_v4_proxy=False)
        with self.proxy_path.open() as proxy_file:
            lines = [line.strip() for line in proxy_file.readlines()]
            self.assertNotIn("dns_v4_first on", lines)

    def test__with_prefer_v4_proxy_True(self):
        cidr = factory.make_ipv4_network()
        config.write_config([cidr], prefer_v4_proxy=True)
        with self.proxy_path.open() as proxy_file:
            lines = [line.strip() for line in proxy_file.readlines()]
            self.assertIn("dns_v4_first on", lines)

    def test__port_changes_port(self):
        cidr = factory.make_ipv4_network()
        port = random.randint(1, 65535)
        config.write_config([cidr], maas_proxy_port=port)
        with self.proxy_path.open() as proxy_file:
            lines = [line.strip() for line in proxy_file.readlines()]
            self.assertIn("http_port %s" % port, lines)

    def test__user_in_snap(self):
        self.patch(snappy, "running_in_snap").return_value = True
        config.write_config(allowed_cidrs=[])
        with self.proxy_path.open() as proxy_file:
            lines = [line.strip() for line in proxy_file.readlines()]
            self.assertIn("cache_effective_user snap_daemon", lines)
            self.assertIn("cache_effective_group snap_daemon", lines)

    def test__user_not_in_snap(self):
        self.patch(snappy, "running_in_snap").return_value = False
        config.write_config(allowed_cidrs=[])
        with self.proxy_path.open() as proxy_file:
            lines = [line.strip() for line in proxy_file.readlines()]
            self.assertNotIn("cache_effective_user snap_daemon", lines)
            self.assertNotIn("cache_effective_group snap_daemon", lines)
