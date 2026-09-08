import json
import os
import urllib.parse
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests
import base64
import uuid
import time
import threading

# ========== 1. КОНФИГУРАЦИЯ ==========
from config import (
    BOT_TOKEN, APP_URL, WEBHOOK_URL, GROUP_CHAT_ID, DAILY_NOTIFICATION_SECRET,
    BASE_DIR, SUPPLIERS_FILE, PRODUCTS_FILE, ORDERS_FILE, SHOPS_FILE,
    SCHEDULE_FILE, ADMINS_FILE, GROUPS_FILE, USERS_FILE, STORE_CATALOG_FILE,
    ATTACHMENTS_DIR, PRICELISTS_DIR, PRICELISTS_FILE, MINIAPP_DIR,
    PERM_OFFSET, TELEGRAM_AUTH_MAX_AGE_SECONDS,
)
from core.telegram_auth import get_user_id as validated_user_id, get_user_name as validated_user_name
from core.store_registry import ensure_store_catalog, store_id_for_name, active_stores
from core.users import get_user_profile

# Создаем папки
for dir_path in [MINIAPP_DIR, ATTACHMENTS_DIR, PRICELISTS_DIR]:
    if not os.path.exists(dir_path):
        os.makedirs(dir_path, exist_ok=True)

app = Flask(__name__)
CORS(app)  # Добавляем поддержку CORS

# ========== 2. ЧАСОВОЙ ПОЯС (ПЕРМЬ UTC+5) ==========

def get_current_day():
    now = datetime.utcnow() + timedelta(hours=PERM_OFFSET)
    days = {0: "Понедельник", 1: "Вторник", 2: "Среда", 3: "Четверг",
            4: "Пятница", 5: "Суббота", 6: "Воскресенье"}
    return days[now.weekday()]

def get_current_time():
    now = datetime.utcnow() + timedelta(hours=PERM_OFFSET)
    return now.strftime("%H:%M")

def get_current_date():
    now = datetime.utcnow() + timedelta(hours=PERM_OFFSET)
    return now.strftime("%d.%m.%Y")

# ========== 3. ФУНКЦИИ ДЛЯ РАБОТЫ С ФАЙЛАМИ ==========
_DATA_LOCK = threading.RLock()

def load_data(filename, default_data):
    directory = os.path.dirname(filename)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

    if not os.path.exists(filename):
        save_data(filename, default_data)
        return default_data

    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        # Never silently overwrite a damaged production JSON file.
        raise RuntimeError(f"Поврежден JSON-файл {filename}: {exc}") from exc

def save_data(filename, data):
    directory = os.path.dirname(filename)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
    temp_file = f"{filename}.tmp"
    with _DATA_LOCK:
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, filename)

# Загружаем данные
suppliers = load_data(SUPPLIERS_FILE, {})
products = load_data(PRODUCTS_FILE, {})
orders = load_data(ORDERS_FILE, [])
shops = load_data(SHOPS_FILE, {})

if not shops:
    shops = {
        "Магазин №1": {}, "Магазин №2": {}, "Магазин №3": {},
        "Магазин №4": {}, "Магазин №5": {}, "Магазин №6": {},
        "Магазин №7": {}, "Магазин №8": {}, "Магазин №9": {}
    }
    save_data(SHOPS_FILE, shops)

# Stable IDs coexist with legacy shop names. Existing MiniApp keeps using names.
store_catalog = ensure_store_catalog(shops, STORE_CATALOG_FILE)

schedule = load_data(SCHEDULE_FILE, {})
if not schedule:
    for shop in shops:
        schedule[shop] = {}
        for supplier in suppliers:
            schedule[shop][supplier] = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница"]
    save_data(SCHEDULE_FILE, schedule)

def load_admins():
    if os.path.exists(ADMINS_FILE):
        with open(ADMINS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    default = [317111945]
    with open(ADMINS_FILE, 'w', encoding='utf-8') as f:
        json.dump(default, f, ensure_ascii=False, indent=4)
    return default

def save_admins(admins):
    with open(ADMINS_FILE, 'w', encoding='utf-8') as f:
        json.dump(admins, f, ensure_ascii=False, indent=4)

admins = load_admins()
groups = load_data(GROUPS_FILE, [])

def load_pricelists():
    if os.path.exists(PRICELISTS_FILE):
        with open(PRICELISTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_pricelists(pricelists):
    with open(PRICELISTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(pricelists, f, ensure_ascii=False, indent=4)

# ========== 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def send_message(chat_id, text, reply_markup=None):
    if not BOT_TOKEN:
        print("BOT_TOKEN не настроен: сообщение Telegram не отправлено")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки: {e}")

def is_admin(user_id):
    return user_id in admins

def send_notification_to_admins(order):
    text = f"📦 <b>НОВАЯ ЗАЯВКА #{order['id']}</b>\n\n"
    text += f"🏪 <b>Магазин:</b> {order['shop']}\n"
    text += f"📦 <b>Поставщик:</b> {order['supplier']}\n"
    text += f"👤 <b>Создал:</b> {order.get('user_name', 'Пользователь')}\n"
    text += f"📅 <b>Дата:</b> {order['date']} {order.get('time', '')}\n\n"

    if order.get('comment'):
        text += f"💬 <b>Комментарий:</b> {order['comment']}\n\n"

    if order.get('items') and len(order['items']) > 0:
        text += f"🛒 <b>Товары:</b>\n"
        for product, data in order['items'].items():
            if isinstance(data, dict):
                qty = data.get('quantity', 0)
                unit = data.get('unit', 'шт')
                text += f"   • {product}: {qty} {unit}\n"
            else:
                text += f"   • {product}: {data} шт\n"
    else:
        text += f"📭 <b>Нет товаров</b> (только комментарий)\n"

    text += f"\n🌐 <b>Открыть приложение:</b> {APP_URL}/miniapp"

    for admin_id in admins:
        try:
            send_message(admin_id, text)
        except Exception as e:
            print(f"Ошибка отправки уведомления {admin_id}: {e}")

def extract_user_id_from_init_data(init_data):
    return validated_user_id(init_data, BOT_TOKEN, TELEGRAM_AUTH_MAX_AGE_SECONDS)

def extract_user_name_from_init_data(init_data):
    return validated_user_name(init_data, BOT_TOKEN, TELEGRAM_AUTH_MAX_AGE_SECONDS)

def check_schedule(shop_name, supplier_name):
    current_day = get_current_day()
    if shop_name in schedule and supplier_name in schedule.get(shop_name, {}):
        allowed_days = schedule[shop_name][supplier_name]
        if current_day in allowed_days:
            return True, allowed_days
    return True, ["Любой день"]

def send_daily_notification():
    current_day = get_current_day()
    current_date = get_current_date()
    current_time = get_current_time()

    message = f"ЕЖЕДНЕВНАЯ РАССЫЛКА\n"
    message += f"Дата: {current_date} ({current_day})\n"
    message += f"Время: {current_time}\n\n"
    message += "Доступные поставщики:\n\n"

    has_any = False
    for shop in shops.keys():
        available_suppliers = []
        for supplier in suppliers.keys():
            if shop in schedule and supplier in schedule.get(shop, {}):
                allowed_days = schedule[shop][supplier]
                if current_day in allowed_days:
                    available_suppliers.append(supplier)

        if available_suppliers:
            has_any = True
            message += f"Магазин: {shop}\n"
            for supplier in available_suppliers:
                message += f"  - {supplier}\n"
            message += "\n"

    if not has_any:
        message += "Сегодня нет доступных поставщиков\n\n"

    message += f"Ссылка: {APP_URL}/miniapp"

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": GROUP_CHAT_ID, "text": message}
        response = requests.post(url, json=payload, timeout=10)
        result = response.json()

        if result.get("ok"):
            print(f"✅ Рассылка отправлена в группу {GROUP_CHAT_ID}")
            return True
        else:
            print(f"❌ Ошибка API: {result}")
            send_message(admins[0], f"❌ Ошибка отправки в группу\n{result}")
            return False
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        send_message(admins[0], f"❌ Ошибка: {e}")
        return False

# ========== 5. API ДЛЯ MINI APP ==========
@app.route('/api/test', methods=['GET'])
def api_test():
    return jsonify({"status": "ok", "message": "API работает"})

@app.route('/api/get_data', methods=['GET', 'POST'])
def api_get_data():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)
    profile = get_user_profile(user_id, USERS_FILE, admins) if user_id else None
    return jsonify({
        'shops': shops,
        'suppliers': suppliers,
        'is_admin': is_admin(user_id) if user_id else False,
        # v2 fields are additive and do not break the existing MiniApp.
        'user': profile,
        'store_catalog': active_stores(shops, STORE_CATALOG_FILE),
        'auth_valid': bool(user_id)
    })

@app.route('/api/get_products', methods=['GET'])
def api_get_products():
    supplier = request.args.get('supplier')
    if supplier in products:
        return jsonify(products[supplier])
    return jsonify({"categories": {}})

@app.route('/api/create_order', methods=['POST'])
def api_create_order():
    try:
        data = request.get_json()
        items = data.get('items', {})
        comment = data.get('comment', '').strip()
        is_empty = data.get('is_empty', False)
        attachments = data.get('attachments', [])
        
        # Получаем статус из запроса (по умолчанию 'new')
        status = data.get('status', 'new')
        valid_statuses = ['new', 'completed', 'cancelled', 'skip']
        if status not in valid_statuses:
            status = 'new'

        init_data = request.headers.get('X-Telegram-InitData', '')
        user_id = extract_user_id_from_init_data(init_data)
        user_name = extract_user_name_from_init_data(init_data)

        # Для заявок-пропусков не проверяем наличие товаров и комментария
        if status != 'skip':
            if not items and not comment:
                return jsonify({"success": False, "error": "Укажите комментарий или добавьте товары"}), 400

        shop_name = data.get('shop')
        new_order = {
            "id": (max([o.get("id", 0) for o in orders] or [0]) + 1),
            "shop": shop_name,
            "store_id": store_id_for_name(shop_name, shops, STORE_CATALOG_FILE),
            "supplier": data.get('supplier'),
            "items": items,
            "comment": comment,
            "attachments": attachments,
            "is_empty": is_empty or (not items and bool(comment)) or status == 'skip',
            "status": status,
            "date": get_current_date(),
            "day": get_current_day(),
            "time": get_current_time(),
            "user_id": user_id,
            "user_name": user_name
        }

        orders.append(new_order)
        save_data(ORDERS_FILE, orders)

        # Отправляем уведомления только для обычных заявок, не для пропусков
        if status != 'skip':
            send_notification_to_admins(new_order)

        return jsonify({"success": True, "order_id": new_order["id"]})
    except Exception as e:
        print(f"Ошибка создания заявки: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/get_orders', methods=['GET'])
def api_get_orders():
    return jsonify(orders)

@app.route('/api/update_order', methods=['POST'])
def api_update_order():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    order_id = data.get('order_id')

    for i, order in enumerate(orders):
        if order['id'] == order_id:
            orders[i]['shop'] = data.get('shop', order['shop'])
            orders[i]['store_id'] = store_id_for_name(orders[i]['shop'], shops, STORE_CATALOG_FILE)
            orders[i]['supplier'] = data.get('supplier', order['supplier'])
            orders[i]['items'] = data.get('items', order['items'])
            orders[i]['comment'] = data.get('comment', order['comment'])
            orders[i]['attachments'] = data.get('attachments', order.get('attachments', []))
            save_data(ORDERS_FILE, orders)
            return jsonify({"success": True})

    return jsonify({"success": False, "error": "Заявка не найдена"}), 404

@app.route('/api/delete_order', methods=['POST'])
def api_delete_order():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    order_id = data.get('order_id')

    global orders
    new_orders = [o for o in orders if o['id'] != order_id]

    if len(new_orders) == len(orders):
        return jsonify({"success": False, "error": "Заявка не найдена"}), 404

    orders = new_orders
    save_data(ORDERS_FILE, orders)

    return jsonify({"success": True})

@app.route('/api/upload_attachment', methods=['POST'])
def api_upload_attachment():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    #if not is_admin(user_id):
    #    return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    file_data = data.get('file_data', '')
    file_name = data.get('file_name', 'file')
    file_type = data.get('file_type', 'image')

    if not file_data:
        return jsonify({"success": False, "error": "Нет данных файла"}), 400

    file_id = str(uuid.uuid4())
    ext = file_name.split('.')[-1] if '.' in file_name else 'png'
    filename = f"{file_id}.{ext}"
    filepath = os.path.join(ATTACHMENTS_DIR, filename)

    try:
        if ',' in file_data:
            file_data = file_data.split(',')[1]

        with open(filepath, 'wb') as f:
            f.write(base64.b64decode(file_data))

        file_url = f"{APP_URL}/attachments/{filename}"

        return jsonify({
            "success": True,
            "attachment": {
                "id": file_id,
                "name": file_name,
                "url": file_url,
                "type": file_type
            }
        })
    except Exception as e:
        print(f"Ошибка сохранения файла: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/attachments/<filename>')
def serve_attachment(filename):
    return send_from_directory(ATTACHMENTS_DIR, filename)

@app.route('/api/update_order_status', methods=['POST'])
def api_update_order_status():
    try:
        init_data = request.headers.get('X-Telegram-InitData', '')
        user_id = extract_user_id_from_init_data(init_data)

        if not is_admin(user_id):
            return jsonify({"success": False, "error": "Нет прав"}), 403

        data = request.get_json()
        order_id = data.get('order_id')
        new_status = data.get('status')

        valid_statuses = ['new', 'completed', 'cancelled', 'skip']
        if new_status not in valid_statuses:
            return jsonify({"success": False, "error": "Неверный статус"}), 400

        for order in orders:
            if order['id'] == order_id:
                order['status'] = new_status
                save_data(ORDERS_FILE, orders)
                return jsonify({"success": True})

        return jsonify({"success": False, "error": "Заявка не найдена"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/get_schedule', methods=['GET'])
def api_get_schedule():
    shop = request.args.get('shop')
    if shop in schedule:
        return jsonify(schedule[shop])
    return jsonify({})

@app.route('/api/get_available_suppliers', methods=['GET'])
def api_get_available_suppliers():
    shop = request.args.get('shop')
    current_day = get_current_day()
    available = []
    for supplier in suppliers.keys():
        if shop in schedule and supplier in schedule.get(shop, {}):
            if current_day in schedule[shop][supplier]:
                available.append(supplier)
        else:
            available.append(supplier)
    return jsonify(available)

@app.route('/api/get_suppliers', methods=['GET'])
def api_get_suppliers():
    return jsonify(suppliers)

@app.route('/api/get_all_products', methods=['GET'])
def api_get_all_products():
    return jsonify(products)

@app.route('/api/get_shops', methods=['GET'])
def api_get_shops():
    return jsonify(shops)

@app.route('/api/add_supplier', methods=['POST'])
def api_add_supplier():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    name = data.get('name')

    if name and name not in suppliers:
        suppliers[name] = {}
        save_data(SUPPLIERS_FILE, suppliers)

        for shop in schedule:
            if shop not in schedule:
                schedule[shop] = {}
            schedule[shop][name] = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница"]
        save_data(SCHEDULE_FILE, schedule)

        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Поставщик уже существует"})

@app.route('/api/add_shop', methods=['POST'])
def api_add_shop():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    name = data.get('name')

    if name and name not in shops:
        shops[name] = {}
        save_data(SHOPS_FILE, shops)
        ensure_store_catalog(shops, STORE_CATALOG_FILE)

        schedule[name] = {}
        for supplier in suppliers.keys():
            schedule[name][supplier] = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница"]
        save_data(SCHEDULE_FILE, schedule)

        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Магазин уже существует"})

@app.route('/api/add_product', methods=['POST'])
def api_add_product():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    supplier = data.get('supplier')
    category = data.get('category', 'Общие')
    name = data.get('name')
    unit = data.get('unit', 'шт')

    valid_units = ["шт", "кг", "кег", "кейс", "бут", "упак"]
    if unit not in valid_units:
        return jsonify({"success": False, "error": "Неверная единица измерения"}), 400

    if supplier not in products:
        products[supplier] = {"categories": {}}

    if isinstance(products[supplier], list):
        old_items = products[supplier]
        products[supplier] = {"categories": {"Общие": old_items}}

    if "categories" not in products[supplier]:
        products[supplier]["categories"] = {}

    if category not in products[supplier]["categories"]:
        products[supplier]["categories"][category] = []

    products[supplier]["categories"][category].append({"name": name, "unit": unit})
    save_data(PRODUCTS_FILE, products)

    return jsonify({"success": True})

@app.route('/api/add_category', methods=['POST'])
def api_add_category():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    supplier = data.get('supplier')
    category_name = data.get('category_name')

    if supplier not in products:
        products[supplier] = {"categories": {}}

    if "categories" not in products[supplier]:
        products[supplier]["categories"] = {}

    if category_name in products[supplier]["categories"]:
        return jsonify({"success": False, "error": "Категория уже существует"}), 400

    products[supplier]["categories"][category_name] = []
    save_data(PRODUCTS_FILE, products)

    return jsonify({"success": True})

@app.route('/api/delete_category', methods=['POST'])
def api_delete_category():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    supplier = data.get('supplier')
    category_name = data.get('category_name')

    if supplier not in products:
        return jsonify({"success": False, "error": "Поставщик не найден"}), 404

    if "categories" not in products[supplier]:
        return jsonify({"success": False, "error": "Категории не найдены"}), 404

    if category_name not in products[supplier]["categories"]:
        return jsonify({"success": False, "error": "Категория не найдена"}), 404

    del products[supplier]["categories"][category_name]
    save_data(PRODUCTS_FILE, products)

    return jsonify({"success": True})

@app.route('/api/update_category', methods=['POST'])
def api_update_category():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    supplier = data.get('supplier')
    old_name = data.get('old_name')
    new_name = data.get('new_name')

    if supplier not in products:
        return jsonify({"success": False, "error": "Поставщик не найден"}), 404

    if "categories" not in products[supplier]:
        products[supplier]["categories"] = {}

    if old_name not in products[supplier]["categories"]:
        return jsonify({"success": False, "error": "Категория не найдена"}), 404

    products[supplier]["categories"][new_name] = products[supplier]["categories"].pop(old_name)
    save_data(PRODUCTS_FILE, products)

    return jsonify({"success": True})

@app.route('/api/get_categories', methods=['GET'])
def api_get_categories():
    supplier = request.args.get('supplier')
    if supplier in products and "categories" in products[supplier]:
        return jsonify({"success": True, "categories": list(products[supplier]["categories"].keys())})
    return jsonify({"success": True, "categories": []})

@app.route('/api/update_product', methods=['POST'])
def api_update_product():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    supplier = data.get('supplier')
    old_name = data.get('old_name')
    new_name = data.get('new_name')
    new_unit = data.get('new_unit')
    category = data.get('category', 'Общие')

    valid_units = ["шт", "кг", "кег", "кейс", "бут", "упак"]
    if new_unit not in valid_units:
        return jsonify({"success": False, "error": "Неверная единица измерения"}), 400

    if supplier not in products:
        return jsonify({"success": False, "error": "Поставщик не найден"}), 404

    if isinstance(products[supplier], list):
        old_items = products[supplier]
        products[supplier] = {"categories": {"Общие": old_items}}

    if "categories" not in products[supplier]:
        products[supplier]["categories"] = {}

    if category not in products[supplier]["categories"]:
        products[supplier]["categories"][category] = []

    for i, p in enumerate(products[supplier]["categories"][category]):
        if p.get('name') == old_name:
            products[supplier]["categories"][category][i] = {"name": new_name, "unit": new_unit}
            save_data(PRODUCTS_FILE, products)
            return jsonify({"success": True})

    return jsonify({"success": False, "error": "Товар не найден"}), 404

@app.route('/api/upload_pricelist', methods=['POST'])
def api_upload_pricelist():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    if 'file' not in request.files:
        return jsonify({"success": False, "error": "Нет файла"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "Файл не выбран"}), 400

    if not file.filename.endswith(('.xls', '.xlsx')):
        return jsonify({"success": False, "error": "Поддерживаются только .xls и .xlsx файлы"}), 400

    file_id = str(uuid.uuid4())
    ext = file.filename.split('.')[-1]
    filename = f"{file_id}.{ext}"
    filepath = os.path.join(PRICELISTS_DIR, filename)

    file.save(filepath)

    pricelists = load_pricelists()
    pricelist_info = {
        "id": file_id,
        "name": file.filename,
        "url": f"{APP_URL}/pricelists/{filename}",
        "upload_date": get_current_date(),
        "upload_time": get_current_time(),
        "uploaded_by": extract_user_name_from_init_data(init_data),
        "size": os.path.getsize(filepath)
    }
    pricelists.insert(0, pricelist_info)
    save_pricelists(pricelists)

    return jsonify({"success": True, "pricelist": pricelist_info})

@app.route('/api/get_pricelists', methods=['GET'])
def api_get_pricelists():
    return jsonify({"success": True, "pricelists": load_pricelists()})

@app.route('/api/delete_pricelist', methods=['POST'])
def api_delete_pricelist():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    pricelist_id = data.get('id')

    pricelists = load_pricelists()
    pricelist_to_delete = None
    for p in pricelists:
        if p['id'] == pricelist_id:
            pricelist_to_delete = p
            break

    if pricelist_to_delete:
        filename = pricelist_to_delete['url'].split('/')[-1]
        filepath = os.path.join(PRICELISTS_DIR, filename)
        if os.path.exists(filepath):
            os.remove(filepath)

        pricelists = [p for p in pricelists if p['id'] != pricelist_id]
        save_pricelists(pricelists)
        return jsonify({"success": True})

    return jsonify({"success": False, "error": "Прайс-лист не найден"}), 404

@app.route('/pricelists/<filename>')
def serve_pricelist(filename):
    return send_from_directory(PRICELISTS_DIR, filename)

@app.route('/api/import_products_from_excel', methods=['POST'])
def api_import_products_from_excel():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    if 'file' not in request.files:
        return jsonify({"success": False, "error": "Нет файла"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "Файл не выбран"}), 400

    temp_filename = f"temp_{uuid.uuid4()}.xlsx"
    temp_path = os.path.join(PRICELISTS_DIR, temp_filename)
    file.save(temp_path)

    try:
        import openpyxl
        wb = openpyxl.load_workbook(temp_path, data_only=True)
        ws = wb.active

        imported_count = 0
        errors = []

        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or len(row) < 4:
                continue

            supplier = str(row[0]).strip() if row[0] else None
            category = str(row[1]).strip() if row[1] else "Общие"
            product_name = str(row[2]).strip() if row[2] else None
            unit = str(row[3]).strip() if row[3] else "шт"

            if not supplier or not product_name:
                continue

            valid_units = ["шт", "кг", "кег", "кейс", "бут", "упак"]
            if unit not in valid_units:
                unit = "шт"

            if supplier not in suppliers:
                suppliers[supplier] = {}
                save_data(SUPPLIERS_FILE, suppliers)

            if supplier not in products:
                products[supplier] = {"categories": {}}

            if isinstance(products[supplier], list):
                old_items = products[supplier]
                products[supplier] = {"categories": {"Общие": old_items}}

            if "categories" not in products[supplier]:
                products[supplier]["categories"] = {}

            if category not in products[supplier]["categories"]:
                products[supplier]["categories"][category] = []

            existing = False
            for p in products[supplier]["categories"][category]:
                if p.get('name') == product_name:
                    existing = True
                    break

            if not existing:
                products[supplier]["categories"][category].append({"name": product_name, "unit": unit})
                imported_count += 1

        save_data(PRODUCTS_FILE, products)
        os.remove(temp_path)

        return jsonify({"success": True, "imported": imported_count, "errors": errors})

    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/get_schedule_for_shop', methods=['GET'])
def api_get_schedule_for_shop():
    shop = request.args.get('shop')
    supplier = request.args.get('supplier')

    if shop in schedule and supplier in schedule.get(shop, {}):
        return jsonify({"days": schedule[shop][supplier]})
    return jsonify({"days": ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница"]})

@app.route('/api/save_schedule', methods=['POST'])
def api_save_schedule():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    shop = data.get('shop')
    supplier = data.get('supplier')
    days = data.get('days', [])

    if shop not in schedule:
        schedule[shop] = {}

    schedule[shop][supplier] = days
    save_data(SCHEDULE_FILE, schedule)

    return jsonify({"success": True})

@app.route('/api/delete_supplier', methods=['POST'])
def api_delete_supplier():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)
    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    name = data.get('name')

    if name not in suppliers:
        return jsonify({"success": False, "error": "Поставщик не найден"}), 404

    del suppliers[name]
    save_data(SUPPLIERS_FILE, suppliers)

    if name in products:
        del products[name]
        save_data(PRODUCTS_FILE, products)

    for shop in schedule:
        if name in schedule[shop]:
            del schedule[shop][name]
    save_data(SCHEDULE_FILE, schedule)

    return jsonify({"success": True})

@app.route('/api/delete_product', methods=['POST'])
def api_delete_product():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)
    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    supplier = data.get('supplier')
    product_name = data.get('product_name')

    if supplier not in products:
        return jsonify({"success": False, "error": "Поставщик не найден"}), 404

    if isinstance(products[supplier], dict) and "categories" in products[supplier]:
        for cat in products[supplier]["categories"]:
            products[supplier]["categories"][cat] = [p for p in products[supplier]["categories"][cat] if p.get('name') != product_name]
    elif isinstance(products[supplier], list):
        products[supplier] = [p for p in products[supplier] if p.get('name') != product_name]

    save_data(PRODUCTS_FILE, products)

    return jsonify({"success": True})

@app.route('/api/delete_shop', methods=['POST'])
def api_delete_shop():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)
    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    name = data.get('name')

    if name not in shops:
        return jsonify({"success": False, "error": "Магазин не найден"}), 404

    del shops[name]
    save_data(SHOPS_FILE, shops)
    ensure_store_catalog(shops, STORE_CATALOG_FILE)

    if name in schedule:
        del schedule[name]
        save_data(SCHEDULE_FILE, schedule)

    return jsonify({"success": True})

@app.route('/api/get_admins', methods=['GET'])
def api_get_admins():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    return jsonify({"success": True, "admins": admins})

@app.route('/api/add_admin', methods=['POST'])
def api_add_admin():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    new_admin_id = data.get('admin_id')

    if not new_admin_id:
        return jsonify({"success": False, "error": "ID не указан"}), 400

    if new_admin_id in admins:
        return jsonify({"success": False, "error": "Уже администратор"}), 400

    admins.append(new_admin_id)
    save_admins(admins)

    try:
        send_message(new_admin_id, "🎉 Поздравляем! Вы стали администратором бота.")
    except:
        pass

    return jsonify({"success": True})

@app.route('/api/remove_admin', methods=['POST'])
def api_remove_admin():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)

    if not is_admin(user_id):
        return jsonify({"success": False, "error": "Нет прав"}), 403

    data = request.get_json()
    remove_admin_id = data.get('admin_id')

    if not remove_admin_id:
        return jsonify({"success": False, "error": "ID не указан"}), 400

    if remove_admin_id == user_id:
        return jsonify({"success": False, "error": "Нельзя удалить себя"}), 400

    if remove_admin_id not in admins:
        return jsonify({"success": False, "error": "Не администратор"}), 400

    admins.remove(remove_admin_id)
    save_admins(admins)

    return jsonify({"success": True})

@app.route('/api/daily_notification', methods=['GET'])
def api_daily_notification():
    secret = request.args.get('secret', '')
    if not DAILY_NOTIFICATION_SECRET:
        return jsonify({"error": "DAILY_NOTIFICATION_SECRET не настроен", "success": False}), 503
    if secret != DAILY_NOTIFICATION_SECRET:
        return jsonify({"error": "Unauthorized", "success": False}), 403

    try:
        success = send_daily_notification()
        return jsonify({
            "success": success,
            "message": "Рассылка отправлена в группу" if success else "Ошибка отправки",
            "timestamp": get_current_date() + " " + get_current_time()
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ========== 5.1 V2 FOUNDATION API ==========
@app.route('/api/v2/me', methods=['GET'])
def api_v2_me():
    init_data = request.headers.get('X-Telegram-InitData', '')
    user_id = extract_user_id_from_init_data(init_data)
    if not user_id:
        return jsonify({"success": False, "error": "Telegram initData не прошел проверку"}), 401
    return jsonify({
        "success": True,
        "user": get_user_profile(user_id, USERS_FILE, admins)
    })

@app.route('/api/v2/stores', methods=['GET'])
def api_v2_stores():
    return jsonify({
        "success": True,
        "stores": active_stores(shops, STORE_CATALOG_FILE)
    })

# ========== 6. МАРШРУТЫ ДЛЯ MINI APP ==========
@app.route('/miniapp')
def miniapp_index():
    return send_from_directory(MINIAPP_DIR, 'index.html')

@app.route('/miniapp/<path:filename>')
def serve_miniapp(filename):
    return send_from_directory(MINIAPP_DIR, filename)

# ========== 7. ВЕБХУК ==========
@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update:
        return jsonify({"status": "ok"})

    if 'message' in update:
        message = update['message']
        chat_id = message['chat']['id']
        text = message.get('text', '')

        if text == '/get_group_id':
            chat_type = message['chat'].get('type', '')
            if chat_type in ['group', 'supergroup']:
                send_message(chat_id, f"📊 ID этой группы: <code>{chat_id}</code>\n\nДобавьте этот ID в код:\nGROUP_CHAT_ID = {chat_id}")
            else:
                send_message(chat_id, "❌ Эта команда работает только в группах")
        elif text == '/start':
            keyboard = {
                "keyboard": [[{"text": "🌐 Открыть приложение", "web_app": {"url": f"{APP_URL}/miniapp"}}]],
                "resize_keyboard": True
            }
            send_message(chat_id, f"👋 Привет!\n📅 Сегодня: {get_current_day()}\n\nНажмите кнопку, чтобы открыть приложение.", keyboard)
        else:
            send_message(chat_id, "🌐 Нажмите кнопку 'Открыть приложение'", {
                "keyboard": [[{"text": "🌐 Открыть приложение", "web_app": {"url": f"{APP_URL}/miniapp"}}]],
                "resize_keyboard": True
            })

    return jsonify({"status": "ok"})

# ========== 8. ДОПОЛНИТЕЛЬНЫЕ МАРШРУТЫ ==========
@app.route('/setwebhook', methods=['GET'])
def set_webhook():
    if not BOT_TOKEN:
        return jsonify({"success": False, "error": "BOT_TOKEN не настроен"}), 503
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
        json={"url": WEBHOOK_URL}
    )
    return jsonify(response.json())

@app.route('/health', methods=['GET'])
def health_check():
    products_count = 0
    for p_data in products.values():
        if isinstance(p_data, dict) and "categories" in p_data:
            for items in p_data["categories"].values():
                products_count += len(items)
        elif isinstance(p_data, list):
            products_count += len(p_data)

    return jsonify({
        "status": "ok",
        "time": get_current_time(),
        "date": get_current_date(),
        "day": get_current_day(),
        "suppliers": len(suppliers),
        "products": products_count,
        "orders": len(orders),
        "shops": len(shops),
        "admins": len(admins)
    })

@app.route('/test_group', methods=['GET'])
def test_group():
    test_chat_id = request.args.get('chat_id', '')
    if not test_chat_id:
        return jsonify({"error": "Укажите chat_id"}), 400

    try:
        test_message = f"🧪 Тестовое сообщение\nВремя: {get_current_time()}\nID: {test_chat_id}"
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": test_chat_id, "text": test_message, "parse_mode": "HTML"}
        )
        result = response.json()
        return jsonify({"success": result.get("ok", False), "response": result, "chat_id": test_chat_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/test_full_message', methods=['GET'])
def test_full_message():
    try:
        current_day = get_current_day()
        current_date = get_current_date()

        message = f"ЕЖЕДНЕВНАЯ РАССЫЛКА\n"
        message += f"Дата: {current_date} ({current_day})\n\n"
        message += "Доступные поставщики:\n\n"

        for shop in shops.keys():
            available_suppliers = []
            for supplier in suppliers.keys():
                if shop in schedule and supplier in schedule.get(shop, {}):
                    allowed_days = schedule[shop][supplier]
                    if current_day in allowed_days:
                        available_suppliers.append(supplier)

            if available_suppliers:
                message += f"Магазин: {shop}\n"
                for supplier in available_suppliers:
                    message += f"  - {supplier}\n"
                message += "\n"

        message += f"Ссылка: {APP_URL}/miniapp"

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        response = requests.post(url, json={"chat_id": GROUP_CHAT_ID, "text": message})
        return jsonify({"success": response.json().get("ok"), "response": response.json()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ========== 9. ЗАПУСК ==========
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Запуск бота на порту {port}")
    print(f"🌐 Mini App доступен по адресу: {APP_URL}/miniapp")
    print(f"🔗 Вебхук: {WEBHOOK_URL}")
    app.run(host='0.0.0.0', port=port)