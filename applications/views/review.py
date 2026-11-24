"""Lead-mentor review pages for applications.

Provides custom views (list / detail / approve / decline / edit / delete)
gated by the ``applications.review_application`` permission. The
``LeadMentor`` group (unified in migration ``0011``) carries that permission.
"""

from __future__ import annotations

import datetime
import json
import logging

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import SuspiciousFileOperation, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import models, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.decorators import method_decorator
from django.views import View

from audit.events import AuditEvent
from audit.service import log_event
from programs.constants import (
    GRADE_CHOICES,
    PHONE_TYPE_CHOICES,
    RELATIONSHIP_CHOICES,
    STATE_CHOICES,
    TSHIRT_SIZE_CHOICES,
)
from programs.models import (
    Adult,
    Program,
    ProgramDocument,
    RaceEthnicity,
    School,
    Student,
)
from programs.permission_views import user_is_lead_mentor
from programs.utils import GroupedProgramChoiceField
from programs.utils.email_async import send_messages_in_background
from programs.utils.email_html import build_email_parts
from programs.utils.grades import calculate_graduation_year
from programs.utils.notifications import get_sender_connection

from ..answers import EXPERIENCE_QUESTIONS, extract_experience_answers
from ..forms import (
    NOT_LISTED_SCHOOL,
    ParentInfoForm,
    StaffDocumentUploadForm,
    StudentInfoForm,
)
from ..identity_sync import delete_orphaned_incomplete_profiles
from ..models import (
    Application,
    ApplicationDocumentSubmission,
    ApplicationFixedAnswer,
    ApplicationGuardianDraft,
    ApplicationIdentityProposal,
    ApplicationQuestionAnswer,
    ApplicationQuestionSet,
    ApplicationRelationshipProposal,
    ApplicationStudentDraft,
    ApplicationStudentDraftRaceEthnicity,
    normalize_step_keys,
)
from ..proposals import (
    ApplicationFinalizationError,
    finalize_profile_workflow_application,
    identity_candidate_options,
    lock_application_proposal_context,
    proposal_review_state,
)
from ..services import (
    ApplicationConversionError,
    _collect_applicant_recipients,
    convert_application_to_student,
    get_primary_parent_email,
    has_required_application_documents,
    required_application_documents_complete,
    send_application_approved_email,
    send_application_converted_email,
    send_application_declined_email,
    send_application_submitted_email,
    send_otp_email,
    send_parent_handoff_email,
)
from .documents import application_document_file_response

logger = logging.getLogger(__name__)

REVIEW_PERM = "applications.review_application"


def _tenant_applications(organization):
    if organization is None:
        raise Http404
    return (
        Application.objects.for_organization(organization)
        .filter(superseded_at__isnull=True)
        .filter(
            models.Q(program__isnull=True)
            | models.Q(program__organization=organization)
        )
    )


def _get_tenant_application(request, app_id):
    organization = getattr(request, "organization", None)
    return get_object_or_404(
        _tenant_applications(organization),
        application_id=(app_id or "").upper(),
    )


def _has_foreign_document_submissions(application, organization):
    return (
        ApplicationDocumentSubmission.objects.filter(application=application)
        .exclude(
            organization=organization,
            document__program__organization=organization,
        )
        .exists()
    )


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------


class DeclineForm(forms.Form):
    """Captures the reason the lead mentors are declining an application.

    The reason is emailed to the applicant (along with the parent, when
    applicable) so they understand why and can follow up.
    """

    reason = forms.CharField(
        label="Reason (will be emailed to the applicant)",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 5,
                "placeholder": "Briefly explain why this application is being declined.",
            }
        ),
        required=False,
        help_text=(
            "Optional. If provided, this text will appear in the email "
            "sent to the applicant."
        ),
    )


# ---------------------------------------------------------------------------
# Per-field edit of the captured wizard ``data``.
#
# The wizard stores each step's cleaned data as a top-level key in
# ``Application.data`` (e.g. ``step5-student``). Instead of asking lead
# mentors to hand-edit JSON, we build a form with one field per captured
# value, grouped by step and laid out in application order. Fields that
# start with ``_`` (e.g. ``_existing_student_id``) are internal and are
# preserved on save but never shown.
# ---------------------------------------------------------------------------


_EDIT_STATE = [("", "---")] + list(STATE_CHOICES)
_EDIT_PHONE = list(PHONE_TYPE_CHOICES)
_EDIT_RELATIONSHIP = [("", "---")] + list(RELATIONSHIP_CHOICES)
_EDIT_TSHIRT = [("", "---")] + list(TSHIRT_SIZE_CHOICES)
_EDIT_GRADE = [("", "—")] + [(str(v), label) for v, label in GRADE_CHOICES]
_CLEARANCE_INTEREST = [
    ("", "---"),
    ("yes", "Yes, I want to start / complete clearances."),
    ("no", "No, not at this time."),
]
_CLEARANCE_STATUS = [
    ("", "---"),
    ("have", "I already have this clearance."),
    ("need", "I don't have this yet — I'll need to get it."),
]

_STUDENT_FIELDS = [
    {"name": "legal_first_name", "label": "Legal first name", "kind": "text"},
    {
        "name": "preferred_first_name",
        "label": "Preferred first name (if different)",
        "kind": "text",
    },
    {"name": "last_name", "label": "Last name", "kind": "text"},
    {"name": "pronouns", "label": "Pronouns", "kind": "text"},
    {"name": "date_of_birth", "label": "Date of birth", "kind": "date"},
    {"name": "address", "label": "Address", "kind": "text"},
    {"name": "city", "label": "City", "kind": "text"},
    {"name": "state", "label": "State", "kind": "select", "choices": _EDIT_STATE},
    {"name": "zip_code", "label": "Zip code", "kind": "text"},
    {"name": "personal_email", "label": "Student's personal email", "kind": "email"},
    {"name": "phone_number", "label": "Student's phone number", "kind": "text"},
    {
        "name": "phone_type",
        "label": "Phone type",
        "kind": "select",
        "choices": _EDIT_PHONE,
    },
    {"name": "can_receive_texts", "label": "Can receive texts?", "kind": "checkbox"},
    {
        "name": "directory_consent",
        "label": "OK to share name, address, and phone for directory / carpool map",
        "kind": "checkbox",
        "initial": True,
    },
    {"name": "school_name", "label": "School", "kind": "text"},
    {
        "name": "grade",
        "label": "Grade (K–12)",
        "kind": "select",
        "choices": _EDIT_GRADE,
    },
    {
        "name": "graduation_year",
        "label": "Expected graduation year",
        "kind": "int",
    },
    {"name": "race_ethnicities", "label": "Race / Ethnicity", "kind": "multi"},
    {
        "name": "tshirt_size",
        "label": "T-shirt size",
        "kind": "select",
        "choices": _EDIT_TSHIRT,
    },
    {"name": "allergies", "label": "Allergies", "kind": "textarea"},
    {
        "name": "dietary_restrictions",
        "label": "Dietary restrictions",
        "kind": "textarea",
    },
    {"name": "medical_notes", "label": "Other medical notes", "kind": "textarea"},
]

_EXPERIENCE_FIELDS = [
    {
        "name": "interest_reason",
        "label": "Why are you interested in this program this season?",
        "kind": "textarea",
    },
    {
        "name": "hoped_gains",
        "label": "What do you hope to gain from the experience?",
        "kind": "textarea",
    },
    {
        "name": "prior_robotics_experience",
        "label": "What prior robotics experience do you have?",
        "kind": "textarea",
    },
    {
        "name": "referral_source",
        "label": "How did you hear about this program?",
        "kind": "textarea",
    },
]

_PARENT_FIELDS = [
    {"name": "legal_first_name", "label": "Legal first name", "kind": "text"},
    {
        "name": "preferred_first_name",
        "label": "Preferred first name (if different)",
        "kind": "text",
    },
    {"name": "last_name", "label": "Last name", "kind": "text"},
    {"name": "pronouns", "label": "Pronouns", "kind": "text"},
    {
        "name": "relationship_to_student",
        "label": "Relationship to student",
        "kind": "select",
        "choices": _EDIT_RELATIONSHIP,
    },
    {
        "name": "specific_relationship",
        "label": "Specific relationship",
        "kind": "text",
    },
    {"name": "email", "label": "Email address", "kind": "email"},
    {"name": "address", "label": "Address", "kind": "text"},
    {"name": "city", "label": "City", "kind": "text"},
    {"name": "state", "label": "State", "kind": "select", "choices": _EDIT_STATE},
    {"name": "zip_code", "label": "Zip code", "kind": "text"},
    {"name": "phone_number", "label": "Phone number", "kind": "text"},
    {
        "name": "phone_type",
        "label": "Phone type",
        "kind": "select",
        "choices": _EDIT_PHONE,
    },
    {
        "name": "can_receive_texts",
        "label": "Can receive texts?",
        "kind": "checkbox",
        "initial": True,
    },
    {"name": "email_updates", "label": "Receive email updates", "kind": "checkbox"},
]

_HANDOFF_FIELDS = [
    {"name": "parent_email", "label": "Parent / guardian email", "kind": "email"},
]

_MENTOR_FIELDS = [
    {"name": "legal_first_name", "label": "Legal first name", "kind": "text"},
    {
        "name": "preferred_first_name",
        "label": "Preferred first name (if different)",
        "kind": "text",
    },
    {"name": "last_name", "label": "Last name", "kind": "text"},
    {"name": "pronouns", "label": "Pronouns", "kind": "text"},
    {"name": "phone_number", "label": "Phone number", "kind": "text"},
    {
        "name": "phone_type",
        "label": "Phone type",
        "kind": "select",
        "choices": _EDIT_PHONE,
    },
    {
        "name": "can_receive_texts",
        "label": "Can receive texts?",
        "kind": "checkbox",
        "initial": True,
    },
    {
        "name": "andrew_id",
        "label": "Organization account ID (if you have one)",
        "kind": "text",
    },
    {"name": "employer", "label": "Employer / affiliation", "kind": "text"},
    {
        "name": "notes",
        "label": "Why are you interested in volunteering?",
        "kind": "textarea",
    },
]

_CLEARANCE_INTEREST_FIELDS = [
    {
        "name": "interested",
        "label": "Interested in obtaining PA child-protection clearances?",
        "kind": "select",
        "choices": _CLEARANCE_INTEREST,
    },
]

_CLEARANCE_DETAIL_FIELDS = [
    {
        "name": "paca",
        "label": "PA Child Abuse Clearance (PACA)",
        "kind": "select",
        "choices": _CLEARANCE_STATUS,
    },
    {
        "name": "patch",
        "label": "PA Criminal Record Clearance (PATCH)",
        "kind": "select",
        "choices": _CLEARANCE_STATUS,
    },
    {
        "name": "fbi",
        "label": "FBI criminal fingerprint check",
        "kind": "select",
        "choices": _CLEARANCE_STATUS,
    },
]

# (data key, section heading, [field specs]) — in application order.
STUDENT_SECTIONS = [
    ("step5-student", "Student information", _STUDENT_FIELDS),
    ("step6-experience", "Student experience", _EXPERIENCE_FIELDS),
    ("step7-primaryparent", "Primary parent / guardian", _PARENT_FIELDS),
    ("step8-secondaryparent", "Secondary parent / guardian", _PARENT_FIELDS),
    ("step7_handoff", "Parent handoff", _HANDOFF_FIELDS),
]

MENTOR_SECTIONS = [
    ("mentor_info", "Mentor information", _MENTOR_FIELDS),
    ("mentor_clearance_interest", "Clearance interest", _CLEARANCE_INTEREST_FIELDS),
    ("mentor_clearance_detail", "Clearance details", _CLEARANCE_DETAIL_FIELDS),
]


def _sections_for(application) -> list:
    if application.applicant_type == Application.Type.MENTOR:
        return MENTOR_SECTIONS
    return STUDENT_SECTIONS


class ApplicationDataEditForm(forms.Form):
    """Per-field edit of the wizard's captured ``data`` JSON.

    Renders every value the wizard captures, grouped by step and laid out
    in application order, so lead mentors can fix typos on behalf of an
    applicant without hand-editing JSON. Internal keys (prefixed with ``_``)
    are preserved on save and never surfaced.

    The top-level ``email`` field edits the primary contact email on the
    Application model (separate from the email fields captured inside each
    wizard step).
    """

    email = forms.EmailField(
        label="Primary contact email",
        required=False,
        widget=forms.EmailInput(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, sections=(), custom_snapshot=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sections = list(sections)
        self.custom_snapshot = custom_snapshot
        self.fields["custom_snapshot_version"] = forms.CharField(
            required=False, widget=forms.HiddenInput
        )
        if custom_snapshot is not None:
            for item in custom_snapshot:
                self.fields[f"custom_answer_{item['question_id']}"] = forms.CharField(
                    label=item["question_text"],
                    required=item.get("required", False),
                    initial=item["answer"],
                    widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
                )
        for _data_key, _title, fields in self.sections:
            for spec in fields:
                self.fields[self._field_name(_data_key, spec["name"])] = (
                    self._make_field(spec)
                )

    def clean(self):
        cleaned = super().clean()
        if self.custom_snapshot is not None and any(
            f"custom_answer_{item['question_id']}" not in self.data
            for item in self.custom_snapshot
        ):
            raise forms.ValidationError("Submit all custom answer fields.")
        return cleaned

    @staticmethod
    def _field_name(data_key, field_name):
        return f"{data_key}__{field_name}"

    @staticmethod
    def _make_field(spec):
        kind = spec["kind"]
        label = spec.get("label")
        help_text = spec.get("help_text", "")
        required = False
        attrs = {"class": "form-control"}
        if kind == "textarea":
            attrs["rows"] = 3
            field = forms.CharField(
                label=label,
                required=required,
                help_text=help_text,
                widget=forms.Textarea(attrs=attrs),
            )
        elif kind == "email":
            field = forms.EmailField(
                label=label,
                required=required,
                help_text=help_text,
                widget=forms.EmailInput(attrs=attrs),
            )
        elif kind == "date":
            attrs["type"] = "date"
            field = forms.DateField(
                label=label,
                required=required,
                help_text=help_text,
                widget=forms.DateInput(attrs=attrs, format="%Y-%m-%d"),
            )
        elif kind == "int":
            field = forms.IntegerField(
                label=label,
                required=required,
                help_text=help_text,
                widget=forms.TextInput(attrs=attrs),
            )
        elif kind == "select":
            field = forms.ChoiceField(
                label=label,
                required=required,
                help_text=help_text,
                choices=spec["choices"],
                widget=forms.Select(attrs={"class": "form-select"}),
            )
        elif kind == "checkbox":
            field = forms.BooleanField(
                label=label,
                required=False,
                help_text=help_text,
                widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
            )
        elif kind == "multi":
            field = forms.ModelMultipleChoiceField(
                label=label,
                required=required,
                help_text=help_text,
                queryset=RaceEthnicity.objects.all().order_by("name"),
                widget=forms.CheckboxSelectMultiple(
                    attrs={"class": "form-check-input"}
                ),
            )
        else:  # "text"
            field = forms.CharField(
                label=label,
                required=required,
                help_text=help_text,
                widget=forms.TextInput(attrs=attrs),
            )
        if spec.get("initial") is not None:
            field.initial = spec["initial"]
        return field

    def rebuild_data(self, current_data, initial=None):
        """Rebuild ``Application.data`` from submitted fields.

        Only the fields represented in this form are overwritten; any other
        keys already stored for a step (e.g. ``_existing_student_id``) are
        preserved. Empty sections are only written back if they already
        existed or the mentor actually entered a value. A value that merely
        reproduces the form's prefilled ``initial`` (e.g. a default-checked
        checkbox) does *not* count as a real edit, so an untouched section is
        never created just to hold defaults.
        """
        current_data = dict(current_data or {})
        data = dict(current_data)
        initial = initial or {}
        for data_key, _title, fields in self.sections:
            existing = current_data.get(data_key)
            step = dict(existing) if isinstance(existing, dict) else {}
            touched = False
            for spec in fields:
                fname = spec["name"]
                field_name = self._field_name(data_key, fname)
                if field_name in self.cleaned_data:
                    raw_value = self.cleaned_data[field_name]
                    if spec["kind"] == "multi":
                        value = list(raw_value.values_list("pk", flat=True))
                    elif hasattr(raw_value, "isoformat"):
                        value = raw_value.isoformat()
                    else:
                        value = raw_value
                    step[fname] = value
                    reproduced_default = (
                        field_name in initial and initial[field_name] == raw_value
                    )
                    if value not in (None, "", [], False) and not reproduced_default:
                        touched = True
            if existing or touched:
                data[data_key] = step
        return data


class ApplicationTypedAnswerEditForm(forms.Form):
    state_version = forms.CharField(widget=forms.HiddenInput)

    def __init__(
        self,
        *args,
        application,
        fixed_answers,
        custom_answers,
        state_version,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.fields["state_version"].initial = state_version
        self.answer_fields = []

        for answer in fixed_answers:
            labels = EXPERIENCE_QUESTIONS.get(answer.question_key)
            label = (
                labels[application.applicant_type == "parent"]
                if labels
                else answer.question_key.replace("_", " ").capitalize()
            )
            if answer.source_step != "step6-experience":
                label = f"{label} ({answer.source_step})"
            name = f"fixed_answer_{answer.pk}"
            self.fields[name] = forms.CharField(
                label=label,
                required=False,
                initial=answer.answer,
                widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            )
            self.answer_fields.append((answer, name))

        for answer in custom_answers:
            name = f"custom_answer_{answer.pk}"
            self.fields[name] = forms.CharField(
                label=answer.question_text,
                required=answer.is_required,
                initial=answer.answer,
                widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            )
            self.answer_fields.append((answer, name))


class ApplicationEmailForm(forms.Form):
    program = GroupedProgramChoiceField(
        queryset=Program.objects.none(),
        required=False,
        empty_label="All programs",
        widget=forms.Select(attrs={"class": "form-select"}),
        help_text="Select the program whose applicants you want to email. Leave blank for all programs.",
    )
    statuses = forms.MultipleChoiceField(
        required=True,
        choices=Application.Status.choices,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
        help_text="Choose one or more application statuses to email.",
    )
    subject = forms.CharField(
        max_length=255, widget=forms.TextInput(attrs={"class": "form-control"})
    )
    body = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 12, "class": "form-control"}),
        help_text="Rich text is supported. Paste content or use the editor.",
    )
    test_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"class": "form-control"}),
        help_text="Optional: send only to this address for testing.",
    )
    from_account = forms.ChoiceField(
        required=False,
        label="Sender identity",
        help_text="Select the sender identity and reply-to address.",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["program"].queryset = (
            Program.objects.for_organization(organization)
            if organization is not None
            else Program.objects.none()
        )

        from organizations.utils import get_organization_email_settings

        org_cfg = (
            get_organization_email_settings(organization)
            if organization is not None
            else {}
        )
        accounts = org_cfg.get("sender_accounts", [])
        default_email = (
            org_cfg.get("notification_from_email")
            or getattr(settings, "NOTIFICATION_FROM_EMAIL", None)
            or getattr(settings, "DEFAULT_FROM_EMAIL", "")
        )
        default_name = org_cfg.get("sender_display_name") or getattr(
            settings, "DEFAULT_FROM_NAME", None
        )
        if default_name and default_email:
            default_label = f"Default ({default_name} <{default_email}>)"
        elif default_name:
            default_label = f"Default ({default_name})"
        elif default_email:
            default_label = f"Default ({default_email})"
        else:
            default_label = "Default"

        choices = [("DEFAULT", default_label)]
        for account in accounts:
            email = account.get("email") or ""
            display = account.get("display_name") or email or "Sender"
            value = account.get("key") or email
            label = f"{display} <{email}>" if email else display
            choices.append((value, label))

        self.fields["from_account"].choices = choices
        self.fields["from_account"].initial = "DEFAULT"


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class _ReviewerRequiredMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """Require a Lead Mentor in the request organization or a superuser."""

    raise_exception = False  # default behavior: redirect to login

    def has_permission(self):
        user = self.request.user
        organization = getattr(self.request, "organization", None)
        return user.is_superuser or (
            organization is not None and user_is_lead_mentor(user, org=organization)
        )

    def handle_no_permission(self):
        if (
            getattr(self.request, "organization", None) is None
            and not self.request.user.is_superuser
        ):
            raise Http404
        return super().handle_no_permission()


def _lock_review_application(request, app_id, using):
    return get_object_or_404(
        Application.objects.using(using)
        .for_organization(request.organization)
        .select_for_update()
        .filter(superseded_at__isnull=True),
        application_id=(app_id or "").upper(),
    )


def _proposal_review_redirect(application):
    return redirect("application_review_detail", app_id=application.application_id)


class ApplicationIdentityProposalDecisionView(_ReviewerRequiredMixin, View):
    """Record a lead-reviewed identity decision without changing live profiles."""

    def post(self, request, app_id: str, proposal_id: int):
        application = _get_tenant_application(request, app_id)
        using = application._state.db
        try:
            with transaction.atomic(using=using):
                current = _lock_review_application(request, app_id, using)
                if current.profile_workflow_version != 1:
                    messages.error(
                        request, "Identity review is not enabled for this application."
                    )
                    return _proposal_review_redirect(current)
                context = lock_application_proposal_context(current, using=using)
                proposal = next(
                    (
                        item
                        for item in context.identity_proposals
                        if item.pk == proposal_id
                    ),
                    None,
                )
                if proposal is None:
                    raise Http404
                if not proposal.is_baseline_current():
                    messages.error(
                        request,
                        "This identity proposal is stale; reload it before deciding.",
                    )
                    return _proposal_review_redirect(current)

                decision = request.POST.get("decision", "")
                if decision == ApplicationIdentityProposal.Decision.CONFIRMED:
                    target_reference = request.POST.get("target_profile", "")
                    try:
                        target_type, target_id = target_reference.split(":", 1)
                        target_id = int(target_id)
                    except (TypeError, ValueError):
                        raise ValidationError("Select a valid profile type.")
                    available = {
                        (candidate["profile_type"], candidate["profile"].pk)
                        for candidate in identity_candidate_options(current, proposal)
                    }
                    if (target_type, target_id) not in available:
                        raise Http404
                    if target_type == "student":
                        target = get_object_or_404(
                            Student.all_objects.using(using)
                            .for_organization(request.organization)
                            .select_for_update()
                            .filter(is_incomplete_application_profile=False),
                            pk=target_id,
                        )
                    elif target_type == "adult":
                        target = get_object_or_404(
                            Adult.all_objects.using(using)
                            .for_organization(request.organization)
                            .select_for_update()
                            .filter(is_incomplete_application_profile=False),
                            pk=target_id,
                        )
                    else:
                        raise Http404
                    refreshed_candidates = {
                        (candidate["profile_type"], candidate["profile"].pk)
                        for candidate in identity_candidate_options(current, proposal)
                    }
                    if (target_type, target_id) not in refreshed_candidates:
                        raise ValidationError(
                            "The selected profile no longer matches the application identity."
                        )
                    proposal.set_target_profile(target)
                elif decision in (
                    ApplicationIdentityProposal.Decision.NO_MATCH,
                    ApplicationIdentityProposal.Decision.REJECTED,
                ):
                    proposal.target_student = None
                    proposal.target_adult = None
                    proposal.target_baseline_revision = None
                else:
                    raise ValidationError("Select a valid identity decision.")

                old_decision = proposal.decision
                proposal.decision = decision
                proposal.reviewed_by = request.user
                proposal.reviewed_at = timezone.now()
                proposal.save(
                    update_fields=[
                        "target_student",
                        "target_adult",
                        "target_baseline_revision",
                        "decision",
                        "reviewed_by",
                        "reviewed_at",
                        "updated_at",
                    ]
                )
                log_event(
                    request=request,
                    event=AuditEvent.ADMISSION_DECISION,
                    resource=current,
                    before={
                        "identity_proposal_id": proposal.pk,
                        "decision": old_decision,
                    },
                    after={"identity_proposal_id": proposal.pk, "decision": decision},
                    notes="Application identity proposal reviewed.",
                )
                messages.success(request, "Identity proposal decision saved.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        return _proposal_review_redirect(application)


class ApplicationRelationshipProposalDecisionView(_ReviewerRequiredMixin, View):
    """Record a lead-reviewed relationship decision without changing family access."""

    def post(self, request, app_id: str, proposal_id: int):
        application = _get_tenant_application(request, app_id)
        using = application._state.db
        try:
            with transaction.atomic(using=using):
                current = _lock_review_application(request, app_id, using)
                if current.profile_workflow_version != 1:
                    messages.error(
                        request,
                        "Relationship review is not enabled for this application.",
                    )
                    return _proposal_review_redirect(current)
                context = lock_application_proposal_context(current, using=using)
                proposal = next(
                    (
                        item
                        for item in context.relationship_proposals
                        if item.pk == proposal_id
                    ),
                    None,
                )
                if proposal is None:
                    raise Http404
                if not proposal.is_baseline_current():
                    messages.error(
                        request,
                        "This relationship proposal is stale; reload it before deciding.",
                    )
                    return _proposal_review_redirect(current)

                decision = request.POST.get("decision", "")
                if decision not in (
                    ApplicationRelationshipProposal.Decision.APPROVED,
                    ApplicationRelationshipProposal.Decision.REJECTED,
                ):
                    raise ValidationError("Select a valid relationship decision.")
                old_decision = proposal.decision
                proposal.decision = decision
                proposal.reviewed_by = request.user
                proposal.reviewed_at = timezone.now()
                proposal.save(
                    update_fields=[
                        "decision",
                        "reviewed_by",
                        "reviewed_at",
                        "updated_at",
                    ]
                )
                log_event(
                    request=request,
                    event=AuditEvent.ADMISSION_DECISION,
                    resource=current,
                    before={
                        "relationship_proposal_id": proposal.pk,
                        "decision": old_decision,
                    },
                    after={
                        "relationship_proposal_id": proposal.pk,
                        "decision": decision,
                    },
                    notes="Application relationship proposal reviewed.",
                )
                messages.success(
                    request,
                    "Relationship proposal decision saved; access is unchanged until acceptance.",
                )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        return _proposal_review_redirect(application)


@method_decorator(login_required, name="dispatch")
class ApplicationReviewListView(_ReviewerRequiredMixin, View):
    """List page with status + applicant-type + program filters."""

    template_name = "applications/review/list.html"

    def get(self, request):
        org = getattr(request, "organization", None)
        qs = _tenant_applications(org).select_related("program")
        status = (request.GET.get("status") or "").strip()
        applicant_type = (request.GET.get("type") or "").strip()
        program_id = (request.GET.get("program") or "").strip()
        open_only = (request.GET.get("open") or "").strip() == "1"

        valid_statuses = {c[0] for c in Application.Status.choices}
        if status and status in valid_statuses:
            qs = qs.filter(status=status)

        valid_types = {c[0] for c in Application.Type.choices}
        if applicant_type and applicant_type in valid_types:
            qs = qs.filter(applicant_type=applicant_type)

        if program_id.isdigit():
            qs = qs.filter(program_id=int(program_id))

        if open_only:
            today = timezone.now().date()
            qs = (
                qs.filter(program__active=True)
                .filter(
                    models.Q(program__applications_open__lte=today)
                    | models.Q(program__applications_open__isnull=True)
                )
                .filter(
                    models.Q(program__applications_close__gte=today)
                    | models.Q(program__applications_close__isnull=True)
                )
                .filter(
                    models.Q(program__end_date__gte=today)
                    | models.Q(program__end_date__isnull=True)
                )
            )

        # Sorting
        sort = (request.GET.get("sort") or "submitted").strip()
        direction = (request.GET.get("dir") or "desc").strip()

        sort_map = {
            "id": "application_id",
            "type": "applicant_type",
            "program": "program__name",
            "status": "status",
            "started": "created_at",
            "submitted": "submitted_at",
            "email": "email",
        }

        if sort in sort_map:
            order_by_val = sort_map[sort]
            if direction == "desc":
                if isinstance(order_by_val, str):
                    order_by_val = f"-{order_by_val}"
                else:
                    order_by_val = order_by_val.desc()
            qs = qs.order_by(order_by_val)

        # Grouping logic
        # Admin actions:
        # * Review to convert to student (all "App + Signed" statuses)
        # * Review to approve application (all "Submitted" statuses)
        # Applicant actions:
        # * Waiting on forms to be signed (all "App Approved" statuses)
        # * Waiting on Parent data (all "awaiting parent")
        # * Waiting on Student data (all "Email verified")
        # * No data yet (all "Draft" statuses)

        grouped = [
            {
                "title": "Admin actions: Review to convert to student",
                "statuses": [Application.Status.APPROVED_SIGNED],
                "apps": [],
            },
            {
                "title": "Admin actions: Review to approve application",
                "statuses": [Application.Status.SUBMITTED],
                "apps": [],
            },
            {
                "title": "Applicant actions: Waiting on forms to be signed",
                "statuses": [Application.Status.APPROVED],
                "apps": [],
            },
            {
                "title": "Applicant actions: Waiting on Parent data",
                "statuses": [Application.Status.AWAITING_PARENT],
                "apps": [],
            },
            {
                "title": "Applicant actions: Waiting on Student data",
                "statuses": [Application.Status.EMAIL_VERIFIED],
                "apps": [],
            },
            {
                "title": "Applicant actions: No data yet",
                "statuses": [Application.Status.DRAFT],
                "apps": [],
            },
            {
                "title": "Other (Converted / Declined)",
                "statuses": [Application.Status.CONVERTED, Application.Status.DECLINED],
                "apps": [],
            },
        ]

        invalid_group = {
            "title": "Closed or Ended Programs (Invalid)",
            "apps": [],
            "is_invalid": True,
        }

        # Partition applications into groups
        for app in qs:
            # Applications for closed/ended programs go into the special invalid group
            if app.program and app.program.applications_are_invalid:
                invalid_group["apps"].append(app)
                continue

            found = False
            for group in grouped:
                if app.status in group["statuses"]:
                    group["apps"].append(app)
                    found = True
                    break
            if not found:
                # Fallback group if we ever add a status forgot to categorize
                if not grouped or grouped[-1]["title"] != "Other":
                    grouped.append({"title": "Other", "statuses": [], "apps": []})
                grouped[-1]["apps"].append(app)

        # Add the invalid group at the end if it has apps
        if invalid_group["apps"]:
            grouped.append(invalid_group)

        # Remove empty groups if a filter is active
        if status or applicant_type or program_id or open_only:
            grouped = [g for g in grouped if g["apps"]]

        # KPI summary metrics across applications (or filtered program)
        kpi_base = _tenant_applications(org)
        if program_id.isdigit():
            kpi_base = kpi_base.filter(program_id=int(program_id))

        kpi_pending_review = kpi_base.filter(
            status=Application.Status.SUBMITTED
        ).count()
        kpi_awaiting_signatures = kpi_base.filter(
            status=Application.Status.APPROVED
        ).count()
        kpi_ready_to_convert = kpi_base.filter(
            status=Application.Status.APPROVED_SIGNED
        ).count()
        kpi_converted = kpi_base.filter(status=Application.Status.CONVERTED).count()

        from programs.models import Program

        return render(
            request,
            self.template_name,
            {
                "grouped_applications": grouped,
                "status_choices": Application.Status.choices,
                "type_choices": Application.Type.choices,
                "programs": Program.objects.for_organization(org).order_by(
                    "-active", "name"
                ),
                "current_status": status,
                "current_type": applicant_type,
                "filter_program_id": program_id,
                "open_only": open_only,
                "current_sort": sort,
                "current_dir": direction,
                "kpi_pending_review": kpi_pending_review,
                "kpi_awaiting_signatures": kpi_awaiting_signatures,
                "kpi_ready_to_convert": kpi_ready_to_convert,
                "kpi_converted": kpi_converted,
            },
        )


class ApplicationReviewDetailView(_ReviewerRequiredMixin, View):
    """Detail page showing all collected data + action buttons."""

    template_name = "applications/review/detail.html"

    def get(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        # Merge any legacy step keys now so legacy applications (which may
        # hold both the old and current key for the same step) render each
        # section once, and the migration is persisted.
        application.normalize_step_data(save=True)
        # Build per-document status rows for the Signed Documents card.
        documents_status = []
        all_required_uploaded = True
        any_documents = False
        if application.program_id:
            from programs.models import ProgramDocument

            program_docs = ProgramDocument.objects.filter(
                program_id=application.program_id,
                program__organization=application.organization,
                is_active=True,
            ).order_by("display_order", "name")
            submissions = {
                s.document_id: s
                for s in ApplicationDocumentSubmission.objects.for_organization(
                    application.organization
                )
                .filter(
                    application=application,
                    document__program__organization=application.organization,
                )
                .select_related("document")
            }
            for doc in program_docs:
                any_documents = True
                submission = submissions.get(doc.id)
                if doc.is_required and (submission is None or not submission.file):
                    all_required_uploaded = False
                documents_status.append(
                    {
                        "document": doc,
                        "submission": submission,
                    }
                )
        # The Convert button is enabled when the application has been
        # approved and there are no required signed documents still
        # pending. APPROVED_SIGNED implies that's already true; APPROVED
        # with no required docs (or all required docs uploaded) is also
        # convertible — the auto-upgrade to APPROVED_SIGNED only fires
        # via the applicant's upload flow, so an applicant whose program
        # has no required documents would otherwise be stuck.
        can_convert = application.profile_workflow_version == 0 and (
            application.status == Application.Status.APPROVED_SIGNED
            or (
                application.status == Application.Status.APPROVED
                and all_required_uploaded
            )
        )
        proposals_ready, proposal_review_issue = proposal_review_state(application)
        can_approve = (
            application.status
            not in (
                Application.Status.APPROVED,
                Application.Status.APPROVED_SIGNED,
                Application.Status.CONVERTED,
                Application.Status.DECLINED,
            )
            and (application.is_mentor or all_required_uploaded)
            and proposals_ready
            and (
                application.profile_workflow_version == 0
                or application.status == Application.Status.SUBMITTED
            )
        )
        identity_proposals = list(
            ApplicationIdentityProposal.objects.for_organization(
                application.organization
            )
            .filter(application_id=application.pk)
            .select_related(
                "candidate_student",
                "candidate_adult",
                "target_student",
                "target_adult",
                "reviewed_by",
            )
            .order_by("subject_slot")
        )
        for proposal in identity_proposals:
            proposal.review_baseline_current = proposal.is_baseline_current()
            proposal.available_candidates = identity_candidate_options(
                application, proposal
            )
        relationship_proposals = list(
            ApplicationRelationshipProposal.objects.for_organization(
                application.organization
            )
            .filter(application_id=application.pk)
            .select_related(
                "student_profile",
                "student_draft",
                "guardian_profile",
                "guardian_draft",
                "reviewed_by",
            )
            .order_by("pk")
        )
        for proposal in relationship_proposals:
            proposal.review_baseline_current = proposal.is_baseline_current()
        workflow_student = None
        workflow_primary_guardian = None
        workflow_secondary_guardian = None
        workflow_student_draft = None
        workflow_guardian_drafts = []
        workflow_fixed_answers = []
        workflow_question_sets = []
        if application.profile_workflow_version == 1:
            workflow_student = (
                Student.all_objects.for_organization(application.organization)
                .filter(pk=application.student_profile_id)
                .first()
            )
            workflow_primary_guardian = (
                Adult.all_objects.for_organization(application.organization)
                .filter(pk=application.primary_guardian_profile_id)
                .first()
            )
            workflow_secondary_guardian = (
                Adult.all_objects.for_organization(application.organization)
                .filter(pk=application.secondary_guardian_profile_id)
                .first()
            )
            workflow_student_draft = (
                ApplicationStudentDraft.objects.for_organization(
                    application.organization
                )
                .filter(application_id=application.pk)
                .first()
            )
            workflow_guardian_drafts = list(
                ApplicationGuardianDraft.objects.for_organization(
                    application.organization
                )
                .filter(application_id=application.pk)
                .order_by("slot")
            )
            workflow_fixed_answers = list(
                ApplicationFixedAnswer.objects.for_organization(
                    application.organization
                ).filter(application_id=application.pk)
            )
            for item in workflow_fixed_answers:
                wording = EXPERIENCE_QUESTIONS.get(item.question_key)
                item.display_question = (
                    wording[application.applicant_type == Application.Type.PARENT]
                    if wording
                    else item.question_key
                )
            workflow_question_sets = list(
                ApplicationQuestionSet.objects.for_organization(
                    application.organization
                )
                .filter(application_id=application.pk)
                .prefetch_related("answers")
                .order_by("program_id")
            )
        custom_question_error = False
        data = application.data or {}
        if "step6-custom" in data:
            try:
                if not isinstance(data["step6-custom"], list):
                    raise ValueError("Custom application answers are malformed.")
                extract_experience_answers({"step6-custom": data["step6-custom"]})
            except ValueError:
                custom_question_error = True
        return render(
            request,
            self.template_name,
            {
                "application": application,
                "data": data,
                "custom_question_error": custom_question_error,
                "decline_form": DeclineForm(),
                "upload_form": StaffDocumentUploadForm(program=application.program),
                "documents_status": documents_status,
                "all_required_uploaded": all_required_uploaded,
                "any_documents": any_documents,
                "can_convert": can_convert,
                "can_approve": can_approve,
                "identity_proposals": identity_proposals,
                "relationship_proposals": relationship_proposals,
                "proposal_review_issue": proposal_review_issue,
                "can_resolve_proposals": application.profile_workflow_version == 1,
                "workflow_student": workflow_student,
                "workflow_primary_guardian": workflow_primary_guardian,
                "workflow_secondary_guardian": workflow_secondary_guardian,
                "workflow_student_draft": workflow_student_draft,
                "workflow_guardian_drafts": workflow_guardian_drafts,
                "can_edit_typed_profile_drafts": (
                    application.profile_workflow_version == 1
                    and application.status == Application.Status.SUBMITTED
                    and workflow_student_draft is not None
                ),
                "workflow_fixed_answers": workflow_fixed_answers,
                "workflow_question_sets": workflow_question_sets,
            },
        )


class ApplicationReviewDocumentDownloadView(_ReviewerRequiredMixin, View):
    """Stream a signed application document to an authorized tenant reviewer."""

    def get(self, request, app_id: str, submission_id: int):
        application = _get_tenant_application(request, app_id)
        organization = application.organization
        submission = get_object_or_404(
            ApplicationDocumentSubmission.objects.for_organization(organization).filter(
                application=application,
                document__program=application.program,
                document__program__organization=organization,
            ),
            pk=submission_id,
        )
        return application_document_file_response(submission)


class ApplicationApproveView(_ReviewerRequiredMixin, View):
    """POST: approve an application after required forms are received."""

    def _approve_transaction(self, request, application, app_id, using):
        with transaction.atomic(using=using):
            current = get_object_or_404(
                Application.objects.using(using)
                .for_organization(request.organization)
                .select_for_update()
                .filter(superseded_at__isnull=True),
                pk=application.pk,
            )
            if current.program_id:
                get_object_or_404(
                    Program.objects.using(using)
                    .for_organization(request.organization)
                    .select_for_update(),
                    pk=current.program_id,
                )
            if current.status in (
                Application.Status.APPROVED,
                Application.Status.APPROVED_SIGNED,
                Application.Status.CONVERTED,
                Application.Status.DECLINED,
            ):
                if current.status == Application.Status.DECLINED:
                    messages.info(request, "Cannot approve a declined application.")
                else:
                    messages.info(request, "Application is already approved.")
                return None, redirect(
                    "application_review_detail", app_id=current.application_id
                )

            if current.program_id:
                list(
                    ProgramDocument.objects.using(using)
                    .filter(
                        program_id=current.program_id,
                        program__organization_id=request.organization.pk,
                        is_active=True,
                        is_required=True,
                    )
                    .select_for_update()
                    .order_by("pk")
                    .values_list("pk", flat=True)
                )
            list(
                ApplicationDocumentSubmission.objects.using(using)
                .filter(
                    application_id=current.pk,
                    organization_id=request.organization.pk,
                )
                .select_for_update()
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            if not required_application_documents_complete(current):
                messages.error(
                    request,
                    "Required permission forms must be uploaded before this application can be approved.",
                )
                return None, redirect(
                    "application_review_detail", app_id=current.application_id
                )

            if current.profile_workflow_version:
                finalized, _, finalization_error = (
                    finalize_profile_workflow_application(current, request, using=using)
                )
                if not finalized:
                    messages.error(request, finalization_error)
                    return None, redirect(
                        "application_review_detail", app_id=current.application_id
                    )

            old_status = current.status
            current.status = (
                Application.Status.APPROVED_SIGNED
                if has_required_application_documents(current)
                else Application.Status.APPROVED
            )
            current.reviewed_at = timezone.now()
            current.reviewed_by = request.user
            current.decline_reason = ""
            current.current_step = max(current.current_step, 9)
            current.save(
                update_fields=[
                    "status",
                    "reviewed_at",
                    "reviewed_by",
                    "decline_reason",
                    "current_step",
                    "updated_at",
                ]
            )

            log_event(
                request=request,
                event=AuditEvent.ADMISSION_DECISION,
                resource=current,
                before={"status": old_status},
                after={"status": current.status},
                notes=f"Application approved by {request.user}.",
            )
            application = current
        return application, None

    def post(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        using = application._state.db
        try:
            application, response = self._approve_transaction(
                request, application, app_id, using
            )
        except ApplicationFinalizationError as exc:
            messages.error(request, str(exc))
            return redirect(
                "application_review_detail", app_id=application.application_id
            )
        if response is not None:
            return response

        try:
            send_application_approved_email(application, request=request)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "Failed to send approval email for %s",
                application.application_id,
            )
            messages.warning(
                request,
                "Application was approved, but the confirmation email "
                "could not be sent.",
            )
        else:
            messages.success(
                request,
                f"Approved application {application.application_id}. "
                "The applicant has been emailed.",
            )
        return redirect("application_review_detail", app_id=application.application_id)


class ApplicationDeclineView(_ReviewerRequiredMixin, View):
    """POST: mark an application DECLINED and email the applicant the reason."""

    template_name = "applications/review/decline.html"

    def _check_already_declined(self, request, application):
        """Return True if the application is already DECLINED/CONVERTED."""
        if application.status in (
            Application.Status.DECLINED,
            Application.Status.CONVERTED,
        ):
            messages.info(
                request,
                "This application has already been "
                + (
                    "converted."
                    if application.status == Application.Status.CONVERTED
                    else "declined."
                ),
            )
            return True
        return False

    def get(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        if self._check_already_declined(request, application):
            return redirect(
                "application_review_detail", app_id=application.application_id
            )
        return render(
            request,
            self.template_name,
            {"application": application, "form": DeclineForm()},
        )

    def post(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        form = DeclineForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {"application": application, "form": form},
            )
        reason = form.cleaned_data.get("reason") or ""
        using = application._state.db
        with transaction.atomic(using=using):
            application = get_object_or_404(
                Application.objects.using(using)
                .for_organization(request.organization)
                .select_for_update()
                .filter(superseded_at__isnull=True),
                pk=application.pk,
            )
            if self._check_already_declined(request, application):
                return redirect(
                    "application_review_detail", app_id=application.application_id
                )
            old_status = application.status
            application.status = Application.Status.DECLINED
            application.decline_reason = reason
            application.reviewed_at = timezone.now()
            application.reviewed_by = request.user
            application.save(
                update_fields=[
                    "status",
                    "decline_reason",
                    "reviewed_at",
                    "reviewed_by",
                    "updated_at",
                ]
            )

            log_event(
                request=request,
                event=AuditEvent.ADMISSION_DECISION,
                resource=application,
                before={"status": old_status},
                after={"status": Application.Status.DECLINED},
                notes=f"Application declined by {request.user}. Reason: {reason}",
            )

        try:
            send_application_declined_email(application, reason=reason, request=request)
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "Failed to send decline email for %s",
                application.application_id,
            )
            messages.warning(
                request,
                "Application was declined, but the notification email "
                "could not be sent.",
            )
        else:
            messages.success(
                request,
                f"Declined application {application.application_id}. "
                "The applicant has been emailed.",
            )
        return redirect("application_review_detail", app_id=application.application_id)


class ApplicationEditView(_ReviewerRequiredMixin, View):
    """Per-field edit of the captured ``data`` and primary email."""

    template_name = "applications/review/edit.html"

    def _custom_snapshot(self, application):
        data = application.data or {}
        if "step6-custom" not in data:
            return None
        snapshot = data["step6-custom"]
        if not isinstance(snapshot, list):
            raise ValueError("Custom application questions are malformed.")
        try:
            extract_experience_answers({"step6-custom": snapshot})
        except ValueError as exc:
            raise ValueError("Custom application questions are malformed.") from exc
        if any(
            "required" in item and not isinstance(item["required"], bool)
            for item in snapshot
        ):
            raise ValueError("Custom application questions are malformed.")
        if "_custom_question_program_id" in data and (
            type(data["_custom_question_program_id"]) is not int
            or data["_custom_question_program_id"] != application.program_id
        ):
            raise ValueError(
                "Custom application questions belong to a different program."
            )
        return snapshot

    @staticmethod
    def _custom_version(application):
        state = json.dumps(
            {
                "id": application.pk,
                "organization": application.organization_id,
                "program": application.program_id,
                "status": application.status,
                "email": application.email,
                "data": application.data,
                "updated_at": application.updated_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return salted_hmac(
            "application-review-custom-snapshot", state, algorithm="sha1"
        ).hexdigest()

    def _form(self, application, post_data=None):
        snapshot = None
        snapshot_error = None
        try:
            snapshot = self._custom_snapshot(application)
        except ValueError as exc:
            snapshot_error = str(exc)
        editable = application.status in (
            Application.Status.SUBMITTED,
            Application.Status.APPROVED,
            Application.Status.APPROVED_SIGNED,
        )
        initial = self._initial(application)
        initial["custom_snapshot_version"] = self._custom_version(application)
        form = ApplicationDataEditForm(
            post_data,
            sections=_sections_for(application),
            custom_snapshot=snapshot if editable else None,
            initial=initial,
        )
        form.custom_snapshot_error = snapshot_error
        form.custom_snapshot_readonly = snapshot is not None and not editable
        return form

    def _initial(self, application: Application):
        sections = _sections_for(application)
        initial = {"email": application.email}
        data = normalize_step_keys(application.data)
        for data_key, _title, fields in sections:
            step = data.get(data_key) or {}
            for spec in fields:
                fname = spec["name"]
                field_name = ApplicationDataEditForm._field_name(data_key, fname)
                step_value = step.get(fname)
                if step_value is None and fname == "legal_first_name":
                    # Legacy parent steps stored the legal first name under
                    # the plain "first_name" key.
                    step_value = step.get("first_name")
                if step_value is not None:
                    value = step_value
                    if spec["kind"] == "date":
                        try:
                            value = datetime.date.fromisoformat(str(step_value))
                        except (TypeError, ValueError):
                            value = None
                    initial[field_name] = value
                elif spec.get("initial") is not None:
                    initial[field_name] = spec["initial"]
        return initial

    def _render(self, request, application, form):
        groups = []
        for data_key, title, fields in form.sections:
            bound = [
                form[ApplicationDataEditForm._field_name(data_key, spec["name"])]
                for spec in fields
            ]
            groups.append({"title": title, "fields": bound})
        if form.custom_snapshot:
            groups.append(
                {
                    "title": "Custom application questions",
                    "fields": [
                        form[f"custom_answer_{item['question_id']}"]
                        for item in form.custom_snapshot
                    ],
                }
            )
        return render(
            request,
            self.template_name,
            {
                "application": application,
                "form": form,
                "groups": groups,
            },
        )

    def get(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        if application.profile_workflow_version != 0:
            messages.error(
                request, "Editing structured application details is not available yet."
            )
            return _proposal_review_redirect(application)
        # Persist any legacy step-key migration up front so the form (and any
        # subsequent save) always operate on the current key names.
        application.normalize_step_data(save=True)
        # Normalization may have changed ``updated_at`` while persisting the
        # merged data; sign the persisted row rather than the pre-save value.
        application.refresh_from_db()
        form = self._form(application)
        return self._render(request, application, form)

    @transaction.atomic
    def post(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        application = (
            Application.objects.for_organization(application.organization)
            .select_for_update()
            .get(pk=application.pk, superseded_at__isnull=True)
        )
        if application.profile_workflow_version != 0:
            messages.error(
                request, "Editing structured application details is not available yet."
            )
            return _proposal_review_redirect(application)
        if (
            application.program_id
            and application.program.organization_id != application.organization_id
        ):
            raise Http404
        application.normalize_step_data(save=False)
        form = self._form(application, request.POST)
        if not constant_time_compare(
            request.POST.get("custom_snapshot_version", ""),
            self._custom_version(application),
        ):
            form.is_valid()
            form.add_error(
                None,
                "The application changed after you opened this form. Your entries "
                "are still shown but were not saved. Reload this page before "
                "trying again.",
            )
            return self._render(request, application, form)
        if form.custom_snapshot_error:
            form.is_valid()
            form.add_error(None, form.custom_snapshot_error)
            return self._render(request, application, form)
        if not form.is_valid():
            return self._render(request, application, form)
        old_data = application.data
        application.data = form.rebuild_data(
            application.data, initial=self._initial(application)
        )
        if form.custom_snapshot is not None:
            application.data["step6-custom"] = [
                {
                    **item,
                    "answer": form.cleaned_data[f"custom_answer_{item['question_id']}"],
                }
                for item in form.custom_snapshot
            ]
        new_email = (form.cleaned_data.get("email") or "").strip()
        if new_email:
            application.email = new_email
        application.save()

        log_event(
            request=request,
            event=AuditEvent.CONTACT_INFO_UPDATED,
            resource=application,
            before={
                "data": old_data,
                "email": application.email if not new_email else "changed",
            },
            after={"data": application.data, "email": application.email},
            notes=f"Application data edited by {request.user}.",
        )
        messages.success(
            request,
            f"Updated application {application.application_id}.",
        )
        return redirect("application_review_detail", app_id=application.application_id)


def _typed_answer_state(application, *, lock=False):
    using = application._state.db or "default"
    fixed_answers_query = (
        ApplicationFixedAnswer._base_manager.using(using)
        .filter(application_id=application.pk)
        .order_by("pk")
    )
    question_sets_query = (
        ApplicationQuestionSet._base_manager.using(using)
        .filter(application_id=application.pk)
        .order_by("pk")
    )
    if lock:
        fixed_answers_query = fixed_answers_query.select_for_update()
        question_sets_query = question_sets_query.select_for_update()
    fixed_answers = list(fixed_answers_query)
    question_sets = list(question_sets_query)
    if any(
        item.organization_id != application.organization_id for item in fixed_answers
    ):
        raise Http404
    if any(
        item.organization_id != application.organization_id for item in question_sets
    ):
        raise Http404
    if any(
        item.program.organization_id != application.organization_id
        for item in question_sets
    ):
        raise Http404

    answers_query = (
        ApplicationQuestionAnswer._base_manager.using(using)
        .filter(question_set_id__in=[item.pk for item in question_sets])
        .order_by("question_set_id", "position", "pk")
    )
    if lock:
        answers_query = answers_query.select_for_update()
    question_answers = list(answers_query)
    if any(
        item.organization_id != application.organization_id for item in question_answers
    ):
        raise Http404

    active_sets = {
        item.pk: item
        for item in question_sets
        if item.program_id == application.program_id
    }
    active_answers = [
        answer for answer in question_answers if answer.question_set_id in active_sets
    ]
    return fixed_answers, question_sets, question_answers, active_answers


def _typed_answer_state_version(application, fixed_answers, question_sets, answers):
    state = {
        "application": {
            "id": application.pk,
            "organization_id": application.organization_id,
            "workflow_version": application.profile_workflow_version,
            "status": application.status,
            "applicant_type": application.applicant_type,
            "program_id": application.program_id,
        },
        "fixed_answers": [
            [
                item.pk,
                item.organization_id,
                item.source_step,
                item.question_key,
                item.answer,
            ]
            for item in fixed_answers
        ],
        "question_sets": [
            [item.pk, item.organization_id, item.program_id, item.frozen_at.isoformat()]
            for item in question_sets
        ],
        "question_answers": [
            [
                item.pk,
                item.organization_id,
                item.question_set_id,
                str(item.question_id),
                item.position,
                item.question_text,
                item.is_required,
                item.answer,
            ]
            for item in answers
        ],
    }
    serialized = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return salted_hmac(
        "applications.typed_answer_state", serialized, algorithm="sha1"
    ).hexdigest()


def _nullable_bool_value(value):
    if value is None:
        return ""
    return "true" if value else "false"


def _coerce_nullable_bool(value):
    return {"true": True, "false": False}.get(value)


def _nullable_bool_editor_field(label, initial):
    return forms.TypedChoiceField(
        label=label,
        choices=(
            ("", "No answer"),
            ("true", "Yes"),
            ("false", "No"),
        ),
        coerce=_coerce_nullable_bool,
        empty_value=None,
        initial=_nullable_bool_value(initial),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )


def _typed_profile_drafts(application, *, lock=False):
    using = application._state.db or "default"
    student_query = ApplicationStudentDraft._base_manager.using(using).filter(
        application_id=application.pk
    )
    guardian_query = ApplicationGuardianDraft._base_manager.using(using).filter(
        application_id=application.pk
    )
    if lock:
        student_query = student_query.select_for_update()
        guardian_query = guardian_query.select_for_update()
    student_drafts = list(student_query.order_by("pk"))
    guardian_drafts = list(guardian_query.order_by("slot", "pk"))
    if (
        len(student_drafts) != 1
        or student_drafts[0].organization_id != application.organization_id
    ):
        raise Http404
    if any(
        draft.organization_id != application.organization_id
        or draft.slot not in ApplicationGuardianDraft.Slot.values
        or draft.presence not in ApplicationGuardianDraft.Presence.values
        for draft in guardian_drafts
    ) or len({draft.slot for draft in guardian_drafts}) != len(guardian_drafts):
        raise Http404

    student_draft = student_drafts[0]
    race_links_query = (
        ApplicationStudentDraftRaceEthnicity._base_manager.using(using)
        .filter(student_draft_id=student_draft.pk)
        .order_by("pk")
    )
    if lock:
        race_links_query = race_links_query.select_for_update()
    race_links = list(race_links_query)
    if any(link.organization_id != application.organization_id for link in race_links):
        raise Http404
    race_ids = [link.race_ethnicity_id for link in race_links]
    race_query = RaceEthnicity._base_manager.using(using).filter(pk__in=race_ids)
    if lock:
        race_query = race_query.select_for_update()
    races_by_id = {race.pk: race for race in race_query}
    if any(
        race_id not in races_by_id
        or races_by_id[race_id].organization_id != application.organization_id
        for race_id in race_ids
    ):
        raise Http404
    return student_draft, guardian_drafts, race_links, races_by_id


def _typed_profile_proposals(application, *, lock=False):
    using = application._state.db or "default"
    identity_query = (
        ApplicationIdentityProposal._base_manager.using(using)
        .filter(application_id=application.pk)
        .order_by("pk")
    )
    relationship_query = (
        ApplicationRelationshipProposal._base_manager.using(using)
        .filter(application_id=application.pk)
        .order_by("pk")
    )
    if lock:
        identity_query = identity_query.select_for_update()
        relationship_query = relationship_query.select_for_update()
    identity_proposals = list(identity_query)
    relationship_proposals = list(relationship_query)
    if any(
        proposal.organization_id != application.organization_id
        for proposal in [*identity_proposals, *relationship_proposals]
    ):
        raise Http404
    return identity_proposals, relationship_proposals


def _typed_profile_state_version(
    application,
    student_draft,
    guardian_drafts,
    race_links,
    identity_proposals,
    relationship_proposals,
):
    def record_state(record):
        values = []
        for field in record._meta.concrete_fields:
            value = getattr(record, field.attname)
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            values.append([field.attname, value])
        return values

    state = {
        "application": [
            application.pk,
            application.organization_id,
            application.profile_workflow_version,
            application.program_id,
            application.status,
            application.updated_at.isoformat() if application.updated_at else None,
        ],
        "student": record_state(student_draft),
        "guardians": [record_state(draft) for draft in guardian_drafts],
        "race_ids": sorted(link.race_ethnicity_id for link in race_links),
        "identity_proposals": [
            record_state(proposal) for proposal in identity_proposals
        ],
        "relationship_proposals": [
            record_state(proposal) for proposal in relationship_proposals
        ],
    }
    serialized = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return salted_hmac(
        "applications.typed_profile_draft_state", serialized, algorithm="sha1"
    ).hexdigest()


def _typed_profile_editor_forms(
    request,
    application,
    student_draft,
    guardian_drafts,
    races_by_id,
):
    data = request.POST if request.method == "POST" else None
    using = application._state.db or "default"
    program = application.program if application.program_id else None
    student_initial = {
        "legal_first_name": student_draft.legal_first_name,
        "preferred_first_name": student_draft.preferred_first_name,
        "last_name": student_draft.last_name,
        "pronouns": student_draft.pronouns,
        "date_of_birth": student_draft.date_of_birth,
        "address": student_draft.address,
        "city": student_draft.city,
        "state": student_draft.state,
        "zip_code": student_draft.zip_code,
        "personal_email": student_draft.personal_email,
        "phone_number": student_draft.phone_number,
        "phone_type": student_draft.phone_type,
        "school_name": (
            NOT_LISTED_SCHOOL
            if student_draft.school_not_listed
            else student_draft.school_name
        ),
        "grade": student_draft.grade,
        "graduation_year": student_draft.graduation_year,
        "tshirt_size": student_draft.tshirt_size,
        "allergies": student_draft.allergies,
        "dietary_restrictions": student_draft.dietary_restrictions,
        "medical_notes": student_draft.medical_notes,
        "race_ethnicities": list(races_by_id),
    }
    nullable_student_fields = {
        "confirm_age": "Confirm date of birth",
        "can_receive_texts": "Can receive texts?",
        "directory_consent": "Directory consent",
        "confirm_grade": "Confirm grade",
        "allergies_answered": "Allergies answered",
        "dietary_restrictions_answered": "Dietary restrictions answered",
        "medical_notes_answered": "Medical notes answered",
    }
    for field_name in nullable_student_fields:
        student_initial[field_name] = _nullable_bool_value(
            getattr(student_draft, field_name)
        )

    student_form = StudentInfoForm(
        data=data,
        initial=student_initial,
        program_start_date=program.start_date if program else None,
        tshirt_enabled=bool(program and program.has_feature("tshirt-size")),
        prefix="student",
    )
    for field_name, label in nullable_student_fields.items():
        student_form.fields[field_name] = _nullable_bool_editor_field(
            label, getattr(student_draft, field_name)
        )
    graduation_year_min = student_form.fields["graduation_year"].min_value
    if student_draft.graduation_year is not None and graduation_year_min is not None:
        graduation_year_min = min(graduation_year_min, student_draft.graduation_year)
        student_form.fields["graduation_year"].min_value = graduation_year_min
        for validator in student_form.fields["graduation_year"].validators:
            if getattr(validator, "code", None) == "min_value":
                validator.limit_value = graduation_year_min
    student_form.fields["school_name"].choices = [
        ("", "---"),
        *[
            (school.name, school.name)
            for school in School._base_manager.using(using)
            .filter(organization_id=application.organization_id)
            .order_by("name")
        ],
        (NOT_LISTED_SCHOOL, "My school isn't listed"),
    ]
    current_school = student_initial["school_name"]
    if (
        current_school
        and current_school != NOT_LISTED_SCHOOL
        and not any(
            value == current_school
            for value, _label in student_form.fields["school_name"].choices
        )
    ):
        student_form.fields["school_name"].choices.insert(
            1, (current_school, current_school)
        )
    student_form.fields["race_ethnicities"].queryset = (
        RaceEthnicity._base_manager.using(using)
        .filter(organization_id=application.organization_id)
        .order_by("name")
    )

    guardian_by_slot = {draft.slot: draft for draft in guardian_drafts}
    guardian_forms = []
    for slot in (
        ApplicationGuardianDraft.Slot.PRIMARY,
        ApplicationGuardianDraft.Slot.SECONDARY,
    ):
        draft = guardian_by_slot.get(slot)
        if draft is None or draft.presence != ApplicationGuardianDraft.Presence.PRESENT:
            guardian_forms.append(
                {
                    "draft": draft,
                    "slot": slot,
                    "label": (
                        "Primary guardian"
                        if slot == ApplicationGuardianDraft.Slot.PRIMARY
                        else "Secondary guardian"
                    ),
                    "form": None,
                }
            )
            continue
        form_initial = {
            field_name: getattr(draft, field_name)
            for field_name in (
                "legal_first_name",
                "preferred_first_name",
                "last_name",
                "pronouns",
                "relationship_to_student",
                "specific_relationship",
                "email",
                "address",
                "city",
                "state",
                "zip_code",
                "phone_number",
                "phone_type",
            )
        }
        form_initial["can_receive_texts"] = _nullable_bool_value(
            draft.can_receive_texts
        )
        form_initial["email_updates"] = _nullable_bool_value(draft.email_updates)
        guardian_form = ParentInfoForm(
            data,
            initial=form_initial,
            require_email=False,
            require_address=False,
            student_emails=[],
            other_parent_email=None,
            prefix=f"guardian_{slot}",
        )
        guardian_form.fields["can_receive_texts"] = _nullable_bool_editor_field(
            "Can receive texts?", draft.can_receive_texts
        )
        guardian_form.fields["email_updates"] = _nullable_bool_editor_field(
            "Receive email updates", draft.email_updates
        )
        guardian_forms.append(
            {
                "draft": draft,
                "slot": slot,
                "label": (
                    "Primary guardian"
                    if slot == ApplicationGuardianDraft.Slot.PRIMARY
                    else "Secondary guardian"
                ),
                "form": guardian_form,
            }
        )
    return student_form, guardian_forms


def _typed_profile_forms_valid(student_form, guardian_forms, application, program):
    valid = student_form.is_valid()
    for entry in guardian_forms:
        if entry["form"] is not None:
            valid = entry["form"].is_valid() and valid
    if not valid:
        return False

    dob = student_form.cleaned_data["date_of_birth"]
    today = timezone.localdate()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age < 5 or age == 18:
        if student_form.cleaned_data.get("confirm_age") is not True:
            student_form.add_error(
                "confirm_age",
                "Confirm that this birthdate is correct before saving the application details.",
            )
            valid = False

    if (
        program
        and program.grade_range_start is not None
        and program.grade_range_end is not None
    ):
        grade = int(student_form.cleaned_data["grade"])
        if not program.grade_range_start <= grade <= program.grade_range_end:
            if student_form.cleaned_data.get("confirm_grade") is not True:
                student_form.add_error(
                    "confirm_grade",
                    "Confirm that this grade is correct before saving the application details.",
                )
                valid = False
    return valid


def _typed_profile_emails_valid(student_form, guardian_forms, student_draft):
    entries = [
        (
            student_form,
            "personal_email",
            student_draft.personal_email,
            student_form.cleaned_data.get("personal_email", ""),
        )
    ]
    for entry in guardian_forms:
        if entry["form"] is None:
            continue
        form = entry["form"]
        entries.append(
            (
                form,
                "email",
                entry["draft"].email,
                form.cleaned_data.get("email", ""),
            )
        )

    def normalized(email):
        return email.strip().lower() if isinstance(email, str) else ""

    valid = True
    for index, (left_form, left_field, left_old, left_new) in enumerate(entries):
        left_key = normalized(left_new)
        if not left_key:
            continue
        for right_form, right_field, right_old, right_new in entries[index + 1 :]:
            if left_key != normalized(right_new):
                continue
            if normalized(left_old) == left_key and normalized(right_old) == left_key:
                continue
            message = "Student and guardian contact emails must be different."
            left_form.add_error(left_field, message)
            right_form.add_error(right_field, message)
            valid = False
    return valid


def _save_typed_profile_form(draft, cleaned_data, using):
    values = {
        "legal_first_name": cleaned_data["legal_first_name"],
        "preferred_first_name": cleaned_data.get("preferred_first_name", ""),
        "last_name": cleaned_data["last_name"],
        "pronouns": cleaned_data.get("pronouns", ""),
        "address": cleaned_data.get("address", ""),
        "city": cleaned_data.get("city", ""),
        "state": cleaned_data.get("state", ""),
        "zip_code": cleaned_data.get("zip_code", ""),
        "phone_number": cleaned_data.get("phone_number", ""),
        "phone_type": cleaned_data.get("phone_type", ""),
        "can_receive_texts": cleaned_data.get("can_receive_texts"),
    }
    if isinstance(draft, ApplicationStudentDraft):
        school_name = cleaned_data["school_name"]
        not_listed = school_name == NOT_LISTED_SCHOOL
        grade = int(cleaned_data["grade"])
        program_start = (
            draft.application.program.start_date
            if draft.application.program_id
            else None
        )
        values.update(
            {
                "date_of_birth": cleaned_data["date_of_birth"],
                "confirm_age": cleaned_data.get("confirm_age"),
                "personal_email": cleaned_data.get("personal_email", ""),
                "directory_consent": cleaned_data.get("directory_consent"),
                "school_name": "" if not_listed else school_name,
                "school_not_listed": not_listed,
                "grade": grade,
                "confirm_grade": cleaned_data.get("confirm_grade"),
                "graduation_year": cleaned_data.get("graduation_year")
                or calculate_graduation_year(grade, program_start),
                "tshirt_size": cleaned_data.get("tshirt_size", draft.tshirt_size),
                "allergies": cleaned_data.get("allergies", ""),
                "dietary_restrictions": cleaned_data.get("dietary_restrictions", ""),
                "medical_notes": cleaned_data.get("medical_notes", ""),
                "allergies_answered": cleaned_data.get("allergies_answered"),
                "dietary_restrictions_answered": cleaned_data.get(
                    "dietary_restrictions_answered"
                ),
                "medical_notes_answered": cleaned_data.get("medical_notes_answered"),
            }
        )
    else:
        values.update(
            {
                "relationship_to_student": cleaned_data["relationship_to_student"],
                "specific_relationship": cleaned_data.get("specific_relationship", ""),
                "email": cleaned_data.get("email", ""),
                "email_updates": cleaned_data.get("email_updates"),
            }
        )
    for field_name, value in values.items():
        if value == "" and getattr(draft, field_name) is None:
            values[field_name] = None
    changed_fields = [
        field_name
        for field_name, value in values.items()
        if getattr(draft, field_name) != value
    ]
    if changed_fields:
        for field_name in changed_fields:
            setattr(draft, field_name, values[field_name])
        draft.save(using=using, update_fields=changed_fields)
    return changed_fields


class ApplicationTypedProfileDraftEditView(_ReviewerRequiredMixin, View):
    template_name = "applications/review/edit_typed_profile_drafts.html"

    def _get_application(self, request, app_id):
        application = _get_tenant_application(request, app_id)
        if application.profile_workflow_version != 1:
            raise Http404
        if application.status != Application.Status.SUBMITTED:
            messages.error(
                request,
                "Typed application details can only be edited while the application awaits review.",
            )
            return None
        return application

    def _render(
        self, request, application, student_draft, guardian_drafts, races_by_id, version
    ):
        student_form, guardian_forms = _typed_profile_editor_forms(
            request, application, student_draft, guardian_drafts, races_by_id
        )
        return render(
            request,
            self.template_name,
            {
                "application": application,
                "student_form": student_form,
                "guardian_forms": guardian_forms,
                "state_version": version,
                "student_draft": student_draft,
            },
        )

    def get(self, request, app_id):
        application = self._get_application(request, app_id)
        if application is None:
            return _proposal_review_redirect(_get_tenant_application(request, app_id))
        identity_proposals, relationship_proposals = _typed_profile_proposals(
            application
        )
        student_draft, guardian_drafts, race_links, races_by_id = _typed_profile_drafts(
            application
        )
        version = _typed_profile_state_version(
            application,
            student_draft,
            guardian_drafts,
            race_links,
            identity_proposals,
            relationship_proposals,
        )
        return self._render(
            request, application, student_draft, guardian_drafts, races_by_id, version
        )

    @transaction.atomic
    def post(self, request, app_id):
        application = _get_tenant_application(request, app_id)
        using = application._state.db or "default"
        current = _lock_review_application(request, app_id, using)
        if current.profile_workflow_version != 1:
            raise Http404
        if current.status != Application.Status.SUBMITTED:
            messages.error(
                request,
                "Typed application details can only be edited while the application awaits review.",
            )
            return _proposal_review_redirect(current)

        identity_proposals, relationship_proposals = _typed_profile_proposals(
            current, lock=True
        )
        student_draft, guardian_drafts, race_links, races_by_id = _typed_profile_drafts(
            current, lock=True
        )

        version = _typed_profile_state_version(
            current,
            student_draft,
            guardian_drafts,
            race_links,
            identity_proposals,
            relationship_proposals,
        )
        student_form, guardian_forms = _typed_profile_editor_forms(
            request, current, student_draft, guardian_drafts, races_by_id
        )
        forms_valid = _typed_profile_forms_valid(
            student_form,
            guardian_forms,
            current,
            current.program if current.program_id else None,
        )
        if forms_valid:
            forms_valid = _typed_profile_emails_valid(
                student_form, guardian_forms, student_draft
            )
        if not constant_time_compare(request.POST.get("state_version", ""), version):
            student_form.add_error(
                None,
                "The application or its typed details changed after you opened this page. "
                "Your entries remain here but were not saved; reload before trying again.",
            )
            return render(
                request,
                self.template_name,
                {
                    "application": current,
                    "student_form": student_form,
                    "guardian_forms": guardian_forms,
                    "state_version": version,
                    "student_draft": student_draft,
                },
            )
        if not forms_valid:
            return render(
                request,
                self.template_name,
                {
                    "application": current,
                    "student_form": student_form,
                    "guardian_forms": guardian_forms,
                    "state_version": version,
                    "student_draft": student_draft,
                },
            )
        if any(not item.is_baseline_current() for item in identity_proposals):
            student_form.add_error(
                None,
                "An identity review decision is stale. Resolve it before editing these details.",
            )
            return render(
                request,
                self.template_name,
                {
                    "application": current,
                    "student_form": student_form,
                    "guardian_forms": guardian_forms,
                    "state_version": version,
                    "student_draft": student_draft,
                },
            )
        if any(not item.is_baseline_current() for item in relationship_proposals):
            student_form.add_error(
                None,
                "A relationship review decision is stale. Resolve it before editing these details.",
            )
            return render(
                request,
                self.template_name,
                {
                    "application": current,
                    "student_form": student_form,
                    "guardian_forms": guardian_forms,
                    "state_version": version,
                    "student_draft": student_draft,
                },
            )

        old_revisions = {
            student_draft.pk: student_draft.revision,
            **{draft.pk: draft.revision for draft in guardian_drafts},
        }
        student_changes = _save_typed_profile_form(
            student_draft, student_form.cleaned_data, using
        )
        new_race_ids = set(
            student_form.cleaned_data["race_ethnicities"].values_list("pk", flat=True)
        )
        old_race_ids = {link.race_ethnicity_id for link in race_links}
        if new_race_ids != old_race_ids:
            if not student_changes:
                student_draft.save(using=using, update_fields=["school_name"])
            ApplicationStudentDraftRaceEthnicity._base_manager.using(using).filter(
                student_draft_id=student_draft.pk
            ).delete()
            for race_id in sorted(new_race_ids):
                ApplicationStudentDraftRaceEthnicity.objects.using(using).create(
                    organization_id=current.organization_id,
                    student_draft=student_draft,
                    race_ethnicity_id=race_id,
                )

        guardian_changes = []
        guardian_by_pk = {draft.pk: draft for draft in guardian_drafts}
        for entry in guardian_forms:
            guardian_form = entry["form"]
            if guardian_form is None:
                continue
            draft = guardian_by_pk[entry["draft"].pk]
            changed = _save_typed_profile_form(draft, guardian_form.cleaned_data, using)
            if changed:
                guardian_changes.append(draft)

        changed = bool(
            student_changes or new_race_ids != old_race_ids or guardian_changes
        )
        if not changed:
            messages.info(request, "No typed application details were changed.")
            return _proposal_review_redirect(current)

        current.save(using=using, update_fields=["updated_at"])
        for proposal in identity_proposals:
            proposal.match_state = ApplicationIdentityProposal.MatchState.AMBIGUOUS
            proposal.candidate_student = None
            proposal.candidate_adult = None
            proposal.candidate_baseline_revision = None
            proposal.target_student = None
            proposal.target_adult = None
            proposal.target_baseline_revision = None
            proposal.decision = ApplicationIdentityProposal.Decision.PENDING
            proposal.reviewed_by = None
            proposal.reviewed_at = None
            proposal.baseline_application_updated_at = current.updated_at
            proposal.save(
                using=using,
                update_fields=[
                    "match_state",
                    "candidate_student",
                    "candidate_adult",
                    "candidate_baseline_revision",
                    "target_student",
                    "target_adult",
                    "target_baseline_revision",
                    "decision",
                    "reviewed_by",
                    "reviewed_at",
                    "baseline_application_updated_at",
                    "updated_at",
                ],
            )
        current_drafts = {student_draft.pk: student_draft, **guardian_by_pk}
        for proposal in relationship_proposals:
            proposal.decision = ApplicationRelationshipProposal.Decision.PENDING
            proposal.reviewed_by = None
            proposal.reviewed_at = None
            proposal.baseline_application_updated_at = current.updated_at
            if proposal.student_draft_id:
                target = current_drafts.get(proposal.student_draft_id)
                if target is None:
                    raise Http404
                proposal.baseline_student_draft_revision = target.revision
            if proposal.guardian_draft_id:
                target = current_drafts.get(proposal.guardian_draft_id)
                if target is None:
                    raise Http404
                proposal.baseline_guardian_draft_revision = target.revision
            proposal.save(
                using=using,
                update_fields=[
                    "decision",
                    "reviewed_by",
                    "reviewed_at",
                    "baseline_application_updated_at",
                    "baseline_student_draft_revision",
                    "baseline_guardian_draft_revision",
                    "updated_at",
                ],
            )

        log_event(
            request=request,
            event=AuditEvent.CONTACT_INFO_UPDATED,
            resource=current,
            before={"draft_revisions": old_revisions},
            after={
                "draft_revisions": {
                    student_draft.pk: student_draft.revision,
                    **{draft.pk: draft.revision for draft in guardian_drafts},
                },
                "identity_proposals_reset": len(identity_proposals),
                "relationship_proposals_reset": len(relationship_proposals),
            },
            notes="Typed application draft details updated; dependent review decisions were reset.",
        )
        messages.success(
            request,
            "Typed application details were saved. Identity and relationship proposals must be reviewed again.",
        )
        return _proposal_review_redirect(current)


class ApplicationTypedAnswerEditView(_ReviewerRequiredMixin, View):
    template_name = "applications/review/edit_typed_answers.html"

    def _get_application(self, request, app_id):
        application = _get_tenant_application(request, app_id)
        if application.profile_workflow_version != 1:
            raise Http404
        if application.status != Application.Status.SUBMITTED:
            messages.error(
                request,
                "Typed application answers can only be edited while the application is awaiting review.",
            )
            return None
        return application

    def _render(self, request, application, form):
        return render(
            request,
            self.template_name,
            {"application": application, "form": form},
        )

    def get(self, request, app_id: str):
        application = self._get_application(request, app_id)
        if application is None:
            return _proposal_review_redirect(_get_tenant_application(request, app_id))
        fixed_answers, question_sets, answers, active_answers = _typed_answer_state(
            application
        )
        version = _typed_answer_state_version(
            application, fixed_answers, question_sets, answers
        )
        form = ApplicationTypedAnswerEditForm(
            application=application,
            fixed_answers=fixed_answers,
            custom_answers=active_answers,
            state_version=version,
        )
        return self._render(request, application, form)

    @transaction.atomic
    def post(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        application = (
            Application.objects.for_organization(application.organization)
            .select_for_update()
            .get(pk=application.pk, superseded_at__isnull=True)
        )
        if application.profile_workflow_version != 1:
            raise Http404
        if application.status != Application.Status.SUBMITTED:
            messages.error(
                request,
                "Typed application answers can only be edited while the application is awaiting review.",
            )
            return _proposal_review_redirect(application)

        fixed_answers, question_sets, answers, active_answers = _typed_answer_state(
            application, lock=True
        )
        version = _typed_answer_state_version(
            application, fixed_answers, question_sets, answers
        )
        form = ApplicationTypedAnswerEditForm(
            request.POST,
            application=application,
            fixed_answers=fixed_answers,
            custom_answers=active_answers,
            state_version=version,
        )
        if not constant_time_compare(request.POST.get("state_version", ""), version):
            form.is_valid()
            form.add_error(
                None,
                "The application or its answers changed after you opened this page. "
                "Your entries remain here but were not saved; reload before trying again.",
            )
            return self._render(request, application, form)
        if not form.is_valid():
            return self._render(request, application, form)

        changed_fixed = []
        changed_custom = []
        for answer, field_name in form.answer_fields:
            new_answer = form.cleaned_data[field_name] or ""
            if (answer.answer or "") == new_answer:
                continue
            old_value = answer.answer or ""
            answer.answer = new_answer
            answer.save(using=application._state.db, update_fields=["answer"])
            if isinstance(answer, ApplicationFixedAnswer):
                changed_fixed.append((answer.pk, old_value, new_answer))
            else:
                changed_custom.append((answer.pk, old_value, new_answer))

        if changed_fixed or changed_custom:
            log_event(
                request=request,
                event=AuditEvent.CONTACT_INFO_UPDATED,
                resource=application,
                before={
                    "fixed_answer_ids": [item[0] for item in changed_fixed],
                    "custom_answer_ids": [item[0] for item in changed_custom],
                },
                after={
                    "fixed_answer_ids": [item[0] for item in changed_fixed],
                    "custom_answer_ids": [item[0] for item in changed_custom],
                },
                notes=(
                    f"Typed application answers edited by {request.user}; "
                    f"{len(changed_fixed)} fixed and {len(changed_custom)} custom answers changed."
                ),
            )
            messages.success(request, "Updated the typed application answers.")
        else:
            messages.info(request, "No typed application answers were changed.")
        return redirect("application_review_detail", app_id=application.application_id)


class ApplicationDeleteView(_ReviewerRequiredMixin, View):
    """Two-step delete: GET shows confirmation, POST performs the delete."""

    template_name = "applications/review/delete.html"

    def get(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        return render(request, self.template_name, {"application": application})

    def post(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        using = application._state.db
        with transaction.atomic(using=using):
            application = get_object_or_404(
                Application.objects.using(using)
                .for_organization(application.organization)
                .select_for_update()
                .filter(superseded_at__isnull=True),
                pk=application.pk,
            )
            if _has_foreign_document_submissions(application, application.organization):
                messages.error(
                    request,
                    "This application cannot be deleted while it has records owned by another organization.",
                )
                return redirect(
                    "application_review_detail", app_id=application.application_id
                )
            app_str = application.application_id
            profile_ids = (
                (Student, application.student_profile_id),
                (Adult, application.primary_guardian_profile_id),
                (Adult, application.secondary_guardian_profile_id),
            )
            log_event(
                event=AuditEvent.RECORD_DELETED,
                resource=application,
                request=request,
                before={
                    "application_id": app_str,
                    "status": application.status,
                    "email": application.email,
                },
                notes=f"Application {app_str} deleted by reviewer.",
            )
            application.delete()
            delete_orphaned_incomplete_profiles(application, profile_ids, using=using)
        messages.success(request, f"Deleted application {app_str}.")
        return redirect("application_review_list")


class ApplicationCleanupView(_ReviewerRequiredMixin, View):
    """POST: Delete all applications older than 30 days."""

    def post(self, request):
        organization = getattr(request, "organization", None)
        cutoff = timezone.now() - timezone.timedelta(days=30)
        foreign_submission_app_ids = ApplicationDocumentSubmission.objects.exclude(
            organization=organization,
            document__program__organization=organization,
        ).values_list("application_id", flat=True)
        stale_apps = (
            _tenant_applications(organization)
            .filter(created_at__lt=cutoff)
            .exclude(pk__in=foreign_submission_app_ids)
        )
        count = stale_apps.count()
        sample = stale_apps.first()
        profile_ids = list(
            stale_apps.values_list(
                "student_profile_id",
                "primary_guardian_profile_id",
                "secondary_guardian_profile_id",
            )
        )
        if sample is not None:
            log_event(
                event=AuditEvent.RECORD_DELETED,
                resource=sample,
                request=request,
                before={
                    "count": count,
                    "cutoff": cutoff.isoformat(),
                    "statuses": sorted(
                        stale_apps.values_list("status", flat=True).distinct()
                    ),
                },
                notes=(
                    f"Bulk cleanup: {count} application(s) older than 30 days deleted."
                ),
            )
        stale_apps.delete()
        if profile_ids and sample is not None:
            orphan_candidates = tuple(
                pair
                for student_pk, primary_pk, secondary_pk in profile_ids
                for pair in (
                    (Student, student_pk),
                    (Adult, primary_pk),
                    (Adult, secondary_pk),
                )
            )
            delete_orphaned_incomplete_profiles(sample, orphan_candidates)

        if count > 0:
            messages.success(
                request, f"Deleted {count} applications older than 30 days."
            )
        else:
            messages.info(request, "No applications older than 30 days were found.")

        return redirect("application_review_list")


class ApplicationResendEmailView(_ReviewerRequiredMixin, View):
    """POST: resend various system emails (OTP, handoff, etc.)"""

    def post(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        email_type = request.POST.get("type")

        try:
            self._handle_resend(request, application, email_type)
        except Exception:
            logger.exception(
                "Failed to resend %s email for %s",
                email_type,
                application.application_id,
            )
            messages.error(
                request, f"Failed to resend {email_type} email. Please check logs."
            )

        return redirect("application_review_detail", app_id=application.application_id)

    def _handle_resend(self, request, application: Application, email_type: str):
        if email_type == "otp":
            code = application.issue_otp()
            send_otp_email(application, code, request=request)
            messages.success(
                request, f"Resent OTP verification email to {application.email}."
            )
        elif email_type == "handoff":
            parent_email = get_primary_parent_email(application)
            if not parent_email:
                messages.error(
                    request,
                    "No parent email address found. "
                    "This application might not have reached the handoff step yet.",
                )
            else:
                send_parent_handoff_email(application, parent_email, request=request)
                messages.success(
                    request, f"Resent parent handoff email to {parent_email}."
                )
        elif email_type == "submitted":
            if not application.submitted_at:
                messages.error(request, "This application has not been submitted yet.")
            else:
                send_application_submitted_email(application, request=request)
                messages.success(request, "Resent submission confirmation email.")
        elif email_type == "approved":
            if application.status not in (
                Application.Status.APPROVED,
                Application.Status.APPROVED_SIGNED,
                Application.Status.CONVERTED,
            ):
                messages.error(request, "This application has not been approved yet.")
            else:
                try:
                    send_application_approved_email(application, request=request)
                except ValueError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(request, "Resent approval email.")
        elif email_type == "declined":
            if application.status != Application.Status.DECLINED:
                messages.error(request, "This application has not been declined yet.")
            else:
                send_application_declined_email(
                    application, application.decline_reason, request=request
                )
                messages.success(request, "Resent decline email.")
        elif email_type == "converted":
            if application.status != Application.Status.CONVERTED:
                messages.error(request, "This application has not been converted yet.")
            else:
                send_application_converted_email(application, request=request)
                messages.success(request, "Resent conversion enrollment email.")
        else:
            messages.error(request, f"Unknown email type: {email_type}")


class ApplicationConvertView(_ReviewerRequiredMixin, View):
    """POST: convert an APPROVED_SIGNED application into a real Student.

    Creates / updates Student + Adult records from the captured wizard
    data, enrolls the new (or existing) Student in the application's
    program, and flips the application status to CONVERTED.
    """

    def post(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        if application.profile_workflow_version != 0:
            messages.error(
                request,
                "Profile-workflow applications are finalized during approval and cannot use legacy conversion.",
            )
            return redirect(
                "application_review_detail", app_id=application.application_id
            )
        if application.status == Application.Status.CONVERTED:
            messages.info(
                request,
                "This application has already been converted to a student.",
            )
            return redirect(
                "application_review_detail", app_id=application.application_id
            )
        try:
            student = convert_application_to_student(application, request=request)
        except ApplicationConversionError as exc:
            messages.error(request, str(exc))
            return redirect(
                "application_review_detail", app_id=application.application_id
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "Failed to convert application %s", application.application_id
            )
            messages.error(
                request,
                "Something went wrong converting this application. "
                "Please check the data and try again.",
            )
            return redirect(
                "application_review_detail", app_id=application.application_id
            )
        if application.applicant_type == Application.Type.MENTOR:
            messages.success(
                request,
                f"Converted application {application.application_id} into "
                f"mentor “{student}”.",
            )
        else:
            messages.success(
                request,
                f"Converted application {application.application_id} into "
                f"student “{student}” enrolled in {application.program}.",
            )
        return redirect("application_review_detail", app_id=application.application_id)


class ApplicationEmailView(_ReviewerRequiredMixin, View):
    """Bulk email messaging for applicants by status and program."""

    template_name = "applications/review/email_form.html"

    def get(self, request):
        organization = getattr(request, "organization", None)
        if organization is None:
            raise Http404
        form = ApplicationEmailForm(organization=organization)
        return self._render(form)

    def post(self, request):
        organization = getattr(request, "organization", None)
        if organization is None:
            raise Http404
        form = ApplicationEmailForm(request.POST, organization=organization)
        if form.is_valid():
            prog = form.cleaned_data.get("program")
            statuses = form.cleaned_data["statuses"]
            subject = form.cleaned_data["subject"]
            html_body = form.cleaned_data["body"]
            # Sanitize + normalize the fragment, wrap it in an email document
            # and inline CSS so the message renders the way it looked in the
            # compose editor, with a readable plain-text alternative.
            inlined_html_body, text_body, inline_images = build_email_parts(
                html_body,
                organization=organization,
                subject=subject,
            )
            test_email = form.cleaned_data.get("test_email")

            apps = _tenant_applications(organization).filter(status__in=statuses)
            if prog:
                apps = apps.filter(program=prog, program__organization=organization)

            recipients = set()
            for app in apps:
                for addr in _collect_applicant_recipients(app):
                    recipients.add(addr)

            if not recipients and not test_email:
                messages.error(
                    request, "No recipients found for the selected criteria."
                )
                return self._render(form)

            to_send = [test_email] if test_email else sorted(recipients)

            # Determine sender account and SMTP connection
            selected = form.cleaned_data.get("from_account")
            connection, from_email, reply_to = get_sender_connection(
                selected, organization=organization
            )
            from programs.utils.notifications import _mark_delivery_tracking

            messages_to_send = []
            for addr in to_send:
                msg = EmailMultiAlternatives(
                    subject=subject,
                    body=text_body,
                    from_email=from_email,
                    to=[addr],
                    reply_to=[reply_to] if reply_to else None,
                    connection=connection,
                )
                msg.attach_alternative(inlined_html_body, "text/html")
                for inline_img in inline_images:
                    msg.attach(inline_img.to_mime())
                _mark_delivery_tracking(
                    msg,
                    organization,
                    enabled=not test_email,
                    history_category="application_message",
                    history_initiated_by=request.user,
                    retain_history_body=True,
                )
                messages_to_send.append(msg)

            try:
                send_messages_in_background(messages_to_send, connection=connection)
                messages.success(
                    request,
                    f"Successfully sent email to {len(to_send)} recipient(s).",
                )
                return redirect("application_review_list")
            except Exception as e:
                logger.error(
                    "Bulk application email dispatch failed (%s).",
                    type(e).__name__,
                )
                messages.error(request, f"Failed to send email: {e}")

        return self._render(form)

    def _render(self, form):
        return render(self.request, self.template_name, {"form": form})


class ApplicationStaffDocumentUploadView(_ReviewerRequiredMixin, View):
    """POST: lead mentor uploads a signed document on behalf of an applicant.

    This allows paper copies received in person to be attached to the
    application so the reviewer can mark it as approved + signed.
    """

    http_method_names = ["post"]

    def post(self, request, app_id: str):
        application = _get_tenant_application(request, app_id)
        organization = application.organization
        using = application._state.db
        try:
            with transaction.atomic(using=using):
                current = get_object_or_404(
                    Application.objects.using(using)
                    .for_organization(organization)
                    .select_for_update()
                    .filter(superseded_at__isnull=True),
                    pk=application.pk,
                )
                if (
                    not current.program_id
                    or not Program.objects.filter(
                        pk=current.program_id, organization=organization
                    ).exists()
                ):
                    raise Http404
                form = StaffDocumentUploadForm(
                    request.POST, request.FILES, program=current.program
                )
                if not form.is_valid():
                    messages.error(
                        request,
                        "Please select a document and choose a file to upload.",
                    )
                    return redirect(
                        "application_review_detail",
                        app_id=current.application_id,
                    )

                document = form.cleaned_data["document"]
                if document.program.organization_id != organization.pk:
                    messages.error(
                        request,
                        "This document does not belong to the application's organization.",
                    )
                    return redirect(
                        "application_review_detail",
                        app_id=current.application_id,
                    )
                submission, created = (
                    ApplicationDocumentSubmission.objects.get_or_create(
                        application=current,
                        document=document,
                        defaults={"organization": organization},
                    )
                )
                if not created and submission.organization_id != organization.pk:
                    messages.error(
                        request,
                        "This document submission has inconsistent organization ownership and cannot be changed.",
                    )
                    return redirect(
                        "application_review_detail",
                        app_id=current.application_id,
                    )
                submission.file = form.cleaned_data["file"]
                submission.save()

                if (
                    current.status == Application.Status.APPROVED
                    and has_required_application_documents(current)
                    and required_application_documents_complete(current)
                ):
                    current.status = Application.Status.APPROVED_SIGNED
                    current.save(update_fields=["status", "updated_at"])
                application = current
        except SuspiciousFileOperation:
            messages.error(
                request,
                "The filename of your uploaded document is too long or "
                "contains invalid characters. Please rename the file and "
                "try again.",
            )
            return redirect(
                "application_review_detail", app_id=application.application_id
            )

        messages.success(
            request,
            f'Uploaded signed copy of "{document.name}" for '
            f"{application.application_id}.",
        )
        return redirect("application_review_detail", app_id=application.application_id)
