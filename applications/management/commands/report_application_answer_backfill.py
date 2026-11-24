from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Prefetch

from applications.answers import EXPERIENCE_QUESTIONS
from applications.models import Application, StudentApplicationAnswer
from organizations.models import Organization


class Command(BaseCommand):
    help = "Report approved student applications whose answers need manual review."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="Organization slug")
        parser.add_argument(
            "--check-copied",
            action="store_true",
            help="Also identify linked applications whose historical answers are not reflected on the student profile.",
        )

    def handle(self, *args, **options):
        organization = Organization.objects.filter(slug=options["organization"]).first()
        if organization is None:
            raise CommandError("Organization not found.")
        # This report's "no linked student" / "malformed answer data" checks
        # are written against the legacy JSON data and ``converted_student``
        # link; a profile-workflow (version 1) application is finalized onto
        # ``student_profile`` instead and never sets ``converted_student``,
        # so it would always be misreported here. Skip those rather than
        # produce false positives; they're reviewed through the typed
        # profile-workflow review screen instead.
        skipped_profile_workflow = (
            Application.objects.for_organization(organization)
            .filter(
                status__in=(
                    Application.Status.APPROVED,
                    Application.Status.APPROVED_SIGNED,
                    Application.Status.CONVERTED,
                ),
                profile_workflow_version=1,
            )
            .exclude(applicant_type=Application.Type.MENTOR)
            .count()
        )
        applications = (
            Application.objects.for_organization(organization)
            .filter(
                status__in=(
                    Application.Status.APPROVED,
                    Application.Status.APPROVED_SIGNED,
                    Application.Status.CONVERTED,
                ),
                profile_workflow_version=0,
            )
            .exclude(applicant_type=Application.Type.MENTOR)
            .select_related("converted_student", "program")
            .order_by("pk")
        )
        if options["check_copied"]:
            applications = applications.prefetch_related(
                Prefetch(
                    "student_answers",
                    queryset=StudentApplicationAnswer.objects.for_organization(
                        organization
                    ).filter(
                        student__organization=organization,
                        student_id=F("application__converted_student_id"),
                    ),
                    to_attr="reported_student_answers",
                )
            )
        count = 0
        for application in applications.iterator(chunk_size=500):
            if (
                application.program_id
                and application.program.organization_id != organization.pk
            ) or (
                application.converted_student_id
                and application.converted_student.organization_id != organization.pk
            ):
                reason = "inconsistent ownership"
            elif not application.converted_student_id:
                reason = "no linked student"
            else:
                data = application.data
                if not isinstance(data, dict):
                    reason = "malformed answer data"
                else:
                    malformed = False
                    for step in ("step6-experience", "step6"):
                        values = data.get(step)
                        if values is None:
                            continue
                        if not isinstance(values, dict) or any(
                            value is not None and not isinstance(value, str)
                            for key, value in values.items()
                            if key in EXPERIENCE_QUESTIONS
                        ):
                            malformed = True
                    if malformed:
                        reason = "malformed answer data"
                    elif options["check_copied"]:
                        copied = {
                            (answer.question_key, answer.answer)
                            for answer in application.reported_student_answers
                        }
                        seen = set()
                        missing = False
                        for step in ("step6-experience", "step6"):
                            for key, answer in (data.get(step) or {}).items():
                                if (
                                    key not in EXPERIENCE_QUESTIONS
                                    or answer is None
                                    or not answer.strip()
                                    or (key, answer) in seen
                                ):
                                    continue
                                seen.add((key, answer))
                                if (key, answer) not in copied:
                                    missing = True
                        if not missing:
                            continue
                        reason = "missing copied answers"
                    else:
                        continue
            self.stdout.write(f"{application.application_id}: {reason}")
            count += 1
        self.stdout.write(f"Applications requiring review: {count}")
        if skipped_profile_workflow:
            self.stdout.write(
                f"Skipped {skipped_profile_workflow} profile-workflow "
                "(version 1) application(s); review those through the "
                "typed profile-workflow review screen instead."
            )
