# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""
System Triggers

Each trigger will call a procedure to send the notification. Each procedure
will raise a notify message in Postgres that a regiond process is listening
for.
"""

__all__ = ["register_system_triggers"]

from textwrap import dedent

from maasserver.models.dnspublication import zone_serial
from maasserver.triggers import register_procedure, register_trigger
from maasserver.utils.orm import transactional

# Note that the corresponding test module (test_system) only tests that the
# triggers and procedures are registered.  The behavior of these procedures
# is tested (end-to-end testing) in test_system_listener.  We test it there
# because the asynchronous nature of the PG events makes it easier to seperate
# the tests.

# Helper that returns the number of rack controllers that region process is
# currently managing.
CORE_GET_MANAGING_COUNT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_core_get_managing_count(
      process maasserver_regioncontrollerprocess)
    RETURNS integer as $$
    BEGIN
      RETURN (SELECT count(*)
        FROM maasserver_node
        WHERE maasserver_node.managing_process_id = process.id);
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Helper that returns the total number of RPC connections for the
# rack controller.
CORE_GET_NUMBER_OF_CONN = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_core_get_num_conn(rack maasserver_node)
    RETURNS integer as $$
    BEGIN
      RETURN (
        SELECT count(*)
        FROM
          maasserver_regionrackrpcconnection AS connection
        WHERE connection.rack_controller_id = rack.id);
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Helper that returns the total number of region processes.
CORE_GET_NUMBER_OF_PROCESSES = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_core_get_num_processes()
    RETURNS integer as $$
    BEGIN
      RETURN (
        SELECT count(*) FROM maasserver_regioncontrollerprocess);
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Helper that picks a new region process that can manage the given
# rack controller.
CORE_PICK_NEW_REGION = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_core_pick_new_region(rack maasserver_node)
    RETURNS maasserver_regioncontrollerprocess as $$
    DECLARE
      selected_managing integer;
      number_managing integer;
      selected_process maasserver_regioncontrollerprocess;
      process maasserver_regioncontrollerprocess;
    BEGIN
      -- Get best region controller that can manage this rack controller.
      -- This is identified by picking a region controller process that
      -- at least has a connection to the rack controller and managing the
      -- least number of rack controllers.
      FOR process IN (
        SELECT DISTINCT ON (maasserver_regioncontrollerprocess.id)
          maasserver_regioncontrollerprocess.*
        FROM
          maasserver_regioncontrollerprocess,
          maasserver_regioncontrollerprocessendpoint,
          maasserver_regionrackrpcconnection
        WHERE maasserver_regionrackrpcconnection.rack_controller_id = rack.id
          AND maasserver_regionrackrpcconnection.endpoint_id =
            maasserver_regioncontrollerprocessendpoint.id
          AND maasserver_regioncontrollerprocessendpoint.process_id =
            maasserver_regioncontrollerprocess.id)
      LOOP
        IF selected_process IS NULL THEN
          -- First time through the loop so set the default.
          selected_process = process;
          selected_managing = sys_core_get_managing_count(process);
        ELSE
          -- See if the current process is managing less then the currently
          -- selected process.
          number_managing = sys_core_get_managing_count(process);
          IF number_managing = 0 THEN
            -- This process is managing zero so its the best, so we exit the
            -- loop now to return the selected.
            selected_process = process;
            EXIT;
          ELSIF number_managing < selected_managing THEN
            -- Managing less than the currently selected; select this process
            -- instead.
            selected_process = process;
            selected_managing = number_managing;
          END IF;
        END IF;
      END LOOP;
      RETURN selected_process;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Helper that picks and sets a new region process to manage this rack
# controller.
CORE_SET_NEW_REGION = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_core_set_new_region(rack maasserver_node)
    RETURNS void as $$
    DECLARE
      region_process maasserver_regioncontrollerprocess;
    BEGIN
      -- Pick the new region process to manage this rack controller.
      region_process = sys_core_pick_new_region(rack);

      -- Update the rack controller and alert the region controller.
      UPDATE maasserver_node SET managing_process_id = region_process.id
      WHERE maasserver_node.id = rack.id;
      PERFORM pg_notify(
        CONCAT('sys_core_', region_process.id),
        CONCAT('watch_', CAST(rack.id AS text)));
      RETURN;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when a new region <-> rack RPC connection is made. This provides
# the logic to select the region controller that should manage this rack
# controller. Balancing of managed rack controllers is also done by this
# trigger.
CORE_REGIONRACKRPCONNECTION_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_core_rpc_insert()
    RETURNS trigger as $$
    DECLARE
      rack_controller maasserver_node;
      region_process maasserver_regioncontrollerprocess;
    BEGIN
      -- New connection from region <-> rack, check that the rack controller
      -- has a managing region controller.
      SELECT maasserver_node.* INTO rack_controller
      FROM maasserver_node
      WHERE maasserver_node.id = NEW.rack_controller_id;

      IF rack_controller.managing_process_id IS NULL THEN
        -- No managing region process for this rack controller.
        PERFORM sys_core_set_new_region(rack_controller);
      ELSE
        -- Currently managed check that the managing process is not dead.
        SELECT maasserver_regioncontrollerprocess.* INTO region_process
        FROM maasserver_regioncontrollerprocess
        WHERE maasserver_regioncontrollerprocess.id =
          rack_controller.managing_process_id;
        IF EXTRACT(EPOCH FROM region_process.updated) -
          EXTRACT(EPOCH FROM now()) > 90 THEN
          -- Region controller process is dead. A new region process needs to
          -- be selected for this rack controller.
          UPDATE maasserver_node SET managing_process_id = NULL
          WHERE maasserver_node.id = NEW.rack_controller_id;
          NEW.rack_controller_id = NULL;
          PERFORM sys_core_set_new_region(rack_controller);
        ELSE
          -- Currently being managed but lets see if we can re-balance the
          -- managing processes better. We only do the re-balance once the
          -- rack controller is connected to more than half of the running
          -- processes.
          IF sys_core_get_num_conn(rack_controller) /
            sys_core_get_num_processes() > 0.5 THEN
            -- Pick a new region process for this rack controller. Only update
            -- and perform the notification if the selection is different.
            region_process = sys_core_pick_new_region(rack_controller);
            IF region_process.id != rack_controller.managing_process_id THEN
              -- Alter the old process that its no longer responsable for
              -- this rack controller.
              PERFORM pg_notify(
                CONCAT('sys_core_', rack_controller.managing_process_id),
                CONCAT('unwatch_', CAST(rack_controller.id AS text)));
              -- Update the rack controller and alert the region controller.
              UPDATE maasserver_node
              SET managing_process_id = region_process.id
              WHERE maasserver_node.id = rack_controller.id;
              PERFORM pg_notify(
                CONCAT('sys_core_', region_process.id),
                CONCAT('watch_', CAST(rack_controller.id AS text)));
            END IF;
          END IF;
        END IF;
      END IF;

      -- First connection of the rack controller requires the DNS to be
      -- reloaded for the internal MAAS domain.
      IF sys_core_get_num_conn(rack_controller) = 1 THEN
        PERFORM sys_dns_publish_update(
          'rack controller ' || rack_controller.hostname || ' connected');
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when a region <-> rack connection is delete. When the managing
# region process is the one that loses its connection it will find a
# new region process to manage the rack controller.
CORE_REGIONRACKRPCONNECTION_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_core_rpc_delete()
    RETURNS trigger as $$
    DECLARE
      rack_controller maasserver_node;
      region_process maasserver_regioncontrollerprocess;
    BEGIN
      -- Connection from region <-> rack, has been removed. If that region
      -- process was managing that rack controller then a new one needs to
      -- be selected.
      SELECT maasserver_node.* INTO rack_controller
      FROM maasserver_node
      WHERE maasserver_node.id = OLD.rack_controller_id;

      -- Get the region process from the endpoint.
      SELECT
        process.* INTO region_process
      FROM
        maasserver_regioncontrollerprocess AS process,
        maasserver_regioncontrollerprocessendpoint AS endpoint
      WHERE process.id = endpoint.process_id
        AND endpoint.id = OLD.endpoint_id;

      -- Only perform an action if processes equal.
      IF rack_controller.managing_process_id = region_process.id THEN
        -- Region process was managing this rack controller. Tell it to stop
        -- watching the rack controller.
        PERFORM pg_notify(
          CONCAT('sys_core_', region_process.id),
          CONCAT('unwatch_', CAST(rack_controller.id AS text)));

        -- Pick a new region process for this rack controller.
        region_process = sys_core_pick_new_region(rack_controller);

        -- Update the rack controller and inform the new process.
        UPDATE maasserver_node
        SET managing_process_id = region_process.id
        WHERE maasserver_node.id = rack_controller.id;
        IF region_process.id IS NOT NULL THEN
          PERFORM pg_notify(
            CONCAT('sys_core_', region_process.id),
            CONCAT('watch_', CAST(rack_controller.id AS text)));
        END IF;
      END IF;

      -- No connections of the rack controller requires the DNS to be
      -- reloaded for the internal MAAS domain.
      IF sys_core_get_num_conn(rack_controller) = 0 THEN
        PERFORM sys_dns_publish_update(
          'rack controller ' || rack_controller.hostname || ' disconnected');
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when the VLAN is modified. When DHCP is turned off/on it will alert
# the primary/secondary rack controller to update. If the primary rack or
# secondary rack is changed it will alert the previous and new rack controller.
DHCP_VLAN_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_vlan_update()
    RETURNS trigger as $$
    DECLARE
      relay_vlan maasserver_vlan;
    BEGIN
      -- DHCP was turned off.
      IF OLD.dhcp_on AND NOT NEW.dhcp_on THEN
        PERFORM pg_notify(CONCAT('sys_dhcp_', OLD.primary_rack_id), '');
        IF OLD.secondary_rack_id IS NOT NULL THEN
          PERFORM pg_notify(CONCAT('sys_dhcp_', OLD.secondary_rack_id), '');
        END IF;
      -- DHCP was turned on.
      ELSIF NOT OLD.dhcp_on AND NEW.dhcp_on THEN
        PERFORM pg_notify(CONCAT('sys_dhcp_', NEW.primary_rack_id), '');
        IF NEW.secondary_rack_id IS NOT NULL THEN
          PERFORM pg_notify(CONCAT('sys_dhcp_', NEW.secondary_rack_id), '');
        END IF;
      -- MTU was changed.
      ELSIF OLD.mtu != NEW.mtu THEN
        PERFORM pg_notify(CONCAT('sys_dhcp_', OLD.primary_rack_id), '');
        IF OLD.secondary_rack_id IS NOT NULL THEN
          PERFORM pg_notify(CONCAT('sys_dhcp_', OLD.secondary_rack_id), '');
        END IF;
      -- DHCP state was not changed but the rack controllers might have been.
      ELSIF NEW.dhcp_on AND (
         OLD.primary_rack_id != NEW.primary_rack_id OR (
           OLD.secondary_rack_id IS NULL AND
           NEW.secondary_rack_id IS NOT NULL) OR (
           OLD.secondary_rack_id IS NOT NULL AND
           NEW.secondary_rack_id IS NULL) OR
         OLD.secondary_rack_id != NEW.secondary_rack_id) THEN
        -- Send the message to the old primary if no longer the primary.
        IF OLD.primary_rack_id != NEW.primary_rack_id THEN
          PERFORM pg_notify(CONCAT('sys_dhcp_', OLD.primary_rack_id), '');
        END IF;
        -- Always send the message to the primary as it has to be set.
        PERFORM pg_notify(CONCAT('sys_dhcp_', NEW.primary_rack_id), '');
        -- Send message to both old and new secondary rack controller if set.
        IF OLD.secondary_rack_id IS NOT NULL THEN
          PERFORM pg_notify(CONCAT('sys_dhcp_', OLD.secondary_rack_id), '');
        END IF;
        IF NEW.secondary_rack_id IS NOT NULL THEN
          PERFORM pg_notify(CONCAT('sys_dhcp_', NEW.secondary_rack_id), '');
        END IF;
      END IF;

      -- Relay VLAN was set when it was previously unset, or
      -- the MTU has changed for a VLAN with DHCP relay enabled.
      IF (OLD.relay_vlan_id IS NULL AND NEW.relay_vlan_id IS NOT NULL)
         OR (OLD.mtu != NEW.mtu AND NEW.relay_vlan_id IS NOT NULL) THEN
        SELECT maasserver_vlan.* INTO relay_vlan
        FROM maasserver_vlan
        WHERE maasserver_vlan.id = NEW.relay_vlan_id;
        IF relay_vlan.primary_rack_id IS NOT NULL THEN
          PERFORM pg_notify(
            CONCAT('sys_dhcp_', relay_vlan.primary_rack_id), '');
          IF relay_vlan.secondary_rack_id IS NOT NULL THEN
            PERFORM pg_notify(
              CONCAT('sys_dhcp_', relay_vlan.secondary_rack_id), '');
          END IF;
        END IF;
      -- Relay VLAN was unset when it was previously set.
      ELSIF OLD.relay_vlan_id IS NOT NULL AND NEW.relay_vlan_id IS NULL THEN
        SELECT maasserver_vlan.* INTO relay_vlan
        FROM maasserver_vlan
        WHERE maasserver_vlan.id = OLD.relay_vlan_id;
        IF relay_vlan.primary_rack_id IS NOT NULL THEN
          PERFORM pg_notify(
            CONCAT('sys_dhcp_', relay_vlan.primary_rack_id), '');
          IF relay_vlan.secondary_rack_id IS NOT NULL THEN
            PERFORM pg_notify(
              CONCAT('sys_dhcp_', relay_vlan.secondary_rack_id), '');
          END IF;
        END IF;
      -- Relay VLAN has changed on the VLAN.
      ELSIF OLD.relay_vlan_id != NEW.relay_vlan_id THEN
        -- Alert old VLAN if required.
        SELECT maasserver_vlan.* INTO relay_vlan
        FROM maasserver_vlan
        WHERE maasserver_vlan.id = OLD.relay_vlan_id;
        IF relay_vlan.primary_rack_id IS NOT NULL THEN
          PERFORM pg_notify(
            CONCAT('sys_dhcp_', relay_vlan.primary_rack_id), '');
          IF relay_vlan.secondary_rack_id IS NOT NULL THEN
            PERFORM pg_notify(
              CONCAT('sys_dhcp_', relay_vlan.secondary_rack_id), '');
          END IF;
        END IF;
        -- Alert new VLAN if required.
        SELECT maasserver_vlan.* INTO relay_vlan
        FROM maasserver_vlan
        WHERE maasserver_vlan.id = NEW.relay_vlan_id;
        IF relay_vlan.primary_rack_id IS NOT NULL THEN
          PERFORM pg_notify(
            CONCAT('sys_dhcp_', relay_vlan.primary_rack_id), '');
          IF relay_vlan.secondary_rack_id IS NOT NULL THEN
            PERFORM pg_notify(
              CONCAT('sys_dhcp_', relay_vlan.secondary_rack_id), '');
          END IF;
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Helper that alerts the primary and secondary rack controller for a VLAN.
DHCP_ALERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_alert(vlan maasserver_vlan)
    RETURNS void AS $$
    DECLARE
      relay_vlan maasserver_vlan;
    BEGIN
      IF vlan.dhcp_on THEN
        PERFORM pg_notify(CONCAT('sys_dhcp_', vlan.primary_rack_id), '');
        IF vlan.secondary_rack_id IS NOT NULL THEN
          PERFORM pg_notify(CONCAT('sys_dhcp_', vlan.secondary_rack_id), '');
        END IF;
      END IF;
      IF vlan.relay_vlan_id IS NOT NULL THEN
        SELECT maasserver_vlan.* INTO relay_vlan
        FROM maasserver_vlan
        WHERE maasserver_vlan.id = vlan.relay_vlan_id;
        IF relay_vlan.dhcp_on THEN
          PERFORM pg_notify(CONCAT(
            'sys_dhcp_', relay_vlan.primary_rack_id), '');
          IF relay_vlan.secondary_rack_id IS NOT NULL THEN
            PERFORM pg_notify(CONCAT(
              'sys_dhcp_', relay_vlan.secondary_rack_id), '');
          END IF;
        END IF;
      END IF;
      RETURN;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when a subnet's VLAN, CIDR, gateway IP, or DNS servers change.
# If the VLAN was changed it alerts both the rack controllers of the old VLAN
# and then the rack controllers of the new VLAN. Any other field that is
# updated just alerts the rack controllers of the current VLAN.
DHCP_SUBNET_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_subnet_update()
    RETURNS trigger as $$
    DECLARE
      vlan maasserver_vlan;
    BEGIN
      -- Subnet was moved to a new VLAN.
      IF OLD.vlan_id != NEW.vlan_id THEN
        -- Update old VLAN if DHCP is enabled.
        SELECT * INTO vlan
        FROM maasserver_vlan WHERE id = OLD.vlan_id;
        PERFORM sys_dhcp_alert(vlan);
        -- Update the new VLAN if DHCP is enabled.
        SELECT * INTO vlan
        FROM maasserver_vlan WHERE id = NEW.vlan_id;
        PERFORM sys_dhcp_alert(vlan);
      -- Related fields of subnet where changed.
      ELSIF OLD.cidr != NEW.cidr OR
        (OLD.gateway_ip IS NULL AND NEW.gateway_ip IS NOT NULL) OR
        (OLD.gateway_ip IS NOT NULL AND NEW.gateway_ip IS NULL) OR
        host(OLD.gateway_ip) != host(NEW.gateway_ip) OR
        OLD.dns_servers != NEW.dns_servers THEN
        -- Network has changed update alert DHCP if enabled.
        SELECT * INTO vlan
        FROM maasserver_vlan WHERE id = NEW.vlan_id;
        PERFORM sys_dhcp_alert(vlan);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when the subnet is deleted. Alerts the rack controllers of the
# VLAN the subnet belonged to.
DHCP_SUBNET_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_subnet_delete()
    RETURNS trigger as $$
    DECLARE
      vlan maasserver_vlan;
    BEGIN
      -- Update VLAN if DHCP is enabled.
      SELECT * INTO vlan
      FROM maasserver_vlan WHERE id = OLD.vlan_id;
      PERFORM sys_dhcp_alert(vlan);
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when a dynamic DHCP range is added to a subnet that is on a managed
# VLAN. Alerts the rack controllers for that VLAN.
DHCP_IPRANGE_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_iprange_insert()
    RETURNS trigger as $$
    DECLARE
      vlan maasserver_vlan;
    BEGIN
      -- Update VLAN if DHCP is enabled and a dynamic range.
      IF NEW.type = 'dynamic' THEN
        SELECT maasserver_vlan.* INTO vlan
        FROM maasserver_vlan, maasserver_subnet
        WHERE maasserver_subnet.id = NEW.subnet_id AND
          maasserver_subnet.vlan_id = maasserver_vlan.id;
        PERFORM sys_dhcp_alert(vlan);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when a dynamic DHCP range is updated on a subnet that is on a
# managed VLAN. Alerts the rack controllers for that VLAN.
DHCP_IPRANGE_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_iprange_update()
    RETURNS trigger as $$
    DECLARE
      vlan maasserver_vlan;
    BEGIN
      -- Update VLAN if DHCP is enabled and was or is now a dynamic range.
      IF OLD.type = 'dynamic' OR NEW.type = 'dynamic' THEN
        SELECT maasserver_vlan.* INTO vlan
        FROM maasserver_vlan, maasserver_subnet
        WHERE maasserver_subnet.id = NEW.subnet_id AND
          maasserver_subnet.vlan_id = maasserver_vlan.id;
        PERFORM sys_dhcp_alert(vlan);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when a dynamic DHCP range is deleted from a subnet that is on a
# managed VLAN. Alerts the rack controllers for that VLAN.
DHCP_IPRANGE_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_iprange_delete()
    RETURNS trigger as $$
    DECLARE
      vlan maasserver_vlan;
    BEGIN
      -- Update VLAN if DHCP is enabled and was dynamic range.
      IF OLD.type = 'dynamic' THEN
        SELECT maasserver_vlan.* INTO vlan
        FROM maasserver_vlan, maasserver_subnet
        WHERE maasserver_subnet.id = OLD.subnet_id AND
          maasserver_subnet.vlan_id = maasserver_vlan.id;
        PERFORM sys_dhcp_alert(vlan);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when an IP address that has an IP set (not temp) and is not
# DISCOVERED is inserted to a subnet on a managed VLAN. Alerts the rack
# controllers for that VLAN.
DHCP_STATICIPADDRESS_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_staticipaddress_insert()
    RETURNS trigger as $$
    DECLARE
      vlan maasserver_vlan;
    BEGIN
      -- Update VLAN if DHCP is enabled, IP is set and not DISCOVERED.
      IF NEW.alloc_type != 6 AND NEW.ip IS NOT NULL AND host(NEW.ip) != '' AND
        NEW.temp_expires_on IS NULL THEN
        SELECT maasserver_vlan.* INTO vlan
        FROM maasserver_vlan, maasserver_subnet
        WHERE maasserver_subnet.id = NEW.subnet_id AND
          maasserver_subnet.vlan_id = maasserver_vlan.id;
        PERFORM sys_dhcp_alert(vlan);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when an IP address that has an IP set (not temp) and is not
# DISCOVERED is updated. If the subnet changes then it alerts the rack
# controllers of each VLAN if the VLAN differs from the previous VLAN.
DHCP_STATICIPADDRESS_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_staticipaddress_update()
    RETURNS trigger as $$
    DECLARE
      old_vlan maasserver_vlan;
      new_vlan maasserver_vlan;
    BEGIN
      -- Ignore DISCOVERED IP addresses.
      IF NEW.alloc_type != 6 THEN
        IF OLD.subnet_id != NEW.subnet_id THEN
          -- Subnet has changed; update each VLAN if different.
          SELECT maasserver_vlan.* INTO old_vlan
          FROM maasserver_vlan, maasserver_subnet
          WHERE maasserver_subnet.id = OLD.subnet_id AND
            maasserver_subnet.vlan_id = maasserver_vlan.id;
          SELECT maasserver_vlan.* INTO new_vlan
          FROM maasserver_vlan, maasserver_subnet
          WHERE maasserver_subnet.id = NEW.subnet_id AND
            maasserver_subnet.vlan_id = maasserver_vlan.id;
          IF old_vlan.id != new_vlan.id THEN
            -- Different VLAN's; update each if DHCP enabled.
            PERFORM sys_dhcp_alert(old_vlan);
            PERFORM sys_dhcp_alert(new_vlan);
          ELSE
            -- Same VLAN so only need to update once.
            PERFORM sys_dhcp_alert(new_vlan);
          END IF;
        ELSIF (OLD.ip IS NULL AND NEW.ip IS NOT NULL) OR
          (OLD.ip IS NOT NULL and NEW.ip IS NULL) OR
          (OLD.temp_expires_on IS NULL AND NEW.temp_expires_on IS NOT NULL) OR
          (OLD.temp_expires_on IS NOT NULL AND NEW.temp_expires_on IS NULL) OR
          (host(OLD.ip) != host(NEW.ip)) THEN
          -- Assigned IP address has changed.
          SELECT maasserver_vlan.* INTO new_vlan
          FROM maasserver_vlan, maasserver_subnet
          WHERE maasserver_subnet.id = NEW.subnet_id AND
            maasserver_subnet.vlan_id = maasserver_vlan.id;
          PERFORM sys_dhcp_alert(new_vlan);
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when an IP address is removed from a subnet that is on a
# managed VLAN. Alerts the rack controllers of that VLAN.
DHCP_STATICIPADDRESS_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_staticipaddress_delete()
    RETURNS trigger as $$
    DECLARE
      vlan maasserver_vlan;
    BEGIN
      -- Update VLAN if DHCP is enabled and has an IP address.
      IF host(OLD.ip) != '' AND OLD.temp_expires_on IS NULL THEN
        SELECT maasserver_vlan.* INTO vlan
        FROM maasserver_vlan, maasserver_subnet
        WHERE maasserver_subnet.id = OLD.subnet_id AND
          maasserver_subnet.vlan_id = maasserver_vlan.id;
        PERFORM sys_dhcp_alert(vlan);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when the interface name or MAC address is updated. Alerts
# rack controllers on all managed VLAN's that the interface has a non
# DISCOVERED IP address on.
DHCP_INTERFACE_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_interface_update()
    RETURNS trigger as $$
    DECLARE
      vlan maasserver_vlan;
    BEGIN
      -- Update VLAN if DHCP is enabled and the interface name or MAC
      -- address has changed.
      IF OLD.name != NEW.name OR OLD.mac_address != NEW.mac_address THEN
        FOR vlan IN (
          SELECT DISTINCT ON (maasserver_vlan.id)
            maasserver_vlan.*
          FROM
            maasserver_vlan,
            maasserver_subnet,
            maasserver_staticipaddress,
            maasserver_interface_ip_addresses AS ip_link
          WHERE maasserver_staticipaddress.subnet_id = maasserver_subnet.id
          AND ip_link.staticipaddress_id = maasserver_staticipaddress.id
          AND ip_link.interface_id = NEW.id
          AND maasserver_staticipaddress.alloc_type != 6
          AND maasserver_staticipaddress.ip IS NOT NULL
          AND maasserver_staticipaddress.temp_expires_on IS NULL
          AND host(maasserver_staticipaddress.ip) != ''
          AND maasserver_vlan.id = maasserver_subnet.vlan_id)
        LOOP
          PERFORM sys_dhcp_alert(vlan);
        END LOOP;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when the hostname of the node is changed. Alerts rack controllers
# for all VLAN's that this interface has a non DISCOVERED IP address.
DHCP_NODE_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_node_update()
    RETURNS trigger as $$
    DECLARE
      vlan maasserver_vlan;
    BEGIN
      -- Update VLAN if on every interface on the node that is managed when
      -- the node hostname is changed.
      IF OLD.hostname != NEW.hostname THEN
        FOR vlan IN (
          SELECT DISTINCT ON (maasserver_vlan.id)
            maasserver_vlan.*
          FROM
            maasserver_vlan,
            maasserver_staticipaddress,
            maasserver_subnet,
            maasserver_interface,
            maasserver_interface_ip_addresses AS ip_link
          WHERE maasserver_staticipaddress.subnet_id = maasserver_subnet.id
          AND ip_link.staticipaddress_id = maasserver_staticipaddress.id
          AND ip_link.interface_id = maasserver_interface.id
          AND maasserver_interface.node_id = NEW.id
          AND maasserver_staticipaddress.alloc_type != 6
          AND maasserver_staticipaddress.ip IS NOT NULL
          AND maasserver_staticipaddress.temp_expires_on IS NULL
          AND host(maasserver_staticipaddress.ip) != ''
          AND maasserver_vlan.id = maasserver_subnet.vlan_id)
        LOOP
          PERFORM sys_dhcp_alert(vlan);
        END LOOP;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

DHCP_UPDATE_ALL_VLANS = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_update_all_vlans()
    RETURNS void as $$
    DECLARE
      rack INTEGER;
    BEGIN
      FOR rack IN (
        WITH racks AS (
          SELECT primary_rack_id, secondary_rack_id FROM maasserver_vlan
          WHERE maasserver_vlan.dhcp_on = true
        )
        SELECT primary_rack_id FROM racks
        WHERE primary_rack_id IS NOT NULL
        UNION
        SELECT secondary_rack_id FROM racks
        WHERE secondary_rack_id IS NOT NULL)
      LOOP
        PERFORM pg_notify(CONCAT('sys_dhcp_', rack), '');
      END LOOP;
      RETURN;
    END;
    $$ LANGUAGE plpgsql;
    """
)

DHCP_SNIPPET_UPDATE_SUBNET = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_snippet_update_subnet(
      _subnet_id INTEGER)
    RETURNS void as $$
    DECLARE
      vlan maasserver_vlan;
    BEGIN
      FOR vlan IN (
        SELECT
          maasserver_vlan.*
        FROM
          maasserver_vlan,
          maasserver_subnet
        WHERE maasserver_subnet.id = _subnet_id
          AND maasserver_vlan.id = maasserver_subnet.vlan_id
          AND (maasserver_vlan.dhcp_on = true
            OR maasserver_vlan.relay_vlan_id IS NOT NULL))
        LOOP
          PERFORM sys_dhcp_alert(vlan);
        END LOOP;
      RETURN;
    END;
    $$ LANGUAGE plpgsql;
    """
)

DHCP_SNIPPET_UPDATE_NODE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_snippet_update_node(_node_id INTEGER)
    RETURNS void as $$
    DECLARE
      rack INTEGER;
    BEGIN
      FOR rack IN (
        WITH racks AS (
          SELECT primary_rack_id, secondary_rack_id
          FROM maasserver_vlan, maasserver_interface
          WHERE maasserver_interface.node_id = _node_id
            AND maasserver_interface.vlan_id = maasserver_vlan.id
          AND (maasserver_vlan.dhcp_on = true
            OR maasserver_vlan.relay_vlan_id IS NOT NULL))
        SELECT primary_rack_id FROM racks
        WHERE primary_rack_id IS NOT NULL
        UNION
        SELECT secondary_rack_id FROM racks
        WHERE secondary_rack_id IS NOT NULL)
      LOOP
        PERFORM pg_notify(CONCAT('sys_dhcp_', rack), '');
      END LOOP;
      RETURN;
    END;
    $$ LANGUAGE plpgsql;
    """
)

DHCP_SNIPPET_UPDATE_VALUE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_snippet_update_value(
        _dhcp_snippet maasserver_dhcpsnippet)
    RETURNS void as $$
    BEGIN
      IF _dhcp_snippet.subnet_id IS NOT NULL THEN
        PERFORM sys_dhcp_snippet_update_subnet(_dhcp_snippet.subnet_id);
      ELSIF _dhcp_snippet.node_id is NOT NULL THEN
        PERFORM sys_dhcp_snippet_update_node(_dhcp_snippet.node_id);
      ELSE
        -- This is a global snippet, everyone has to update. This should only
        -- be triggered when neither subnet_id or node_id are set. We verify
        -- that only subnet_id xor node_id are set in DHCPSnippet.clean()
        PERFORM sys_dhcp_update_all_vlans();
      END IF;
      RETURN;
    END;
    $$ LANGUAGE plpgsql;
    """
)

DHCP_SNIPPET_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_snippet_insert()
    RETURNS trigger as $$
    BEGIN
      IF NEW.enabled THEN
        PERFORM sys_dhcp_snippet_update_value(NEW);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

DHCP_SNIPPET_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_snippet_update()
    RETURNS trigger as $$
    BEGIN
      IF OLD.enabled = NEW.enabled AND NEW.enabled IS FALSE THEN
        -- If the DHCP snippet is disabled don't fire any alerts
        RETURN NEW;
      ELSIF ((OLD.value_id != NEW.value_id) OR
          (OLD.enabled != NEW.enabled) OR
          (OLD.description != NEW.description)) THEN
        PERFORM sys_dhcp_snippet_update_value(NEW);
      ELSIF ((OLD.subnet_id IS NULL AND NEW.subnet_id IS NOT NULL) OR
          (OLD.subnet_id IS NOT NULL AND NEW.subnet_id IS NULL) OR
          (OLD.subnet_id != NEW.subnet_id)) THEN
        IF NEW.subnet_id IS NOT NULL THEN
          PERFORM sys_dhcp_snippet_update_subnet(NEW.subnet_id);
        END IF;
        IF OLD.subnet_id IS NOT NULL THEN
          PERFORM sys_dhcp_snippet_update_subnet(OLD.subnet_id);
        END IF;
      ELSIF ((OLD.node_id IS NULL AND NEW.node_id IS NOT NULL) OR
          (OLD.node_id IS NOT NULL AND NEW.node_id IS NULL) OR
          (OLD.node_id != NEW.node_id)) THEN
        IF NEW.node_id IS NOT NULL THEN
          PERFORM sys_dhcp_snippet_update_node(NEW.node_id);
        END IF;
        IF OLD.node_id IS NOT NULL THEN
          PERFORM sys_dhcp_snippet_update_node(OLD.node_id);
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

DHCP_SNIPPET_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_snippet_delete()
    RETURNS trigger as $$
    BEGIN
      IF OLD.enabled THEN
        PERFORM sys_dhcp_snippet_update_value(OLD);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

DHCP_CONFIG_NTP_SERVERS_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_config_ntp_servers_insert()
    RETURNS trigger as $$
    BEGIN
      IF NEW.name = 'ntp_servers' THEN
        PERFORM sys_dhcp_update_all_vlans();
      ELSIF NEW.name = 'ntp_external_only' THEN
        PERFORM sys_dhcp_update_all_vlans();
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

DHCP_CONFIG_NTP_SERVERS_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_config_ntp_servers_update()
    RETURNS trigger as $$
    BEGIN
      IF OLD.name IN ('ntp_servers', 'ntp_external_only')
      OR NEW.name IN ('ntp_servers', 'ntp_external_only') THEN
        IF OLD.value != NEW.value THEN
          PERFORM sys_dhcp_update_all_vlans();
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

DHCP_CONFIG_NTP_SERVERS_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dhcp_config_ntp_servers_delete()
    RETURNS trigger as $$
    BEGIN
      IF OLD.name IN ('ntp_servers', 'ntp_external_only') THEN
        PERFORM sys_dhcp_update_all_vlans();
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when DNS needs to be published. In essense this means on insert
# into maasserver_dnspublication.
DNS_PUBLISH = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_publish()
    RETURNS trigger AS $$
    BEGIN
      PERFORM pg_notify('sys_dns', '');
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Procedure to mark DNS as needing an update.
DNS_PUBLISH_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_publish_update(reason text)
    RETURNS void as $$
    BEGIN
      INSERT INTO maasserver_dnspublication
        (serial, created, source)
      VALUES
        (nextval('maasserver_zone_serial_seq'), now(),
         substring(reason FOR 255));
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a new domain is added. Increments the zone serial and
# notifies that DNS needs to be updated.
DNS_DOMAIN_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_domain_insert()
    RETURNS trigger as $$
    BEGIN
      IF NEW.authoritative THEN
          PERFORM sys_dns_publish_update(
            'added zone ' || NEW.name);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a domain is updated. Increments the zone serial and
# notifies that DNS needs to be updated. Only watches authoritative, name,
# and ttl.
DNS_DOMAIN_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_domain_update()
    RETURNS trigger as $$
    DECLARE
      changes text[];
    BEGIN
      IF OLD.authoritative AND NOT NEW.authoritative THEN
        PERFORM sys_dns_publish_update(
            'removed zone ' || NEW.name);
      ELSIF NOT OLD.authoritative AND NEW.authoritative THEN
        PERFORM sys_dns_publish_update(
            'added zone ' || NEW.name);
      ELSIF OLD.authoritative and NEW.authoritative THEN
        IF OLD.name != NEW.name THEN
            changes := changes || ('renamed to ' || NEW.name);
        END IF;
        IF ((OLD.ttl IS NULL AND NEW.ttl IS NOT NULL) OR
            (OLD.ttl IS NOT NULL and NEW.ttl IS NULL) OR
            (OLD.ttl != NEW.ttl)) THEN
            changes := changes || (
              'ttl changed to ' || COALESCE(text(NEW.ttl), 'default'));
        END IF;
        IF array_length(changes, 1) != 0 THEN
          PERFORM sys_dns_publish_update(
            'zone ' || OLD.name || ' ' || array_to_string(changes, ' and '));
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a domain is deleted. Increments the zone serial and
# notifies that DNS needs to be updated.
DNS_DOMAIN_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_domain_delete()
    RETURNS trigger as $$
    BEGIN
      IF OLD.authoritative THEN
        PERFORM sys_dns_publish_update(
            'removed zone ' || OLD.name);
      END IF;
      RETURN OLD;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a static IP address is updated. Increments the zone serial and
# notifies that DNS needs to be updated. Only watches ip.
DNS_STATICIPADDRESS_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_staticipaddress_update()
    RETURNS trigger as $$
    BEGIN
      IF ((OLD.ip IS NULL and NEW.ip IS NOT NULL) OR
          (OLD.ip IS NOT NULL and NEW.ip IS NULL) OR
          (OLD.temp_expires_on IS NULL AND NEW.temp_expires_on IS NOT NULL) OR
          (OLD.temp_expires_on IS NOT NULL AND NEW.temp_expires_on IS NULL) OR
          (OLD.ip != NEW.ip)) OR
          (OLD.alloc_type != NEW.alloc_type) THEN
        IF EXISTS (
            SELECT
              domain.id
            FROM maasserver_staticipaddress AS staticipaddress
            LEFT JOIN (
              maasserver_interface_ip_addresses AS iia
              JOIN maasserver_interface AS interface ON
                iia.interface_id = interface.id
              JOIN maasserver_node AS node ON
                node.id = interface.node_id) ON
              iia.staticipaddress_id = staticipaddress.id
            LEFT JOIN (
              maasserver_dnsresource_ip_addresses AS dia
              JOIN maasserver_dnsresource AS dnsresource ON
                dia.dnsresource_id = dnsresource.id) ON
              dia.staticipaddress_id = staticipaddress.id
            JOIN maasserver_domain AS domain ON
              domain.id = node.domain_id OR domain.id = dnsresource.domain_id
            WHERE
              domain.authoritative = TRUE AND
              (staticipaddress.id = OLD.id OR
               staticipaddress.id = NEW.id))
        THEN
          IF OLD.ip IS NULL and NEW.ip IS NOT NULL and
            NEW.temp_expires_on IS NULL THEN
            PERFORM sys_dns_publish_update(
              'ip ' || host(NEW.ip) || ' allocated');
            RETURN NEW;
          ELSIF OLD.ip IS NOT NULL and NEW.ip IS NULL and
            NEW.temp_expires_on IS NULL THEN
            PERFORM sys_dns_publish_update(
              'ip ' || host(OLD.ip) || ' released');
            RETURN NEW;
          ELSIF OLD.ip != NEW.ip and NEW.temp_expires_on IS NULL THEN
            PERFORM sys_dns_publish_update(
              'ip ' || host(OLD.ip) || ' changed to ' || host(NEW.ip));
            RETURN NEW;
          ELSIF OLD.ip = NEW.ip and OLD.temp_expires_on IS NOT NULL and
            NEW.temp_expires_on IS NULL THEN
            PERFORM sys_dns_publish_update(
              'ip ' || host(NEW.ip) || ' allocated');
            RETURN NEW;
          ELSIF OLD.ip = NEW.ip and OLD.temp_expires_on IS NULL and
            NEW.temp_expires_on IS NOT NULL THEN
            PERFORM sys_dns_publish_update(
              'ip ' || host(NEW.ip) || ' released');
            RETURN NEW;
          END IF;

          -- Made it this far then only alloc_type has changed. Only send
          -- a notification is the IP address is assigned.
          IF NEW.ip IS NOT NULL and NEW.temp_expires_on IS NULL THEN
            PERFORM sys_dns_publish_update(
              'ip ' || host(OLD.ip) || ' alloc_type changed to ' ||
              NEW.alloc_type);
          END IF;
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when an interface is linked to an IP address. Increments the zone
# serial and notifies that DNS needs to be updated.
DNS_NIC_IP_LINK = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_nic_ip_link()
    RETURNS trigger as $$
    DECLARE
      node maasserver_node;
      nic maasserver_interface;
      ip maasserver_staticipaddress;
    BEGIN
      SELECT maasserver_interface.* INTO nic
      FROM maasserver_interface
      WHERE maasserver_interface.id = NEW.interface_id;
      SELECT maasserver_node.* INTO node
      FROM maasserver_node
      WHERE maasserver_node.id = nic.node_id;
      SELECT maasserver_staticipaddress.* INTO ip
      FROM maasserver_staticipaddress
      WHERE maasserver_staticipaddress.id = NEW.staticipaddress_id;
      IF (ip.ip IS NOT NULL AND ip.temp_expires_on IS NULL AND
          host(ip.ip) != '' AND EXISTS (
            SELECT maasserver_domain.id
            FROM maasserver_domain
            WHERE
              maasserver_domain.id = node.domain_id AND
              maasserver_domain.authoritative = TRUE))
      THEN
        PERFORM sys_dns_publish_update(
          'ip ' || host(ip.ip) || ' connected to ' || node.hostname ||
          ' on ' || nic.name);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when an interface is unlinked to an IP address. Increments the zone
# serial and notifies that DNS needs to be updated.
DNS_NIC_IP_UNLINK = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_nic_ip_unlink()
    RETURNS trigger as $$
    DECLARE
      node maasserver_node;
      nic maasserver_interface;
      ip maasserver_staticipaddress;
      changes text[];
    BEGIN
      SELECT maasserver_interface.* INTO nic
      FROM maasserver_interface
      WHERE maasserver_interface.id = OLD.interface_id;
      SELECT maasserver_node.* INTO node
      FROM maasserver_node
      WHERE maasserver_node.id = nic.node_id;
      SELECT maasserver_staticipaddress.* INTO ip
      FROM maasserver_staticipaddress
      WHERE maasserver_staticipaddress.id = OLD.staticipaddress_id;
      IF (ip.ip IS NOT NULL AND ip.temp_expires_on IS NULL AND EXISTS (
            SELECT maasserver_domain.id
            FROM maasserver_domain
            WHERE
              maasserver_domain.id = node.domain_id AND
              maasserver_domain.authoritative = TRUE))
      THEN
        PERFORM sys_dns_publish_update(
          'ip ' || host(ip.ip) || ' disconnected from ' || node.hostname ||
          ' on ' || nic.name);
      END IF;
      RETURN OLD;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a subnet is inserted. Increments the zone serial and notifies
# that DNS needs to be updated. Doesn't notify if the rdns_mode is
# disabled (0).
DNS_SUBNET_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_subnet_insert()
    RETURNS trigger as $$
    DECLARE
      changes text[];
    BEGIN
      IF NEW.rdns_mode != 0 THEN
        PERFORM sys_dns_publish_update('added subnet ' || text(NEW.cidr));
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a subnet is updated. Increments the zone serial and notifies
# that DNS needs to be updated. Only watches changes on the cidr, rdns_mode
# and allow_dns.
DNS_SUBNET_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_subnet_update()
    RETURNS trigger as $$
    BEGIN
      IF OLD.cidr != NEW.cidr THEN
        PERFORM sys_dns_publish_update(
            'subnet ' || text(OLD.cidr) || ' changed to ' || text(NEW.CIDR));
        RETURN NEW;
      END IF;
      IF OLD.rdns_mode != NEW.rdns_mode THEN
        PERFORM sys_dns_publish_update(
            'subnet ' || text(NEW.cidr) || ' rdns changed to ' ||
            NEW.rdns_mode);
      END IF;
      IF OLD.allow_dns != NEW.allow_dns THEN
        PERFORM sys_dns_publish_update(
            'subnet ' || text(NEW.cidr) || ' allow_dns changed to ' ||
            NEW.allow_dns);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a subnet is deleted. Increments the zone serial and notifies
# that DNS needs to be updated. Doesn't notify if the rdns_mode is
# disabled (0).
DNS_SUBNET_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_subnet_delete()
    RETURNS trigger as $$
    DECLARE
      changes text[];
    BEGIN
      IF OLD.rdns_mode != 0 THEN
        PERFORM sys_dns_publish_update('removed subnet ' || text(OLD.cidr));
      END IF;
      RETURN OLD;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a node is updated. Increments the zone serial and notifies
# that DNS needs to be updated. Only watches changes on the hostname and
# linked domain for the node.
DNS_NODE_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_node_update()
    RETURNS trigger as $$
    DECLARE
      domain maasserver_domain;
      new_domain maasserver_domain;
      changes text[];
    BEGIN
      IF OLD.hostname != NEW.hostname AND OLD.domain_id = NEW.domain_id THEN
        IF EXISTS(
            SELECT maasserver_domain.id
            FROM maasserver_domain
            WHERE
              maasserver_domain.authoritative = TRUE AND
              maasserver_domain.id = NEW.domain_id) THEN
          PERFORM sys_dns_publish_update(
            'node ' || OLD.hostname || ' changed hostname to ' ||
            NEW.hostname);
        END IF;
      ELSIF OLD.domain_id != NEW.domain_id THEN
        -- Domains have changed. If either one is authoritative then DNS
        -- needs to be updated.
        SELECT maasserver_domain.* INTO domain
        FROM maasserver_domain
        WHERE maasserver_domain.id = OLD.domain_id;
        SELECT maasserver_domain.* INTO new_domain
        FROM maasserver_domain
        WHERE maasserver_domain.id = NEW.domain_id;
        IF domain.authoritative = TRUE OR new_domain.authoritative = TRUE THEN
            PERFORM sys_dns_publish_update(
              'node ' || NEW.hostname || ' changed zone to ' ||
              new_domain.name);
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a node is deleted. Increments the zone serial and notifies
# that DNS needs to be updated.
DNS_NODE_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_node_DELETE()
    RETURNS trigger as $$
    DECLARE
      domain maasserver_domain;
      new_domain maasserver_domain;
      changes text[];
    BEGIN
      IF EXISTS(
          SELECT maasserver_domain.id
          FROM maasserver_domain
          WHERE
            maasserver_domain.authoritative = TRUE AND
            maasserver_domain.id = OLD.domain_id) THEN
        PERFORM sys_dns_publish_update(
          'removed node ' || OLD.hostname);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a interface is updated. Increments the zone serial and
# notifies that DNS needs to be updated. Only watches changes on the name and
# the node that the interface belongs to.
DNS_INTERFACE_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_interface_update()
    RETURNS trigger as $$
    DECLARE
      node maasserver_node;
      changes text[];
    BEGIN
      IF OLD.name != NEW.name AND OLD.node_id = NEW.node_id THEN
        IF NEW.node_id IS NOT NULL THEN
            SELECT maasserver_node.* INTO node
            FROM maasserver_node
            WHERE maasserver_node.id = NEW.node_id;
            IF EXISTS(
                SELECT maasserver_domain.id
                FROM maasserver_domain
                WHERE
                  maasserver_domain.authoritative = TRUE AND
                  maasserver_domain.id = node.domain_id) THEN
              PERFORM sys_dns_publish_update(
                'node ' || node.hostname || ' renamed interface ' ||
                OLD.name || ' to ' || NEW.name);
            END IF;
        END IF;
      ELSIF OLD.node_id IS NULL and NEW.node_id IS NOT NULL THEN
        SELECT maasserver_node.* INTO node
        FROM maasserver_node
        WHERE maasserver_node.id = NEW.node_id;
        IF EXISTS(
            SELECT maasserver_domain.id
            FROM maasserver_domain
            WHERE
              maasserver_domain.authoritative = TRUE AND
              maasserver_domain.id = node.domain_id) THEN
          PERFORM sys_dns_publish_update(
            'node ' || node.hostname || ' added interface ' || NEW.name);
        END IF;
      ELSIF OLD.node_id IS NOT NULL and NEW.node_id IS NULL THEN
        SELECT maasserver_node.* INTO node
        FROM maasserver_node
        WHERE maasserver_node.id = OLD.node_id;
        IF EXISTS(
            SELECT maasserver_domain.id
            FROM maasserver_domain
            WHERE
              maasserver_domain.authoritative = TRUE AND
              maasserver_domain.id = node.domain_id) THEN
          PERFORM sys_dns_publish_update(
            'node ' || node.hostname || ' removed interface ' || NEW.name);
        END IF;
      ELSIF OLD.node_id != NEW.node_id THEN
        SELECT maasserver_node.* INTO node
        FROM maasserver_node
        WHERE maasserver_node.id = OLD.node_id;
        IF EXISTS(
            SELECT maasserver_domain.id
            FROM maasserver_domain
            WHERE
              maasserver_domain.authoritative = TRUE AND
              maasserver_domain.id = node.domain_id) THEN
          PERFORM sys_dns_publish_update(
            'node ' || node.hostname || ' removed interface ' || NEW.name);
        END IF;
        SELECT maasserver_node.* INTO node
        FROM maasserver_node
        WHERE maasserver_node.id = NEW.node_id;
        IF EXISTS(
            SELECT maasserver_domain.id
            FROM maasserver_domain
            WHERE
              maasserver_domain.authoritative = TRUE AND
              maasserver_domain.id = node.domain_id) THEN
          PERFORM sys_dns_publish_update(
            'node ' || node.hostname || ' added interface ' || NEW.name);
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a config is inserted. Increments the zone serial and notifies
# that DNS needs to be updated. Only watches for inserts on config
# upstream_dns, dnssec_validation, default_dns_ttl, windows_kms_host,
# dns_trusted_acls and maas_internal_domain.
DNS_CONFIG_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_config_insert()
    RETURNS trigger as $$
    BEGIN
      -- Only care about the
      IF (NEW.name = 'upstream_dns' OR
          NEW.name = 'dnssec_validation' OR
          NEW.name = 'dns_trusted_acl' OR
          NEW.name = 'default_dns_ttl' OR
          NEW.name = 'windows_kms_host' OR
          NEW.name = 'maas_internal_domain')
      THEN
        PERFORM sys_dns_publish_update(
          'configuration ' || NEW.name || ' set to ' ||
          COALESCE(NEW.value, 'NULL'));
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)

# Triggered when a config is updated. Increments the zone serial and notifies
# that DNS needs to be updated. Only watches for updates on config
# upstream_dns, dnssec_validation, dns_trusted_acl, default_dns_ttl,
# and windows_kms_host.
DNS_CONFIG_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_config_update()
    RETURNS trigger as $$
    BEGIN
      -- Only care about the upstream_dns, default_dns_ttl,
      -- dns_trusted_acl and windows_kms_host.
      IF (OLD.value != NEW.value AND (
          NEW.name = 'upstream_dns' OR
          NEW.name = 'dnssec_validation' OR
          NEW.name = 'dns_trusted_acl' OR
          NEW.name = 'default_dns_ttl' OR
          NEW.name = 'windows_kms_host' OR
          NEW.name = 'maas_internal_domain'))
      THEN
        PERFORM sys_dns_publish_update(
          'configuration ' || NEW.name || ' changed to ' || NEW.value);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a DNS resource is inserted. Increments the zone serial and
# notifies that DNS needs to be updated.
DNS_DNSRESOURCE_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_dnsresource_insert()
    RETURNS trigger as $$
    DECLARE
      domain maasserver_domain;
    BEGIN
      SELECT maasserver_domain.* INTO domain
      FROM maasserver_domain
      WHERE maasserver_domain.id = NEW.domain_id;
      PERFORM sys_dns_publish_update(
        'zone ' || domain.name || ' added resource ' ||
        COALESCE(NEW.name, 'NULL'));
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a DNS resource is updated. Increments the zone serial and
# notifies that DNS needs to be updated.
DNS_DNSRESOURCE_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_dnsresource_update()
    RETURNS trigger as $$
    DECLARE
      domain maasserver_domain;
    BEGIN
      IF OLD.domain_id != NEW.domain_id THEN
        SELECT maasserver_domain.* INTO domain
        FROM maasserver_domain
        WHERE maasserver_domain.id = OLD.domain_id;
        PERFORM sys_dns_publish_update(
          'zone ' || domain.name || ' removed resource ' ||
          COALESCE(NEW.name, 'NULL'));
        SELECT maasserver_domain.* INTO domain
        FROM maasserver_domain
        WHERE maasserver_domain.id = NEW.domain_id;
        PERFORM sys_dns_publish_update(
          'zone ' || domain.name || ' added resource ' ||
          COALESCE(NEW.name, 'NULL'));
      ELSIF ((OLD.name IS NULL AND NEW.name IS NOT NULL) OR
          (OLD.name IS NOT NULL AND NEW.name IS NULL) OR
          (OLD.name != NEW.name) OR
          (OLD.address_ttl IS NULL AND NEW.address_ttl IS NOT NULL) OR
          (OLD.address_ttl IS NOT NULL AND NEW.address_ttl IS NULL) OR
          (OLD.address_ttl != NEW.address_ttl)) THEN
        SELECT maasserver_domain.* INTO domain
        FROM maasserver_domain
        WHERE maasserver_domain.id = NEW.domain_id;
        PERFORM sys_dns_publish_update(
          'zone ' || domain.name || ' updated resource ' ||
          COALESCE(NEW.name, 'NULL'));
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a DNS resource is deleted. Increments the zone serial and
# notifies that DNS needs to be updated.
DNS_DNSRESOURCE_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_dnsresource_delete()
    RETURNS trigger as $$
    DECLARE
      domain maasserver_domain;
    BEGIN
      SELECT maasserver_domain.* INTO domain
      FROM maasserver_domain
      WHERE maasserver_domain.id = OLD.domain_id;
      PERFORM sys_dns_publish_update(
        'zone ' || domain.name || ' removed resource ' ||
        COALESCE(OLD.name, 'NULL'));
      RETURN OLD;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when an IP address is linked to a DNS resource. Increments the zone
# serial and notifies that DNS needs to be updated.
DNS_DNSRESOURCE_IP_LINK = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_dnsresource_ip_link()
    RETURNS trigger as $$
    DECLARE
      sip maasserver_staticipaddress;
      resource maasserver_dnsresource;
      domain maasserver_domain;
    BEGIN
      SELECT maasserver_staticipaddress.* INTO sip
      FROM maasserver_staticipaddress
      WHERE maasserver_staticipaddress.id = NEW.staticipaddress_id;
      SELECT maasserver_dnsresource.* INTO resource
      FROM maasserver_dnsresource
      WHERE maasserver_dnsresource.id = NEW.dnsresource_id;
      SELECT maasserver_domain.* INTO domain
      FROM maasserver_domain
      WHERE maasserver_domain.id = resource.domain_id;
      IF sip.ip IS NOT NULL THEN
          PERFORM sys_dns_publish_update(
            'ip ' || host(sip.ip) || ' linked to resource ' ||
            COALESCE(resource.name, 'NULL') || ' on zone ' || domain.name);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when an IP address is unlinked to a DNS resource. Increments the
# zone serial and notifies that DNS needs to be updated.
DNS_DNSRESOURCE_IP_UNLINK = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_dnsresource_ip_unlink()
    RETURNS trigger as $$
    DECLARE
      sip maasserver_staticipaddress;
      resource maasserver_dnsresource;
      domain maasserver_domain;
    BEGIN
      SELECT maasserver_staticipaddress.* INTO sip
      FROM maasserver_staticipaddress
      WHERE maasserver_staticipaddress.id = OLD.staticipaddress_id;
      SELECT maasserver_dnsresource.* INTO resource
      FROM maasserver_dnsresource
      WHERE maasserver_dnsresource.id = OLD.dnsresource_id;
      SELECT maasserver_domain.* INTO domain
      FROM maasserver_domain
      WHERE maasserver_domain.id = resource.domain_id;
      IF sip.ip IS NOT NULL THEN
          PERFORM sys_dns_publish_update(
            'ip ' || host(sip.ip) || ' unlinked from resource ' ||
            COALESCE(resource.name, 'NULL') || ' on zone ' || domain.name);
      END IF;
      RETURN OLD;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when data is added to a DNS resource. Increments the
# zone serial and notifies that DNS needs to be updated.
DNS_DNSDATA_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_dnsdata_insert()
    RETURNS trigger as $$
    DECLARE
      resource maasserver_dnsresource;
      domain maasserver_domain;
    BEGIN
      SELECT maasserver_dnsresource.* INTO resource
      FROM maasserver_dnsresource
      WHERE maasserver_dnsresource.id = NEW.dnsresource_id;
      SELECT maasserver_domain.* INTO domain
      FROM maasserver_domain
      WHERE maasserver_domain.id = resource.domain_id;
      PERFORM sys_dns_publish_update(
        'added ' || NEW.rrtype || ' to resource ' || resource.name ||
        ' on zone ' || domain.name);
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when data is update for a DNS resource. Increments the
# zone serial and notifies that DNS needs to be updated.
DNS_DNSDATA_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_dnsdata_update()
    RETURNS trigger as $$
    DECLARE
      resource maasserver_dnsresource;
      domain maasserver_domain;
    BEGIN
      SELECT maasserver_dnsresource.* INTO resource
      FROM maasserver_dnsresource
      WHERE maasserver_dnsresource.id = NEW.dnsresource_id;
      SELECT maasserver_domain.* INTO domain
      FROM maasserver_domain
      WHERE maasserver_domain.id = resource.domain_id;
      PERFORM sys_dns_publish_update(
        'updated ' || NEW.rrtype || ' in resource ' || resource.name ||
        ' on zone ' || domain.name);
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when data is removed from a DNS resource. Increments the
# zone serial and notifies that DNS needs to be updated.
DNS_DNSDATA_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_dns_dnsdata_delete()
    RETURNS trigger as $$
    DECLARE
      resource maasserver_dnsresource;
      domain maasserver_domain;
    BEGIN
      SELECT maasserver_dnsresource.* INTO resource
      FROM maasserver_dnsresource
      WHERE maasserver_dnsresource.id = OLD.dnsresource_id;
      SELECT maasserver_domain.* INTO domain
      FROM maasserver_domain
      WHERE maasserver_domain.id = resource.domain_id;
      PERFORM sys_dns_publish_update(
        'removed ' || OLD.rrtype || ' from resource ' || resource.name ||
        ' on zone ' || domain.name);
      RETURN OLD;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a subnet is updated. Increments notifies that proxy needs to
# be updated. Only watches changes on the cidr and allow_proxy.
PROXY_SUBNET_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_proxy_subnet_update()
    RETURNS trigger as $$
    BEGIN
      IF OLD.cidr != NEW.cidr OR OLD.allow_proxy != NEW.allow_proxy THEN
        PERFORM pg_notify('sys_proxy', '');
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when the proxy settings are updated.
PEER_PROXY_CONFIG_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_proxy_config_use_peer_proxy_insert()
    RETURNS trigger as $$
    BEGIN
      IF (NEW.name = 'enable_proxy' OR
          NEW.name = 'maas_proxy_port' OR
          NEW.name = 'use_peer_proxy' OR
          NEW.name = 'http_proxy' OR
          NEW.name = 'prefer_v4_proxy') THEN
        PERFORM pg_notify('sys_proxy', '');
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when the proxy settings are updated.
PEER_PROXY_CONFIG_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_proxy_config_use_peer_proxy_update()
    RETURNS trigger as $$
    BEGIN
      IF (NEW.name = 'enable_proxy' OR
          NEW.name = 'maas_proxy_port' OR
          NEW.name = 'use_peer_proxy' OR
          NEW.name = 'http_proxy' OR
          NEW.name = 'prefer_v4_proxy') THEN
        PERFORM pg_notify('sys_proxy', '');
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when RBAC need to be synced. In essense this means on
# insert into maasserver_rbacsync.
RBAC_SYNC = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_rbac_sync()
    RETURNS trigger AS $$
    BEGIN
      PERFORM pg_notify('sys_rbac', '');
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Procedure to mark RBAC as needing a sync.
RBAC_SYNC_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_rbac_sync_update(
      reason text,
      action text DEFAULT 'full',
      resource_type text DEFAULT '',
      resource_id int DEFAULT NULL,
      resource_name text DEFAULT '')
    RETURNS void as $$
    BEGIN
      INSERT INTO maasserver_rbacsync
        (created, source, action, resource_type, resource_id, resource_name)
      VALUES (
        now(), substring(reason FOR 255),
        action, resource_type, resource_id, resource_name);
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a new resource pool is added. Notifies that RBAC needs
# to be synced.
RBAC_RPOOL_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_rbac_rpool_insert()
    RETURNS trigger as $$
    BEGIN
      PERFORM sys_rbac_sync_update(
        'added resource pool ' || NEW.name,
        'add', 'resource-pool', NEW.id, NEW.name);
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a resource pool is updated. Notifies that RBAC needs
# to be synced. Only watches name.
RBAC_RPOOL_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_rbac_rpool_update()
    RETURNS trigger as $$
    DECLARE
      changes text[];
    BEGIN
      IF OLD.name != NEW.name THEN
        PERFORM sys_rbac_sync_update(
          'renamed resource pool ' || OLD.name || ' to ' || NEW.name,
          'update', 'resource-pool', OLD.id, NEW.name);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when a resource pool is deleted. Notifies that RBAC needs
# to be synced.
RBAC_RPOOL_DELETE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_rbac_rpool_delete()
    RETURNS trigger as $$
    BEGIN
      PERFORM sys_rbac_sync_update(
        'removed resource pool ' || OLD.name,
        'remove', 'resource-pool', OLD.id, OLD.name);
      RETURN OLD;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when the Candid/RBAC settings are inserted. Notifies that RBAC
# needs to be synced.
RBAC_CONFIG_INSERT = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_rbac_config_insert()
    RETURNS trigger as $$
    BEGIN
      IF (NEW.name = 'external_auth_url' OR
          NEW.name = 'external_auth_user' OR
          NEW.name = 'external_auth_key' OR
          NEW.name = 'rbac_url') THEN
        PERFORM sys_rbac_sync_update(
          'configuration ' || NEW.name || ' set to ' ||
          COALESCE(NEW.value, 'NULL'));
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


# Triggered when the Candid/RBAC settings are updated. Notifies that RBAC
# needs to be synced. The external_auth_* keys are included that way if
# the agent account that MAAS uses to update RBAC is updated in MAAS, MAAS will
# ensure that a full sync of data is performed.
RBAC_CONFIG_UPDATE = dedent(
    """\
    CREATE OR REPLACE FUNCTION sys_rbac_config_update()
    RETURNS trigger as $$
    BEGIN
      IF (OLD.value != NEW.value AND (
          NEW.name = 'external_auth_url' OR
          NEW.name = 'external_auth_user' OR
          NEW.name = 'external_auth_key' OR
          NEW.name = 'rbac_url')) THEN
        PERFORM sys_rbac_sync_update(
          'configuration ' || NEW.name || ' changed to ' || NEW.value);
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """
)


def render_sys_proxy_procedure(proc_name, on_delete=False):
    """Render a database procedure with name `proc_name` that notifies that a
    proxy update is needed.

    :param proc_name: Name of the procedure.
    :param on_delete: True when procedure will be used as a delete trigger.
    """
    return dedent(
        """\
        CREATE OR REPLACE FUNCTION %s() RETURNS trigger AS $$
        BEGIN
          PERFORM pg_notify('sys_proxy', '');
          RETURN %s;
        END;
        $$ LANGUAGE plpgsql;
        """
        % (proc_name, "NEW" if not on_delete else "OLD")
    )


@transactional
def register_system_triggers():
    """Register all system triggers into the database."""
    # Core
    register_procedure(CORE_GET_MANAGING_COUNT)
    register_procedure(CORE_GET_NUMBER_OF_CONN)
    register_procedure(CORE_GET_NUMBER_OF_PROCESSES)
    register_procedure(CORE_PICK_NEW_REGION)
    register_procedure(CORE_SET_NEW_REGION)

    # RegionRackRPCConnection
    register_procedure(CORE_REGIONRACKRPCONNECTION_INSERT)
    register_trigger(
        "maasserver_regionrackrpcconnection", "sys_core_rpc_insert", "insert"
    )
    register_procedure(CORE_REGIONRACKRPCONNECTION_DELETE)
    register_trigger(
        "maasserver_regionrackrpcconnection", "sys_core_rpc_delete", "delete"
    )

    # DHCP
    register_procedure(DHCP_ALERT)

    # - VLAN
    register_procedure(DHCP_VLAN_UPDATE)
    register_trigger("maasserver_vlan", "sys_dhcp_vlan_update", "update")

    # - Subnet
    register_procedure(DHCP_SUBNET_UPDATE)
    register_trigger("maasserver_subnet", "sys_dhcp_subnet_update", "update")
    register_procedure(DHCP_SUBNET_DELETE)
    register_trigger("maasserver_subnet", "sys_dhcp_subnet_delete", "delete")

    # - IPRange
    register_procedure(DHCP_IPRANGE_INSERT)
    register_trigger("maasserver_iprange", "sys_dhcp_iprange_insert", "insert")
    register_procedure(DHCP_IPRANGE_UPDATE)
    register_trigger("maasserver_iprange", "sys_dhcp_iprange_update", "update")
    register_procedure(DHCP_IPRANGE_DELETE)
    register_trigger("maasserver_iprange", "sys_dhcp_iprange_delete", "delete")

    # - StaticIPAddress
    register_procedure(DHCP_STATICIPADDRESS_INSERT)
    register_trigger(
        "maasserver_staticipaddress",
        "sys_dhcp_staticipaddress_insert",
        "insert",
    )
    register_procedure(DHCP_STATICIPADDRESS_UPDATE)
    register_trigger(
        "maasserver_staticipaddress",
        "sys_dhcp_staticipaddress_update",
        "update",
    )
    register_procedure(DHCP_STATICIPADDRESS_DELETE)
    register_trigger(
        "maasserver_staticipaddress",
        "sys_dhcp_staticipaddress_delete",
        "delete",
    )

    # - Interface
    register_procedure(DHCP_INTERFACE_UPDATE)
    register_trigger(
        "maasserver_interface", "sys_dhcp_interface_update", "update"
    )

    # - Node
    register_procedure(DHCP_NODE_UPDATE)
    register_trigger("maasserver_node", "sys_dhcp_node_update", "update")

    # - DHCPSnippet
    register_procedure(DHCP_UPDATE_ALL_VLANS)
    register_procedure(DHCP_SNIPPET_UPDATE_SUBNET)
    register_procedure(DHCP_SNIPPET_UPDATE_NODE)
    register_procedure(DHCP_SNIPPET_UPDATE_VALUE)

    register_procedure(DHCP_SNIPPET_INSERT)
    register_trigger(
        "maasserver_dhcpsnippet", "sys_dhcp_snippet_insert", "insert"
    )
    register_procedure(DHCP_SNIPPET_UPDATE)
    register_trigger(
        "maasserver_dhcpsnippet", "sys_dhcp_snippet_update", "update"
    )
    register_procedure(DHCP_SNIPPET_DELETE)
    register_trigger(
        "maasserver_dhcpsnippet", "sys_dhcp_snippet_delete", "delete"
    )

    # - Config/ntp_servers (and ntp_external_only)
    register_procedure(DHCP_CONFIG_NTP_SERVERS_INSERT)
    register_trigger(
        "maasserver_config", "sys_dhcp_config_ntp_servers_insert", "insert"
    )
    register_procedure(DHCP_CONFIG_NTP_SERVERS_UPDATE)
    register_trigger(
        "maasserver_config", "sys_dhcp_config_ntp_servers_update", "update"
    )
    register_procedure(DHCP_CONFIG_NTP_SERVERS_DELETE)
    register_trigger(
        "maasserver_config", "sys_dhcp_config_ntp_servers_delete", "delete"
    )

    # DNS
    # The zone serial is used in the 'sys_dns' triggers. Ensure that it exists
    # before creating the triggers.
    zone_serial.create_if_not_exists()

    # - DNSPublication
    register_procedure(DNS_PUBLISH)
    register_trigger("maasserver_dnspublication", "sys_dns_publish", "insert")
    register_procedure(DNS_PUBLISH_UPDATE)

    # - Domain
    register_procedure(DNS_DOMAIN_INSERT)
    register_trigger("maasserver_domain", "sys_dns_domain_insert", "insert")
    register_procedure(DNS_DOMAIN_UPDATE)
    register_trigger("maasserver_domain", "sys_dns_domain_update", "update")
    register_procedure(DNS_DOMAIN_DELETE)
    register_trigger("maasserver_domain", "sys_dns_domain_delete", "delete")

    # - StaticIPAddress
    register_procedure(DNS_STATICIPADDRESS_UPDATE)
    register_trigger(
        "maasserver_staticipaddress",
        "sys_dns_staticipaddress_update",
        "update",
    )

    # - Interface -> StaticIPAddress
    register_procedure(DNS_NIC_IP_LINK)
    register_trigger(
        "maasserver_interface_ip_addresses", "sys_dns_nic_ip_link", "insert"
    )
    register_procedure(DNS_NIC_IP_UNLINK)
    register_trigger(
        "maasserver_interface_ip_addresses", "sys_dns_nic_ip_unlink", "delete"
    )

    # - DNSResource
    register_procedure(DNS_DNSRESOURCE_INSERT)
    register_trigger(
        "maasserver_dnsresource", "sys_dns_dnsresource_insert", "insert"
    )
    register_procedure(DNS_DNSRESOURCE_UPDATE)
    register_trigger(
        "maasserver_dnsresource", "sys_dns_dnsresource_update", "update"
    )
    register_procedure(DNS_DNSRESOURCE_DELETE)
    register_trigger(
        "maasserver_dnsresource", "sys_dns_dnsresource_delete", "delete"
    )

    # - DNSResource -> StaticIPAddress
    register_procedure(DNS_DNSRESOURCE_IP_LINK)
    register_trigger(
        "maasserver_dnsresource_ip_addresses",
        "sys_dns_dnsresource_ip_link",
        "insert",
    )
    register_procedure(DNS_DNSRESOURCE_IP_UNLINK)
    register_trigger(
        "maasserver_dnsresource_ip_addresses",
        "sys_dns_dnsresource_ip_unlink",
        "delete",
    )

    # - DNSData
    register_procedure(DNS_DNSDATA_INSERT)
    register_trigger("maasserver_dnsdata", "sys_dns_dnsdata_insert", "insert")
    register_procedure(DNS_DNSDATA_UPDATE)
    register_trigger("maasserver_dnsdata", "sys_dns_dnsdata_update", "update")
    register_procedure(DNS_DNSDATA_DELETE)
    register_trigger("maasserver_dnsdata", "sys_dns_dnsdata_delete", "delete")

    # - Subnet
    register_procedure(DNS_SUBNET_INSERT)
    register_trigger("maasserver_subnet", "sys_dns_subnet_insert", "insert")
    register_procedure(DNS_SUBNET_UPDATE)
    register_trigger("maasserver_subnet", "sys_dns_subnet_update", "update")
    register_procedure(DNS_SUBNET_DELETE)
    register_trigger("maasserver_subnet", "sys_dns_subnet_delete", "delete")

    # - Node
    register_procedure(DNS_NODE_UPDATE)
    register_trigger("maasserver_node", "sys_dns_node_update", "update")
    register_procedure(DNS_NODE_DELETE)
    register_trigger("maasserver_node", "sys_dns_node_delete", "delete")

    # - Interface
    register_procedure(DNS_INTERFACE_UPDATE)
    register_trigger(
        "maasserver_interface", "sys_dns_interface_update", "update"
    )

    # - Config
    register_procedure(DNS_CONFIG_INSERT)
    register_procedure(DNS_CONFIG_UPDATE)
    register_trigger("maasserver_config", "sys_dns_config_insert", "insert")
    register_trigger("maasserver_config", "sys_dns_config_update", "update")

    # Proxy

    # - Subnet
    register_procedure(render_sys_proxy_procedure("sys_proxy_subnet_insert"))
    register_trigger("maasserver_subnet", "sys_proxy_subnet_insert", "insert")
    register_procedure(PROXY_SUBNET_UPDATE)
    register_trigger("maasserver_subnet", "sys_proxy_subnet_update", "update")
    register_procedure(
        render_sys_proxy_procedure("sys_proxy_subnet_delete", on_delete=True)
    )
    register_trigger("maasserver_subnet", "sys_proxy_subnet_delete", "delete")

    # - Config/http_proxy (when use_peer_proxy)
    register_procedure(PEER_PROXY_CONFIG_INSERT)
    register_trigger(
        "maasserver_config", "sys_proxy_config_use_peer_proxy_insert", "insert"
    )
    register_procedure(PEER_PROXY_CONFIG_UPDATE)
    register_trigger(
        "maasserver_config", "sys_proxy_config_use_peer_proxy_update", "update"
    )

    # - RBACSync
    register_procedure(RBAC_SYNC)
    register_trigger("maasserver_rbacsync", "sys_rbac_sync", "insert")
    register_procedure(RBAC_SYNC_UPDATE)

    # - ResourcePool
    register_procedure(RBAC_RPOOL_INSERT)
    register_trigger(
        "maasserver_resourcepool", "sys_rbac_rpool_insert", "insert"
    )
    register_procedure(RBAC_RPOOL_UPDATE)
    register_trigger(
        "maasserver_resourcepool", "sys_rbac_rpool_update", "update"
    )
    register_procedure(RBAC_RPOOL_DELETE)
    register_trigger(
        "maasserver_resourcepool", "sys_rbac_rpool_delete", "delete"
    )

    # - Config (Candid/RBAC)
    register_procedure(RBAC_CONFIG_INSERT)
    register_trigger("maasserver_config", "sys_rbac_config_insert", "insert")
    register_procedure(RBAC_CONFIG_UPDATE)
    register_trigger("maasserver_config", "sys_rbac_config_update", "update")
