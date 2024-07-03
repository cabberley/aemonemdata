from .str_enum import StrEnum


class BaseUrl(StrEnum):
    
    API= "https://visualisations.aemo.com.au"

   
class EndPoint(StrEnum):
    
    API_5MIN_URL ="/aemo/apps/api/report/5MIN",
    API_ELEC_NEM_SUMMARY_URL = "/aemo/apps/api/report/ELEC_NEM_SUMMARY"
    API_MARKET_LIMITS_URL = "/aemo/apps/api/report/NEM_DASHBOARD_MARKET_PRICE_LIMITS"
    API_CUMULATIVE_PRICE_URL = "/aemo/apps/api/report/NEM_DASHBOARD_CUMUL_PRICE"
    

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