import importlib.util
import sys
import unittest
from pathlib import Path


class DryRunSimulatedFillTests(unittest.TestCase):
    def _load_script_module(self):
        script_path = Path(__file__).resolve().parents[1] / "scripts" / "dry_run_simulated_fill.py"
        spec = importlib.util.spec_from_file_location("dry_run_simulated_fill", script_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_dry_run_simulated_fill_uses_legacy_payload_shape_without_live_broker_calls(self):
        module = self._load_script_module()

        result = module.run_dry_fill(output_path=None, log_level="WARNING")

        self.assertEqual(result["broker_calls"], ["/api/Trade/search"])
        self.assertEqual(len(result["supabase_inserts"]), 1)
        insert = result["supabase_inserts"][0]
        self.assertEqual(insert["url"], "http://supabase-dry-run.invalid/rest/v1/trade_results")

        payload = result["payload"]
        self.assertEqual(payload["strategy"], "sim_broker_dry_run")
        self.assertEqual(payload["signal"], "BUY")
        self.assertEqual(payload["symbol"], "CON.F.US.MES.SIM")
        self.assertEqual(payload["account"], "paper")
        self.assertEqual(payload["size"], 1)
        self.assertEqual(payload["total_pnl"], 42.5)
        self.assertEqual(payload["fees_total"], 2.4)
        self.assertEqual(payload["net_pnl"], 40.1)
        self.assertEqual(payload["entry_price"], 5000.0)
        self.assertEqual(payload["exit_price"], 5008.5)
        self.assertEqual(payload["entry_price_source"], "trade_fills_vwap")
        self.assertEqual(payload["exit_price_source"], "trade_fills_vwap")
        self.assertEqual(payload["order_id"], '["SIM-ENTRY-0001"]')
        self.assertEqual(len(payload["raw_trades"]), 2)
        self.assertEqual(payload["raw_trades"][0]["contractId"], "CON.F.US.MES.SIM")
        self.assertEqual(payload["raw_trades"][1]["profitAndLoss"], 42.5)


if __name__ == "__main__":
    unittest.main()
