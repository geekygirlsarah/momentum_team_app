"""Forms for Platform Administration."""

from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.events import AuditEvent
from organizations.models import (
    SHARED_ORGANIZATION_SETTING_KEYS,
    Organization,
    OrganizationSettings,
    UserOrganizationMembership,
)
from organizations.utils import (
    is_valid_tenant_label,
    validate_iana_timezone,
)
from programs.models import ProgramFeature


class PlatformOrganizationForm(forms.ModelForm):
    """Form to create and edit organizations along with their OrganizationSettings."""

    # OrganizationSettings fields
    portal_title = forms.CharField(
        max_length=150,
        required=False,
        help_text="Custom title for this organization's portal.",
    )
    footer_tagline = forms.CharField(
        max_length=255,
        required=False,
        help_text="Custom footer text (e.g. 'is an outreach program of CMU').",
    )
    contact_email = forms.EmailField(
        required=False,
        help_text="Public contact email for inquiries and support.",
    )
    phone = forms.CharField(
        max_length=30,
        required=False,
        help_text="Contact telephone number.",
    )
    time_zone = forms.CharField(
        max_length=50,
        initial=timezone.get_default_timezone_name(),
        help_text="Primary time zone (e.g. 'America/New_York', 'America/Chicago').",
    )
    theme_color = forms.CharField(
        max_length=20,
        initial="#0056b3",
        widget=forms.TextInput(attrs={"type": "color"}),
        help_text="Primary branding/theme color.",
    )
    allowed_domains = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
        help_text="Allowed email domains for auto-matching, one per line or comma-separated.",
    )
    org_account_enabled = forms.BooleanField(
        required=False,
        initial=False,
        help_text="Enable organization/school email accounts and identifiers.",
    )
    org_account_name = forms.CharField(
        max_length=100,
        required=False,
        initial="",
        help_text="Display name for organization or school accounts (e.g. 'School Account').",
    )
    org_email_domain = forms.CharField(
        max_length=255,
        required=False,
        initial="",
        help_text="Primary email domain(s) for organization accounts.",
    )
    force_mentor_org_email = forms.BooleanField(
        required=False,
        initial=False,
        help_text="Require mentors to log in using an email matching the organization domain.",
    )
    force_student_org_email = forms.BooleanField(
        required=False,
        initial=False,
        help_text="Require students to log in using an email matching the organization domain.",
    )
    force_parent_org_email = forms.BooleanField(
        required=False,
        initial=False,
        help_text="Require parents to log in using an email matching the organization domain.",
    )
    force_alumni_org_email = forms.BooleanField(
        required=False,
        initial=False,
        help_text="Require alumni to log in using an email matching the organization domain.",
    )

    # Initial Lead Mentor (Creation only)
    initial_lead_mentor_email = forms.EmailField(
        required=False,
        help_text="Email of user to assign/invite as initial Lead Mentor for this organization.",
    )

    class Meta:
        model = Organization
        fields = ["name", "slug", "subdomain", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. Steel City Robotics"}),
            "slug": forms.TextInput(attrs={"placeholder": "e.g. steel-city"}),
            "subdomain": forms.TextInput(attrs={"placeholder": "e.g. steelcity"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            organization_data = (
                dict(self.instance.settings)
                if isinstance(self.instance.settings, dict)
                else {}
            )
            structured_data = (
                OrganizationSettings.objects.filter(organization=self.instance)
                .values_list("data", flat=True)
                .first()
            )
            if not isinstance(structured_data, dict):
                structured_data = {}
            for key in SHARED_ORGANIZATION_SETTING_KEYS:
                if key in organization_data:
                    self.fields[key].initial = organization_data[key]
                elif key in structured_data:
                    self.fields[key].initial = structured_data[key]
            # Hide initial lead mentor field on edit
            self.fields.pop("initial_lead_mentor_email", None)
            time_zone_name = organization_data.get("time_zone")
            if time_zone_name is None:
                time_zone_name = structured_data.get("time_zone")
            try:
                self.fields["time_zone"].initial = validate_iana_timezone(
                    time_zone_name
                )
            except ValidationError:
                self.fields["time_zone"].initial = timezone.get_default_timezone_name()

    def clean_time_zone(self):
        return validate_iana_timezone(self.cleaned_data.get("time_zone"))

    def clean_slug(self):
        slug = (self.cleaned_data.get("slug") or "").strip().lower()
        if not slug:
            raise ValidationError("Organization slug is required.")
        if not is_valid_tenant_label(slug) and not (
            self.instance.pk and slug == str(self.instance.slug or "").lower()
        ):
            raise ValidationError(
                "Use an unreserved DNS label for the organization slug."
            )
        qs = Organization.objects.filter(slug__iexact=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("An organization with this slug already exists.")
        qs = Organization.objects.filter(subdomain__iexact=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                "This slug conflicts with another organization's subdomain."
            )
        return slug

    def clean_subdomain(self):
        subdomain = (self.cleaned_data.get("subdomain") or "").strip().lower()
        if not subdomain:
            return None
        if not is_valid_tenant_label(subdomain) and not (
            self.instance.pk and subdomain == str(self.instance.subdomain or "").lower()
        ):
            raise ValidationError("Use an unreserved DNS label for the subdomain.")
        qs = Organization.objects.filter(subdomain__iexact=subdomain)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("An organization with this subdomain already exists.")
        qs = Organization.objects.filter(slug__iexact=subdomain)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                "This subdomain conflicts with another organization's slug."
            )
        return subdomain

    def save(self, commit=True):
        org = super().save(commit=False)
        settings_dict = dict(org.settings) if isinstance(org.settings, dict) else {}
        shared_settings = {}
        for key in SHARED_ORGANIZATION_SETTING_KEYS:
            val = self.cleaned_data.get(key)
            if val is not None:
                shared_settings[key] = val

        settings_dict.update(shared_settings)
        org.settings = settings_dict
        if not commit:
            return org

        with transaction.atomic():
            if org.pk:
                saved_org = Organization.objects.select_for_update().get(pk=org.pk)
                for field in self._meta.fields:
                    setattr(saved_org, field, getattr(org, field))
                org = saved_org
                settings_dict = (
                    dict(org.settings) if isinstance(org.settings, dict) else {}
                )
                settings_dict.update(shared_settings)
                org.settings = settings_dict

            org.save()
            (
                settings_obj,
                _,
            ) = OrganizationSettings.objects.select_for_update().get_or_create(
                organization=org
            )
            structured_data = (
                dict(settings_obj.data) if isinstance(settings_obj.data, dict) else {}
            )
            structured_data.update(shared_settings)
            settings_obj.data = structured_data
            settings_obj.save(update_fields=["data", "updated_at"])
        return org


class PlatformMemberForm(forms.ModelForm):
    """Form to manage a user's membership and role in an organization."""

    user_identifier = forms.CharField(
        label="User (Username or Email)",
        help_text="Search existing user by username or email address.",
        widget=forms.TextInput(attrs={"placeholder": "e.g. mentor@example.com"}),
    )

    class Meta:
        model = UserOrganizationMembership
        fields = ["role", "is_active"]

    def __init__(self, *args, organization=None, **kwargs):
        self.organization = organization
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["user_identifier"].initial = (
                self.instance.user.email or self.instance.user.username
            )
            self.fields["user_identifier"].disabled = True

    def clean_user_identifier(self):
        ident = self.cleaned_data.get("user_identifier", "").strip()
        if self.instance and self.instance.pk:
            return self.instance.user

        user = (
            User.objects.filter(email__iexact=ident).first()
            or User.objects.filter(username__iexact=ident).first()
        )
        if not user:
            raise ValidationError(f"No user found matching '{ident}'.")

        if self.organization:
            existing = UserOrganizationMembership.objects.filter(
                user=user, organization=self.organization
            ).exists()
            if existing:
                raise ValidationError(
                    f"User '{user.username}' already has a membership in this organization."
                )
        return user


class PlatformUserSearchForm(forms.Form):
    """Search and filter form for Global User Support."""

    q = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Search by name, email, username, phone, or organization account ID...",
                "class": "form-control",
            }
        ),
    )
    role = forms.ChoiceField(
        required=False,
        choices=[("", "All Roles")] + list(UserOrganizationMembership.ROLE_CHOICES),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    organization = forms.ModelChoiceField(
        required=False,
        queryset=Organization.objects.all().order_by("name"),
        empty_label="All Organizations",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All Statuses"),
            ("active", "Active Users"),
            ("inactive", "Inactive Users"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class PlatformAuditLogFilterForm(forms.Form):
    """Filter form for Cross-Tenant Audit Log Explorer."""

    event = forms.ChoiceField(
        required=False,
        choices=[("", "All Events")] + list(AuditEvent.choices),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    search = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Search actor, target, notes, or IP...",
                "class": "form-control",
            }
        ),
    )
    outcome = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All Outcomes"),
            ("success", "Success Only"),
            ("failure", "Failure Only"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
