import hashlib
import hmac
import json
import time
import urllib.parse


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400):
    if not init_data or not bot_token:
        return None
    try:
        parsed = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
        fields = dict(parsed)
        received_hash = fields.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        auth_date = fields.get("auth_date")
        if auth_date and max_age_seconds > 0:
            age = int(time.time()) - int(auth_date)
            if age < -60 or age > max_age_seconds:
                return None
        user_raw = fields.get("user")
        if not user_raw:
            return None
        user = json.loads(user_raw)
        return user if isinstance(user, dict) and user.get("id") else None
    except Exception:
        return None
