from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User

from .models import Organization


def _activated_user(email, password='UserPass123!'):
    user = User.objects.create_user(email=email, role='home_user', password=password)
    user.is_active = True
    user.must_set_password = False
    user.save()
    return user


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
)
class OrganizationFlowTests(TestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser(email='admin@example.com', password='AdminPass123!')
        admin_login = self.client.post(reverse('login'), {'email': 'admin@example.com', 'password': 'AdminPass123!'})
        self.admin_auth = {'HTTP_AUTHORIZATION': f"Bearer {admin_login.json()['access']}"}

        self.user = _activated_user('owner@example.com')
        user_login = self.client.post(reverse('login'), {'email': 'owner@example.com', 'password': 'UserPass123!'})
        self.user_auth = {'HTTP_AUTHORIZATION': f"Bearer {user_login.json()['access']}"}

    def _create_org(self, name='Acme Corp', **auth):
        return self.client.post(reverse('organization-list-create'), {
            'name': name, 'phone': '12345', 'latitude': '40.123456', 'longitude': '23.654321',
        }, **auth)

    def test_activated_user_can_create_pending_organization(self):
        resp = self._create_org(**self.user_auth)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['status'], 'pending')
        org = Organization.objects.get(name='Acme Corp')
        self.assertEqual(org.owner, self.user)

    def test_anonymous_user_cannot_create_organization(self):
        resp = self._create_org()
        self.assertEqual(resp.status_code, 401)

    def test_user_can_own_multiple_organizations(self):
        self._create_org('Org One', **self.user_auth)
        self._create_org('Org Two', **self.user_auth)
        self.assertEqual(Organization.objects.filter(owner=self.user).count(), 2)

    def test_user_list_only_shows_own_organizations(self):
        _activated_user('other@example.com')
        other_login = self.client.post(reverse('login'), {'email': 'other@example.com', 'password': 'UserPass123!'})
        other_auth = {'HTTP_AUTHORIZATION': f"Bearer {other_login.json()['access']}"}

        self._create_org('Mine', **self.user_auth)
        self._create_org('Not Mine', **other_auth)

        resp = self.client.get(reverse('organization-list-create'), **self.user_auth)
        names = {item['name'] for item in resp.json()}
        self.assertEqual(names, {'Mine'})

    def test_admin_sees_all_and_can_filter_by_owner_and_status(self):
        other_user = _activated_user('other2@example.com')
        self._create_org('Org A', **self.user_auth)
        other_login = self.client.post(reverse('login'), {'email': 'other2@example.com', 'password': 'UserPass123!'})
        other_auth = {'HTTP_AUTHORIZATION': f"Bearer {other_login.json()['access']}"}
        self._create_org('Org B', **other_auth)

        resp = self.client.get(reverse('organization-list-create'), **self.admin_auth)
        self.assertEqual(len(resp.json()), 2)

        filtered = self.client.get(
            reverse('organization-list-create'), {'owner': other_user.id}, **self.admin_auth
        )
        self.assertEqual([item['name'] for item in filtered.json()], ['Org B'])

    def test_user_cannot_retrieve_other_users_organization(self):
        create_resp = self._create_org(**self.user_auth)
        org_id = create_resp.json()['id']
        other_user = _activated_user('other3@example.com')
        other_login = self.client.post(reverse('login'), {'email': 'other3@example.com', 'password': 'UserPass123!'})
        other_auth = {'HTTP_AUTHORIZATION': f"Bearer {other_login.json()['access']}"}

        resp = self.client.get(reverse('organization-detail', args=[org_id]), **other_auth)
        self.assertEqual(resp.status_code, 404)

        own_resp = self.client.get(reverse('organization-detail', args=[org_id]), **self.user_auth)
        self.assertEqual(own_resp.status_code, 200)

    def test_admin_approve_activates_organization_and_sends_email(self):
        create_resp = self._create_org(**self.user_auth)
        org_id = create_resp.json()['id']

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse('organization-approve', args=[org_id]), {}, **self.admin_auth)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'active')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('اعتماد', mail.outbox[0].subject)

        org = Organization.objects.get(id=org_id)
        self.assertEqual(org.status, 'active')
        self.assertEqual(org.reviewed_by, self.admin)

    def test_admin_deny_deletes_organization_and_sends_email(self):
        create_resp = self._create_org(**self.user_auth)
        org_id = create_resp.json()['id']

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse('organization-deny', args=[org_id]), {}, **self.admin_auth)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Organization.objects.filter(id=org_id).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('رفض', mail.outbox[0].subject)

    def test_non_admin_cannot_approve_or_deny(self):
        create_resp = self._create_org(**self.user_auth)
        org_id = create_resp.json()['id']

        approve_resp = self.client.post(
            reverse('organization-approve', args=[org_id]), {}, **self.user_auth
        )
        self.assertEqual(approve_resp.status_code, 403)

        deny_resp = self.client.post(
            reverse('organization-deny', args=[org_id]), {}, **self.user_auth
        )
        self.assertEqual(deny_resp.status_code, 403)

    def test_owner_can_update_own_organization(self):
        create_resp = self._create_org(**self.user_auth)
        org_id = create_resp.json()['id']

        resp = self.client.patch(
            reverse('organization-detail', args=[org_id]),
            {'name': 'Renamed Corp'},
            content_type='application/json',
            **self.user_auth,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Organization.objects.get(id=org_id).name, 'Renamed Corp')

    def test_other_user_cannot_update_organization(self):
        create_resp = self._create_org(**self.user_auth)
        org_id = create_resp.json()['id']
        _activated_user('other4@example.com')
        other_login = self.client.post(reverse('login'), {'email': 'other4@example.com', 'password': 'UserPass123!'})
        other_auth = {'HTTP_AUTHORIZATION': f"Bearer {other_login.json()['access']}"}

        resp = self.client.patch(
            reverse('organization-detail', args=[org_id]),
            {'name': 'Hijacked'},
            content_type='application/json',
            **other_auth,
        )
        self.assertEqual(resp.status_code, 404)  # not visible to other_auth's scoped queryset

    def test_admin_cannot_update_organization_they_do_not_own(self):
        create_resp = self._create_org(**self.user_auth)
        org_id = create_resp.json()['id']

        resp = self.client.patch(
            reverse('organization-detail', args=[org_id]),
            {'name': 'Admin Edit'},
            content_type='application/json',
            **self.admin_auth,
        )
        self.assertEqual(resp.status_code, 403)

    def test_owner_can_delete_own_organization(self):
        create_resp = self._create_org(**self.user_auth)
        org_id = create_resp.json()['id']

        resp = self.client.delete(reverse('organization-detail', args=[org_id]), **self.user_auth)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Organization.objects.filter(id=org_id).exists())

    def test_other_user_cannot_delete_organization(self):
        create_resp = self._create_org(**self.user_auth)
        org_id = create_resp.json()['id']
        _activated_user('other5@example.com')
        other_login = self.client.post(reverse('login'), {'email': 'other5@example.com', 'password': 'UserPass123!'})
        other_auth = {'HTTP_AUTHORIZATION': f"Bearer {other_login.json()['access']}"}

        resp = self.client.delete(reverse('organization-detail', args=[org_id]), **other_auth)
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(Organization.objects.filter(id=org_id).exists())

    def test_admin_can_delete_any_organization(self):
        create_resp = self._create_org(**self.user_auth)
        org_id = create_resp.json()['id']

        resp = self.client.delete(reverse('organization-detail', args=[org_id]), **self.admin_auth)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Organization.objects.filter(id=org_id).exists())
