from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from tennants.models import FlatBuilding, House, Tenant, RentCharge, Payment, PaymentRequest
from tennants.services.payment_requests import approve_payment_request, reject_payment_request


class PropertyIntegrityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="landlord", password="pass12345")
        self.building = FlatBuilding.objects.create(
            user=self.user, building_name="Main", address="1 Main Street", number_of_houses=2
        )
        self.house = House.objects.create(
            user=self.user, flat_building=self.building, house_number="1", house_rent_amount=1000
        )
        self.other_house = House.objects.create(
            user=self.user, flat_building=self.building, house_number="2", house_rent_amount=1100
        )

    def make_tenant(self, name, email, phone, house):
        return Tenant.objects.create(
            user=self.user,
            full_name=name,
            email=email,
            phone=phone,
            id_number=name.replace(" ", "")[:10],
            house=house,
            is_active=True,
        )

    def test_occupied_house_cannot_be_deleted(self):
        self.make_tenant("First Tenant", "first@example.com", "+12025550101", self.house)

        with self.assertRaises(ValidationError):
            self.house.delete()

        self.assertTrue(House.objects.filter(pk=self.house.pk).exists())

    def test_moving_tenant_recalculates_both_houses(self):
        tenant = self.make_tenant("First Tenant", "first@example.com", "+12025550101", self.house)
        self.house.refresh_from_db()
        self.assertTrue(self.house.occupation)

        tenant.house = self.other_house
        tenant.save()

        self.house.refresh_from_db()
        self.other_house.refresh_from_db()
        self.assertFalse(self.house.occupation)
        self.assertTrue(self.other_house.occupation)

    def test_building_capacity_cannot_be_reduced_below_existing_houses(self):
        self.building.number_of_houses = 1
        with self.assertRaises(ValidationError):
            self.building.full_clean()

    def test_house_owner_must_match_building_owner(self):
        other_user = User.objects.create_user(username="other", password="pass12345")
        house = House(
            user=other_user,
            flat_building=self.building,
            house_number="3",
            house_rent_amount=1000,
        )
        with self.assertRaises(ValidationError):
            house.full_clean()


class PaymentIntegrityTests(TestCase):
    def setUp(self):
        self.landlord = User.objects.create_user(username="landlord", password="pass12345")
        building = FlatBuilding.objects.create(
            user=self.landlord, building_name="Main", address="1 Main Street", number_of_houses=2
        )
        house = House.objects.create(
            user=self.landlord, flat_building=building, house_number="1", house_rent_amount=1000
        )
        self.tenant = Tenant.objects.create(
            user=self.landlord,
            full_name="Tenant One",
            email="tenant@example.com",
            phone="+12025550101",
            id_number="TENANT001",
            house=house,
        )
        self.charge = RentCharge.objects.create(
            user=self.landlord, tenant=self.tenant, year=2026, month=1, amount_due=1000
        )

    def test_payment_and_request_amounts_must_be_positive(self):
        with self.assertRaises(ValidationError):
            Payment.objects.create(
                tenant=self.tenant,
                rent_charge=self.charge,
                amount=0,
                payment_method="cash",
            )
        with self.assertRaises(ValidationError):
            PaymentRequest.objects.create(
                tenant=self.tenant,
                rent_charge=self.charge,
                amount=Decimal("-1"),
                payment_method="cash",
            )

    def test_request_requires_matching_charge_and_reference(self):
        other_tenant = Tenant.objects.create(
            user=self.landlord,
            full_name="Tenant Two",
            email="tenant2@example.com",
            phone="+12025550102",
            id_number="TENANT002",
            house=None,
            is_active=False,
        )
        with self.assertRaises(ValidationError):
            PaymentRequest.objects.create(
                tenant=other_tenant,
                rent_charge=self.charge,
                amount=100,
                payment_method="mobile_money",
            )

    def test_review_transitions_only_allow_pending_requests(self):
        request = PaymentRequest.objects.create(
            tenant=self.tenant,
            rent_charge=self.charge,
            amount=1000,
            payment_method="mobile_money",
            payment_reference="TX-1",
        )
        self.assertTrue(reject_payment_request(request, user=self.landlord))
        request.refresh_from_db()
        self.assertFalse(approve_payment_request(request, user=self.landlord))
        self.assertEqual(request.status, "rejected")
        self.assertFalse(Payment.objects.filter(rent_charge=self.charge).exists())
