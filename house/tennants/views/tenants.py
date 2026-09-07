from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.http import JsonResponse
from django.db import transaction

from tennants.models import Tenant, Payment, RentCharge, Issue


def health(request):
    return JsonResponse({
        "status": "ok"
    })

from django.shortcuts import get_object_or_404
from django.contrib import messages
from tennants.models import PaymentRequest
from tennants.services.payment_requests import (
    notify_landlord_issue_report,
    notify_landlord_payment_request,
)

@login_required
def tenant_dashboard(request):
    tenant = Tenant.objects.filter(user=request.user).first()
    if tenant is None:
        return HttpResponseForbidden()

    payments = Payment.objects.filter(tenant=tenant).order_by("-paid_at")
    charges = RentCharge.objects.filter(tenant=tenant).order_by("-month")
    issues = Issue.objects.filter(tenant=tenant).order_by("-created_at")
    payment_requests = (
        PaymentRequest.objects.filter(tenant=tenant)
        .select_related("rent_charge")
        .order_by("-created_at")[:10]
    )
    pending_charge_ids = set(
        PaymentRequest.objects.filter(tenant=tenant, status="pending").values_list(
            "rent_charge_id", flat=True
        )
    )
    next_unpaid_charge = next(
        (
            c
            for c in charges
            if not c.is_paid and c.id not in pending_charge_ids
        ),
        None,
    )

    total_paid = sum(p.amount for p in payments)
    total_due = sum(c.amount_due for c in charges)

    context = {
        "tenant": tenant,
        "payments": payments,
        "charges": charges,
        "issues": issues,
        "payment_requests": payment_requests,
        "pending_charge_ids": pending_charge_ids,
        "next_unpaid_charge": next_unpaid_charge,
        "balance": total_due - total_paid,
    }

    return render(request, "tenantviews/dashboard.html", context)


@login_required
def initiate_payment(request, charge_id):
    tenant = Tenant.objects.filter(user=request.user).first()
    if tenant is None:
        return HttpResponseForbidden()
    
    charge = get_object_or_404(RentCharge, id=charge_id, tenant=tenant)
    
    if PaymentRequest.objects.filter(
        tenant=tenant, rent_charge=charge, status="pending"
    ).exists():
        messages.info(
            request,
            "You already have a payment awaiting landlord verification for this charge.",
        )
        return redirect("tenant_dashboard")


    if request.method == "POST":
        amount = request.POST.get("amount")
        payment_method = request.POST.get('payment_method')
        reference = request.POST.get('reference')

        if not amount or not payment_method:
            messages.error(request, "Amount and Payment Method are required.")
            return redirect("initiate_payment", charge_id=charge.id)

        if payment_method != "cash" and not reference:
            messages.error(request, "Reference is required for non-cash payments.")
            return redirect("initiate_payment", charge_id=charge.id)
        
        payment_request = PaymentRequest.objects.create(
            tenant=tenant,
            rent_charge=charge,
            amount=amount,
            payment_method=payment_method,
            payment_reference=reference,
            status='pending'
        )
        transaction.on_commit(lambda: notify_landlord_payment_request(payment_request))
        
        messages.success(request, "Payment request submitted successfully. Waiting for landlord approval.")
        return redirect("tenant_dashboard")

    return render(request, "tenantviews/initiate_payment.html", {"charge": charge, "tenant": tenant})


@login_required
def report_issue(request):
    tenant = Tenant.objects.filter(user=request.user).first()
    if tenant is None:
        return HttpResponseForbidden()

    if request.method == "POST":
        title = request.POST.get("title")
        description = request.POST.get("description")

        issue = Issue.objects.create(
            tenant=tenant,
            title=title,
            description=description
        )
        transaction.on_commit(lambda: notify_landlord_issue_report(issue))

        return redirect("tenant_dashboard")

    return render(request, "tenantviews/report_issue.html", {"tenant": tenant})
