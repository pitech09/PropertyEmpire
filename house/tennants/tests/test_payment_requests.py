from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from decimal import Decimal
from unittest.mock import patch

from tennants.models import FlatBuilding, House, Tenant, RentCharge, Payment, PaymentRequest


class PaymentRequestFlowTests(TestCase):
    """End-to-end checks for the tenant-initiate / landlord-verify payment flow."""

    def setUp(self):
        self.landlord = User.objects.create_user(
            username="landlord", password="pass12345", email="landlord@test.com"
        )
        self.tenant_user = User.objects.create_user(
            username="tenant1", password="pass12345", email="tenant@test.com"
        )

        building = FlatBuilding.objects.create(
            user=self.landlord,
            building_name="Sunrise",
            address="Main Street",
            number_of_houses=2,
        )
        self.house = House.objects.create(
            user=self.landlord,
            flat_building=building,
            house_number="A1",
            house_rent_amount=Decimal("1200.00"),
            occupation=True,
        )
        self.tenant = Tenant.objects.create(
            user=self.tenant_user,
            full_name="Jane Doe",
            email="jane@test.com",
            phone="+12025550123",
            id_number="ID1234",
            house=self.house,
            is_active=True,
        )
        self.charge = RentCharge.objects.create(
            tenant=self.tenant,
            year=2026,
            month=6,
            amount_due=Decimal("1200.00"),
        )

    def _make_pending_request(self):
        return PaymentRequest.objects.create(
            tenant=self.tenant,
            rent_charge=self.charge,
            amount=Decimal("1200.00"),
            payment_method="mobile_money",
            payment_reference="TX-12345",
            status="pending",
        )

    @patch("tennants.services.payment_requests.notify_tenant_payment_request_status")
    def test_approve_records_payment_and_flips_status(self, mock_notify):
        from tennants.services.payment_requests import approve_payment_request

        req = self._make_pending_request()
        created = approve_payment_request(req, user=self.landlord)

        self.assertTrue(created)
        req.refresh_from_db()
        self.assertEqual(req.status, "approved")
        payment = Payment.objects.get(tenant=self.tenant, rent_charge=self.charge)
        self.assertEqual(payment.amount, Decimal("1200.00"))
        self.assertEqual(payment.payment_method, "mobile_money")
        self.assertEqual(payment.payment_reference, "TX-12345")
        mock_notify.assert_called_once_with(req)

        # Idempotent: second call adds no duplicate payment
        self.assertFalse(approve_payment_request(req, user=self.landlord))
        self.assertEqual(
            Payment.objects.filter(tenant=self.tenant, rent_charge=self.charge).count(),
            1,
        )

    @patch("tennants.services.payment_requests.notify_tenant_payment_request_status")
    def test_reject_flips_status_and_creates_no_payment(self, mock_notify):
        from tennants.services.payment_requests import reject_payment_request

        req = self._make_pending_request()
        changed = reject_payment_request(req, user=self.landlord)

        self.assertTrue(changed)
        req.refresh_from_db()
        self.assertEqual(req.status, "rejected")
        self.assertFalse(
            Payment.objects.filter(tenant=self.tenant, rent_charge=self.charge).exists()
        )
        mock_notify.assert_called_once_with(req)

    @patch("tennants.services.payment_requests.notify_tenant_payment_request_status")
    def test_landlord_can_verify_via_view(self, mock_notify):
        req = self._make_pending_request()
        self.client.login(username="landlord", password="pass12345")

        response = self.client.post(
            reverse("payment_request_approve", args=[req.pk]),
            {"next": reverse("dashboard")},
        )

        self.assertEqual(response.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, "approved")
        self.assertTrue(
            Payment.objects.filter(tenant=self.tenant, rent_charge=self.charge).exists()
        )

    def test_landlord_cannot_review_another_landlords_request(self):
        other_landlord = User.objects.create_user(
            username="other", password="pass12345", email="other@test.com"
        )
        req = self._make_pending_request()
        self.client.login(username="other", password="pass12345")

        self.client.post(reverse("payment_request_approve", args=[req.pk]))
        req.refresh_from_db()
        # Other landlord must not be able to approve it
        self.assertEqual(req.status, "pending")

    def test_tenant_can_initiate_payment_request(self):
        self.client.login(username="tenant1", password="pass12345")

        response = self.client.post(
            reverse("initiate_payment", args=[self.charge.pk]),
            {
                "amount": "1200.00",
                "payment_method": "mobile_money",
                "reference": "TX-999",
            },
        )

        self.assertEqual(response.status_code, 302)
        req = PaymentRequest.objects.get(tenant=self.tenant, rent_charge=self.charge)
        self.assertEqual(req.status, "pending")
        self.assertEqual(req.payment_reference, "TX-999")

    def test_tenant_cannot_duplicate_pending_request(self):
        self._make_pending_request()
        self.client.login(username="tenant1", password="pass12345")

        response = self.client.post(
            reverse("initiate_payment", args=[self.charge.pk]),
            {"amount": "600.00", "payment_method": "cash"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            PaymentRequest.objects.filter(
                tenant=self.tenant, rent_charge=self.charge
            ).count(),
            1,
        )