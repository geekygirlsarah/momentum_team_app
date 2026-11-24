import datetime
from io import StringIO

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from applications.models import (
    Application,
    ApplicationGuardianDraft,
    ApplicationStudentDraft,
)
from organizations.models import Organization
from programs.models import Adult, Student


class AnonymizeEmailsCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="realstudent",
            email="real_student@gmail.com",
        )
        self.email_addr = EmailAddress.objects.create(
            user=self.user,
            email="real_student@gmail.com",
            primary=True,
            verified=True,
        )
        self.student = Student.objects.create(
            legal_first_name="Alice",
            last_name="Smith",
            date_of_birth=datetime.date(2008, 5, 10),
            personal_email="real_student@gmail.com",
            andrew_email="asmith@andrew.cmu.edu",
            user=self.user,
        )
        self.adult = Adult.objects.create(
            legal_first_name="Bob",
            last_name="Smith",
            personal_email="bob.smith@company.com",
            andrew_email="bsmith@andrew.cmu.edu",
            is_parent=True,
        )
        self.application = Application.objects.create(
            email="applicant@yahoo.com",
            applicant_type="student",
            data={
                "step5-student": {
                    "personal_email": "applicant@yahoo.com",
                }
            },
        )

        self.org = Organization.objects.get(slug="gos")
        # A profile-workflow application in progress: an isolated, not-yet-
        # approved student profile plus its typed student/guardian drafts.
        self.draft_student_profile = Student.objects.create(
            organization=self.org,
            legal_first_name="Casey",
            last_name="Doe",
            date_of_birth=datetime.date(2010, 1, 1),
            personal_email="casey.draft@gmail.com",
            is_incomplete_application_profile=True,
        )
        self.draft_guardian_profile = Adult.objects.create(
            organization=self.org,
            legal_first_name="Pat",
            last_name="Doe",
            personal_email="pat.draft@gmail.com",
            is_incomplete_application_profile=True,
        )
        self.profile_workflow_application = Application.objects.create(
            organization=self.org,
            email="casey.draft@gmail.com",
            applicant_type="student",
            profile_workflow_version=1,
        )
        self.student_draft = ApplicationStudentDraft.objects.create(
            organization=self.org,
            application=self.profile_workflow_application,
            personal_email="casey.draft@gmail.com",
        )
        self.guardian_draft = ApplicationGuardianDraft.objects.create(
            organization=self.org,
            application=self.profile_workflow_application,
            slot=ApplicationGuardianDraft.Slot.PRIMARY,
            email="pat.draft@gmail.com",
        )

    def test_dry_run_makes_no_changes(self):
        out = StringIO()
        call_command(
            "anonymize_emails", "--dry-run", "--domain=dev.invalid", stdout=out
        )
        output = out.getvalue()

        self.assertIn("[DRY-RUN]", output)

        self.student.refresh_from_db()
        self.assertEqual(self.student.personal_email, "real_student@gmail.com")
        self.assertEqual(self.student.andrew_email, "asmith@andrew.cmu.edu")

        self.adult.refresh_from_db()
        self.assertEqual(self.adult.personal_email, "bob.smith@company.com")

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "real_student@gmail.com")

        self.draft_student_profile.refresh_from_db()
        self.assertEqual(
            self.draft_student_profile.personal_email, "casey.draft@gmail.com"
        )
        self.draft_guardian_profile.refresh_from_db()
        self.assertEqual(
            self.draft_guardian_profile.personal_email, "pat.draft@gmail.com"
        )
        self.student_draft.refresh_from_db()
        self.assertEqual(self.student_draft.personal_email, "casey.draft@gmail.com")
        self.guardian_draft.refresh_from_db()
        self.assertEqual(self.guardian_draft.email, "pat.draft@gmail.com")

    def test_anonymize_without_force_or_dry_run_prompts_or_warns(self):
        out = StringIO()
        call_command("anonymize_emails", stdout=out)
        output = out.getvalue()
        self.assertIn("specify --force", output)

        self.student.refresh_from_db()
        self.assertEqual(self.student.personal_email, "real_student@gmail.com")

    def test_anonymize_executes_with_force(self):
        out = StringIO()
        call_command("anonymize_emails", "--force", "--domain=example.test", stdout=out)

        self.student.refresh_from_db()
        self.assertEqual(
            self.student.personal_email, f"student_{self.student.pk}@example.test"
        )
        self.assertEqual(
            self.student.org_email, f"student_{self.student.pk}_org@example.test"
        )

        self.adult.refresh_from_db()
        self.assertEqual(
            self.adult.personal_email, f"adult_{self.adult.pk}@example.test"
        )
        self.assertEqual(
            self.adult.org_email, f"adult_{self.adult.pk}_org@example.test"
        )

        self.user.refresh_from_db()
        self.assertTrue(self.user.email.endswith("@example.test"))

        self.email_addr.refresh_from_db()
        self.assertEqual(self.email_addr.email, self.user.email)

        self.application.refresh_from_db()
        self.assertEqual(
            self.application.email, f"application_{self.application.pk}@example.test"
        )

    def test_anonymize_reaches_incomplete_profiles_and_typed_drafts(self):
        """The isolated, not-yet-approved profiles and typed drafts a
        profile-workflow application creates hold real applicant/guardian
        emails too and must not be skipped by this dev-safety tool."""
        out = StringIO()
        call_command("anonymize_emails", "--force", "--domain=example.test", stdout=out)

        self.draft_student_profile.refresh_from_db()
        self.assertEqual(
            self.draft_student_profile.personal_email,
            f"student_{self.draft_student_profile.pk}@example.test",
        )

        self.draft_guardian_profile.refresh_from_db()
        self.assertEqual(
            self.draft_guardian_profile.personal_email,
            f"adult_{self.draft_guardian_profile.pk}@example.test",
        )

        self.student_draft.refresh_from_db()
        self.assertEqual(
            self.student_draft.personal_email,
            f"application_{self.profile_workflow_application.pk}_student@example.test",
        )

        self.guardian_draft.refresh_from_db()
        self.assertEqual(
            self.guardian_draft.email,
            f"application_{self.profile_workflow_application.pk}_primary@example.test",
        )
