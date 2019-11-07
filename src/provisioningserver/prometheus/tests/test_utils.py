import os
from pathlib import Path
from subprocess import Popen

from fixtures import EnvironmentVariable
from maastesting.fixtures import TempDirectory
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
import prometheus_client
from provisioningserver.prometheus import utils
from provisioningserver.prometheus.utils import (
    clean_prometheus_dir,
    create_metrics,
    MetricDefinition,
    PrometheusMetrics,
)
from twisted.internet.defer import inlineCallbacks, returnValue


class TestPrometheusMetrics(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_empty(self):
        prometheus_metrics = PrometheusMetrics()
        self.assertEqual(prometheus_metrics.available_metrics, [])
        self.assertIsNone(prometheus_metrics.generate_latest())

    def test_update_empty(self):
        prometheus_metrics = PrometheusMetrics()
        prometheus_metrics.update("some_metric", "inc")
        self.assertIsNone(prometheus_metrics.generate_latest())

    def test_update(self):
        definitions = [
            MetricDefinition("Gauge", "a_gauge", "A Gauge", ["foo", "bar"])
        ]
        prometheus_metrics = PrometheusMetrics(
            definitions=definitions,
            registry=prometheus_client.CollectorRegistry(),
        )
        prometheus_metrics.update(
            "a_gauge", "set", value=22, labels={"foo": "FOO", "bar": "BAR"}
        )
        self.assertIn(
            'a_gauge{bar="BAR",foo="FOO"} 22.0',
            prometheus_metrics.generate_latest().decode("ascii"),
        )

    def test_update_call_value_class(self):
        definitions = [MetricDefinition("Counter", "a_counter", "A Counter")]
        prometheus_metrics = PrometheusMetrics(
            definitions=definitions,
            registry=prometheus_client.CollectorRegistry(),
        )
        prometheus_metrics.update("a_counter", "set", value=22)
        self.assertIn(
            "a_counter 22.0",
            prometheus_metrics.generate_latest().decode("ascii"),
        )

    def test_update_with_extra_labels(self):
        definitions = [
            MetricDefinition("Gauge", "a_gauge", "A Gauge", ["foo", "bar"])
        ]
        prometheus_metrics = PrometheusMetrics(
            definitions=definitions,
            extra_labels={"baz": "BAZ", "bza": "BZA"},
            registry=prometheus_client.CollectorRegistry(),
        )
        prometheus_metrics.update(
            "a_gauge", "set", value=22, labels={"foo": "FOO", "bar": "BAR"}
        )
        self.assertIn(
            'a_gauge{bar="BAR",baz="BAZ",bza="BZA",foo="FOO"} 22.0',
            prometheus_metrics.generate_latest().decode("ascii"),
        )

    def test_with_update_handlers(self):
        def update_gauge(metrics):
            metrics.update("a_gauge", "set", value=33)

        prometheus_metrics = PrometheusMetrics(
            definitions=[MetricDefinition("Gauge", "a_gauge", "A Gauge", [])],
            update_handlers=[update_gauge],
            registry=prometheus_client.CollectorRegistry(),
        )
        self.assertIn(
            "a_gauge 33.0",
            prometheus_metrics.generate_latest().decode("ascii"),
        )

    @inlineCallbacks
    def test_record_call_latency_async(self):
        definitions = [
            MetricDefinition(
                "Histogram", "histo", "An histogram", ["foo", "bar"]
            )
        ]
        prometheus_metrics = PrometheusMetrics(
            definitions=definitions,
            registry=prometheus_client.CollectorRegistry(),
        )
        label_call_args = []

        def get_labels(*args, **kwargs):
            label_call_args.append((args, kwargs))
            return {"foo": "FOO", "bar": "BAR"}

        @prometheus_metrics.record_call_latency("histo", get_labels=get_labels)
        @inlineCallbacks
        def func(param1, param2=None):
            yield
            returnValue(param1)

        obj = object()
        result = yield func(obj, param2="baz")
        self.assertIs(result, obj)
        # the get_labels function is called with the same args as the function
        self.assertEqual(label_call_args, [((obj,), {"param2": "baz"})])
        self.assertIn(
            'histo_count{bar="BAR",foo="FOO"} 1.0',
            prometheus_metrics.generate_latest().decode("ascii"),
        )

    def test_record_call_latency_sync(self):
        definitions = [
            MetricDefinition(
                "Histogram", "histo", "An histogram", ["foo", "bar"]
            )
        ]
        prometheus_metrics = PrometheusMetrics(
            definitions=definitions,
            registry=prometheus_client.CollectorRegistry(),
        )
        label_call_args = []

        def get_labels(*args, **kwargs):
            label_call_args.append((args, kwargs))
            return {"foo": "FOO", "bar": "BAR"}

        @prometheus_metrics.record_call_latency("histo", get_labels=get_labels)
        def func(param1, param2=None):
            return param1

        obj = object()
        result = func(obj, param2="baz")
        self.assertIs(result, obj)
        # the get_labels function is called with the same args as the function
        self.assertEqual(label_call_args, [((obj,), {"param2": "baz"})])
        self.assertIn(
            'histo_count{bar="BAR",foo="FOO"} 1.0',
            prometheus_metrics.generate_latest().decode("ascii"),
        )


class TestCreateMetrics(MAASTestCase):
    def setUp(self):
        super().setUp()
        self.metrics_definitions = [
            MetricDefinition(
                "Histogram", "sample_histogram", "Sample histogram", []
            ),
            MetricDefinition(
                "Counter", "sample_counter", "Sample counter", []
            ),
        ]

    def test_metrics(self):
        prometheus_metrics = create_metrics(
            self.metrics_definitions,
            registry=prometheus_client.CollectorRegistry(),
        )
        self.assertIsInstance(prometheus_metrics, PrometheusMetrics)
        self.assertCountEqual(
            prometheus_metrics.available_metrics,
            ["sample_counter", "sample_histogram"],
        )

    def test_metrics_prometheus_not_availble(self):
        self.patch(utils, "PROMETHEUS_SUPPORTED", False)
        prometheus_metrics = create_metrics(
            self.metrics_definitions,
            registry=prometheus_client.CollectorRegistry(),
        )
        self.assertEqual(prometheus_metrics.available_metrics, [])

    def test_extra_labels(self):
        prometheus_metrics = create_metrics(
            self.metrics_definitions,
            extra_labels={"foo": "FOO", "bar": "BAR"},
            registry=prometheus_client.CollectorRegistry(),
        )
        prometheus_metrics.update("sample_counter", "inc")
        content = prometheus_metrics.generate_latest().decode("ascii")
        self.assertIn('sample_counter{bar="BAR",foo="FOO"} 1.0', content)

    def test_extra_labels_callable(self):
        values = ["a", "b"]
        prometheus_metrics = create_metrics(
            self.metrics_definitions,
            extra_labels={"foo": values.pop},
            registry=prometheus_client.CollectorRegistry(),
        )
        prometheus_metrics.update("sample_counter", "inc")
        prometheus_metrics.update("sample_counter", "inc")
        content = prometheus_metrics.generate_latest().decode("ascii")
        self.assertIn('sample_counter{foo="a"} 1.0', content)
        self.assertIn('sample_counter{foo="b"} 1.0', content)


class TestCleanPrometheusDir(MAASTestCase):
    def get_unused_pid(self):
        """Return a PID for a process that has just finished running."""
        proc = Popen(["/bin/true"])
        proc.wait()
        return proc.pid

    def test_dir_not_existent(self):
        self.assertIsNone(clean_prometheus_dir("/not/here"))

    def test_env_not_specified(self):
        self.useFixture(EnvironmentVariable("prometheus_multiproc_dir", None))
        self.assertIsNone(clean_prometheus_dir())

    def test_env_dir_not_existent(self):
        self.useFixture(
            EnvironmentVariable("prometheus_multiproc_dir", "/not/here")
        )
        self.assertIsNone(clean_prometheus_dir())

    def test_delete_for_nonexistent_processes(self):
        tmpdir = Path(self.useFixture(TempDirectory()).path)
        pid = os.getpid()
        file1 = tmpdir / "histogram_1.db"
        file1.touch()
        file2 = tmpdir / "histogram_{}.db".format(pid)
        file2.touch()
        file3 = tmpdir / "histogram_{}.db".format(self.get_unused_pid())
        file3.touch()
        file4 = tmpdir / "histogram_{}.db".format(self.get_unused_pid())
        file4.touch()
        clean_prometheus_dir(str(tmpdir))
        self.assertTrue(file1.exists())
        self.assertTrue(file2.exists())
        self.assertFalse(file3.exists())
        self.assertFalse(file4.exists())

    def test_delete_file_disappeared(self):
        real_os_remove = os.remove

        def mock_os_remove(path):
            # remove it twice, so that FileNotFoundError is raised
            real_os_remove(path)
            real_os_remove(path)

        self.patch(os, "remove", mock_os_remove)
        tmpdir = Path(self.useFixture(TempDirectory()).path)
        file1 = tmpdir / "histogram_{}.db".format(self.get_unused_pid())
        file1.touch()
        self.assertIsNone(clean_prometheus_dir(str(tmpdir)))
