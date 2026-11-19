import copy
import unittest

from lerk_vpn.model import SpecError, parse_topology

from .helpers import RAW


class ModelTests(unittest.TestCase):
    def raw(self):
        return copy.deepcopy(RAW)

    def test_ok(self):
        topo = parse_topology(self.raw())
        self.assertEqual(topo.nodes["fi1"].country, "FI")
        self.assertEqual(topo.nodes["fi1"].swap_gb, 2)
        self.assertEqual(topo.nodes["fr1"].port, 8443)

    def test_unknown_node_in_chain(self):
        r = self.raw()
        r["chains"]["main"]["path"] = ["fi1", "nope"]
        with self.assertRaises(SpecError):
            parse_topology(r)

    def test_duplicate_in_path(self):
        r = self.raw()
        r["chains"]["main"]["path"] = ["fi1", "fi1"]
        with self.assertRaises(SpecError):
            parse_topology(r)

    def test_same_host_twice(self):
        r = self.raw()
        r["nodes"]["de1"]["host"] = "203.0.113.10"
        with self.assertRaises(SpecError):
            parse_topology(r)

    def test_bad_names_and_values(self):
        for mutate in (
            lambda r: r["nodes"].update({"Bad Name": {"host": "1.2.3.4"}}),
            lambda r: r["nodes"]["fi1"].update({"host": "1.2.3.4; rm -rf /"}),
            lambda r: r["nodes"]["fi1"].update({"port": 70000}),
            lambda r: r["nodes"]["fi1"].update({"sni": "bad sni"}),
            lambda r: r["nodes"]["fi1"].update({"unknown": 1}),
            lambda r: r["nodes"]["fi1"].update({"bbr": "yes"}),
        ):
            r = self.raw()
            mutate(r)
            with self.assertRaises(SpecError):
                parse_topology(r)
