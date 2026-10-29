from datetime import datetime, timezone as dt_timezone

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def format_until(value) -> str | None:
    """'19 Oct', or '2 Jan 2027' outside the current year."""
    if value is None:
        return None
    if hasattr(value, "astimezone"):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        dt = dt.astimezone(dt_timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        dt = dt.astimezone(dt_timezone.utc)
    day = f"{dt.day} {_MONTHS[dt.month - 1]}"
    if dt.year != datetime.now(dt_timezone.utc).year:
        return f"{day} {dt.year}"
    return day
