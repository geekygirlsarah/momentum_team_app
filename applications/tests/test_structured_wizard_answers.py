from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from applications.models import (
    Application,
    ApplicationFixedAnswer,
    ApplicationQuestionAnswer,
    ApplicationQuestionSet,
    ProgramApplicationQuestion,
)
from applications.tests.access_helpers import verify_application_browser
from organizations.models import Organization
from programs.models import Program


class StructuredWizardAnswersTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Structured Answers",
            slug="structured-wizard-answers",
            subdomain="structured-wizard-answers",
        )
        self.program = Program.objects.create(
            organization=self.organization,
            name="Structured Program",
        )
        self.required_question = ProgramApplicationQuestion.objects.create(
            organization=self.organization,
            program=self.program,
            text="Why do you want to join?",
            is_required=True,
            display_order=1,
        )
        self.optional_question = ProgramApplicationQuestion.objects.create(
            organization=self.organization,
            program=self.program,
            text="Anything else?",
            is_required=False,
            display_order=2,
        )
        self.application = Application.objects.create(
            organization=self.organization,
            program=self.program,
            applicant_type=Application.Type.STUDENT,
            email="student@example.test",
            email_verified_at=timezone.now(),
            profile_workflow_version=1,
            data={"preserved": {"value": "legacy"}},
        )
        self.client = Client(HTTP_HOST=f"{self.organization.subdomain}.localhost")
        verify_application_browser(self.client, self.application)
        self.step6_url = reverse(
            "apply_step6", kwargs={"app_id": self.application.application_id}
        )
        self.confirm_url = reverse(
            "apply_step9", kwargs={"app_id": self.application.application_id}
        )

    def test_step6_freezes_typed_questions_without_writing_legacy_json(self):
        response = self.client.get(self.step6_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Why do you want to join?")
        self.assertContains(response, "Anything else?")
        question_set = ApplicationQuestionSet.objects.get(
            application=self.application,
            program=self.program,
            organization=self.organization,
        )
        answers = list(question_set.answers.order_by("position"))
        self.assertEqual(len(answers), 2)
        self.assertEqual(answers[0].question_id, self.required_question.question_id)
        self.assertEqual(answers[0].question_text, "Why do you want to join?")
        self.assertTrue(answers[0].is_required)
        self.assertEqual(answers[0].answer, "")
        self.assertEqual(self.application.data, {"preserved": {"value": "legacy"}})

    def test_step6_saves_typed_fixed_and_custom_answers(self):
        response = self.client.get(self.step6_url)
        token = response.context["form"].initial["experience_version"]

        response = self.client.post(
            self.step6_url,
            {
                "experience_version": token,
                "interest_reason": "I enjoy building robots.",
                "hoped_gains": "Learn more engineering.",
                "prior_robotics_experience": "None yet.",
                "referral_source": "A friend.",
                f"question_{self.required_question.question_id}": "I like teamwork.",
                f"question_{self.optional_question.question_id}": "",
            },
        )

        self.assertRedirects(
            response,
            reverse("apply_step7", kwargs={"app_id": self.application.application_id}),
        )
        fixed_answers = {
            item.question_key: item.answer
            for item in ApplicationFixedAnswer.objects.filter(
                application=self.application,
                organization=self.organization,
                source_step="step6-experience",
            )
        }
        self.assertEqual(
            fixed_answers,
            {
                "interest_reason": "I enjoy building robots.",
                "hoped_gains": "Learn more engineering.",
                "prior_robotics_experience": "None yet.",
                "referral_source": "A friend.",
            },
        )
        typed_answers = {
            item.question_id: item.answer
            for item in ApplicationQuestionAnswer.objects.filter(
                question_set__application=self.application
            )
        }
        self.assertEqual(
            typed_answers,
            {
                self.required_question.question_id: "I like teamwork.",
                self.optional_question.question_id: "",
            },
        )
        self.application.refresh_from_db()
        self.assertEqual(self.application.data, {"preserved": {"value": "legacy"}})

    def test_stale_step6_form_does_not_overwrite_typed_answers(self):
        response = self.client.get(self.step6_url)
        token = response.context["form"].initial["experience_version"]
        answer = ApplicationFixedAnswer.objects.create(
            organization=self.organization,
            application=self.application,
            source_step="step6-experience",
            question_key="interest_reason",
            answer="Saved by another writer.",
        )

        response = self.client.post(
            self.step6_url,
            {
                "experience_version": token,
                "interest_reason": "Stale replacement.",
                "hoped_gains": "",
                "prior_robotics_experience": "",
                "referral_source": "",
                f"question_{self.required_question.question_id}": "A required answer.",
                f"question_{self.optional_question.question_id}": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "changed after you opened this page")
        answer.refresh_from_db()
        self.assertEqual(answer.answer, "Saved by another writer.")

    def test_program_change_rejects_stale_step6_submission(self):
        response = self.client.get(self.step6_url)
        token = response.context["form"].initial["experience_version"]
        new_program = Program.objects.create(
            organization=self.organization,
            name="New Program",
        )
        Application.objects.filter(pk=self.application.pk).update(program=new_program)

        response = self.client.post(
            self.step6_url,
            {
                "experience_version": token,
                "interest_reason": "Must not be saved.",
                "hoped_gains": "",
                "prior_robotics_experience": "",
                "referral_source": "",
                f"question_{self.required_question.question_id}": "A required answer.",
                f"question_{self.optional_question.question_id}": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "program changed")
        self.assertFalse(
            ApplicationFixedAnswer.objects.filter(application=self.application).exists()
        )
        self.assertEqual(
            ApplicationQuestionSet.objects.filter(application=self.application).count(),
            1,
        )

    def test_confirmation_displays_typed_answers_and_rejects_missing_required_answer(
        self,
    ):
        step6 = self.client.get(self.step6_url)
        token = step6.context["form"].initial["experience_version"]
        ApplicationFixedAnswer.objects.create(
            organization=self.organization,
            application=self.application,
            source_step="step6-experience",
            question_key="interest_reason",
            answer="Typed fixed response.",
        )
        answer = ApplicationQuestionAnswer.objects.get(
            question_set__application=self.application,
            question_id=self.required_question.question_id,
        )
        answer.answer = ""
        answer.save(update_fields=["answer"])

        response = self.client.get(self.confirm_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Typed fixed response.")
        self.assertContains(response, "Why do you want to join?")
        self.assertContains(response, "Not answered")

        response = self.client.post(self.confirm_url, {"confirm": "on"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response, "Required custom application questions are unanswered"
        )
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, Application.Status.DRAFT)
        self.assertIsNone(self.application.submitted_at)

    def test_legacy_application_keeps_json_step6_behavior(self):
        self.application.profile_workflow_version = 0
        self.application.save(update_fields=["profile_workflow_version"])

        response = self.client.get(self.step6_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Why do you want to join?")
        self.assertFalse(
            ApplicationQuestionSet.objects.filter(application=self.application).exists()
        )
        self.assertEqual(self.application.data["preserved"]["value"], "legacy")
