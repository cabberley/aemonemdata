"""AemoNemData class to get data from AEMO API."""
import json
from asyncio import Lock, TimeoutError
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

from aiohttp import ClientError as AiohttpClientError
from aiohttp import ClientResponse, ClientSession

from .constants import (
    AUTH_ERROR_CODES,
    REGIONS,
    BaseUrl,
    EndPoint,
)
from .exceptions import (
    AuthError,
    ClientError,
)
from .utils import current_30min_window


class AemoNemData:
    """AEMO data client; public fetches are serialized on the caller's event loop."""

    def __init__(self, client_session: ClientSession | None = None):
        """Accept a caller-owned session, preferably Home Assistant's shared session."""
        self._update_lock = Lock()
        self._region_id = None
        self._aemo_data_full = {}
        self._aemo_data_now = {}
        self._aemo_data_cumul_price = {}
        self._aemo_data_results ={}
        self._aemo_data_elec_nem_summary = {}
        self._aemo_data_elec_nem_summary_market_notice = []
        self._aemo_data_elec_nem_summary_prices = {}
        self._aemo_data_actual = []
        self._aemo_data_forecast = []
        self._timeout = 15
        self._session = client_session
        self._api_key: str | None = None
        self._session_manage = True
        self._ameo_mkt_limits = {}
        self._mkt_cap = None
        if client_session:
            self._session_manage = False

    async def get_aemo_data(self, state: list) -> dict[str, Any]:
        """Get AEMO Data."""
        async with self._update_lock:
            regions = []
            if state is not None:
                for region in state:
                    regions.append(REGIONS[region.lower()])
                async with self._session_scope():
                    await self._get_current_30min_price(regions)
                    self._aemo_data_results.pop("current_price")
            return self._aemo_data_results

    @asynccontextmanager
    async def _session_scope(self) -> AsyncIterator[None]:
        """Close only sessions created by this client, including on failure."""
        if self._session_manage and self._session is None:
            async with ClientSession() as session:
                self._session = session
                try:
                    yield
                finally:
                    self._session = None
        else:
            if self._session is None or self._session.closed:
                raise ClientError("The supplied HTTP session is closed")
            yield

    @staticmethod
    def _require_fields(record: Any, fields: set[str], context: str) -> None:
        if not isinstance(record, dict) or not fields.issubset(record):
            raise ClientError(f"Invalid {context}: missing required fields")

    @staticmethod
    def _require_numbers(record: dict[str, Any], fields: set[str], context: str) -> None:
        for field in fields:
            value = record[field]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(value)
            ):
                raise ClientError(f"Invalid {context}: {field} must be a finite number")

    @staticmethod
    def _require_region(value: Any) -> None:
        if not isinstance(value, str) or value not in REGIONS.values():
            raise ClientError("Invalid dashboard region ID")

    @staticmethod
    def _settlement_date(value: str) -> datetime:
        """Use fixed NEM market time, not a daylight-saving timezone."""
        try:
            result = datetime.fromisoformat(value)
        except (TypeError, ValueError) as error:
            raise ClientError(f"Invalid settlement timestamp: {value!r}") from error
        market_time = timezone(timedelta(hours=10))
        if result.tzinfo is None:
            return result.replace(tzinfo=market_time)
        return result.astimezone(market_time)

    async def _load_api_key(self) -> str:
        response = await self._api_get(
            f"{BaseUrl.API}{EndPoint.API_CONFIG_URL}", {"Accept": "application/json"}, None
        )
        key = response.get("xApiKey")
        if not isinstance(key, str) or not key.strip():
            raise ClientError("Dashboard configuration is missing xApiKey")
        self._api_key = key
        return key

    async def _dashboard_data(self, endpoint: str) -> dict[str, Any]:
        async with self._session_scope():
            key = self._api_key or await self._load_api_key()
            for attempt in range(2):
                try:
                    response = await self._api_get(
                        f"{BaseUrl.API}{endpoint}",
                        {"Accept": "application/json", "x-api-key": key},
                        None,
                    )
                    break
                except AuthError:
                    self._api_key = None
                    if attempt:
                        raise
                    key = await self._load_api_key()
            data = response.get("data")
            if not isinstance(data, dict):
                raise ClientError(f"Invalid dashboard response for {endpoint}: missing data")
            return data

    async def _dashboard_items(self, endpoint: str) -> list[dict[str, Any]]:
        data = await self._dashboard_data(endpoint)
        items = data.get("items")
        if not isinstance(items, list) or not items or not all(
            isinstance(item, dict) for item in items
        ):
            raise ClientError(f"Invalid dashboard response for {endpoint}: missing items")
        return items

    async def _price_records(self) -> list[dict[str, Any]]:
        items = await self._dashboard_items(EndPoint.API_5MIN_URL)
        records = []
        for item in items:
            self._require_fields(
                item, {"settlementDate", "regionId", "rrp", "periodType"}, "price record"
            )
            self._require_numbers(item, {"rrp"}, "price record")
            self._require_region(item["regionId"])
            if item["periodType"] not in ("ACTUAL", "FORECAST"):
                raise ClientError("Invalid price record: unknown periodType")
            record = {key.upper(): value for key, value in item.items()}
            record["SETTLEMENTDATE"] = self._settlement_date(item["settlementDate"])
            record["SPOTPRICEPERKW"] = round(item["rrp"] / 1000, 4)
            minutes = 5 if item["periodType"] == "ACTUAL" else 30
            record["PERIODSTARTDATE"] = record["SETTLEMENTDATE"] - timedelta(minutes=minutes)
            records.append(record)
        return sorted(records, key=lambda record: record["SETTLEMENTDATE"])

    async def get_data(self, region: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Get AEMO Data."""
        async with self._update_lock:
            self._region_id = region
            _first_forecast = False
            self._aemo_data_actual = []
            self._aemo_data_forecast = []
            self._aemo_data_now = {}
            for record in await self._price_records():
                if record['REGIONID'] == self._region_id:
                    if record['PERIODTYPE'] == 'ACTUAL':
                        self._aemo_data_actual.append(record)
                    elif record['PERIODTYPE'] == 'FORECAST':
                        self._aemo_data_forecast.append(record)
                        if not _first_forecast:
                            _first_forecast = True
                            self._aemo_data_now = record
            return self._aemo_data_actual, self._aemo_data_forecast

    async def _get_data_full(self) -> dict[str, Any]:
        """Get AEMO Data."""
        self._aemo_data_full = {}
        self._aemo_data_actual = []
        self._aemo_data_forecast = []
        for record in await self._price_records():
            if record['PERIODTYPE'] not in self._aemo_data_full:
                self._aemo_data_full[record['PERIODTYPE']] = {}
            if record["PERIODTYPE"] == "ACTUAL":
                self._aemo_data_actual.append(record)
            else:
                self._aemo_data_forecast.append(record)
            if record['REGIONID'] not in self._aemo_data_full[record['PERIODTYPE']]:
                self._aemo_data_full[record['PERIODTYPE']][record['REGIONID']] = []
            self._aemo_data_full[record['PERIODTYPE']][record['REGIONID']].append(record)          
        return self._aemo_data_full

    async def _get_current_cumulative_price(self) -> dict[str, Any]:
        """Get AEMO Data."""
        items = await self._dashboard_items(EndPoint.API_CUMULATIVE_PRICE_URL)
        self._aemo_data_cumul_price = {}
        for record in items:
            self._require_fields(record, {"a", "dt", "r", "p", "cp"}, "cumulative price")
            self._require_numbers(record, {"p", "cp"}, "cumulative price")
            self._require_region(record["r"])
            clean_record = {}
            if record["a"] == 1:
                clean_record["period_type"] = "actual"
                clean_record["settlement_date"] = self._settlement_date(record["dt"])
                clean_record["period_start_date"] = clean_record["settlement_date"] - timedelta(minutes=5)
            elif record["a"] == 0:
                clean_record["period_type"] = "forecast"
                clean_record["settlement_date"] = self._settlement_date(record["dt"])
                clean_record["period_start_date"] = clean_record["settlement_date"] - timedelta(minutes=30)
            else:
                raise ClientError("Invalid cumulative price: unknown actual flag")
            clean_record["region_id"] = record["r"]
            clean_record["price_mw"] = record["p"]
            clean_record["price_kw"] = round(record["p"]/1000,4)
            clean_record["cumulative_price"] = record["cp"]
            if clean_record["period_type"] not in self._aemo_data_cumul_price:
                self._aemo_data_cumul_price[clean_record["period_type"]] = {}
            if clean_record["region_id"] not in self._aemo_data_cumul_price[clean_record["period_type"]]:
                self._aemo_data_cumul_price[clean_record["period_type"]][clean_record["region_id"]] = []
            self._aemo_data_cumul_price[clean_record["period_type"]][clean_record["region_id"]].append(clean_record)

        for regions in self._aemo_data_cumul_price.values():
            for records in regions.values():
                records.sort(key=lambda record: record["settlement_date"])
        return self._aemo_data_cumul_price

    async def _get_current_cumul_price(self):
        """Get AEMO Data."""
        test= await self._get_current_cumulative_price()
        self._aemo_data_cumul_price = {}
        for region in test["actual"]:
            actual_current = max(test["actual"][region], key=lambda x:x["settlement_date"])
            if "current" not in self._aemo_data_cumul_price:
                self._aemo_data_cumul_price["current"] = {}
            self._aemo_data_cumul_price["current"][region] = actual_current
        return

    async def _get_current_30min_price(self, regions: list[str]):
        """Get AEMO Data."""
        self._aemo_data_results = {}
        self._aemo_data_cumul_price = {}
        current_30min_window_start, current_30min_window_end = current_30min_window()
        current_price_data= await self._get_current_cumulative_price()
        for region in regions:
            for period_type in ("actual", "forecast"):
                if not current_price_data.get(period_type, {}).get(region):
                    raise ClientError(f"Missing {period_type} price data for {region}")
        if self._mkt_cap is None:
            self._mkt_cap = await self._get_mkt_limit_cap()
        mkt_limits = await self._get_mkt_limit()
        for region in regions:
            if region not in mkt_limits:
                raise ClientError(f"Missing electricity summary for {region}")
        if "current_price" not in self._aemo_data_results:
            self._aemo_data_results["current_price"] = {}
        for region in regions:
            if region not in self._aemo_data_results["current_price"]:
                self._aemo_data_results["current_price"][region] = []
        for region in current_price_data["actual"]:
            if region in regions:
                for record in current_price_data["actual"][region]:
                    if record["period_start_date"] >= current_30min_window_start and record["period_start_date"] < current_30min_window_end:
                        self._aemo_data_results["current_price"][region].append(record)
        for region in current_price_data["actual"]:
            if region in regions:
                if "current_price_window" not in self._aemo_data_results:
                    self._aemo_data_results["current_price_window"] = {}
                period_order = {
                    f"period_{index}": {
                        "period_type": None,
                        "settlement_date": None,
                        "period_start_date": None,
                        "region_id": region,
                        "price_mw": None,
                        "price_kw": None,
                        "cumulative_price": None,
                    }
                    for index in range(1, 7)
                }
                for record in current_price_data["actual"][region]:
                    if record["period_start_date"] >= current_30min_window_start and record["period_start_date"] < current_30min_window_end:
                        index = record["period_start_date"].minute % 30 // 5 + 1
                        period_order[f"period_{index}"] = record
                self._aemo_data_results["current_price_window"][region] = period_order
        for region in self._aemo_data_results["current_price"]:
            if region in regions:
                records_count = len(self._aemo_data_results["current_price"][region])
                current_actual_prices = sum(item["price_kw"] for item in self._aemo_data_results["current_price"][region])
                current_5min_price = (max(current_price_data["actual"][region], key=lambda x:x["settlement_date"]))["price_kw"]
                if records_count !=0 :
                    current_30min_avg = round(current_actual_prices/records_count,4)
                else:
                    current_30min_avg = None
                current_30min_forecast = round((min(current_price_data["forecast"][region], key=lambda x:x["settlement_date"]))["price_kw"],4)
                current_30min_estimated = round((current_actual_prices + current_30min_forecast*(6-records_count))/6,4)
                current_cumulative_price = round((max(current_price_data["actual"][region], key=lambda x:x["settlement_date"]))["cumulative_price"],0)
                if "current_30min_forecast" not in self._aemo_data_results:
                    self._aemo_data_results["current_30min_forecast"] = {}
                if region not in self._aemo_data_results["current_30min_forecast"]:
                    self._aemo_data_results["current_30min_forecast"][region] = {}
                forcast_data = []
                for record in current_price_data["forecast"][region]:
                    forcast_data.append({"start_time": record["period_start_date"] ,"end_time": record["settlement_date"], "price": record["price_kw"]})
                data = {
                    "current_5min_period_price": current_5min_price,
                    "current_30min_avg": current_30min_avg,
                    "current_30min_forecast": current_30min_forecast,
                    "current_30min_estimated": current_30min_estimated,
                    "current_cumulative_price": int(current_cumulative_price),
                    "current_percent_cumulative_price": round(current_cumulative_price/self._mkt_cap["CumulativePriceThreshold"]*100,2),
                    "administered_price_cap": self._mkt_cap["AdministeredPriceCap"],
                    "market_price_cap": self._mkt_cap["MarketPriceCap"],
                    "cumulative_price_threshold": self._mkt_cap["CumulativePriceThreshold"],
                    "market_suspended_flag": (True if mkt_limits[region]["MARKETSUSPENDEDFLAG"] ==1 else False),
                    "apc_flag": (True if mkt_limits[region]["APCFLAG"] == 1 else False),
                    "periods_of_current_30min": records_count,
                    "forecast": forcast_data,
                    "total_demand": mkt_limits[region]["TOTALDEMAND"],
                    "scheduled_generation": mkt_limits[region]["SCHEDULEDGENERATION"],
                    "interconnector_flows": mkt_limits[region]["INTERCONNECTORFLOWS"],
                    "semi_scheduled_generation": mkt_limits[region]["SEMISCHEDULEDGENERATION"],
                    "net_interconnector_flows": mkt_limits[region]["NETINTERCHANGE"],
                    "settlement_date_str": mkt_limits[region]["SETTLEMENTDATE"],
                    "settlement_date": self._settlement_date(mkt_limits[region]["SETTLEMENTDATE"]),
                }
                for flow in mkt_limits[region]["INTERCONNECTORFLOWS"]:
                    data[f"flow_{flow['name']}_name"] = flow["name"]
                    data[f"flow_{flow['name']}_value"] = flow["value"]
                    data[f"flow_{flow['name']}_export_limit"] = flow["exportlimit"]
                    data[f"flow_{flow['name']}_import_limit"] = flow["importlimit"]

                self._aemo_data_results["current_30min_forecast"][region] = data
        return


    async def _get_mkt_limit_cap(self) -> dict[str, Any]:
        """Get AEMO Data."""
        items = await self._dashboard_items(EndPoint.API_MARKET_LIMITS_URL)
        limits = {}
        for item in items:
            self._require_fields(item, {"key", "value"}, "market price limit")
            if not isinstance(item["key"], str):
                raise ClientError("Invalid market price limit key")
            limits[item["key"]] = item["value"]
        required = {"AdministeredPriceCap", "CumulativePriceThreshold", "MarketPriceCap"}
        self._require_fields(limits, required, "market price limits")
        self._require_numbers(limits, required, "market price limit")
        for name in required:
            if limits[name] <= 0:
                raise ClientError(f"Invalid market price limit: {name}")
        self._ameo_mkt_limits = {name: limits[name] for name in required}
        return self._ameo_mkt_limits

    async def _get_mkt_limit(self) -> dict[str, Any]:
        """Get AEMO Data."""
        response = await self._dashboard_data(EndPoint.API_ELEC_NEM_SUMMARY_URL)
        if not isinstance(response.get("summary"), list) or not response["summary"]:
            raise ClientError("Invalid electricity summary: missing summary records")
        if not isinstance(response.get("prices"), list):
            raise ClientError("Invalid electricity summary: missing price records")
        summaries = {}
        prices = {}
        for item in response["summary"]:
            self._require_fields(
                item,
                {"settlementDate", "regionId", "apcFlag", "marketsuspendedFlag",
                 "totalDemand", "netInterchange", "scheduledGeneration",
                 "semischeduledGeneration", "interconnectorFlows"},
                "electricity summary",
            )
            self._require_region(item["regionId"])
            self._require_numbers(
                item,
                {"totalDemand", "netInterchange", "scheduledGeneration",
                 "semischeduledGeneration"},
                "electricity summary",
            )
            if item["apcFlag"] not in (0, 1) or item["marketsuspendedFlag"] not in (0, 1):
                raise ClientError("Invalid electricity summary: unknown market flag")
            data_set = {key.upper(): value for key, value in item.items()}
            try:
                flows = json.loads(item["interconnectorFlows"])
            except (TypeError, ValueError) as error:
                raise ClientError("Invalid interconnector flow JSON") from error
            if not isinstance(flows, list):
                raise ClientError("Invalid interconnector flows: expected a list")
            for flow in flows:
                self._require_fields(
                    flow, {"name", "value", "exportlimit", "importlimit"}, "interconnector flow"
                )
                self._require_numbers(
                    flow, {"value", "exportlimit", "importlimit"}, "interconnector flow"
                )
                if not isinstance(flow["name"], str) or not flow["name"]:
                    raise ClientError("Invalid interconnector flow name")
            data_set["INTERCONNECTORFLOWS"] = flows
            data_set["SETTLEMENTDATE"] = (
                self._settlement_date(item["settlementDate"]).replace(tzinfo=None).isoformat()
            )
            summaries[data_set["REGIONID"]] = data_set
        for item in response["prices"]:
            self._require_fields(item, {"regionId", "rrp"}, "electricity price")
            self._require_region(item["regionId"])
            self._require_numbers(item, {"rrp"}, "electricity price")
            prices[item["regionId"]] = {key.upper(): value for key, value in item.items()}
        self._aemo_data_elec_nem_summary = summaries
        self._aemo_data_elec_nem_summary_prices = prices
        self._aemo_data_elec_nem_summary_market_notice = []
        return self._aemo_data_elec_nem_summary

    async def _api_post(self, url: str, headers: dict[str, Any], data ) -> dict[str, Any]:
        """Make POST API call."""
        async with self._session.post(
            url,
            headers=headers,
            data=data,
            timeout=self._timeout
            ) as resp:
            return await self._api_response(resp)

    async def _api_post_json(self, url: str, headers: dict[str, Any], data ) -> dict[str, Any]:
        """Make POST API call."""
        async with self._session.post(
            url,
            headers=headers,
            json=data,
            timeout=self._timeout
            ) as resp:
            return await self._api_response(resp)

    async def _api_get(
            self,
            url: str,
            headers: dict[str, Any],
            data: dict[str, Any] | None
        ) -> dict[str, Any]:
        """Make GET API call."""

        if self._session is None or self._session.closed:
            raise ClientError("No open HTTP session")
        try:
            async with self._session.get(
                url,
                headers=headers,
                data=data,
                timeout=self._timeout
            ) as resp:
                return await self._api_response(resp)
        except (AiohttpClientError, TimeoutError) as error:
            raise ClientError(f"Dashboard request failed: {type(error).__name__}") from error

    async def _api_delete(
            self,
            url: str,
            headers: dict[str, Any],
            data: dict[str, Any]
        ) -> dict[str, Any]:
        """Make GET API call."""

        async with self._session.delete(
                url,
                headers=headers,
                data=data,
                timeout=self._timeout
            ) as resp:
            return await self._api_response(resp)

    @staticmethod
    async def _api_response(resp: ClientResponse):
        """Return response from API call."""
        if resp.status in (401, 403):
            raise AuthError(f"Dashboard API authentication failed (HTTP {resp.status})")
        if resp.status != 200:
            raise ClientError(f"Dashboard API request failed (HTTP {resp.status})")
        try:
            response: dict[str, Any] = await resp.json()
        except (AiohttpClientError, ValueError) as error:
            raise ClientError("Dashboard API returned invalid JSON") from error
        if not isinstance(response, dict):
            raise ClientError("Dashboard API returned a non-object JSON response")
        errors = response.get("errors", [])
        if not isinstance(errors, list):
            raise ClientError("Dashboard API returned an invalid errors collection")
        if errors:
            if any(
                isinstance(error, dict) and str(error.get("code")) in ("401", "403")
                for error in errors
            ):
                raise AuthError("Dashboard API authentication failed")
            raise ClientError(f"Dashboard API errors: {errors}")
        if 'error' in response:
            code = response['error']
            if code in AUTH_ERROR_CODES:
                raise AuthError(f'AMEO Data API Error: {code}')
            else:
                raise ClientError(f'AMEO Data API Error: {code}')
        return response
