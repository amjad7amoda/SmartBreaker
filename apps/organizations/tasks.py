from celery import shared_task

from config.emails import send_branded_email

from .models import Organization


@shared_task
def send_organization_approved_email_task(org_id):
    org = Organization.objects.get(id=org_id)
    send_branded_email(
        subject='Organization Approved | Fluxa',
        recipient=org.owner.email,
        preheader=f'تم اعتماد مؤسستك "{org.name}" وأصبحت نشطة الآن.',
        status='active',
        heading_ar='Organzation Approved',
        paragraphs_ar=[
            f'يسعدنا إبلاغك بأنه تمت الموافقة على مؤسستك "{org.name}".',
            'أصبحت المؤسسة الآن نشطة، ويمكنك البدء بإدارة القواطع والأحمال الكهربائية ضمنها.',
        ],
    )


@shared_task
def send_organization_denied_email_task(owner_email, organization_name):
    send_branded_email(
        subject='Organization Denied | Fluxa',
        recipient=owner_email,
        preheader=f'لم تتم الموافقة على طلب إنشاء المؤسسة "{organization_name}".',
        status='denied',
        heading_ar='Organization Denied',
        paragraphs_ar=[
            f'تمت مراجعة طلبك لإنشاء المؤسسة "{organization_name}" ولم تتم الموافقة عليه.',
            'يمكنك تعديل التفاصيل وإعادة تقديم الطلب، أو التواصل مع المسؤول لمزيد من المعلومات.',
        ],
    )
