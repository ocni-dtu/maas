# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Utilities for the provisioning server."""

__all__ = [
    "CircularDependency",
    "flatten",
    "locate_config",
    "locate_template",
    "parse_key_value_file",
    "ShellTemplate",
    "sorttop",
    "sudo",
    "typed",
]

from collections import Iterable
from functools import lru_cache, reduce
from itertools import chain
import os
from pipes import quote
from typing import Tuple

from provisioningserver.utils import snappy
import tempita

# Use typecheck-decorator if it's available.
try:
    from maastesting.typecheck import typed
except ImportError:
    typed = lambda func: func


@typed
def locate_config(*path: Tuple[str]):
    """Return the location of a given config file or directory.

    :param path: Path elements to resolve relative to `${MAAS_ROOT}/etc/maas`.
    """
    # The `os.curdir` avoids a crash when `path` is empty.
    path = os.path.join(os.curdir, *path)
    if os.path.isabs(path):
        return path
    else:
        # Avoid circular imports.
        from provisioningserver.path import get_tentative_data_path

        return get_tentative_data_path("etc", "maas", path)


def locate_template(*path: Tuple[str]):
    """Return the absolute path of a template.

    :param path: Path elemets to resolve relative to the location the
                 Python library provisioning server is located in.
    """
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "templates", *path)
    )


@lru_cache(256)
def load_template(*path: Tuple[str]):
    """Load the template."""
    return tempita.Template.from_filename(
        locate_template(*path), encoding="UTF-8"
    )


def dict_depth(d, depth=0):
    """Returns the max depth of a dictionary."""
    if not isinstance(d, dict) or not d:
        return depth
    return max(dict_depth(v, depth + 1) for _, v in d.items())


def split_lines(input, separator):
    """Split each item from `input` into a key/value pair."""
    return (line.split(separator, 1) for line in input if line.strip() != "")


def strip_pairs(input):
    """Strip whitespace of each key/value pair in input."""
    return ((key.strip(), value.strip()) for (key, value) in input)


def parse_key_value_file(file_name, separator=":"):
    """Parse a text file into a dict of key/value pairs.

    Use this for simple key:value or key=value files. There are no sections,
    as required for python's ConfigParse. Whitespace and empty lines are
    ignored, and it is assumed that the file is encoded as UTF-8.

    :param file_name: Name of file to parse.
    :param separator: The text that separates each key from its value.
    """
    with open(file_name, "r", encoding="utf-8") as input:
        return dict(strip_pairs(split_lines(input, separator)))


class Safe:
    """An object that is safe to render as-is."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return "<%s %r>" % (self.__class__.__name__, self.value)


class ShellTemplate(tempita.Template):
    """A Tempita template specialised for writing shell scripts.

    By default, substitutions will be escaped using `pipes.quote`, unless
    they're marked as safe. This can be done using Tempita's filter syntax::

      {{foobar|safe}}

    or as a plain Python expression::

      {{safe(foobar)}}

    """

    default_namespace = dict(tempita.Template.default_namespace, safe=Safe)

    def _repr(self, value, pos):
        """Shell-quote the value by default."""
        rep = super(ShellTemplate, self)._repr
        if isinstance(value, Safe):
            return rep(value.value, pos)
        else:
            return quote(rep(value, pos))


def classify(func, subjects):
    """Classify `subjects` according to `func`.

    Splits `subjects` into two lists: one for those which `func`
    returns a truth-like value, and one for the others.

    :param subjects: An iterable of `(ident, subject)` tuples, where
        `subject` is an argument that can be passed to `func` for
        classification.
    :param func: A function that takes a single argument.

    :return: A ``(matched, other)`` tuple, where ``matched`` and
        ``other`` are `list`s of `ident` values; `subject` values are
        not returned.
    """
    matched, other = [], []
    for ident, subject in subjects:
        bucket = matched if func(subject) else other
        bucket.append(ident)
    return matched, other


def flatten(*things):
    """Recursively flatten iterable parts of `things`.

    For example::

      >>> sorted(flatten([1, 2, {3, 4, (5, 6)}]))
      [1, 2, 3, 4, 5, 6]

    :return: An iterator.
    """

    def _flatten(things):
        if isinstance(things, str):
            # String-like objects are treated as leaves; iterating through a
            # string yields more strings, each of which is also iterable, and
            # so on, until the heat-death of the universe.
            return iter((things,))
        elif isinstance(things, Iterable):
            # Recurse and merge in order to flatten nested structures.
            return chain.from_iterable(map(_flatten, things))
        else:
            # This is a leaf; return an single-item iterator so that it can be
            # chained with any others.
            return iter((things,))

    return _flatten(things)


def is_true(value):
    if value is None:
        return False
    return value.lower() in ("yes", "true", "t", "1")


def sudo(command_args):
    """Wrap the command arguments in a sudo command, if not in debug mode."""
    if snappy.running_in_snap():
        return command_args
    else:
        return ["sudo", "-n", *command_args]


class CircularDependency(ValueError):
    """A circular dependency has been found."""


def sorttop(data):
    """Sort `data` topologically.

    `data` should be a `dict`, where each entry maps a "thing" to a `set` of
    other things they depend on, or should be sorted after. For example:

      >>> list(sorttop({1: {2}, 2: {3, 4}}))
      [{3, 4}, {2}, {1}]

    :raises CircularDependency: If two or more things depend on one another,
        making it impossible to resolve their relative ordering.
    """
    empty = frozenset()
    # Copy data and discard self-referential dependencies.
    data = {thing: set(deps) for thing, deps in data.items()}
    for thing, deps in data.items():
        deps.discard(thing)
    # Find ghost dependencies and add them as "things".
    ghosts = reduce(set.union, data.values(), set()).difference(data)
    for ghost in ghosts:
        data[ghost] = empty
    # Skim batches off the top until we're done.
    while len(data) != 0:
        batch = {thing for thing, deps in data.items() if deps == empty}
        if len(batch) == 0:
            raise CircularDependency(data)
        else:
            for thing in batch:
                del data[thing]
            for deps in data.values():
                deps.difference_update(batch)
            yield batch


def is_instance_or_subclass(test, *query):
    """Checks if a `test` object is an instance or type matching `query`.

    The `query` parameter will be flattened into a tuple before being used.
    """
    # isinstance() requires a tuple.
    query_tuple = tuple(flatten(query))
    if isinstance(test, query_tuple):
        return True
    try:
        return issubclass(test, query_tuple)
    except TypeError:
        return False


# Capacity units supported by convert_size_to_bytes() function.
CAPACITY_UNITS = {
    "KiB": 2 ** 10,
    "MiB": 2 ** 20,
    "GiB": 2 ** 30,
    "TiB": 2 ** 40,
    "PiB": 2 ** 50,
    "EiB": 2 ** 60,
    "ZiB": 2 ** 70,
    "YiB": 2 ** 80,
}


class UnknownCapacityUnitError(Exception):
    """Unknown capacity unit used."""


def convert_size_to_bytes(value):
    """
    Converts storage size values with units (GiB, TiB...) to bytes.

    :param value: A string containing a number and unit separated by at least
        one space character.  If unit is not specified, defaults to bytes.
    :return: An integer indicating the number of bytes for the given value in
        any other size unit.
    :raises UnknownCapacityUnitError: unsupported capacity unit.
    """
    # Split value on the first space.
    capacity_def = value.split(" ", 1)
    if len(capacity_def) == 1:
        # No unit specified, default to bytes.
        return int(capacity_def[0])

    capacity_value, capacity_unit = capacity_def
    capacity_value = float(capacity_value)
    capacity_unit = capacity_unit.strip()
    if capacity_unit in CAPACITY_UNITS:
        multiplier = CAPACITY_UNITS[capacity_unit]
    else:
        raise UnknownCapacityUnitError(
            "Unknown capacity unit '%s'" % capacity_unit
        )
    # Convert value to bytes.
    return int(capacity_value * multiplier)
