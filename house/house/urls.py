"""
URL configuration for house project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
"""

from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path, include
from django.http import HttpResponse
from rest_framework.authtoken.views import obtain_auth_token
from django.contrib.auth.decorators import login_required
from django.contrib.auth import views as auth_views
from django.shortcuts import redirect
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt import views as jwt_views
from tennants.views.auth import (
    RegistrationForm,
    login_view,
    register,
    sms_password_reset_confirm,
    sms_password_reset_request,
)
from tennants.views.web import landing_page, dashboard
from tennants.views.tenants import report_issue, tenant_dashboard

from django.urls import reverse_lazy

LOGOUT_REDIRECT_URL = reverse_lazy('landing')

urlpatterns = [
    path("", landing_page, name="landing"),
    path("admin/", admin.site.urls),
    path("api/admin/logout/", TokenRefreshView.as_view(), name="admin_logout"),
    path("acounts/", include("django.contrib.auth.urls")),
    path("api/", include("tennants.urls")),
    path("api/token/", jwt_views.TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/token/refresh/", jwt_views.TokenRefreshView.as_view(), name="token_refresh"),
    path("register/", register, name="register"),
    path("login/", login_view, name="login"),
    path("password-reset/", sms_password_reset_request, name="sms_password_reset"),
    path("password-reset/confirm/", sms_password_reset_confirm, name="sms_password_reset_confirm"),
    path("dashboard/", dashboard, name="dashboard"),
    path("tenant/dashboard/", tenant_dashboard, name="tenant_dashboard"),
    path("tenant/report/", report_issue, name="report_issue"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
