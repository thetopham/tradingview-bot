import unittest

from position_management import decide_pm_action


class TestPositionManagement(unittest.TestCase):
    def setUp(self):
        self.base_position = {
            "current_pnl": 0,
            "duration_minutes": 0,
            "side": "LONG",
        }
        self.base_account = {"can_trade": True}
        self.cfg = {
            "PM_CUT_LOSS": -20.0,
            "PM_OPPOSITE_PERSIST_K": 2,
            "PM_OPPOSITE_MIN_PNL": -5.0,
            "PM_TIME_STOP_MINUTES": 20,
            "PM_TIME_STOP_PNL_BAND": 5.0,
        }

    def test_can_trade_false_flats(self):
        account_state = {"can_trade": False}
        action, reason = decide_pm_action(self.base_position, "HOLD", account_state, 0, self.cfg)
        self.assertEqual(action, "FLAT")
        self.assertEqual(reason, "RISK_CAN_TRADE_FALSE")

    def test_cut_loser_flats(self):
        position_state = {**self.base_position, "current_pnl": -25}
        action, reason = decide_pm_action(position_state, "HOLD", self.base_account, 0, self.cfg)
        self.assertEqual(action, "FLAT")
        self.assertEqual(reason, "CUT_LOSER")

    def test_opposite_persistence_flats_only_if_pnl_negative_threshold_met(self):
        position_state = {**self.base_position, "current_pnl": -6}
        action, reason = decide_pm_action(position_state, "SELL", self.base_account, 2, self.cfg)
        self.assertEqual(action, "FLAT")
        self.assertEqual(reason, "OPP_PERSIST")

        # Should hold if pnl not below threshold despite persistence
        position_state_positive = {**self.base_position, "current_pnl": 1}
        action, reason = decide_pm_action(position_state_positive, "SELL", self.base_account, 3, self.cfg)
        self.assertEqual(action, "HOLD")
        self.assertEqual(reason, "HOLD")

    def test_time_stop_flats(self):
        position_state = {**self.base_position, "duration_minutes": 25, "current_pnl": 4}
        action, reason = decide_pm_action(position_state, "HOLD", self.base_account, 0, self.cfg)
        self.assertEqual(action, "FLAT")
        self.assertEqual(reason, "TIME_STOP")

    def test_default_hold(self):
        action, reason = decide_pm_action(self.base_position, "HOLD", self.base_account, 0, self.cfg)
        self.assertEqual(action, "HOLD")
        self.assertEqual(reason, "HOLD")


if __name__ == "__main__":
    unittest.main()
