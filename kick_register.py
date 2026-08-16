"""
Kick.com otomatik hesap oluşturma modülü.
Kick kayıt akışı çok aşamalıdır:
  1. POST /api/v1/signup/agreed-terms
  2. POST /api/v1/signup/username
  3. POST /api/v1/signup/send/email
  4. POST /api/v1/signup/verify/code
  5. POST /api/v1/signup/complete
"""

import random
import re
import string
import time
from urllib.parse import unquote

try:
    from curl_cffi import requests as crequests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as crequests
    HAS_CURL_CFFI = False

from temp_email import TempEmailClient


KICK_BASE = "https://kick.com"
KICK_API_V1 = "https://kick.com/api/v1"


def _random_username(length=10):
    prefixes = [
        "user", "cool", "pro", "gamer", "kick",
        "live", "stream", "play", "fan", "star",
        "turk", "ace", "neo", "max", "tr",
    ]
    prefix = random.choice(prefixes)
    suffix = "".join(random.choices(string.digits + string.ascii_lowercase, k=length - len(prefix)))
    return f"{prefix}{suffix}"


def _random_password(length=16):
    lower = string.ascii_lowercase
    upper = string.ascii_uppercase
    digits = string.digits
    special = "!@#$%&*"
    password = [
        random.choice(lower), random.choice(lower),
        random.choice(upper), random.choice(upper),
        random.choice(digits), random.choice(digits),
        random.choice(special),
    ]
    all_chars = lower + upper + digits + special
    password += [random.choice(all_chars) for _ in range(length - len(password))]
    random.shuffle(password)
    return "".join(password)


def _random_birthday():
    year = random.randint(1994, 2005)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year}-{month:02d}-{day:02d}"


class KickAccountCreator:
    """Kick.com otomatik hesap oluşturma - çok aşamalı akış."""

    def __init__(self, log_callback=None, proxy=None):
        self.log_callback = log_callback or (lambda msg: None)
        self.proxy = proxy
        self.email_client = None
        self.username = None
        self.password = None
        self.email = None
        self.token = None
        self.xsrf_token = None
        self.session = None

    def _log(self, msg):
        self.log_callback(msg)

    def _create_session(self):
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        if HAS_CURL_CFFI:
            self.session = crequests.Session(impersonate="chrome120", proxies=proxies)
        else:
            self.session = crequests.Session()
            if proxies:
                self.session.proxies = proxies

    def _refresh_xsrf(self):
        """Session cookie'lerinden XSRF token'ı güncelle."""
        if hasattr(self.session, 'cookies'):
            for cn, cv in self.session.cookies.items():
                if cn.upper() == "XSRF-TOKEN":
                    self.xsrf_token = unquote(cv)
                    return True
        return False

    def _get_headers(self, referer=None, content_type="application/json"):
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "origin": KICK_BASE,
            "referer": referer or f"{KICK_BASE}/register",
            "sec-ch-ua": '"Chromium";v="120", "Google Chrome";v="120", "Not=A?Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "x-app-platform": "web",
        }
        if content_type:
            headers["content-type"] = content_type
        if self.xsrf_token:
            headers["x-xsrf-token"] = self.xsrf_token
        return headers

    def _api_post(self, path, payload, step_name):
        """API POST isteği gönder, hata yönetimi ile."""
        url = f"{KICK_API_V1}{path}"
        self._refresh_xsrf()
        headers = self._get_headers()

        try:
            resp = self.session.post(url, headers=headers, json=payload, timeout=15)
            status = resp.status_code
            body = resp.text[:500] if resp.text else ""

            if status in (200, 201, 204):
                self._log(f"[✅ {step_name}] Başarılı (HTTP {status})")
                try:
                    return True, resp.json()
                except Exception:
                    return True, {}
            elif status == 419:
                self._log(f"[❌ {step_name}] CSRF token hatası (419)")
                # Token'ı yenilemeyi dene
                self._refresh_xsrf()
                return False, {"error": "csrf", "status": status}
            elif status == 422:
                self._log(f"[❌ {step_name}] Doğrulama hatası (422): {body[:200]}")
                return False, {"error": "validation", "status": status, "body": body}
            elif status == 429:
                self._log(f"[⚠ {step_name}] Rate limit (429) - 30sn bekleniyor...")
                time.sleep(30)
                return False, {"error": "rate_limit", "status": status}
            elif status == 403:
                self._log(f"[❌ {step_name}] Cloudflare engeli (403)")
                return False, {"error": "cloudflare", "status": status}
            elif status == 404:
                self._log(f"[❌ {step_name}] Endpoint bulunamadı (404)")
                return False, {"error": "not_found", "status": status}
            else:
                self._log(f"[❌ {step_name}] HTTP {status}: {body[:200]}")
                return False, {"error": "unknown", "status": status, "body": body}

        except Exception as e:
            self._log(f"[❌ {step_name}] İstek hatası: {e}")
            return False, {"error": "exception", "message": str(e)}

    def _init_session(self):
        """Kick.com'a ilk bağlantı - cookie ve XSRF token al."""
        self._log("[1/6] Kick.com oturumu başlatılıyor...")
        self._create_session()

        try:
            # Ana sayfaya git
            resp = self.session.get(
                KICK_BASE,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                        " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
                timeout=15,
            )

            if resp.status_code != 200:
                self._log(f"[HATA] Kick.com'a bağlanılamadı: HTTP {resp.status_code}")
                return False

            # XSRF token al
            self._refresh_xsrf()

            # Register sayfasını ziyaret et (ek cookie + güncel token için)
            time.sleep(1)
            resp2 = self.session.get(
                f"{KICK_BASE}/register",
                headers={
                    "accept": "text/html,application/xhtml+xml",
                    "referer": KICK_BASE,
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                        " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
                timeout=10,
            )

            self._refresh_xsrf()

            if self.xsrf_token:
                self._log(f"[1/6] ✅ Oturum + XSRF token alındı")
            else:
                self._log("[1/6] ⚠ Oturum başlatıldı ama XSRF token bulunamadı")

            return True

        except Exception as e:
            self._log(f"[HATA] Bağlantı hatası: {e}")
        return False

    def _create_temp_email(self):
        """Geçici e-posta hesabı oluştur."""
        self._log("[2/6] Geçici e-posta oluşturuluyor...")
        self.email_client = TempEmailClient()
        success, msg = self.email_client.create_account()
        self._log(f"[2/6] {msg}")

        if success:
            info = self.email_client.get_info()
            self.email = info["address"]
            return True
        return False

    def _step_agree_terms(self):
        """Adım 1: Kullanım şartlarını kabul et."""
        self._log("[3/6] Kullanım şartları kabul ediliyor...")
        birthday = _random_birthday()
        payload = {
            "birthday": birthday,
            "agreed_to_terms": True,
            "agreed_to_privacy": True,
        }
        ok, data = self._api_post("/signup/agreed-terms", payload, "Şartlar")
        return ok

    def _step_set_username(self):
        """Adım 2: Kullanıcı adı belirle."""
        self.username = _random_username()
        self.password = _random_password()
        self._log(f"[4/6] Kullanıcı adı ayarlanıyor: {self.username}")

        payload = {
            "username": self.username,
            "password": self.password,
            "password_confirmation": self.password,
        }
        ok, data = self._api_post("/signup/username", payload, "Kullanıcı Adı")
        return ok

    def _step_send_email(self):
        """Adım 3: Doğrulama kodu gönder."""
        self._log(f"[5/6] Doğrulama kodu gönderiliyor: {self.email}")
        payload = {"email": self.email}
        ok, data = self._api_post("/signup/send/email", payload, "E-posta Gönder")
        return ok

    def _step_verify_code(self):
        """Adım 4: E-postadan gelen kodu doğrula."""
        if not self.email_client:
            self._log("[HATA] E-posta istemcisi yok!")
            return False

        self._log("[5/6] Doğrulama kodu bekleniyor (maks 120sn)...")

        code, subject = self.email_client.wait_for_verification_code(
            timeout=120,
            poll_interval=5,
            sender_filter="kick",
        )

        if not code:
            self._log("[HATA] Doğrulama kodu alınamadı!")
            return False

        self._log(f"[5/6] Doğrulama kodu alındı: {code}")

        if code.startswith("http"):
            # Link tabanlı doğrulama
            self._log("[5/6] Doğrulama linki ziyaret ediliyor...")
            try:
                resp = self.session.get(code, headers=self._get_headers(), timeout=15, allow_redirects=True)
                if resp.status_code in (200, 301, 302):
                    self._log("[5/6] ✅ E-posta doğrulandı (link)")
                    return True
            except Exception as e:
                self._log(f"[HATA] Link hatası: {e}")
            return False

        # Kod tabanlı doğrulama
        payload = {"code": code}
        ok, data = self._api_post("/signup/verify/code", payload, "Kod Doğrula")
        return ok

    def _step_complete(self):
        """Adım 5: Kayıt tamamla."""
        self._log("[6/6] Kayıt tamamlanıyor...")
        payload = {}
        ok, data = self._api_post("/signup/complete", payload, "Kayıt Tamamla")

        if ok and isinstance(data, dict):
            self.token = data.get("token") or data.get("access_token")
        return ok

    def create_account(self):
        """Tam hesap oluşturma akışı - 6 adım."""
        self._log("=" * 50)
        self._log("[SİSTEM] Yeni Kick hesabı oluşturma başlatıldı...")
        self._log("=" * 50)

        # Adım 1: Oturum başlat
        if not self._init_session():
            return {"success": False, "error": "Kick.com'a bağlanılamadı"}

        # Adım 2: Geçici e-posta oluştur
        if not self._create_temp_email():
            return {"success": False, "error": "Geçici e-posta oluşturulamadı"}

        # Adım 3: Şartları kabul et
        if not self._step_agree_terms():
            self._log("[SONUÇ] ❌ Şartlar kabul aşamasında başarısız")
            return self._fail_result()

        time.sleep(random.uniform(1, 3))

        # Adım 4: Kullanıcı adı belirle
        if not self._step_set_username():
            self._log("[SONUÇ] ❌ Kullanıcı adı aşamasında başarısız")
            return self._fail_result()

        time.sleep(random.uniform(1, 3))

        # Adım 5: E-posta gönder ve doğrula
        if not self._step_send_email():
            self._log("[SONUÇ] ❌ E-posta gönderme aşamasında başarısız")
            return self._fail_result()

        if not self._step_verify_code():
            self._log("[SONUÇ] ❌ E-posta doğrulama aşamasında başarısız")
            return self._fail_result()

        time.sleep(random.uniform(1, 2))

        # Adım 6: Kayıt tamamla
        registered = self._step_complete()

        result = {
            "success": registered,
            "verified": registered,
            "email": self.email,
            "username": self.username,
            "password": self.password,
            "token": self.token,
        }

        if registered:
            self._log(f"[SONUÇ] ✅ Hesap oluşturuldu!")
            self._log(f"[SONUÇ] E-posta: {self.email}")
            self._log(f"[SONUÇ] Kullanıcı: {self.username}")
            self._log(f"[SONUÇ] Şifre: {self.password}")
            if self.token:
                self._log(f"[SONUÇ] Token: {self.token}")
        else:
            self._log("[SONUÇ] ❌ Hesap oluşturulamadı!")

        self._log("=" * 50)
        return result

    def _fail_result(self):
        self._log("=" * 50)
        return {
            "success": False,
            "verified": False,
            "email": self.email,
            "username": self.username,
            "password": self.password,
            "token": None,
        }


def create_multiple_accounts(count=1, delay=5, log_callback=None):
    results = []
    log_fn = log_callback or (lambda msg: None)

    for i in range(count):
        log_fn(f"\n[SİSTEM] Hesap {i + 1}/{count} oluşturuluyor...")
        creator = KickAccountCreator(log_callback=log_callback)
        result = creator.create_account()
        results.append(result)

        if i < count - 1:
            log_fn(f"[SİSTEM] {delay} saniye bekleniyor...")
            time.sleep(delay)

    successful = sum(1 for r in results if r.get("success"))
    log_fn(f"\n[ÖZET] {successful}/{count} hesap başarıyla oluşturuldu.")
    return results
