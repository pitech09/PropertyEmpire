from django.test import TestCase, override_settings
from django.contrib.auth.models import User

from tennants.models import FlatBuilding, House, LandlordProfile, Tenant


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class LandingSearchTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(
            username="landlord", password="pass12345", email="landlord@example.com"
        )
        LandlordProfile.objects.create(user=user, phone="+266 5000 0000")
        building = FlatBuilding.objects.create(
            user=user,
            building_name="Maseru Central Apartments",
            address="12 Kingsway, Maseru",
            number_of_houses=2,
        )
        self.available_house = House.objects.create(
            user=user,
            flat_building=building,
            house_number="A1",
            house_size="2 bedroom",
            house_rent_amount=2500,
        )
        occupied_house = House.objects.create(
            user=user,
            flat_building=building,
            house_number="A2",
            house_size="1 bedroom",
            house_rent_amount=1800,
        )
        Tenant.objects.create(
            user=user,
            full_name="Current Tenant",
            email="current@example.com",
            phone="+12025550101",
            id_number="CURRENT001",
            house=occupied_house,
            is_active=True,
        )

    def test_landing_search_matches_location(self):
        response = self.client.get("/?q=Kingsway")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "House A1")
        self.assertContains(response, "House A2")
        self.assertContains(response, "/rentals/1/")

    def test_landing_search_matches_availability_status(self):
        response = self.client.get("/?q=available")

        self.assertContains(response, "House A1")
        self.assertNotContains(response, "House A2")

    def test_landing_search_has_no_match_message(self):
        response = self.client.get("/?q=nowhere")

        self.assertContains(response, "No rental houses matched your search")

    def test_search_result_provides_phone_and_whatsapp_contact(self):
        response = self.client.get("/?q=Kingsway")

        self.assertContains(response, "+26650000000")
        self.assertContains(response, "tel:+26650000000")
        self.assertContains(response, "https://wa.me/26650000000")
        self.assertContains(response, "hello%2C%20ke%20kopa%20ho%20botsa")

    def test_property_details_are_public_and_include_contact_information(self):
        response = self.client.get(f"/rentals/{self.available_house.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Maseru Central Apartments")
        self.assertContains(response, "2 bedroom")
        self.assertContains(response, "+26650000000")
        self.assertContains(response, "Message on WhatsApp")
