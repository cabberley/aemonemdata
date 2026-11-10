from .str_enum import StrEnum


class BaseUrl(StrEnum):
    
    API = "https://dashboards.public.aemo.com.au"

   
class EndPoint(StrEnum):
    
    API_CONFIG_URL = "/config.json"
    API_5MIN_URL = "/NEM/v1/PWS/NEMDashboard/priceAndDemand?timescale=30MIN"
    API_ELEC_NEM_SUMMARY_URL = "/NEM/v1/PWS/NEMDashboard/elecSummary"
    API_MARKET_LIMITS_URL = "/NEM/v1/PWS/NEMDashboard/marketPriceLimits"
    API_CUMULATIVE_PRICE_URL = "/NEM/v1/PWS/NEMDashboard/cumulativePrice"
    

REGIONS = {
    "nsw": "NSW1",
    "qld": "QLD1",
    "vic": "VIC1",
    "sa": "SA1",
    "tas": "TAS1",
}
    
AUTH_ERROR_CODES = [
    "unauthorized_client",
    "Login session expired.",
]