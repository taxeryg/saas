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

def safe_request(method, url, **kwargs):
  """curl_cffi ile güvenli istek yapar, chrome120 hatası durumunda chrome'a ve ardından standart requests'e düşer."""
  method_name = method.lower()
  func = getattr(crequests, method_name, None)
  if func is None:
    raise AttributeError(f"crequests has no method '{method_name}'")
  try:
    return func(url, **kwargs)
  except Exception as e:
    if "impersonate" in kwargs:
      kwargs["impersonate"] = "chrome"
      try:
        return func(url, **kwargs)
      except Exception:
        kwargs.pop("impersonate", None)
        try:
          return func(url, **kwargs)
        except Exception as final_err:
          raise final_err
    raise e

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

# Çoklu Bot global durumları
bots_state = {
    "1": {"is_running": False, "thread": None, "logs": []},
    "2": {"is_running": False, "thread": None, "logs": []},
    "3": {"is_running": False, "thread": None, "logs": []}
}

# Hesap oluşturma için global değişkenler
account_thread = None
is_creating_accounts = False
account_logs = []
created_accounts = []

CONFIG_FILE = "config.json"

DEFAULT_CONFIG = {
    "bots": {
        "1": {
            "channel": "sefoge",
            "chatroom_id": "",
            "proxy": "",
            "tokens": [
                "380813590|MlQJsOlYlAjKrEvnTWkswKdT0WntbptbgUiVUHrD",
                "409791057|0gKGwsYCKVxI5fZmcJIA1EqWrKfu5Z4LYhtHR581",
            ],
            "messages": ["Selam!", "Nasılsınız?", "Harika yayın!", "Merhaba!"],
            "delay": 4.0,
        },
        "2": {
            "channel": "sefoge",
            "chatroom_id": "",
            "proxy": "",
            "tokens": [],
            "messages": ["Selam!", "Nasılsınız?", "Harika yayın!", "Merhaba!"],
            "delay": 4.0,
        },
        "3": {
            "channel": "sefoge",
            "chatroom_id": "",
            "proxy": "",
            "tokens": [],
            "messages": ["Selam!", "Nasılsınız?", "Harika yayın!", "Merhaba!"],
            "delay": 4.0,
        }
    }
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "bots" in data:
                    for b_id in ["1", "2", "3"]:
                        if b_id not in data["bots"]:
                            data["bots"][b_id] = {
                                "channel": "sefoge",
                                "chatroom_id": "",
                                "proxy": "",
                                "tokens": [],
                                "messages": ["Selam!", "Nasılsınız?", "Harika yayın!"],
                                "delay": 4.0
                            }
                    return data
                else:
                    migrated = {
                        "bots": {
                            "1": {
                                "channel": data.get("channel", "sefoge"),
                                "chatroom_id": data.get("chatroom_id", ""),
                                "proxy": data.get("proxy", ""),
                                "tokens": data.get("tokens", []),
                                "messages": data.get("messages", []),
                                "delay": data.get("delay", 4.0)
                            },
                            "2": {
                                "channel": "sefoge",
                                "chatroom_id": "",
                                "proxy": "",
                                "tokens": [],
                                "messages": ["Selam!", "Nasılsınız?", "Harika yayın!"],
                                "delay": 4.0
                            },
                            "3": {
                                "channel": "sefoge",
                                "chatroom_id": "",
                                "proxy": "",
                                "tokens": [],
                                "messages": ["Selam!", "Nasılsınız?", "Harika yayın!"],
                                "delay": 4.0
                            }
                        }
                    }
                    save_config(migrated)
                    return migrated
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

  def __init__(self, token, proxy=None):
    self.token = token
    self.token_masked = token[:10] + "..." if token else "NO_TOKEN"
    self.proxy = proxy

  def get_channel_info(self, channel_slug):
    """Kanal kullanıcı ID (user_id), kanal ID (channel_id) ve sohbet (chatroom_id) verilerini çeker."""
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-ch-ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    }
    endpoints = [
        f"https://kick.com/api/v2/channels/{channel_slug}",
        f"https://kick.com/api/v1/channels/{channel_slug}",
    ]
    proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
    for url in endpoints:
      try:
        resp = safe_request(
            "GET", url, headers=headers, impersonate="chrome120", timeout=8, proxies=proxies
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

    proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
    for url in endpoints:
      try:
        response = safe_request(
            "POST", url, headers=headers, json={}, impersonate="chrome120", timeout=8, proxies=proxies
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


def bot_worker(bot_id):
  current_channel = None
  channel_info = {}

  while bots_state[bot_id]["is_running"]:
    bot_cfg = config["bots"][bot_id]
    if not bot_cfg.get("tokens") or not bot_cfg.get("messages"):
      bots_state[bot_id]["logs"].insert(
          0, "[HATA] Token veya mesaj listesi boş! Bot durduruldu."
      )
      bots_state[bot_id]["is_running"] = False
      break

    target_chatroom_id = bot_cfg.get("chatroom_id", "").strip()
    target_channel = bot_cfg.get("channel", "kick").strip().lower() or "kick"

    if not target_chatroom_id:
      bots_state[bot_id]["logs"].insert(0, "[HATA] Chatroom ID boş! Ayarlardan Chatroom ID girin.")
      bots_state[bot_id]["is_running"] = False
      break

    # Chatroom ID değiştiyse channel_info'yu güncelle
    if channel_info.get("chatroom_id") != target_chatroom_id:
      current_channel = target_channel
      channel_info = {
          "slug": target_channel,
          "chatroom_id": target_chatroom_id,
          "channel_id": None,
          "user_id": None,
      }
      bots_state[bot_id]["logs"].insert(
          0,
          f"[SİSTEM] Chatroom ID: {target_chatroom_id} | Kanal: {target_channel}",
      )
    else:
      current_channel = target_channel

    # === TUR BAZLI SİSTEM ===
    active_tokens = list(bot_cfg["tokens"])
    delay_sec = float(bot_cfg["delay"])
    window_sec = delay_sec * 10  # Toplam pencere süresi
    token_count = len(active_tokens)

    if token_count == 0:
      time.sleep(1)
      continue

    # Tokenları karıştır (rastgele sıra)
    random.shuffle(active_tokens)
    avg_gap = window_sec / token_count

    bots_state[bot_id]["logs"].insert(
        0,
        f"[SİSTEM] Yeni tur başladı: {token_count} hesap, {window_sec:.0f}sn pencere"
    )

    for i, selected_token in enumerate(active_tokens):
      if not bots_state[bot_id]["is_running"]:
        break

      selected_message = random.choice(bot_cfg["messages"])
      chatroom_id = channel_info.get("chatroom_id")

      if not chatroom_id:
        bots_state[bot_id]["logs"].insert(0, f"[HATA] [{current_channel}] Chatroom ID bulunamadı!")
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
        proxy_val = bot_cfg.get("proxy", "").strip()
        proxies = {"http": proxy_val, "https": proxy_val} if proxy_val else None
        response = safe_request(
            "POST",
            url,
            headers=headers,
            json=payload,
            impersonate="chrome120",
            timeout=10,
            proxies=proxies,
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

      bots_state[bot_id]["logs"].insert(0, log_msg)
      if len(bots_state[bot_id]["logs"]) > 50:
        bots_state[bot_id]["logs"].pop()

      # Son token değilse, sonraki mesaja kadar rastgele bekle
      if i < token_count - 1 and bots_state[bot_id]["is_running"]:
        # avg_gap etrafında %50 sapma ile rastgele bekleme
        wait = random.uniform(avg_gap * 0.5, avg_gap * 1.5)
        # Çok kısa olmasın, minimum 2 saniye
        wait = max(2.0, wait)
        time.sleep(wait)


@app.route("/")
def index():
  if session.get("logged_in"):
    return render_template("index.html", config=config)
  return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
  if request.method == "GET":
    if session.get("logged_in"):
      return redirect(url_for("index"))
    return render_template("login.html")
  
  if request.is_json:
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
  else:
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

  if not username or not password:
    if request.is_json:
      return jsonify({"success": False, "message": "Kullanıcı adı veya şifre boş bırakılamaz!"}), 400
    return render_template("login.html", error="Kullanıcı adı veya şifre boş bırakılamaz!")

  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  cursor.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
  row = cursor.fetchone()
  conn.close()

  if row and check_password_hash(row[0], password):
    session["logged_in"] = True
    session["username"] = username
    session["is_admin"] = True
    if request.is_json:
      return jsonify({"success": True, "message": "Giriş başarılı!"})
    return redirect(url_for("index"))
  
  if request.is_json:
    return jsonify({"success": False, "message": "Kullanıcı adı veya şifre hatalı!"}), 401
  return render_template("login.html", error="Kullanıcı adı veya şifre hatalı!")


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
    if request.is_json:
      return jsonify({"success": False, "message": "Kullanıcı adı veya şifre boş bırakılamaz!"}), 400
    return render_template("login.html", error="Kullanıcı adı veya şifre boş bırakılamaz!")

  if len(password) < 6:
    if request.is_json:
      return jsonify({"success": False, "message": "Şifre en az 6 karakter olmalıdır!"}), 400
    return render_template("login.html", error="Şifre en az 6 karakter olmalıdır!")

  conn = sqlite3.connect(DB_FILE)
  cursor = conn.cursor()
  try:
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    if cursor.fetchone():
      conn.close()
      if request.is_json:
        return jsonify({"success": False, "message": "Bu kullanıcı adı zaten alınmış!"}), 409
      return render_template("login.html", error="Bu kullanıcı adı zaten alınmış!")
    
    hashed = generate_password_hash(password)
    cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, hashed))
    conn.commit()
    conn.close()
    
    session["logged_in"] = True
    session["username"] = username
    session["is_admin"] = True
    if request.is_json:
      return jsonify({"success": True, "message": "Kayıt başarılı! Giriş yapıldı."})
    return redirect(url_for("index"))
  except Exception as e:
    if conn:
      conn.close()
    if request.is_json:
      return jsonify({"success": False, "message": f"Kayıt esnasında bir hata oluştu: {str(e)}"}), 500
    return render_template("login.html", error=f"Kayıt esnasında bir hata oluştu: {str(e)}")


@app.route("/logout")
def logout():
  session.clear()
  return redirect(url_for("index"))


@app.route("/update/<bot_id>", methods=["POST"])
@login_required
def update_config(bot_id):
  global config
  if bot_id not in config["bots"]:
    return jsonify({"success": False, "message": "Geçersiz Bot ID!"}), 400

  channel_raw = request.form.get("channel", "").strip().lower() or config["bots"][bot_id].get("channel", "kick")
  chatroom_id_raw = request.form.get("chatroom_id", "")
  proxy_raw = request.form.get("proxy", "")
  tokens_raw = request.form.get("tokens", "")
  messages_raw = request.form.get("messages", "")
  delay_raw = request.form.get("delay", "4")

  bot_cfg = config["bots"][bot_id]
  bot_cfg["channel"] = channel_raw
  bot_cfg["chatroom_id"] = chatroom_id_raw.strip()
  bot_cfg["proxy"] = proxy_raw.strip()
  bot_cfg["tokens"] = [t.strip() for t in tokens_raw.split("\n") if t.strip()]
  bot_cfg["messages"] = [
      m.strip() for m in messages_raw.split("\n") if m.strip()
  ]
  try:
    bot_cfg["delay"] = float(delay_raw)
  except ValueError:
    bot_cfg["delay"] = 4.0

  save_config(config)

  return jsonify({"success": True})


@app.route("/get-chatroom/<channel_slug>")
def get_chatroom(channel_slug):
    bot_id = request.args.get("bot_id", "1")
    bot_cfg = config["bots"].get(bot_id, config["bots"]["1"])
    
    if bot_cfg.get("channel", "").strip().lower() == channel_slug.strip().lower() and bot_cfg.get("chatroom_id"):
        return jsonify({
            "success": True,
            "chatroom_id": bot_cfg.get("chatroom_id"),
            "slug": channel_slug
        })

    token = bot_cfg["tokens"][0] if bot_cfg.get("tokens") else ""
    helper = KickFollowAutomation(token, proxy=bot_cfg.get("proxy"))
    info = helper.get_channel_info(channel_slug.strip().lower())
    return jsonify(info)


@app.route("/start/<bot_id>", methods=["POST"])
@login_required
def start_bot(bot_id):
  if bot_id not in bots_state:
    return jsonify({"message": "Geçersiz Bot ID!"}), 400
    
  state = bots_state[bot_id]
  if not state["is_running"]:
    state["is_running"] = True
    state["thread"] = threading.Thread(target=bot_worker, args=(bot_id,), daemon=True)
    state["thread"].start()
    bot_cfg = config["bots"][bot_id]
    return jsonify({
        "message": (
            f"Bot #{bot_id} '{bot_cfg.get('channel', 'sefoge')}' kanalı için başlatıldı!"
        )
    })
  return jsonify({"message": f"Bot #{bot_id} zaten çalışıyor!"})


@app.route("/stop/<bot_id>", methods=["POST"])
@login_required
def stop_bot(bot_id):
  if bot_id not in bots_state:
    return jsonify({"message": "Geçersiz Bot ID!"}), 400
  bots_state[bot_id]["is_running"] = False
  return jsonify({"message": f"Bot #{bot_id} durduruldu!"})


@app.route("/logs/<bot_id>")
@login_required
def get_logs(bot_id):
  if bot_id not in bots_state:
    return jsonify({"message": "Geçersiz Bot ID!"}), 400
  
  bot_cfg = config["bots"][bot_id]
  return jsonify({
      "running": bots_state[bot_id]["is_running"],
      "channel": bot_cfg.get("channel", ""),
      "logs": bots_state[bot_id]["logs"],
  })


# =============================================
# HESAP OLUŞTURMA FONKSİYONLARI
# =============================================

def account_worker(count, delay, target_bot):
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
    
    proxy_val = ""
    if target_bot in config["bots"]:
      proxy_val = config["bots"][target_bot].get("proxy", "")
    elif config["bots"].get("1"):
      proxy_val = config["bots"]["1"].get("proxy", "")

    creator = KickAccountCreator(log_callback=log_cb, proxy=proxy_val)
    result = creator.create_account()

    if result.get("success"):
      created_accounts.append(result)
      if result.get("token"):
        added_bots = []
        if target_bot == "all":
          for b_id in config["bots"]:
            config["bots"][b_id]["tokens"].append(result["token"])
            added_bots.append(f"Bot #{b_id}")
        elif target_bot in config["bots"]:
          config["bots"][target_bot]["tokens"].append(result["token"])
          added_bots.append(f"Bot #{target_bot}")
        
        if added_bots:
          save_config(config)
          log_cb(f"[SİSTEM] ✅ Token otomatik olarak {', '.join(added_bots)} listesine eklendi ve kaydedildi!")

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
  target_bot = request.form.get("target_bot", "1")

  if count < 1:
    count = 1
  if count > 20:
    count = 20

  is_creating_accounts = True
  account_thread = threading.Thread(
      target=account_worker, args=(count, delay, target_bot), daemon=True
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
    bot_id = request.json.get("bot_id", "1")
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
        proxy_val = ""
        if "bots" in config and bot_id in config["bots"]:
            proxy_val = config["bots"][bot_id].get("proxy", "").strip()
        proxies = {"http": proxy_val, "https": proxy_val} if proxy_val else None
        resp = safe_request("GET", "https://kick.com/api/v1/user", headers=headers, impersonate="chrome120", timeout=10, proxies=proxies)
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