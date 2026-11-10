# aemonemdata

Py Module to extract live data that is used for the NEM Dashboards

## Dashboard API

The client uses `https://dashboards.public.aemo.com.au`, the API behind the
[AEMO NEM data dashboard](https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/data-nem/data-dashboard-nem).
The former `visualisations.aemo.com.au/aemo/apps/api/report/` endpoints are no
longer used.

The dashboard key is loaded from `/config.json`, cached in memory, and refreshed
once if an API request returns an authentication error. No key needs to be
configured or embedded in application code.

| Data | GET endpoint |
| --- | --- |
| Actual prices and pre-dispatch forecasts | `/NEM/v1/PWS/NEMDashboard/priceAndDemand?timescale=30MIN` |
| Regional summary and interconnector flows | `/NEM/v1/PWS/NEMDashboard/elecSummary` |
| Market price limits | `/NEM/v1/PWS/NEMDashboard/marketPriceLimits` |
| Cumulative prices | `/NEM/v1/PWS/NEMDashboard/cumulativePrice` |

Responses are normalized to preserve the existing public result keys. Prices
remain converted from $/MWh to $/kWh, actual intervals are five minutes, and
pre-dispatch forecast intervals are thirty minutes. Datetimes use fixed NEM
market time (UTC+10), including during daylight saving. `settlement_date_str`
retains its original timezone-free ISO format. Missing current-window periods
retain `None` prices and cumulative prices; `current_30min_avg` remains `None`
when no actual intervals are available in that window. The new summary has no
market-notice collection.

`AemoNemData()` closes its own HTTP sessions, including on failure. When passing
an `aiohttp.ClientSession`, the caller remains responsible for closing it.
HTTP, authentication, and malformed-response failures raise `ClientError` or
`AuthError` from `aemonemdata.exceptions`.

These are dashboard-facing endpoints rather than a version-stable API contract;
future AEMO dashboard changes may require further updates.

## Tests

Tests use Python's built-in `unittest` and the existing `aiohttp` dependency.
Offline tests use a local HTTP server and do not contact AEMO. They cover all
regions, output shape, price calculations, forecasts, timestamps, interconnectors,
null-valued missing periods, repeated polling, key rotation, errors and session
ownership. CI runs them on Python 3.10 and 3.14.

Run from the repository root in PowerShell:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -W error::ResourceWarning -m unittest discover -s tests -v
```

To additionally run the opt-in live tests:

```powershell
$env:AEMONEM_LIVE_TESTS = "1"
python -m unittest discover -s tests -p test_live.py -v
Remove-Item Env:AEMONEM_LIVE_TESTS
```

Live tests depend on AEMO availability and are excluded from normal CI.
