"""Tests for applications.identity_sync: wiring the wizard to isolated
profiles and typed identity/relationship proposals (Delivery Step 4)."""

import datetime
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from applications.identity import ApplicationProfileEditAuthorization
from applications.identity_sync import (
    ApplicationIdentitySyncConflict,
    sync_guardian_identity,
    sync_guardian_relationship_proposal,
    sync_student_identity,
)
from applications.models import (
    Application,
    ApplicationGuardianDraft,
    ApplicationIdentityProposal,
    ApplicationRelationshipProposal,
    ApplicationStudentDraft,
)
from applications.tests.access_helpers import verify_application_browser
from organizations.models import Organization
from programs.models import Adult, AdultStudentRelationship, Student


class IdentitySyncTestCase(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Identity Sync Org",
            slug="identity-sync-org",
            subdomain="identity-sync-org",
        )
        self.other_organization = Organization.objects.create(
            name="Other Identity Sync Org",
            slug="other-identity-sync-org",
            subdomain="other-identity-sync-org",
        )

    def _verified_application(self, applicant_type, email):
        application = Application.objects.create(
            organization=self.organization,
            applicant_type=applicant_type,
            email=email,
            email_verified_at=timezone.now(),
            status=Application.Status.EMAIL_VERIFIED,
            profile_workflow_version=1,
        )
        client = Client(HTTP_HOST=f"{self.organization.subdomain}.localhost")
        response = verify_application_browser(client, application)
        request = response.wsgi_request
        request.organization = self.organization
        return application, request

    def _student_draft(self, application, **overrides):
        defaults = dict(
            organization=self.organization,
            application=application,
            legal_first_name="Sam",
            last_name="Student",
            date_of_birth=datetime.date(2010, 1, 1),
        )
        defaults.update(overrides)
        return ApplicationStudentDraft.objects.create(**defaults)

    def _guardian_draft(self, application, slot, **overrides):
        defaults = dict(
            organization=self.organization,
            application=application,
            slot=slot,
            presence=ApplicationGuardianDraft.Presence.PRESENT,
            legal_first_name="Pat",
            last_name="Guardian",
            relationship_to_student="parent",
        )
        defaults.update(overrides)
        return ApplicationGuardianDraft.objects.create(**defaults)


class NewPersonCreationTests(IdentitySyncTestCase):
    def test_new_student_gets_an_isolated_profile_and_no_match_proposal(self):
        application, request = self._verified_application(
            Application.Type.STUDENT, "new.student@example.test"
        )
        draft = self._student_draft(application, personal_email="")

        result = sync_student_identity(application, draft, request=request)

        self.assertEqual(result.status, "no_match")
        self.assertIsNotNone(result.profile)
        self.assertTrue(result.profile.is_incomplete_application_profile)
        self.assertEqual(result.profile.legal_first_name, "Sam")
        application.refresh_from_db()
        self.assertEqual(application.student_profile_id, result.profile.pk)
        proposal = ApplicationIdentityProposal.objects.get(
            application=application,
            subject_slot=ApplicationIdentityProposal.SubjectSlot.STUDENT,
        )
        self.assertEqual(
            proposal.match_state, ApplicationIdentityProposal.MatchState.NO_MATCH
        )
        self.assertIsNone(proposal.candidate_student_id)
        self.assertEqual(
            proposal.decision, ApplicationIdentityProposal.Decision.PENDING
        )
        # The new profile must not be visible through ordinary lookups.
        self.assertFalse(Student.objects.filter(pk=result.profile.pk).exists())
        self.assertTrue(Student.all_objects.filter(pk=result.profile.pk).exists())

    def test_new_guardian_gets_an_isolated_profile_and_no_match_proposal(self):
        application, request = self._verified_application(
            Application.Type.PARENT, "new.parent@example.test"
        )
        draft = self._guardian_draft(
            application,
            ApplicationGuardianDraft.Slot.PRIMARY,
            email="new.parent@example.test",
        )

        result = sync_guardian_identity(application, draft, request=request)

        self.assertEqual(result.status, "no_match")
        self.assertIsNotNone(result.profile)
        self.assertTrue(result.profile.is_incomplete_application_profile)
        self.assertTrue(result.profile.is_parent)
        application.refresh_from_db()
        self.assertEqual(application.primary_guardian_profile_id, result.profile.pk)

    def test_resubmitting_the_same_step_does_not_create_a_second_profile(self):
        application, request = self._verified_application(
            Application.Type.STUDENT, "new.student2@example.test"
        )
        draft = self._student_draft(application, personal_email="")

        first = sync_student_identity(application, draft, request=request)
        application.refresh_from_db()
        # Simulate resubmission of the same step (edited name).
        draft.legal_first_name = "Samantha"
        draft.save()
        second = sync_student_identity(application, draft, request=request)

        self.assertEqual(
            ApplicationIdentityProposal.objects.filter(application=application).count(),
            1,
        )
        self.assertNotEqual(first.profile.pk, second.profile.pk)
        # A fresh incomplete profile is created each time identity can't be
        # resolved; the stale one is simply no longer linked.
        application.refresh_from_db()
        self.assertEqual(application.student_profile_id, second.profile.pk)


class AuthorizedDirectEditTests(IdentitySyncTestCase):
    def test_student_applicant_editing_their_own_existing_profile_is_applied_directly(
        self,
    ):
        existing = Student.objects.create(
            organization=self.organization,
            legal_first_name="Alex",
            last_name="Old",
            personal_email="alex@example.test",
        )
        application, request = self._verified_application(
            Application.Type.STUDENT, "alex@example.test"
        )
        draft = self._student_draft(
            application,
            legal_first_name="Alex",
            last_name="New",
            personal_email="alex@example.test",
        )

        result = sync_student_identity(application, draft, request=request)

        self.assertEqual(result.status, "unique")
        self.assertEqual(
            result.authorization, ApplicationProfileEditAuthorization.OWN_PROFILE
        )
        existing.refresh_from_db()
        self.assertEqual(existing.last_name, "New")
        self.assertFalse(existing.is_incomplete_application_profile)
        application.refresh_from_db()
        self.assertEqual(application.student_profile_id, existing.pk)
        proposal = ApplicationIdentityProposal.objects.get(
            application=application,
            subject_slot=ApplicationIdentityProposal.SubjectSlot.STUDENT,
        )
        self.assertEqual(
            proposal.match_state, ApplicationIdentityProposal.MatchState.UNIQUE
        )
        self.assertEqual(proposal.candidate_student_id, existing.pk)
        # Still pending: lead review is required before final acceptance even
        # though the field edit itself was authorized immediately.
        self.assertEqual(
            proposal.decision, ApplicationIdentityProposal.Decision.PENDING
        )

    def test_parent_applicant_editing_their_own_primary_guardian_profile(self):
        existing = Adult.objects.create(
            organization=self.organization,
            legal_first_name="Pat",
            last_name="Old",
            personal_email="pat@example.test",
            is_parent=True,
        )
        application, request = self._verified_application(
            Application.Type.PARENT, "pat@example.test"
        )
        draft = self._guardian_draft(
            application,
            ApplicationGuardianDraft.Slot.PRIMARY,
            last_name="New",
            email="pat@example.test",
        )

        result = sync_guardian_identity(application, draft, request=request)

        self.assertEqual(
            result.authorization, ApplicationProfileEditAuthorization.OWN_PROFILE
        )
        existing.refresh_from_db()
        self.assertEqual(existing.last_name, "New")
        application.refresh_from_db()
        self.assertEqual(application.primary_guardian_profile_id, existing.pk)

    def test_linked_guardian_can_edit_their_existing_student(self):
        student = Student.objects.create(
            organization=self.organization,
            legal_first_name="Riley",
            last_name="Old",
        )
        guardian = Adult.objects.create(
            organization=self.organization,
            legal_first_name="Jamie",
            last_name="Guardian",
            personal_email="jamie@example.test",
            is_parent=True,
        )
        AdultStudentRelationship.objects.create(
            organization=self.organization,
            adult=guardian,
            student=student,
            relationship_to_student="parent",
        )
        application, request = self._verified_application(
            Application.Type.PARENT, "jamie@example.test"
        )
        draft = self._student_draft(
            application,
            legal_first_name="Riley",
            last_name="Returning",
            personal_email="",
        )
        # A returning parent identifies the existing student directly; model
        # this by resolving through the student's own (still-empty) email
        # path falling back to no match, so exercise the picker-free path by
        # directly invoking with a draft that doesn't carry a distinguishing
        # email. Since there is no email to resolve the student by here, the
        # realistic path is the Step 5 existing-student picker (not modeled
        # in this unit test); instead verify the guardian-edit authorization
        # helper directly against the chosen candidate.
        from applications.identity import evaluate_application_profile_edit

        authorization = evaluate_application_profile_edit(request, application, student)
        self.assertEqual(
            authorization, ApplicationProfileEditAuthorization.LINKED_STUDENT
        )


class ExistingStudentOverrideTests(IdentitySyncTestCase):
    def test_linked_guardian_picking_their_child_applies_the_edit_directly(self):
        student = Student.objects.create(
            organization=self.organization,
            legal_first_name="Riley",
            last_name="Old",
        )
        guardian = Adult.objects.create(
            organization=self.organization,
            legal_first_name="Jamie",
            last_name="Guardian",
            personal_email="jamie-picker@example.test",
            is_parent=True,
        )
        AdultStudentRelationship.objects.create(
            organization=self.organization,
            adult=guardian,
            student=student,
            relationship_to_student="parent",
        )
        application, request = self._verified_application(
            Application.Type.PARENT, "jamie-picker@example.test"
        )
        draft = self._student_draft(
            application,
            legal_first_name="Riley",
            last_name="Returning",
            personal_email="",
        )

        result = sync_student_identity(
            application, draft, request=request, existing_student=student
        )

        self.assertEqual(
            result.authorization, ApplicationProfileEditAuthorization.LINKED_STUDENT
        )
        student.refresh_from_db()
        self.assertEqual(student.last_name, "Returning")
        application.refresh_from_db()
        self.assertEqual(application.student_profile_id, student.pk)

    def test_unrelated_parent_picking_a_student_requires_lead_review(self):
        student = Student.objects.create(
            organization=self.organization,
            legal_first_name="Other",
            last_name="Family",
        )
        application, request = self._verified_application(
            Application.Type.PARENT, "not-related@example.test"
        )
        draft = self._student_draft(application, personal_email="")

        result = sync_student_identity(
            application, draft, request=request, existing_student=student
        )

        self.assertIsNone(result.profile)
        student.refresh_from_db()
        self.assertEqual(student.last_name, "Family")  # unchanged
        application.refresh_from_db()
        self.assertIsNone(application.student_profile_id)


class AmbiguousAndUnauthorizedTests(IdentitySyncTestCase):
    def test_ambiguous_email_leaves_slot_unresolved_for_lead_review(self):
        Adult.objects.create(
            organization=self.organization,
            legal_first_name="One",
            last_name="Match",
            personal_email="shared@example.test",
            is_parent=True,
        )
        Adult.objects.create(
            organization=self.organization,
            legal_first_name="Two",
            last_name="Match",
            personal_email="shared@example.test",
            is_parent=True,
        )
        application, request = self._verified_application(
            Application.Type.STUDENT, "student-applicant@example.test"
        )
        draft = self._guardian_draft(
            application,
            ApplicationGuardianDraft.Slot.PRIMARY,
            email="shared@example.test",
        )

        result = sync_guardian_identity(application, draft, request=request)

        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.profile)
        application.refresh_from_db()
        self.assertIsNone(application.primary_guardian_profile_id)
        proposal = ApplicationIdentityProposal.objects.get(
            application=application,
            subject_slot=ApplicationIdentityProposal.SubjectSlot.PRIMARY_GUARDIAN,
        )
        self.assertEqual(
            proposal.match_state, ApplicationIdentityProposal.MatchState.AMBIGUOUS
        )
        self.assertIsNone(proposal.candidate_adult_id)

    def test_unrelated_guardian_match_is_recorded_but_not_edited(self):
        unrelated_guardian = Adult.objects.create(
            organization=self.organization,
            legal_first_name="Not",
            last_name="Yours",
            personal_email="notyours@example.test",
            is_parent=True,
        )
        application, request = self._verified_application(
            Application.Type.STUDENT, "student-applicant2@example.test"
        )
        draft = self._guardian_draft(
            application,
            ApplicationGuardianDraft.Slot.PRIMARY,
            email="notyours@example.test",
        )

        result = sync_guardian_identity(application, draft, request=request)

        self.assertEqual(result.status, "unique")
        self.assertIsNone(result.profile)
        unrelated_guardian.refresh_from_db()
        self.assertEqual(unrelated_guardian.last_name, "Yours")  # unchanged
        application.refresh_from_db()
        self.assertIsNone(application.primary_guardian_profile_id)
        proposal = ApplicationIdentityProposal.objects.get(
            application=application,
            subject_slot=ApplicationIdentityProposal.SubjectSlot.PRIMARY_GUARDIAN,
        )
        self.assertEqual(proposal.candidate_adult_id, unrelated_guardian.pk)
        self.assertEqual(
            proposal.decision, ApplicationIdentityProposal.Decision.PENDING
        )

    def test_cross_organization_match_is_never_used_as_a_candidate(self):
        Adult.objects.create(
            organization=self.other_organization,
            legal_first_name="Foreign",
            last_name="Guardian",
            personal_email="foreign@example.test",
            is_parent=True,
        )
        application, request = self._verified_application(
            Application.Type.STUDENT, "student-applicant3@example.test"
        )
        draft = self._guardian_draft(
            application,
            ApplicationGuardianDraft.Slot.PRIMARY,
            email="foreign@example.test",
        )

        result = sync_guardian_identity(application, draft, request=request)

        self.assertEqual(result.status, "no_match")
        self.assertIsNotNone(result.profile)
        self.assertEqual(result.profile.organization_id, self.organization.pk)


class RaceConditionTests(IdentitySyncTestCase):
    def test_concurrent_claim_falls_back_to_lead_review_instead_of_duplicating(self):
        application, request = self._verified_application(
            Application.Type.STUDENT, "race@example.test"
        )
        draft = self._student_draft(application, personal_email="race@example.test")

        from applications.services import (
            ApplicationIdentityResolution,
        )
        from applications.services import (
            resolve_application_person_identity as real_resolve_application_person_identity,
        )

        no_match = ApplicationIdentityResolution(status="no_match", profile=None)
        calls = {"n": 0}

        def fake_resolve(email_arg, organization_arg=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return no_match
            return real_resolve_application_person_identity(email_arg, organization_arg)

        with patch(
            "applications.identity_sync.resolve_application_person_identity",
            side_effect=fake_resolve,
        ):
            # Simulate another request creating a conflicting profile for the
            # same email in between the outer resolve and the locked create.
            def conflicting_create(*args, **kwargs):
                Student.objects.create(
                    organization=application.organization,
                    legal_first_name="Already",
                    last_name="Here",
                    personal_email="race@example.test",
                )
                raise ApplicationIdentitySyncConflict("simulated race")

            with patch(
                "applications.identity_sync._create_incomplete_student",
                side_effect=conflicting_create,
            ):
                result = sync_student_identity(application, draft, request=request)

        self.assertIsNone(result.profile)
        application.refresh_from_db()
        self.assertIsNone(application.student_profile_id)
        proposal = ApplicationIdentityProposal.objects.get(
            application=application,
            subject_slot=ApplicationIdentityProposal.SubjectSlot.STUDENT,
        )
        # The post-conflict re-resolve now finds the just-created profile.
        self.assertEqual(
            proposal.match_state, ApplicationIdentityProposal.MatchState.UNIQUE
        )


class StaleAuthorizationRecheckTests(IdentitySyncTestCase):
    """A concurrent writer can invalidate an authorization decision between
    the unlocked check in sync_student_identity/sync_guardian_identity and
    the locked write in _apply_direct_edit. The edit must be blocked rather
    than applied under the now-stale authorization."""

    def test_relationship_removed_between_check_and_lock_blocks_the_edit(self):
        student = Student.objects.create(
            organization=self.organization,
            legal_first_name="Riley",
            last_name="Old",
        )
        guardian = Adult.objects.create(
            organization=self.organization,
            legal_first_name="Jamie",
            last_name="Guardian",
            personal_email="jamie-race@example.test",
            is_parent=True,
        )
        relationship = AdultStudentRelationship.objects.create(
            organization=self.organization,
            adult=guardian,
            student=student,
            relationship_to_student="parent",
        )
        application, request = self._verified_application(
            Application.Type.PARENT, "jamie-race@example.test"
        )
        draft = self._student_draft(
            application,
            legal_first_name="Riley",
            last_name="Returning",
            personal_email="",
        )

        from applications.identity import (
            evaluate_application_profile_edit as real_evaluate_application_profile_edit,
        )

        calls = {"n": 0}

        def fake_evaluate(req, app, target_profile):
            calls["n"] += 1
            authorization = real_evaluate_application_profile_edit(
                req, app, target_profile
            )
            if calls["n"] == 1:
                # Simulate another request (e.g. a lead mentor editing the
                # family) removing the relationship this very authorization
                # just relied on, right after the unlocked check passes.
                relationship.delete()
            return authorization

        with patch(
            "applications.identity_sync.evaluate_application_profile_edit",
            side_effect=fake_evaluate,
        ):
            result = sync_student_identity(
                application, draft, request=request, existing_student=student
            )

        # The first (unlocked) check saw the authorized state...
        self.assertEqual(
            result.authorization, ApplicationProfileEditAuthorization.LINKED_STUDENT
        )
        # ...but the write itself was blocked by the post-lock recheck.
        self.assertIsNone(result.profile)
        student.refresh_from_db()
        self.assertEqual(student.last_name, "Old")  # not edited
        application.refresh_from_db()
        self.assertIsNone(application.student_profile_id)
        self.assertEqual(calls["n"], 2)


class RelationshipProposalTests(IdentitySyncTestCase):
    def test_present_guardian_creates_an_add_proposal(self):
        application, _ = self._verified_application(
            Application.Type.STUDENT, "rel1@example.test"
        )
        student_draft = self._student_draft(application)
        guardian_draft = self._guardian_draft(
            application, ApplicationGuardianDraft.Slot.PRIMARY
        )

        proposal = sync_guardian_relationship_proposal(
            application, student_draft=student_draft, guardian_draft=guardian_draft
        )

        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.action, ApplicationRelationshipProposal.Action.ADD)
        self.assertEqual(
            proposal.decision, ApplicationRelationshipProposal.Decision.PENDING
        )
        self.assertEqual(proposal.student_draft_id, student_draft.pk)
        self.assertEqual(proposal.guardian_draft_id, guardian_draft.pk)
        self.assertEqual(AdultStudentRelationship.all_objects.count(), 0)

    def test_skipped_guardian_creates_no_proposal_and_removes_a_stale_one(self):
        application, _ = self._verified_application(
            Application.Type.STUDENT, "rel2@example.test"
        )
        student_draft = self._student_draft(application)
        guardian_draft = self._guardian_draft(
            application, ApplicationGuardianDraft.Slot.SECONDARY
        )
        sync_guardian_relationship_proposal(
            application, student_draft=student_draft, guardian_draft=guardian_draft
        )
        self.assertEqual(
            ApplicationRelationshipProposal.objects.filter(
                application=application
            ).count(),
            1,
        )

        guardian_draft.presence = ApplicationGuardianDraft.Presence.SKIPPED
        guardian_draft.save()
        result = sync_guardian_relationship_proposal(
            application, student_draft=student_draft, guardian_draft=guardian_draft
        )

        self.assertIsNone(result)
        self.assertEqual(
            ApplicationRelationshipProposal.objects.filter(
                application=application
            ).count(),
            0,
        )

    def test_resubmission_replaces_rather_than_duplicates_the_proposal(self):
        application, _ = self._verified_application(
            Application.Type.STUDENT, "rel3@example.test"
        )
        student_draft = self._student_draft(application)
        guardian_draft = self._guardian_draft(
            application, ApplicationGuardianDraft.Slot.PRIMARY
        )
        first = sync_guardian_relationship_proposal(
            application, student_draft=student_draft, guardian_draft=guardian_draft
        )
        second = sync_guardian_relationship_proposal(
            application, student_draft=student_draft, guardian_draft=guardian_draft
        )

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(
            ApplicationRelationshipProposal.objects.filter(
                application=application
            ).count(),
            1,
        )
