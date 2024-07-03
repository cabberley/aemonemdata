"""Exceptions for API Calls"""

from typing import Any

class AuthError(Exception):
    """Authentication issue from api."""

    def __init__(self, *args: Any) -> None:
        """Initialize the exception."""
        Exception.__init__(self, *args)
        
class ClientError(Exception):
    """Error from api."""

    def __init__(self, *args: Any) -> None:
        """Initialize the exception."""
        Exception.__init__(self, *args)