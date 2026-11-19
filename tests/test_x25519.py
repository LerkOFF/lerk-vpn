import unittest

from lerk_vpn.x25519 import new_reality_keypair, public_for, x25519


class X25519Tests(unittest.TestCase):
    def test_rfc7748_vector(self):
        k = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
        u = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
        self.assertEqual(x25519(k, u).hex(), "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")

    def test_rfc7748_public_key(self):
        k = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
        nine = (9).to_bytes(32, "little")
        self.assertEqual(x25519(k, nine).hex(), "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")

    def test_matches_xray_output(self):
        # пара, выпущенная командой `xray x25519` специально для теста
        private = "qBv2APZKLLp4Hwa3aZMQP3XdABaXHd35ho5o7eT38mM"
        self.assertEqual(public_for(private), "yZKrDgWW1q2kMUbJdwrAGeb5E0sZtsCqnzmHoZwieXU")

    def test_new_pair_is_consistent(self):
        private, public = new_reality_keypair()
        self.assertEqual(public_for(private), public)
        self.assertEqual(len(private), 43)
