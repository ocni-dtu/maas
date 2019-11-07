# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Clean up old snapshot directories."""

__all__ = ["cleanup_snapshots_and_cache"]

import os
import shutil


def list_old_snapshots(storage):
    """List of snapshot directories that are no longer in use."""
    current_dir = os.path.join(storage, "current")
    if os.path.exists(current_dir):
        current_snapshot = os.path.basename(os.readlink(current_dir))
    else:
        current_snapshot = None
    return [
        os.path.join(storage, directory)
        for directory in os.listdir(storage)
        if directory.startswith("snapshot-") and directory != current_snapshot
    ]


def cleanup_snapshots(storage):
    """Remove old snapshot directories."""
    old_snapshots = list_old_snapshots(storage)
    for snapshot in old_snapshots:
        shutil.rmtree(snapshot)


def list_unused_cache_files(storage):
    """List of cache files that are no longer being referenced by snapshots."""
    cache_dir = os.path.join(storage, "cache")
    if os.path.exists(cache_dir):
        cache_files = [
            os.path.join(cache_dir, filename)
            for filename in os.listdir(cache_dir)
            if os.path.isfile(os.path.join(cache_dir, filename))
        ]
    else:
        cache_files = []
    return [
        cache_file
        for cache_file in cache_files
        if os.stat(cache_file).st_nlink == 1
    ]


def cleanup_cache(storage):
    """Remove files that are no longer being referenced by snapshots."""
    cache_files = list_unused_cache_files(storage)
    for cache_file in cache_files:
        os.remove(cache_file)


def cleanup_snapshots_and_cache(storage):
    """Remove old snapshot directories and old cache files."""
    cleanup_snapshots(storage)
    cleanup_cache(storage)
