from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import RegistrationRequest, User


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class RegistrationApprovalFlowTests(TestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser(email='admin@example.com', password='AdminPass123!')
        login = self.client.post(reverse('login'), {'email': 'admin@example.com', 'password': 'AdminPass123!'})
        self.admin_auth = {'HTTP_AUTHORIZATION': f"Bearer {login.json()['access']}"}

    def test_request_creation_rejects_duplicate_email(self):
        User.objects.create_user(email='existing@example.com', role='home_user')
        resp = self.client.post(reverse('registration-request-list-create'), {
            'email': 'existing@example.com', 'phone': '1', 'role': 'home_user',
        })
        self.assertEqual(resp.status_code, 400)

    def test_request_creation_rejects_duplicate_pending(self):
        self.client.post(reverse('registration-request-list-create'), {
            'email': 'dup@example.com', 'phone': '1', 'role': 'home_user',
        })
        resp = self.client.post(reverse('registration-request-list-create'), {
            'email': 'dup@example.com', 'phone': '1', 'role': 'home_user',
        })
        self.assertEqual(resp.status_code, 400)

    def test_non_admin_cannot_approve(self):
        req = RegistrationRequest.objects.create(email='a@example.com', role='home_user')
        user = User.objects.create_user(email='plain@example.com', role='home_user', password='x')
        user.is_active = True
        user.must_set_password = False
        user.save()
        login = self.client.post(reverse('login'), {'email': 'plain@example.com', 'password': 'x'})
        self.assertEqual(login.status_code, 200)
        access = login.json()['access']
        resp = self.client.post(
            reverse('registration-request-approve', args=[req.id]), {}, HTTP_AUTHORIZATION=f'Bearer {access}'
        )
        self.assertEqual(resp.status_code, 403)

    def test_full_approve_otp_login_set_password_flow(self):
        self.client.post(reverse('registration-request-list-create'), {
            'email': 'newuser@example.com', 'phone': '123', 'role': 'home_user',
        })
        req = RegistrationRequest.objects.get(email='newuser@example.com')

        with self.captureOnCommitCallbacks(execute=True):
            approve_resp = self.client.post(
                reverse('registration-request-approve', args=[req.id]), {}, **self.admin_auth
            )
        self.assertEqual(approve_resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

        otp = mail.outbox[0].body.split('log in:')[1].splitlines()[0].strip()

        early_login = self.client.post(reverse('login'), {'email': 'newuser@example.com', 'password': 'whatever'})
        self.assertEqual(early_login.status_code, 401)

        otp_login = self.client.post(reverse('otp-login'), {'email': 'newuser@example.com', 'otp': otp})
        self.assertEqual(otp_login.status_code, 200)
        self.assertTrue(otp_login.json()['must_set_password'])
        access = otp_login.json()['access']

        set_pw = self.client.post(
            reverse('set-password'),
            {'new_password': 'NewStrongPass123!', 'new_password_confirm': 'NewStrongPass123!'},
            HTTP_AUTHORIZATION=f'Bearer {access}',
        )
        self.assertEqual(set_pw.status_code, 204)

        final_login = self.client.post(
            reverse('login'), {'email': 'newuser@example.com', 'password': 'NewStrongPass123!'}
        )
        self.assertEqual(final_login.status_code, 200)

        reuse_otp = self.client.post(reverse('otp-login'), {'email': 'newuser@example.com', 'otp': otp})
        self.assertEqual(reuse_otp.status_code, 400)

    def test_resend_otp_lets_stuck_user_recover_and_invalidates_old_otp(self):
        self.client.post(reverse('registration-request-list-create'), {
            'email': 'stuck@example.com', 'phone': '123', 'role': 'home_user',
        })
        req = RegistrationRequest.objects.get(email='stuck@example.com')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('registration-request-approve', args=[req.id]), {}, **self.admin_auth)
        old_otp = mail.outbox[0].body.split('log in:')[1].splitlines()[0].strip()

        from django.utils import timezone
        stuck_user = User.objects.get(email='stuck@example.com')
        stuck_user.otp_last_sent_at = timezone.now() - timezone.timedelta(minutes=5)
        stuck_user.save()

        with self.captureOnCommitCallbacks(execute=True):
            resend_resp = self.client.post(reverse('resend-otp'), {'email': 'stuck@example.com'})
        self.assertEqual(resend_resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        new_otp = mail.outbox[1].body.split('log in:')[1].splitlines()[0].strip()

        old_otp_login = self.client.post(reverse('otp-login'), {'email': 'stuck@example.com', 'otp': old_otp})
        self.assertEqual(old_otp_login.status_code, 400)

        new_otp_login = self.client.post(reverse('otp-login'), {'email': 'stuck@example.com', 'otp': new_otp})
        self.assertEqual(new_otp_login.status_code, 200)

    def test_resend_otp_cooldown_blocks_rapid_repeat_requests(self):
        self.client.post(reverse('registration-request-list-create'), {
            'email': 'rapid@example.com', 'phone': '123', 'role': 'home_user',
        })
        req = RegistrationRequest.objects.get(email='rapid@example.com')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('registration-request-approve', args=[req.id]), {}, **self.admin_auth)
        self.assertEqual(len(mail.outbox), 1)

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse('resend-otp'), {'email': 'rapid@example.com'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)  # still 1: cooldown blocked the resend

    def test_resend_otp_is_noop_and_silent_for_unknown_or_already_activated_email(self):
        resp = self.client.post(reverse('resend-otp'), {'email': 'doesnotexist@example.com'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

        active_user = User.objects.create_user(email='already@example.com', role='home_user', password='x')
        active_user.is_active = True
        active_user.must_set_password = False
        active_user.save()
        resp = self.client.post(reverse('resend-otp'), {'email': 'already@example.com'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_deny_flow_sends_email_and_blocks_login(self):
        self.client.post(reverse('registration-request-list-create'), {
            'email': 'denyme@example.com', 'phone': '1', 'role': 'technician',
        })
        req = RegistrationRequest.objects.get(email='denyme@example.com')
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse('registration-request-deny', args=[req.id]), {}, **self.admin_auth)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(RegistrationRequest.objects.get(id=req.id).status, 'denied')
        self.assertFalse(User.objects.filter(email='denyme@example.com').exists())
        self.assertEqual(len(mail.outbox), 1)

    def test_admin_can_list_and_filter_requests(self):
        self.client.post(reverse('registration-request-list-create'), {
            'email': 'pending1@example.com', 'phone': '1', 'role': 'home_user',
        })
        self.client.post(reverse('registration-request-list-create'), {
            'email': 'pending2@example.com', 'phone': '1', 'role': 'technician',
        })
        approved_req_id = RegistrationRequest.objects.get(email='pending1@example.com').id
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('registration-request-approve', args=[approved_req_id]), {}, **self.admin_auth)

        resp = self.client.get(reverse('registration-request-list-create'), **self.admin_auth)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 2)

        pending_resp = self.client.get(
            reverse('registration-request-list-create'), {'status': 'pending'}, **self.admin_auth
        )
        emails = {item['email'] for item in pending_resp.json()}
        self.assertEqual(emails, {'pending2@example.com'})

        detail_resp = self.client.get(
            reverse('registration-request-detail', args=[approved_req_id]), **self.admin_auth
        )
        self.assertEqual(detail_resp.status_code, 200)
        self.assertEqual(detail_resp.json()['status'], 'approved')
        self.assertEqual(detail_resp.json()['reviewed_by'], 'admin@example.com')

    def test_non_admin_cannot_list_requests(self):
        user = User.objects.create_user(email='plain2@example.com', role='home_user', password='x')
        user.is_active = True
        user.must_set_password = False
        user.save()
        login = self.client.post(reverse('login'), {'email': 'plain2@example.com', 'password': 'x'})
        access = login.json()['access']
        resp = self.client.get(
            reverse('registration-request-list-create'), HTTP_AUTHORIZATION=f'Bearer {access}'
        )
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_cannot_list_requests(self):
        resp = self.client.get(reverse('registration-request-list-create'))
        self.assertEqual(resp.status_code, 401)


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class ForgotPasswordFlowTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(email='active@example.com', role='home_user', password='OldPass123!')
        self.user.is_active = True
        self.user.must_set_password = False
        self.user.save()

    def _request_code(self, email='active@example.com'):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('forgot-password'), {'email': email})

    def test_forgot_password_sends_code_and_reset_updates_password(self):
        resp = self._request_code()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        code = mail.outbox[0].body.split('reset your password:')[1].splitlines()[0].strip()

        confirm = self.client.post(reverse('reset-password'), {
            'email': 'active@example.com', 'code': code,
            'new_password': 'BrandNewPass123!', 'new_password_confirm': 'BrandNewPass123!',
        })
        self.assertEqual(confirm.status_code, 204)

        old_login = self.client.post(reverse('login'), {'email': 'active@example.com', 'password': 'OldPass123!'})
        self.assertEqual(old_login.status_code, 401)

        new_login = self.client.post(reverse('login'), {'email': 'active@example.com', 'password': 'BrandNewPass123!'})
        self.assertEqual(new_login.status_code, 200)

        reuse_code = self.client.post(reverse('reset-password'), {
            'email': 'active@example.com', 'code': code,
            'new_password': 'AnotherPass123!', 'new_password_confirm': 'AnotherPass123!',
        })
        self.assertEqual(reuse_code.status_code, 400)

    def test_reset_password_rejects_mismatched_confirmation(self):
        self._request_code()
        code = mail.outbox[0].body.split('reset your password:')[1].splitlines()[0].strip()
        resp = self.client.post(reverse('reset-password'), {
            'email': 'active@example.com', 'code': code,
            'new_password': 'BrandNewPass123!', 'new_password_confirm': 'Different123!',
        })
        self.assertEqual(resp.status_code, 400)

    def test_forgot_password_cooldown_blocks_rapid_repeat_requests(self):
        self._request_code()
        self.assertEqual(len(mail.outbox), 1)
        self._request_code()
        self.assertEqual(len(mail.outbox), 1)  # cooldown blocked the second send

    def test_forgot_password_is_noop_and_silent_for_unknown_or_incomplete_account(self):
        resp = self._request_code('doesnotexist@example.com')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

        stuck_user = User.objects.create_user(email='stuck2@example.com', role='home_user')
        resp = self._request_code('stuck2@example.com')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)


class AdminUserCreationTests(TestCase):
    def test_user_admin_has_no_add_permission(self):
        from django.contrib import admin as django_admin
        admin_instance = django_admin.site._registry[User]
        self.assertFalse(admin_instance.has_add_permission(request=None))
