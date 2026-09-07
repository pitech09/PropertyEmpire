from django.core.management.base import BaseCommand

from tennants.services.rent_charges import generate_due_rent_charges


class Command(BaseCommand):
    help = (
        "Create RentCharges automatically for every active tenant whose rent "
        "due date has arrived. Idempotent - safe to run daily via cron."
    )

    def handle(self, *args, **options):
        created = generate_due_rent_charges()
        self.stdout.write(
            self.style.SUCCESS(f"Created {len(created)} rent charge(s).")
        )
        for charge in created:
            self.stdout.write(
                f"  - {charge.tenant.full_name}: {charge.get_month_display()} "
                f"{charge.year} (M{charge.amount_due})"
            )
