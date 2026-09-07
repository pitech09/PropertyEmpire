from django.test import TestCase
from django.contrib.auth.models import User
from datetime import date
from unittest.mock import patch

from tennants.models import FlatBuilding, House, Tenant, RentCharge
from tennants.services.rent_charges import (
    generate_due_rent_charges,
    generate_rent_charges_for_tenant,
    add_months,
)


class RentChargeGenerationTests(TestCase):
    """Auto-creation of RentCharges when a tenant's rent due date arrives."""

    # The RentCharge post_save signal sends real SMS reminders; disable it.
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._sms_patcher = patch.multiple(
            "tennants.services.sms.TwilioNotificationService",
            send_sms=lambda self, *a, **k: (False, "disabled in tests"),
            send_whatsapp=lambda self, *a, **k: (False, "disabled in tests"),
            send_rent_due_reminder=lambda self, *a, **k: (False, "disabled in tests"),
        )
        cls._sms_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._sms_patcher.stop()
        super().tearDownClass()

    def setUp(self):
        self.landlord = User.objects.create_user(
            username="landlord", password="pass12345"
        )
        building = FlatBuilding.objects.create(
            user=self.landlord,
            building_name="Sunrise",
            address="Main St",
            number_of_houses=2,
        )
        self.house = House.objects.create(
            user=self.landlord,
            flat_building=building,
            house_number="A1",
            house_rent_amount=1000,
            occupation=True,
        )
        self.tenant = Tenant.objects.create(
            full_name="Jane Doe",
            email="jane@test.com",
            phone="+12025550123",
            id_number="ID1234",
            house=self.house,
            is_active=True,
            rent_due_date=date(2026, 6, 1),
        )

    def test_creates_charge_when_due_date_arrives(self):
        created = generate_due_rent_charges(today=date(2026, 6, 1))

        self.assertEqual(len(created), 1)
        charge = created[0]
        self.assertEqual(charge.tenant, self.tenant)
        self.assertEqual((charge.year, charge.month), (2026, 6))
        self.assertEqual(charge.amount_due, 1000)
        self.assertEqual(charge.user, self.landlord)
        # Due date rolled forward
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.rent_due_date, date(2026, 7, 1))

    def test_no_charge_created_before_due_date(self):
        created = generate_due_rent_charges(today=date(2026, 5, 15))
        self.assertEqual(len(created), 0)
        self.assertFalse(RentCharge.objects.exists())

    def test_idempotent_on_second_run(self):
        generate_due_rent_charges(today=date(2026, 6, 1))
        created_again = generate_due_rent_charges(today=date(2026, 6, 15))

        self.assertEqual(len(created_again), 0)
        self.assertEqual(RentCharge.objects.filter(tenant=self.tenant).count(), 1)

    def test_catches_up_multiple_missed_months(self):
        created = generate_due_rent_charges(today=date(2026, 8, 1))

        self.assertEqual(len(created), 3)  # Jun, Jul, Aug
        months = sorted(RentCharge.objects.values_list("year", "month"))
        self.assertEqual(months, [(2026, 6), (2026, 7), (2026, 8)])
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.rent_due_date, date(2026, 9, 1))

    def test_skips_inactive_tenants_and_tenants_without_house(self):
        Tenant.objects.create(
            full_name="No House",
            email="nohouse@test.com",
            phone="+12025550124",
            id_number="ID9999",
            house=None,
            is_active=True,
            rent_due_date=date(2026, 6, 1),
        )
        self.tenant.is_active = False
        self.tenant.save()

        created = generate_due_rent_charges(today=date(2026, 6, 1))
        self.assertEqual(len(created), 0)

    def test_add_months_clamps_short_months(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2026, 3, 15), 1), date(2026, 4, 15))

    def test_single_tenant_helper_advances_repeatedly(self):
        generate_rent_charges_for_tenant(self.tenant, date(2026, 7, 10))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.rent_due_date, date(2026, 8, 1))
        self.assertEqual(RentCharge.objects.filter(tenant=self.tenant).count(), 2)
