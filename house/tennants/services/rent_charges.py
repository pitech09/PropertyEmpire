"""
Automatic rent charge generation.

Generates :class:`~tennants.models.RentCharge` records for active tenants
when their ``rent_due_date`` arrives.  Generation is idempotent:
``RentCharge`` has a unique constraint on (tenant, year, month), so a
charge is only created once per month, and the tenant's ``rent_due_date``
is rolled forward until it lies in the future.
"""
import logging
from datetime import date

from django.db import transaction

from tennants.models import RentCharge, Tenant

logger = logging.getLogger(__name__)

# Safety cap: never chase more than 3 years of missed months per tenant.
MAX_CATCH_UP_MONTHS = 36


def add_months(d, months):
    """Return the same day-of-month shifted by ``months`` (clamped for short months)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    # Clamp day (e.g. Jan 31 -> Feb 28/29)
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def generate_rent_charges_for_tenant(tenant, today):
    """Create due rent charges for a single tenant.

    Returns the list of newly created RentCharge objects.
    """
    created = []
    for _ in range(MAX_CATCH_UP_MONTHS):
        due = tenant.rent_due_date
        if due > today:
            break

        with transaction.atomic():
            charge, was_created = RentCharge.objects.get_or_create(
                tenant=tenant,
                year=due.year,
                month=due.month,
                defaults={
                    "amount_due": tenant.rent or 0,
                    "user": tenant.house.user if tenant.house else None,
                },
            )
        if was_created:
            created.append(charge)
            logger.info(
                "Auto-created rent charge for %s (%s %s, M%s)",
                tenant.full_name, charge.get_month_display(), charge.year, charge.amount_due,
            )

        # Roll the due date forward so this month is not processed again.
        tenant.rent_due_date = add_months(due, 1)
        tenant.save(update_fields=["rent_due_date"])

    return created


def generate_due_rent_charges(today=None):
    """Create rent charges for every active tenant whose due date has arrived.

    Returns the list of all newly created RentCharge objects.
    """
    if today is None:
        today = date.today()

    created = []
    tenants = Tenant.objects.filter(is_active=True, house__isnull=False)
    for tenant in tenants:
        try:
            created.extend(generate_rent_charges_for_tenant(tenant, today))
        except Exception:
            logger.exception("Failed to generate rent charges for tenant %s", tenant.pk)
    return created
