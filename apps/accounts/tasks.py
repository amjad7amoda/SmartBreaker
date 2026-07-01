from celery import shared_task

from config.emails import send_branded_email

from .models import User


@shared_task
def send_otp_email_task(user_id, otp_plain, otp_valid_minutes):
    user = User.objects.get(id=user_id)
    send_branded_email(
        subject='Email Approaved | Fluxa',
        recipient=user.email,
        preheader='تمت الموافقة على حسابك، استخدم رمز الدخول لمرة واحدة لتسجيل الدخول.',
        status='approved',
        heading_ar= 'Email OTP',
        paragraphs_ar=[
            'تمت الموافقة على طلب إنشاء حسابك من قبل المسؤول.',
            f'استخدم رمز الدخول لمرة واحدة أدناه لتسجيل الدخول. الرمز صالح لمدة {otp_valid_minutes} دقيقة ويُستخدم مرة واحدة فقط.',
            'بعد تسجيل الدخول سيُطلب منك تعيين كلمة مرور دائمة لحسابك.',
        ],
        highlight={
            'value': otp_plain,
            'caption_ar': 'رمز الدخول لمرة واحدة',
        },
    )


@shared_task
def send_denial_email_task(email):
    send_branded_email(
        subject='Email Denied | Fluxa',
        recipient=email,
        preheader='نأسف، لم تتم الموافقة على طلب إنشاء حسابك.',
        status='denied',
        heading_ar='Email Denied',
        paragraphs_ar=[
            'نشكر اهتمامك بمنصة القاطع الذكي.',
            'تمت مراجعة طلب إنشاء حسابك ولم تتم الموافقة عليه في الوقت الحالي.',
            'إذا كنت تعتقد أن هذا عن طريق الخطأ، يمكنك التواصل مع المسؤول أو إعادة تقديم الطلب.',
        ],
    )


@shared_task
def send_password_reset_email_task(user_id, code_plain, code_valid_minutes):
    user = User.objects.get(id=user_id)
    send_branded_email(
        subject='Password Reset | Fluxa',
        recipient=user.email,
        preheader='Request to reset your password.',
        heading_ar='Reset Password',
        paragraphs_ar=[
            'تلقّينا طلباً لإعادة تعيين كلمة المرور الخاصة بحسابك.',
            f'استخدم الرمز أدناه لإكمال العملية. الرمز صالح لمدة {code_valid_minutes} دقيقة ويُستخدم مرة واحدة فقط.',
            'إذا لم تطلب ذلك، يمكنك تجاهل هذه الرسالة بأمان ولن يتغير شيء.',
        ],
        highlight={
            'value': code_plain,
            'caption_ar': 'رمز إعادة التعيين',
        },
    )
