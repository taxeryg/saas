"""
Geçici e-posta modülü - mail.tm API kullanarak
Kick hesap doğrulaması için geçici e-posta oluşturma ve doğrulama kodu okuma.
"""

import random
import re
import string
import time

try:
    from curl_cffi import requests as crequests
except ImportError:
    import requests as crequests


MAIL_TM_API = "https://api.mail.tm"


class TempEmailClient:
    """mail.tm API ile geçici e-posta hesabı yönetimi."""

    def __init__(self):
        self.address = None
        self.password = None
        self.token = None
        self.account_id = None

    def _headers(self, auth=False):
        """API istekleri için header oluştur."""
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if auth and self.token:
            headers["authorization"] = f"Bearer {self.token}"
        return headers

    def get_domains(self):
        """Kullanılabilir e-posta domainlerini al."""
        try:
            resp = crequests.get(
                f"{MAIL_TM_API}/domains",
                headers=self._headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                # mail.tm API bazen hydra formatında döner
                if isinstance(data, dict) and "hydra:member" in data:
                    domains = data["hydra:member"]
                elif isinstance(data, list):
                    domains = data
                else:
                    domains = []

                active_domains = [
                    d["domain"] for d in domains
                    if d.get("isActive", True)
                ]
                return active_domains
        except Exception as e:
            print(f"[HATA] Domain listesi alınamadı: {e}")
        return []

    def _generate_random_username(self, length=10):
        """Rastgele kullanıcı adı oluştur."""
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    def _generate_random_password(self, length=14):
        """Rastgele güçlü şifre oluştur."""
        lower = string.ascii_lowercase
        upper = string.ascii_uppercase
        digits = string.digits
        special = "!@#$%^&*"
        all_chars = lower + upper + digits + special

        # En az birer karakter garanti et
        password = [
            random.choice(lower),
            random.choice(upper),
            random.choice(digits),
            random.choice(special),
        ]
        password += [random.choice(all_chars) for _ in range(length - 4)]
        random.shuffle(password)
        return "".join(password)

    def create_account(self):
        """Yeni geçici e-posta hesabı oluştur."""
        domains = self.get_domains()
        if not domains:
            return False, "Kullanılabilir domain bulunamadı!"

        domain = random.choice(domains)
        username = self._generate_random_username()
        self.address = f"{username}@{domain}"
        self.password = self._generate_random_password()

        payload = {
            "address": self.address,
            "password": self.password,
        }

        try:
            resp = crequests.post(
                f"{MAIL_TM_API}/accounts",
                headers=self._headers(),
                json=payload,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                self.account_id = data.get("id")
                # Token al
                token_ok = self._get_token()
                if token_ok:
                    return True, f"Geçici e-posta oluşturuldu: {self.address}"
                else:
                    return False, "E-posta oluşturuldu ama token alınamadı!"
            else:
                return False, f"Hesap oluşturulamadı: {resp.status_code} - {resp.text}"
        except Exception as e:
            return False, f"Hesap oluşturma hatası: {e}"

    def _get_token(self):
        """JWT token al."""
        payload = {
            "address": self.address,
            "password": self.password,
        }
        try:
            resp = crequests.post(
                f"{MAIL_TM_API}/token",
                headers=self._headers(),
                json=payload,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                self.token = data.get("token")
                return True
        except Exception as e:
            print(f"[HATA] Token alınamadı: {e}")
        return False

    def get_messages(self):
        """Gelen kutusundaki mesajları al."""
        if not self.token:
            return []

        try:
            resp = crequests.get(
                f"{MAIL_TM_API}/messages",
                headers=self._headers(auth=True),
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "hydra:member" in data:
                    return data["hydra:member"]
                elif isinstance(data, list):
                    return data
        except Exception as e:
            print(f"[HATA] Mesajlar alınamadı: {e}")
        return []

    def get_message_detail(self, message_id):
        """Belirli bir mesajın detayını al."""
        if not self.token:
            return None

        try:
            resp = crequests.get(
                f"{MAIL_TM_API}/messages/{message_id}",
                headers=self._headers(auth=True),
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            print(f"[HATA] Mesaj detayı alınamadı: {e}")
        return None

    def wait_for_verification_code(self, timeout=120, poll_interval=5, sender_filter=None):
        """
        Doğrulama kodu içeren e-posta gelene kadar bekle.

        Args:
            timeout: Maksimum bekleme süresi (saniye)
            poll_interval: Kontrol aralığı (saniye)
            sender_filter: Gönderen filtresi (ör: 'kick.com')

        Returns:
            (code, subject) veya (None, None)
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            messages = self.get_messages()

            for msg in messages:
                # Gönderen filtresi uygula
                sender = msg.get("from", {})
                if isinstance(sender, dict):
                    sender_addr = sender.get("address", "")
                else:
                    sender_addr = str(sender)

                if sender_filter and sender_filter.lower() not in sender_addr.lower():
                    continue

                # Mesaj detayını al
                msg_id = msg.get("id")
                if not msg_id:
                    continue

                detail = self.get_message_detail(msg_id)
                if not detail:
                    continue

                # Doğrulama kodunu ara
                body_text = detail.get("text", "") or ""
                body_html = detail.get("html", "") or ""
                # html listeyse birleştir
                if isinstance(body_html, list):
                    body_html = " ".join(body_html)

                full_text = f"{body_text} {body_html}"
                subject = detail.get("subject", "")

                code = self._extract_verification_code(full_text)
                if code:
                    return code, subject

                # Link tabanlı doğrulama kontrolü
                link = self._extract_verification_link(full_text)
                if link:
                    return link, subject

            time.sleep(poll_interval)

        return None, None

    def _extract_verification_code(self, text):
        """Metin içinden doğrulama kodunu çıkar."""
        if not text:
            return None

        # Yaygın doğrulama kodu formatları
        patterns = [
            # "verification code: 123456" veya "code is 123456"
            r'(?:verification|doğrulama|confirm|onay|code|kod)\s*(?:is|:)?\s*(\d{4,8})',
            # "123456" tek başına 6 haneli kod
            r'\b(\d{6})\b',
            # "1234" tek başına 4 haneli kod
            r'\b(\d{4})\b',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    def _extract_verification_link(self, text):
        """Metin içinden doğrulama linkini çıkar."""
        if not text:
            return None

        # Doğrulama linki ara
        patterns = [
            r'(https?://[^\s"<>]+(?:verify|confirm|activate|validate)[^\s"<>]*)',
            r'(https?://kick\.com[^\s"<>]*(?:verify|confirm|email)[^\s"<>]*)',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    def get_info(self):
        """Hesap bilgilerini döndür."""
        return {
            "address": self.address,
            "password": self.password,
            "account_id": self.account_id,
        }
