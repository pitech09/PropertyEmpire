from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from rest_framework.views import APIView
from rest_framework.decorators import api_view
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers, generics
from rest_framework.filters import OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from tennants.models import Tenant, House, Payment, FlatBuilding, RentCharge
from tennants.serializers import (TenantSerializer, HouseSerializer, PaymentSerializer,
                          FlatBuildingSerializer, RegisterAdminSerializer, AdminLoginSerializer, ForgotPasswordSerializer)
import logging
import requests
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.contrib.auth import authenticate, login
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.core.cache import cache
from django.core.exceptions import ValidationError
from rest_framework.decorators import permission_classes

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from rest_framework.exceptions import PermissionDenied, NotFound
from rest_framework_simplejwt.tokens import RefreshToken
import hashlib
import json
import logging
import secrets
from datetime import timedelta
from tennants.forms import (
    RegistrationForm,
    SMSPasswordResetConfirmForm,
    SMSPasswordResetRequestForm,
)
from tennants.models import LandlordProfile, PasswordResetCode
from tennants.services.sms import TwilioNotificationService
from django.shortcuts import render, redirect
from django.db import transaction

from django.views.generic import ListView, CreateView, UpdateView, DeleteView, DetailView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.urls import reverse_lazy
from django.contrib import messages
from django.shortcuts import render
from django.core.exceptions import ValidationError
from django.utils import timezone



logger = logging.getLogger(__name__)
# ============================================================================
# AUTHENTICATION VIEWS
# ============================================================================



class RegisterAdminView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RegisterAdminSerializer(data=request.data)
        if serializer.is_valid():
            username = serializer.validated_data['username']
            password = serializer.validated_data['password']
            email = serializer.validated_data.get('email', '')
            phone = serializer.validated_data.get('phone')

            try:
                user = User.objects.create_superuser(username=username, password=password, email=email)
                if phone:
                    LandlordProfile.objects.create(user=user, phone=phone)
                return Response({"message": "Admin registered successfully."}, status=status.HTTP_201_CREATED)
            except Exception as e:
                return Response({"message": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    
class AdminLogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            RefreshToken(request.data['refresh_token']).blacklist()
            return Response({"message": "Logout successful"}, status=status.HTTP_205_RESET_CONTENT)
        except Exception as e:
            return Response({"message": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        
# ============================================================================
# SINGLE LOGIN VIEW - AUTO DETECTS USER TYPE
# ============================================================================


def get_login_redirect_url(user):
    if user.is_superuser or user.is_staff:
        return "/api/owner/dashboard/"

    if Tenant.objects.filter(user=user).exists():
        return "/tenant/dashboard/"

    if hasattr(user, "worker_profile") and user.worker_profile is not None:
        return "/workers/dashboard/"

    return "/dashboard/"


@ensure_csrf_cookie
def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            login(request, user)

            return redirect(get_login_redirect_url(user))

        messages.error(request, 'Invalid username or password.')
    
    return render(request, 'login.html')

# ===========================================================================
# REGISTER USER VIEW
# ===========================================================================
# for normal user registration, not admin
class RegisterUserView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = RegisterAdminSerializer(data=request.data)
        if serializer.is_valid():
            username = serializer.validated_data['username']
            password = serializer.validated_data['password']
            email = serializer.validated_data.get('email', '')
            phone = serializer.validated_data.get('phone')

            try:
                user = User.objects.create_user(username=username, password=password, email=email)
                if phone:
                    LandlordProfile.objects.create(user=user, phone=phone)
                return Response({"message": "User registered successfully."}, status=status.HTTP_201_CREATED)
            except Exception as e:
                return Response({"message": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    

def register(request):
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_staff = False
            user.is_superuser = False
            user.save()
            LandlordProfile.objects.create(user=user, phone=form.cleaned_data["phone"])
            
            #  Automatically log the user in
            login(request, user)
            
            messages.success(request, "Welcome! Get started by adding your first building or property.")
            return redirect("dashboard")
            
    else:
        form = RegistrationForm()
    
    return render(request, "register.html", {"form": form})


def _find_user_for_sms_reset(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return None

    user = User.objects.filter(username__iexact=identifier).first()
    if user:
        return user

    user = User.objects.filter(email__iexact=identifier).first()
    if user:
        return user

    tenant = Tenant.objects.filter(phone=identifier).select_related("user").first()
    if tenant and tenant.user_id:
        return tenant.user

    profile = LandlordProfile.objects.filter(phone=identifier).select_related("user").first()
    if profile:
        return profile.user

    return None


def _sms_number_for_user(user):
    tenant = Tenant.objects.filter(user=user, sms_notifications=True).first()
    if tenant and tenant.phone:
        return tenant.phone

    profile = getattr(user, "landlord_profile", None)
    if profile and profile.sms_notifications and profile.phone:
        return profile.phone

    return None


def sms_password_reset_request(request):
    if request.method == "POST":
        form = SMSPasswordResetRequestForm(request.POST)
        if form.is_valid():
            user = _find_user_for_sms_reset(form.cleaned_data["identifier"])
            sms_number = _sms_number_for_user(user) if user else None

            if user and sms_number:
                code = f"{secrets.randbelow(1000000):06d}"
                PasswordResetCode.objects.create(
                    user=user,
                    code=code,
                    expires_at=timezone.now() + timedelta(minutes=15),
                )
                message = f"PropertyEmpire password reset code: {code}. It expires in 15 minutes."
                TwilioNotificationService().send_sms(sms_number, message)
                request.session["sms_password_reset_user_id"] = user.pk

            messages.success(request, "If the account has an SMS number, a reset code has been sent.")
            return redirect("sms_password_reset_confirm")
    else:
        form = SMSPasswordResetRequestForm()

    return render(request, "password_reset_sms_request.html", {"form": form})


def sms_password_reset_confirm(request):
    user_id = request.session.get("sms_password_reset_user_id")
    user = User.objects.filter(pk=user_id).first() if user_id else None

    if request.method == "POST":
        form = SMSPasswordResetConfirmForm(request.POST)
        if form.is_valid() and user:
            code = PasswordResetCode.objects.filter(
                user=user,
                code=form.cleaned_data["code"],
                used_at__isnull=True,
                expires_at__gte=timezone.now(),
            ).first()

            if code is None:
                form.add_error("code", "Invalid or expired reset code.")
            else:
                try:
                    validate_password(form.cleaned_data["new_password"], user)
                except ValidationError as exc:
                    form.add_error("new_password", exc)
                else:
                    user.set_password(form.cleaned_data["new_password"])
                    user.save(update_fields=["password"])
                    code.mark_used()
                    request.session.pop("sms_password_reset_user_id", None)
                    messages.success(request, "Password reset successfully. You can sign in now.")
                    return redirect("login")
        elif not user:
            messages.error(request, "Please request a new reset code.")
            return redirect("sms_password_reset")
    else:
        form = SMSPasswordResetConfirmForm()

    return render(request, "password_reset_sms_confirm.html", {"form": form})
