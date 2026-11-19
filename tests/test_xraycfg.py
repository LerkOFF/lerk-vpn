import unittest
from urllib.parse import parse_qs, urlparse

from lerk_vpn.xraycfg import apply_order, build_node_config, client_links, users_at

from .helpers import make


def outbound(cfg, tag):
    return next(o for o in cfg["outbounds"] if o["tag"] == tag)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.topo, self.clients, self.state = make()

    def test_entry_has_only_enabled_clients(self):
        emails = [e for _, e, _ in users_at(self.topo, self.clients, self.state, "fi1")]
        self.assertIn("c.main.ann", emails)
        self.assertIn("c.triple.ann", emails)
        self.assertNotIn("c.main.off", emails)

    def test_relay_has_hop_user_and_forwards(self):
        cfg = build_node_config(self.topo, self.clients, self.state, "de1")
        emails = [c["email"] for c in cfg["inbounds"][0]["settings"]["clients"]]
        self.assertIn("h.triple.0", emails)
        self.assertIn("c.solo.bob", emails)
        out = outbound(cfg, "to.triple")
        target = out["settings"]["vnext"][0]
        self.assertEqual(target["address"], "203.0.113.12")
        self.assertEqual(target["port"], 8443)
        self.assertEqual(target["users"][0]["id"], self.state.hop_uuid("triple", 1))
        self.assertEqual(out["streamSettings"]["realitySettings"]["publicKey"], self.state.node("fr1")["public_key"])

    def test_exit_routes_to_direct_and_blocks_private(self):
        cfg = build_node_config(self.topo, self.clients, self.state, "fr1")
        rules = cfg["routing"]["rules"]
        self.assertEqual(rules[0]["outboundTag"], "api")
        self.assertEqual(rules[1]["outboundTag"], "block")
        self.assertEqual(rules[1]["ip"], ["geoip:private"])
        by_tag = {tuple(r.get("user", [])): r["outboundTag"] for r in rules if "user" in r}
        self.assertEqual(by_tag[("h.main.0",)], "direct")
        self.assertEqual(by_tag[("h.triple.1",)], "direct")
        self.assertTrue(cfg["inbounds"][0]["sniffing"]["routeOnly"])
        self.assertEqual(cfg["inbounds"][0]["port"], 8443)

    def test_entry_not_exit_has_no_direct(self):
        cfg = build_node_config(self.topo, self.clients, self.state, "fi1")
        tags = [o["tag"] for o in cfg["outbounds"]]
        self.assertNotIn("direct", tags)
        self.assertEqual(set(tags), {"block", "to.main", "to.triple"})
        self.assertNotIn("sniffing", cfg["inbounds"][0])

    def test_first_outbound_is_block_for_unmatched(self):
        cfg = build_node_config(self.topo, self.clients, self.state, "de1")
        self.assertEqual(cfg["outbounds"][0]["tag"], "block")

    def test_single_node_chain_goes_direct(self):
        cfg = build_node_config(self.topo, self.clients, self.state, "de1")
        solo = next(r for r in cfg["routing"]["rules"] if r.get("user") == ["c.solo.bob"])
        self.assertEqual(solo["outboundTag"], "direct")

    def test_apply_order_exit_first(self):
        order = apply_order(self.topo)
        self.assertLess(order.index("fr1"), order.index("de1"))
        self.assertLess(order.index("de1"), order.index("fi1"))

    def test_link_fields(self):
        ann = self.clients[0]
        links = client_links(self.topo, self.state, ann)
        self.assertEqual(len(links), 2)
        url = urlparse(links[0])
        q = parse_qs(url.query)
        self.assertEqual(url.scheme, "vless")
        self.assertEqual(url.hostname, "203.0.113.10")
        self.assertEqual(url.port, 443)
        self.assertEqual(url.username, self.state.client_uuid("ann", "main"))
        self.assertEqual(q["security"], ["reality"])
        self.assertEqual(q["sni"], ["dl.google.com"])
        self.assertEqual(q["pbk"], [self.state.node("fi1")["public_key"]])
        self.assertEqual(q["flow"], ["xtls-rprx-vision"])
        self.assertTrue(links[0].endswith("#FI-FR-ann"))

    def test_state_is_stable_between_ensure_calls(self):
        before = dict(self.state.data["nodes"]["fi1"])
        self.state.ensure(self.topo, self.clients)
        self.assertEqual(before, self.state.data["nodes"]["fi1"])


class StatsTests(unittest.TestCase):
    def test_stats_api_is_local_only(self):
        topo, clients, state = make()
        cfg = build_node_config(topo, clients, state, "fi1")
        api = next(i for i in cfg["inbounds"] if i["tag"] == "api-in")
        self.assertEqual(api["listen"], "127.0.0.1")
        self.assertEqual(cfg["api"]["services"], ["StatsService"])
        self.assertTrue(cfg["policy"]["levels"]["0"]["statsUserDownlink"])
        self.assertEqual(cfg["routing"]["rules"][0]["inboundTag"], ["api-in"])
