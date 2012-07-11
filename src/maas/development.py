# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Django DEVELOPMENT settings for maas project."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

__metaclass__ = type

import logging
import os
from os.path import abspath

from maas import (
    import_local_settings,
    import_settings,
    settings,
    )
from metadataserver.address import guess_server_address

# We expect the following settings to be overridden. They are mentioned here
# to silence lint warnings.
INSTALLED_APPS = None

# Extend base settings.
import_settings(settings)

# In development, django can be accessed directly on port 5240.
DEFAULT_MAAS_URL = "http://%s:5240/" % guess_server_address()

# Use our custom test runner, which makes sure that a local database
# cluster is running in the branch.
TEST_RUNNER = 'maastesting.runner.TestRunner'

# Use a fake provisioning server for test/demo purposes.
USE_REAL_PSERV = False

# Invalid strings should be visible.
TEMPLATE_STRING_IF_INVALID = '#### INVALID STRING ####'

DEBUG = True
TEMPLATE_DEBUG = DEBUG
YUI_DEBUG = DEBUG
STATIC_LOCAL_SERVE = True

RABBITMQ_PUBLISH = False

# Silent South during tests.
logging.getLogger('south').setLevel(logging.WARNING)

DATABASES = {
    'default': {
        # 'postgresql_psycopg2', 'postgresql', 'mysql', 'sqlite3' etc.
        'ENGINE': 'django.db.backends.postgresql_psycopg2',
        'NAME': 'maas',
        # For PostgreSQL, a "hostname" starting with a slash indicates a
        # Unix socket directory.
        'HOST': abspath('db'),
    }
}

# Absolute filesystem path to the directory that will hold user-uploaded files.
# Example: "/home/media/media.lawrence.com/media/"
MEDIA_ROOT = abspath("media/development")

INSTALLED_APPS += (
    'django.contrib.admin',
    'maastesting',
    'debug_toolbar',
    'django_nose',
)

INTERNAL_IPS = ('127.0.0.1',)

DEBUG_TOOLBAR_CONFIG = {
    'INTERCEPT_REDIRECTS': False,
    }

# Make all nodes' metadata visible.  This is not safe; do not enable it
# on a production MAAS.
ALLOW_UNSAFE_METADATA_ACCESS = True

# Use in-branch preseed templates.
PRESEED_TEMPLATE_LOCATIONS = (
    abspath("etc/preseeds"),
    abspath("contrib/preseeds_v2"),
    )

# The root directory of the MAAS project for this dev instance.
DEV_ROOT_DIRECTORY = os.path.join(
    os.path.dirname(__file__), os.pardir, os.pardir)


COMMISSIONING_SCRIPT = os.path.join(
    DEV_ROOT_DIRECTORY, 'etc/maas/commissioning-user-data')


# Set up celery to use the demo settings.
os.environ['CELERY_CONFIG_MODULE'] = 'democeleryconfig'


# Allow the user to override settings in maas_local_settings.
import_local_settings()
