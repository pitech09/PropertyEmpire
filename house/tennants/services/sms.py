from django.conf import settings
from django.utils import timezone
from datetime import timedelta
import logging
import os
import requests
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

logger = logging.getLogger(__name__)

class TwilioNotificationService:
    def __init__(self):
        self.provider = getattr(settings, "SMS_PROVIDER", "twilio").lower()
        self.client = None
        self.from_number = settings.TWILIO_PHONE_NUMBER
        self.textbee_api_key = getattr(settings, "TEXTBEE_API_KEY", None)
        self.textbee_device_id = getattr(settings, "TEXTBEE_DEVICE_ID", None)
        self.textbee_base_url = getattr(settings, "TEXTBEE_BASE_URL", "https://api.textbee.dev/api/v1").rstrip("/")
        self.textbee_sim_subscription_id = getattr(settings, "TEXTBEE_SIM_SUBSCRIPTION_ID", None)
    
    def send_sms(self, to_number, message, queue_on_failure=True):
        """Send SMS through the configured provider."""
        message = self._prepare_sms_message(message)
        if self.provider == "textbee":
            success, result = self._send_textbee_sms(to_number, message)
        else:
            success, result = self._send_twilio_sms(to_number, message)

        if not success and queue_on_failure:
            self._queue_failed_sms(to_number, message, result)
        return success, result

    def _prepare_sms_message(self, message):
        message = " ".join(str(message).split())
        max_length = getattr(settings, "SMS_MAX_LENGTH", 160)
        if max_length and len(message) > max_length:
            logger.warning("SMS message trimmed from %s to %s characters", len(message), max_length)
            return message[: max_length - 3].rstrip() + "..."
        return message

    def _queue_failed_sms(self, to_number, message, error):
        if not to_number:
            return

        from tennants.models import SMSRetryMessage

        retry_interval = getattr(settings, "SMS_RETRY_INTERVAL_MINUTES", 6)
        SMSRetryMessage.objects.create(
            to_number=str(to_number),
            message=message,
            provider=self.provider,
            max_attempts=getattr(settings, "SMS_RETRY_MAX_ATTEMPTS", 5),
            next_attempt_at=timezone.now() + timedelta(minutes=retry_interval),
            last_error=str(error),
        )

    def _get_twilio_client(self):
        if self.client is None:
            proxy_client = TwilioHttpClient(
                proxy={'http': os.environ.get('HTTP_PROXY'), 'https': os.environ.get('HTTP_PROXY')}
            )
            self.client = Client(
                settings.TWILIO_ACCOUNT_SID,
                settings.TWILIO_AUTH_TOKEN,
                http_client=proxy_client
            )
        return self.client

    def _send_twilio_sms(self, to_number, message):
        """Send SMS via Twilio."""
        try:
            msg = self._get_twilio_client().messages.create(
                body=message,
                from_=self.from_number,
                to=str(to_number)
            )
            logger.info(f"SMS sent successfully to {to_number}. SID: {msg.sid}")
            return True, msg.sid
        except Exception as e:
            logger.error(f"Failed to send SMS to {to_number}: {str(e)}")
            return False, str(e)

    def _send_textbee_sms(self, to_number, message):
        """Send SMS via TextBee."""
        if not self.textbee_api_key:
            return False, "Missing TEXTBEE_API_KEY"
        if not self.textbee_device_id:
            return False, "Missing TEXTBEE_DEVICE_ID"
        if not to_number:
            return False, "Missing SMS recipient"

        url = f"{self.textbee_base_url}/gateway/devices/{self.textbee_device_id}/send-sms"
        payload = {
            "recipients": [str(to_number)],
            "message": message,
        }
        if self.textbee_sim_subscription_id:
            try:
                payload["simSubscriptionId"] = int(self.textbee_sim_subscription_id)
            except (TypeError, ValueError):
                return False, "TEXTBEE_SIM_SUBSCRIPTION_ID must be a number"

        try:
            response = requests.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.textbee_api_key,
                },
                timeout=20,
            )
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                data = {"response": response.text}
            message_id = data.get("id") or data.get("messageId") or data.get("batchId") or str(data)
            logger.info(f"SMS sent successfully to {to_number} via TextBee. Result: {message_id}")
            return True, message_id
        except requests.RequestException as e:
            detail = getattr(e.response, "text", "") if getattr(e, "response", None) is not None else str(e)
            logger.error(f"Failed to send SMS to {to_number} via TextBee: {detail}")
            return False, detail

    def send_whatsapp(self, to_number, message):
        """Send WhatsApp message via Twilio."""
        if not to_number:
            return False, "Missing WhatsApp recipient"
        if not getattr(settings, "TWILIO_WHATSAPP_FROM", None):
            return False, "Missing Twilio WhatsApp sender"

        to_number = str(to_number)
        if not to_number.startswith("whatsapp:"):
            to_number = f"whatsapp:{to_number}"

        try:
            msg = self._get_twilio_client().messages.create(
                body=message,
                from_=settings.TWILIO_WHATSAPP_FROM,
                to=to_number,
            )
            logger.info(f"WhatsApp sent successfully to {to_number}. SID: {msg.sid}")
            return True, msg.sid
        except Exception as e:
            logger.error(f"Failed to send WhatsApp to {to_number}: {str(e)}")
            return False, str(e)
    
    def send_rent_due_reminder(self, rent_charge):
        """Send rent due reminder"""
        tenant = rent_charge.tenant
        
        if not tenant.sms_notifications:
            logger.info(f"SMS notifications disabled for {tenant.full_name}")
            return False, "Notifications disabled"
        
        days_until_due = (tenant.rent_due_date - timezone.now().date()).days
        message = self._generate_rent_reminder(rent_charge, days_until_due)
        
        success, result = self.send_sms(tenant.phone, message)
        
        if success:
            rent_charge.reminder_sent = True
            rent_charge.save(update_fields=['reminder_sent'])
            
            tenant.last_reminder_sent = timezone.now()
            tenant.save(update_fields=['last_reminder_sent'])
        
        return success, result
    
    def send_payment_confirmation(self, payment):
        """Send payment confirmation"""
        tenant = payment.tenant
        
        if not tenant.sms_notifications:
            return False, "Notifications disabled"
        
        message = self._generate_payment_confirmation(payment)
        return self.send_sms(tenant.phone, message)
    
    def send_move_in_welcome(self, tenant):
        """Send welcome message when tenant moves in"""
        if not tenant.sms_notifications:
            return False, "Notifications disabled"
        
        message = self._generate_welcome_message(tenant)
        return self.send_sms(tenant.phone, message)
    
    def send_overdue_notice(self, rent_charge):
        """Send overdue payment notice"""
        tenant = rent_charge.tenant
        
        if not tenant.sms_notifications_enabled:
            return False, "Notifications disabled"
        
        message = self._generate_overdue_notice(rent_charge)
        return self.send_sms(tenant.phone, message)
    
    def _generate_rent_reminder(self, rent_charge, days_until_due):
        """Generate rent reminder message"""
        tenant = rent_charge.tenant
        
        if days_until_due <= 0:
            urgency = "TODAY"
        elif days_until_due == 1:
            urgency = "TOMORROW"
        else:
            urgency = f"in {days_until_due} days"
        
        message = (
            f"Rent reminder: M{rent_charge.amount_due:,.2f} for "
            f"{tenant.building_name} house {tenant.house.house_number} "
            f"is due {urgency}."
        )
        
        if rent_charge.balance > 0:
            message += f" Balance: M{rent_charge.balance:,.2f}."
        
        return message
    
    def _generate_payment_confirmation(self, payment):
        """Generate payment confirmation message"""
        tenant = payment.tenant
        rent_charge = payment.rent_charge
        
        message = (
            f"Payment received: M{payment.amount:,.2f} for "
            f"{rent_charge.get_month_display()} {rent_charge.year} rent. "
        )
        
        if rent_charge.balance > 0:
            message += f"Balance: M{rent_charge.balance:,.2f}."
        else:
            message += "Rent fully paid. Thank you."
        
        return message
    
    def _generate_welcome_message(self, tenant):
        """Generate welcome message for new tenant"""
        message = (
            f"Welcome {tenant.full_name}. House {tenant.house.house_number}, "
            f"{tenant.building_name}. Rent: M{tenant.rent:,.2f}, "
            f"due every {tenant.rent_due_date.day}."
        )
        return message
    
    def _generate_overdue_notice(self, rent_charge):
        """Generate overdue payment notice"""
        tenant = rent_charge.tenant
        days_overdue = (timezone.now().date() - tenant.rent_due_date).days
        
        message = (
            f"Overdue rent: {rent_charge.get_month_display()} {rent_charge.year} "
            f"is {days_overdue} days late. Amount due: M{rent_charge.balance:,.2f}. "
            "Please pay as soon as possible."
        )
        return message
