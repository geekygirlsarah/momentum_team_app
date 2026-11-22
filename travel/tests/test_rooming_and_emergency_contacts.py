from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from audit.events import AuditEvent
from audit.models import AuditLog
from organizations.models import Organization, UserOrganizationMembership
from programs.models import (
    Adult,
    AdultStudentRelationship,
    Enrollment,
    Program,
    ProgramFeature,
    RolePermission,
    School,
    Student,
)
from travel.forms import TravelEventForm
from travel.models import (
    TravelDeparture,
    TravelEvent,
    TravelMentorSignup,
    TravelParentChaperoneSignup,
    TravelSignup,
)


class TravelRoomingAndEmergencyContactsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.get(slug="gos")
        self.feature, _ = ProgramFeature.objects.get_or_create(
            key="travel", defaults={"name": "Travel"}
        )
        self.program = Program.objects.create(
            organization=self.organization, name="Rooming Test Program"
        )
        self.program.features.add(self.feature)
        self.school = School.objects.create(name="Rooming Test School")

        self.lead_user = User.objects.create_user(
            username="rooming-lead", password="password"
        )  # nosec B106
        UserOrganizationMembership.objects.create(
            user=self.lead_user,
            organization=self.organization,
            role="LeadMentor",
            is_active=True,
        )

        self.student_user = User.objects.create_user(
            username="rooming-student", password="password"
        )  # nosec B106
        self.student = Student.objects.create(
            organization=self.organization,
            user=self.student_user,
            legal_first_name="Taylor",
            last_name="Traveler",
            phone_number="412-555-0101",
            directory_consent=False,
            school=self.school,
            graduation_year=2028,
        )
        Enrollment.objects.create(
            organization=self.organization,
            student=self.student,
            program=self.program,
            active=True,
        )

        self.parent_user = User.objects.create_user(
            username="rooming-parent", password="password"
        )  # nosec B106
        self.parent = Adult.objects.create(
            organization=self.organization,
            user=self.parent_user,
            legal_first_name="Pat",
            last_name="Guardian",
            phone_number="412-555-0102",
            is_parent=True,
        )
        AdultStudentRelationship.objects.create(
            organization=self.organization,
            adult=self.parent,
            student=self.student,
        )

        self.mentor_user = User.objects.create_user(
            username="rooming-mentor", password="password"
        )  # nosec B106
        self.mentor = Adult.objects.create(
            organization=self.organization,
            user=self.mentor_user,
            legal_first_name="Morgan",
            last_name="Mentor",
            phone_number="412-555-0103",
            is_mentor=True,
            mentor_active=True,
        )
        RolePermission.objects.update_or_create(
            organization=self.organization,
            role="Mentor",
            section="travel_details",
            defaults={"can_read": True, "can_write": False},
        )

        self.other_user = User.objects.create_user(
            username="rooming-nontraveler", password="password"
        )  # nosec B106
        self.other_student = Student.objects.create(
            organization=self.organization,
            user=self.other_user,
            legal_first_name="Avery",
            last_name="Nontraveler",
            school=self.school,
            graduation_year=2028,
        )
        Enrollment.objects.create(
            organization=self.organization,
            student=self.other_student,
            program=self.program,
            active=True,
        )
        self.event = TravelEvent.objects.create(
            organization=self.organization,
            program=self.program,
            name="Rooming Test Trip",
            start_date=timezone.localdate() + timedelta(days=30),
            end_date=timezone.localdate() + timedelta(days=32),
        )
        self.student_signup = TravelSignup.objects.create(
            organization=self.organization,
            student=self.student,
            event=self.event,
            status=TravelSignup.INTERESTED,
        )
        self.mentor_signup = TravelMentorSignup.objects.create(
            organization=self.organization,
            adult=self.mentor,
            event=self.event,
        )
        self.chaperone_signup = TravelParentChaperoneSignup.objects.create(
            organization=self.organization,
            adult=self.parent,
            event=self.event,
        )

    def test_event_settings_separate_organized_travel_from_hotel_needs(self):
        self.assertTrue(self.event.has_organized_travel)
        self.assertTrue(self.event.needs_hotel)

        form = TravelEventForm(
            {
                "name": "Local Competition",
                "is_competition": "on",
                "has_organized_travel": "",
                "needs_hotel": "on",
                "start_date": self.event.start_date.isoformat(),
                "end_date": self.event.end_date.isoformat(),
                "requires_departure": "on",
            },
            instance=self.event,
        )

        self.assertTrue(form.is_valid(), form.errors)
        event = form.save()
        self.assertTrue(event.is_competition)
        self.assertFalse(event.has_organized_travel)
        self.assertTrue(event.needs_hotel)
        self.assertTrue(event.requires_departure)

    def room_assignments_url(self, event=None):
        return reverse(
            "travel:room_assignments",
            args=[self.program.pk, (event or self.event).pk],
        )

    def emergency_contacts_url(self, event=None):
        return reverse(
            "travel:emergency_contacts",
            args=[self.program.pk, (event or self.event).pk],
        )

    def login_as(self, user):
        self.client.force_login(user)

    def test_lead_can_set_room_count_and_assign_students_mentors_and_chaperones(self):
        self.login_as(self.lead_user)

        count_response = self.client.post(
            self.room_assignments_url(),
            {
                "room_count": "2",
                f"room_student_{self.student_signup.pk}": "",
                f"room_adult_{self.mentor.pk}": "",
                f"room_adult_{self.parent.pk}": "",
            },
        )
        self.assertEqual(count_response.status_code, 302)

        response = self.client.post(
            self.room_assignments_url(),
            {
                "room_count": "2",
                f"room_student_{self.student_signup.pk}": "1",
                f"room_adult_{self.mentor.pk}": "2",
                f"room_adult_{self.parent.pk}": "2",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.student_signup.refresh_from_db()
        self.mentor_signup.refresh_from_db()
        self.chaperone_signup.refresh_from_db()
        self.assertEqual(self.event.hotel_room_count, 2)
        self.assertEqual(self.student_signup.room_number, 1)
        self.assertEqual(self.mentor_signup.room_number, 2)
        self.assertEqual(self.chaperone_signup.room_number, 2)
        audit_logs = list(
            AuditLog.objects.filter(
                event=AuditEvent.TRAVEL_ROOM_ASSIGNMENTS_UPDATED,
                organization=self.organization,
            ).order_by("timestamp", "pk")
        )
        self.assertEqual(len(audit_logs), 2)
        self.assertEqual(audit_logs[0].before["hotel_room_count"], 0)
        self.assertEqual(audit_logs[0].after["hotel_room_count"], 2)
        self.assertEqual(audit_logs[1].before["hotel_room_count"], 2)
        self.assertEqual(audit_logs[1].after["hotel_room_count"], 2)
        self.assertEqual(
            audit_logs[1].after["room_assignments"][
                f"room_student_{self.student_signup.pk}"
            ],
            1,
        )
        self.assertNotIn("phone_number", audit_logs[1].after)

    def test_lead_can_set_actual_hotel_room_numbers_visible_to_travelers(self):
        self.event.hotel_room_count = 2
        self.event.save(update_fields=["hotel_room_count"])
        self.login_as(self.lead_user)

        response = self.client.post(
            self.room_assignments_url(),
            {
                "room_count": "2",
                "hotel_room_label_1": "0320",
                "hotel_room_label_2": "332A",
                f"room_student_{self.student_signup.pk}": "1",
                f"room_adult_{self.mentor.pk}": "2",
                f"room_adult_{self.parent.pk}": "2",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.student_signup.refresh_from_db()
        self.mentor_signup.refresh_from_db()
        self.chaperone_signup.refresh_from_db()
        self.assertEqual(self.event.hotel_room_labels, {"1": "0320", "2": "332A"})
        self.assertEqual(self.student_signup.room_number, 1)
        self.assertEqual(self.mentor_signup.room_number, 2)
        self.assertEqual(self.chaperone_signup.room_number, 2)

        audit_log = AuditLog.objects.get(
            event=AuditEvent.TRAVEL_ROOM_ASSIGNMENTS_UPDATED,
            organization=self.organization,
        )
        self.assertEqual(
            audit_log.after["hotel_room_labels"], {"1": "0320", "2": "332A"}
        )

        self.login_as(self.student_user)
        chart = self.client.get(self.room_assignments_url())
        self.assertEqual(chart.status_code, 200)
        self.assertContains(chart, "Hotel room 0320")
        self.assertContains(chart, "Hotel room 332A")

    def test_omitting_room_label_fields_preserves_existing_hotel_numbers(self):
        self.event.hotel_room_count = 2
        self.event.hotel_room_labels = {"1": "320", "2": "332"}
        self.event.save(update_fields=["hotel_room_count", "hotel_room_labels"])
        self.login_as(self.lead_user)

        response = self.client.post(
            self.room_assignments_url(),
            {
                "room_count": "2",
                f"room_student_{self.student_signup.pk}": "",
                f"room_adult_{self.mentor.pk}": "",
                f"room_adult_{self.parent.pk}": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.assertEqual(self.event.hotel_room_labels, {"1": "320", "2": "332"})

    def test_room_labels_for_rooms_hidden_by_count_reduction_are_preserved(self):
        self.event.hotel_room_count = 2
        self.event.hotel_room_labels = {"1": "320", "2": "332"}
        self.event.save(update_fields=["hotel_room_count", "hotel_room_labels"])
        for signup in (
            self.student_signup,
            self.mentor_signup,
            self.chaperone_signup,
        ):
            signup.room_number = 1
            signup.save(update_fields=["room_number"])
        self.login_as(self.lead_user)

        response = self.client.post(
            self.room_assignments_url(),
            {
                "room_count": "1",
                "hotel_room_label_1": "320",
                "hotel_room_label_2": "",
                f"room_student_{self.student_signup.pk}": "1",
                f"room_adult_{self.mentor.pk}": "1",
                f"room_adult_{self.parent.pk}": "1",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.assertEqual(self.event.hotel_room_count, 1)
        self.assertEqual(self.event.hotel_room_labels, {"1": "320", "2": "332"})

    def test_disabling_hotel_planning_preserves_room_labels_and_assignments(self):
        self.event.hotel_room_count = 2
        self.event.hotel_room_labels = {"1": "320", "2": "332"}
        self.event.save(update_fields=["hotel_room_count", "hotel_room_labels"])
        self.student_signup.room_number = 2
        self.student_signup.save(update_fields=["room_number"])
        self.event.needs_hotel = False
        self.event.save(update_fields=["needs_hotel"])
        self.login_as(self.lead_user)

        response = self.client.get(self.room_assignments_url())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("home"))
        self.event.refresh_from_db()
        self.student_signup.refresh_from_db()
        self.assertEqual(self.event.hotel_room_labels, {"1": "320", "2": "332"})
        self.assertEqual(self.student_signup.room_number, 2)

    def test_hotel_room_number_length_is_validated(self):
        self.event.hotel_room_count = 1
        self.event.hotel_room_labels = {"1": "320"}
        self.event.save(update_fields=["hotel_room_count", "hotel_room_labels"])
        self.login_as(self.lead_user)

        response = self.client.post(
            self.room_assignments_url(),
            {
                "room_count": "1",
                "hotel_room_label_1": "1" * 33,
                f"room_student_{self.student_signup.pk}": "",
                f"room_adult_{self.mentor.pk}": "",
                f"room_adult_{self.parent.pk}": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "hotel_room_label_1",
            response.context["room_assignments_form"].errors,
        )
        self.event.refresh_from_db()
        self.assertEqual(self.event.hotel_room_labels, {"1": "320"})

    def test_room_choices_are_limited_to_saved_rooms_and_empty_rooms_are_listed(self):
        self.event.hotel_room_count = 3
        self.event.save(update_fields=["hotel_room_count"])
        self.login_as(self.lead_user)

        response = self.client.get(self.room_assignments_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Room 1")
        self.assertContains(response, "Room 2")
        self.assertContains(response, "Room 3")
        self.assertContains(response, "No travelers assigned to this room")

    def test_only_lead_can_edit_rooming_and_out_of_range_room_is_rejected(self):
        self.login_as(self.student_user)
        denied = self.client.post(
            self.room_assignments_url(),
            {"room_count": "2", f"room_student_{self.student_signup.pk}": "1"},
        )
        self.assertEqual(denied.status_code, 403)

        self.login_as(self.lead_user)
        invalid = self.client.post(
            self.room_assignments_url(),
            {
                "room_count": "1",
                f"room_student_{self.student_signup.pk}": "2",
                f"room_adult_{self.mentor.pk}": "",
                f"room_adult_{self.parent.pk}": "",
            },
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertTrue(invalid.context["room_assignments_form"].errors)
        self.event.refresh_from_db()
        self.assertEqual(self.event.hotel_room_count, 0)

    def test_room_count_cannot_be_reduced_without_reassigning_existing_occupants(self):
        self.event.hotel_room_count = 2
        self.event.save(update_fields=["hotel_room_count"])
        self.student_signup.room_number = 2
        self.student_signup.save(update_fields=["room_number"])
        self.login_as(self.lead_user)

        response = self.client.post(
            self.room_assignments_url(),
            {
                "room_count": "1",
                f"room_student_{self.student_signup.pk}": "2",
                f"room_adult_{self.mentor.pk}": "",
                f"room_adult_{self.parent.pk}": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["room_assignments_form"].errors)
        self.event.refresh_from_db()
        self.student_signup.refresh_from_db()
        self.assertEqual(self.event.hotel_room_count, 2)
        self.assertEqual(self.student_signup.room_number, 2)

    def test_current_travelers_and_linked_guardian_can_view_room_chart_but_others_cannot(
        self,
    ):
        urls = (self.room_assignments_url(), self.emergency_contacts_url())
        for user in (
            self.lead_user,
            self.student_user,
            self.parent_user,
            self.mentor_user,
        ):
            with self.subTest(user=user.username):
                self.login_as(user)
                for url in urls:
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)

        self.login_as(self.other_user)
        for url in urls:
            denied = self.client.get(url)
            self.assertEqual(denied.status_code, 302)
            self.assertEqual(denied["Location"], reverse("home"))

    def test_mentor_trip_read_permission_is_enforced_for_new_pages(self):
        RolePermission.objects.filter(
            organization=self.organization,
            role="Mentor",
            section="travel_details",
        ).update(can_read=False)
        self.login_as(self.mentor_user)

        for url in (self.room_assignments_url(), self.emergency_contacts_url()):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse("home"))

    def test_event_list_links_to_trip_pages_for_current_travelers_only(self):
        url = reverse("travel:event_list", args=[self.program.pk])
        self.login_as(self.student_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.room_assignments_url())
        self.assertContains(response, self.emergency_contacts_url())

        self.login_as(self.other_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.room_assignments_url())
        self.assertNotContains(response, self.emergency_contacts_url())

    def test_local_competition_keeps_signup_approval_and_contact_access(self):
        self.event.is_competition = True
        self.event.requires_departure = True
        self.event.hotel_room_count = 2
        self.event.save(
            update_fields=[
                "is_competition",
                "requires_departure",
                "hotel_room_count",
            ]
        )
        departure = TravelDeparture.objects.create(
            organization=self.organization,
            event=self.event,
            label="Old saved departure",
            leave_date=self.event.start_date,
        )
        self.student_signup.departure = departure
        self.student_signup.room_number = 2
        self.student_signup.save(update_fields=["departure", "room_number"])
        self.event.has_organized_travel = False
        self.event.needs_hotel = False
        self.event.save(update_fields=["has_organized_travel", "needs_hotel"])

        self.login_as(self.student_user)
        event_list = self.client.get(
            reverse("travel:event_list", args=[self.program.pk])
        )
        self.assertEqual(event_list.status_code, 200)
        self.assertContains(event_list, self.emergency_contacts_url())
        self.assertNotContains(event_list, self.room_assignments_url())
        self.assertNotContains(
            event_list, f'id="student_rsvp_departure_{self.event.pk}"'
        )

        contacts = self.client.get(self.emergency_contacts_url())
        self.assertEqual(contacts.status_code, 200)
        self.assertContains(contacts, "412-555-0101")
        self.assertContains(contacts, "412-555-0102")
        room_page = self.client.get(self.room_assignments_url())
        self.assertEqual(room_page.status_code, 302)
        self.assertEqual(room_page["Location"], reverse("home"))

        self.student_signup.status = TravelSignup.WITHDRAWN
        self.student_signup.save(update_fields=["status"])
        event_list = self.client.get(
            reverse("travel:event_list", args=[self.program.pk])
        )
        self.assertEqual(event_list.status_code, 200)
        self.assertNotContains(
            event_list, f'id="student_rsvp_departure_{self.event.pk}"'
        )

        self.login_as(self.other_user)
        rsvp_url = reverse(
            "travel:competition_rsvp", args=[self.program.pk, self.event.pk]
        )
        rejected_rsvp = self.client.post(
            rsvp_url,
            {"response": "yes", "departure": str(departure.pk)},
        )
        self.assertEqual(rejected_rsvp.status_code, 302)
        self.assertFalse(
            TravelSignup.objects.filter(
                event=self.event, student=self.other_student
            ).exists()
        )

        accepted_rsvp = self.client.post(rsvp_url, {"response": "yes"})
        self.assertEqual(accepted_rsvp.status_code, 302)
        local_signup = TravelSignup.objects.get(
            event=self.event, student=self.other_student
        )
        self.assertEqual(local_signup.status, TravelSignup.INTERESTED)
        self.assertIsNone(local_signup.departure)

        self.student_signup.status = TravelSignup.INTERESTED
        self.student_signup.save(update_fields=["status"])
        self.login_as(self.parent_user)
        approval = self.client.post(
            reverse(
                "travel:signup_approve", args=[self.program.pk, self.student_signup.pk]
            ),
            {"approval_token": self.student_signup.approval_token},
        )
        self.assertEqual(approval.status_code, 302)
        self.student_signup.refresh_from_db()
        self.event.refresh_from_db()
        self.assertEqual(self.student_signup.status, TravelSignup.APPROVED)
        self.assertEqual(self.student_signup.departure_id, departure.pk)
        self.assertEqual(self.student_signup.room_number, 2)
        self.assertEqual(self.event.hotel_room_count, 2)

    def test_trip_emergency_contacts_include_opted_out_student_guardian_mentor_and_chaperone(
        self,
    ):
        self.login_as(self.student_user)

        response = self.client.get(self.emergency_contacts_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Taylor Traveler")
        self.assertContains(response, "412-555-0101")
        self.assertContains(response, "Pat Guardian")
        self.assertContains(response, "412-555-0102")
        self.assertContains(response, "Morgan Mentor")
        self.assertContains(response, "412-555-0103")

    def test_emergency_page_excludes_declined_students_and_shows_missing_phone(self):
        no_phone_student = Student.objects.create(
            organization=self.organization,
            legal_first_name="Casey",
            last_name="NoPhone",
            school=self.school,
            graduation_year=2028,
        )
        Enrollment.objects.create(
            organization=self.organization,
            student=no_phone_student,
            program=self.program,
            active=True,
        )
        TravelSignup.objects.create(
            organization=self.organization,
            student=no_phone_student,
            event=self.event,
            status=TravelSignup.APPROVED,
        )
        declined_student = Student.objects.create(
            organization=self.organization,
            legal_first_name="Drew",
            last_name="Declined",
            school=self.school,
            graduation_year=2028,
        )
        TravelSignup.objects.create(
            organization=self.organization,
            student=declined_student,
            event=self.event,
            status=TravelSignup.DECLINED,
        )
        self.login_as(self.lead_user)

        response = self.client.get(self.emergency_contacts_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Casey NoPhone")
        self.assertContains(response, "No phone number on file")
        self.assertNotContains(response, "Drew Declined")

    def test_foreign_event_and_foreign_room_field_are_rejected(self):
        foreign_org = Organization.objects.create(
            name="Foreign Travel Org", slug="foreign-travel", subdomain="foreign-travel"
        )
        foreign_program = Program.objects.create(
            organization=foreign_org, name="Foreign Travel Program"
        )
        foreign_event = TravelEvent.objects.create(
            organization=foreign_org,
            program=foreign_program,
            name="Foreign Trip",
            start_date=timezone.localdate() + timedelta(days=30),
            end_date=timezone.localdate() + timedelta(days=32),
        )
        foreign_student = Student.objects.create(
            organization=foreign_org,
            legal_first_name="Foreign",
            last_name="Traveler",
            school=self.school,
            graduation_year=2028,
        )
        foreign_signup = TravelSignup.objects.create(
            organization=foreign_org,
            student=foreign_student,
            event=foreign_event,
            status=TravelSignup.INTERESTED,
        )
        self.login_as(self.lead_user)

        foreign_response = self.client.get(self.room_assignments_url(foreign_event))
        self.assertEqual(foreign_response.status_code, 302)
        self.assertEqual(foreign_response["Location"], reverse("home"))
        foreign_contacts_response = self.client.get(
            self.emergency_contacts_url(foreign_event)
        )
        self.assertEqual(foreign_contacts_response.status_code, 302)
        self.assertEqual(foreign_contacts_response["Location"], reverse("home"))
        local_response = self.client.post(
            self.room_assignments_url(),
            {
                "room_count": "1",
                f"room_student_{self.student_signup.pk}": "1",
                f"room_adult_{self.mentor.pk}": "",
                f"room_adult_{self.parent.pk}": "",
                f"room_student_{foreign_signup.pk}": "1",
            },
        )
        self.assertEqual(local_response.status_code, 200)
        self.assertTrue(local_response.context["room_assignments_form"].errors)
        foreign_signup.refresh_from_db()
        self.assertIsNone(foreign_signup.room_number)
