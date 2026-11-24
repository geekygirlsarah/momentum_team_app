from django.core.exceptions import ValidationError
from django.test import TestCase

from organizations.models import Organization
from programs.models import Adult, RaceEthnicity, School, Student


class StudentModelTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="Carnegie High")
        self.student = Student.objects.create(
            legal_first_name="Alex",
            last_name="Smith",
            school=self.school,
        )

    def test_student_save_does_not_override_parent_opt_out(self):
        # Saving a student must NOT silently flip a parent's explicit opt-out.
        # The email_updates preference is the parent's own choice and should
        # only be changed through the application wizard form validation.
        parent = Adult.objects.create(
            legal_first_name="Pat",
            last_name="Smith",
            is_parent=True,
            email_updates=False,
        )
        s = Student(legal_first_name="Riley", last_name="Jones", primary_contact=parent)
        s.save()
        parent.refresh_from_db()
        self.assertFalse(parent.email_updates)

    def test_student_display_name_fallback(self):
        s1 = Student(legal_first_name="Alexandra", last_name="Smith")
        self.assertEqual(s1.display_name, "Alexandra Smith")

        s2 = Student(
            legal_first_name="Alexandra", preferred_first_name="Alex", last_name="Smith"
        )
        self.assertEqual(s2.display_name, "Alex Smith")
        self.assertEqual(s2.full_name, "Alex Smith")

    def test_student_member_since_year(self):
        from datetime import date

        from django.utils import timezone

        from programs.models import Enrollment, Program

        # Case 1: Fresh student without enrollments uses creation year
        s = Student.objects.create(legal_first_name="Fresh", last_name="Student")
        self.assertEqual(s.member_since_year, s.created_at.year)

        # Case 2: Student enrolled in a program starting earlier uses earliest program start year
        prog_older = Program.objects.create(
            name="Past Program", start_date=date(2022, 9, 1)
        )
        prog_newer = Program.objects.create(
            name="Current Program", start_date=date(2025, 1, 1)
        )
        Enrollment.objects.create(student=s, program=prog_newer)
        Enrollment.objects.create(student=s, program=prog_older)

        # Uncached check
        s_fresh = Student.objects.get(pk=s.pk)
        self.assertEqual(s_fresh.member_since_year, 2022)

        # Prefetched check
        s_prefetched = Student.objects.prefetch_related("enrollment_set__program").get(
            pk=s.pk
        )
        self.assertEqual(s_prefetched.member_since_year, 2022)


class DemographicModelTests(TestCase):
    def test_race_ethnicity_match_from_text(self):
        keys = [
            ("black-or-african-american", "Black or African-American"),
            ("hispanic-or-latino", "Hispanic or Latino"),
            ("asian", "Asian"),
            ("white", "White"),
            ("other", "Other"),
        ]
        for k, n in keys:
            RaceEthnicity.objects.get_or_create(key=k, defaults={"name": n})
        qs = RaceEthnicity.match_from_text("Black, Hispanic and Asian")
        self.assertSetEqual(
            set(qs.values_list("key", flat=True)),
            {"black-or-african-american", "hispanic-or-latino", "asian"},
        )
        qs2 = RaceEthnicity.match_from_text("Something totally unknown")
        self.assertIn("other", set(qs2.values_list("key", flat=True)))


class StudentEmailUniquenessOrganizationScopingTests(TestCase):
    """``Student.clean()``'s email-uniqueness check is scoped to the
    student's own organization: a shared email across two different,
    unrelated organizations is not a conflict, and must not be rejected or
    otherwise reveal the existence of a same-email record in another
    tenant."""

    def setUp(self):
        self.org_a = Organization.objects.create(name="Org A", slug="org-a-email-scope")
        self.org_b = Organization.objects.create(name="Org B", slug="org-b-email-scope")

    def test_same_organization_duplicate_student_email_is_rejected(self):
        Student.objects.create(
            organization=self.org_a,
            legal_first_name="Riley",
            last_name="First",
            personal_email="shared@example.test",
        )
        duplicate = Student(
            organization=self.org_a,
            legal_first_name="Jordan",
            last_name="Second",
            personal_email="shared@example.test",
        )
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_same_organization_student_email_matching_adult_is_rejected(self):
        Adult.objects.create(
            organization=self.org_a,
            legal_first_name="Pat",
            last_name="Guardian",
            personal_email="shared@example.test",
        )
        student = Student(
            organization=self.org_a,
            legal_first_name="Jordan",
            last_name="Second",
            personal_email="shared@example.test",
        )
        with self.assertRaises(ValidationError):
            student.full_clean()

    def test_different_organization_duplicate_student_email_is_allowed(self):
        Student.objects.create(
            organization=self.org_a,
            legal_first_name="Riley",
            last_name="First",
            personal_email="shared@example.test",
        )
        other_org_student = Student(
            organization=self.org_b,
            legal_first_name="Jordan",
            last_name="Second",
            personal_email="shared@example.test",
        )
        # Must NOT raise: a different organization's record with the same
        # email is not a conflict in a multi-organization portal.
        other_org_student.full_clean()

    def test_different_organization_student_email_matching_adult_is_allowed(self):
        Adult.objects.create(
            organization=self.org_a,
            legal_first_name="Pat",
            last_name="Guardian",
            personal_email="shared@example.test",
        )
        other_org_student = Student(
            organization=self.org_b,
            legal_first_name="Jordan",
            last_name="Second",
            personal_email="shared@example.test",
        )
        other_org_student.full_clean()


class UnsavedStudentModelTests(TestCase):
    def test_all_parents_on_unsaved_student(self):
        student = Student(legal_first_name="Unsaved", last_name="Student")
        # Accessing all_parents should not raise ValueError
        try:
            parents = student.all_parents
            self.assertEqual(parents, [])
        except ValueError as e:
            self.fail(f"all_parents() raised ValueError on unsaved student: {e}")
