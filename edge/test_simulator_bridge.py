"""Unit tests for the browser-to-Tier-1 payload adapter."""

import unittest

from edge.simulator_bridge import (
    PayloadError, Tier1ConfigResolver, evaluate_payload,
)


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

    def test_backend_config_overrides_request_overload_rating(self):
        requested_paths = []

        def backend_config(path):
            requested_paths.append(path)
            return {'config': {
                'max_inverter_power_W': 4000,
                'overload_fraction': 1.0,
            }}

        resolver = Tier1ConfigResolver(
            'http://backend.invalid', 'device.secret', transport=backend_config,
        )
        result = evaluate_payload({
            'inverter': {'ac_output_active_power_W': 1200},
            'breakers': [{
                'device_id': 'comfort-load',
                'priority_type': 'comfort',
                'switch': True,
                'cur_power_W': 500,
            }],
            'config': {
                'max_inverter_power_W': 1000,
                'overload_fraction': 1.05,
            },
        }, config_resolver=resolver)

        self.assertEqual(result['situation'], '')
        self.assertEqual(result['facts']['config']['max_inverter_power_W'], 4000)
        self.assertEqual(result['facts']['config']['overload_fraction'], 1.0)
        self.assertEqual(requested_paths, ['/api/kbs/edge/tier1-config/'])


if __name__ == "__main__":
    unittest.main(verbosity=2)
