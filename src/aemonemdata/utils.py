"""Utils to support module"""

from datetime import datetime, timedelta, timezone


def current_30min_window():
    """Returns the start and end of the current 30 minute window."""
    current_time = datetime.now(timezone.utc)
    if current_time.minute < 30:
        current_30min_window_start = current_time - timedelta(
            minutes=current_time.minute,
            seconds=current_time.second,
            microseconds=current_time.microsecond,
        )
    elif current_time.minute >= 30:
        current_30min_window_start = current_time - timedelta(
            minutes=current_time.minute - 30,
            seconds=current_time.second,
            microseconds=current_time.microsecond,
        )
    current_30min_window_end = current_30min_window_start + timedelta(minutes=30)
    return current_30min_window_start, current_30min_window_end
