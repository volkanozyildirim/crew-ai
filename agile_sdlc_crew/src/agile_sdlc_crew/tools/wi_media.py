"""WI description'daki resim ve linkleri analiz eder.

Azure DevOps iş kalemi açıklamasında HTML inline:
- <img src="..."> — ekran görüntüsü, mockup, akış diyagramı
- <a href="..."> — Confluence, docs, referans PR'lar

Bu modül:
1. Description HTML'inden img/a URL'lerini çıkarır
2. Resimleri PAT ile indirir → base64 → vision LLM ile textual açıklama
3. Linkleri fetch edip HTML strip ederek metin içeriği döndürür
4. Hepsini birleştirip "enrichment" metni olarak döndürür
"""

import base64
import logging
import os
import re

import requests

log = logging.getLogger("pipeline")

# URL extraction
_IMG_PATTERN = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_LINK_PATTERN = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]*)</a>',
    re.IGNORECASE,
)

# Limitler
MAX_IMAGES = 5
MAX_LINKS = 5
MAX_LINK_TEXT = 3000
MAX_IMAGE_BYTES = 5_000_000  # 5MB


class WIMediaAnalyzer:
    """WI description'daki resim ve link içeriklerini textual rapora çevirir."""

    def __init__(self, client):
        """client: AzureDevOpsClient (PAT ile attachment indirir)."""
        self.client = client

    def extract_media(self, html: str) -> dict:
        """HTML'den img src ve a href URL'lerini çıkar."""
        if not html:
            return {"images": [], "links": []}
        images = list(dict.fromkeys(_IMG_PATTERN.findall(html)))
        links = []
        seen_urls = set()
        for url, text in _LINK_PATTERN.findall(html):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            links.append({"url": url, "text": (text or "").strip()[:100]})
        return {"images": images[:MAX_IMAGES], "links": links[:MAX_LINKS]}

    def _is_azure_devops_url(self, url: str) -> bool:
        """URL Azure DevOps'a mı ait?"""
        org = os.environ.get("AZURE_DEVOPS_ORG_URL", "").rstrip("/")
        return bool(org) and url.startswith(org)

    def _download_image(self, url: str) -> bytes | None:
        """Resim URL'sini indir. Azure ise PAT, değilse normal."""
        try:
            if self._is_azure_devops_url(url):
                data = self.client.download_attachment(url, timeout=30)
            else:
                # User-Agent ekle — bazi siteler (wikipedia, github vb) bunu ister
                headers = {
                    "User-Agent": "Mozilla/5.0 (agile-sdlc-crew) AppleWebKit/537.36",
                    "Accept": "image/*,*/*",
                }
                resp = requests.get(url, timeout=30, verify=False, headers=headers)
                resp.raise_for_status()
                data = resp.content
            if len(data) > MAX_IMAGE_BYTES:
                log.warning(f"  Resim çok büyük ({len(data)} byte), atlaniyor")
                return None
            return data
        except Exception as e:
            log.warning(f"  Resim indirme hatasi: {e}")
            return None

    def analyze_image(self, url: str) -> str:
        """Resim → base64 → vision provider → textual aciklama.

        Provider/model/base_url/api_key tum bilgi vision/resolver.py'den okunur;
        dashboard'dan yonetilebilir. Geriye uyumluluk: CREW_USE_LOCAL_VISION env'i
        varsa ollama'ya yonlendirilir."""
        data = self._download_image(url)
        if not data:
            return f"(Resim indirilemedi: {url[:80]}...)"

        # Mime type tespiti
        mime = "image/png"
        if data[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            mime = "image/gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mime = "image/webp"

        b64 = base64.b64encode(data).decode("ascii")

        prompt_text = (
            "Bu resmi islevsel/teknik gereksinim perspektifinden acikla. "
            "UI mockup/ekran goruntusu/akis diyagrami/diagram olabilir. "
            "Turkce, 3-5 cumle, teknik detaylari on plana cikart. "
            "Ornek butonlar, alanlar, veri akisi, durumlar yaz."
        )

        try:
            from agile_sdlc_crew.vision import (
                analyze_image as _analyze,
                get_api_key, get_base_url, get_model, get_provider,
            )
            provider = get_provider()
            model = get_model()
            log.info(f"  Vision call: provider={provider} model={model}")
            return _analyze(
                provider=provider,
                image_b64=b64,
                mime=mime,
                prompt=prompt_text,
                model=model,
                base_url=get_base_url(),
                api_key=get_api_key(),
            )
        except Exception as e:
            log.warning(f"  Vision call hatasi: {e}")
            return f"(Resim analizi yapilamadi: {type(e).__name__}: {str(e)[:100]})"

    def fetch_link(self, url: str) -> str:
        """Link'i fetch et, HTML strip edip metin dön."""
        try:
            if self._is_azure_devops_url(url):
                # Azure internal link (başka WI, PR, vb.)
                content = self.client.download_attachment(url, timeout=20)
            else:
                headers = {
                    "User-Agent": "Mozilla/5.0 (agile-sdlc-crew) AppleWebKit/537.36",
                    "Accept": "text/html,application/xhtml+xml,text/plain,*/*",
                }
                resp = requests.get(url, timeout=20, verify=False, allow_redirects=True, headers=headers)
                resp.raise_for_status()
                content = resp.content

            # Bytes → text (utf-8 varsayalım)
            try:
                text = content.decode("utf-8", errors="replace")
            except Exception:
                return f"(Link icerigi binary, okunamadi: {url[:80]})"

            # HTML ise strip
            if "<html" in text.lower() or "<body" in text.lower() or "<div" in text.lower():
                # Script/style temizle
                text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
                # Title al
                title_match = re.search(r'<title[^>]*>([^<]+)</title>', text, re.IGNORECASE)
                title = title_match.group(1).strip() if title_match else ""
                # Tag strip
                text = re.sub(r'<[^>]+>', ' ', text)
                # Whitespace normalize
                text = re.sub(r'\s+', ' ', text).strip()
                if title:
                    text = f"[Başlık: {title}]\n\n{text}"

            return text[:MAX_LINK_TEXT]
        except Exception as e:
            log.warning(f"  Link fetch hatasi ({url[:50]}): {e}")
            return f"(Link icerigi alinamadi: {type(e).__name__})"

    def enrich_description(self, html_desc: str) -> str:
        """Description'ı parse et, resim+link içeriklerini çıkar, textual rapor dön."""
        if not html_desc or not html_desc.strip():
            return ""

        media = self.extract_media(html_desc)
        if not media["images"] and not media["links"]:
            return ""

        parts = []

        # Resimler
        if media["images"]:
            parts.append(f"## Description'daki Resimler ({len(media['images'])})")
            for i, img_url in enumerate(media["images"], 1):
                log.info(f"  WI resim analizi {i}/{len(media['images'])}: {img_url[:80]}")
                analysis = self.analyze_image(img_url)
                parts.append(f"\n### Resim {i}\n{analysis}")

        # Linkler
        if media["links"]:
            parts.append(f"\n## Description'daki Linkler ({len(media['links'])})")
            for i, link in enumerate(media["links"], 1):
                log.info(f"  WI link fetch {i}/{len(media['links'])}: {link['url'][:80]}")
                content = self.fetch_link(link["url"])
                label = f" — {link['text']}" if link["text"] else ""
                parts.append(
                    f"\n### Link {i}: {link['url']}{label}\n"
                    f"**Icerik:** {content[:2000]}"
                )

        return "\n".join(parts)
