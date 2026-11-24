from django.core.exceptions import ValidationError
from django.test import TestCase

from organizations.models import Organization
from programs.models import Adult, Student


class AdultModelTests(TestCase):
    def test_adult_email_shared_allowed(self):
        # Two adults (e.g. a mother and father) may share the same personal
        # email address — the unique constraint was intentionally removed to
        # support this real-world case.
        Adult.objects.create(
            legal_first_name="Mary",
            last_name="Smith",
            personal_email="shared@example.com",
        )
        # Should NOT raise — different person, same email
        Adult.objects.create(
            legal_first_name="John",
            last_name="Smith",
            personal_email="shared@example.com",
        )
        self.assertEqual(
            Adult.objects.filter(personal_email="shared@example.com").count(), 2
        )

    def test_adult_email_null_allowed_multiple_times(self):
        # Multiple adults with NULL email should be allowed
        Adult.objects.create(legal_first_name="A", last_name="B", personal_email=None)
        Adult.objects.create(legal_first_name="C", last_name="D", personal_email=None)
        self.assertEqual(Adult.objects.filter(personal_email__isnull=True).count(), 2)

    def test_adult_display_name_and_full_name(self):
        a1 = Adult(legal_first_name="Robert", last_name="Johnson")
        self.assertEqual(a1.display_name, "Robert Johnson")
        self.assertEqual(a1.full_name, "Robert Johnson")

        a2 = Adult(
            legal_first_name="Robert",
            preferred_first_name="Bob",
            last_name="Johnson",
        )
        self.assertEqual(a2.display_name, "Bob Johnson")
        self.assertEqual(a2.full_name, "Bob Johnson")


class AdultEmailUniquenessOrganizationScopingTests(TestCase):
    """``Adult.clean()``'s student-email-conflict check is scoped to the
    adult's own organization: a shared email across two different,
    unrelated organizations is not a conflict, and must not be rejected or
    otherwise reveal the existence of a same-email record in another
    tenant."""

    def setUp(self):
        self.org_a = Organization.objects.create(
            name="Org A", slug="org-a-adult-email-scope"
        )
        self.org_b = Organization.objects.create(
            name="Org B", slug="org-b-adult-email-scope"
        )

    def test_same_organization_adult_email_matching_student_is_rejected(self):
        Student.objects.create(
            organization=self.org_a,
            legal_first_name="Riley",
            last_name="Student",
            personal_email="shared@example.test",
        )
        adult = Adult(
            organization=self.org_a,
            legal_first_name="Pat",
            last_name="Guardian",
            personal_email="shared@example.test",
        )
        with self.assertRaises(ValidationError):
            adult.full_clean()

    def test_different_organization_adult_email_matching_student_is_allowed(self):
        Student.objects.create(
            organization=self.org_a,
            legal_first_name="Riley",
            last_name="Student",
            personal_email="shared@example.test",
        )
        other_org_adult = Adult(
            organization=self.org_b,
            legal_first_name="Pat",
            last_name="Guardian",
            personal_email="shared@example.test",
        )
        # Must NOT raise: a different organization's student with the same
        # email is not a conflict in a multi-organization portal.
        other_org_adult.full_clean()
