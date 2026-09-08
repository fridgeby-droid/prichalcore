import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Secrets are intentionally read only from environment variables.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DAILY_NOTIFICATION_SECRET = os.getenv("DAILY_NOTIFICATION_SECRET", "").strip()

# Non-secret runtime settings may have safe defaults.
APP_URL = os.getenv("APP_URL", "https://prichal59.bothost.tech").rstrip("/")
WEBHOOK_URL = f"{APP_URL}/webhook"
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "-1002451968808"))
PERM_OFFSET = int(os.getenv("PERM_OFFSET", "5"))
TELEGRAM_AUTH_MAX_AGE_SECONDS = int(os.getenv("TELEGRAM_AUTH_MAX_AGE_SECONDS", "86400"))

SUPPLIERS_FILE = os.path.join(BASE_DIR, "suppliers.json")
PRODUCTS_FILE = os.path.join(BASE_DIR, "products.json")
ORDERS_FILE = os.path.join(BASE_DIR, "orders.json")
SHOPS_FILE = os.path.join(BASE_DIR, "shops.json")
SCHEDULE_FILE = os.path.join(BASE_DIR, "schedule.json")
ADMINS_FILE = os.path.join(BASE_DIR, "admins.json")
GROUPS_FILE = os.path.join(BASE_DIR, "groups.json")
USERS_FILE = os.path.join(BASE_DIR, "users_v2.json")
STORE_CATALOG_FILE = os.path.join(BASE_DIR, "store_catalog.json")
ATTACHMENTS_DIR = os.path.join(BASE_DIR, "attachments")
PRICELISTS_DIR = os.path.join(BASE_DIR, "pricelists")
PRICELISTS_FILE = os.path.join(BASE_DIR, "pricelists.json")
MINIAPP_DIR = os.path.join(BASE_DIR, "miniapp")
