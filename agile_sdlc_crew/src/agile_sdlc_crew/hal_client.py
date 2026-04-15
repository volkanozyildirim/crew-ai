"""HAL API istemcisi - login + analysis/chat."""

import os
import re
import ssl

import requests
from urllib3.exceptions import InsecureRequestWarning

# Kurumsal proxy self-signed sertifika uyarılarını kapat
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


class HALClient:
    """HAL API uzerinden work item analizi yapan istemci."""

    def __init__(self):
        self.base_url = os.environ.get("HAL_BASE_URL", "").rstrip("/")
        self.email = os.environ.get("HAL_EMAIL", "")
        self.password = os.environ.get("HAL_PASSWORD", "")

        if not all([self.base_url, self.email, self.password]):
            raise ValueError(
                "HAL_BASE_URL, HAL_EMAIL ve HAL_PASSWORD ortam degiskenleri ayarlanmalidir."
            )

        self._token: str | None = None
        self._conversation_id: str | None = None

    def login(self) -> str:
        """HAL API'ye login olup access token alir."""
        url = f"{self.base_url}/api/auth/login"
        resp = requests.post(
            url,
            json={"email": self.email, "password": self.password},
            headers={"Content-Type": "application/json"},
            timeout=30,
            verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"HAL login basarisiz: {data.get('message', 'bilinmeyen hata')}")
        self._token = data["detail"]["access_token"]
        return self._token

    def _ensure_auth(self):
        """Token yoksa login ol."""
        if not self._token:
            self.login()

    def _chat(self, message: str) -> dict:
        """HAL chat API'ye mesaj gonder. Auth hatasi olursa re-login yap.

        Returns:
            dict: API detail response
        """
        self._ensure_auth()

        url = f"{self.base_url}/api/analysis/chat"
        body = {"message": message, "save_history": True}
        if self._conversation_id:
            body["session_id"] = self._conversation_id

        for attempt in range(2):
            resp = requests.post(
                url,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._token}",
                },
                timeout=180,
                verify=False,
            )

            # 401/403 -> re-login ve tekrar dene
            if resp.status_code in (401, 403) and attempt == 0:
                print("  HAL auth hatasi, re-login yapiliyor...")
                self.login()
                resp = None
                continue

            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                raise RuntimeError(f"HAL basarisiz: {data.get('message', '')}")

            detail = data["detail"]
            # conversation_id'yi kaydet (ayni sohbette devam)
            if detail.get("session_id"):
                self._conversation_id = detail["session_id"]

            return detail

        raise RuntimeError("HAL auth basarisiz (2 deneme)")

    def analyze_work_item(self, work_item_id: int | str, prompt: str | None = None) -> dict:
        """HAL ile work item analizi yapar (iki asamali).

        1. Is kaleminin aciklamasini oku ve kabul kriterlerini belirle
        2. Ayni sohbette gelistirme detaylarini iste

        Returns:
            dict with keys: response (str), ai_response (dict), session_id (str)
        """
        # Adim 1: Is aciklamasi ve kabul kriterleri
        step1_prompt = (
            f"{work_item_id} idli isin sadece aciklamasini oku ve kabul kriterlerini belirle."
        )
        self._chat(step1_prompt)

        # Adim 2: Ayni sohbette gelistirme detaylari
        step2_prompt = (
            f"Bu gereksinimleri karsilayacak gelistirme detaylarini yazilimciya aktarmak uzere ver.\n"
            f"Yanitini su basliklar ile yaz:\n"
            f"- Repository: repo adi\n"
            f"- Dosya: /tam/dosya/yolu\n"
            f"- Mevcut Kod: degisecek kod blogu\n"
            f"- Yeni Kod: yerine yazilacak kod"
        )
        return self._chat(step2_prompt)

    def followup(self, message: str) -> dict:
        """Ayni sohbette devam mesaji gonder."""
        return self._chat(message)

    def parse_analysis_response(self, detail: dict) -> dict:
        """HAL yanitini o4-mini ile parse eder."""
        response_text = detail.get("response", "")
        if not response_text:
            return {"summary": "", "repo_name": "", "changes": [], "raw_response": ""}

        return self._llm_parse(response_text)

    def _llm_parse(self, text: str) -> dict:
        """o4-mini kullanarak serbest formattaki metni yapilandirilmis veriye cevirir."""
        import json as _json
        import os
        import litellm

        model = os.environ.get("LITELLM_MODEL", "o4-mini")
        base_url = os.environ.get("LITELLM_BASE_URL")
        api_key = os.environ.get("LITELLM_API_KEY")

        if base_url and not model.startswith("openai/"):
            model = f"openai/{model}"

        system_prompt = (
            "Asagidaki metinden su bilgileri JSON olarak cikar:\n"
            '{"repo_name":"...","summary":"...","changes":['
            '{"file_path":"/...","change_type":"edit|add","description":"...",'
            '"current_code":"...","new_code":"..."}]}\n'
            "Kurallar:\n"
            "- repo_name: Azure DevOps git repo adi (orn: stock-api, webservice)\n"
            "- file_path: / ile baslayan dosya yolu, repo adi DAHIL ETME\n"
            "- current_code: SADECE degistirilecek/silinecek kod blogu. "
            "Dosyanin sonuna yeni kod EKLENECEKSE current_code BOS olmali.\n"
            "- new_code: current_code yerine yazilacak kod VEYA dosyaya eklenecek yeni kod\n"
            "- change_type: mevcut kod DEGISTIRILIYORSA 'edit', dosyaya yeni kod EKLENIYORSA 'add'\n"
            "- Test dosyasina yeni test fonksiyonu EKLENIYORSA: change_type='add', current_code='' (BOS!)\n"
            "- ONEMLI: change_type='add' olan dosyalarda current_code MUTLAKA bos olmali\n"
            "- Sadece JSON dondur, baska bir sey yazma"
        )

        try:
            resp = litellm.completion(
                model=model,
                base_url=base_url,
                api_key=api_key,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text[:8000]},
                ],
                max_tokens=4096,
            )
            content = resp.choices[0].message.content or ""

            # JSON cikar
            m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
            candidate = m.group(1).strip() if m else content.strip()
            if not candidate.startswith("{"):
                brace = re.search(r'\{.*\}', candidate, re.DOTALL)
                candidate = brace.group(0) if brace else candidate

            # Kontrol karakterlerini temizle (tab ve newline haric)
            candidate = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', candidate)
            data = _json.loads(candidate)

            changes = []
            for ch in data.get("changes", []):
                path = ch.get("file_path", ch.get("path", ""))
                if path and not path.startswith("/"):
                    path = "/" + path
                changes.append({
                    "path": path,
                    "change_type": ch.get("change_type", "edit"),
                    "description": ch.get("description", ""),
                    "current_code": ch.get("current_code", ""),
                    "code": ch.get("new_code", ch.get("code", "")),
                })

            return {
                "summary": data.get("summary", ""),
                "repo_name": data.get("repo_name", ""),
                "changes": changes,
                "raw_response": text,
            }

        except Exception as e:
            print(f"  LLM parse hatasi: {e}, fallback kullaniliyor")
            return self._regex_fallback_parse(text)

    def _regex_fallback_parse(self, text: str) -> dict:
        """LLM basarisiz olursa basit regex fallback."""
        repo_name = ""
        for pat in [
            r'[Rr]epository[:\s]+([A-Za-z0-9._-]+)',
            r'_git/([A-Za-z0-9._-]+)',
        ]:
            m = re.search(pat, text)
            if m:
                repo_name = m.group(1)
                break

        return {
            "summary": "",
            "repo_name": repo_name,
            "changes": [],
            "raw_response": text,
        }
