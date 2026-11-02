# Site Health — core router (the one with DHCP, Wi-Fi access-list, uplinks).
# Review, replace HA_IP / IOT_IFACE / the password, then paste into a terminal.
# Nothing here reboots the router or touches existing rules.
#
# HA_IP      address of Home Assistant          (Beregovaya: 10.5.20.5)
# IOT_IFACE  interface HA sits behind            (Beregovaya: vlan20-IoT)
# MGMT_NET   subnet of radios / APs to be polled (Beregovaya: 10.5.10.0/24)

:local haIp "10.5.20.5"
:local iotIface "vlan20-IoT"
:local mgmtNet "10.5.10.0/24"

# 1. HTTPS for the REST API, self-signed, reachable only from HA.
:if ([:len [/certificate find name=site-health-rest]] = 0) do={
  /certificate add name=site-health-rest common-name=site-health-rest days-valid=3650 \
    key-usage=digital-signature,key-encipherment,tls-server
  /certificate sign site-health-rest
}
/ip service set www-ssl certificate=site-health-rest disabled=no address=($haIp . "/32")

# 2. Group: read everything, write only what HA needs (Wi-Fi access-list for the
#    family list). No ssh/winbox/ftp/reboot/policy/sensitive (no passwords/keys).
:if ([:len [/user group find name=ha-infra]] = 0) do={
  /user group add name=ha-infra comment="Site Health (Home Assistant)" \
    policy=read,write,api,rest-api,!local,!telnet,!ssh,!ftp,!reboot,!policy,!test,!winbox,!password,!web,!sniff,!sensitive,!romon
}
# 3. User: choose your own password; it is entered once in the HA config flow.
#    /user add name=ha-infra group=ha-infra address=10.5.20.5/32 password="..."

# 4. Firewall: HA -> router REST, HA -> REST of radios/APs in MGMT.
:if ([:len [/ip firewall filter find comment="input-ha-rest"]] = 0) do={
  /ip firewall filter add chain=input action=accept protocol=tcp dst-port=443 \
    src-address=$haIp in-interface=$iotIface comment="input-ha-rest" \
    place-before=[find comment="input-drop-all"]
}
:if ([:len [/ip firewall filter find comment="fwd-ha-mgmt-rest"]] = 0) do={
  /ip firewall filter add chain=forward action=accept protocol=tcp dst-port=443 \
    src-address=$haIp dst-address=$mgmtNet comment="fwd-ha-mgmt-rest" \
    place-before=[find comment="fwd-iot-drop-lan"]
}

# 5. Uplink probes (no scripts): HA reads status, rtt-avg and loss-percent.
:if ([:len [/tool netwatch find name="wan-8.8.8.8"]] = 0) do={
  /tool netwatch add name="wan-8.8.8.8" host=8.8.8.8 type=icmp interval=30s comment="Internet via 8.8.8.8"
}
:if ([:len [/tool netwatch find name="wan-1.1.1.1"]] = 0) do={
  /tool netwatch add name="wan-1.1.1.1" host=1.1.1.1 type=icmp interval=30s comment="Internet via 1.1.1.1"
}
:if ([:len [/tool netwatch find name="srvprm-wg"]] = 0) do={
  /tool netwatch add name="srvprm-wg" host=10.255.0.1 type=icmp interval=30s comment="WireGuard to SRVPRM hub"
}
