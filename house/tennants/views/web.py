from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from rest_framework.views import APIView
from rest_framework.decorators import api_view
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework import serializers, generics
from rest_framework.filters import OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from tennants.models import Tenant, House, HouseImage, Payment, FlatBuilding, RentCharge, Issue, PaymentRequest
from tennants.serializers import (TenantSerializer, HouseSerializer, PaymentSerializer,
                          FlatBuildingSerializer, RegisterAdminSerializer, AdminLoginSerializer, ForgotPasswordSerializer)
import logging
import requests
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import authenticate, login
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.core.cache import cache
from django.db.models import Sum, F, Value, ExpressionWrapper, DecimalField
from django.db.models.functions import Coalesce, Greatest
from django.core.exceptions import ValidationError
from rest_framework.decorators import permission_classes

from django.contrib.auth.models import User
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from rest_framework.exceptions import PermissionDenied, NotFound
from rest_framework_simplejwt.tokens import RefreshToken
import hashlib
import json
import logging
from tennants.forms import RegistrationForm, HouseImageForm
from django.shortcuts import render, redirect
from django.db import IntegrityError, transaction

from django.views.generic import ListView, CreateView, UpdateView, DeleteView, DetailView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.urls import reverse_lazy
from django.contrib import messages
from django.shortcuts import render
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db.models import Sum
from django.http import HttpResponseForbidden
from tennants.services.sms import TwilioNotificationService
from tennants.services.tenant_accounts import create_tenant_login_user, send_tenant_credentials_sms
from tennants.services.payment_requests import (
    notify_tenant_issue_status,
    notify_tenant_payment_request_status,
    approve_payment_request as service_approve_payment_request,
    reject_payment_request as service_reject_payment_request,
)
from decimal import Decimal




logger = logging.getLogger(__name__)


def _money(value):
    return value or Decimal("0.00")


def _landlord_financials(user):
    from tennants.models import Expense
    payments = Payment.objects.filter(tenant__house__user=user)
    charges = RentCharge.objects.filter(tenant__house__user=user).select_related("tenant", "tenant__house")
    active_tenants = Tenant.objects.filter(house__user=user, is_active=True, house__isnull=False)
    houses = House.objects.filter(user=user)

    # Income
    total_collected = _money(payments.aggregate(total=Sum("amount"))["total"])
    total_billed = _money(charges.aggregate(total=Sum("amount_due"))["total"])

    # Outstanding computed in SQL (avoids the N+1 from RentCharge.total_paid)
    # and clamped at zero so overpayments don't mask arrears on other charges.
    zero = Value(Decimal("0.00"), output_field=DecimalField(max_digits=10, decimal_places=2))
    charges = charges.annotate(
        paid_total=Coalesce(Sum("payments__amount"), zero),
        outstanding_balance=ExpressionWrapper(
            Greatest(F("amount_due") - Coalesce(Sum("payments__amount"), zero), zero),
            output_field=DecimalField(max_digits=10, decimal_places=2),
        ),
    )
    total_outstanding = _money(charges.aggregate(total=Sum("outstanding_balance"))["total"])
    expected_monthly_rent = sum((tenant.rent for tenant in active_tenants), Decimal("0.00"))
    collection_rate = round((total_collected / total_billed * 100), 1) if total_billed else 0

    # Expenses
    total_expenses = _money(Expense.objects.filter(user=user).aggregate(total=Sum("amount"))["total"])
    unrecovered_expenses = _money(
        Expense.objects.filter(user=user, is_recoverable=True, is_recovered=False)
        .aggregate(total=Sum("amount"))["total"]
    )
    effective_expenses = total_expenses - unrecovered_expenses
    net_income = total_collected - effective_expenses

    # Platform fees. The subscription is a monthly cost, so the "net after
    # fees" figure is computed against the current month's collections rather
    # than all-time collections (which would understate fees for any landlord
    # with more than one month of history).
    platform_fee = total_collected * settings.PLATFORM_TRANSACTION_FEE_RATE
    monthly_sub = settings.LANDLORD_MONTHLY_SUBSCRIPTION
    now = timezone.now()
    collected_this_month = _money(
        payments.filter(paid_at__year=now.year, paid_at__month=now.month)
        .aggregate(total=Sum("amount"))["total"]
    )
    platform_fee_this_month = collected_this_month * settings.PLATFORM_TRANSACTION_FEE_RATE

    # Security deposits held — only for houses with active tenants
    # (a vacant house's deposit has been refunded and must not be counted)
    occupied_houses = houses.filter(tenants__is_active=True).distinct()
    total_deposits_held = _money(
        occupied_houses.aggregate(total=Sum("deposit_amount"))["total"]
    )

    # Pending
    pending_payment_requests = PaymentRequest.objects.filter(
        tenant__house__user=user, status="pending"
    ).count()
    pending_recoverable_count = Expense.objects.filter(
        user=user, is_recoverable=True, is_recovered=False
    ).count()

    return {
        # Income
        "total_collected": total_collected,
        "total_billed": total_billed,
        "total_outstanding": total_outstanding,
        "expected_monthly_rent": expected_monthly_rent,
        "collection_rate": collection_rate,
        # Expenses
        "total_expenses": total_expenses,
        "unrecovered_expenses": unrecovered_expenses,
        "net_income": net_income,
        # Deposits
        "total_deposits_held": total_deposits_held,
        "deposit_count": occupied_houses.filter(deposit_amount__gt=0).count(),
        # Platform
        "platform_fee": platform_fee,
        "collected_this_month": collected_this_month,
        "platform_fee_this_month": platform_fee_this_month,
        "monthly_subscription": monthly_sub,
        "net_after_fees_platform": collected_this_month - platform_fee_this_month - monthly_sub,
        # Pending
        "pending_recoverable_count": pending_recoverable_count,
        "pending_payment_requests": pending_payment_requests,
        # Counts
        "payment_count": payments.count(),
        "tenant_count": active_tenants.count(),
        "active_tenant_count": active_tenants.count(),
        "house_count": houses.count(),
    }


def _landlord_building_financials(user):
    rows = []
    buildings = FlatBuilding.objects.filter(user=user).prefetch_related("houses")
    from tennants.models import Expense
    from django.db.models import Sum, Q

    for building in buildings:
        payments = Payment.objects.filter(tenant__house__flat_building=building)
        zero = Value(Decimal("0.00"), output_field=DecimalField(max_digits=10, decimal_places=2))
        charges = RentCharge.objects.filter(tenant__house__flat_building=building).annotate(
            outstanding_balance=ExpressionWrapper(
                Greatest(F("amount_due") - Coalesce(Sum("payments__amount"), zero), zero),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ),
        )
        active_tenants = Tenant.objects.filter(house__flat_building=building, is_active=True)
        collected = _money(payments.aggregate(total=Sum("amount"))["total"])
        billed = _money(charges.aggregate(total=Sum("amount_due"))["total"])
        outstanding = _money(charges.aggregate(total=Sum("outstanding_balance"))["total"])
        expected_rent = sum((tenant.rent for tenant in active_tenants), Decimal("0.00"))

        # Expense tracking per building (consistent with the top-level logic:
        # only expenses that will NOT be recovered reduce net income).
        # Expenses can be attached to the building directly, to a house in the
        # building, or to a tenant of the building — all three count here so
        # building rows reconcile with the top-level totals.
        expense_scope = Q(flat_building=building) | Q(house__flat_building=building) | Q(tenant__house__flat_building=building)
        total_expenses = _money(
            Expense.objects.filter(Q(user=user) & expense_scope).aggregate(total=Sum("amount"))["total"]
        )
        unrecovered_expenses = _money(
            Expense.objects.filter(Q(user=user) & expense_scope, is_recoverable=True, is_recovered=False)
            .aggregate(total=Sum("amount"))["total"]
        )
        net_income = collected - (total_expenses - unrecovered_expenses)

        rows.append({
            "building": building,
            "collected": collected,
            "billed": billed,
            "outstanding": outstanding,
            "expected_rent": expected_rent,
            "expenses": total_expenses,
            "net_income": net_income,
            "active_tenants": active_tenants.count(),
            "occupancy": building.how_many_occupied,
        })

    return rows


# this views are for returning html pages
# ============================================================================
# WEB TEMPLATE VIEWS (for normal users in browser)
# ============================================================================

# landing page
def landing_page(request):
    """Render the landing page"""
    return render(request, 'landing.html')

# view to change passowrd for normal users
class ForgotPasswordViewWeb(APIView):
    permission_classes = [AllowAny]
    
    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        

# Dashboard
@login_required
def dashboard(request):
    """Main dashboard showing summary stats"""
    buildings = FlatBuilding.objects.filter(user=request.user)
    total_houses = House.objects.filter(user=request.user).count()
    occupied_houses = House.objects.filter(user=request.user, occupation=True).count()
    active_tenants = Tenant.objects.filter(house__user=request.user, is_active=True).count()
    
    # Recent payments (last 5) — scoped via the tenant/house chain so payments
    # saved without a `user` are still attributed to the right landlord.
    recent_payments = Payment.objects.filter(
        tenant__house__user=request.user
    ).order_by('-paid_at')[:5]

    unpaid_charges = [
        charge for charge in RentCharge.objects.filter(
            tenant__house__user=request.user
        ).select_related("tenant", "tenant__house").order_by("-year", "-month")
        if not charge.is_paid
    ][:5]

    recent_issues = Issue.objects.filter(
        tenant__house__user=request.user
    ).select_related(
        "tenant",
        "tenant__house",
        "tenant__house__flat_building",
    ).order_by("-created_at")[:5]

    recent_payment_requests = PaymentRequest.objects.filter(
        tenant__house__user=request.user
    ).select_related(
        "tenant",
        "tenant__house",
        "rent_charge",
    ).order_by("-created_at")[:5]
    pending_payment_requests = PaymentRequest.objects.filter(
        tenant__house__user=request.user,
        status="pending",
    ).count()

    pending_issues = Issue.objects.filter(
        tenant__house__user=request.user,
        status="pending",
    ).count()
    in_progress_issues = Issue.objects.filter(
        tenant__house__user=request.user,
        status="in_progress",
    ).count()
    resolved_issues = Issue.objects.filter(
        tenant__house__user=request.user,
        status="resolved",
    ).count()
    
    # Calculate percentage of occupied houses (avoid division by zero)
    if total_houses:
        percent_occupied = round((occupied_houses / total_houses) * 100, 2)
    else:
        percent_occupied = 0.0
    
    context = {
        'buildings': buildings,
        'total_houses': total_houses,
        'occupied_houses': occupied_houses,
        'vacant_houses': total_houses - occupied_houses,
        'active_tenants': active_tenants,
        'recent_payments': recent_payments,
        'unpaid_charges': unpaid_charges,
        'recent_issues': recent_issues,
        'recent_payment_requests': recent_payment_requests,
        'pending_issues': pending_issues,
        'in_progress_issues': in_progress_issues,
        'resolved_issues': resolved_issues,
        'pending_payment_requests': pending_payment_requests,
        'percent_occupied': percent_occupied,
    }
    return render(request, 'dashboard.html', context)


@login_required
def reports(request):
    """Reports hub: financial, occupancy, payment, maintenance, tenant."""
    from datetime import datetime
    from django.db.models.functions import TruncMonth
    from django.db.models import Count

    if Tenant.objects.filter(user=request.user).exists():
        return HttpResponseForbidden()

    user = request.user
    report_type = request.GET.get('type', 'financial')
    months_back = int(request.GET.get('months', '6'))
    months_back = max(1, min(months_back, 24))

    now = timezone.now()
    start_date = now - timezone.timedelta(days=30 * months_back)

    # --- Financial report ---
    financials = _landlord_financials(user)
    payments = Payment.objects.filter(
        tenant__house__user=user, paid_at__gte=start_date
    ).select_related('tenant', 'tenant__house', 'rent_charge').order_by('-paid_at')
    charges = RentCharge.objects.filter(
        tenant__house__user=user
    ).select_related('tenant', 'tenant__house').order_by('-year', '-month')

    # --- Occupancy report ---
    buildings = FlatBuilding.objects.filter(user=user).prefetch_related('houses')
    total_houses = House.objects.filter(user=user).count()
    occupied_houses = House.objects.filter(user=user, occupation=True).count()
    vacant_houses = total_houses - occupied_houses
    occupancy_pct = round((occupied_houses / total_houses) * 100, 1) if total_houses else 0

    # --- Payment report (monthly buckets) ---
    monthly_payments = (
        Payment.objects
        .filter(tenant__house__user=user, paid_at__gte=start_date)
        .annotate(month=TruncMonth('paid_at'))
        .values('month')
        .annotate(total=Sum('amount'), count=Count('id'))
        .order_by('month')
    )
    monthly_outstanding = (
        RentCharge.objects
        .filter(tenant__house__user=user, year__gte=start_date.year)
        .values('year', 'month')
        .annotate(billed=Sum('amount_due'))
        .order_by('year', 'month')
    )

    # --- Maintenance report ---
    issues_qs = Issue.objects.filter(tenant__house__user=user)
    issues_pending = issues_qs.filter(status='pending').count()
    issues_inprogress = issues_qs.filter(status='in_progress').count()
    issues_approved = issues_qs.filter(status='approved').count()
    issues_resolved = issues_qs.filter(status='resolved').count()
    recent_issues = issues_qs.select_related('tenant', 'tenant__house').order_by('-created_at')[:20]

    # --- Tenant report ---
    tenants_all = Tenant.objects.filter(house__user=user).select_related('house', 'house__flat_building')
    tenants_active = tenants_all.filter(is_active=True)
    tenants_inactive = tenants_all.filter(is_active=False)
    tenants_with_balance = []
    for t in tenants_active:
        b = sum(c.balance for c in RentCharge.objects.filter(tenant=t))
        if b > 0:
            tenants_with_balance.append({'tenant': t, 'balance': b})
    tenants_with_balance.sort(key=lambda x: x['balance'], reverse=True)

    context = {
        'report_type': report_type,
        'months_back': months_back,
        'start_date': start_date,
        'end_date': now,
        'buildings_count': buildings.count(),

        # Financial
        'financials': financials,
        'payments': payments[:50],
        'charges': charges[:50],

        # Occupancy
        'buildings': buildings,
        'total_houses': total_houses,
        'occupied_houses': occupied_houses,
        'vacant_houses': vacant_houses,
        'occupancy_pct': occupancy_pct,

        # Monthly series
        'monthly_payments': list(monthly_payments),
        'monthly_outstanding': list(monthly_outstanding),

        # Maintenance
        'issues_pending': issues_pending,
        'issues_inprogress': issues_inprogress,
        'issues_approved': issues_approved,
        'issues_resolved': issues_resolved,
        'recent_issues': recent_issues,

        # Tenants
        'tenants_active_count': tenants_active.count(),
        'tenants_inactive_count': tenants_inactive.count(),
        'tenants_with_balance': tenants_with_balance[:20],
    }
    return render(request, 'reports.html', context)


@login_required
def export_payments_csv(request):
    """Export payments to CSV for the current landlord."""
    import csv
    from django.http import HttpResponse

    if Tenant.objects.filter(user=request.user).exists():
        return HttpResponseForbidden()

    payments = Payment.objects.filter(
        tenant__house__user=request.user
    ).select_related('tenant', 'tenant__house', 'rent_charge').order_by('-paid_at')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="payments_export.csv"'
    writer = csv.writer(response)
    writer.writerow(['Date', 'Tenant', 'House', 'Rent Period', 'Amount (M)', 'Method', 'Reference'])
    for p in payments:
        writer.writerow([
            p.paid_at.strftime('%Y-%m-%d %H:%M'),
            p.tenant.full_name,
            p.tenant.house.house_number if p.tenant.house else '-',
            f"{p.rent_charge.get_month_display()} {p.rent_charge.year}" if p.rent_charge else '-',
            p.amount,
            p.get_payment_method_display(),
            p.payment_reference or '-',
        ])
    return response


@login_required
def export_tenants_csv(request):
    """Export tenants to CSV for the current landlord."""
    import csv
    from django.http import HttpResponse

    if Tenant.objects.filter(user=request.user).exists():
        return HttpResponseForbidden()

    tenants = Tenant.objects.filter(
        house__user=request.user
    ).select_related('house', 'house__flat_building').order_by('full_name')

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="tenants_export.csv"'
    writer = csv.writer(response)
    writer.writerow(['Name', 'Phone', 'Email', 'ID Number', 'House', 'Building', 'Status', 'Rent Due Date'])
    for t in tenants:
        writer.writerow([
            t.full_name,
            t.phone or '-',
            t.email or '-',
            t.id_number or '-',
            t.house.house_number if t.house else '-',
            t.house.flat_building.building_name if t.house and t.house.flat_building else '-',
            'Active' if t.is_active else 'Inactive',
            t.rent_due_date.strftime('%Y-%m-%d') if t.rent_due_date else '-',
        ])
    return response


@login_required
def landlord_financial_dashboard(request):

    if Tenant.objects.filter(user=request.user).exists():
        return HttpResponseForbidden()

    financials = _landlord_financials(request.user)
    recent_payments = Payment.objects.filter(
        tenant__house__user=request.user
    ).select_related("tenant", "tenant__house", "rent_charge").order_by("-paid_at")[:10]

    # Unpaid charges filtered in SQL (annotated balance) instead of evaluating
    # the per-instance `balance` property, which ran 2 queries per charge.
    unpaid_charges = (
        RentCharge.objects.filter(tenant__house__user=request.user)
        .select_related("tenant", "tenant__house")
        .annotate(
            paid_total=Coalesce(
                Sum("payments__amount"),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ),
            balance_annot=ExpressionWrapper(
                F("amount_due") - Coalesce(
                    Sum("payments__amount"),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=10, decimal_places=2),
                ),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ),
        )
        .filter(amount_due__gt=F("paid_total"))
        .order_by("-year", "-month")[:10]
    )

    context = {
        **financials,
        "transaction_fee_rate": settings.PLATFORM_TRANSACTION_FEE_RATE,
        "transaction_fee_rate_percent": settings.PLATFORM_TRANSACTION_FEE_RATE * 100,
        "monthly_subscription": settings.LANDLORD_MONTHLY_SUBSCRIPTION,
        "building_financials": _landlord_building_financials(request.user),
        "recent_payments": recent_payments,
        "unpaid_charges": unpaid_charges,
    }
    return render(request, "financials/landlord_dashboard.html", context)


@login_required
def owner_dashboard(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return HttpResponseForbidden()

    landlords = User.objects.filter(
        is_staff=False,
        is_superuser=False,
        tenant__isnull=True,
    ).distinct().order_by("username")
    landlord_rows = []
    platform_transaction_revenue = Decimal("0.00")
    total_collected = Decimal("0.00")
    total_outstanding = Decimal("0.00")

    for landlord in landlords:
        financials = _landlord_financials(landlord)
        platform_transaction_revenue += financials["platform_fee"]
        total_collected += financials["total_collected"]
        total_outstanding += financials["total_outstanding"]
        landlord_rows.append({
            "landlord": landlord,
            **financials,
            "buildings": FlatBuilding.objects.filter(user=landlord).count(),
            "houses": House.objects.filter(user=landlord).count(),
            "occupied_houses": House.objects.filter(user=landlord, occupation=True).count(),
            "payment_requests": PaymentRequest.objects.filter(tenant__house__user=landlord).count(),
            "maintenance_reports": Issue.objects.filter(tenant__house__user=landlord).count(),
        })

    subscription_revenue = landlords.count() * settings.LANDLORD_MONTHLY_SUBSCRIPTION
    total_houses = House.objects.count()
    occupied_houses = House.objects.filter(occupation=True).count()
    occupancy_rate = round((occupied_houses / total_houses) * 100, 2) if total_houses else 0

    context = {
        "landlord_rows": landlord_rows,
        "landlord_count": landlords.count(),
        "building_count": FlatBuilding.objects.count(),
        "house_count": total_houses,
        "occupied_houses": occupied_houses,
        "vacant_houses": total_houses - occupied_houses,
        "tenant_count": Tenant.objects.count(),
        "active_tenant_count": Tenant.objects.filter(is_active=True).count(),
        "occupancy_rate": occupancy_rate,
        "payment_count": Payment.objects.count(),
        "payment_request_count": PaymentRequest.objects.count(),
        "pending_payment_request_count": PaymentRequest.objects.filter(status="pending").count(),
        "maintenance_report_count": Issue.objects.count(),
        "pending_maintenance_count": Issue.objects.filter(status="pending").count(),
        "approved_maintenance_count": Issue.objects.filter(status="approved").count(),
        # Rental (houses) totals
        "total_collected": total_collected,
        "total_outstanding": total_outstanding,
        # Rental platform revenue split
        "rental_transaction_revenue": platform_transaction_revenue,
        "subscription_revenue": subscription_revenue,
        "platform_transaction_revenue": platform_transaction_revenue,
        "total_platform_revenue": (
            platform_transaction_revenue + subscription_revenue
        ),
        "transaction_fee_rate": settings.PLATFORM_TRANSACTION_FEE_RATE,
        "monthly_subscription": settings.LANDLORD_MONTHLY_SUBSCRIPTION,
        "recent_payments": Payment.objects.select_related(
            "tenant",
            "tenant__house",
            "tenant__house__user",
            "rent_charge",
        ).order_by("-paid_at")[:10],
        "recent_payment_requests": PaymentRequest.objects.select_related(
            "tenant",
            "tenant__house",
            "tenant__house__user",
            "rent_charge",
        ).order_by("-created_at")[:10],
        "recent_issues": Issue.objects.select_related(
            "tenant",
            "tenant__house",
            "tenant__house__user",
        ).order_by("-created_at")[:10],
    }
    return render(request, "financials/owner_dashboard.html", context)


# ============================================================================
# BUILDING VIEWS
# ============================================================================

class BuildingListViewWeb(LoginRequiredMixin, ListView):
    model = FlatBuilding
    template_name = 'buildings/building_list.html'
    context_object_name = 'buildings'
    
    def get_queryset(self):
        return FlatBuilding.objects.filter(user=self.request.user)


class BuildingCreateViewWeb(LoginRequiredMixin, CreateView):
    model = FlatBuilding
    fields = ['building_name', 'address', 'number_of_houses']
    template_name = 'buildings/building_form.html'
    success_url = reverse_lazy('building_list')
    
    def form_valid(self, form):
        form.instance.user = self.request.user
        messages.success(self.request, 'Building created successfully!')
        return super().form_valid(form)


class BuildingUpdateViewWeb(LoginRequiredMixin, UpdateView):
    model = FlatBuilding
    fields = ['building_name', 'address', 'number_of_houses']
    template_name = 'buildings/building_form.html'
    success_url = reverse_lazy('building_list')
    
    def get_queryset(self):
        return FlatBuilding.objects.filter(user=self.request.user)
    
    def form_valid(self, form):
        messages.success(self.request, 'Building updated successfully!')
        return super().form_valid(form)


class BuildingDeleteViewWeb(LoginRequiredMixin, DeleteView):
    model = FlatBuilding
    template_name = 'buildings/building_confirm_delete.html'
    success_url = reverse_lazy('building_list')
    
    def get_queryset(self):
        return FlatBuilding.objects.filter(user=self.request.user)
    
    def delete(self, request, *args, **kwargs):
        # first check if building has houses and those houses have tenants
        building = self.get_object()
        if building.houses.filter(occupation=True).exists():
            messages.error(self.request, 'Cannot delete building with occupied houses!')
            return redirect('building_list')
        messages.success(self.request, 'Building deleted successfully!')
        return super().delete(request, *args, **kwargs)
        

class BuildingDetailViewWeb(LoginRequiredMixin, DetailView):
    model = FlatBuilding
    template_name = 'buildings/building_detail.html'
    context_object_name = 'building'
    
    def get_queryset(self):
        return FlatBuilding.objects.filter(user=self.request.user)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Get all houses in this building
        context['houses'] = self.object.houses.all()
        return context


# ============================================================================
# HOUSE VIEWS
# ============================================================================

class HouseListViewWeb(LoginRequiredMixin, ListView):
    model = House
    template_name = 'houses/house_list.html'
    context_object_name = 'houses'
    
    def get_queryset(self):
        queryset = House.objects.filter(user=self.request.user)
        # Optional filter by building
        building_id = self.request.GET.get('building')
        if building_id:
            queryset = queryset.filter(flat_building_id=building_id)
        return queryset
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['buildings'] = FlatBuilding.objects.filter(user=self.request.user)
        return context


class HouseCreateViewWeb(LoginRequiredMixin, CreateView):
    model = House
    fields = ['flat_building', 'house_number', 'house_size', 'house_rent_amount', 'deposit_amount']
    template_name = 'houses/house_form.html'
    success_url = reverse_lazy('house_list')
    
    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Only show user's buildings in dropdown
        form.fields['flat_building'].queryset = FlatBuilding.objects.filter(user=self.request.user)
        return form
    
    def form_valid(self, form):
        form.instance.user = self.request.user
        try:
            messages.success(self.request, 'House created successfully!')
            return super().form_valid(form)
        except ValidationError as e:
            messages.error(self.request, str(e))
            return self.form_invalid(form)


class HouseUpdateViewWeb(LoginRequiredMixin, UpdateView):
    model = House
    fields = ['flat_building', 'house_number', 'house_size', 'house_rent_amount', 'deposit_amount']
    template_name = 'houses/house_form.html'
    success_url = reverse_lazy('house_list')
    
    def get_queryset(self):
        return House.objects.filter(user=self.request.user)
    
    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields['flat_building'].queryset = FlatBuilding.objects.filter(user=self.request.user)
        return form
    
    def form_valid(self, form):
        messages.success(self.request, 'House updated successfully!')
        return super().form_valid(form)


class HouseDeleteViewWeb(LoginRequiredMixin, DeleteView):
    model = House
    template_name = 'houses/house_confirm_delete.html'
    success_url = reverse_lazy('house_list')
    
    def get_queryset(self):
        return House.objects.filter(user=self.request.user)


class HouseDetailViewWeb(LoginRequiredMixin, DetailView):
    model = House
    template_name = 'houses/house_detail.html'
    context_object_name = 'house'
    
    def get_queryset(self):
        return House.objects.filter(user=self.request.user)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Get current tenant if any
        context['tenant'] = self.object.tenants.filter(is_active=True).first()
        context['issues'] = Issue.objects.filter(
            tenant__house=self.object,
            tenant__house__user=self.request.user,
        ).select_related("tenant").order_by("-created_at")
        return context


# ============================================================================
# TENANT VIEWS
# ============================================================================

class TenantListViewWeb(LoginRequiredMixin, ListView):
    model = Tenant
    template_name = 'tenants/tenant_list.html'
    context_object_name = 'tenants'
    
    def get_queryset(self):
        queryset = Tenant.objects.filter(house__user=self.request.user)
        # Optional filter by active status
        status = self.request.GET.get('status')
        if status == 'active':
            queryset = queryset.filter(is_active=True)
        elif status == 'inactive':
            queryset = queryset.filter(is_active=False)
        return queryset


class TenantCreateViewWeb(LoginRequiredMixin, CreateView):
    model = Tenant
    fields = ['full_name', 'email', 'phone', 'id_number', 'house', 'rent_due_date']
    template_name = 'tenants/tenant_form.html'
    success_url = reverse_lazy('tenant_list')
    
    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Only show vacant houses
        form.fields['house'].queryset = House.objects.filter(
            user=self.request.user, 
            occupation=False
        )
        return form
    
    def form_valid(self, form):
        try:
            with transaction.atomic():
                tenant = form.save(commit=False)
                tenant.user, password = create_tenant_login_user(
                    tenant.full_name,
                    email=tenant.email,
                    phone=tenant.phone,
                    id_number=tenant.id_number,
                )
                tenant._skip_welcome_sms = True
                tenant.save()
                form.save_m2m()
                self.object = tenant

                login_url = self.request.build_absolute_uri(reverse_lazy("login"))
                transaction.on_commit(
                    lambda: send_tenant_credentials_sms(tenant, password, login_url=login_url)
                )

            messages.success(
                self.request,
                f"Tenant added successfully. Login credentials were sent to {tenant.phone}.",
            )
            return redirect(self.get_success_url())
        except ValidationError as e:
            messages.error(self.request, str(e))
            return self.form_invalid(form)


class TenantUpdateViewWeb(LoginRequiredMixin, UpdateView):
    model = Tenant
    fields = ['full_name', 'email', 'phone', 'id_number', 'house', 'rent_due_date', 'is_active']
    template_name = 'tenants/tenant_form.html'
    success_url = reverse_lazy('tenant_list')
    
    def get_queryset(self):
        return Tenant.objects.filter(house__user=self.request.user)
    
    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Show all user's houses
        form.fields['house'].queryset = House.objects.filter(user=self.request.user)
        return form
    
    def form_valid(self, form):
        messages.success(self.request, 'Tenant updated successfully!')
        return super().form_valid(form)


class TenantDeleteViewWeb(LoginRequiredMixin, DeleteView):
    model = Tenant
    template_name = 'tenants/tenant_confirm_delete.html'
    success_url = reverse_lazy('tenant_list')
    
    def get_queryset(self):
        return Tenant.objects.filter(house__user=self.request.user)


class TenantDetailViewWeb(LoginRequiredMixin, DetailView):
    model = Tenant
    template_name = 'tenants/tenant_detail.html'
    context_object_name = 'tenant'
    
    def get_queryset(self):
        return Tenant.objects.filter(house__user=self.request.user)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Get payment history
        context['payments'] = self.object.payments.all().order_by('-paid_at')
        context['issues'] = self.object.issue_set.all().order_by("-created_at")
        # context['overdue_payments'] = self.object.rent_payments.filter(is_paid=False)
        return context


class PaymentDeleteViewWeb(LoginRequiredMixin, DeleteView):
    model = PaymentRequest
    template_name = 'payments/payment_confirm_delete.html'
    success_url = reverse_lazy('payment_list')
    
    def get_queryset(self):
        return PaymentRequest.objects.filter(tenant__house__user=self.request.user)


class PaymentRequestListViewWeb(LoginRequiredMixin, ListView):
    model = PaymentRequest
    template_name = "payments/payment_request_list.html"
    context_object_name = "payment_requests"

    def get_queryset(self):
        queryset = PaymentRequest.objects.filter(
            tenant__house__user=self.request.user
        ).select_related("tenant", "tenant__house", "rent_charge")

        status_filter = self.request.GET.get("status")
        if status_filter in dict(PaymentRequest.STATUS_CHOICES):
            queryset = queryset.filter(status=status_filter)

        return queryset.order_by("-created_at")


class PaymentRequestDetailViewWeb(LoginRequiredMixin, DetailView):
    model = PaymentRequest
    template_name = "payments/payment_request_detail.html"
    context_object_name = "payment_request"

    def get_queryset(self):
        return PaymentRequest.objects.filter(
            tenant__house__user=self.request.user
        ).select_related("tenant", "tenant__house", "rent_charge")


class PaymentRequestCreateViewWeb(LoginRequiredMixin, CreateView):
    model = PaymentRequest
    fields = ["tenant", "rent_charge", "amount", "payment_method", "payment_reference", "status"]
    template_name = "payments/payment_request_form.html"
    success_url = reverse_lazy("payment_request_list")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["tenant"].queryset = Tenant.objects.filter(house__user=self.request.user)
        form.fields["rent_charge"].queryset = RentCharge.objects.filter(
            tenant__house__user=self.request.user
        )
        for field in form.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        return form

    def form_valid(self, form):
        messages.success(self.request, "Payment request created successfully!")
        return super().form_valid(form)


class PaymentRequestUpdateViewWeb(LoginRequiredMixin, UpdateView):
    model = PaymentRequest
    fields = ["status"]
    template_name = "payments/payment_request_form.html"
    success_url = reverse_lazy("payment_request_list")

    def get_queryset(self):
        return PaymentRequest.objects.filter(
            tenant__house__user=self.request.user
        ).select_related("tenant", "rent_charge")

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        for field in form.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        return form

    def form_valid(self, form):
        old_status = PaymentRequest.objects.get(pk=form.instance.pk).status
        response = super().form_valid(form)

        if self.object.status == "approved" and old_status != "approved":
            Payment.objects.get_or_create(
                user=self.request.user,
                tenant=self.object.tenant,
                rent_charge=self.object.rent_charge,
                amount=self.object.amount,
                payment_method=self.object.payment_method,
                payment_reference=self.object.payment_reference,
            )

        if self.object.status != old_status and self.object.status in {"approved", "rejected"}:
            transaction.on_commit(lambda: notify_tenant_payment_request_status(self.object))

        messages.success(self.request, "Payment request status updated successfully!")
        return response


def _payment_request_base_queryset(request):
    """Payment requests that belong to the current user's properties."""
    return PaymentRequest.objects.filter(
        tenant__house__user=request.user
    ).select_related("tenant", "tenant__house", "rent_charge")


@login_required
def approve_payment_request(request, pk):
    """Verify a tenant's payment request, recording it in the payment ledger."""
    payment_request = get_object_or_404(_payment_request_base_queryset(request), pk=pk)
    redirect_url = request.POST.get("next") or request.GET.get("next") or "payment_request_list"

    if payment_request.status == "pending":
        service_approve_payment_request(payment_request, user=request.user)
        messages.success(
            request,
            f"Payment of M{payment_request.amount} by "
            f"{payment_request.tenant.full_name} verified and recorded.",
        )
    else:
        messages.info(request, "This payment request has already been reviewed.")

    return redirect(redirect_url)


@login_required
def reject_payment_request(request, pk):
    """Reject a tenant's payment request."""
    payment_request = get_object_or_404(_payment_request_base_queryset(request), pk=pk)
    redirect_url = request.POST.get("next") or request.GET.get("next") or "payment_request_list"

    if payment_request.status == "pending":
        service_reject_payment_request(payment_request, user=request.user)
        messages.success(
            request,
            f"Payment request from {payment_request.tenant.full_name} rejected.",
        )
    else:
        messages.info(request, "This payment request has already been reviewed.")

    return redirect(redirect_url)


class IssueListViewWeb(LoginRequiredMixin, ListView):
    model = Issue
    template_name = "issues/issue_list.html"
    context_object_name = "issues"

    def get_queryset(self):
        queryset = Issue.objects.filter(
            tenant__house__user=self.request.user
        ).select_related("tenant", "tenant__house", "tenant__house__flat_building")

        status_filter = self.request.GET.get("status")
        if status_filter in dict(Issue.STATUS_CHOICES):
            queryset = queryset.filter(status=status_filter)

        return queryset.order_by("-created_at")


class IssueDetailViewWeb(LoginRequiredMixin, DetailView):
    model = Issue
    template_name = "issues/issue_detail.html"
    context_object_name = "issue"

    def get_queryset(self):
        return Issue.objects.filter(
            tenant__house__user=self.request.user
        ).select_related("tenant", "tenant__house", "tenant__house__flat_building")


class IssueUpdateViewWeb(LoginRequiredMixin, UpdateView):
    model = Issue
    fields = ["status"]
    template_name = "issues/issue_form.html"
    success_url = reverse_lazy("issue_list")

    def get_queryset(self):
        return Issue.objects.filter(tenant__house__user=self.request.user)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        for field in form.fields.values():
            field.widget.attrs.setdefault("class", "form-control")
        return form

    def form_valid(self, form):
        old_status = Issue.objects.get(pk=form.instance.pk).status
        response = super().form_valid(form)

        if old_status != self.object.status:
            transaction.on_commit(lambda: notify_tenant_issue_status(self.object))

        messages.success(self.request, "Maintenance report status updated successfully!")
        return response

# ============================================================================
# PAYMENT VIEWS
# ============================================================================
class PaymentDetailViewWeb(LoginRequiredMixin, DetailView):
    model = Payment
    template_name = 'payments/payment_detail.html'
    context_object_name = 'payment'
    
    def get_queryset(self):
        return Payment.objects.filter(tenant__house__user=self.request.user)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        return context

class PaymentUpdateViewWeb(LoginRequiredMixin, UpdateView):
    model = Payment
    fields = [
        "tenant",
        "rent_charge",
        "amount",
        "payment_reference",
        "payment_method",
    ]
    template_name = 'payments/payment_form.html'
    success_url = reverse_lazy('payment_list')
    
    def get_queryset(self):
        return Payment.objects.filter(tenant__house__user=self.request.user)
    
    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Only show user's tenants
        form.fields['tenant'].queryset = Tenant.objects.filter(house__user=self.request.user)
        form.fields['rent_charge'].queryset = RentCharge.objects.filter(tenant__house__user=self.request.user)
        return form
    
    def form_valid(self, form):
        form.instance.user = self.request.user
        messages.success(self.request, 'Payment updated successfully!')
        return super().form_valid(form)

class PaymentListViewWeb(LoginRequiredMixin, ListView):
    model = Payment
    template_name = 'payments/payment_list.html'
    context_object_name = 'payments'
    
    def get_queryset(self):
        queryset = Payment.objects.filter(tenant__house__user=self.request.user)
        # Optional filter by tenant
        tenant_id = self.request.GET.get('tenant')
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        return queryset.order_by('-paid_at')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['tenants'] = Tenant.objects.filter(house__user=self.request.user)
        return context

class PaymentCreateViewWeb(LoginRequiredMixin, CreateView):
    model = Payment
    fields = [
        "tenant",
        "rent_charge",
        "amount",
        "payment_reference",
        "payment_method",
    ]
    template_name = 'payments/payment_form.html'
    success_url = reverse_lazy('payment_list')
    
    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Only show user's tenants
        form.fields['tenant'].queryset = Tenant.objects.filter(house__user=self.request.user)
        form.fields['rent_charge'].queryset = RentCharge.objects.filter(tenant__house__user=self.request.user)
        return form
    
    def form_valid(self, form):
        form.instance.user = self.request.user
        messages.success(self.request, 'Payment recorded successfully!')
        return super().form_valid(form)
    
class RentChargeCreateViewWeb(LoginRequiredMixin, CreateView):
    model = RentCharge
    fields = ['tenant', 'year', 'month', 'amount_due']
    template_name = 'rentcharges/rentcharge_form.html'
    success_url = reverse_lazy('rent_charge_list')

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields['tenant'].queryset = Tenant.objects.filter(
            house__user=self.request.user
        )
        return form

    def form_valid(self, form):
        form.instance.user = self.request.user
        try:
            return super().form_valid(form)
        except IntegrityError:
            form.add_error(None, "Rent charge already exists for this tenant/month/year.")
            return self.form_invalid(form)
 

class RentChargeDetailViewWeb(LoginRequiredMixin, DetailView):
    model = RentCharge
    template_name = 'rentcharges/rentcharge_detail.html'
    context_object_name = 'rentcharge'
    
    def get_queryset(self):
        return RentCharge.objects.filter(tenant__house__user=self.request.user)
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        return context
    


class RentChargeListViewWeb(LoginRequiredMixin, ListView):
    model = RentCharge
    template_name = 'rentcharges/rent_charge_list.html'
    context_object_name = 'rentcharges'

    # display only rent charges for current user
    def get_queryset(self):
        queryset = RentCharge.objects.filter(tenant__house__user=self.request.user)
        tenant_id = self.request.GET.get("tenant")
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        return queryset.order_by('-year', '-month')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['tenants'] = Tenant.objects.filter(house__user=self.request.user)
        return context


class RentChargeUpdateViewWeb(LoginRequiredMixin, UpdateView):
    model = RentCharge
    fields = ['tenant', 'year', 'month', 'amount_due']
    template_name = 'rentcharges/rentcharge_form.html'
    success_url = reverse_lazy('rent_charge_list')
    
    def get_queryset(self):
        return RentCharge.objects.filter(tenant__house__user=self.request.user)
    
    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Only show user's tenants
        form.fields['tenant'].queryset = Tenant.objects.filter(house__user=self.request.user)
        return form
    
    def form_valid(self, form):
        form.instance.user = self.request.user
        messages.success(self.request, 'Rent charge updated successfully!')
        try:
            return super().form_valid(form)
        except IntegrityError:
            form.add_error(None, "Rent charge already exists for this tenant/month/year.")
            return self.form_invalid(form)


@login_required
def bulk_create_rent_charges(request):
    current_year = timezone.now().year
    current_month = timezone.now().month

    # Filter by current user
    active_tenants = Tenant.objects.filter(
        house__user=request.user, 
        is_active=True,
        house__isnull=False  # Only tenants with houses
    ).select_related("house")

    active_tenants_count = active_tenants.count()

    # Calculate expected total rent safely
    total_rent = sum(
        tenant.house.house_rent_amount
        for tenant in active_tenants
        if tenant.house
    )

    if request.method == "POST":
        month = request.POST.get("month")
        year = request.POST.get("year")
        tenant_ids = request.POST.getlist("tenant_ids")

        if not month or not year:
            messages.error(request, "Please select month and year.")
            return redirect("rent_charge_bulk_create")  #  Fixed: use URL name

        if not tenant_ids:
            messages.warning(request, "No tenants selected.")
            return redirect("rent_charge_bulk_create")  #  Fixed: use URL name

        # Convert to integers
        try:
            month = int(month)
            year = int(year)
            tenant_ids = [int(tid) for tid in tenant_ids]
        except ValueError:
            messages.error(request, "Invalid month, year, or tenant selection.")
            return redirect("rent_charge_bulk_create")  #  Fixed: use URL name

        # Create rent charges
        created_count = 0
        skipped_count = 0
        error_count = 0

        with transaction.atomic():
            for tenant_id in tenant_ids:
                try:
                    tenant = Tenant.objects.get(id=tenant_id, house__user=request.user)
                    
                    # Check if already exists
                    if RentCharge.objects.filter(
                        tenant=tenant,
                        year=year,
                        month=month
                    ).exists():
                        skipped_count += 1
                        continue
                    
                    # Create rent charge
                    RentCharge.objects.create(
                        user=request.user,
                        tenant=tenant,
                        year=year,
                        month=month,
                        amount_due=tenant.house.house_rent_amount
                    )
                    created_count += 1
                    
                except Tenant.DoesNotExist:
                    error_count += 1
                    logger.error(f"Tenant {tenant_id} not found for user {request.user.id}")
                except Exception as e:
                    error_count += 1
                    logger.error(f"Error creating rent charge for tenant {tenant_id}: {str(e)}")

        # Success messages
        month_name = dict(RentCharge.MONTH_CHOICES).get(month, month)
        
        if created_count > 0:
            messages.success(
                request, 
                f" Successfully created {created_count} rent charge(s) for {month_name} {year}"
            )
        if skipped_count > 0:
            messages.info(
                request, 
                f" Skipped {skipped_count} - charges already exist"
            )
        if error_count > 0:
            messages.error(
                request, 
                f" Failed to create {error_count} charge(s)"
            )
        
        return redirect("rent_charge_bulk_create")  #  Fixed: use URL name

    context = {
        "current_year": current_year,
        "current_month": current_month,
        "active_tenants": active_tenants,
        "active_tenants_count": active_tenants_count,
        "total_rent": total_rent,
        "months": RentCharge.MONTH_CHOICES,
        "years": range(current_year - 1, current_year + 2),
    }

    return render(request, "rentcharges/rentcharge_bulk_create.html", context)



@login_required
def send_rent_reminders(request):
    """Manual trigger for sending rent reminders"""
    if request.method == 'POST':
        notification_service = TwilioNotificationService()
        today = timezone.now().date()
        sent_count = 0
        failed_count = 0
        
        # Get all active tenants with SMS enabled
        active_tenants = Tenant.objects.filter(
            house__user=request.user,
            is_active=True, 
            sms_notifications=True
        )
        
        for tenant in active_tenants:
            days_until_due = (tenant.rent_due_date - today).days
            
            # Check if reminder should be sent
            if 0 <= days_until_due <= tenant.reminder_days_before:
                try:
                    rent_charge = RentCharge.objects.get(
                        tenant=tenant,
                        year=today.year,
                        month=today.month
                    )
                    
                    if not rent_charge.reminder_sent:
                        success, result = notification_service.send_rent_due_reminder(rent_charge)
                        if success:
                            sent_count += 1
                        else:
                            failed_count += 1
                            
                except RentCharge.DoesNotExist:
                    pass
        
        messages.success(request, f' Sent {sent_count} reminders. Failed: {failed_count}')
        return redirect('send_rent_reminders')
    
    # GET request - show preview
    today = timezone.now().date()
    tenants_to_remind = []
    
    active_tenants = Tenant.objects.filter(
        house__user=request.user,
        is_active=True, 
        sms_notifications=True
    )
    
    for tenant in active_tenants:
        days_until_due = (tenant.rent_due_date - today).days
        
        if 0 <= days_until_due <= tenant.reminder_days_before:
            try:
                rent_charge = RentCharge.objects.get(
                    tenant=tenant,
                    year=today.year,
                    month=today.month
                )
                
                if not rent_charge.reminder_sent:
                    tenants_to_remind.append({
                        'tenant': tenant,
                        'rent_charge': rent_charge,
                        'days_until_due': days_until_due,
                    })
            except RentCharge.DoesNotExist:
                pass
    
    context = {
        'tenants_to_remind': tenants_to_remind,
        'today': today,
    }

    return render(request, 'payments/send_sms.html', context)


# ============================================================================
# EXPENSE VIEWS
# ============================================================================
from tennants.models import Expense, MaintenanceBid, Worker
from tennants.forms import ExpenseForm
from django.db.models import Sum


@login_required
def expense_list(request):
    """List all expenses for the landlord's properties"""
    expenses = Expense.objects.filter(user=request.user).select_related(
        "flat_building", "house"
    ).order_by("-expense_date", "-created_at")

    building_id = request.GET.get("building")
    if building_id:
        expenses = expenses.filter(flat_building_id=building_id)

    category = request.GET.get("category")
    if category:
        expenses = expenses.filter(category=category)

    total = expenses.aggregate(total=Sum("amount"))["total"] or 0
    buildings = FlatBuilding.objects.filter(user=request.user)

    context = {
        "expenses": expenses,
        "total": total,
        "buildings": buildings,
        "expense_categories": Expense.EXPENSE_CATEGORIES,
    }
    return render(request, "expenses/expense_list.html", context)


@login_required
def expense_create(request):
    """Create a new expense"""
    if request.method == "POST":
        form = ExpenseForm(user=request.user, data=request.POST, files=request.FILES)
        if form.is_valid():
            expense = form.save(commit=False)
            expense.user = request.user
            expense.save()
            messages.success(request, f"Expense of M{expense.amount:,.2f} recorded!")
            return redirect("expense_list")
    else:
        form = ExpenseForm(user=request.user)

    context = {"form": form}
    return render(request, "expenses/expense_form.html", context)


@login_required
def expense_edit(request, pk):
    """Edit an expense"""
    expense = get_object_or_404(Expense, pk=pk, user=request.user)
    if request.method == "POST":
        form = ExpenseForm(user=request.user, data=request.POST, files=request.FILES, instance=expense)
        if form.is_valid():
            form.save()
            messages.success(request, "Expense updated successfully!")
            return redirect("expense_list")
    else:
        form = ExpenseForm(user=request.user, instance=expense)

    context = {"form": form, "expense": expense}
    return render(request, "expenses/expense_form.html", context)


@login_required
def expense_delete(request, pk):
    """Delete an expense"""
    expense = get_object_or_404(Expense, pk=pk, user=request.user)
    if request.method == "POST":
        expense.delete()
        messages.success(request, "Expense deleted.")
        return redirect("expense_list")
    return render(request, "expenses/expense_confirm_delete.html", {"expense": expense})


# ============================================================================
# BID REVIEW VIEWS (Landlord accepts/rejects worker bids)
# ============================================================================

@login_required
def issue_bids(request, issue_id):
    """View all bids on a specific issue"""
    issue = get_object_or_404(Issue, pk=issue_id, tenant__house__user=request.user)
    bids = MaintenanceBid.objects.filter(issue=issue).select_related(
        "worker", "worker__user"
    ).order_by("amount")  # lowest first

    context = {
        "issue": issue,
        "bids": bids,
    }
    return render(request, "expenses/issue_bids.html", context)


@login_required
def accept_bid(request, bid_id):
    """Landlord accepts a bid"""
    bid = get_object_or_404(
        MaintenanceBid, pk=bid_id, issue__tenant__house__user=request.user, status="pending"
    )
    bid.accept()
    messages.success(
        request,
        f"Bid by {bid.worker.full_name} (M{bid.amount:,.2f}) accepted! "
        f"Issue status updated to 'In Progress'."
    )
    return redirect("issue_bids", issue_id=bid.issue_id)


# ============================================================================
# HOUSE IMAGES VIEWS (Property owners manage up to 5 pictures per house)
# ============================================================================

@login_required
def house_images(request, house_pk):
    """View and manage images for a house (up to 5 pictures)."""
    house = get_object_or_404(House, pk=house_pk, user=request.user)
    images = house.images.all().order_by("sort_order", "id")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add":
            if images.count() >= HouseImage.MAX_IMAGES:
                messages.error(request, f"Maximum of {HouseImage.MAX_IMAGES} images allowed.")
                return redirect("house_images", house_pk=house.pk)

            form = HouseImageForm(house=house, data=request.POST, files=request.FILES)
            if form.is_valid():
                house_image = form.save(commit=False)
                house_image.house = house
                house_image.save()
                messages.success(request, "Image uploaded successfully!")
                return redirect("house_images", house_pk=house.pk)
            else:
                for error in form.errors.values():
                    messages.error(request, error.as_text())

        elif action == "delete":
            image_id = request.POST.get("image_id")
            if image_id:
                img = get_object_or_404(HouseImage, pk=image_id, house=house)
                img.delete()
                messages.success(request, "Image deleted.")

        return redirect("house_images", house_pk=house.pk)

    form = HouseImageForm(house=house)
    context = {
        "house": house,
        "images": images,
        "form": form,
        "remaining_slots": HouseImage.MAX_IMAGES - images.count(),
    }
    return render(request, "houses/house_images.html", context)


@login_required
def house_image_delete(request, house_pk, image_pk):
    """Delete a specific house image."""
    house = get_object_or_404(House, pk=house_pk, user=request.user)
    img = get_object_or_404(HouseImage, pk=image_pk, house=house)
    if request.method == "POST":
        img.delete()
        messages.success(request, "Image deleted successfully!")
    return redirect("house_images", house_pk=house.pk)


@login_required
def reject_bid(request, bid_id):
    """Landlord rejects a bid"""
    bid = get_object_or_404(
        MaintenanceBid, pk=bid_id, issue__tenant__house__user=request.user, status="pending"
    )
    bid.reject()
    messages.info(request, f"Bid by {bid.worker.full_name} rejected.")
    return redirect("issue_bids", issue_id=bid.issue_id)
