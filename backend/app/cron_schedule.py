"""Small, dependency-free five-field cron parser with IANA timezone support."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CronExpressionError(ValueError):
    """Raised when a cron expression or timezone is invalid."""


def _parse_field(raw: str, minimum: int, maximum: int, *, sunday: bool = False) -> tuple[set[int], bool]:
    values: set[int] = set()
    wildcard = raw == "*"
    for component in raw.split(","):
        if not component:
            raise CronExpressionError("Cron fields cannot contain empty list items.")
        base, slash, step_text = component.partition("/")
        if slash:
            try:
                step = int(step_text)
            except ValueError as exc:
                raise CronExpressionError(f"Invalid cron step: {step_text}") from exc
            if step < 1:
                raise CronExpressionError("Cron steps must be greater than zero.")
        else:
            step = 1

        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            parts = base.split("-", 1)
            try:
                start, end = int(parts[0]), int(parts[1])
            except ValueError as exc:
                raise CronExpressionError(f"Invalid cron range: {base}") from exc
            if start > end:
                raise CronExpressionError(f"Descending cron ranges are not supported: {base}")
        else:
            try:
                start = int(base)
            except ValueError as exc:
                raise CronExpressionError(f"Invalid cron value: {base}") from exc
            # For a single value with /step, include values from that value to the field maximum.
            end = maximum if slash else start

        if start < minimum or end > maximum:
            raise CronExpressionError(f"Cron value must be between {minimum} and {maximum}.")
        for value in range(start, end + 1, step):
            values.add(0 if sunday and value == 7 else value)

    if not values:
        raise CronExpressionError("Cron field does not select any values.")
    return values, wildcard


def parse_cron(expression: str) -> tuple[tuple[set[int], bool], ...]:
    parts = expression.split()
    if len(parts) != 5:
        raise CronExpressionError("Cron expression must contain exactly five fields: minute hour day month weekday.")
    return (
        _parse_field(parts[0], 0, 59),
        _parse_field(parts[1], 0, 23),
        _parse_field(parts[2], 1, 31),
        _parse_field(parts[3], 1, 12),
        _parse_field(parts[4], 0, 7, sunday=True),
    )


def next_cron_occurrence(expression: str, timezone_name: str, after_epoch: float) -> float:
    """Return the first matching local cron minute strictly after after_epoch."""
    fields = parse_cron(expression)
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronExpressionError(f"Unknown IANA timezone: {timezone_name}") from exc

    candidate = datetime.fromtimestamp(after_epoch, tz=timezone.utc).replace(second=0, microsecond=0)
    candidate += timedelta(minutes=1)
    # 370 days permits annual schedules and tolerates leap years. Iterate in UTC so
    # daylight-saving transitions never produce duplicate or nonexistent UTC instants.
    max_minutes = 370 * 24 * 60
    minute_values, _ = fields[0]
    hour_values, _ = fields[1]
    dom_values, dom_wildcard = fields[2]
    month_values, _ = fields[3]
    dow_values, dow_wildcard = fields[4]

    for _ in range(max_minutes):
        local = candidate.astimezone(zone)
        cron_dow = (local.weekday() + 1) % 7
        dom_match = local.day in dom_values
        dow_match = cron_dow in dow_values
        if dom_wildcard and dow_wildcard:
            day_match = True
        elif dom_wildcard:
            day_match = dow_match
        elif dow_wildcard:
            day_match = dom_match
        else:
            day_match = dom_match or dow_match

        if (
            local.minute in minute_values
            and local.hour in hour_values
            and local.month in month_values
            and day_match
        ):
            return candidate.timestamp()
        candidate += timedelta(minutes=1)

    raise CronExpressionError("No cron occurrence found within the next 370 days.")
