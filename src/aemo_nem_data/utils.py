from datetime import datetime, timedelta, timezone


def current_30min_window():
    current_30min_window= datetime.now(timezone.utc)
    if current_30min_window.minute < 30:
        current_30min_window_start = current_30min_window - timedelta(minutes=current_30min_window.minute,seconds=current_30min_window.second, microseconds=current_30min_window.microsecond)
    elif current_30min_window.minute >= 30:
        current_30min_window_start = current_30min_window - timedelta(minutes=current_30min_window.minute-30,seconds=current_30min_window.second, microseconds=current_30min_window.microsecond)
    current_30min_window_end = current_30min_window_start + timedelta(minutes=30)
    return current_30min_window_start, current_30min_window_end