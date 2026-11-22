from django import forms
from django.db.models import Value
from django.db.models.functions import Coalesce, Lower, NullIf
from PIL import Image, UnidentifiedImageError

from programs.models import Adult, Student
from programs.utils import active_students_in_program
from travel.models import (
    TravelCompetitionRSVP,
    TravelCostOption,
    TravelDeparture,
    TravelEvent,
    TravelMentorSignup,
    TravelParentChaperoneSignup,
    TravelSignup,
)
from travel.utils import sanitize_travel_description


def _apply_bootstrap_classes(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.Select):
            css_class = "form-select"
        elif isinstance(widget, forms.CheckboxInput):
            css_class = "form-check-input"
        else:
            css_class = "form-control"
        existing_classes = widget.attrs.get("class", "").split()
        if css_class not in existing_classes:
            existing_classes.append(css_class)
        widget.attrs["class"] = " ".join(existing_classes)


class TravelSignedPermissionFormUpload(forms.Form):
    file = forms.FileField()
    form_version = forms.UUIDField()

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if uploaded.size > 10 * 1024 * 1024:
            raise forms.ValidationError("The signed form must be 10 MB or smaller.")
        extension = uploaded.name.rsplit(".", 1)[-1].lower()
        try:
            if extension == "pdf":
                valid = uploaded.read(5) == b"%PDF-"
            elif extension in ("jpg", "jpeg", "png"):
                image = Image.open(uploaded)
                valid = image.format == ("PNG" if extension == "png" else "JPEG")
                image.verify()
            else:
                valid = False
        except (OSError, ValueError, UnidentifiedImageError):
            valid = False
        finally:
            uploaded.seek(0)
        if not valid:
            raise forms.ValidationError("Upload a PDF, JPEG, or PNG signed form.")
        return uploaded


class TravelEventForm(forms.ModelForm):
    class Meta:
        model = TravelEvent
        fields = [
            "name",
            "is_competition",
            "has_organized_travel",
            "needs_hotel",
            "start_date",
            "end_date",
            "start_time",
            "end_time",
            "signup_deadline",
            "requires_departure",
            "location_name",
            "location_address",
            "description",
            "permission_form",
        ]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
            "signup_deadline": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "description": forms.Textarea(
                attrs={
                    "data-email-editor": "",
                    "data-email-editor-placeholder": "Add itinerary details...",
                    "rows": 8,
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["requires_departure"].help_text = (
            "When organized travel is enabled, require students to select one "
            "of this trip's departures."
        )
        _apply_bootstrap_classes(self)

    def clean_description(self):
        return sanitize_travel_description(self.cleaned_data.get("description"))


class TravelDepartureForm(forms.ModelForm):
    class Meta:
        model = TravelDeparture
        fields = ["label", "leave_date", "leave_time", "meeting_location", "notes"]
        widgets = {
            "leave_date": forms.DateInput(attrs={"type": "date"}),
            "leave_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _apply_bootstrap_classes(self)


class TravelCostOptionForm(forms.ModelForm):
    class Meta:
        model = TravelCostOption
        fields = [
            "name",
            "amount",
            "is_required",
            "unit_label",
            "notes",
            "sort_order",
        ]
        widgets = {
            "amount": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit_label"].required = False
        self.fields["sort_order"].required = False
        _apply_bootstrap_classes(self)

    def has_changed(self):
        if self.instance.pk or not super().has_changed():
            return super().has_changed()

        name = self.data.get(self.add_prefix("name"), "").strip()
        amount = self.data.get(self.add_prefix("amount"), "").strip()
        notes = self.data.get(self.add_prefix("notes"), "").strip()
        unit_label = self.data.get(self.add_prefix("unit_label"), "").strip()
        sort_order = self.data.get(self.add_prefix("sort_order"), "").strip()
        initial_unit_label = str(self.initial.get("unit_label") or "per person")
        initial_sort_order = str(self.initial.get("sort_order") or 0)
        return bool(
            name
            or amount
            or notes
            or (unit_label and unit_label != initial_unit_label)
            or (sort_order and sort_order != initial_sort_order)
        )


TravelDepartureFormSet = forms.inlineformset_factory(
    TravelEvent,
    TravelDeparture,
    form=TravelDepartureForm,
    extra=1,
    can_delete=True,
    min_num=0,
    validate_min=False,
)

TravelCostOptionFormSet = forms.inlineformset_factory(
    TravelEvent,
    TravelCostOption,
    form=TravelCostOptionForm,
    extra=1,
    can_delete=True,
    min_num=0,
    validate_min=False,
)


class TravelSignupForm(forms.ModelForm):
    """Student signs up for a trip: optional departure pick + cost check-list.

    The view hides optional departure selectors when only one departure exists;
    required choices remain visible and all choice querysets are scoped.
    """

    class Meta:
        model = TravelSignup
        fields = ["departure", "cost_items"]
        widgets = {
            "cost_items": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, **kwargs):
        departures = kwargs.pop("departures", None)
        cost_options = kwargs.pop("cost_options", None)
        requires_departure = kwargs.pop("requires_departure", False)
        departure_enabled = kwargs.pop("departure_enabled", True)
        super().__init__(*args, **kwargs)
        self.requires_departure = requires_departure
        self.departure_enabled = departure_enabled
        self.cost_options = (
            cost_options
            if cost_options is not None
            else TravelCostOption.objects.none()
        )
        available_departures = list(departures) if departures is not None else []
        available_cost_options = list(self.cost_options)
        self.fields["departure"].queryset = (
            departures if departures is not None else TravelDeparture.objects.none()
        )
        self.fields["cost_items"].queryset = self.cost_options
        self.fields["cost_items"].choices = [
            (option.pk, str(option)) for option in available_cost_options
        ]
        if self.instance.pk and not self.is_bound:
            selected_ids = {option.pk for option in self.instance.cost_items.all()}
            required_ids = {
                option.pk for option in available_cost_options if option.is_required
            }
            self.initial["cost_items"] = selected_ids | required_ids
        self.fields["departure"].required = requires_departure
        self.fields["departure"].empty_label = (
            "Choose a departure" if requires_departure else "No specific departure"
        )
        departure_choices = [("", self.fields["departure"].empty_label)]
        departure_choices.extend(
            (departure.pk, str(departure)) for departure in available_departures
        )
        self.fields["departure"].choices = departure_choices
        if not departure_enabled:
            self.fields.pop("departure")

    def clean(self):
        cleaned_data = super().clean()
        if (
            not self.departure_enabled
            and self.is_bound
            and self.data.get(self.add_prefix("departure"))
        ):
            self.add_error(None, "This event does not use organized travel.")
        return cleaned_data

    def clean_cost_items(self):
        selected = self.cleaned_data["cost_items"]
        required = self.cost_options.filter(is_required=True)
        return selected | required


class TravelSignupCostForm(forms.Form):
    """Lead Mentor cost editor for an existing signup."""

    cost_items = forms.ModelMultipleChoiceField(
        queryset=TravelCostOption.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, cost_options=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cost_options = (
            cost_options
            if cost_options is not None
            else TravelCostOption.objects.none()
        )
        self.fields["cost_items"].queryset = self.cost_options.filter(is_required=False)

    def clean_cost_items(self):
        selected = list(self.cleaned_data["cost_items"])
        required = list(self.cost_options.filter(is_required=True))
        return required + selected


class TravelStaffSignupForm(TravelSignupForm):
    """Lead mentors adding a student after a conversation. The student must be
    an active student of the program; the signup lands in 'interested' and the
    parents still have to approve."""

    student = forms.ModelChoiceField(queryset=Student.objects.none(), label="Student")

    def __init__(self, *args, **kwargs):
        program = kwargs.pop("program", None)
        super().__init__(*args, **kwargs)
        if program is not None:
            self.fields["student"].queryset = active_students_in_program(
                program
            ).order_by(
                Lower(
                    Coalesce(
                        NullIf("preferred_first_name", Value("")),
                        "legal_first_name",
                    )
                ),
                Lower("last_name"),
            )

    def clean_departure(self):
        departure = self.cleaned_data.get("departure")
        return departure


class TravelSignupDepartureForm(forms.ModelForm):
    class Meta:
        model = TravelSignup
        fields = ["departure"]

    def __init__(self, *args, **kwargs):
        departures = kwargs.pop("departures", None)
        requires_departure = kwargs.pop("requires_departure", False)
        super().__init__(*args, **kwargs)
        self.fields["departure"].queryset = (
            departures if departures is not None else TravelDeparture.objects.none()
        )
        self.fields["departure"].required = requires_departure
        self.fields["departure"].empty_label = (
            "Choose a departure" if requires_departure else "No specific departure"
        )


class TravelMentorDepartureForm(forms.ModelForm):
    class Meta:
        model = TravelMentorSignup
        fields = ["departure"]

    def __init__(self, *args, **kwargs):
        departures = kwargs.pop("departures", None)
        requires_departure = kwargs.pop("requires_departure", False)
        super().__init__(*args, **kwargs)
        self.fields["departure"].queryset = (
            departures if departures is not None else TravelDeparture.objects.none()
        )
        self.fields["departure"].required = requires_departure
        self.fields["departure"].empty_label = (
            "Choose a departure" if requires_departure else "No specific departure"
        )
        _apply_bootstrap_classes(self)


class TravelCompetitionRSVPForm(forms.Form):
    student = forms.ModelChoiceField(queryset=Student.objects.none())
    response = forms.ChoiceField(choices=TravelCompetitionRSVP.Response.choices)

    def __init__(self, *args, **kwargs):
        students = kwargs.pop("students", None)
        super().__init__(*args, **kwargs)
        self.fields["student"].queryset = (
            students if students is not None else Student.objects.none()
        )
        _apply_bootstrap_classes(self)


class TravelParentChaperoneStaffForm(forms.ModelForm):
    """Lead Mentors add a parent as a chaperone. Only parents with at least one
    active student in the program are eligible."""

    class Meta:
        model = TravelParentChaperoneSignup
        fields = ["adult"]

    def __init__(self, *args, **kwargs):
        program = kwargs.pop("program", None)
        super().__init__(*args, **kwargs)
        if program is not None:
            self.fields["adult"].queryset = TravelParentChaperoneSignup.eligible_adults(
                program
            )
            self.fields["adult"].label = "Parent"
            self.fields["adult"].empty_label = "-- Choose a parent --"


class TravelMentorStaffSignupForm(forms.ModelForm):
    """Lead Mentors add another active mentor to a trip."""

    class Meta:
        model = TravelMentorSignup
        fields = ["adult", "departure"]

    def __init__(self, *args, **kwargs):
        program = kwargs.pop("program", None)
        event = kwargs.pop("event", None)
        departures = kwargs.pop("departures", None)
        departure_enabled = kwargs.pop("departure_enabled", True)
        super().__init__(*args, **kwargs)
        self.departure_enabled = departure_enabled

        self.fields["adult"].queryset = Adult.objects.none()
        self.fields["departure"].queryset = TravelDeparture.objects.none()
        if program is not None and program.organization_id:
            organization = program.organization
            mentors = Adult.objects.for_organization(organization).filter(
                organization=organization,
                is_mentor=True,
                mentor_active=True,
            )
            if event is not None:
                existing_adult_ids = (
                    TravelMentorSignup.objects.for_organization(organization)
                    .filter(event=event)
                    .values("adult_id")
                )
                mentors = mentors.exclude(pk__in=existing_adult_ids)
            self.fields["adult"].queryset = mentors.order_by(
                Lower(
                    Coalesce(
                        NullIf("preferred_first_name", Value("")),
                        "legal_first_name",
                    )
                ),
                Lower("last_name"),
            )
        if departures is not None:
            self.fields["departure"].queryset = departures

        self.fields["adult"].label = "Mentor"
        self.fields["adult"].empty_label = "-- Choose a mentor --"
        self.fields["departure"].required = False
        self.fields["departure"].empty_label = "-- Choose a departure --"
        _apply_bootstrap_classes(self)
        if not departure_enabled:
            self.fields.pop("departure")

    def clean(self):
        cleaned_data = super().clean()
        if (
            not self.departure_enabled
            and self.is_bound
            and self.data.get(self.add_prefix("departure"))
        ):
            self.add_error(None, "This event does not use organized travel.")
        return cleaned_data


class TravelApprovalForm(forms.Form):
    """Parent approval. When the trip has an uploaded permission form, the
    parent must acknowledge it online before they can approve."""

    permission_acknowledged = forms.BooleanField(
        required=False,
        label="I confirm I have reviewed the attached permission form",
    )

    def __init__(self, *args, **kwargs):
        self.requires_form = kwargs.pop("requires_form", False)
        super().__init__(*args, **kwargs)
        if self.requires_form:
            self.fields["permission_acknowledged"].required = True

    def clean(self):
        cleaned_data = super().clean()
        if self.requires_form and not cleaned_data.get("permission_acknowledged"):
            self.add_error(
                "permission_acknowledged",
                "This trip has a permission form that must be acknowledged before approving.",
            )
        return cleaned_data


class TravelRoomAssignmentsForm(forms.Form):
    room_count = forms.IntegerField(
        label="Number of hotel rooms",
        min_value=0,
        max_value=32767,
        widget=forms.NumberInput(attrs={"min": 0, "max": 32767}),
    )

    def __init__(self, *args, event, travelers, **kwargs):
        self.travelers = travelers
        super().__init__(*args, **kwargs)
        self.fields["room_count"].initial = event.hotel_room_count
        room_labels = (
            event.hotel_room_labels if isinstance(event.hotel_room_labels, dict) else {}
        )
        room_choices = [("", "Not assigned")] + [
            (
                room_number,
                (
                    f"Room {room_number} — Hotel room {room_labels[str(room_number)]}"
                    if room_labels.get(str(room_number))
                    else f"Room {room_number}"
                ),
            )
            for room_number in range(1, event.hotel_room_count + 1)
        ]
        for room_number in range(1, event.hotel_room_count + 1):
            self.fields[f"hotel_room_label_{room_number}"] = forms.CharField(
                label=f"Hotel room number for Room {room_number}",
                required=False,
                max_length=32,
                initial=room_labels.get(str(room_number), ""),
                widget=forms.TextInput(attrs={"placeholder": "e.g. 320"}),
            )
        for traveler in travelers:
            self.fields[traveler["room_field"]] = forms.TypedChoiceField(
                label=f"Room for {traveler['name']}",
                required=False,
                choices=room_choices,
                coerce=int,
                empty_value=None,
                initial=traveler["room_number"],
            )
        _apply_bootstrap_classes(self)

    def clean(self):
        cleaned_data = super().clean()
        room_count = cleaned_data.get("room_count")
        expected_fields = set(self.fields)
        unexpected_fields = {
            key
            for key in self.data
            if (key.startswith("room_") or key.startswith("hotel_room_label_"))
            and key not in expected_fields
        }
        if unexpected_fields:
            raise forms.ValidationError(
                "A room assignment does not belong to a traveler on this trip."
            )

        missing_fields = [
            traveler["room_field"]
            for traveler in self.travelers
            if traveler["room_field"] not in self.data
        ]
        if missing_fields:
            raise forms.ValidationError(
                "The traveler list changed. Reload the page before saving assignments."
            )

        if room_count is not None:
            for traveler in self.travelers:
                field_name = traveler["room_field"]
                room_number = cleaned_data.get(field_name)
                if room_number is not None and room_number > room_count:
                    self.add_error(
                        field_name,
                        "Choose a room number no greater than the room count.",
                    )
        return cleaned_data
