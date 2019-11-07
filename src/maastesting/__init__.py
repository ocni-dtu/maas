# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Testing infrastructure for MAAS."""

__all__ = ["bindir", "root"]

import copy
from os.path import abspath, dirname, join, pardir, realpath
import re
from unittest import mock
from warnings import filterwarnings

# The root of the source tree.
root = abspath(join(dirname(realpath(__file__)), pardir, pardir))

# The bin/ directory in the source tree.
bindir = join(root, "bin")

# Construct a regular expression that matches all of MAAS's core
# packages, and their subpackages.
packages = {
    "apiclient",
    "maas",
    "maascli",
    "maasserver",
    "maastesting",
    "metadataserver",
    "provisioningserver",
}
packages_expr = r"^(?:%s)\b" % "|".join(
    re.escape(package) for package in packages
)

# Enable some warnings that we ought to pay heed to.
filterwarnings("error", category=BytesWarning, module=packages_expr)
filterwarnings("default", category=DeprecationWarning, module=packages_expr)
filterwarnings("default", category=ImportWarning, module=packages_expr)

# Ignore noisy deprecation warnings inside Twisted.
filterwarnings("ignore", category=DeprecationWarning, module=r"^twisted\b")

# Make sure that sentinel objects are not copied.
sentinel_type = type(mock.sentinel.foo)
copy._copy_dispatch[sentinel_type] = copy._copy_immutable
copy._deepcopy_dispatch[sentinel_type] = copy._copy_immutable

# Monkey-patch test dependencies here.
from maastesting.twisted import maybe_fix_bug_230_in_CaptureTwistedLogs

maybe_fix_bug_230_in_CaptureTwistedLogs()
del maybe_fix_bug_230_in_CaptureTwistedLogs
