import logging

from django.conf import settings
from django.core.mail import send_mail

from tennants.models import Payment

from tennants.services.sms import TwilioNotificationService

logger = logging.getLogger(__name__)


def _send_email_backup(subject, message, recipients):
    recipients = [recipient for recipient in recipients if recipient]
    if not recipients:
        logger.info("No email recipients for notification: %s", subject)
        return False

    try:
        send_mail(
            subject,
            message,
            settings.DEFAULT_FROM_EMAIL,
            recipients,
            fail_silently=False,
        )
        return True
    except Exception as exc:
        logger.error("Failed to send email backup for %s: %s", subject, exc)
        return False


def _landlord_for_tenant(tenant):
    return tenant.house.user if tenant.house else None


def _landlord_sms_number(landlord):
    profile = getattr(landlord, "landlord_profile", None)
    if profile and profile.sms_notifications and profile.phone:
        return profile.phone
    return getattr(settings, "LANDLORD_SMS_NUMBER", None)


def _payment_request_message(payment_request):
    tenant = payment_request.tenant
    charge = payment_request.rent_charge
    house = tenant.house
    house_label = house.house_number if house else "Unassigned"
    building = tenant.building_name or "Unknown building"

    return (
        f"Payment request: {tenant.full_name}, M{payment_request.amount}, "
        f"{charge.get_month_display()} {charge.year}, house {house_label}, "
        f"{building}. Ref: {payment_request.payment_reference or '-'}."
    )


def notify_landlord_payment_request(payment_request):
    landlord = _landlord_for_tenant(payment_request.tenant)
    if landlord is None:
        logger.warning("Payment request %s has no landlord to notify", payment_request.pk)
        return

    subject = f"New payment request from {payment_request.tenant.full_name}"
    message = _payment_request_message(payment_request)

    sms_number = _landlord_sms_number(landlord)
    if sms_number:
        TwilioNotificationService().send_sms(sms_number, message)
    else:
        logger.info("No landlord SMS number configured for user %s", landlord.pk)

    whatsapp_number = getattr(settings, "LANDLORD_WHATSAPP_NUMBER", None)
    if whatsapp_number:
        TwilioNotificationService().send_whatsapp(whatsapp_number, message)
    else:
        logger.info("LANDLORD_WHATSAPP_NUMBER is not configured")

    _send_email_backup(subject, message, [landlord.email])


def notify_tenant_payment_request_status(payment_request):
    tenant = payment_request.tenant
    status_label = payment_request.get_status_display().lower()
    subject = f"Payment request {status_label}"

    message = (
        f"Payment request {status_label}: M{payment_request.amount} for "
        f"{payment_request.rent_charge.get_month_display()} {payment_request.rent_charge.year} rent."
    )
    if payment_request.status == "approved":
        message += " Your payment has been recorded."
    elif payment_request.status == "rejected":
        message += " Please contact your landlord."

    if tenant.phone and tenant.sms_notifications:
        TwilioNotificationService().send_sms(tenant.phone, message)
    else:
        logger.info("Tenant %s has no enabled SMS number for payment request status", tenant.pk)

    _send_email_backup(subject, message, [tenant.email])


def _issue_report_message(issue):
    tenant = issue.tenant
    house = tenant.house
    house_label = house.house_number if house else "Unassigned"
    building = tenant.building_name or "Unknown building"

    return (
        f"Maintenance request: {tenant.full_name}, house {house_label}, "
        f"{building}. Issue: {issue.title}. {issue.description}"
    )


def notify_landlord_issue_report(issue):
    landlord = _landlord_for_tenant(issue.tenant)
    if landlord is None:
        logger.warning("Issue %s has no landlord to notify", issue.pk)
        return

    subject = f"New maintenance request from {issue.tenant.full_name}"
    message = _issue_report_message(issue)

    sms_number = _landlord_sms_number(landlord)
    if sms_number:
        TwilioNotificationService().send_sms(sms_number, message)
    else:
        logger.info("No landlord SMS number configured for user %s", landlord.pk)

    whatsapp_number = getattr(settings, "LANDLORD_WHATSAPP_NUMBER", None)
    if whatsapp_number:
        TwilioNotificationService().send_whatsapp(whatsapp_number, message)
    else:
        logger.info("LANDLORD_WHATSAPP_NUMBER is not configured")

    _send_email_backup(subject, message, [landlord.email])


def notify_tenant_issue_approved(issue):
    tenant = issue.tenant
    subject = f"Maintenance request approved: {issue.title}"
    message = (
        f"Hi {tenant.full_name}, your maintenance request '{issue.title}' "
        "was approved. Help will be sent within a week."
    )

    if tenant.phone:
        TwilioNotificationService().send_sms(tenant.phone, message)
    else:
        logger.info("Tenant %s has no phone for maintenance approval SMS", tenant.pk)

    _send_email_backup(subject, message, [tenant.email])


def notify_tenant_issue_status(issue):
    tenant = issue.tenant
    status_label = issue.get_status_display().lower()
    subject = f"Maintenance request {status_label}: {issue.title}"
    message = f"Maintenance update: '{issue.title}' is now {status_label}."

    if issue.status == "approved":
        message += " Help will be sent within a week."
    elif issue.status == "in_progress":
        message += " Work has started."
    elif issue.status == "resolved":
        message += " Please contact your landlord if the issue remains."

    if tenant.phone and tenant.sms_notifications:
        TwilioNotificationService().send_sms(tenant.phone, message)
    else:
        logger.info("Tenant %s has no enabled SMS number for issue status", tenant.pk)

    _send_email_backup(subject, message, [tenant.email])
def approve_payment_request(payment_request, user=None):
    """Verify/approve a pending payment request.

    Records the payment in the ledger (a :class:`Payment`), flips the request
    status to "approved", and notifies the tenant.  Idempotent: already-approved
    requests are left untouched and ``False`` is returned.
    """
    if payment_request.status == "approved":
        return False

    Payment.objects.get_or_create(
        user=user,
        tenant=payment_request.tenant,
        rent_charge=payment_request.rent_charge,
        amount=payment_request.amount,
        payment_method=payment_request.payment_method,
        payment_reference=payment_request.payment_reference,
    )

    payment_request.status = "approved"
    payment_request.save(update_fields=["status"])
    notify_tenant_payment_request_status(payment_request)
    return True


def reject_payment_request(payment_request, user=None):
    """Reject a pending payment request and notify the tenant."""
    if payment_request.status == "rejected":
        return False

    payment_request.status = "rejected"
    payment_request.save(update_fields=["status"])
    notify_tenant_payment_request_status(payment_request)
    return True
