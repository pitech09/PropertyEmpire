from django import forms
from .models import House, HouseImage, Payment, Tenant, FlatBuilding, LandlordProfile, RentCharge, PaymentRequest, Worker, Expense, MaintenanceBid
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from phonenumber_field.formfields import PhoneNumberField


class RegistrationForm(UserCreationForm):
    first_name = forms.CharField(required=True, max_length=30)
    last_name = forms.CharField(required=True, max_length=30)
    email = forms.EmailField(required=True)
    phone = PhoneNumberField(required=True)

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "phone", "password1", "password2"]

    def clean_phone(self):
        phone = self.cleaned_data["phone"]
        if LandlordProfile.objects.filter(phone=phone).exists():
            raise forms.ValidationError("A landlord with this phone number already exists.")
        return phone


class SMSPasswordResetRequestForm(forms.Form):
    identifier = forms.CharField(
        label="Username, email, or phone",
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Username, email, or phone"}),
    )


class SMSPasswordResetConfirmForm(forms.Form):
    code = forms.CharField(
        label="Reset code",
        min_length=6,
        max_length=6,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "6-digit code"}),
    )
    new_password = forms.CharField(
        label="New password",
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "New password"}),
    )
    confirm_password = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Confirm password"}),
    )

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("new_password") != cleaned_data.get("confirm_password"):
            raise forms.ValidationError("Passwords do not match.")
        return cleaned_data


class HouseForm(forms.ModelForm):
    class Meta:
        model = House
        fields = '__all__'
        widgets = {
            'image': forms.ClearableFileInput(attrs={'accept': 'image/*'}),
        }

class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = '__all__'


class TenantForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = '__all__'

class FlatBuildingForm(forms.ModelForm):
    class Meta:
        model = FlatBuilding
        fields = '__all__'

class RentChargeForm(forms.ModelForm):
    class Meta:
        model = RentCharge
        fields = '__all__'

class PaymentInitForm(forms.ModelForm):
    class Meta:
        model = PaymentRequest
        fields ='__all__'


# ------------------------------
# Worker Registration Form
# ------------------------------
class WorkerRegistrationForm(UserCreationForm):
    full_name = forms.CharField(max_length=100, required=True)
    email = forms.EmailField(required=True)
    phone = PhoneNumberField(required=True)
    id_number = forms.CharField(max_length=20, required=False)
    skills = forms.CharField(
        max_length=255, required=True,
        help_text="Comma-separated skills e.g. plumbing, electrical, carpentry",
        widget=forms.TextInput(attrs={"placeholder": "e.g. painting, electrical, plumbing"})
    )
    bio = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Briefly describe your experience"}),
        required=False
    )

    class Meta:
        model = User
        fields = ["username", "full_name", "email", "phone", "id_number", "skills", "bio", "password1", "password2"]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
            Worker.objects.create(
                user=user,
                full_name=self.cleaned_data["full_name"],
                email=self.cleaned_data["email"],
                phone=self.cleaned_data["phone"],
                id_number=self.cleaned_data.get("id_number", ""),
                skills=self.cleaned_data["skills"],
                bio=self.cleaned_data.get("bio", ""),
                is_approved=False,
            )
        return user


# ------------------------------
# Worker Profile Update Form
# ------------------------------
class WorkerProfileForm(forms.ModelForm):
    class Meta:
        model = Worker
        fields = ["full_name", "phone", "id_number", "skills", "bio"]
        widgets = {
            "skills": forms.TextInput(attrs={"placeholder": "e.g. painting, electrical, plumbing"}),
            "bio": forms.Textarea(attrs={"rows": 3}),
        }


# ------------------------------
# Maintenance Bid Form (for workers)
# ------------------------------
class MaintenanceBidForm(forms.ModelForm):
    class Meta:
        model = MaintenanceBid
        fields = ["amount", "estimated_days", "notes"]
        widgets = {
            "amount": forms.NumberInput(attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0",
                "placeholder": "0.00",
            }),
            "estimated_days": forms.NumberInput(attrs={
                "class": "form-control",
                "min": "1",
                "placeholder": "e.g. 3",
            }),
            "notes": forms.Textarea(attrs={
                "class": "form-control",
                "rows": 5,
                "placeholder": "Describe your approach, materials needed, and any clarifications for the landlord.",
            }),
        }


# ------------------------------
# Expense Form
# ------------------------------
class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["flat_building", "house", "tenant", "category", "amount", "description", "vendor", "expense_date", "receipt", "is_recoverable"]
        widgets = {
            "expense_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.TextInput(attrs={"placeholder": "What was this expense for?"}),
            "vendor": forms.TextInput(attrs={"placeholder": "Who was paid?"}),
            "receipt": forms.ClearableFileInput(),
        }
        help_texts = {
            "is_recoverable": "Check if this expense should be charged back to the tenant (e.g. excess water usage)",
        }

    def __init__(self, user=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if user:
            self.fields["flat_building"].queryset = FlatBuilding.objects.filter(user=user)
            self.fields["house"].queryset = House.objects.filter(user=user)
            self.fields["tenant"].queryset = Tenant.objects.filter(house__user=user, is_active=True)
            # Prioritize tenant when recoverable
            self.fields["tenant"].required = False


# ------------------------------
# House Image Form (up to 5 pictures)
# ------------------------------
class HouseImageForm(forms.ModelForm):
    class Meta:
        model = HouseImage
        fields = ["image", "caption", "sort_order"]
        widgets = {
            "image": forms.ClearableFileInput(attrs={"accept": "image/*"}),
            "caption": forms.TextInput(attrs={"placeholder": "Optional caption (e.g. Living Room, Kitchen)"}),
            "sort_order": forms.NumberInput(attrs={"min": 0, "max": 100}),
        }
        help_texts = {
            "image": "Upload a photo (JPG, PNG, GIF, WebP). Max size: 5MB.",
        }

    def __init__(self, house=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.house = house

    def clean_image(self):
        image = self.cleaned_data.get("image")
        if image:
            import os
            ext = os.path.splitext(image.name)[1].lower().lstrip(".")
            if ext not in HouseImage.ALLOWED_EXTENSIONS:
                raise forms.ValidationError(f"Image format not supported. Allowed: {', '.join(HouseImage.ALLOWED_EXTENSIONS)}")
            if image.size > HouseImage.MAX_FILE_SIZE_MB * 1024 * 1024:
                raise forms.ValidationError(f"Image must be less than {HouseImage.MAX_FILE_SIZE_MB}MB.")
        return image

    def clean(self):
        cleaned_data = super().clean()
        if self.house:
            from django.core.exceptions import ValidationError as VE
            existing = self.house.images.count()
            if existing >= HouseImage.MAX_IMAGES:
                raise VE(f"Maximum of {HouseImage.MAX_IMAGES} images allowed. Please delete one before adding another.")
        return cleaned_data
