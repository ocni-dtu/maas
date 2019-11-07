# -*- coding: utf-8 -*-
# Generated by Django 1.11.11 on 2019-04-16 16:24
from __future__ import unicode_literals

from logging import DEBUG

from django.db import migrations
from provisioningserver.events import EVENT_TYPES


CHANGED_EVENTS_INFO_TO_DEBUG = [
    EVENT_TYPES.NODE_POWERED_ON,
    EVENT_TYPES.NODE_POWERED_OFF,
    EVENT_TYPES.NODE_POWER_QUERIED,
    EVENT_TYPES.NODE_PXE_REQUEST,
    EVENT_TYPES.NODE_INSTALLATION_FINISHED,
    EVENT_TYPES.NODE_CHANGED_STATUS,
    EVENT_TYPES.REQUEST_NODE_START_COMMISSIONING,
    EVENT_TYPES.REQUEST_NODE_ABORT_COMMISSIONING,
    EVENT_TYPES.REQUEST_NODE_START_TESTING,
    EVENT_TYPES.REQUEST_NODE_ABORT_TESTING,
    EVENT_TYPES.REQUEST_NODE_OVERRIDE_FAILED_TESTING,
    EVENT_TYPES.REQUEST_NODE_ABORT_DEPLOYMENT,
    EVENT_TYPES.REQUEST_NODE_ACQUIRE,
    EVENT_TYPES.REQUEST_NODE_ERASE_DISK,
    EVENT_TYPES.REQUEST_NODE_ABORT_ERASE_DISK,
    EVENT_TYPES.REQUEST_NODE_RELEASE,
    EVENT_TYPES.REQUEST_NODE_MARK_FAILED,
    EVENT_TYPES.REQUEST_NODE_MARK_BROKEN,
    EVENT_TYPES.REQUEST_NODE_MARK_FIXED,
    EVENT_TYPES.REQUEST_NODE_LOCK,
    EVENT_TYPES.REQUEST_NODE_UNLOCK,
    EVENT_TYPES.REQUEST_NODE_START_DEPLOYMENT,
    EVENT_TYPES.REQUEST_NODE_START,
    EVENT_TYPES.REQUEST_NODE_STOP,
    EVENT_TYPES.REQUEST_NODE_START_RESCUE_MODE,
    EVENT_TYPES.REQUEST_NODE_STOP_RESCUE_MODE,
    EVENT_TYPES.REQUEST_CONTROLLER_REFRESH,
    EVENT_TYPES.REQUEST_RACK_CONTROLLER_ADD_CHASSIS,
    EVENT_TYPES.RACK_IMPORT_INFO,
    EVENT_TYPES.REGION_IMPORT_INFO,
]


def change_event_levels_from_info_to_debug(apps, schema_editor):
    EventType = apps.get_model("maasserver", "EventType")
    for event_type in EventType.objects.filter(
        name__in=CHANGED_EVENTS_INFO_TO_DEBUG
    ):
        event_type.level = DEBUG
        event_type.save()


class Migration(migrations.Migration):

    dependencies = [("maasserver", "0186_node_description")]

    operations = [migrations.RunPython(change_event_levels_from_info_to_debug)]