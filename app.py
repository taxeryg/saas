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

import os

ADMIN_USERNAME = "taxer"
ADMIN_PASSWORD = "babaproxx123"
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
  """Kick API ile mesaj gönderme ve takip işlemleri - robust version"""

  def __init__(self, token):
    self.token = token
    self.token_masked = token[:10] + "..." if token else "NO_TOKEN"
    self.session = None
    self._init_session()

  def _init_session(self):
    """Session oluştur - curl_cffi veya requests"""
    try:
      self.session = crequests.Session()
    except Exception as e:
      print(f"[DEBUG] Session init error: {e}")
      self.session = crequests.Session()

  def _get_headers(self, include_auth=False, include_token=False):
    """Generic headers - Windows/Linux uyumlu"""
    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "origin": "https://kick.com",
    }
    if include_token and self.token:
      headers["authorization"] = f"Bearer {self.token}"
    if include_auth:
      headers["content-type"] = "application/json"
    return headers

  def get_channel_info(self, channel_slug):
    """Kanal bilgileri çek - v2 ve v1 endpoint'lerini dene"""
    print(f"[DEBUG] Fetching channel info for: {channel_slug}")
    
    for attempt in range(3):
      for endpoint_version in ["v2", "v1"]:
        try:
          url = f"https://kick.com/api/{endpoint_version}/channels/{channel_slug}"
          print(f"[DEBUG] Attempt {attempt + 1}/3 - {endpoint_version}: {url}")
          
          resp = crequests.get(url, headers=self._get_headers(), timeout=10)
          print(f"[DEBUG] Status: {resp.status_code}")
          print(f"[DEBUG] Response length: {len(resp.text)}")
          
          if resp.status_code == 200:
            try:
              data = resp.json()
              print(f"[DEBUG] JSON parsed OK. Top-level keys: {list(data.keys())}")
              print(f"[DEBUG] Full response (first 500 chars): {str(data)[:500]}")
              
              # Chatroom ID'sini çeşitli yerlerden ara
              chatroom_id = (
                  data.get("chatroom", {}).get("id") or
                  data.get("chatroom_id") or
                  data.get("id")
              )
              
              print(f"[DEBUG] Extracted chatroom_id: {chatroom_id}")
              
              if chatroom_id:
                print(f"[DEBUG] ✓ Success! Found chatroom: {chatroom_id}")
                return {
                    "success": True,
                    "chatroom_id": chatroom_id,
                    "channel_id": data.get("id"),
                    "user_id": data.get("user_id"),
                    "slug": channel_slug,
                }
              else:
                print(f"[DEBUG] No chatroom_id found in response")
            except Exception as je:
              print(f"[DEBUG] JSON parse error: {je}")
          else:
            print(f"[DEBUG] Non-200 status: {resp.status_code}")
            print(f"[DEBUG] Response text (first 200 chars): {resp.text[:200]}")
        except Exception as e:
          print(f"[DEBUG] {endpoint_version} Exception: {type(e).__name__}: {str(e)[:150]}")
          time.sleep(1)
          continue
      
      if attempt < 2:
        print(f"[DEBUG] Waiting 2s before retry...")
        time.sleep(2)
    
    print(f"[DEBUG] ✗ Failed after all retries for {channel_slug}")
    return {"success": False, "chatroom_id": None, "channel_id": None, "user_id": None}

  def send_message(self, chatroom_id, message):
    """Doğrudan mesaj gönder"""
    print(f"[DEBUG] Sending message to chatroom {chatroom_id}")
    
    url = f"https://kick.com/api/v2/messages/send/{chatroom_id}"
    payload = {"content": message, "type": "message"}
    
    headers = self._get_headers(include_auth=True, include_token=True)
    
    for attempt in range(2):
      try:
        print(f"[DEBUG] POST {url} (attempt {attempt + 1}/2)")
        resp = crequests.post(url, headers=headers, json=payload, timeout=10)
        print(f"[DEBUG] Response: {resp.status_code}")
        
        if 200 <= resp.status_code < 300:
          print(f"[DEBUG] ✓ Message sent!")
          return True
        else:
          print(f"[DEBUG] Error response: {resp.text[:200]}")
      except Exception as e:
        print(f"[DEBUG] Exception: {str(e)[:100]}")
        if attempt < 1:
          time.sleep(1)
    
    print(f"[DEBUG] ✗ Failed to send message")
    return False

  def follow_user(self, broadcaster_identifier):
    """Kullanıcı takip et"""
    print(f"[DEBUG] Following: {broadcaster_identifier}")
    
    for endpoint_version in ["v2", "v1"]:
      try:
        url = f"https://kick.com/api/{endpoint_version}/channels/{broadcaster_identifier}/follow"
        print(f"[DEBUG] POST {url}")
        
        headers = self._get_headers(include_auth=True, include_token=True)
        resp = crequests.post(url, headers=headers, json={}, timeout=8)
        print(f"[DEBUG] Status: {resp.status_code}")
        
        if resp.status_code in (200, 201, 204):
          return True, f"[✓] Followed {broadcaster_identifier}"
        elif resp.status_code in (400, 409) or "already" in resp.text.lower():
          return True, f"[i] Already following {broadcaster_identifier}"
      except Exception as e:
        print(f"[DEBUG] {endpoint_version} error: {str(e)[:100]}")
        continue
    
    return False, f"[✗] Failed to follow {broadcaster_identifier}"


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
  """Mesaj gönderme botu - robust versiyon"""
  global is_running
  current_channel = None
  channel_info = {}
  refresh_counter = 0

  while is_running:
    try:
      # Config kontrol
      if not config.get("tokens") or not config.get("messages"):
        bot_logs.insert(0, "[HATA] Token veya mesaj listesi boş!")
        time.sleep(5)
        continue

      target_channel = config.get("channel", "sefoge").strip().lower()
      if not target_channel:
        bot_logs.insert(0, "[HATA] Hedef kanal adı boş!")
        time.sleep(5)
        continue

      # Kanal bilgilerini her 60 saniyede veya kanal değişikliğinde yenile
      if current_channel != target_channel or refresh_counter % 120 == 0:
        current_channel = target_channel
        bot = KickFollowAutomation(config["tokens"][0])
        channel_info = bot.get_channel_info(current_channel)
        
        if channel_info.get("success"):
          bot_logs.insert(0, f"[SİSTEM] Channel info fetched: {current_channel} (Chatroom: {channel_info['chatroom_id']})")
        else:
          bot_logs.insert(0, f"[HATA] Could not fetch channel info for {current_channel}")
          time.sleep(5)
          refresh_counter += 1
          continue

      # Mesaj gönderme loop
      tokens = list(config.get("tokens", []))
      messages = list(config.get("messages", []))
      delay = float(config.get("delay", 1))
      window = delay * 10
      
      if not tokens or not messages:
        time.sleep(5)
        refresh_counter += 1
        continue

      random.shuffle(tokens)
      chatroom_id = channel_info.get("chatroom_id")
      
      if not chatroom_id:
        bot_logs.insert(0, "[HATA] No chatroom_id available!")
        time.sleep(5)
        refresh_counter += 1
        continue

      bot_logs.insert(0, f"[SİSTEM] Starting round: {len(tokens)} accounts, {window:.0f}s window")
      avg_gap = window / len(tokens)

      for i, token in enumerate(tokens):
        if not is_running:
          break

        message = random.choice(messages)
        token_display = token[:10] + "..."
        
        bot = KickFollowAutomation(token)
        success = bot.send_message(chatroom_id, message)
        
        if success:
          bot_logs.insert(0, f"[✓] {token_display} sent: {message[:50]}")
        else:
          bot_logs.insert(0, f"[✗] {token_display} failed to send message")
        
        # Keep logs size manageable
        if len(bot_logs) > 100:
          bot_logs.pop()

        # Wait between messages
        if i < len(tokens) - 1 and is_running:
          wait = random.uniform(avg_gap * 0.5, avg_gap * 1.5)
          wait = max(1.0, wait)
          time.sleep(wait)

      refresh_counter += 1

    except Exception as e:
      bot_logs.insert(0, f"[KRITIK HATA] {str(e)[:100]}")
      time.sleep(5)
      refresh_counter += 1
      continue


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
    try:
        token = config["tokens"][0] if config.get("tokens") else ""
        helper = KickFollowAutomation(token)
        info = helper.get_channel_info(channel_slug.strip().lower())
        return jsonify(info)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


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
            "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    
    try:
        resp = crequests.get("https://kick.com/api/v1/user", headers=headers, timeout=10)
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
  app.run(host="0.0.0.0", port=5000, debug=True)