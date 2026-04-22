"""Datetime parsing helpers."""

from datetime import datetime


def parse_semantic_datetime(token: str, ref_tz: str = "America/New_York") -> datetime:
    """
    Parses semantic temporal tokens like 'tomorrow_1400' or 'friday_0930'.
    Falls back to strict ISO 8601 parsing if no semantic pattern matches.
    """
    import re
    import zoneinfo
    from datetime import datetime, timedelta

    try:
        tz = zoneinfo.ZoneInfo(ref_tz)
    except Exception:
        tz = zoneinfo.ZoneInfo("America/New_York")

    now = datetime.now(tz)

    # 1. Semantic parsing: format <day>_<HHMM>
    match = re.match(r"^([a-z]+)_(\d{4})$", token.lower())
    if match:
        day_str, time_str = match.groups()
        hour, minute = int(time_str[:2]), int(time_str[2:])

        target_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if day_str == "today":
            pass
        elif day_str == "tomorrow":
            target_date += timedelta(days=1)
        else:
            # Day of week parsing: 'monday', 'tuesday', etc.
            days_of_week = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
            if day_str in days_of_week:
                target_dow = days_of_week.index(day_str)
                current_dow = now.weekday()

                # Calculate days ahead: if today is target_dow, and time has passed, assume next week.
                days_ahead = target_dow - current_dow
                if days_ahead < 0 or (days_ahead == 0 and now >= target_date):
                    days_ahead += 7

                target_date += timedelta(days=days_ahead)
            else:
                raise ValueError(f"Unknown day format in semantic token: {day_str}")

        return target_date

    # 2. Strict ISO format fallback
    return datetime.fromisoformat(token.replace("Z", "+00:00")).astimezone(tz)
