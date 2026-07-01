from django.db import transaction
from django.utils import timezone

from .tasks import send_organization_approved_email_task, send_organization_denied_email_task


@transaction.atomic
def approve_organization(organization, reviewed_by):
    if organization.status != 'pending':
        raise ValueError('Only pending organizations can be approved.')

    organization.status = 'active'
    organization.reviewed_by = reviewed_by
    organization.reviewed_at = timezone.now()
    organization.save()

    transaction.on_commit(
        lambda: send_organization_approved_email_task.delay(organization.id)
    )


@transaction.atomic
def deny_organization(organization, reviewed_by):
    if organization.status != 'pending':
        raise ValueError('Only pending organizations can be denied.')

    owner_email = organization.owner.email
    organization_name = organization.name
    organization.delete()

    transaction.on_commit(
        lambda: send_organization_denied_email_task.delay(owner_email, organization_name)
    )
