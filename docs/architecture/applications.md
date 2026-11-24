# Public Applications Wizard & Staff Review Workflow

This document details the public student application wizard (`applications/`), multi-step state management, step-key normalization, and staff conversion to active student profiles.

## Public Application Wizard (`/apply/`)

The public application wizard replaces the legacy `StudentApplication` model.
- **Login-Exempt & Throttled**: Accessible publicly with OTP email verification. Rate limits are enforced via `ApplyRateLimitMiddleware` and `applications/rate_limiting.py`.
- **Resumable State**: Application answers are persisted in the `data` JSONField across wizard steps.

---

## Step Key Normalization Invariant

Legacy applications stored responses under older keys (`step5`, `step6`, `step7`, `step8`, `step6_handoff`). Modern applications use dashed keys:
- `step5-student`
- `step6-experience`
- `step7-primaryparent`
- `step8-secondaryparent`
- `step7_handoff`

### Normalization Rule
Never write code that reads or writes the legacy key names directly. Always call `normalize_step_keys()` (module-level helper in `applications/models.py`) or `Application.normalize_step_data(save=True)` before inspecting or converting application data.

Persisted normalization, the shared wizard step writer, applicant type/email,
program selection, parent handoff, and parent swapping reload and lock the
organization-bound application row before updating. These writers preserve
newer unrelated JSON and wizard progress rather than saving a stale instance.
Step 2 rechecks identity and status under the lock and rejects a request whose
applicant/email changed while the form was being submitted; changing the email
clears its verification and pending OTP. Program selection rechecks the
tenant's eligible programs and current status before updating the program,
progress, and program-bound custom-question snapshot together. When switching
programs, it archives the active snapshot by program ID and restores a saved
snapshot for the selected program; malformed, unbound, or ambiguous snapshots
are left unchanged and require staff assistance. Handoff also revalidates the
parent address and applicant status against the locked row before issuing a
token; the notification is sent after the transaction. Conversion extracts
historical answers from the same locked data before normalizing it.

Student steps 5–8 and mentor information/clearance writes also recheck the
locked row after form validation. They do not save when the application has
become terminal, browser or handoff authorization no longer applies, the
applicant type or program changed, or student-contact context used for parent
email validation has changed. Step 8 rechecks the primary/secondary contact
email-update opt-in against current data. Mentor clearance details are saved
only while current clearance interest remains affirmative, and clearance
interest edits reject a conflicting intervening change. Rejected forms are
rendered with their submitted values so applicants can review and resubmit.
Primary and secondary guardian forms reject addresses that match each other
after trimming surrounding whitespace and comparing case-insensitively. The
comparison is repeated against the locked application before saving, so an
intervening change cannot introduce a shared guardian address. Blank secondary
emails remain optional when email updates are not selected, and plus-tagged
addresses remain distinct. This is an application-form check, not a global
email uniqueness constraint; historical profile addresses are unchanged.
Step 7 applies the tenant-scoped unique-person resolver to direct guardian-email
prefill, welcome-back display, and swap-prompt discovery. If that email matches
multiple people in the organization, it does not select or reveal one of their
profiles, and previously saved application fields remain untouched. Relationship-
derived guardian details and student-address fallback now require the saved
student to be identified by the verified student email or by a uniquely matched
guardian with an existing same-organization relationship. When a parent swap
needs to hydrate a missing contact, the relationship must belong to that saved
student and organization. Invalid, ambiguous, or cross-organization references
fail closed without deleting application answers. This is only a prefill
safeguard: it does not implement incomplete live profiles, lead-review
decisions, conversion matching changes, or protection against concurrent
profile-email claims.

### Resume and restart

Starting over from a verified duplicate page locks the current application and
all eligible matching drafts in the same organization. It marks eligible older
drafts with `superseded_at` without changing their status, applicant progress
timestamp, legacy `updated_at` fallback, answers, document submissions, files,
or history. Superseded applications are omitted from active wizard and resume
routes, reviewer queues, applicant dashboards, duplicate discovery, and
reminders. They remain eligible for the existing 30-day expiration policy.
That command is not scheduled automatically.

A student email verification alone cannot supersede a student-started
application awaiting guardian handoff. That record remains active unless its
own guardian-specific browser grant and handoff authorization are present.
Starting over reports partial completion without identifying protected
applications. Resume keeps the new draft active until the selected existing
application is verified in its own browser session. A session-bound intent is
rechecked with both application rows locked in deterministic order; a guardian
handoff requires the guardian's OTP and token. Failed or stale verification
leaves both records and their documents unchanged. No browser grant or handoff
token is copied between applications. The marker is additive to the legacy
JSON workflow and does not replace conversion or profile matching.
The ordered row locks coordinate existing records but cannot prevent another
request from inserting a new same-email draft at the same time; there is no
global email uniqueness constraint, so a concurrent new draft may remain active.

Student/parent and mentor confirmation also reload and lock the organization-
bound application after form validation. They recheck terminal status and the
current browser verification; student/parent submission additionally rechecks
parent handoff authorization, program selection, and the current parent email-
updates opt-in. The status transition is saved once, and applicant/lead notices
are sent only after the transaction, so stale or repeated confirmation posts
do not reset submission time or resend notifications.

Application JSON writers outside these coordinated paths may not all use the
same locking protocol, so this is not a complete concurrency guarantee; SQLite
does not provide row-level `select_for_update` locking. Step 6 freezes the
program's active question set and saves it with experience answers under the
application lock. A signed version rejects competing Step 6 answer edits, and
the locked confirmation path checks required answers again so direct submission
cannot bypass them.

---

## Staff Review & Student Conversion

Staff manage applications via `/applications/review/`:
- **Workflow States**: `draft` → `submitted` → `under_review` → `accepted` / `waitlisted` / `rejected`.
- **Required forms before acceptance**: Active required student/parent program documents must have tenant-owned uploaded submissions before the reviewer can accept an application. The locked decision rechecks current uploads; an applicant or staff upload does not itself accept a submitted application. New approvals with required documents use `APPROVED_SIGNED`; existing approved applications remain accessible so families and staff can finish their uploads without changing the prior decision.
- **Edit conflict protection**: Staff edit forms carry a signed version of the tenant-bound application state. The locked POST rejects missing, forged, or stale versions before saving and redisplays submitted values with a reload notice. GET first persists legacy step-key normalization and then signs the refreshed row so its timestamp matches the normalized data.
- **Conversion Workflow (`convert_application_to_student`)**:
  - Automatically provisions or links the `Student` profile.
  - Provisions or links primary and secondary parent `Adult` profiles.
  - Creates the program `Enrollment` record.
  - Links application documents (`ApplicationDocumentSubmission`) into `StudentDocument` records.
  - Logs `AuditEvent.APPLICATION_CONVERTED`.

### Historical student application answers

`StudentApplicationAnswer` stores the question text and response for each
application and linked student. Lead Mentors and platform superusers can see
these answers on the student profile under the same tenant restrictions as
application review. Ordinary student-profile access does not grant application
review access. Program information and the application ID identify the source;
different applications never overwrite one another.
The card is inside the collapsed “View Contact & Personal Details” section and
shows an empty state when no linked answers are visible to the reviewer.
It also shows nonblank responses already stored in the four fixed student
profile fields, separately because their source application is not recorded.

The backfill copies the four existing experience questions from approved,
approved-and-signed, and converted student/parent applications only when
`converted_student` is present and the student, application, and any program
belong to the same organization. It does not infer a student from an email or
an unconfirmed picker selection. Both old and current experience-step keys are
examined separately; identical answers are de-duplicated, while conflicting
answers are retained with their source step. Empty answers are not copied.
Original application data and existing fixed Student fields remain unchanged.
New conversions also preserve these answers before step-key normalization.
For historical answers, question text is reconstructed from the current fixed
wizard labels; earlier wording was not stored in the application data.

Malformed answers, missing student links, and inconsistent ownership require
manual review; see `report_application_answer_backfill` in `docs/operations.md`.
Unlinked approved applications are expected until conversion. The backfill
cannot recover data already lost before it ran. Answer rows follow the source
application's and student's deletion lifecycle, and student merges transfer
them only after validating their ownership. Conversion stops with an error if
an experience answer is malformed rather than discarding it during step-key
normalization.

Lead Mentors can configure organization-scoped, free-text questions for a
program, including active/inactive state, required/optional status, and display
order. When an applicant reaches Step 6, the application freezes the active
question set—including exact wording and required status—against its selected
program. Later configuration changes do not rewrite that application's frozen
questions. Switching programs archives the outgoing snapshot and restores a
saved snapshot for the target program; malformed, unbound, or ambiguous state
is retained for staff assistance rather than guessed or discarded.

Applicants answer the frozen questions alongside the existing experience
questions. Required answers are checked by the Step 6 form and again against the
freshly locked application during confirmation; direct submission cannot skip
an unfrozen set or submit an unanswered required question. The confirmation
page shows the saved wording and answers. Step 6 reads legacy fixed experience
answers through the existing key normalizer without rewriting legacy data just
because the page was viewed. New conversions preserve nonblank custom answers
with the exact wording shown, alongside the legacy experience answers.

Staff review renders custom snapshots and flags malformed data. For legacy
applications, the existing JSON editor remains available. The typed answer editor
is available to Lead Mentors only while a profile-workflow application is
submitted and awaiting review; it edits fixed and active-program answers while
preserving frozen wording and archived answers. Required answers remain required,
and stale edits or edits after acceptance are rejected. Answer edits use the
existing application edit audit event without recording answer values. This
editor changes application answers only; it does not edit student or guardian
profiles, and converted applications remain read-only.

### Typed application draft foundation (inactive)

Schema-only migrations `0024` and `0025` add application-owned student drafts,
guardian contact drafts, frozen question sets, and fixed/custom answer rows.
`Application` remains the progress tracker. The optional one-to-one student
draft includes typed Step 5 fields and encrypted medical text; guardian rows are
keyed by primary/secondary slot and distinguish unanswered, present, and
explicitly skipped contacts. Race/ethnicity selections are stored through a
tenant-owned join model. These records do not create or link live student/adult
profiles, relationships, enrollments, user accounts, or portal access.

Schema-only migrations `applications.0026` and `programs.0143` add optional
tenant-validated profile links to `Application` and an explicit incomplete
lifecycle flag to `Student` and `Adult`. Existing profiles retain the default
active state; the links remain unset for existing applications. Incomplete
profiles are excluded by the ordinary Student, Adult, Enrollment, and
AdultStudentRelationship managers, active-student helpers, and the dashboard's
relationship-derived access paths.
Primary/secondary contact properties suppress incomplete endpoints, and model
save paths reject account links, enrollments, and live guardian relationships
for incomplete profiles. Database constraints also prohibit incomplete
profiles from carrying linked user accounts. The unrestricted `all_objects`
managers are for scoped internal lifecycle and invariant checks, not ordinary
views or forms.

This isolation foundation is not connected to the public wizard: new applicant
profile creation and direct updates remain disabled. The legacy application
workflow continues to use `Application.data`; `profile_workflow_version` stays
at its legacy default until the complete identity, review, conversion, and
compatibility flow is ready. Authorized application reviewers retain access to
their tenant-scoped application review pages; ordinary profile detail routes do
not expose incomplete profiles.

Schema-only migrations `applications.0027`–`0034` add tenant-owned identity and
relationship proposal records, while `programs.0144`–`0146` add revision
counters and database defaults for the profile lifecycle fields. Identity
resolution distinguishes no match, one active same-organization match, and
ambiguity across both Student and Adult personal
and organization email addresses; an ambiguous result carries no candidate.
Proposals store typed candidate/target references, action, baseline and reviewer
decision metadata. Live profile, application-draft and relationship revisions
detect partial saves as well as ordinary updates, and application progress is
checked again before a proposal baseline is considered current. `pghistory`
records proposal decision history without copying applicant contact or profile
values into proposal events.

The relationship action model supports additions, removals, replacements, and
primary-contact changes. Authorized reviewers can record identity and
relationship decisions for `profile_workflow_version=1`; those decisions do not
change live access until final approval. That approval rechecks required forms,
proposal decisions, current baselines, profile ownership, email conflicts, and
relationship endpoints under tenant-bound locks, then activates linked
incomplete profiles, applies only approved relationship changes, enrolls the
student, and preserves structured answers in one transaction. Profile-workflow
applications cannot use the legacy conversion endpoint.

For profile-workflow approval, validated typed student and guardian draft
fields are copied only to linked incomplete profiles in the finalization
transaction. Null draft values do not erase profile values, while explicit false
preferences are retained. The finalizer also validates tenant ownership of
race/ethnicity selections, resolves a submitted school name within the
application organization, and rejects email conflicts involving an incomplete
profile. Typed draft values never overwrite confirmed existing profiles. This
email check does not serialize writers in unrelated profile workflows, so it is
not a portal-wide uniqueness guarantee and does not unblock the public wizard.

Before finalization, the approval transaction locks fixed answers, frozen
question sets, their question rows, and referenced programs, then validates their
tenant ownership and program binding. The selected program must have exactly one
frozen set; a frozen set with no questions is valid, while missing or malformed
question wording and unanswered required questions block acceptance. These
checks run before any profile, relationship, enrollment, or answer-copy writes.
The same approval transaction then copies each validated signed submission to
the linked student's protected document records; the original application
submission remains available. A missing or unreadable source, inconsistent
tenant/program ownership, or failed copy aborts database finalization and
prevents the acceptance notice. File storage is not transactional with the
database, so a failed copy can leave an orphaned destination file for staff to
reconcile; it does not remove or replace the original submission. Legacy
conversion uses the same validated transfer path.

The read-only `evaluate_application_profile_edit` helper encodes the approved
exact-linked-account, unique verified-email, and existing linked-guardian rules;
edits to another guardian, ambiguous identities, or an unrelated student
require lead review. It now authorizes the immediate profile edits described
under "Verified wizard connected to isolated profiles and proposals" below.
The public wizard still creates legacy version-0 applications by default, and
conversion for those applications continues through its existing
email-matching path; a version-1 application can be created directly (as the
new end-to-end tests do) and finalized without any JSON or email-matching
conversion step. The JSON editor is unavailable for
profile-workflow applications. Lead reviewers can use the typed draft editor
while a version-1 application awaits review; it reuses student and guardian
form validation, scopes school and race choices to the application organization,
and checks a signed state version covering both draft rows and review proposals.
Changed values update only application-owned drafts, advance their revisions,
reset dependent identity and relationship decisions, and emit an audit event
containing revision metadata rather than profile or medical values. It does not
edit live profiles or relationships, and it does not connect the public wizard
to the structured workflow.

A question-set row is keyed by application and program; the row itself means
the set was frozen, including when it has zero answer rows. Its ordered answers
copy the stable question UUID, displayed wording, required flag, and nullable
answer without requiring the live program-question record to remain. Separate
sets can retain snapshots for earlier programs. Fixed experience answers use
source-step and question-key identity, so legacy and current responses can
coexist without overwriting one another. Model save paths validate that related
application, program, and answer records share an organization; database
uniqueness constraints enforce row cardinality, not cross-table tenant ownership
or integrity for bulk operations that bypass model saves.

Step 6 is connected to typed question and fixed-answer records for version-1
profile-workflow applications: the question set is frozen before display,
answers are written under the locked application row with a signed state
version, and confirmation reads and revalidates the typed answers. Version-0
applications continue to use their existing JSON path.

### Verified wizard connected to isolated profiles and proposals (Delivery Step 4)

`applications/identity_sync.py` connects Steps 5, 7, and 8 to typed drafts,
isolated profiles, and identity/relationship proposals for
`profile_workflow_version=1` applications, under the same `transaction.atomic`
plus `select_for_update` lock on the application row that every other step
writer already uses. Version-0 applications are unaffected; these steps keep
writing `Application.data` exactly as before. On each version-1 step submit:

- The typed `ApplicationStudentDraft`/`ApplicationGuardianDraft` row (and, for
  Step 5, its race/ethnicity links) is created or updated from the validated
  form data, replacing the equivalent JSON write.
- `resolve_application_person_identity` is re-evaluated and the slot's
  `ApplicationIdentityProposal` is replaced: a genuinely new person gets a
  fresh, minimal incomplete profile (immediately linked via
  `Application.student_profile`/`primary_guardian_profile`/
  `secondary_guardian_profile`); a uniquely matched existing profile is
  recorded as a candidate without being linked; an ambiguous match records no
  candidate. Replacing the proposal on every submit keeps its baseline and any
  prior decision in sync with the just-saved step, mirroring how editing a
  typed draft from the review screen already resets dependent decisions.
- `evaluate_application_profile_edit` decides whether the just-resolved unique
  match may be edited immediately: an exact same-organization linked account,
  a uniquely OTP-verified own email, or an existing authorized same-organization
  guardian-student relationship. Only those cases write the submitted fields
  onto the *existing* profile right away (locked, audited via
  `AuditEvent.CONTACT_INFO_UPDATED`, using the same draft-to-profile field maps
  `finalize_profile_workflow_application` uses for incomplete profiles). Every
  other case — ambiguous identity, an unrelated guardian, another guardian's
  email — leaves the profile unlinked for lead review and changes nothing.
  `sync_student_identity` also accepts an `existing_student` override so a
  parent's existing-student picker (not just an email match) can drive the same
  authorization path.
- For a present guardian draft, `sync_guardian_relationship_proposal` replaces
  the guardian's `ApplicationRelationshipProposal` (action `ADD`, decision
  `PENDING`). It never creates or edits a live `AdultStudentRelationship`:
  additions only take effect at final approval, after lead review.
- `touch_identity_proposal_baselines` re-stamps every proposal's
  `baseline_application_updated_at` after the step's own routine progress save
  (`current_step`/`applicant_progress_at`), so a later, unrelated step's save
  does not make an earlier step's already-correct proposal look stale. The more
  meaningful staleness checks — the linked profile's identity and its
  `profile_revision` — are untouched by this and still catch an actual
  conflicting edit.

New-profile creation and an authorized direct edit's email change both take the
tenant-scoped `programs.utils.email_coordination.claim_profile_emails` lock
(see below) before re-checking for a conflicting profile and writing, so a
concurrent wizard submission or an existing staff/import writer cannot create
or claim the same organization/email at the same time; losing that race leaves
the slot unresolved for lead review instead of creating a duplicate.

Prefill for these three steps now reads the typed draft when one exists
(falling back to the verified email for a brand-new Step 5 student applicant),
so revisiting a saved version-1 step shows the same values without touching
JSON. Step 9 confirmation's "at least one guardian opted in to email updates"
check, and the post-submission `SubmittedView` summary page, both branch on
`profile_workflow_version` and read the typed guardian/student drafts for
version-1 applications instead of the JSON step keys, which are never
populated for this workflow.

#### Profile-email write coordination (`programs.utils.email_coordination`)

`programs.models.ProfileEmailClaim` is a tenant-scoped `(organization,
normalized_email)` row used purely as a lock target. Its organization
foreign key uses `PROTECT`, consistent with every other organization-owned
model. `claim_profile_emails(organization, emails, using=None)` must be called
inside an existing `transaction.atomic()` block; it get-or-creates a claim row
per normalized (trimmed, case-folded) email and returns them locked via
`select_for_update()`, ordered by email to avoid deadlocking against another
caller claiming the same emails in a different order. It takes no lock for a
blank email. `claim_profile_instance_emails(instance, using=None)` is a
convenience wrapper that claims a single `Student`/`Adult` instance's
`personal_email` and `org_email` (a no-op if the instance has no organization
yet). This is a coordination primitive only: it does not add a portal-wide
email-uniqueness constraint, and a caller must still perform its own conflict
check (and write) while holding the lock.

Every profile-email writer identified in the original inventory now takes this
lock immediately before its write, inside its own existing transaction:
`identity_sync.py`'s new-profile creation and authorized-edit paths;
`programs/forms.py`'s `StudentForm.save()`/`AdultForm.save()` (the portal's
student/adult create and update views, including the admin-form subclass used
by Django admin for editing); all four CSV import writers in
`programs/views/imports.py` (student, parent, relationship, mentor row
import/matching); `programs/utils/alumni.py`'s student-to-alumni conversion;
and both merge paths in `programs/utils/merges.py`. No writer can create or
change a Student/Adult email for an organization while another writer
(including the wizard) holds the claim for that same normalized email.

One narrow gap remains: Django admin's `StudentAdmin`/`ParentAdmin` call
`form.save(commit=False)` and then a separate, unlocked `obj.save()` (Django's
default `ModelAdmin.save_form`/`save_model` behavior), so the claim taken
inside `StudentForm.save()`/`AdultForm.save()` is released before that second
`obj.save()` runs; admin edits are not actually covered by the lock. Closing
this properly means moving the claim into `Student.save()`/`Adult.save()`
themselves (next to their existing revision-bump lock), which is a
cross-cutting change to methods used by every Student/Adult write path
project-wide and was left for a separately approved pass rather than done
here. Django admin is superuser-only tooling, not the multi-org portal path
the wizard's identity check needs to be race-free against.

#### Maintenance and notification consumers migrated for version-1 applications

- `applications/reminders.py`'s missing-documents candidate filter previously
  matched a guardian recipient only through the JSON `step7_handoff`,
  `step7-primaryparent`, or legacy `step7` paths, so a version-1 student
  application with a linked primary guardian profile could never be selected
  as a reminder candidate. It now also matches when
  `profile_workflow_version=1` and the linked primary guardian profile (looked
  up the same way `applications.services.document_recipient` does) has a
  non-blank email.
- Deleting an application — through the applicant's own withdraw action, a
  reviewer's single delete, the reviewer's bulk 30-day cleanup, or the 30-day
  expiration command — never deletes its linked incomplete `Student`/`Adult`
  profiles (`Application.student_profile` and the two guardian links use
  `on_delete=SET_NULL`, so the profile row survives on its own). Left
  unaddressed, an abandoned incomplete profile would permanently occupy its
  email and push every future application from that same person into lead
  review forever, with no application left to review against. All four
  deletion paths now call `applications.identity_sync.delete_orphaned_incomplete_profiles`
  after the application row is gone, which deletes a captured profile only if
  it is still incomplete, in the same organization, and no longer referenced
  by any other application.
- `programs/utils/merges.py`'s student and parent merges now also reassign
  `Application.student_profile`/`primary_guardian_profile`/
  `secondary_guardian_profile` from the discarded record to the merge
  survivor (previously only `converted_student` was reassigned). Without
  this, merging away a profile-workflow applicant's draft or activated
  profile would silently `SET_NULL` the application's link to it.
- `anonymize_emails` previously only reached `Student.objects`/`Adult.objects`
  (which exclude incomplete profiles by design) and never touched
  `ApplicationStudentDraft.personal_email`/`ApplicationGuardianDraft.email`,
  so a profile-workflow applicant's or guardian's real email could survive a
  dev-data anonymization pass. It now also reaches `Student.all_objects`/
  `Adult.all_objects` and both typed draft models.
- `report_application_answer_backfill` assumed every approved/converted
  application has `converted_student` set and legacy `step6`/`step6-experience`
  JSON answers. Neither is ever true for a `profile_workflow_version=1`
  application (it finalizes onto `student_profile` instead and is never
  converted through the legacy path), so every approved version-1
  application was being misreported as "no linked student". It now skips
  version-1 applications and reports the skipped count instead; they are
  reviewed through the typed profile-workflow review screen, not this
  report.
- Checked and left unchanged: `move_application_documents`'s
  `converted_student__isnull=False, status=CONVERTED` filter already
  naturally excludes version-1 applications (which never set either field),
  so it has no false-positive risk. The portal/organization dashboards and
  digest emails do not reference applications at all.
  `reencrypt_student_medical`'s last-resort recovery fallback (reading a
  garbled medical field back out of legacy JSON) has no typed-draft
  equivalent for a profile-workflow-derived student profile; this is a rare
  disaster-recovery tool, not a routine report, so it was left as a known
  limitation rather than expanded.

#### Delivery Step 4 status: live by default

The public wizard (`WelcomeView.post`) now creates `profile_workflow_version=1`
applications by default. New applicants' isolated incomplete `Student`/`Adult`
profiles are populated with every answer as the applicant progresses through
the wizard — not just minimal identity fields held back until approval —
using the same `STUDENT_DRAFT_PROFILE_FIELD_MAP`/`GUARDIAN_DRAFT_PROFILE_FIELD_MAP`
field maps (including school lookup/creation and race/ethnicity selections)
that finalization re-applies at acceptance, so finalization's re-copy is a
no-op for fields already current. The complete applicant → guardian document
verification/upload → lead approval flow for a version-1 application is
covered end to end by `applications/tests/test_profile_workflow_end_to_end.py`,
including new families, idempotent re-approval, an ambiguous guardian identity
blocking approval until a lead resolves it, cross-organization reviewer
denial, and staff document upload. `applications/tests/test_integration_flows.py`
exercises the same default through the full public wizard URLs for student,
parent, and multi-step back-and-forward flows.

A previously identified stale-authorization race in `applications/identity_sync.py`
is fixed: `_apply_direct_edit` re-evaluates `evaluate_application_profile_edit`
against the just-locked profile before writing, instead of trusting the
caller's earlier unlocked check, so a concurrent change to the authorizing
fact (e.g. a guardian relationship removed between the check and the lock)
blocks the edit rather than applying it under stale authorization
(`test_identity_sync.py::StaleAuthorizationRecheckTests`).

`Student.clean()`/`Adult.clean()`'s email-uniqueness checks are now scoped to
the profile's own organization. Previously these checks queried across every
organization in the portal, so creating a student or adult in one organization
could be blocked by — or reveal the existence of — a same-email record in a
completely unrelated organization, a cross-tenant information leak in a
multi-organization portal. A shared email across two different organizations
is no longer treated as a conflict (`programs/tests/test_models/test_students.py::StudentEmailUniquenessOrganizationScopingTests`,
`test_adults.py::AdultEmailUniquenessOrganizationScopingTests`).

`programs/utils/email_coordination.py`'s claim lock has a genuine
multi-threaded regression (`test_email_coordination.py::ClaimProfileEmailsConcurrencyTests`)
that runs two concurrent writers on separate threads/connections against the
same (organization, email) pair. This environment cannot reach PostgreSQL
(port 5432 is not reachable here), so locally it only exercises the sequential
SQLite fallback; CI's `ci.yml` runs the suite against a real `postgres:16`
service, so this is the first test that can give genuine row-lock evidence
there. The project owner tests locally (including against real PostgreSQL)
before any deploy, per their own stated workflow, and has directed that the
live default be flipped now on that basis; this is noted here rather than
claimed as independently verified in this environment.

Before typed records can fully replace JSON for every consumer, each remaining
reader and writer must be migrated with the same application-row locking,
stale conflict detection, and audit coverage used here. Legacy approved
applications, protected documents, snapshot wording, fixed-answer conflicts,
and historical audit snapshots remain recoverable throughout; any backfill or
JSON removal needs a separately approved migration and recovery plan.

Conversion matches existing students and adults only within the application's
organization and explicitly assigns that organization to new profiles,
relationships, and enrollments. A selected foreign student, foreign program, or
inconsistent relationship, enrollment, or signed-document link stops conversion
without reassigning or deleting the existing records. `School` remains a shared
catalog and is not tenant-filtered.
If an application document is missing or cannot be copied, conversion fails and
the database changes are rolled back; the reviewer must resolve the document
before retrying. Storage writes are not transactional, so files copied before a
later conversion error may remain in storage.

### Application reminders

`send_application_reminders --organization <slug>` processes a bounded batch for
one active organization. Its `--dry-run` reports only an eligible-message count;
it does not create claims, update application timestamps, or send email. No
automatic scheduler is configured by this feature; operators invoke it on
their own recurring schedule (for example, operating-system `cron`). See
`docs/operations.md` for a documented, illustrative cadence.

New applications start with `applicant_progress_at`. Successful applicant wizard
writes, handoff, submission, and applicant document uploads advance that clock;
OTP activity, staff edits, and reminder delivery do not. Older applications
without the field use `updated_at` as the approximate inactivity clock, which
may reflect OTP or staff activity. Inactivity reminders become due after 48
hours. A deadline reminder is due from three date-days before `applications_close`
through the inclusive closing date, using `timezone.now().date()`; programs with
no close date have no deadline reminder, and applications for closed windows are
not reminded.

Draft student, guardian, and mentor applications are eligible only for a
verified applicant address or an authorized student-to-guardian handoff. A
handoff reminder uses the recorded guardian address and tokenized resume route;
it does not grant browser verification. Submitted student/guardian applications
are eligible only while required forms remain missing, and notices use
`document_recipient` and the protected documents route. Applications merely
awaiting staff review with complete forms and finalized applications are not
reminded. Simultaneous inactivity and deadline triggers are combined into one
message for that application phase. Separate tenant-scoped ledger rows dedupe
each trigger within the draft or missing-documents phase.

Delivery uses synchronous email history and organization tracking. A confirmed
pre-send failure may be retried; accepted delivery is recorded, while a transport
exception or interrupted pending claim is treated as uncertain and must not be
automatically resent. Operators should investigate the delivery history and
provider before resolving an uncertain outcome. This is not an exactly-once
delivery guarantee, and reminder links continue to rely on existing browser and
guardian document verification.

### Application expiration

`expire_applications --organization <slug> [--limit <count>]` checks a bounded
batch for one active organization. It is read-only by default; only the explicit
`--execute` option permanently deletes eligible applications and their owned
uploaded files. The default batch size is 200 and the maximum is 500. No
automatic scheduler is configured; see `docs/operations.md` for a documented,
illustrative cadence.

The 30-day clock starts at the last recorded applicant progress. Existing
applications without `applicant_progress_at` use `updated_at` as an
approximation, which may include OTP or staff activity. Eligible phases are
unfinished drafts, student applications awaiting parent handoff, and submitted
student/parent applications that still have at least one active required form
missing. Applications with complete forms that are waiting for staff review,
mentor submissions, and finalized/declined applications are excluded. The
policy applies to applications already in progress as well as new applications;
expired applicants must start over and cannot resume the deleted application.

Execution rechecks the tenant, progress, status, program, and required-document
state under an application-row lock. It removes only files under that exact
organization/application storage prefix, preserving the deletion audit event; an
inconsistent submission, unsafe path, unsupported storage listing, or failed
audit prevents deletion. Database and storage deletion are not atomic: a storage
failure can leave some files already removed while the application remains, so
operators should review failures before retrying. The existing reviewer stale
cleanup is a separate manual path with its own selection behavior and is not the
30-day timeout.

### Tenant isolation

Reviewer lists, filters, details, decisions, edits, conversions, uploads, and deletes require the resolved organization and only load applications owned by it. Review authorization requires a Lead Mentor in that organization or a platform superuser; a global Django reviewer permission does not override tenant roles. Applications linked to a program owned by another organization are treated as inconsistent and are not available in review. Program choices and application KPIs are tenant-scoped.

Stale cleanup only considers applications belonging to the current organization. Review pages read document submissions only when both the submission and its program are owned by that organization; individual deletes and cleanup leave applications with foreign-owned or mismatched document submissions untouched. Staff uploads explicitly assign the current organization and do not overwrite an inconsistently owned submission.

Public wizard routes require a resolved organization and load application IDs only within that organization. New applications are assigned to the request organization; program choices, duplicate-draft actions, and returning student/adult prefill are likewise organization-scoped. A missing tenant context fails closed. Lead notifications go only to active users with an active `LeadMentor` membership in the application organization; if there are no eligible email addresses, no lead notification is sent.

Step 5 student self-prefill and the guardian's existing-student picker require a
unique email match across both Student and Adult records in the application
organization, checking personal and organization email fields. If the address
matches multiple records, or matches both a Student and an Adult, the wizard
withholds the profile prefill or picker rather than selecting one. The student
match is rechecked against the locked application before saving Step 5. This is
limited to these wizard lookup paths: it does not enforce global email
uniqueness, change conversion matching, link profiles, or authorize profile
edits. The application-row lock does not serialize concurrent changes to the
matched profiles, so this is not a portal-wide identity guarantee.

Step 10 displays and accepts documents for submitted student/parent applications before approval, as well as approved legacy applications, only when the application, program, document, and submission ownership are consistent with the request organization. New submissions explicitly inherit the application's organization. Existing mismatched submissions cannot be exposed or overwritten, and duplicate/withdraw deletion is blocked when it would cascade through an inconsistent submission. Applicant downloads and uploads require a fresh, rate-limited email code verified in the same browser session; the grant expires with the OTP lifetime and is bound to the tenant, application, and document recipient. For a student-started application, the recipient is the handoff parent, or the primary parent's saved email for a legacy application without a handoff address. For a parent-started application, it is the application email. After submission, a separate required-document notice and protected link are sent only to that same parent/guardian; the student is never used as a fallback recipient. A missing parent address requires staff assistance, not a fallback to the student. The document challenge is separate from application email verification, and a recipient change invalidates both pending codes and access grants. Staff use a separate download route protected by application-review permission and tenant scoping; staff uploads recheck the application and program under a row lock. Completing uploads while the application is still submitted does not auto-accept it. Previously approved applications with missing required documents retain their approval and can complete uploads; completion preserves the existing `APPROVED_SIGNED` transition.

Approved and later application states are read-only through earlier public wizard steps. An application ID alone is not proof of applicant identity. Other in-progress wizard routes continue to follow the existing email-verification and parent-handoff workflow; this Step 10 challenge is an additional control for signed-document access.

Student photos and signed application uploads are stored under organization-scoped random paths beneath the existing `MEDIA_ROOT`. The path is not an authorization control. The `/media/` handler blocks both these private paths and legacy `photos/students/` and `application_documents/` paths in debug and production, without database access. Existing files remain in place and are streamed from their stored paths only through authorized endpoints; no automatic move or backfill is performed. Deployment proxies, CDNs, and storage origins must not serve these prefixes directly.
