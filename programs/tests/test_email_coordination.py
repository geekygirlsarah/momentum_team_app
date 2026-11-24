"""Tests for the tenant-scoped profile-email coordination lock."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from organizations.models import Organization
from programs.models import Student
from programs.models.email_claims import ProfileEmailClaim
from programs.utils.email_coordination import (
    claim_profile_emails,
    claim_profile_instance_emails,
    normalize_claim_email,
)


class NormalizeClaimEmailTests(TestCase):
    def test_strips_and_casefolds(self):
        self.assertEqual(normalize_claim_email("  Pat@Example.COM "), "pat@example.com")

    def test_blank_and_none_normalize_to_empty_string(self):
        self.assertEqual(normalize_claim_email(""), "")
        self.assertEqual(normalize_claim_email(None), "")


class RequiresAtomicBlockTests(TransactionTestCase):
    """Outside of Django's TestCase (which always wraps tests in atomic),
    calling the helper without an explicit transaction must fail fast."""

    def test_requires_an_outer_atomic_block(self):
        org = Organization.objects.create(
            name="Atomic Test Org", slug="atomic-test-org"
        )
        with self.assertRaises(RuntimeError):
            claim_profile_emails(org, ["pat@example.com"])


class ClaimProfileEmailsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.get(slug="gos")
        self.other_org = Organization.objects.create(name="Other Org", slug="other-org")

    def test_blank_emails_take_no_lock_and_create_no_rows(self):
        with transaction.atomic():
            locked = claim_profile_emails(self.org, ["", None, "   "])
        self.assertEqual(locked, [])
        self.assertEqual(ProfileEmailClaim.objects.count(), 0)

    def test_creates_one_claim_row_per_normalized_email(self):
        with transaction.atomic():
            locked = claim_profile_emails(
                self.org, ["Pat@Example.com", "  pat@example.com  ", "Sam@Example.com"]
            )
        self.assertEqual(len(locked), 2)
        self.assertEqual(
            {claim.normalized_email for claim in locked},
            {"pat@example.com", "sam@example.com"},
        )
        self.assertEqual(
            ProfileEmailClaim.objects.filter(organization=self.org).count(), 2
        )

    def test_repeated_claims_reuse_the_same_row(self):
        with transaction.atomic():
            claim_profile_emails(self.org, ["pat@example.com"])
        with transaction.atomic():
            claim_profile_emails(self.org, ["pat@example.com"])
        self.assertEqual(
            ProfileEmailClaim.objects.filter(
                organization=self.org, normalized_email="pat@example.com"
            ).count(),
            1,
        )

    def test_claims_are_scoped_per_organization(self):
        with transaction.atomic():
            claim_profile_emails(self.org, ["pat@example.com"])
        with transaction.atomic():
            claim_profile_emails(self.other_org, ["pat@example.com"])
        self.assertEqual(
            ProfileEmailClaim.objects.filter(
                normalized_email="pat@example.com"
            ).count(),
            2,
        )
        self.assertTrue(
            ProfileEmailClaim.objects.filter(
                organization=self.org, normalized_email="pat@example.com"
            ).exists()
        )
        self.assertTrue(
            ProfileEmailClaim.objects.filter(
                organization=self.other_org, normalized_email="pat@example.com"
            ).exists()
        )

    def test_rollback_does_not_leave_behind_a_bogus_claim_user_state(self):
        """A rolled-back claimant leaves the claim row (harmless), not a partial write."""
        try:
            with transaction.atomic():
                claim_profile_emails(self.org, ["pat@example.com"])
                raise ValueError("simulated failure after claiming")
        except ValueError:
            pass
        # The lock row itself may or may not persist depending on whether the
        # get_or_create was rolled back with everything else; either way, a
        # second claimant must still be able to proceed without error.
        with transaction.atomic():
            locked = claim_profile_emails(self.org, ["pat@example.com"])
        self.assertEqual(len(locked), 1)


class ClaimProfileInstanceEmailsTests(TestCase):
    """Tests for the single-instance convenience wrapper used by the
    student/adult forms, CSV imports, alumni conversion, and merge writers."""

    def setUp(self):
        self.org = Organization.objects.get(slug="gos")

    def test_claims_both_personal_and_org_email(self):
        student = Student(
            organization=self.org,
            legal_first_name="Pat",
            last_name="Doe",
            personal_email="pat@example.com",
            org_email="pdoe@org.example.com",
        )
        with transaction.atomic():
            claim_profile_instance_emails(student)
        self.assertEqual(
            set(
                ProfileEmailClaim.objects.filter(organization=self.org).values_list(
                    "normalized_email", flat=True
                )
            ),
            {"pat@example.com", "pdoe@org.example.com"},
        )

    def test_no_organization_is_a_no_op(self):
        student = Student(legal_first_name="Pat", last_name="Doe", organization=None)
        with transaction.atomic():
            locked = claim_profile_instance_emails(student)
        self.assertEqual(locked, [])
        self.assertEqual(ProfileEmailClaim.objects.count(), 0)


class ClaimProfileEmailsConcurrencyTests(TransactionTestCase):
    """Real, multi-threaded proof that the lock serializes two concurrent
    "does this email already exist? if not, create it" writers for the same
    (organization, email) pair, the exact race the wizard's identity sync and
    every other profile-email writer depend on this lock to prevent.

    SQLite's ``select_for_update`` does not take a real row lock
    (``connection.features.has_select_for_update`` is False), so there only
    the sequential fallback below is meaningful; PostgreSQL (used by CI) runs
    the two writers on genuinely separate threads/connections and proves the
    second one blocks until the first's transaction finishes."""

    def setUp(self):
        self.organization = Organization.objects.create(
            name="Email Claim Concurrency",
            slug="email-claim-concurrency",
            subdomain="email-claim-concurrency",
        )

    def _claim_then_create_if_absent(self, email, barrier=None):
        close_old_connections()
        try:
            if barrier is not None:
                barrier.wait(timeout=10)
            with transaction.atomic():
                claim_profile_emails(self.organization, [email])
                if Student.objects.filter(
                    organization=self.organization, personal_email__iexact=email
                ).exists():
                    return None
                return Student.objects.create(
                    organization=self.organization,
                    legal_first_name="Pat",
                    last_name="Doe",
                    personal_email=email,
                )
        finally:
            connections.close_all()

    def test_only_one_concurrent_writer_creates_the_profile(self):
        email = "concurrent-claim@example.test"
        if connection.features.has_select_for_update:
            barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda _: self._claim_then_create_if_absent(email, barrier),
                        range(2),
                    )
                )
        else:
            results = [
                self._claim_then_create_if_absent(email),
                self._claim_then_create_if_absent(email),
            ]

        created = [result for result in results if result is not None]
        self.assertEqual(len(created), 1)
        self.assertEqual(
            Student.objects.filter(
                organization=self.organization, personal_email__iexact=email
            ).count(),
            1,
        )
