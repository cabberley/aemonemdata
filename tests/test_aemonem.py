"""Offline regression tests against a local HTTP dashboard."""

import asyncio
import json
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import ClientConnectionError, ClientSession, web
from aiohttp.test_utils import TestServer

from aemonemdata import AemoNemData, REGIONS
from aemonemdata.exceptions import AuthError, ClientError
from aemonemdata.utils import current_30min_window


START = datetime(2026, 11, 11, 8, tzinfo=timezone(timedelta(hours=10)))
FIELDS = {
    "current_5min_period_price", "current_30min_avg", "current_30min_forecast",
    "current_30min_estimated", "current_cumulative_price",
    "current_percent_cumulative_price", "administered_price_cap",
    "market_price_cap", "cumulative_price_threshold", "market_suspended_flag",
    "apc_flag", "periods_of_current_30min", "forecast", "total_demand",
    "scheduled_generation", "interconnector_flows", "semi_scheduled_generation",
    "net_interconnector_flows", "settlement_date_str", "settlement_date",
    "flow_NSW1-QLD1_name", "flow_NSW1-QLD1_value",
    "flow_NSW1-QLD1_export_limit", "flow_NSW1-QLD1_import_limit",
}


def envelope(data):
    return {"data": data, "errors": [], "warnings": [], "infos": [], "transactionId": "test"}


def payloads():
    cumulative, prices, summaries = [], [], []
    for region in REGIONS.values():
        for minutes, price, actual in (
            (-5, 900, 1), (5, 100, 1), (10, 200, 1), (15, -50, 1),
            (30, 400, 0), (60, 600, 0),
        ):
            date = (START + timedelta(minutes=minutes)).isoformat()
            cumulative.append({"dt": date, "r": region, "p": price, "cp": 123456, "a": actual})
            prices.append({
                "settlementDate": date, "regionId": region, "rrp": price,
                "periodType": "ACTUAL" if actual else "FORECAST",
                "totalDemand": 7000, "netInterchange": -100,
                "scheduledGeneration": 4000, "semiScheduledGeneration": 3000, "apcFlag": 0,
            })
        summaries.append({
            "settlementDate": (START + timedelta(minutes=15)).isoformat(),
            "regionId": region, "price": -50, "priceStatus": "FIRM",
            "apcFlag": int(region == "SA1"), "marketsuspendedFlag": int(region == "TAS1"),
            "totalDemand": 7000, "netInterchange": -100,
            "scheduledGeneration": 4000, "semischeduledGeneration": 3000,
            "interconnectorFlows": json.dumps([{
                "name": "NSW1-QLD1", "value": -100, "exportlimit": 500, "importlimit": -600,
            }]),
        })
    return {
        "cumulativePrice": envelope({"items": list(reversed(cumulative))}),
        "priceAndDemand": envelope({"items": list(reversed(prices))}),
        "marketPriceLimits": envelope({"items": [
            {"key": "AdministeredPriceCap", "value": 600},
            {"key": "MarketPriceCap", "value": 23200},
            {"key": "CumulativePriceThreshold", "value": 2000000},
        ]}),
        "elecSummary": envelope({
            "summary": summaries,
            "prices": [{"regionId": region, "rrp": -50} for region in REGIONS.values()],
        }),
    }


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.payloads = payloads()
        self.config = {"xApiKey": "test-key"}
        self.required_key = "test-key"
        self.requests = []
        self.auth_failures = 0
        self.auth_status = 401
        app = web.Application()
        app.router.add_get("/config.json", self.handle_config)
        app.router.add_get("/NEM/v1/PWS/NEMDashboard/{endpoint}", self.handle_api)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.session = ClientSession()
        self.addAsyncCleanup(self.session.close)
        self.client = AemoNemData(self.session)
        base = str(self.server.make_url("/")).rstrip("/")
        base_patch = patch("aemonemdata.aemonem.BaseUrl", SimpleNamespace(API=base))
        base_patch.start()
        self.addCleanup(base_patch.stop)
        window_patch = patch(
            "aemonemdata.aemonem.current_30min_window",
            return_value=(START, START + timedelta(minutes=30)),
        )
        window_patch.start()
        self.addCleanup(window_patch.stop)

    async def handle_config(self, request):
        self.requests.append(("config", dict(request.headers), dict(request.query)))
        return web.json_response(self.config)

    async def handle_api(self, request):
        name = request.match_info["endpoint"]
        self.requests.append((name, dict(request.headers), dict(request.query)))
        if self.auth_failures:
            self.auth_failures -= 1
            return web.json_response({"errors": []}, status=self.auth_status)
        if request.headers.get("x-api-key") != self.required_key:
            return web.json_response({"errors": []}, status=401)
        data = self.payloads[name]
        if isinstance(data, web.Response):
            return data
        return web.json_response(data)

    def count(self, endpoint):
        return sum(name == endpoint for name, _, _ in self.requests)

    async def test_all_regions_output_and_calculations(self):
        result = await self.client.get_aemo_data(["NSW", "qld", "vic", "sa", "tas"])
        self.assertEqual(set(result), {"current_price_window", "current_30min_forecast"})
        self.assertEqual(set(result["current_30min_forecast"]), set(REGIONS.values()))
        for region in REGIONS.values():
            with self.subTest(region=region):
                data = result["current_30min_forecast"][region]
                self.assertEqual(set(data), FIELDS)
                expected = {
                    "current_5min_period_price": -0.05, "current_30min_avg": 0.0833,
                    "current_30min_forecast": 0.4, "current_30min_estimated": 0.2417,
                    "current_cumulative_price": 123456, "current_percent_cumulative_price": 6.17,
                    "administered_price_cap": 600, "market_price_cap": 23200,
                    "cumulative_price_threshold": 2000000, "periods_of_current_30min": 3,
                    "apc_flag": region == "SA1", "market_suspended_flag": region == "TAS1",
                    "total_demand": 7000, "scheduled_generation": 4000,
                    "semi_scheduled_generation": 3000, "net_interconnector_flows": -100,
                    "settlement_date_str": "2026-11-11T08:15:00",
                    "settlement_date": START + timedelta(minutes=15),
                    "flow_NSW1-QLD1_name": "NSW1-QLD1", "flow_NSW1-QLD1_value": -100,
                    "flow_NSW1-QLD1_export_limit": 500, "flow_NSW1-QLD1_import_limit": -600,
                    "interconnector_flows": [{
                        "name": "NSW1-QLD1", "value": -100,
                        "exportlimit": 500, "importlimit": -600,
                    }],
                    "forecast": [
                        {"start_time": START, "end_time": START + timedelta(minutes=30),
                         "price": 0.4},
                        {"start_time": START + timedelta(minutes=30),
                         "end_time": START + timedelta(minutes=60), "price": 0.6},
                    ],
                }
                self.assertEqual(data, expected)
                periods = result["current_price_window"][region]
                self.assertEqual(set(periods), {f"period_{i}" for i in range(1, 7)})
                self.assertEqual(periods["period_1"]["price_kw"], 0.1)
                self.assertEqual(periods["period_3"]["price_kw"], -0.05)
                self.assertEqual(periods["period_4"], {
                    "period_type": None, "settlement_date": None, "period_start_date": None,
                    "region_id": region, "price_mw": None, "price_kw": None,
                    "cumulative_price": None,
                })

    async def test_repeated_polling_caches_key_and_limits_without_duplicate_records(self):
        first = await self.client.get_aemo_data(["nsw"])
        self.assertEqual(first, await self.client.get_aemo_data(["nsw"]))
        self.assertEqual(self.count("config"), 1)
        self.assertEqual(self.count("marketPriceLimits"), 1)
        self.assertEqual(self.count("cumulativePrice"), 2)
        self.assertEqual(self.count("elecSummary"), 2)
        self.assertEqual(len(self.client._aemo_data_elec_nem_summary_prices), 5)
        self.assertFalse(self.session.closed)

    async def test_price_route_uses_get_30min_query_and_legacy_shape(self):
        actual, forecast = await self.client.get_data("NSW1")
        self.assertEqual((len(actual), len(forecast)), (4, 2))
        self.assertEqual(actual[-1]["RRP"], -50)
        self.assertEqual(actual[-1]["SPOTPRICEPERKW"], -0.05)
        self.assertEqual(actual[-1]["PERIODSTARTDATE"], START + timedelta(minutes=10))
        self.assertEqual(forecast[0]["PERIODSTARTDATE"], START)
        self.assertEqual(self.client._aemo_data_now, forecast[0])
        self.assertEqual(forecast[0]["SEMISCHEDULEDGENERATION"], 3000)
        name, headers, query = self.requests[-1]
        self.assertEqual(name, "priceAndDemand")
        self.assertEqual(query, {"timescale": "30MIN"})
        self.assertEqual(headers["x-api-key"], self.required_key)
        actual, forecast = await self.client.get_data("QLD1")
        self.assertEqual((len(actual), len(forecast)), (4, 2))
        self.assertTrue(all(row["REGIONID"] == "QLD1" for row in actual + forecast))

    async def test_full_data_groups_regions_without_accumulating_records(self):
        for _ in range(2):
            result = await self.client._get_data_full()
            self.assertEqual(set(result), {"ACTUAL", "FORECAST"})
            self.assertEqual(set(result["ACTUAL"]), set(REGIONS.values()))
            self.assertEqual(len(self.client._aemo_data_actual), 20)
            self.assertEqual(len(self.client._aemo_data_forecast), 10)
            record = result["FORECAST"]["NSW1"][0]
            self.assertEqual(record["SETTLEMENTDATE"] - record["PERIODSTARTDATE"],
                             timedelta(minutes=30))

    async def test_current_cumulative_uses_normalized_timestamp(self):
        await self.client._get_current_cumul_price()
        current = self.client._aemo_data_cumul_price["current"]
        self.assertEqual(set(current), set(REGIONS.values()))
        self.assertEqual(current["NSW1"]["price_kw"], -0.05)

    async def test_rotated_key_is_reloaded_and_retried(self):
        await self.client._get_current_cumulative_price()
        self.config["xApiKey"] = self.required_key = "rotated-test-key"
        await self.client._get_current_cumulative_price()
        self.assertEqual(self.count("config"), 2)
        self.assertEqual(self.count("cumulativePrice"), 3)
        self.assertEqual(self.client._api_key, self.required_key)

    async def test_forbidden_response_refreshes_key(self):
        self.auth_failures, self.auth_status = 1, 403
        await self.client._get_current_cumulative_price()
        self.assertEqual(self.count("config"), 2)

    async def test_repeated_auth_failure_stops_after_one_retry(self):
        self.auth_failures = 2
        with self.assertRaises(AuthError):
            await self.client.get_aemo_data(["nsw"])
        self.assertEqual(self.count("config"), 2)
        self.assertEqual(self.count("cumulativePrice"), 2)
        self.assertIsNone(self.client._api_key)
        self.assertFalse(self.session.closed)

    async def test_success_status_auth_error_is_reported(self):
        self.payloads["cumulativePrice"] = {
            "data": {}, "errors": [{"code": "401", "title": "Expired key"}],
        }
        with self.assertRaises(AuthError):
            await self.client.get_aemo_data(["nsw"])
        self.assertEqual(self.count("cumulativePrice"), 2)

    async def test_missing_or_invalid_config_key(self):
        for config in ({}, {"xApiKey": " "}, {"xApiKey": None}):
            with self.subTest(config=config):
                self.config = config
                with self.assertRaisesRegex(ClientError, "missing xApiKey"):
                    await self.client.get_aemo_data(["nsw"])
        self.assertEqual(self.count("cumulativePrice"), 0)

    async def test_api_error_envelope_is_reported(self):
        self.payloads["cumulativePrice"] = {
            "data": {}, "errors": [{"code": "500", "title": "Unavailable"}],
        }
        with self.assertRaisesRegex(ClientError, "Unavailable"):
            await self.client.get_aemo_data(["nsw"])
        self.assertEqual(self.count("cumulativePrice"), 1)

    async def test_http_failure(self):
        self.payloads["cumulativePrice"] = web.Response(status=500, text="Unavailable")
        with self.assertRaisesRegex(ClientError, "HTTP 500"):
            await self.client.get_aemo_data(["nsw"])

    async def test_invalid_json(self):
        self.payloads["cumulativePrice"] = web.Response(text="{", content_type="application/json")
        with self.assertRaisesRegex(ClientError, "invalid JSON"):
            await self.client.get_aemo_data(["nsw"])

    async def test_html_instead_of_json(self):
        self.payloads["cumulativePrice"] = web.Response(text="<html/>", content_type="text/html")
        with self.assertRaisesRegex(ClientError, "invalid JSON"):
            await self.client.get_aemo_data(["nsw"])

    async def test_non_object_response(self):
        self.payloads["cumulativePrice"] = []
        with self.assertRaisesRegex(ClientError, "non-object"):
            await self.client.get_aemo_data(["nsw"])

    async def test_invalid_error_collection(self):
        self.payloads["cumulativePrice"]["errors"] = "Unavailable"
        with self.assertRaisesRegex(ClientError, "invalid errors"):
            await self.client.get_aemo_data(["nsw"])

    async def test_missing_data_or_items(self):
        for data in (None, {}, {"items": []}, {"items": [None]}):
            with self.subTest(data=data):
                self.payloads["cumulativePrice"] = envelope(data)
                with self.assertRaises(ClientError):
                    await self.client.get_aemo_data(["nsw"])

    async def test_missing_record_fields(self):
        del self.payloads["cumulativePrice"]["data"]["items"][0]["dt"]
        with self.assertRaisesRegex(ClientError, "missing required fields"):
            await self.client.get_aemo_data(["nsw"])

    async def test_invalid_actual_flag(self):
        self.payloads["cumulativePrice"]["data"]["items"][0]["a"] = 2
        with self.assertRaisesRegex(ClientError, "unknown actual flag"):
            await self.client.get_aemo_data(["nsw"])

    async def test_invalid_period_type(self):
        self.payloads["priceAndDemand"]["data"]["items"][0]["periodType"] = "UNKNOWN"
        with self.assertRaisesRegex(ClientError, "unknown periodType"):
            await self.client.get_data("NSW1")

    async def test_invalid_numeric_prices(self):
        for value in (None, "100", True, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.payloads["priceAndDemand"]["data"]["items"][0]["rrp"] = value
                with self.assertRaisesRegex(ClientError, "finite number"):
                    await self.client.get_data("NSW1")

    async def test_invalid_cumulative_price(self):
        self.payloads["cumulativePrice"]["data"]["items"][0]["cp"] = None
        with self.assertRaisesRegex(ClientError, "cp must be a finite number"):
            await self.client.get_aemo_data(["nsw"])

    async def test_invalid_region(self):
        self.payloads["cumulativePrice"]["data"]["items"][0]["r"] = ["NSW1"]
        with self.assertRaisesRegex(ClientError, "Invalid dashboard region"):
            await self.client.get_aemo_data(["nsw"])

    async def test_missing_requested_actual_or_forecast(self):
        original = self.payloads["cumulativePrice"]["data"]["items"]
        for flag, kind in ((0, "forecast"), (1, "actual")):
            with self.subTest(kind=kind):
                self.payloads["cumulativePrice"]["data"]["items"] = [
                    row for row in original if not (row["r"] == "NSW1" and row["a"] == flag)
                ]
                with self.assertRaisesRegex(ClientError, f"Missing {kind} price data for NSW1"):
                    await self.client.get_aemo_data(["nsw"])

    async def test_sparse_actuals_keep_six_slots_with_null_missing_prices(self):
        self.payloads["cumulativePrice"]["data"]["items"] = [
            row for row in self.payloads["cumulativePrice"]["data"]["items"]
            if not (row["a"] == 1 and self.client._settlement_date(row["dt"]).minute == 10)
        ]
        result = await self.client.get_aemo_data(["nsw"])
        periods = result["current_price_window"]["NSW1"]
        self.assertEqual(len(periods), 6)
        for field in ("period_type", "price_mw", "price_kw", "cumulative_price"):
            self.assertIsNone(periods["period_2"][field])
        self.assertEqual(periods["period_3"]["price_kw"], -0.05)

    async def test_empty_current_window_keeps_null_average_and_forecast_estimate(self):
        self.payloads["cumulativePrice"]["data"]["items"] = [
            row for row in self.payloads["cumulativePrice"]["data"]["items"]
            if row["a"] == 0 or self.client._settlement_date(row["dt"]) < START
        ]
        result = await self.client.get_aemo_data(["nsw"])
        data = result["current_30min_forecast"]["NSW1"]
        self.assertEqual(data["periods_of_current_30min"], 0)
        self.assertIsNone(data["current_30min_avg"])
        self.assertEqual(data["current_30min_estimated"], 0.4)
        for row in result["current_price_window"]["NSW1"].values():
            for field in ("period_type", "price_mw", "price_kw", "cumulative_price"):
                self.assertIsNone(row[field])

    async def test_next_half_hour_interval_is_excluded(self):
        self.payloads["cumulativePrice"]["data"]["items"].append({
            "dt": (START + timedelta(minutes=35)).isoformat(),
            "r": "NSW1", "p": 999, "cp": 123456, "a": 1,
        })
        result = await self.client.get_aemo_data(["nsw"])
        self.assertEqual(result["current_30min_forecast"]["NSW1"]["periods_of_current_30min"], 3)

    async def test_missing_summary_does_not_reuse_stale_data(self):
        await self.client.get_aemo_data(["nsw"])
        self.payloads["elecSummary"]["data"]["summary"] = [
            row for row in self.payloads["elecSummary"]["data"]["summary"]
            if row["regionId"] != "NSW1"
        ]
        with self.assertRaisesRegex(ClientError, "Missing electricity summary for NSW1"):
            await self.client.get_aemo_data(["nsw"])

    async def test_malformed_summary_and_interconnectors(self):
        original = deepcopy(self.payloads["elecSummary"])
        cases = [
            ("interconnectorFlows", "{"), ("interconnectorFlows", "{}"),
            ("interconnectorFlows", '[{"name": "test"}]'),
            ("interconnectorFlows", '[{"name":"test","value":null,"exportlimit":1,"importlimit":-1}]'),
            ("semischeduledGeneration", None), ("apcFlag", None),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.payloads["elecSummary"] = deepcopy(original)
                self.payloads["elecSummary"]["data"]["summary"][0][field] = value
                with self.assertRaises(ClientError):
                    await self.client.get_aemo_data(["nsw"])

    async def test_missing_summary_collections(self):
        for data in ({}, {"summary": []}, {"summary": [{}], "prices": None}):
            with self.subTest(data=data):
                self.payloads["elecSummary"] = envelope(data)
                with self.assertRaisesRegex(ClientError, "Invalid electricity summary"):
                    await self.client.get_aemo_data(["nsw"])

    async def test_missing_market_limit(self):
        self.payloads["marketPriceLimits"]["data"]["items"].pop()
        with self.assertRaisesRegex(ClientError, "Invalid market price limits"):
            await self.client.get_aemo_data(["nsw"])

    async def test_invalid_market_limits(self):
        for value in (0, -1, None, float("nan")):
            with self.subTest(value=value):
                self.payloads["marketPriceLimits"]["data"]["items"][-1]["value"] = value
                with self.assertRaisesRegex(ClientError, "Invalid market price limit"):
                    await self.client.get_aemo_data(["nsw"])

    async def test_owned_session_closed_on_success_and_failure(self):
        created = []

        def create_session():
            session = ClientSession()
            created.append(session)
            return session

        client = AemoNemData()
        with patch("aemonemdata.aemonem.ClientSession", side_effect=create_session):
            await client.get_aemo_data(["nsw"])
            self.assertEqual(len(created), 1)
            self.assertTrue(created[-1].closed)
            self.assertIsNone(client._session)
            self.payloads["cumulativePrice"] = envelope({})
            with self.assertRaises(ClientError):
                await client.get_aemo_data(["nsw"])
            self.assertTrue(created[-1].closed)
            self.assertIsNone(client._session)

    async def test_get_data_supports_owned_session(self):
        client = AemoNemData()
        actual, forecast = await client.get_data("NSW1")
        self.assertEqual((len(actual), len(forecast)), (4, 2))
        self.assertIsNone(client._session)

    async def test_closed_external_session(self):
        await self.session.close()
        with self.assertRaisesRegex(ClientError, "session is closed"):
            await self.client.get_aemo_data(["nsw"])

    async def test_transport_failures_are_wrapped(self):
        for error in (asyncio.TimeoutError(), ClientConnectionError()):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.session, "get", side_effect=error):
                    with self.assertRaisesRegex(ClientError, "Dashboard request failed"):
                        await self.client.get_aemo_data(["nsw"])

    async def overlapping_calls(self, client, first_call, second_call, blocked_method):
        first_entered = asyncio.Event()
        second_started = asyncio.Event()
        release = asyncio.Event()
        original = getattr(client, blocked_method)
        calls = 0

        async def controlled(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_entered.set()
                await release.wait()
            return await original(*args, **kwargs)

        async def second():
            second_started.set()
            return await second_call()

        tasks = []
        with patch.object(client, blocked_method, side_effect=controlled):
            try:
                tasks.append(asyncio.create_task(first_call()))
                await asyncio.wait_for(first_entered.wait(), 2)
                tasks.append(asyncio.create_task(second()))
                await asyncio.wait_for(second_started.wait(), 2)
                release.set()
                return await asyncio.wait_for(asyncio.gather(*tasks), 5)
            finally:
                release.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_concurrent_price_calls_keep_regions_and_results_separate(self):
        results = await self.overlapping_calls(
            self.client,
            lambda: self.client.get_data("NSW1"),
            lambda: self.client.get_data("QLD1"),
            "_price_records",
        )
        for (actual, forecast), region in zip(results, ("NSW1", "QLD1")):
            self.assertEqual((len(actual), len(forecast)), (4, 2))
            self.assertTrue(all(row["REGIONID"] == region for row in actual + forecast))
        self.assertIsNot(results[0][0], results[1][0])

    async def test_concurrent_dashboard_calls_keep_regions_and_results_separate(self):
        results = await self.overlapping_calls(
            self.client,
            lambda: self.client.get_aemo_data(["nsw"]),
            lambda: self.client.get_aemo_data(["qld"]),
            "_get_current_cumulative_price",
        )
        for result, region in zip(results, ("NSW1", "QLD1")):
            self.assertEqual(set(result["current_30min_forecast"]), {region})
            self.assertEqual(set(result["current_price_window"]), {region})

    async def test_mixed_public_calls_keep_shared_state_separate(self):
        prices, dashboard = await self.overlapping_calls(
            self.client,
            lambda: self.client.get_data("NSW1"),
            lambda: self.client.get_aemo_data(["qld"]),
            "_price_records",
        )
        self.assertEqual(len(prices[0]), 4)
        self.assertTrue(all(row["REGIONID"] == "NSW1" for row in prices[0]))
        self.assertEqual(set(dashboard["current_30min_forecast"]), {"QLD1"})
        self.assertFalse(self.session.closed)

    async def test_concurrent_owned_calls_close_separate_sessions(self):
        created = []

        def create_session():
            session = ClientSession()
            created.append(session)
            return session

        client = AemoNemData()
        with patch("aemonemdata.aemonem.ClientSession", side_effect=create_session):
            results = await self.overlapping_calls(
                client,
                lambda: client.get_aemo_data(["nsw"]),
                lambda: client.get_aemo_data(["qld"]),
                "_get_current_cumulative_price",
            )
        self.assertEqual(len(created), 2)
        self.assertTrue(all(session.closed for session in created))
        self.assertIsNone(client._session)
        self.assertEqual(set(results[0]["current_30min_forecast"]), {"NSW1"})
        self.assertEqual(set(results[1]["current_30min_forecast"]), {"QLD1"})

    async def test_shared_session_never_creates_a_private_session(self):
        with patch(
            "aemonemdata.aemonem.ClientSession",
            side_effect=AssertionError("Private session created"),
        ):
            await self.client.get_aemo_data(["nsw"])
            await self.client.get_data("NSW1")
        self.assertIs(self.client._session, self.session)
        self.assertFalse(self.session.closed)

    async def check_cancelled_update(self, client, owned):
        entered = asyncio.Event()
        sessions = []

        async def blocked_request(*args, **kwargs):
            sessions.append(client._session)
            entered.set()
            await asyncio.Future()

        with patch.object(client, "_api_get", side_effect=blocked_request):
            task = asyncio.create_task(client.get_aemo_data(["nsw"]))
            try:
                await asyncio.wait_for(entered.wait(), 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(sessions[0].closed, owned)
        if owned:
            self.assertIsNone(client._session)
        else:
            self.assertIs(client._session, self.session)
        result = await client.get_aemo_data(["nsw"])
        self.assertEqual(set(result["current_30min_forecast"]), {"NSW1"})

    async def test_cancelled_update_closes_owned_session_and_allows_next_update(self):
        await self.check_cancelled_update(AemoNemData(), True)

    async def test_cancelled_update_keeps_shared_session_and_allows_next_update(self):
        await self.check_cancelled_update(self.client, False)

    async def test_cancelled_waiting_call_does_not_interrupt_active_update(self):
        entered = asyncio.Event()
        second_started = asyncio.Event()
        release = asyncio.Event()
        original = self.client._price_records

        async def blocked_records():
            entered.set()
            await release.wait()
            return await original()

        async def second_call():
            second_started.set()
            return await self.client.get_data("QLD1")

        with patch.object(self.client, "_price_records", side_effect=blocked_records):
            first = asyncio.create_task(self.client.get_data("NSW1"))
            second = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                second = asyncio.create_task(second_call())
                await asyncio.wait_for(second_started.wait(), 2)
                second.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await second
                release.set()
                actual, _ = await asyncio.wait_for(first, 5)
                self.assertEqual(len(actual), 4)
                self.assertTrue(all(row["REGIONID"] == "NSW1" for row in actual))
                self.assertFalse(self.session.closed)
            finally:
                release.set()
                tasks = [task for task in (first, second) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_none_state_remains_a_no_op(self):
        self.assertEqual(await self.client.get_aemo_data(None), {})
        self.assertEqual(self.requests, [])


class TimestampTests(unittest.TestCase):
    def test_existing_offset_is_not_appended_twice(self):
        self.assertEqual(AemoNemData._settlement_date("2026-11-11T08:00:00+10:00"), START)

    def test_naive_timestamp_is_market_time(self):
        self.assertEqual(AemoNemData._settlement_date("2026-11-11T08:00:00"), START)

    def test_daylight_saving_offset_is_converted_to_fixed_market_time(self):
        date = AemoNemData._settlement_date("2026-11-11T09:00:00+11:00")
        self.assertEqual(date.hour, 8)
        self.assertEqual(date.utcoffset(), timedelta(hours=10))

    def test_invalid_timestamp(self):
        for value in ("not-a-date", None):
            with self.subTest(value=value):
                with self.assertRaises(ClientError):
                    AemoNemData._settlement_date(value)

    def test_half_hour_and_day_boundaries(self):
        for hour, minute, expected in ((0, 0, 0), (8, 29, 0), (8, 30, 30), (23, 59, 30)):
            with self.subTest(hour=hour, minute=minute):
                now = datetime(2026, 11, 11, hour, minute, 59, 123, tzinfo=timezone.utc)
                with patch("aemonemdata.utils.datetime") as clock:
                    clock.now.return_value = now
                    start, end = current_30min_window()
                self.assertEqual(start, now.replace(minute=expected, second=0, microsecond=0))
                self.assertEqual(end - start, timedelta(minutes=30))


if __name__ == "__main__":
    unittest.main()
