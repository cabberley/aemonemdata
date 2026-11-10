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

## Home Assistant integration

The library has no Home Assistant dependency. The following code belongs in the
custom integration, not in this package.

### Shared session and one coordinator

Construct one client per config entry using
`homeassistant.helpers.aiohttp_client.async_get_clientsession(hass)`. Never close
that session in the integration or create standalone sessions for each entity.
Use one `DataUpdateCoordinator` to fetch all configured regions together and let
all entities read its cached result without network I/O in their properties.

For example, the integration's `coordinator.py` can contain:

```python
import logging
from datetime import timedelta
from typing import Any

from aemonemdata import AemoNemData
from aemonemdata.exceptions import AuthError, ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)


class AemoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, regions: list[str]
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="AEMO NEM",
            update_interval=timedelta(minutes=5),
            always_update=False,
        )
        self.client = AemoNemData(async_get_clientsession(hass))
        self.regions = regions

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.client.get_aemo_data(self.regions)
        except (AuthError, ClientError) as err:
            raise UpdateFailed(f"Unable to update AEMO dashboard data: {err}") from err
```

In `async_setup_entry`, create this coordinator, call
`await coordinator.async_config_entry_first_refresh()`, store it in
`entry.runtime_data`, and then forward setup to the entity platforms. Use
`CoordinatorEntity` for entities so update failures make them unavailable.
Read-only coordinator platforms can set `PARALLEL_UPDATES = 0`.

The library wraps network timeouts and connection failures in its own
`ClientError`, so the coordinator must catch it explicitly. `AuthError` is a
separate exception and must also be caught. AEMO's key is public dashboard
configuration, not a user's credential: after the library's single refresh
attempt, treat persistent authentication failure as `UpdateFailed`, not
`ConfigEntryAuthFailed` or a reauthentication flow.

The five-minute interval above matches dispatch granularity; it is not an AEMO
rate-limit guarantee. Avoid aggressive polling and independent polling from each
sensor. The coordinator handles retry and availability; do not add tight retry
loops in entities.

Public `get_data()` and `get_aemo_data()` calls on the same client are serialized
by an async lock, including mixed calls, so overlapping requests cannot mix region
data or session ownership. Cancellation propagates and releases the lock, and
only internally owned sessions are closed. Use a client on one event loop;
private methods are implementation details, not concurrent entry points.

### Sensor values

| Output | Home Assistant mapping |
| --- | --- |
| Demand, generation, interconnector flows | `SensorDeviceClass.POWER`, native unit `UnitOfPower.MEGA_WATT` (`MW`) |
| Spot-price values converted by the library | Native unit `AUD/kWh`; not `POWER`, `ENERGY`, or `MONETARY` (which expects currency alone) |
| `settlement_date` | `SensorDeviceClass.TIMESTAMP`, using the timezone-aware `datetime` |
| Missing period prices or empty-window average | Return `None`; do not turn missing readings into zero |
| Market suspension and administered-price flags | Boolean values, suitable for binary sensors |

Use the datetime output rather than the timezone-free `settlement_date_str` for
timestamp entities. Do not mark price forecasts or the rolling cumulative-price
window as `TOTAL_INCREASING`; they are not accumulating energy meters. Forecast
values should not use the `MEASUREMENT` state class either.

Expose independently useful live values as separate sensors. Avoid attaching the
entire result or large, frequently changing forecast arrays to every entity:
changed attributes are recorded along with state changes and can significantly
increase recorder storage. If forecast attributes are necessary for a card,
limit duplication and document recorder exclusion options for that entity.

When publishing, release the library to PyPI and update the custom integration's
`manifest.json` requirement to the released version. Integration setup, entity
metadata, recorder behavior and manifest changes must be tested in the custom
integration repository separately.

References:
[API libraries](https://contributors.home-assistant.io/core/integrations/api-library/),
[coordinated polling](https://contributors.home-assistant.io/core/integrations/fetching-data/),
and [sensor entities](https://contributors.home-assistant.io/core/entities/sensor/).

## Tests

Tests use Python's built-in `unittest` and the existing `aiohttp` dependency.
Offline tests use a local HTTP server and do not contact AEMO. They cover all
regions, output shape, price calculations, forecasts, timestamps, interconnectors,
null-valued missing periods, repeated polling, key rotation, errors, concurrent
public calls, shared-session reuse, cancellation and session ownership.
CI runs them on Python 3.10 and 3.14.

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
