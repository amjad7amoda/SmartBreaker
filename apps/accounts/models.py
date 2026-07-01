from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.utils import timezone


class UserManager(BaseUserManager):

    def create_user(self, email, password=None, role='home_user', **extra):
        email = self.normalize_email(email)
        user = self.model(email=email, role=role, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def create_superuser(self, email, password, **extra):
        extra['role'] = 'admin'
        extra['is_staff'] = True
        extra['is_superuser'] = True
        extra['is_active'] = True
        extra['must_set_password'] = False
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    ROLES = [
        ('admin', 'Admin'),
        ('technician', 'Technician'),
        ('home_user', 'Home User'),
    ]

    email       = models.EmailField(unique=True)
    phone       = models.CharField(max_length=20, blank=True)
    role        = models.CharField(max_length=20, choices=ROLES)
    is_active   = models.BooleanField(default=False)
    is_staff    = models.BooleanField(default=False)
    must_set_password = models.BooleanField(default=True)
    otp_hash    = models.CharField(max_length=128, null=True, blank=True)
    otp_expires_at = models.DateTimeField(null=True, blank=True)
    otp_last_sent_at = models.DateTimeField(null=True, blank=True)
    reset_code_hash = models.CharField(max_length=128, null=True, blank=True)
    reset_code_expires_at = models.DateTimeField(null=True, blank=True)
    reset_code_last_sent_at = models.DateTimeField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['role']

    objects = UserManager()

    def otp_is_valid(self, otp_plain):
        if not self.otp_hash or not self.otp_expires_at:
            return False
        if timezone.now() > self.otp_expires_at:
            return False
        from django.contrib.auth.hashers import check_password
        return check_password(otp_plain, self.otp_hash)

    def clear_otp(self):
        self.otp_hash = None
        self.otp_expires_at = None

    def reset_code_is_valid(self, code_plain):
        if not self.reset_code_hash or not self.reset_code_expires_at:
            return False
        if timezone.now() > self.reset_code_expires_at:
            return False
        from django.contrib.auth.hashers import check_password
        return check_password(code_plain, self.reset_code_hash)

    def clear_reset_code(self):
        self.reset_code_hash = None
        self.reset_code_expires_at = None


class RegistrationRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('denied', 'Denied'),
    ]
    REQUESTABLE_ROLES = [
        ('home_user', 'Home User'),
        ('technician', 'Technician'),
    ]

    email       = models.EmailField()
    phone       = models.CharField(max_length=20, blank=True)
    role        = models.CharField(max_length=20, choices=REQUESTABLE_ROLES)
    status      = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    reviewed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewed_requests'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_user = models.OneToOneField(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='registration_request'
    )
    created_at  = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.email} ({self.role}) - {self.status}'
