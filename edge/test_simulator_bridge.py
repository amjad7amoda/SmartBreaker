"""Unit tests for the browser-to-Tier-1 payload adapter."""

import unittest

from edge.simulator_bridge import PayloadError, evaluate_payload


class SimulatorBridgePayloadTests(unittest.TestCase):

    def test_payload_runs_the_real_tier1_engine(self):
        result = evaluate_payload({
            "inverter": {
                "ac_output_active_power_W": 1200,
                "heatsink_temp_C": 80,
            },
            "breakers": [
                {
                    "device_id": "server",
                    "priority_type": "mandatory",
                    "switch": True,
                    "cur_power_W": 300,
                },
                {
                    "device_id": "tv",
                    "priority_type": "comfort",
                    "switch": True,
                    "cur_power_W": 900,
                },
            ],
            "config": {"heatsink_temp_limit_C": 70},
        })

        self.assertEqual(result["situation"], "inverter_overheat")
        self.assertEqual([c["device_id"] for c in result["commands"]], ["tv"])
        self.assertEqual(result["engine"], "edge.tier1_kbs.evaluate")
        self.assertEqual(result["facts"]["inverter"]["heatsink_temp_C"], 80)

    def test_unknown_fields_are_rejected(self):
        with self.assertRaises(PayloadError):
            evaluate_payload({
                "inverter": {"made_up_field": 1},
                "breakers": [],
            })

    def test_breakers_must_be_a_list(self):
        with self.assertRaises(PayloadError):
            evaluate_payload({"inverter": {}, "breakers": {}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
