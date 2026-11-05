"""Admin VPN through a provider hub: wanted list, hub report, hub script."""

from __future__ import annotations

from conftest import load_module

hub = load_module("hub_vpn")
vpn = load_module("admin_vpn")


def test_state_roundtrip_and_report():
    st = hub.GatewayState()
    _priv, pub = vpn.gen_keypair()
    ip = st.free_address("10.250.5.0/24")
    assert ip == "10.250.5.254"
    st.add(key=pub, address=ip, name="Phone")
    assert st.free_address("10.250.5.0/24") == "10.250.5.253"
    assert st.wanted() == {"peers": [{"k": pub, "a": ip, "n": "Phone"}]}
    assert st.view()[0]["applied"] is False
    _p, hub_key = vpn.gen_keypair()
    st.report(
        {
            "pub": hub_key,
            "port": "13235",
            "peers": [{"k": pub, "hs": "12s"}, {"k": "junk"}],
        },
        now=100.0,
    )
    assert st.hub_key == hub_key and st.hub_port == 13235 and st.seen == 100.0
    assert st.handshakes == {pub: "12s"}
    assert st.view()[0]["applied"] is True
    st2 = hub.GatewayState.from_dict(st.as_dict())
    assert st2.as_dict() == st.as_dict()
    assert st2.remove(pub) and not st2.remove(pub)


def test_report_ignores_bad_key():
    st = hub.GatewayState(hub_key="")
    st.report({"pub": "not-a-key", "port": "x"})
    assert st.hub_key == "" and st.hub_port == 0


def test_hub_script_limits_clients_to_their_site():
    s = hub.hub_script(
        site="Beregovaya3",
        ha_url="http://10.5.20.5:80",
        token="TOK",
        client_net="10.250.5.0/24",
        site_nets=["10.5.0.0/16"],
        src_address="10.255.0.1",
    )
    assert "http://10.5.20.5:80/api/site_health/hub/TOK" in s
    assert "src-address=10.255.0.1" in s
    assert "list=site-vpn-beregovaya3 address=10.5.0.0/16" in s
    assert "src-address=10.250.5.0/24 dst-address-list=site-vpn-beregovaya3" in s
    assert "action=drop in-interface=WG-Sites" in s
    assert "(\\$a in \\$net)" in s  # the hub checks every address itself
    # The reconcile source is one quoted line with its $variables escaped.
    src_line = next(
        line for line in s.splitlines() if line.startswith("/system script add")
    )
    assert "\n" not in src_line and "\\$url" in src_line
    # Accept for this site is inserted last, so it ends up above the drops.
    order = [i for i, line in enumerate(s.splitlines()) if "chain=forward" in line]
    assert "own site only" in s.splitlines()[order[-1]]
