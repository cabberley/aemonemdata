"""AEMO Nem Data transformation and processing."""

import json
from typing import Any
from datetime import datetime, timedelta
from aiohttp import ClientSession, ClientResponse

from .constants import (
    BaseUrl,
    EndPoint,
    REGIONS,
    AUTH_ERROR_CODES,
)
from .utils import current_30min_window
from .exceptions import (
        AuthError,
        ClientError,
)


class AemoNemData:
    """AEMO Nem Data transformation and processing."""

    def __init__(self: str, client_session: ClientSession = None):
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
        self._session: ClientSession = client_session if client_session else ClientSession()
        self._ameo_mkt_limits = {}
        self._mkt_cap = None

    async def get_aemo_data(self, state: list) -> dict[str, Any]:
        """Get AEMO Data."""
        regions = []
        if state is not None:
            for region in state:
                regions.append(REGIONS[region.lower()])
            await self._get_current_30min_price(regions)
        return self._aemo_data_results


    async def get_data(self, region: str) -> dict[str, Any]:
        """Get AEMO Data."""
        self._region_id = region
        _first_forecast = False
        post_data = {"timeScale":["30MIN"]}
        headers = {
            'Content_type': 'text/json',
            'accept': 'text/plain'
        }
        full_url = f'{BaseUrl.API}{EndPoint.API_5MIN_URL}'
        response = await self._api_post_json(full_url, headers, post_data)
        for record in response['5MIN']:
            if record['REGIONID'] == self._region_id:
                record['SETTLEMENTDATE']=datetime.fromisoformat(record['SETTLEMENTDATE']+'+10:00')
                record['SPOTPRICEPERKW']= round(record['RRP']/1000,4)
                if record['PERIODTYPE'] == 'ACTUAL':
                    record['PERIODSTARTDATE'] = record['SETTLEMENTDATE'] - timedelta(minutes=5)
                    self._aemo_data_actual.append(record)
                elif record['PERIODTYPE'] == 'FORECAST':
                    record['PERIODSTARTDATE'] = record['SETTLEMENTDATE'] - timedelta(minutes=30)
                    self._aemo_data_forecast.append(record)
                    if not _first_forecast:
                        _first_forecast = True
                        self._aemo_data_now = record
        return self._aemo_data_actual, self._aemo_data_forecast

    async def _get_data_full(self) -> dict[str, Any]:
        """Get AEMO Data."""
        post_data = {"timeScale":["30MIN"]}
        headers = {
            'Content_type': 'text/json',
            'accept': 'text/plain'
        }
        full_url = f'{BaseUrl.API}{EndPoint.API_5MIN_URL}'
        response = await self._api_post_json(full_url, headers, post_data)
        for record in response['5MIN']:
            record['SETTLEMENTDATE']=datetime.fromisoformat(record['SETTLEMENTDATE']+'+10:00')
            record['SPOTPRICEPERKW']= round(record['RRP']/1000,4)
            if record['PERIODTYPE'] not in self._aemo_data_full:
                self._aemo_data_full[record['PERIODTYPE']] = {}
            record['PERIODSTARTDATE'] = record['SETTLEMENTDATE'] - timedelta(minutes=5)
            self._aemo_data_actual.append(record)
            if record['REGIONID'] not in self._aemo_data_full[record['PERIODTYPE']]:
                self._aemo_data_full[record['PERIODTYPE']][record['REGIONID']] = []
            self._aemo_data_full[record['PERIODTYPE']][record['REGIONID']].append(record)          
        return self._aemo_data_full

    async def _get_current_cumulative_price(self) -> dict[str, Any]:
        """Get AEMO Data."""
        headers = {
            'Content_type': 'text/json',
            'accept': 'text/plain'
        }
        full_url = f'{BaseUrl.API}{EndPoint.API_CUMULATIVE_PRICE_URL}'
        response = await self._api_get(full_url, headers, None)
        for record in response['NEM_DASHBOARD_CUMUL_PRICE']:
            clean_record = {}
            if record["A"] == 1:
                clean_record["PERIODTYPE"] = "actual"
                clean_record["SETTLEMENTDATE"]=datetime.fromisoformat(record["DT"]+'+10:00')
                clean_record["PERIODSTARTDATE"] = clean_record["SETTLEMENTDATE"] - timedelta(minutes=5)
            elif record["A"] == 0:
                clean_record["PERIODTYPE"] = "forecast"
                clean_record["SETTLEMENTDATE"]=datetime.fromisoformat(record["DT"]+'+10:00')
                clean_record["PERIODSTARTDATE"] = clean_record["SETTLEMENTDATE"] - timedelta(minutes=30)
            clean_record["REGIONID"] = record["R"]
            clean_record["PRICE"] = record["P"]
            clean_record["CUMULATIVEPRICE"] = record["CP"]
            if clean_record["PERIODTYPE"] not in self._aemo_data_cumul_price:
                self._aemo_data_cumul_price[clean_record["PERIODTYPE"]] = {}
            if clean_record["REGIONID"] not in self._aemo_data_cumul_price[clean_record["PERIODTYPE"]]:
                self._aemo_data_cumul_price[clean_record["PERIODTYPE"]][clean_record["REGIONID"]] = []
            self._aemo_data_cumul_price[clean_record["PERIODTYPE"]][clean_record["REGIONID"]].append(clean_record)

        return self._aemo_data_cumul_price

    async def _get_current_cumul_price(self):
        """Get AEMO Data."""
        test= await self._get_current_cumulative_price()
        self._aemo_data_cumul_price = {}
        for region in test["actual"]:
            actual_current = max(test["actual"][region], key=lambda x:x["SETTLEMENTDATE"])
            test["actual"][region] = sorted(test["actual"][region], key=lambda x:x["SETTLEMENTDATE"])
            if "current" not in self._aemo_data_cumul_price:
                self._aemo_data_cumul_price["current"] = {}
            self._aemo_data_cumul_price["current"][region] = actual_current
        return

    async def _get_current_30min_price(self, regions: list[str]):
        """Get AEMO Data."""
        current_30min_window_start, current_30min_window_end = current_30min_window()
        current_price_data= await self._get_current_cumulative_price()
        if self._mkt_cap is None:
            self._mkt_cap = await self._get_mkt_limit_cap()
        mkt_limits = await self._get_mkt_limit()
        for region in current_price_data["actual"]:
            if region in regions:
                for record in current_price_data["actual"][region]:
                    if record["PERIODSTARTDATE"] >= current_30min_window_start and record["PERIODSTARTDATE"] < current_30min_window_end:
                        if "current_price" not in self._aemo_data_results:
                            self._aemo_data_results["current_price"] = {}
                        if region not in self._aemo_data_results["current_price"]:
                            self._aemo_data_results["current_price"][region] = []
                        self._aemo_data_results["current_price"][region].append(record)
        for region in self._aemo_data_results["current_price"]:
            if region in regions:
                records_count = len(self._aemo_data_results["current_price"][region])
                current_actual_prices = round(sum(item["PRICE"] for item in self._aemo_data_results["current_price"][region])/1000,4)
                current_30min_avg = round(current_actual_prices/records_count,4)
                current_30min_forecast = round((min(current_price_data["forecast"][region], key=lambda x:x["SETTLEMENTDATE"]))["PRICE"]/1000,4)
                current_30min_estimated = round((current_actual_prices + current_30min_forecast*(6-records_count))/6,4)
                current_cumulative_price = round((max(current_price_data["actual"][region], key=lambda x:x["SETTLEMENTDATE"]))["CUMULATIVEPRICE"],0)
                if "current_30min_forecast" not in self._aemo_data_results:
                    self._aemo_data_results["current_30min_forecast"] = {}
                if region not in self._aemo_data_results["current_30min_forecast"]:
                    self._aemo_data_results["current_30min_forecast"][region] = {}
                forcast_data = []
                for record in current_price_data["forecast"][region]:
                    forcast_data.append({"start_time": record["PERIODSTARTDATE"] ,"end_time": record["SETTLEMENTDATE"], "price": record["PRICE"]/1000})
                data = {
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
                    
                }
                self._aemo_data_results["current_30min_forecast"][region] = data
        return


    async def _get_mkt_limit_cap(self) -> dict[str, Any]:
        """Get AEMO Data."""
        headers = {
            'Content_type': 'text/json',
            'accept': 'text/plain'
        }
        full_url = f'{BaseUrl.API}{EndPoint.API_MARKET_LIMITS_URL}'
        response = await self._api_get(full_url, headers, None)
        for key in response["NEM_DASHBOARD_MARKET_PRICE_LIMITS"]:
            if key["KEY"] == "AdministeredPriceCap":
                self._ameo_mkt_limits["AdministeredPriceCap"] = key["VALUE"]
            elif key["KEY"] == "CumulativePriceThreshold":
                self._ameo_mkt_limits["CumulativePriceThreshold"] = key["VALUE"]
            elif key["KEY"] == "MarketPriceCap":
                self._ameo_mkt_limits["MarketPriceCap"] = key["VALUE"]
        return self._ameo_mkt_limits

    async def _get_mkt_limit(self) -> dict[str, Any]:
        """Get AEMO Data."""
        headers = {
            'Content_type': 'text/json',
            'accept': 'text/plain'
        }
        full_url = f'{BaseUrl.API}{EndPoint.API_ELEC_NEM_SUMMARY_URL}'
        response = await self._api_get(full_url, headers, None)
        data_set ={}
        for data in response["ELEC_NEM_SUMMARY"]:
            data_set = data
            data_set["INTERCONNECTORFLOWS"]=json.loads(data["INTERCONNECTORFLOWS"])
            self._aemo_data_elec_nem_summary[data_set["REGIONID"]] = data_set
        for data in response["ELEC_NEM_SUMMARY_MARKET_NOTICE"]:
            data_set = data
            self._aemo_data_elec_nem_summary_market_notice.append(data_set)
            for data in response["ELEC_NEM_SUMMARY_PRICES"]:
                data_set = data
                self._aemo_data_elec_nem_summary_prices[data_set["REGIONID"]] = data_set
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
            data: dict[str, Any]
        ) -> dict[str, Any]:
        """Make GET API call."""

        async with self._session.get(
                url,
                headers=headers,
                data=data,
                timeout=self._timeout
            ) as resp:
            return await self._api_response(resp)

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
        if resp.status != 200:
            error = await resp.text()
            raise ClientError(f'API Error Encountered. Status: {resp.status}; Error: {error}')
        try:
            response: dict[str, Any] = await resp.json()
        except Exception as error:
            raise ClientError(f'Could not return json {error}') from error
        if 'error' in response:
            code = response['error']
            if code in AUTH_ERROR_CODES:
                raise AuthError(f'AMEO Data API Error: {code}')
            else:
                raise ClientError(f'AMEO Data API Error: {code}')
        return response
