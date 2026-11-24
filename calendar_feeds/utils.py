import logging
import re
from datetime import datetime, time, timedelta
from datetime import timezone as datetime_timezone

from django.utils import timezone

logger = logging.getLogger(__name__)


def normalize_rrule_until(rrule_str):
    """Normalize any naive UNTIL parameter in an RFC-5545 RRULE to UTC format.

    RFC-5545 and python-dateutil require UNTIL to be specified in UTC with a
    trailing 'Z' when DTSTART is timezone-aware (as is standard in Django with USE_TZ=True).
    Converts naive DATE format (UNTIL=YYYYMMDD) to end-of-day UTC (UNTIL=YYYYMMDDT235959Z)
    and naive DATETIME format (UNTIL=YYYYMMDDTHHMMSS) to UTC (UNTIL=YYYYMMDDTHHMMSSZ).
    """
    if not rrule_str or "until=" not in rrule_str.lower():
        return rrule_str

    # Match UNTIL=YYYYMMDD without time or Z, and append T235959Z
    rrule_str = re.sub(
        r"(?i)\bUNTIL=(\d{8})(?!T)",
        r"UNTIL=\1T235959Z",
        rrule_str,
    )
    # Match UNTIL=YYYYMMDDTHHMMSS without trailing Z, and append Z
    rrule_str = re.sub(
        r"(?i)\bUNTIL=(\d{8}T\d{6})(?!Z)",
        r"UNTIL=\1Z",
        rrule_str,
    )
    return rrule_str


def expand_events_for_range(feed, start_date, end_date):
    """Expand recurring events and apply exceptions for a date range.

    Returns a list of dicts describing concrete event instances for the given
    range, including recurring event instances and excluding/moving dates per
    EventException rules.
    """
    if feed.organization_id is None:
        return []

    from .models import CalendarEvent

    events = (
        CalendarEvent.objects.for_organization(feed.organization)
        .filter(feed=feed)
        .select_related(
            "feed",
            "organization__organization_settings",
            "feed__organization__organization_settings",
        )
        .order_by("start_datetime")
    )

    expanded = []
    for event in events:
        if event.is_recurring:
            expanded.extend(_expand_recurring(event, start_date, end_date))
        else:
            event_date = (
                event.start_datetime.date()
                if event.all_day
                else timezone.localtime(
                    event.start_datetime, get_event_timezone(event)
                ).date()
            )
            if start_date <= event_date <= end_date:
                expanded.append(_make_instance(event, event_date))

    expanded.sort(key=lambda e: (e["date"], e["start_time"] or time.min))
    return expanded


def get_event_timezone(event):
    from organizations.utils import get_organization_timezone

    return get_organization_timezone(event.organization)


def event_recurrence_rule(event):
    """Return a local-time recurrence rule and its optional UTC cutoff."""
    from dateutil.rrule import rrulestr

    rrule_str = normalize_rrule_until(event.rrule_ical)
    until_match = re.search(r"(?i)(?:^|;)UNTIL=([^;]+)", rrule_str)
    until = None
    if until_match:
        until_value = until_match.group(1)
        if "T" in until_value:
            until = datetime.strptime(until_value, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=datetime_timezone.utc
            )
        else:
            until = datetime.strptime(until_value, "%Y%m%d").replace(
                hour=23,
                minute=59,
                second=59,
                tzinfo=datetime_timezone.utc,
            )
        rrule_str = re.sub(r"(?i)(?:^|;)UNTIL=[^;]+", "", rrule_str).strip(";")

    event_timezone = (
        timezone.get_default_timezone() if event.all_day else get_event_timezone(event)
    )
    local_start = timezone.localtime(event.start_datetime, event_timezone)
    return rrulestr(rrule_str, dtstart=local_start), until


def event_occurrence_for_identity(event, original_date, rule=None, until=None):
    """Find the local occurrence stored under its historical UTC date key."""
    if rule is None:
        rule, until = event_recurrence_rule(event)

    event_timezone = (
        timezone.get_default_timezone() if event.all_day else get_event_timezone(event)
    )
    local_start = timezone.localtime(event.start_datetime, event_timezone)
    for day_offset in (-1, 0, 1):
        local_date = original_date + timedelta(days=day_offset)
        candidate = local_start.replace(
            year=local_date.year, month=local_date.month, day=local_date.day
        )
        candidate_utc = candidate.astimezone(datetime_timezone.utc)
        candidate_identity = candidate.date() if event.all_day else candidate_utc.date()
        if candidate_identity != original_date:
            continue
        occurrence = rule.after(candidate - timedelta(microseconds=1), inc=True)
        if occurrence != candidate:
            continue
        if until and candidate_utc > until:
            continue
        return candidate
    return None


def event_occurrence_end(event, occurrence_start):
    duration = event.end_datetime - event.start_datetime
    return timezone.localtime(
        occurrence_start.astimezone(datetime_timezone.utc) + duration,
        get_event_timezone(event),
    )


def _expand_recurring(event, start_date, end_date):
    """Expand a single recurring event into concrete instances."""
    instances = []

    if not event.rrule_ical:
        return instances

    try:
        event_timezone = (
            timezone.get_default_timezone()
            if event.all_day
            else get_event_timezone(event)
        )
        range_start = datetime.combine(start_date, time.min, tzinfo=event_timezone)
        range_end = datetime.combine(
            end_date + timedelta(days=1), time.max, tzinfo=event_timezone
        )
        rrule, until = event_recurrence_rule(event)
        occurrence_iter = rrule.between(range_start, range_end, inc=True)
        excluded_dates = set(event.exdate_list)
        exceptions = {
            exc.original_date: exc
            for exc in event.exceptions.filter(organization_id=event.organization_id)
        }

        for occurrence in occurrence_iter:
            original_date = (
                occurrence.date()
                if event.all_day
                else occurrence.astimezone(datetime_timezone.utc).date()
            )
            target_date = occurrence.date()

            if not start_date <= target_date <= end_date:
                continue
            if until and occurrence.astimezone(datetime_timezone.utc) > until:
                continue

            # Skip manually excluded dates (exdate_ical)
            if original_date in excluded_dates:
                continue

            exc = exceptions.get(original_date)
            if exc:
                if exc.change_type == exc.CHANGE_DELETED:
                    continue
                if exc.change_type == exc.CHANGE_MOVED:
                    continue
                if exc.change_type == exc.CHANGE_CHANGED:
                    instances.append(
                        _make_instance(
                            event,
                            target_date,
                            start_override=occurrence,
                            title_override=exc.new_title or None,
                            location_override=exc.new_location or None,
                            description_override=exc.new_description or None,
                            original_date=original_date,
                            exception_pk=exc.pk,
                        )
                    )
                    continue

            instances.append(
                _make_instance(
                    event,
                    target_date,
                    start_override=occurrence,
                    original_date=original_date,
                )
            )

        for original_date, exc in exceptions.items():
            if exc.change_type != exc.CHANGE_MOVED or not exc.new_start_datetime:
                continue
            if original_date in excluded_dates:
                continue
            if (
                event_occurrence_for_identity(event, original_date, rrule, until)
                is None
            ):
                continue
            moved_date = timezone.localtime(
                exc.new_start_datetime, event_timezone
            ).date()
            if not start_date <= moved_date <= end_date:
                continue
            instances.append(
                _make_instance(
                    event,
                    moved_date,
                    start_override=exc.new_start_datetime,
                    end_override=exc.new_end_datetime,
                    title_override=exc.new_title or None,
                    location_override=exc.new_location or None,
                    description_override=exc.new_description or None,
                    original_date=original_date,
                    exception_pk=exc.pk,
                )
            )

    except Exception:
        logger.exception("Error expanding recurring event %s", event.pk)

    return instances


def _make_instance(
    event,
    target_date,
    start_override=None,
    end_override=None,
    title_override=None,
    location_override=None,
    description_override=None,
    original_date=None,
    exception_pk=None,
):
    """Create a dict representing one instance of a calendar event."""
    if start_override:
        start_dt = start_override
    elif event.all_day:
        start_dt = timezone.make_aware(
            timezone.datetime.combine(target_date, timezone.datetime.min.time())
        )
    else:
        local_start = timezone.localtime(
            event.start_datetime, get_event_timezone(event)
        )
        start_dt = local_start.replace(
            year=target_date.year, month=target_date.month, day=target_date.day
        )

    if end_override:
        end_dt = end_override
    elif event.all_day:
        end_dt = timezone.make_aware(
            timezone.datetime.combine(target_date, timezone.datetime.max.time())
        )
    else:
        end_dt = event_occurrence_end(event, start_dt)

    return {
        "event": event,
        "pk": event.pk,
        "title": title_override or event.title,
        "description": description_override or event.description,
        "location": location_override or event.location,
        "start_datetime": start_dt,
        "end_datetime": end_dt,
        "start_time": start_dt.time() if not event.all_day else None,
        "end_time": end_dt.time() if not event.all_day else None,
        "date": target_date,
        "original_date": original_date or target_date,
        "exception_pk": exception_pk,
        "all_day": event.all_day,
        "color": event.feed.color,
        "feed_name": event.feed.name,
        "feed_slug": event.feed.slug,
        "uid": event.uid,
    }
