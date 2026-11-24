from django.db import models


class ProfileEmailClaim(models.Model):
    """Serialization point for one normalized, tenant-scoped profile email.

    Student and Adult profiles are created and edited from many places: the
    public application wizard, staff forms, CSV imports, the alumni
    conversion helper, and the student/guardian merge tools. Historically
    none of those writers shared any locking, so two concurrent requests
    claiming the same (organization, email) pair could both pass their own
    uniqueness check before either had written its row.

    A writer that is about to create a new profile or change an existing
    profile's email should lock this row (via ``select_for_update``, see
    ``programs.utils.email_coordination.claim_profile_emails``) before
    repeating its conflict check and writing, so a concurrent writer for the
    same organization/email cannot pass the same check at the same time.

    This is a coordination lock only. It does not impose a portal-wide email
    uniqueness constraint, does not affect any email that isn't actively
    being claimed, and does not change or clean up historical duplicate
    records.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="profile_email_claims",
    )
    normalized_email = models.CharField(max_length=254, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "normalized_email"),
                name="unique_profile_email_claim",
            )
        ]

    def __str__(self):  # pragma: no cover - trivial
        return (
            f"Email claim lock for {self.normalized_email} (org {self.organization_id})"
        )
