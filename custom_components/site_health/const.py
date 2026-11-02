"""Constants for the Site Health integration."""

from __future__ import annotations

DOMAIN = "site_health"

# Main entry (the core router).
CONF_SITE_NAME = "site_name"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_TLS = "tls"
CONF_VERIFY_SSL = "verify_ssl"

CONF_HA_IP = "ha_ip"

# Options.
CONF_SCAN_INTERVAL = "scan_interval"
CONF_FAMILY_VLAN = "family_vlan"
CONF_ADMIN_VLAN = "admin_vlan"
CONF_WG_STALE = "wg_stale_seconds"
CONF_AUTO_CONFIGURE = "auto_configure"
DEFAULT_AUTO_CONFIGURE = True

DEFAULT_SCAN_INTERVAL = 30
DEFAULT_FAMILY_VLAN = 30
DEFAULT_ADMIN_VLAN = 99
# WireGuard sends keepalives every 25-30 s; three missed ones mean the
# tunnel is gone, not just idle.
DEFAULT_WG_STALE = 180

# Subentry types.
SUBENTRY_DEVICE = "device"
SUBENTRY_MODEM = "modem"
SUBENTRY_SWITCH = "switch"

HTTP_TIMEOUT = 10

# A client is considered present if the router saw it this recently.
CLIENT_ONLINE_SECONDS = 300

# Segment kinds. Keys are stable identifiers used by the panel and the
# websocket API; labels live in the frontend.
SEG_MGMT = "mgmt"
SEG_VIDEO = "video"
SEG_IOT = "iot"
SEG_FAMILY = "family"
SEG_GUEST = "guest"
SEG_ADMIN = "admin"
SEG_DMZ = "dmz"
SEG_SWITCHES = "switches"
SEG_OTHER = "other"

SEGMENT_ORDER = [
    SEG_FAMILY,
    SEG_GUEST,
    SEG_IOT,
    SEG_VIDEO,
    SEG_ADMIN,
    SEG_MGMT,
    SEG_SWITCHES,
    SEG_DMZ,
    SEG_OTHER,
]

# VLAN numbering shared by all SRVPRM sites (10.N.<vlan>.0/24).
SEGMENT_BY_VLAN: dict[int, str] = {
    1: SEG_SWITCHES,
    10: SEG_MGMT,
    11: SEG_VIDEO,
    12: SEG_VIDEO,
    20: SEG_IOT,
    30: SEG_FAMILY,
    40: SEG_GUEST,
    50: SEG_DMZ,
    99: SEG_ADMIN,
}

# Fallback when an interface carries no VLAN id: match on its name.
SEGMENT_BY_NAME_HINT: list[tuple[str, str]] = [
    ("guest", SEG_GUEST),
    ("famil", SEG_FAMILY),
    ("user", SEG_FAMILY),
    ("iot", SEG_IOT),
    ("smart", SEG_IOT),
    ("cam", SEG_VIDEO),
    ("nvr", SEG_VIDEO),
    ("vid", SEG_VIDEO),
    ("admin", SEG_ADMIN),
    ("mgmt", SEG_MGMT),
    ("manage", SEG_MGMT),
    ("dmz", SEG_DMZ),
]

SERVICE_ADD_FAMILY = "add_family_device"
SERVICE_REMOVE_FAMILY = "remove_family_device"
SERVICE_REFRESH = "refresh"
SERVICE_SET_SEGMENT = "set_device_segment"
SERVICE_MODEM_SIM = "modem_select_sim"

# Wi-Fi placements the integration may set: guest = no access-list entry.
PLACE_GUEST = "guest"
PLACE_FAMILY = "family"
PLACE_ADMIN = "admin"

ATTR_MAC = "mac"
ATTR_NAME = "name"

PANEL_URL = "site-health"
PANEL_STATIC = "/site_health_static"
PANEL_ELEMENT = "site-health-panel"

SIGNAL_UPDATED = f"{DOMAIN}_updated"
