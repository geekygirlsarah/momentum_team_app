"""Connect the verified public wizard to isolated profiles and proposals.

This module implements Delivery Step 4 of the application-process redesign:
wiring ``profile_workflow_version=1`` wizard steps to typed drafts, isolated
``Student``/``Adult`` profiles, and the identity/relationship proposals that
``applications.proposals`` already knows how to review and finalize.

Every function here must run inside the caller's existing lock on the
``Application`` row (see ``applications.views.utils._locked_application_queryset``),
since each one reads and writes the application's profile links. An identity
proposal is deleted and recreated on every call so its baseline and any prior
lead-review decision stay in sync with the just-submitted step data -
mirroring how editing a typed draft already resets dependent decisions
(see ``ApplicationTypedProfileDraftEditView``).

Authorization for an immediate, pre-review profile edit is delegated to
``applications.identity.evaluate_application_profile_edit``, which already
encodes the approved own-account, OTP-unique-email, and existing-linked-
guardian rules. Only those authorized cases write directly to an *existing*
profile; every other case (new person, ambiguous identity, unrelated
guardian) leaves the slot for lead review and changes no live relationship.
New-person creation and authorized direct edits both take the tenant-scoped
``programs.utils.email_coordination.claim_profile_emails`` lock first, so a
concurrent writer (the wizard itself, or an existing staff form) cannot claim
the same organization/email at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q

from audit.events import AuditEvent
from audit.service import log_event
from programs.models import Adult, School, Student
from programs.utils.email_coordination import (
    claim_profile_emails,
    normalize_claim_email,
)

from .identity import (
    ApplicationProfileEditAuthorization,
    evaluate_application_profile_edit,
)
from .models import (
    Application,
    ApplicationGuardianDraft,
    ApplicationIdentityProposal,
    ApplicationRelationshipProposal,
)
from .proposals import (
    GUARDIAN_DRAFT_PROFILE_FIELD_MAP,
    STUDENT_DRAFT_PROFILE_FIELD_MAP,
    ApplicationFinalizationError,
    draft_profile_fields,
    student_draft_race_ethnicity_ids,
)
from .services import ApplicationIdentityResolution, resolve_application_person_identity

#: Authorization outcomes that permit an immediate, pre-review profile edit.
_AUTHORIZED_EDIT_STATES = {
    ApplicationProfileEditAuthorization.OWN_PROFILE,
    ApplicationProfileEditAuthorization.LINKED_ACCOUNT,
    ApplicationProfileEditAuthorization.LINKED_STUDENT,
}

_SLOT_PROFILE_FIELD = {
    ApplicationIdentityProposal.SubjectSlot.STUDENT: "student_profile",
    ApplicationIdentityProposal.SubjectSlot.PRIMARY_GUARDIAN: "primary_guardian_profile",
    ApplicationIdentityProposal.SubjectSlot.SECONDARY_GUARDIAN: "secondary_guardian_profile",
}

_GUARDIAN_SLOT_TO_SUBJECT_SLOT = {
    ApplicationGuardianDraft.Slot.PRIMARY: ApplicationIdentityProposal.SubjectSlot.PRIMARY_GUARDIAN,
    ApplicationGuardianDraft.Slot.SECONDARY: ApplicationIdentityProposal.SubjectSlot.SECONDARY_GUARDIAN,
}


class ApplicationIdentitySyncConflict(Exception):
    """A concurrent writer claimed the same email while this one was syncing.

    The caller should treat the slot as unresolved (left for lead review)
    rather than retry automatically, since retrying could still lose a race
    against a human filling out the same step twice.
    """


@dataclass
class IdentitySyncResult:
    slot: str
    status: str  # resolve_application_person_identity().status
    proposal: ApplicationIdentityProposal
    profile: "Student | Adult | None"
    authorization: ApplicationProfileEditAuthorization | None


def _using(application, using=None) -> str:
    return using or application._state.db or "default"


def _reset_identity_proposal(application, *, slot, using) -> None:
    ApplicationIdentityProposal.objects.using(using).filter(
        application_id=application.pk,
        organization_id=application.organization_id,
        subject_slot=slot,
    ).delete()


def _create_identity_proposal(application, *, slot, resolution, using):
    kwargs = dict(
        organization=application.organization,
        application=application,
        subject_slot=slot,
    )
    if resolution.status == "unique":
        profile = resolution.profile
        kwargs["match_state"] = ApplicationIdentityProposal.MatchState.UNIQUE
        if isinstance(profile, Student):
            kwargs["candidate_student"] = profile
        else:
            kwargs["candidate_adult"] = profile
        kwargs["candidate_baseline_revision"] = profile.profile_revision
    elif resolution.status == "ambiguous":
        kwargs["match_state"] = ApplicationIdentityProposal.MatchState.AMBIGUOUS
    else:
        kwargs["match_state"] = ApplicationIdentityProposal.MatchState.NO_MATCH
    proposal = ApplicationIdentityProposal(**kwargs)
    proposal.save(using=using)
    return proposal


def _set_slot_profile(application, *, slot, profile, using) -> None:
    field_name = _SLOT_PROFILE_FIELD[slot]
    setattr(application, field_name, profile)
    application.save(using=using, update_fields=[field_name, "updated_at"])


def _student_school_updates(application, draft, *, using):
    """Return the ``Student`` field updates for a draft's school choice.

    Mirrors the school handling in ``applications.proposals``'s finalization
    path: an explicit "not listed" clears any school, a named school is
    looked up or created immediately (this always runs inside the caller's
    lock on the ``Application`` row), and an unanswered school leaves the
    field untouched.
    """
    if draft.school_not_listed is True:
        return {"school": None}
    if draft.school_name is not None and draft.school_name.strip():
        school, _ = School._base_manager.using(using).get_or_create(
            organization_id=application.organization_id,
            name=draft.school_name.strip(),
        )
        return {"school": school}
    return {}


def _create_incomplete_student(application, draft, *, using):
    """Create an incomplete ``Student`` profile for a genuinely new person,
    populated with everything the applicant has entered so far.

    This copies the same ``STUDENT_DRAFT_PROFILE_FIELD_MAP`` fields (plus
    school and race/ethnicity) that ``finalize_profile_workflow_application``
    (``applications.proposals``) later re-applies at acceptance time, so the
    isolated profile reflects the applicant's answers as they progress
    through the wizard rather than only once a lead approves. Re-applying the
    same, already-current values at finalization is a no-op.
    """
    organization = application.organization
    email = draft.personal_email or ""
    claim_profile_emails(organization, [email], using=using)
    if (
        email
        and resolve_application_person_identity(email, organization).status
        != "no_match"
    ):
        raise ApplicationIdentitySyncConflict(
            "A matching student or guardian now exists for this email."
        )
    fields = draft_profile_fields(draft, STUDENT_DRAFT_PROFILE_FIELD_MAP)
    fields["personal_email"] = email or None
    fields.update(_student_school_updates(application, draft, using=using))
    student = Student(
        organization=organization,
        is_incomplete_application_profile=True,
        **fields,
    )
    student.save(using=using)
    try:
        race_ids = student_draft_race_ethnicity_ids(application, draft, using)
    except ApplicationFinalizationError as exc:
        raise ApplicationIdentitySyncConflict(str(exc)) from exc
    if race_ids:
        student.race_ethnicities.set(race_ids)
    return student


def _create_incomplete_guardian(application, draft, *, using):
    """Create an incomplete ``Adult`` profile for a genuinely new guardian,
    populated with everything the applicant has entered so far.

    See :func:`_create_incomplete_student` for why the full draft field map
    is copied immediately rather than only at finalization.
    """
    organization = application.organization
    email = draft.email or ""
    claim_profile_emails(organization, [email], using=using)
    if (
        email
        and resolve_application_person_identity(email, organization).status
        != "no_match"
    ):
        raise ApplicationIdentitySyncConflict(
            "A matching student or guardian now exists for this email."
        )
    fields = draft_profile_fields(draft, GUARDIAN_DRAFT_PROFILE_FIELD_MAP)
    fields["personal_email"] = email or None
    adult = Adult(
        organization=organization,
        is_incomplete_application_profile=True,
        is_parent=True,
        **fields,
    )
    adult.save(using=using)
    return adult


def _apply_direct_edit(application, profile, draft, field_map, *, request, using):
    """Lock ``profile``, copy non-null draft fields onto it, and audit the change.

    Unlike new-profile creation, this is the only place that syncs the draft
    onto an authorized *existing* (non-incomplete) profile: finalization in
    ``applications.proposals`` only ever rewrites a profile that is still
    incomplete, so an existing profile's fields need to be applied here, as
    soon as the edit is authorized, using the same field maps that
    finalization uses for incomplete profiles.

    Returns the freshly locked, updated profile. Raises
    ``ApplicationIdentitySyncConflict`` if the profile is gone or has become
    an incomplete profile (someone else's draft) since it was resolved.
    """
    model = type(profile)
    organization = application.organization
    new_email = (
        getattr(draft, "personal_email", None)
        if model is Student
        else getattr(draft, "email", None)
    )
    if new_email and normalize_claim_email(new_email) != normalize_claim_email(
        profile.personal_email
    ):
        claim_profile_emails(organization, [new_email], using=using)

    locked = (
        model.all_objects.using(using)
        .select_for_update()
        .filter(pk=profile.pk, organization_id=organization.id)
        .first()
    )
    if locked is None or locked.is_incomplete_application_profile:
        raise ApplicationIdentitySyncConflict(
            "The matched profile is no longer available for a direct edit."
        )
    # The caller already evaluated authorization against an *unlocked* read of
    # this profile. Re-evaluate it against the just-locked row before writing
    # anything: a concurrent writer could have unlinked the requester's
    # account, removed the guardian relationship this authorization relied
    # on, or otherwise changed the facts between that check and this lock.
    # Without this recheck, a now-stale authorization would still apply the
    # edit under the fresh lock.
    if (
        evaluate_application_profile_edit(request, application, locked)
        not in _AUTHORIZED_EDIT_STATES
    ):
        raise ApplicationIdentitySyncConflict(
            "Authorization for this profile changed while applying the edit."
        )
    before = {"personal_email": locked.personal_email}
    changed_fields = []
    for draft_field, profile_field in field_map.items():
        value = getattr(draft, draft_field)
        if value is None or getattr(locked, profile_field) == value:
            continue
        setattr(locked, profile_field, value)
        changed_fields.append(profile_field)
    if changed_fields:
        locked.save(using=using, update_fields=[*changed_fields, "updated_at"])
        log_event(
            request=request,
            event=AuditEvent.CONTACT_INFO_UPDATED,
            resource=locked,
            before=before,
            after={"application_id": application.application_id},
            notes="Profile updated from an in-progress application by the verified applicant.",
        )
    return locked


def _candidate_email_for_student(application, draft) -> str:
    if application.applicant_type == Application.Type.STUDENT:
        return application.email or draft.personal_email or ""
    return draft.personal_email or ""


def _candidate_email_for_guardian(application, draft) -> str:
    if (
        draft.slot == ApplicationGuardianDraft.Slot.PRIMARY
        and application.applicant_type == Application.Type.PARENT
    ):
        return application.email or draft.email or ""
    return draft.email or ""


def sync_student_identity(
    application, draft, *, request, existing_student=None, using=None
) -> IdentitySyncResult:
    """Resolve identity for the student slot and link or create a profile.

    Must run inside the caller's lock on ``application``. Creates a fresh
    incomplete ``Student`` profile for a genuinely new person, applies an
    immediate field edit for an authorized existing-profile match, or leaves
    the slot unresolved (proposal only) for an ambiguous or unauthorized
    match pending lead review.

    ``existing_student`` lets a caller that already identified a specific
    same-organization student some other way (the Step 5 existing-student
    picker available to parent applicants) short-circuit email resolution and
    treat that student as the one and only candidate; authorization is still
    evaluated normally, so an unrelated guardian's pick still falls back to
    lead review rather than an immediate edit.
    """
    using = _using(application, using)
    slot = ApplicationIdentityProposal.SubjectSlot.STUDENT
    if existing_student is not None:
        resolution = ApplicationIdentityResolution(
            status="unique", profile=existing_student
        )
    else:
        email = _candidate_email_for_student(application, draft)
        resolution = resolve_application_person_identity(
            email, application.organization
        )

    # Link or edit the profile *before* creating the proposal below: the
    # proposal's baseline is captured from the application row as it exists
    # at save time, and _set_slot_profile()/_apply_direct_edit() both touch
    # that row's updated_at. Creating the proposal first would make it stale
    # the instant this same call finishes linking the profile.
    profile = None
    authorization = None
    if resolution.status == "no_match":
        try:
            profile = _create_incomplete_student(application, draft, using=using)
        except ApplicationIdentitySyncConflict:
            resolution = resolve_application_person_identity(
                _candidate_email_for_student(application, draft),
                application.organization,
            )
        else:
            _set_slot_profile(application, slot=slot, profile=profile, using=using)
    elif resolution.status == "unique" and isinstance(resolution.profile, Student):
        authorization = evaluate_application_profile_edit(
            request, application, resolution.profile
        )
        if authorization in _AUTHORIZED_EDIT_STATES:
            try:
                profile = _apply_direct_edit(
                    application,
                    resolution.profile,
                    draft,
                    STUDENT_DRAFT_PROFILE_FIELD_MAP,
                    request=request,
                    using=using,
                )
            except ApplicationIdentitySyncConflict:
                profile = None
            else:
                _set_slot_profile(application, slot=slot, profile=profile, using=using)

    _reset_identity_proposal(application, slot=slot, using=using)
    proposal = _create_identity_proposal(
        application, slot=slot, resolution=resolution, using=using
    )

    return IdentitySyncResult(
        slot=slot,
        status=resolution.status,
        proposal=proposal,
        profile=profile,
        authorization=authorization,
    )


def sync_guardian_identity(
    application, draft, *, request, using=None
) -> IdentitySyncResult:
    """Resolve identity for one guardian slot and link or create a profile.

    Same contract as :func:`sync_student_identity`, for a primary or
    secondary guardian draft. Does not create or change any
    ``AdultStudentRelationship``; see :func:`sync_guardian_relationship_proposal`
    for the (always lead-reviewed) relationship proposal.
    """
    using = _using(application, using)
    slot = _GUARDIAN_SLOT_TO_SUBJECT_SLOT[draft.slot]
    email = _candidate_email_for_guardian(application, draft)
    resolution = resolve_application_person_identity(email, application.organization)

    # See the matching comment in sync_student_identity(): link/edit the
    # profile before creating the proposal so this call's own write doesn't
    # immediately make the proposal it just created look stale.
    profile = None
    authorization = None
    if resolution.status == "no_match":
        try:
            profile = _create_incomplete_guardian(application, draft, using=using)
        except ApplicationIdentitySyncConflict:
            resolution = resolve_application_person_identity(
                _candidate_email_for_guardian(application, draft),
                application.organization,
            )
        else:
            _set_slot_profile(application, slot=slot, profile=profile, using=using)
    elif resolution.status == "unique" and isinstance(resolution.profile, Adult):
        authorization = evaluate_application_profile_edit(
            request, application, resolution.profile
        )
        if authorization in _AUTHORIZED_EDIT_STATES:
            try:
                profile = _apply_direct_edit(
                    application,
                    resolution.profile,
                    draft,
                    GUARDIAN_DRAFT_PROFILE_FIELD_MAP,
                    request=request,
                    using=using,
                )
            except ApplicationIdentitySyncConflict:
                profile = None
            else:
                _set_slot_profile(application, slot=slot, profile=profile, using=using)

    _reset_identity_proposal(application, slot=slot, using=using)
    proposal = _create_identity_proposal(
        application, slot=slot, resolution=resolution, using=using
    )

    return IdentitySyncResult(
        slot=slot,
        status=resolution.status,
        proposal=proposal,
        profile=profile,
        authorization=authorization,
    )


def touch_identity_proposal_baselines(application, *, using=None) -> None:
    """Re-sync every proposal's updated_at baseline to the application's current value.

    Call this after a wizard step saves ``application`` purely for routine
    progress tracking (current_step/applicant_progress_at/status), so an
    earlier step's proposal is not treated as stale merely because the
    application row was touched again by a *later*, unrelated step or slot.
    The more meaningful staleness signals a proposal also tracks - the linked
    profile identity and its revision counter - are untouched by this and
    still catch an actual conflicting change.
    """
    using = _using(application, using)
    current_updated_at = (
        Application.objects.using(using)
        .filter(pk=application.pk, organization_id=application.organization_id)
        .values_list("updated_at", flat=True)
        .first()
    )
    if current_updated_at is None:
        return
    ApplicationIdentityProposal.objects.using(using).filter(
        application_id=application.pk, organization_id=application.organization_id
    ).update(baseline_application_updated_at=current_updated_at)
    ApplicationRelationshipProposal.objects.using(using).filter(
        application_id=application.pk, organization_id=application.organization_id
    ).update(baseline_application_updated_at=current_updated_at)


def delete_orphaned_incomplete_profiles(application, profile_ids, *, using=None) -> int:
    """Delete isolated incomplete profiles left behind by a deleted application.

    ``Application.student_profile``/``primary_guardian_profile``/
    ``secondary_guardian_profile`` use ``on_delete=SET_NULL`` so the profile
    itself survives when only the *application* row is removed (expiration,
    manual withdrawal, or staff deletion). An incomplete profile is never
    shown anywhere ordinary, but leaving it behind forever would permanently
    occupy its email and force every future application for that same person
    into lead review, with no application left to review.

    Call this *after* deleting ``application`` (so it no longer blocks the
    check below) and pass the profile ids captured *before* that delete
    (``(Student, pk)`` / ``(Adult, pk)`` tuples; ``None`` entries are
    skipped). Only deletes a profile that is still incomplete, in the same
    organization, and not referenced by any other application, so a profile
    shared by a still-open duplicate application is left untouched. Returns
    the number of profiles deleted.
    """
    using = using or "default"
    deleted = 0
    for model, pk in profile_ids:
        if not pk:
            continue
        if model is Student:
            still_referenced = Application.objects.using(using).filter(
                student_profile_id=pk
            )
        else:
            still_referenced = Application.objects.using(using).filter(
                Q(primary_guardian_profile_id=pk) | Q(secondary_guardian_profile_id=pk)
            )
        if still_referenced.exists():
            continue
        deleted += (
            model.all_objects.using(using)
            .filter(
                pk=pk,
                organization_id=application.organization_id,
                is_incomplete_application_profile=True,
            )
            .delete()[0]
        )
    return deleted


def sync_guardian_relationship_proposal(
    application, *, student_draft, guardian_draft, using=None
) -> ApplicationRelationshipProposal | None:
    """(Re)create the pending "add guardian" proposal for one guardian slot.

    Always replaces any prior proposal for this exact guardian draft, so a
    resubmitted step cannot leave a stale decision behind. Returns ``None``
    (and removes any existing proposal) when the guardian slot is not
    present, since there is nothing to propose for a skipped/unanswered
    contact. Never creates or changes a live ``AdultStudentRelationship``:
    that only happens at final lead approval.
    """
    using = _using(application, using)
    ApplicationRelationshipProposal.objects.using(using).filter(
        application_id=application.pk,
        organization_id=application.organization_id,
        guardian_draft_id=guardian_draft.pk,
    ).delete()
    if guardian_draft.presence != ApplicationGuardianDraft.Presence.PRESENT:
        return None
    proposal = ApplicationRelationshipProposal(
        organization=application.organization,
        application=application,
        action=ApplicationRelationshipProposal.Action.ADD,
        student_draft=student_draft,
        guardian_draft=guardian_draft,
        relationship_to_student=guardian_draft.relationship_to_student or "",
    )
    proposal.save(using=using)
    return proposal
