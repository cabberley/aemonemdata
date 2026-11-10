"""Explicitly opt in to tests that contact the public AEMO dashboard."""

import os
import unittest
from datetime import timedelta

from aemonemdata import AemoNemData, REGIONS


@unittest.skipUnless(os.environ.get("AEMONEM_LIVE_TESTS") == "1", "Live API tests are opt-in")
class LiveDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_regions_current_data(self):
        client = AemoNemData()
        result = await client.get_aemo_data(list(REGIONS))
        self.assertEqual(set(result), {"current_price_window", "current_30min_forecast"})
        self.assertEqual(set(result["current_30min_forecast"]), set(REGIONS.values()))
        for region in REGIONS.values():
            with self.subTest(region=region):
                data = result["current_30min_forecast"][region]
                periods = result["current_price_window"][region]
                self.assertEqual(set(periods), {f"period_{i}" for i in range(1, 7)})
                self.assertTrue(0 <= data["periods_of_current_30min"] <= 6)
                self.assertIsInstance(data["current_5min_period_price"], (int, float))
                self.assertIsInstance(data["current_cumulative_price"], int)
                self.assertGreater(data["cumulative_price_threshold"], 0)
                self.assertEqual(data["settlement_date"].utcoffset(), timedelta(hours=10))
                self.assertTrue(data["forecast"])
                for forecast in data["forecast"]:
                    self.assertEqual(forecast["end_time"] - forecast["start_time"],
                                     timedelta(minutes=30))
                for flow in data["interconnector_flows"]:
                    self.assertEqual(data[f"flow_{flow['name']}_value"], flow["value"])
                for period in periods.values():
                    if period["period_type"] is None:
                        self.assertIsNone(period["price_kw"])
        self.assertIsNone(client._session)

    async def test_actual_and_forecast_price_route(self):
        actual, forecast = await AemoNemData().get_data("NSW1")
        self.assertTrue(actual)
        self.assertTrue(forecast)
        for records, kind, minutes in ((actual, "ACTUAL", 5), (forecast, "FORECAST", 30)):
            for record in records:
                self.assertEqual(record["REGIONID"], "NSW1")
                self.assertEqual(record["PERIODTYPE"], kind)
                self.assertEqual(record["SETTLEMENTDATE"] - record["PERIODSTARTDATE"],
                                 timedelta(minutes=minutes))
                self.assertEqual(record["SPOTPRICEPERKW"], round(record["RRP"] / 1000, 4))


if __name__ == "__main__":
    unittest.main()
