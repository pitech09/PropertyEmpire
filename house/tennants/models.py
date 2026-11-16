from django.db import models, transaction
from django.contrib.auth.models import User
from datetime import datetime
from django.utils import timezone
from phonenumber_field.modelfields import PhoneNumberField
from django.core.exceptions import ValidationError
from django.db.models import Sum
import logging

logger = logging.getLogger(__name__)

# ------------------------------
# FlatBuilding Model
# ------------------------------
class FlatBuilding(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, blank=True, null=True)
    building_name = models.CharField(max_length=50)
    address = models.CharField(max_length=50)
    number_of_houses = models.IntegerField(default=0, db_index=True)

    @property
    def how_many_occupied(self):
        return self.get_occupied_count()

    @property
    def vacant_houses(self):
        return self.get_vacant_count()

    
    def tenant_count(self):
        return self.houses.filter(tenants__is_active=True).distinct().count()

    def clean(self):
        if self.number_of_houses < 0:
            raise ValidationError("Number of houses must be non-negative")
        if not self.building_name:
            raise ValidationError("Building name is required")
        if self.pk and self.number_of_houses < self.houses.count():
            raise ValidationError(
                "Number of houses cannot be lower than the houses already in this building."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.houses.filter(models.Q(occupation=True) | models.Q(tenants__is_active=True)).exists():
            raise ValidationError("Cannot delete a flat building with occupied houses.")
        super().delete(*args, **kwargs)

    def get_occupied_count(self):
        occupied = self.houses.filter(occupation=True).count()
        return occupied

    def get_vacant_count(self):
        return self.number_of_houses - self.get_occupied_count()

    def __str__(self):
        return self.building_name


# ------------------------------
# House Model
# ------------------------------
class House(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, blank=True, null=True, db_index=True)
    flat_building = models.ForeignKey(FlatBuilding, related_name='houses', on_delete=models.CASCADE, db_index=True)
    house_number = models.CharField(max_length=5, db_index=True)
    house_size = models.CharField(max_length=100, default='1 bedroom')
    house_rent_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, db_index=True)
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, db_index=True)
    occupation = models.BooleanField(default=False)
    image = models.ImageField(upload_to="houses/", blank=True, null=True, help_text="House photo for marketplace and records")
    #Tenant = models.ForeignKey(Tenant, related_name='tenants', on_delete=models.CASCADE, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['flat_building', 'house_number'], name='unique_house_per_building')
        ]
    
    def auto_change_occupation(self):   
        is_occupied = self.tenants.filter(is_active=True).exists()
        if self.occupation != is_occupied:
            self.occupation = is_occupied
            self.save()

    def clean(self):
        if not self.flat_building_id:
            raise ValidationError("House must be associated with a flat building")
        if self.house_rent_amount < 0 or self.deposit_amount < 0:
            raise ValidationError("Rent and deposit must be non-negative")
        if (
            self.user_id is not None
            and self.flat_building.user_id is not None
            and self.user_id != self.flat_building.user_id
        ):
            raise ValidationError("House owner must match the building owner")
        if self.flat_building.houses.exclude(pk=self.pk).filter(house_number=self.house_number).exists():
            raise ValidationError(f"House number {self.house_number} already exists in {self.flat_building.building_name}")
        # ensure number of houses in building is not exceeded
        if self._state.adding:  # only on create
            current_house_count = self.flat_building.houses.count()
            if current_house_count >= self.flat_building.number_of_houses:
                raise ValidationError(f"Cannot add more houses to {self.flat_building.building_name}. Limit reached.")
        

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.occupation or self.tenants.filter(is_active=True).exists():
            raise ValidationError("Cannot delete an occupied house.")
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.flat_building.building_name} - House {self.house_number}"


# ------------------------------
# HouseImage Model (up to 5 pictures per house)
# ------------------------------
class HouseImage(models.Model):
    MAX_IMAGES = 3
    ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
    MAX_FILE_SIZE_MB = 5

    house = models.ForeignKey(House, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="houses/gallery/")
    caption = models.CharField(max_length=160, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("sort_order", "id")

    def clean(self):
        if self.house_id and self.house.images.exclude(pk=self.pk).count() >= self.MAX_IMAGES:
            raise ValidationError(f"A house may have at most {self.MAX_IMAGES} images.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.caption or f"Image for {self.house}"


# ------------------------------
# Tenant Model
# ------------------------------
class Tenant(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, blank=True, null=True, db_index=True)
    full_name = models.CharField(max_length=50, db_index=True)
    email = models.EmailField(unique=True, db_index=True)
    phone = PhoneNumberField(unique=True, db_index=True)
    id_number = models.CharField(max_length=10, null=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    rent_due_date = models.DateField(default=datetime.now)
    house = models.ForeignKey(House, on_delete=models.CASCADE, related_name='tenants', blank=True, null=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    sms_notifications = models.BooleanField(default=True, db_index=True)
    last_notification_sent = models.DateTimeField(blank=True, null=True)
    reminder_days_before = models.IntegerField(default=3, db_index=True)
    last_reminder_sent = models.DateTimeField(blank=True, null=True)
    

    @property
    def building_name(self):
        return self.house.flat_building.building_name if self.house and self.house.flat_building else None

    @property
    def building_address(self):
        return self.house.flat_building.address if self.house and self.house.flat_building else None

    @property
    def rent(self):
        return self.house.house_rent_amount if self.house else 0

    @property
    def security_deposit(self):
        return self.house.deposit_amount if self.house else 0

    @property
    def balance(self):
        total_due = self.rent_charges.aggregate(total=Sum('amount_due'))['total'] or 0
        total_paid = self.payments.aggregate(total=Sum('amount'))['total'] or 0
        return total_due - total_paid

    def clean(self):
        if self.house and self.house.tenants.exclude(pk=self.pk).filter(is_active=True).exists():
            raise ValidationError(f"House {self.house} is already occupied by another tenant.")
        if not self.full_name:
            raise ValidationError("Tenant full name is required")
        if not self.phone:
            raise ValidationError("Phone number is required")
        if self.id_number and Tenant.objects.exclude(pk=self.pk).filter(id_number=self.id_number).exists():
            raise ValidationError(f"ID number {self.id_number} is already in use")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.full_name


class LandlordProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="landlord_profile")
    phone = PhoneNumberField(unique=True, db_index=True)
    sms_notifications = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} - {self.phone}"


class PasswordResetCode(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sms_password_reset_codes")
    code = models.CharField(max_length=6, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def is_valid(self):
        return self.used_at is None and self.expires_at >= timezone.now()

    def mark_used(self):
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])


class SMSRetryMessage(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("sent", "Sent"),
        ("failed", "Failed"),
    ]

    to_number = models.CharField(max_length=128, db_index=True)
    message = models.TextField()
    provider = models.CharField(max_length=32, default="textbee")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    next_attempt_at = models.DateTimeField(db_index=True)
    last_error = models.TextField(blank=True)
    external_id = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("next_attempt_at", "created_at")

    def __str__(self):
        return f"{self.to_number} - {self.status} ({self.attempts}/{self.max_attempts})"

# ------------------------------
# RentCharge Model (Obligation)
# ------------------------------
class RentCharge(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, blank=True, null=True, db_index=True)
    MONTH_CHOICES = [
        (1, 'January'), (2, 'February'), (3, 'March'), (4, 'April'),
        (5, 'May'), (6, 'June'), (7, 'July'), (8, 'August'),
        (9, 'September'), (10, 'October'), (11, 'November'), (12, 'December')
    ]
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="rent_charges", db_index=True)
    year = models.IntegerField()
    month = models.IntegerField(choices=MONTH_CHOICES)
    amount_due = models.DecimalField(max_digits=10, decimal_places=2)
    reminder_sent = models.BooleanField(default=False)
   

    class Meta:
        unique_together = ("tenant", "year", "month")

    def save(self, *args, **kwargs):
        # auto-set amount_due from tenant's house
        if self.amount_due is None and self.tenant and self.tenant.house:
            self.amount_due = self.tenant.house.house_rent_amount
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self):
        if self.amount_due is None or self.amount_due <= 0:
            raise ValidationError({"amount_due": "Amount due must be greater than zero."})
        if self.tenant_id and not self.tenant.house_id:
            raise ValidationError({"tenant": "A rent charge requires a tenant assigned to a house."})

    @property
    def total_paid(self):
        return self.payments.aggregate(total=Sum("amount"))["total"] or 0

    @property
    def balance(self):
        return self.amount_due - self.total_paid

    @property
    def is_paid(self):
        return self.balance <= 0

    def __str__(self):
        return f"{self.tenant.full_name} - {self.get_month_display()} {self.year}"


# ------------------------------
# Payment Model (Ledger)
# ------------------------------
class Payment(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, blank=True, null=True, db_index=True)
    PAYMENT_METHODS = [
        ('cash', 'Cash'),
        ('mobile_money', 'Mobile Money'),
        ('bank_transfer', 'Bank Transfer'),
        ('cheque', 'Cheque'),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="payments")
    rent_charge = models.ForeignKey(RentCharge, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2, db_index=True)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS)
    payment_reference = models.TextField(blank=True, null=True)
    paid_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def clean(self):
        super().clean()
        if self.amount is None or self.amount <= 0:
            raise ValidationError({"amount": "Payment amount must be greater than zero."})
        if self.payment_method != "cash" and not self.payment_reference:
            raise ValidationError({
                "payment_reference": "Reference required for non-cash payments"
            })
        
        if self.rent_charge and self.tenant != self.rent_charge.tenant:
            raise ValidationError("Payment tenant must match rent charge tenant")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.amount} paid by {self.tenant.full_name} via {self.payment_method} on {self.paid_at.date()}"
    
class Issue(models.Model):

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("in_progress", "In Progress"),
        ("resolved", "Resolved"),
    ]

    tenant = models.ForeignKey("Tenant", on_delete=models.CASCADE)
    title = models.CharField(max_length=255)
    description = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} - {self.status}"
    
class PaymentRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    PAYMENT_METHODS = [
        ('cash', 'Cash'),
        ('mobile_money', 'Mobile Money'),
        ('bank_transfer', 'Bank Transfer'),
        ('cheque', 'Cheque'),
    ]
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    rent_charge = models.ForeignKey(RentCharge, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS)
    payment_reference = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.amount is None or self.amount <= 0:
            raise ValidationError({"amount": "Payment amount must be greater than zero."})
        if self.tenant_id and self.rent_charge_id:
            if self.rent_charge.tenant_id != self.tenant_id:
                raise ValidationError(
                    {"rent_charge": "Rent charge must belong to the selected tenant."}
                )
        if self.payment_method != "cash" and not self.payment_reference:
            raise ValidationError(
                {"payment_reference": "Reference required for non-cash payments."}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)



# ------------------------------
# Worker Model (Independent Contractor)
# ------------------------------
class Worker(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="worker_profile", null=True, blank=True)
    full_name = models.CharField(max_length=100, db_index=True)
    email = models.EmailField(unique=True, db_index=True)
    phone = PhoneNumberField(unique=True, db_index=True)
    id_number = models.CharField(max_length=20, null=True, blank=True)
    skills = models.CharField(
        max_length=255,
        help_text="Comma-separated skills e.g. plumbing, electrical, carpentry",
    )
    bio = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    is_approved = models.BooleanField(default=False, db_index=True, help_text="Approved by platform admin")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.full_name} ({self.skills})"

    @property
    def skill_list(self):
        return [s.strip() for s in (self.skills or "").split(",") if s.strip()]


# ------------------------------
# Expense Model (Building-level, House-level, or Tenant-specific)
# ------------------------------
class Expense(models.Model):
    EXPENSE_CATEGORIES = [
        ("utilities", "Utilities (Water/Electricity)"),
        ("maintenance", "Maintenance & Repairs"),
        ("taxes", "Property Taxes & Levies"),
        ("insurance", "Insurance"),
        ("cleaning", "Cleaning & Sanitation"),
        ("security", "Security"),
        ("management", "Management Fees"),
        ("advertising", "Advertising & Marketing"),
        ("supplies", "Supplies"),
        ("other", "Other"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="expenses", db_index=True)
    flat_building = models.ForeignKey(
        FlatBuilding,
        on_delete=models.CASCADE,
        related_name="expenses",
        null=True,
        blank=True,
        db_index=True,
        help_text="Entire building expense (e.g. water bill, security). Optional if house or tenant is set.",
    )
    house = models.ForeignKey(
        House,
        on_delete=models.CASCADE,
        related_name="expenses",
        null=True,
        blank=True,
        db_index=True,
        help_text="House-specific expense (e.g. individual electricity meter).",
    )
    tenant = models.ForeignKey(
        "Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses",
        db_index=True,
        help_text="Tenant-specific expense charged to this tenant (recoverable).",
    )
    category = models.CharField(max_length=20, choices=EXPENSE_CATEGORIES, default="maintenance", db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, db_index=True)
    description = models.CharField(max_length=255)
    vendor = models.CharField(max_length=100, blank=True, help_text="Who was paid (worker name, utility co., etc.)")
    expense_date = models.DateField(default=datetime.now, db_index=True)
    receipt = models.FileField(upload_to="expense_receipts/", null=True, blank=True)
    is_recoverable = models.BooleanField(default=False, db_index=True,
        help_text="If checked, this expense will be charged to the tenant (added to their balance).")
    is_recovered = models.BooleanField(default=False,
        help_text="Has the tenant reimbursed this expense?")
    recovered_date = models.DateField(null=True, blank=True,
        help_text="When the tenant reimbursed this expense.")
    related_issue = models.ForeignKey(
        "Issue",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses",
        help_text="Optional - maintenance issue this expense resolved",
    )
    related_bid = models.ForeignKey(
        "MaintenanceBid",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses",
        help_text="Optional - worker bid this expense settled",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-expense_date", "-created_at")
        indexes = [
            models.Index(fields=["user", "expense_date"]),
            models.Index(fields=["flat_building", "expense_date"]),
        ]

    def clean(self):
        if self.amount is None or self.amount < 0:
            raise ValidationError({"amount": "Amount must be non-negative."})
        if not self.description:
            raise ValidationError({"description": "Description is required."})
        if self.house and self.flat_building and self.house.flat_building_id != self.flat_building.id:
            raise ValidationError({"house": "Selected house does not belong to the chosen building."})
        if self.house and not self.flat_building:
            self.flat_building = self.house.flat_building
        if self.tenant and self.house and self.tenant.house_id != self.house.id:
            raise ValidationError({"tenant": "Selected tenant does not belong to the chosen house."})
        if self.is_recoverable and not self.tenant:
            raise ValidationError("A tenant must be selected for recoverable expenses.")

    def save(self, *args, **kwargs):
        if self.tenant and not self.flat_building:
            self.flat_building = self.tenant.house.flat_building if self.tenant.house else None
        self.full_clean()
        super().save(*args, **kwargs)

    def mark_recovered(self, date=None):
        from django.utils import timezone
        self.is_recovered = True
        self.recovered_date = date or timezone.now().date()
        self.save(update_fields=["is_recovered", "recovered_date"])

    def __str__(self):
        parts = []
        if self.flat_building:
            parts.append(f"Building: {self.flat_building.building_name}")
        if self.house:
            parts.append(f"House: {self.house.house_number}")
        if self.tenant:
            parts.append(f"Tenant: {self.tenant.full_name}")
        target = ", ".join(parts) if parts else "General"
        rec = " (Recoverable)" if self.is_recoverable else ""
        paid = " (Recovered)" if self.is_recovered else ""
        return f"{self.get_category_display()} - M{self.amount} - {target}{rec}{paid}"


# ------------------------------
# MaintenanceBid Model (Worker's Bid on an Issue)
# ------------------------------
class MaintenanceBid(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("accepted", "Accepted"),
        ("rejected", "Rejected"),
        ("withdrawn", "Withdrawn"),
        ("completed", "Completed"),
    ]

    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="bids", db_index=True)
    worker = models.ForeignKey(Worker, on_delete=models.CASCADE, related_name="bids", db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, db_index=True)
    estimated_days = models.PositiveIntegerField(help_text="Estimated days to complete the work")
    notes = models.TextField(blank=True, help_text="Approach, materials, or any clarifications for the landlord")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", db_index=True)
    submitted_at = models.DateTimeField(auto_now_add=True, db_index=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-submitted_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["issue"],
                condition=models.Q(status="accepted"),
                name="unique_accepted_bid_per_issue",
            ),
        ]

    def clean(self):
        if self.amount is None or self.amount < 0:
            raise ValidationError({"amount": "Bid amount must be non-negative."})
        if self.estimated_days is None or self.estimated_days < 1:
            raise ValidationError({"estimated_days": "Estimated days must be at least 1."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def accept(self):
        """Accept this bid and reject siblings."""
        from django.utils import timezone
        with transaction.atomic():
            self.status = "accepted"
            self.decided_at = timezone.now()
            self.save(update_fields=["status", "decided_at"])
            MaintenanceBid.objects.filter(issue=self.issue).exclude(pk=self.pk).update(
                status="rejected", decided_at=timezone.now()
            )
            self.issue.status = "in_progress"
            self.issue.save(update_fields=["status"])

    def reject(self):
        from django.utils import timezone
        self.status = "rejected"
        self.decided_at = timezone.now()
        self.save(update_fields=["status", "decided_at"])

    def __str__(self):
        return f"Bid M{self.amount} by {self.worker.full_name} on '{self.issue.title}' ({self.status})"
