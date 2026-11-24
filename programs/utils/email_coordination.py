"""Database-backed coordination for concurrent profile-email writers.

Student and Adult profiles are created and edited from many places: the
public application wizard, staff forms, CSV imports, the alumni conversion
helper, and the student/guardian merge tools. None of those writers
previously shared any locking, so two concurrent requests claiming the same
(organization, email) pair could both pass their own conflict check before
either had written its row.

``claim_profile_emails`` gives writers a common, tenant-scoped serialization
point: a caller takes the lock, repeats its conflict check, and only then
creates or updates the profile, all inside the same transaction. This is a
coordination lock only. It does not impose a portal-wide email uniqueness
constraint, does not affect any email that isn't actively being claimed, and
does not change or clean up historical duplicate records.

On SQLite, ``select_for_update`` does not take a real row lock, so only
PostgreSQL gives true cross-request serialization; SQLite callers still get
correct sequential behaviour, which is enough for single-process tests, but
not a substitute for PostgreSQL concurrency testing.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db import transaction

from programs.models.email_claims import ProfileEmailClaim


def normalize_claim_email(email: str | None) -> str:
    """Normalize an email the same way for every claimant: trim + casefold."""
    return (email or "").strip().casefold()


def claim_profile_instance_emails(instance, *, using=None):
    """Claim the coordination lock for one ``Student``/``Adult`` instance's emails.

    Convenience wrapper around :func:`claim_profile_emails` for a writer that
    is about to create or save a single profile instance. Must be called
    inside the same ``transaction.atomic()`` block as the write, before it.
    A no-op when the instance has no organization yet (nothing to scope the
    lock to).
    """
    organization = getattr(instance, "organization", None)
    if organization is None:
        return []
    return claim_profile_emails(
        organization,
        [
            getattr(instance, "personal_email", None),
            getattr(instance, "org_email", None),
        ],
        using=using,
    )


def claim_profile_emails(organization, emails: Iterable[str | None], *, using=None):
    """Lock the claim rows for ``emails`` in ``organization`` and return them.

    Must be called inside an existing ``transaction.atomic()`` block that also
    performs the conflict check and the write; the lock is held until that
    transaction commits or rolls back, so a concurrent claim for the same
    (organization, normalized email) blocks until this one finishes. Blank
    emails are ignored and no lock is taken when every email is blank.
    """
    using = using or "default"
    connection = transaction.get_connection(using)
    if not connection.in_atomic_block:
        raise RuntimeError(
            "claim_profile_emails() must be called inside transaction.atomic()."
        )
    normalized = sorted({normalize_claim_email(email) for email in emails} - {""})
    if not normalized:
        return []
    for email in normalized:
        ProfileEmailClaim.objects.using(using).get_or_create(
            organization=organization, normalized_email=email
        )
    return list(
        ProfileEmailClaim.objects.using(using)
        .select_for_update()
        .filter(organization=organization, normalized_email__in=normalized)
        .order_by("normalized_email")
    )
