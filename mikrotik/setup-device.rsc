# Site Health — any further MikroTik (radio base, bridge station, AP).
# Read-only: HA only polls it. Replace the password, then paste.

:local haIp "10.5.20.5"

:if ([:len [/certificate find name=site-health-rest]] = 0) do={
  /certificate add name=site-health-rest common-name=site-health-rest days-valid=3650 \
    key-usage=digital-signature,key-encipherment,tls-server
  /certificate sign site-health-rest
}
/ip service set www-ssl certificate=site-health-rest disabled=no address=($haIp . "/32")

:if ([:len [/user group find name=ha-infra]] = 0) do={
  /user group add name=ha-infra comment="Site Health (Home Assistant)" \
    policy=read,api,rest-api,!local,!telnet,!ssh,!ftp,!reboot,!write,!policy,!test,!winbox,!password,!web,!sniff,!sensitive,!romon
}
#    /user add name=ha-infra group=ha-infra address=10.5.20.5/32 password="..."
