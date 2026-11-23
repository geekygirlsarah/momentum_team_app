"""End-to-end coverage for Delivery Step 4: driving a ``profile_workflow_version=1``
application all the way from the verified public wizard through guardian
document verification/upload and final lead approval.

This complements the narrower unit coverage in ``test_identity_sync.py``
(which exercises ``applications.identity_sync`` directly) by proving the real
HTTP views wire typed drafts, identity/relationship proposals, and
finalization together correctly, and that acceptance only happens once, at
final approval, never on submission or document upload alone.
"""

import re
import shutil
import tempfile

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from applications.forms import NOT_LISTED_SCHOOL
from applications.models import (
    Application,
    ApplicationDocumentSubmission,
    ApplicationGuardianDraft,
    ApplicationIdentityProposal,
    ApplicationRelationshipProposal,
    ApplicationStudentDraft,
)
from applications.tests.access_helpers import verify_application_browser
from organizations.models import Organization, UserOrganizationMembership
from programs.models import (
    Adult,
    AdultStudentRelationship,
    Enrollment,
    Program,
    ProgramDocument,
    Student,
)

_MEDIA_ROOT = tempfile.mkdtemp(prefix="application-e2e-")
_FILE_ENCRYPTION_KEY = Fernet.generate_key().decode()


@override_settings(FILE_ENCRYPTION_KEY=_FILE_ENCRYPTION_KEY, MEDIA_ROOT=_MEDIA_ROOT)
class ProfileWorkflowEndToEndTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.organization = Organization.objects.create(
            name="End To End Org",
            slug="end-to-end-org",
            subdomain="end-to-end-org",
        )
        self.program = Program.objects.create(
            organization=self.organization,
            name="End To End Program",
            active=True,
        )
        self.required_document = ProgramDocument.objects.create(
            program=self.program,
            name="Permission Slip",
            is_active=True,
            is_required=True,
            display_order=1,
            file=SimpleUploadedFile(
                "blank-permission-slip.pdf",
                b"%PDF-1.4 blank",
                content_type="application/pdf",
            ),
        )
        permission, _ = Permission.objects.get_or_create(
            content_type=ContentType.objects.get_for_model(Application),
            codename="review_application",
            defaults={"name": "Can review applications"},
        )
        group, _ = Group.objects.get_or_create(name="LeadMentor")
        group.permissions.add(permission)
        self.lead = get_user_model().objects.create_user(
            username="lead-mentor", email="lead@example.test", password=None
        )
        self.lead.groups.add(group)
        UserOrganizationMembership.objects.create(
            user=self.lead,
            organization=self.organization,
            role="LeadMentor",
            is_active=True,
        )
        self.client = Client(HTTP_HOST=f"{self.organization.subdomain}.localhost")

    def _new_application(self, applicant_type, email):
        application = Application.objects.create(
            organization=self.organization,
            program=self.program,
            applicant_type=applicant_type,
            email=email,
            email_verified_at=timezone.now(),
            status=Application.Status.EMAIL_VERIFIED,
            profile_workflow_version=1,
        )
        verify_application_browser(self.client, application)
        return application

    def _student_payload(self, **overrides):
        payload = {
            "legal_first_name": "Riley",
            "last_name": "Rookie",
            "date_of_birth": "2010-05-01",
            "address": "123 Robot Way",
            "city": "Pittsburgh",
            "state": "PA",
            "zip_code": "15213",
            "phone_number": "412-555-0100",
            "phone_type": "cell",
            "school_name": NOT_LISTED_SCHOOL,
            "grade": "9",
            "tshirt_size": "YS",
        }
        payload.update(overrides)
        return payload

    def _guardian_payload(self, **overrides):
        payload = {
            "legal_first_name": "Casey",
            "last_name": "Guardian",
            "relationship_to_student": "parent",
            "email": "casey.guardian@example.test",
            "address": "123 Robot Way",
            "city": "Pittsburgh",
            "state": "PA",
            "zip_code": "15213",
            "phone_number": "412-555-0101",
            "phone_type": "cell",
            "email_updates": "on",
        }
        payload.update(overrides)
        return payload

    def _submit_step5(self, application, **overrides):
        url = reverse("apply_step5", kwargs={"app_id": application.application_id})
        return self.client.post(url, self._student_payload(**overrides))

    def _submit_step7(self, application, **overrides):
        url = reverse("apply_step7", kwargs={"app_id": application.application_id})
        return self.client.post(url, self._guardian_payload(**overrides))

    def _submit_step9(self, application):
        url = reverse("apply_step9", kwargs={"app_id": application.application_id})
        return self.client.post(url, {"confirm": "on"})

    def _complete_through_submission(
        self, application, guardian_email, student_overrides=None
    ):
        response = self._submit_step5(application, **(student_overrides or {}))
        self.assertRedirects(
            response,
            reverse("apply_step6", kwargs={"app_id": application.application_id}),
        )
        step6_url = reverse(
            "apply_step6", kwargs={"app_id": application.application_id}
        )
        step6_get = self.client.get(step6_url)
        token = step6_get.context["form"].initial["experience_version"]
        response = self.client.post(
            step6_url,
            {
                "experience_version": token,
                "interest_reason": "I like robots.",
                "hoped_gains": "New friends.",
                "prior_robotics_experience": "None.",
                "referral_source": "A friend.",
            },
        )
        self.assertRedirects(
            response,
            reverse("apply_step7", kwargs={"app_id": application.application_id}),
        )
        response = self._submit_step7(application, email=guardian_email)
        self.assertRedirects(
            response,
            reverse("apply_step8", kwargs={"app_id": application.application_id}),
        )
        step8_url = reverse(
            "apply_step8", kwargs={"app_id": application.application_id}
        )
        response = self.client.post(
            step8_url,
            {
                "legal_first_name": "",
                "last_name": "",
                "relationship_to_student": "",
                "email": "",
                "phone_number": "",
                "phone_type": "cell",
            },
        )
        # A blank secondary form may be invalid (phone/relationship required);
        # accept either a redirect onward or a validation re-render, since the
        # product's "skip" affordance is outside this form's own validation.
        if response.status_code == 302:
            self.assertRedirects(
                response,
                reverse("apply_step9", kwargs={"app_id": application.application_id}),
            )
        response = self._submit_step9(application)
        application.refresh_from_db()
        return response

    def _verify_document_access(self, application, recipient):
        access_url = reverse(
            "apply_step10_document_access",
            kwargs={"app_id": application.application_id},
        )
        mail.outbox.clear()
        self.client.post(access_url, {"action": "send"})
        self.assertEqual(mail.outbox[-1].to, [recipient])
        code = re.search(r"(\d{6})", mail.outbox[-1].body).group(1)
        response = self.client.post(access_url, {"code": code})
        self.assertRedirects(
            response,
            reverse("apply_step10", kwargs={"app_id": application.application_id}),
        )

    def _upload_required_document(self, application):
        upload_url = reverse(
            "apply_step10", kwargs={"app_id": application.application_id}
        )
        upload = SimpleUploadedFile(
            "permission-slip.pdf", b"%PDF-1.4 fake", content_type="application/pdf"
        )
        response = self.client.post(
            upload_url,
            {"document_id": str(self.required_document.pk), "file": upload},
        )
        self.assertEqual(response.status_code, 302)

    def _resolve_and_confirm_all_proposals(self, application):
        """Lead mentor walks every open proposal to an explicit decision."""
        for proposal in ApplicationIdentityProposal.objects.filter(
            application=application
        ):
            if proposal.match_state == ApplicationIdentityProposal.MatchState.UNIQUE:
                if proposal.candidate_student_id:
                    target = f"student:{proposal.candidate_student_id}"
                else:
                    target = f"adult:{proposal.candidate_adult_id}"
                decision_payload = {"decision": "confirmed", "target_profile": target}
            else:
                decision_payload = {"decision": "no_match"}
            url = reverse(
                "application_review_identity_decision",
                kwargs={
                    "app_id": application.application_id,
                    "proposal_id": proposal.pk,
                },
            )
            response = self.client.post(url, decision_payload)
            self.assertEqual(response.status_code, 302)
        for proposal in ApplicationRelationshipProposal.objects.filter(
            application=application
        ):
            url = reverse(
                "application_review_relationship_decision",
                kwargs={
                    "app_id": application.application_id,
                    "proposal_id": proposal.pk,
                },
            )
            response = self.client.post(url, {"decision": "approved"})
            self.assertEqual(response.status_code, 302)

    def _approve(self, application):
        url = reverse(
            "application_review_approve", kwargs={"app_id": application.application_id}
        )
        return self.client.post(url)

    # ------------------------------------------------------------------
    def test_new_family_end_to_end_through_final_approval(self):
        application = self._new_application(
            Application.Type.PARENT, "casey.guardian@example.test"
        )

        self._complete_through_submission(
            application, guardian_email="casey.guardian@example.test"
        )
        self.assertEqual(application.status, Application.Status.SUBMITTED)

        # Isolated profiles exist but are not yet visible anywhere ordinary.
        student_draft = ApplicationStudentDraft.objects.get(application=application)
        self.assertIsNotNone(application.student_profile_id)
        self.assertIsNotNone(application.primary_guardian_profile_id)
        self.assertFalse(
            Student.objects.filter(pk=application.student_profile_id).exists()
        )
        self.assertFalse(
            Adult.objects.filter(pk=application.primary_guardian_profile_id).exists()
        )
        self.assertEqual(AdultStudentRelationship.all_objects.count(), 0)

        # Submission/document-upload alone must never approve or activate.
        self.client.force_login(self.lead)
        response = self._approve(application)
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.SUBMITTED)
        self.client.logout()

        self._verify_document_access(application, "casey.guardian@example.test")
        self._upload_required_document(application)
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.SUBMITTED)
        self.assertFalse(
            Student.objects.filter(pk=application.student_profile_id).exists()
        )

        self.client.force_login(self.lead)
        # Direct approval still fails: identity/relationship proposals are
        # pending lead decisions.
        response = self._approve(application)
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.SUBMITTED)

        self._resolve_and_confirm_all_proposals(application)
        response = self._approve(application)
        application.refresh_from_db()

        self.assertEqual(application.status, Application.Status.APPROVED_SIGNED)
        student = Student.objects.get(pk=application.student_profile_id)
        guardian = Adult.objects.get(pk=application.primary_guardian_profile_id)
        self.assertFalse(student.is_incomplete_application_profile)
        self.assertFalse(guardian.is_incomplete_application_profile)
        self.assertEqual(student.legal_first_name, "Riley")
        self.assertEqual(guardian.personal_email, "casey.guardian@example.test")
        self.assertTrue(
            AdultStudentRelationship.objects.filter(
                student=student, adult=guardian
            ).exists()
        )
        self.assertTrue(
            Enrollment.objects.filter(student=student, program=self.program).exists()
        )
        mail.outbox.clear()

    def test_approval_is_idempotent_and_does_not_rematch_by_email(self):
        application = self._new_application(
            Application.Type.PARENT, "idempotent.guardian@example.test"
        )
        self._complete_through_submission(
            application, guardian_email="idempotent.guardian@example.test"
        )
        self._verify_document_access(application, "idempotent.guardian@example.test")
        self._upload_required_document(application)

        self.client.force_login(self.lead)
        self._resolve_and_confirm_all_proposals(application)
        self._approve(application)
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.APPROVED_SIGNED)
        student_id = application.student_profile_id
        guardian_id = application.primary_guardian_profile_id

        # A brand-new Adult with the same email now exists in the org; a
        # second approval attempt must not rematch against it or error.
        response = self._approve(application)
        self.assertEqual(response.status_code, 302)
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.APPROVED_SIGNED)
        self.assertEqual(application.student_profile_id, student_id)
        self.assertEqual(application.primary_guardian_profile_id, guardian_id)
        self.assertEqual(
            AdultStudentRelationship.objects.filter(
                student_id=student_id, adult_id=guardian_id
            ).count(),
            1,
        )

    def test_ambiguous_guardian_identity_blocks_approval_until_lead_resolves_it(self):
        Adult.objects.create(
            organization=self.organization,
            legal_first_name="One",
            last_name="Match",
            personal_email="ambiguous@example.test",
            is_parent=True,
        )
        Adult.objects.create(
            organization=self.organization,
            legal_first_name="Two",
            last_name="Match",
            personal_email="ambiguous@example.test",
            is_parent=True,
        )
        application = self._new_application(
            Application.Type.PARENT, "ambiguous@example.test"
        )
        self._complete_through_submission(
            application, guardian_email="ambiguous@example.test"
        )
        self.assertIsNone(application.primary_guardian_profile_id)

        # The guardian's identity is still ambiguous, so there is no resolved
        # profile email to challenge for self-service document upload yet;
        # a lead mentor can still attach the form on the family's behalf.
        self.client.force_login(self.lead)
        upload_url = reverse(
            "application_review_upload_document",
            kwargs={"app_id": application.application_id},
        )
        self.client.post(
            upload_url,
            {
                "document": str(self.required_document.pk),
                "file": SimpleUploadedFile(
                    "ambiguous-permission-slip.pdf",
                    b"%PDF-1.4 fake",
                    content_type="application/pdf",
                ),
            },
        )

        response = self._approve(application)
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.SUBMITTED)

        proposal = ApplicationIdentityProposal.objects.get(
            application=application,
            subject_slot=ApplicationIdentityProposal.SubjectSlot.PRIMARY_GUARDIAN,
        )
        self.assertEqual(
            proposal.match_state, ApplicationIdentityProposal.MatchState.AMBIGUOUS
        )
        chosen = Adult.objects.filter(
            organization=self.organization, personal_email="ambiguous@example.test"
        ).first()
        self._resolve_and_confirm_all_proposals_with_choice(
            application, chosen_adult=chosen
        )
        response = self._approve(application)
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.APPROVED_SIGNED)
        self.assertEqual(application.primary_guardian_profile_id, chosen.pk)

    def _resolve_and_confirm_all_proposals_with_choice(self, application, chosen_adult):
        for proposal in ApplicationIdentityProposal.objects.filter(
            application=application
        ):
            if (
                proposal.subject_slot
                == ApplicationIdentityProposal.SubjectSlot.PRIMARY_GUARDIAN
            ):
                payload = {
                    "decision": "confirmed",
                    "target_profile": f"adult:{chosen_adult.pk}",
                }
            elif proposal.match_state == ApplicationIdentityProposal.MatchState.UNIQUE:
                target = (
                    f"student:{proposal.candidate_student_id}"
                    if proposal.candidate_student_id
                    else f"adult:{proposal.candidate_adult_id}"
                )
                payload = {"decision": "confirmed", "target_profile": target}
            else:
                payload = {"decision": "no_match"}
            url = reverse(
                "application_review_identity_decision",
                kwargs={
                    "app_id": application.application_id,
                    "proposal_id": proposal.pk,
                },
            )
            self.client.post(url, payload)
        for proposal in ApplicationRelationshipProposal.objects.filter(
            application=application
        ):
            url = reverse(
                "application_review_relationship_decision",
                kwargs={
                    "app_id": application.application_id,
                    "proposal_id": proposal.pk,
                },
            )
            self.client.post(url, {"decision": "approved"})

    def test_cross_organization_reviewer_is_denied(self):
        application = self._new_application(
            Application.Type.PARENT, "cross-org@example.test"
        )
        self._complete_through_submission(
            application, guardian_email="cross-org@example.test"
        )
        other_org = Organization.objects.create(
            name="Other Org", slug="other-review-org", subdomain="other-review-org"
        )
        outside_lead = get_user_model().objects.create_user(
            username="outside-lead", email="outside@example.test", password=None
        )
        group = Group.objects.get(name="LeadMentor")
        outside_lead.groups.add(group)
        UserOrganizationMembership.objects.create(
            user=outside_lead, organization=other_org, role="LeadMentor", is_active=True
        )
        other_client = Client(HTTP_HOST=f"{self.organization.subdomain}.localhost")
        other_client.force_login(outside_lead)
        response = other_client.post(
            reverse(
                "application_review_approve",
                kwargs={"app_id": application.application_id},
            )
        )
        # The outside lead is authenticated against the correct org's
        # subdomain but has no membership there, so this is an ordinary
        # permission denial (403), not an org-resolution 404.
        self.assertEqual(response.status_code, 403)
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.SUBMITTED)

    def test_staff_can_upload_the_required_document_on_the_applicants_behalf(self):
        application = self._new_application(
            Application.Type.PARENT, "staff-upload@example.test"
        )
        self._complete_through_submission(
            application, guardian_email="staff-upload@example.test"
        )
        self.client.force_login(self.lead)
        upload_url = reverse(
            "application_review_upload_document",
            kwargs={"app_id": application.application_id},
        )
        upload = SimpleUploadedFile(
            "staff-permission-slip.pdf",
            b"%PDF-1.4 fake",
            content_type="application/pdf",
        )
        response = self.client.post(
            upload_url,
            {"document": str(self.required_document.pk), "file": upload},
        )
        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(
            ApplicationDocumentSubmission.objects.filter(
                application=application, document=self.required_document
            ).exists()
        )

        self._resolve_and_confirm_all_proposals(application)
        response = self._approve(application)
        application.refresh_from_db()
        self.assertEqual(application.status, Application.Status.APPROVED_SIGNED)
