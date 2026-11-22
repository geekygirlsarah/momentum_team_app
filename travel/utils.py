"""Helpers for the travel feature.

Recipient filtering mirrors the fee/payment notification pattern in
``programs/signals.py``: only parents with portal login enabled, email updates
opted-in and a personal email on file are notified.
"""

import re
from html.parser import HTMLParser

from django.db.models import F, Prefetch
from django.template.defaultfilters import linebreaksbr
from django.utils.html import format_html, format_html_join

from programs.utils.email_html import (
    EMAIL_ALLOWED_ATTRIBUTES,
    EMAIL_ALLOWED_TAGS,
    html_to_text,
    normalize_email_html,
)

_EMAIL_ALLOWED_TAG_PATTERN = "|".join(
    re.escape(tag) for tag in sorted(EMAIL_ALLOWED_TAGS, key=len, reverse=True)
)
_TRAVEL_DESCRIPTION_HTML_TAG_RE = re.compile(
    rf"<\s*/?\s*(?:{_EMAIL_ALLOWED_TAG_PATTERN})(?=[\s/>])", re.IGNORECASE
)
_TRAVEL_DESCRIPTION_VOID_TAGS = frozenset({"br", "hr", "img"})


class _TravelDescriptionHTMLBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in EMAIL_ALLOWED_TAGS:
            return
        allowed_attributes = EMAIL_ALLOWED_ATTRIBUTES.get("*", set()) | (
            EMAIL_ALLOWED_ATTRIBUTES.get(tag, set())
        )
        safe_attributes = (
            (name, value if value is not None else "")
            for name, value in attrs
            if name in allowed_attributes
        )
        attributes_html = format_html_join("", ' {}="{}"', safe_attributes)
        self.parts.append(format_html("<{}{}>", tag, attributes_html))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _TRAVEL_DESCRIPTION_VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in EMAIL_ALLOWED_TAGS and tag not in _TRAVEL_DESCRIPTION_VOID_TAGS:
            self.parts.append(format_html("</{}>", tag))

    def handle_data(self, data):
        self.parts.append(format_html("{}", data))


def sanitize_travel_description(description):
    """Keep legacy plain text unchanged and normalize rich editor HTML safely."""
    description = str(description or "")
    if not _TRAVEL_DESCRIPTION_HTML_TAG_RE.search(description):
        return description
    return normalize_email_html(description)


def travel_description_for_display(description):
    description = str(description or "")
    if _TRAVEL_DESCRIPTION_HTML_TAG_RE.search(description):
        parser = _TravelDescriptionHTMLBuilder()
        parser.feed(sanitize_travel_description(description))
        parser.close()
        return format_html_join("", "{}", ((part,) for part in parser.parts))
    return linebreaksbr(description)


def travel_description_as_text(description):
    description = str(description or "")
    if not _TRAVEL_DESCRIPTION_HTML_TAG_RE.search(description):
        return description
    return html_to_text(sanitize_travel_description(description))


def signed_permission_form_prefetches(organization):
    """Prefetch current and superseded local signed forms for signup displays."""
    from travel.models import TravelSignedPermissionForm

    forms = (
        TravelSignedPermissionForm.objects.for_organization(organization)
        .filter(
            organization=organization,
            signup__organization=organization,
            signup__event__organization=organization,
            signup__student__organization=organization,
        )
        .exclude(file="")
    )
    return (
        Prefetch(
            "signed_permission_forms",
            queryset=forms.filter(
                form_version=F("signup__event__permission_form_version")
            ).order_by("-uploaded_at", "-pk"),
            to_attr="current_signed_permission_forms",
        ),
        Prefetch(
            "signed_permission_forms",
            queryset=forms.exclude(
                form_version=F("signup__event__permission_form_version")
            ).order_by("-uploaded_at", "-pk"),
            to_attr="outdated_signed_permission_forms",
        ),
    )


def parent_recipients(student, organization=None):
    """Unique parent emails we are allowed to notify for ``student``."""
    emails = []
    seen = set()
    for parent in student.all_parents:
        if organization is not None and parent.organization_id != organization.pk:
            continue
        if not (
            parent.login_enabled and parent.email_updates and parent.personal_email
        ):
            continue
        if parent.personal_email in seen:
            continue
        seen.add(parent.personal_email)
        emails.append(parent.personal_email)
    return emails


def student_recipients(student):
    """The student's own linked portal email, if they have one."""
    user = student.user
    if user and user.email:
        return [user.email]
    return []


def get_student_travel_highlights(student, program):
    """Upcoming trips this student is signed up for, for the dashboard card."""
    from datetime import date

    from django.db.models import F, Q

    from travel.models import TravelSignup

    organization = student.organization
    signups = (
        TravelSignup.objects.for_organization(organization)
        .filter(
            organization=organization,
            student=student,
            student__organization=organization,
            event__organization=organization,
            event__program=program,
            event__program__organization=organization,
            event__end_date__gte=date.today(),
        )
        .filter(
            Q(departure__isnull=True)
            | Q(
                departure__organization=organization,
                departure__event_id=F("event_id"),
            )
        )
        .select_related("student", "event", "departure", "approved_by")
        .prefetch_related(
            "cost_items", *signed_permission_form_prefetches(organization)
        )
    )
    return [
        s
        for s in signups
        if s.status in (TravelSignup.INTERESTED, TravelSignup.APPROVED)
    ]


def get_parent_travel_highlights(student, program):
    """Signups needing (or that received) this parent's attention, for the
    dashboard card."""
    from travel.models import TravelSignup

    signups = get_student_travel_highlights(student, program)
    pending = [s for s in signups if s.status == TravelSignup.INTERESTED]
    return signups, pending


def get_mentor_travel_signups(adult):
    """Upcoming trips an adult is going on as a mentor."""
    from travel.models import TravelMentorSignup

    signups = (
        TravelMentorSignup.objects.filter(adult=adult)
        .select_related("event", "event__program", "departure")
        .order_by("event__start_date", "event__name")
    )
    return [s for s in signups if not s.event.is_past]
