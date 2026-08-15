import functools
import json
import os
import random
import sqlite3
import sys
import threading
import time

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash, check_password_hash

try:
  from curl_cffi import requests as crequests
except ImportError:
  import requests as crequests

from kick_register import KickAccountCreator, create_multiple_accounts

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = "kick_bot_super_secret_key_2026"

ADMIN_USERNAME = "taxer"
ADMIN_PASSWORD = "babaproxx123"

# Database path - production için /tmp kullan
if os.environ.get('RENDER'):
    DB_FILE = "/tmp/users.db"
else:
    DB_FILE = "users.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            points INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    
    # Check if points column exists, add it if missing (migration)
    try:
        cursor.execute("SELECT points FROM users LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE users ADD COLUMN points INTEGER DEFAULT 0")
        conn.commit()
    
    # Check if the fallback admin exists, create if not
    cursor.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,))
    if not cursor.fetchone():
        hashed = generate_password_hash(ADMIN_PASSWORD)
        cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (ADMIN_USERNAME, hashed))
        conn.commit()
    conn.close()

init_db()

bot_thread = None
is_running = False
bot_logs = []

# Hesap oluşturma için global değişkenler
account_thread = None
is_creating_accounts = False
account_logs = []
created_accounts = []

CONFIG_FILE = "config.json"

DEFAULT_CONFIG = {
    "channel": "sefoge",
    "tokens": [
        "380813590|MlQJsOlYlAjKrEvnTWkswKdT0WntbptbgUiVUHrD",
        "409791057|0gKGwsYCKVxI5fZmcJIA1EqWrKfu5Z4LYhtHR581",
    ],
    "messages": ["Selam!", "Nasılsınız?", "Harika yayın!", "Merhaba!"],
    "delay": 4.0,
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[HATA] Config yüklenemedi: {e}")
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[HATA] Config kaydedilemedi: {e}")

config = load_config()


class KickFollowAutomation:

  def __init__(self, token):
    self.token = token
    self.token_masked = token[:10] + "..." if token else "NO_TOKEN"

  def get_channel_info(self, channel_slug):
    """Kanal kullanıcı ID (user_id), kanal ID (channel_id) ve sohbet (chatroom_id) verilerini çeker."""
    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    endpoints = [
        f"https://kick.com/api/v2/channels/{channel_slug}",
        f"https://kick.com/api/v1/channels/{channel_slug}",
    ]
    for url in endpoints:
      try:
        resp = crequests.get(
            url, headers=headers, impersonate="chrome120", timeout=8
        )
        if resp.status_code == 200:
          data = resp.json()
          chatroom = data.get("chatroom", {})
          chatroom_id = chatroom.get("id") or data.get("chatroom_id")
          if chatroom_id:
            return {
                "success": True,
                "chatroom_id": chatroom_id,
                "channel_id": data.get("id"),
                "user_id": data.get("user_id"),
                "slug": data.get("slug", channel_slug),
            }
      except Exception:
        pass
    return {
        "success": False,
        "chatroom_id": None,
        "channel_id": None,
        "user_id": None,
    }

  def follow_user(self, broadcaster_identifier):
    """Belirtilen yayıncıyı/kullanıcıyı (ID veya Slug) takip etmeye çalışır."""
    headers = {
        "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {self.token}",
        "content-type": "application/json",
        "origin": "https://kick.com",
        "referer": f"https://kick.com/{broadcaster_identifier}",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "x-app-platform": "web",
    }

    # v2 ve v1 endpoint denemeleri
    endpoints = [
        f"https://kick.com/api/v2/channels/{broadcaster_identifier}/follow",
        f"https://kick.com/api/v1/channels/{broadcaster_identifier}/follow",
    ]

    for url in endpoints:
      try:
        response = crequests.post(
            url, headers=headers, json={}, impersonate="chrome120", timeout=8
        )
        if response.status_code in (200, 201, 204):
          return (
              True,
              f"[BAŞARILI] {self.token_masked} -> '{broadcaster_identifier}'"
              " takip edildi.",
          )
        elif (
            response.status_code in (400, 409)
            or "already" in response.text.lower()
        ):
          return (
              True,
              f"[BİLGİ] {self.token_masked} -> '{broadcaster_identifier}' zaten"
              " takip ediliyor.",
          )
      except Exception:
        pass

    return (
        False,
        f"[TAKİP DENE] {self.token_masked} -> '{broadcaster_identifier}' takip"
        " isteği atıldı.",
    )


def login_required(f):

  @functools.wraps(f)
  def decorated_function(*args, **kwargs):
    if not session.get("logged_in"):
      if request.is_json or request.path in [
          "/update",
          "/start",
          "/stop",
          "/logs",
          "/create-account",
          "/stop-account",
          "/account-logs",
          "/clear-account-logs",
          "/test-token"
      ]:
        return jsonify({"message": "Giriş yapmanız gerekiyor!"}), 401
      return redirect(url_for("index"))
    return f(*args, **kwargs)

  return decorated_function


def run_follow_automation_for_all(channel_slug, channel_info, tokens):
  """Bot başlatıldığında tüm tokenlar sırayla yayıncıyı ve kanal ID'sini takip etmeyi dener."""
  broadcaster_id = channel_info.get("user_id") or channel_info.get("channel_id")
  bot_logs.insert(
      0,
      f"[SİSTEM] Takip otomasyonu çalıştırılıyor... (Hedef: {channel_slug} |"
      f" User ID: {broadcaster_id})",
  )

  for token in tokens:
    if not is_running:
      break
    auto_bot = KickFollowAutomation(token)

    # 1. Slug ile takip dene
    _, log1 = auto_bot.follow_user(channel_slug)
    bot_logs.insert(0, log1)

    # 2. Broadcaster User ID ile takip dene
    if broadcaster_id:
      _, log2 = auto_bot.follow_user(broadcaster_id)
      bot_logs.insert(0, log2)

    if len(bot_logs) > 50:
      bot_logs.pop()
    time.sleep(0.5)

  bot_logs.insert(
      0, "[SİSTEM] Takip işlemleri tamamlandı. Sohbet botu aktifleştirildi."
  )


def bot_worker():
  global is_running
  current_channel = None
  channel_info = {}

  while is_running:
    if not config["tokens"] or not config["messages"]:
      bot_logs.insert(
          0, "[HATA] Token veya mesaj listesi boş! Bot durduruldu."
      )
      is_running = False
      break

    target_channel = config.get("channel", "sefoge").strip().lower()
    if not target_channel:
      bot_logs.insert(0, "[HATA] Hedef kanal adı boş! Bot durduruldu.")
      is_running = False
      break

    # Kanal değiştiyse veya chatroom_id eksikse kanal bilgilerini çek
    if current_channel != target_channel or not channel_info.get("chatroom_id"):
      current_channel = target_channel
      helper = KickFollowAutomation(config["tokens"][0])
      info = helper.get_channel_info(current_channel)

      if info["success"] and info["chatroom_id"]:
        channel_info = info
        bot_logs.insert(
            0,
            f"[SİSTEM] '{current_channel}' kanal bilgileri alındı (User ID:"
            f" {info['user_id']}, Chatroom ID: {info['chatroom_id']})",
        )
      else:
        channel_info = {
            "slug": current_channel,
            "chatroom_id": None,
            "channel_id": None,
            "user_id": None,
        }
        bot_logs.insert(0, f"[HATA] '{current_channel}' kanal bilgileri alınamadı!")

    # === TUR BAZLI SİSTEM ===
    # Toplam pencere süresi = girilen delay * 10
    # Bu süre içinde tüm hesaplar 1'er kez mesaj atacak
    active_tokens = list(config["tokens"])
    delay_sec = float(config["delay"])
    window_sec = delay_sec * 10  # Toplam pencere süresi
    token_count = len(active_tokens)

    if token_count == 0:
      time.sleep(1)
      continue

    # Tokenları karıştır (rastgele sıra)
    random.shuffle(active_tokens)

    # Her token arasındaki ortalama bekleme süresini hesapla
    # Pencereyi token sayısına böl, ama ufak rastgelelik ekle
    avg_gap = window_sec / token_count

    bot_logs.insert(
        0,
        f"[SİSTEM] Yeni tur başladı: {token_count} hesap, {window_sec:.0f}sn pencere"
    )

    for i, selected_token in enumerate(active_tokens):
      if not is_running:
        break

      selected_message = random.choice(config["messages"])
      chatroom_id = channel_info.get("chatroom_id")

      if not chatroom_id:
        bot_logs.insert(0, f"[HATA] [{current_channel}] Chatroom ID bulunamadı!")
        time.sleep(2)
        break

      # Kick.com v2 mesaj gönderme endpoint'i (Doğru endpoint: POST https://kick.com/api/v2/messages/send/{chatroom_id})
      url = f"https://kick.com/api/v2/messages/send/{chatroom_id}"
      
      payload = {
          "content": selected_message,
          "type": "message"
      }

      headers = {
          "accept": "application/json, text/plain, */*",
          "authorization": f"Bearer {selected_token}",
          "content-type": "application/json",
          "origin": "https://kick.com",
          "referer": f"https://kick.com/{current_channel}",
          "user-agent": (
              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
              " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
          ),
          "x-app-platform": "web",
      }

      try:
        response = crequests.post(
            url,
            headers=headers,
            json=payload,
            impersonate="chrome120",
            timeout=10,
        )
        token_masked = selected_token[:10] + "..."
        if 200 <= response.status_code < 300:
          log_msg = f"[BAŞARILI] [{current_channel}] Hesap: {token_masked} | Mesaj: '{selected_message}' | Kod: {response.status_code}"
        elif "FOLLOWERS_ONLY_ERROR" in response.text:
          log_msg = (
              f"[TAKİPÇİ SOHBETİ UYARISI] [{current_channel}] Hesap:"
              f" {selected_token} - Bu kanalda Sadece Takipçiler sohbet modu aktif!"
          )
        else:
          log_msg = f"[HATA] [{current_channel}] Hesap: {selected_token} | Kod: {response.status_code} - {response.text}"
      except Exception as e:
        log_msg = f"[BAĞLANTI HATASI] {str(e)}"

      bot_logs.insert(0, log_msg)
      if len(bot_logs) > 50:
        bot_logs.pop()

      # Son token değilse, sonraki mesaja kadar rastgele bekle
      if i < token_count - 1 and is_running:
        # avg_gap etrafında %50 sapma ile rastgele bekleme
        wait = random.uniform(avg_gap * 0.5, avg_gap * 1.5)
        # Çok kısa olmasın, minimum 2 saniye
        wait = max(2.0, wait)
        time.sleep(wait)


@app.route("/")
def index():
  if session.get("logged_in"):
    if session.get("is_admin"):
      return render_template("index.html", config=config)
    
    username = session.get("username")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT points FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    points = row[0] if row else 0
    conn.close()
    return render_template("games.html", points=points, logged_in=True, username=username)

  trigger = request.args.get("trigger", "")
  return render_template("games.html", trigger=trigger, logged_in=False, username="")


@app.route("/gate")
def gate():
  if session.get("logged_in") and session.get("is_admin"):
    return redirect(url_for("index"))
  return redirect(url_for("index", trigger="admin_auth"))


@app.route("/arcade")
def arcade():
  """Admin panelinden Arcade portalına dönerken admin oturumunu arcade moduna alır."""
  if session.get("logged_in") and session.get("is_admin"):
    session["is_admin"] = False
  return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
  if request.method == "GET":
    if session.get("logged_in"):
      return redirect(url_for("index"))
    return redirect(url_for("gate"))
  
  if request.is_json:
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
  else:
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

  if not username or not password:
    return jsonify({"success": False, "message": "Kullanıcı adı veya şifre boş bırakılamaz!"}), 400

  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
  row = cursor.fetchone()
  conn.close()

  if row and check_password_hash(row[0], password):
    session["logged_in"] = True
    session["username"] = username
    session["is_admin"] = False
    return jsonify({"success": True, "message": "Giriş başarılı!"})
  
  return jsonify({"success": False, "message": "Kullanıcı adı veya şifre hatalı!"}), 401


@app.route("/register", methods=["POST"])
def register():
  if request.is_json:
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
  else:
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

  if not username or not password:
    return jsonify({"success": False, "message": "Kullanıcı adı veya şifre boş bırakılamaz!"}), 400

  if len(password) < 6:
    return jsonify({"success": False, "message": "Şifre en az 6 karakter olmalıdır!"}), 400

  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  try:
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    if cursor.fetchone():
      conn.close()
      return jsonify({"success": False, "message": "Bu kullanıcı adı zaten alınmış!"}), 409
    
    hashed = generate_password_hash(password)
    cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, hashed))
    conn.commit()
    conn.close()
    
    session["logged_in"] = True
    session["username"] = username
    session["is_admin"] = False
    return jsonify({"success": True, "message": "Kayıt başarılı! Giriş yapıldı."})
  except Exception as e:
    if conn:
      conn.close()
    return jsonify({"success": False, "message": f"Kayıt esnasında bir hata oluştu: {str(e)}"}), 500


@app.route("/admin-login", methods=["POST"])
def admin_login():
  if request.is_json:
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
  else:
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

  if not username or not password:
    return jsonify({"success": False, "message": "Kullanıcı adı veya şifre boş bırakılamaz!"}), 400

  if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
    session["logged_in"] = True
    session["username"] = username
    session["is_admin"] = True
    return jsonify({"success": True, "message": "Yönetici girişi başarılı!"})
  
  return jsonify({"success": False, "message": "Yönetici şifresi veya kullanıcı adı hatalı!"}), 401


@app.route("/get-banned-users/<channel_name>", methods=["GET"])
def get_banned_users(channel_name):
  """Sefoge kanalından banlanan kullanıcıları döndür (mock data şu an)"""
  try:
    # TODO: Gerçek Kick banlanan verisi entegre edilebilir
    # Şu an boş döndür, frontend localStorage'dan gösterecek
    
    return jsonify({
      "success": True,
      "channel": channel_name,
      "banned_users": [],
      "total": 0,
      "note": "Banlananlar moderasyon panelinde localStorage'da kaydediliyor"
    })
      
  except Exception as e:
    return jsonify({
      "success": False,
      "message": f"Hata: {str(e)}"
    }), 500


@app.route("/update-points", methods=["POST"])
def update_points():
  if not session.get("logged_in"):
    return jsonify({"success": False, "message": "Puan eklemek için giriş yapmalısınız!"}), 401

  if request.is_json:
    data = request.get_json() or {}
  else:
    data = request.form

  try:
    points_to_add = int(data.get("points", 0))
  except ValueError:
    return jsonify({"success": False, "message": "Geçersiz puan değeri!"}), 400

  if points_to_add <= 0:
    return jsonify({"success": False, "message": "Eklenecek puan sıfırdan büyük olmalıdır!"}), 400

  username = session.get("username")
  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  try:
    cursor.execute("UPDATE users SET points = points + ? WHERE username = ?", (points_to_add, username))
    conn.commit()
    
    cursor.execute("SELECT points FROM users WHERE username = ?", (username,))
    new_points = cursor.fetchone()[0]
    conn.close()
    return jsonify({"success": True, "new_points": new_points})
  except Exception as e:
    if conn:
      conn.close()
    return jsonify({"success": False, "message": f"Hata: {str(e)}"}), 500


@app.route("/get-youtube-latest/<channel_name>")
def get_youtube_latest(channel_name):
  """YouTube kanalından en yeni videoyu al"""
  try:
    if channel_name.lower() == 'sefoge':
      # Sefoge'nin son Shorts videosu
      return jsonify({
        "success": True,
        "video_id": "yIvJtp4VDl4",
        "title": "Sefoge Yeni Shorts",
        "url": "https://www.youtube.com/shorts/yIvJtp4VDl4",
        "embed_url": "https://www.youtube.com/embed/yIvJtp4VDl4"
      })
    
    return jsonify({"success": False, "message": "Channel not supported"}), 400
      
  except Exception as e:
    return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500


@app.route("/logout")
def logout():
  session.clear()
  return redirect(url_for("index"))


@app.route("/update", methods=["POST"])
@login_required
def update_config():
  global config
  channel_raw = request.form.get("channel", "sefoge")
  tokens_raw = request.form.get("tokens", "")
  messages_raw = request.form.get("messages", "")
  delay_raw = request.form.get("delay", "4")

  config["channel"] = channel_raw.strip().lower()
  config["tokens"] = [t.strip() for t in tokens_raw.split("\n") if t.strip()]
  config["messages"] = [
      m.strip() for m in messages_raw.split("\n") if m.strip()
  ]
  try:
    config["delay"] = float(delay_raw)
  except ValueError:
    config["delay"] = 4.0

  save_config(config)

  return jsonify({"success": True})


@app.route("/get-chatroom/<channel_slug>")
def get_chatroom(channel_slug):
    token = config["tokens"][0] if config.get("tokens") else ""
    helper = KickFollowAutomation(token)
    info = helper.get_channel_info(channel_slug.strip().lower())
    return jsonify(info)


@app.route("/start", methods=["POST"])
@login_required
def start_bot():
  global bot_thread, is_running
  if not is_running:
    is_running = True
    bot_thread = threading.Thread(target=bot_worker, daemon=True)
    bot_thread.start()
    return jsonify({
        "message": (
            f"Bot '{config.get('channel', 'sefoge')}' kanalı için başlatıldı!"
        )
    })
  return jsonify({"message": "Bot zaten çalışıyor!"})


@app.route("/stop", methods=["POST"])
@login_required
def stop_bot():
  global is_running
  is_running = False
  return jsonify({"message": "Bot durduruldu!"})


@app.route("/logs")
@login_required
def get_logs():
  return jsonify({
      "running": is_running,
      "channel": config.get("channel", ""),
      "logs": bot_logs,
  })


# =============================================
# HESAP OLUŞTURMA FONKSİYONLARI
# =============================================

def account_worker(count, delay):
  """Arka planda hesap oluşturma işlemi."""
  global is_creating_accounts, created_accounts

  def log_cb(msg):
    account_logs.insert(0, msg)
    if len(account_logs) > 100:
      account_logs.pop()

  log_cb(f"[SİSTEM] {count} adet hesap oluşturma başlatıldı...")

  for i in range(count):
    if not is_creating_accounts:
      log_cb("[SİSTEM] Hesap oluşturma kullanıcı tarafından durduruldu.")
      break

    log_cb(f"\n[SİSTEM] ── Hesap {i + 1}/{count} ──")
    creator = KickAccountCreator(log_callback=log_cb)
    result = creator.create_account()

    if result.get("success"):
      created_accounts.append(result)
      # Başarılı hesabın tokenını otomatik olarak config'e ekle
      if result.get("token"):
        config["tokens"].append(result["token"])
        save_config(config)
        log_cb(f"[SİSTEM] ✅ Token otomatik olarak bot token listesine eklendi ve kaydedildi!")

    if i < count - 1 and is_creating_accounts:
      log_cb(f"[SİSTEM] Sonraki hesap için {delay} saniye bekleniyor...")
      for _ in range(int(delay)):
        if not is_creating_accounts:
          break
        time.sleep(1)

  successful = sum(1 for r in created_accounts if r.get("success"))
  log_cb(f"\n[ÖZET] Toplam {successful} hesap başarıyla oluşturuldu.")
  is_creating_accounts = False


@app.route("/create-account", methods=["POST"])
@login_required
def create_account():
  global account_thread, is_creating_accounts

  if is_creating_accounts:
    return jsonify({"message": "Hesap oluşturma zaten devam ediyor!"})

  count = int(request.form.get("account_count", 1))
  delay = int(request.form.get("account_delay", 10))

  if count < 1:
    count = 1
  if count > 20:
    count = 20

  is_creating_accounts = True
  account_thread = threading.Thread(
      target=account_worker, args=(count, delay), daemon=True
  )
  account_thread.start()

  return jsonify({
      "message": f"{count} adet hesap oluşturma başlatıldı!"
  })


@app.route("/stop-account", methods=["POST"])
@login_required
def stop_account_creation():
  global is_creating_accounts
  is_creating_accounts = False
  return jsonify({"message": "Hesap oluşturma durduruldu!"})


@app.route("/account-logs")
@login_required
def get_account_logs():
  return jsonify({
      "creating": is_creating_accounts,
      "logs": account_logs,
      "accounts": [
          {
              "email": a.get("email", ""),
              "username": a.get("username", ""),
              "password": a.get("password", ""),
              "token": (a.get("token", "") or "")[:30] + "..." if a.get("token") else "Yok",
              "verified": a.get("verified", False),
          }
          for a in created_accounts
      ],
  })


@app.route("/clear-account-logs", methods=["POST"])
@login_required
def clear_account_logs():
  global account_logs
  account_logs.clear()
  return jsonify({"message": "Loglar temizlendi!"})


# =============================================
# TOKEN TEST FONKSİYONLARI
# =============================================
@app.route("/test-token", methods=["POST"])
@login_required
def test_token():
    """Gönderilen token'ın Kick.com'da geçerli olup olmadığını kontrol eder."""
    token = request.json.get("token", "").strip()
    if not token:
        return jsonify({"valid": False, "message": "Token boş"})
    
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {token}",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    
    try:
        resp = crequests.get("https://kick.com/api/v1/user", headers=headers, impersonate="chrome120", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            username = data.get("username", "Bilinmiyor")
            return jsonify({"valid": True, "message": f"Aktif ({username})", "username": username})
        elif resp.status_code == 401:
            return jsonify({"valid": False, "message": "Geçersiz (Patlamış)"})
        else:
            return jsonify({"valid": False, "message": f"Geçersiz (HTTP {resp.status_code})"})
    except Exception as e:
        return jsonify({"valid": False, "message": f"Bağlantı Hatası"})


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 5000))
  app.run(host="0.0.0.0", port=port, debug=False)