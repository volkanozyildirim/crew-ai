"""Pipeline davranis ayarlari — dashboard tarafindan yonetilen toggle'lar.

LLM secimi (llm/), embedding (embed/), provider (providers/) gibi paketlerden
sonra geriye kalan pipeline davranisi knob'lari (kickoff toggle, cost limit,
context budget, vs.) bu modulun schema + yaml store + helper'i ile yonetilir.

Cozumleme onceligi:
    1. config/pipeline_config.yaml (dashboard tarafindan yazilir)
    2. env (CREW_*) — geriye uyumluluk
    3. SCHEMA default

Yeni knob ekleme: SCHEMA listesine bir entry ekle ve call site'tan
`pipeline_config.get("CREW_X")` ile oku — env okumayi asagidaki helper yapar.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("pipeline")

_CONFIG_FILE = Path(__file__).resolve().parent / "config" / "pipeline_config.yaml"


# Knob schemas. UI bu listeden form uretir.
# type: "bool" | "int" | "float"
# bool: env'de "1"/"true"/"yes" -> True
SCHEMA: list[dict] = [
    # ── Pipeline davranis toggle'lari ──
    {
        "key": "CREW_KICKOFF_MEETING",
        "label": "Kickoff Toplantısı",
        "type": "bool",
        "default": True,
        "desc": "Her is basinda 4 ajanin katildigi kickoff adimi. Kapatirsan ilgili adim atlanir.",
    },
    {
        "key": "CREW_SM_REVIEW",
        "label": "Scrum Master İncelemesi",
        "type": "bool",
        "default": False,
        "desc": "Her adim sonrasi SM kalite kontrolu. Ek LLM cagrisi yapar — maliyeti artirir.",
    },
    {
        "key": "CREW_STRUCTURED_REVIEW",
        "label": "Yapısal Review Madde Takibi",
        "type": "bool",
        "default": True,
        "desc": "Reviewer'in REVIEW_ISSUES_JSON madde listesini parse edip developer'a "
                "dogrudan aktarma + verify_review_task ile madde-madde kapanma dogrulamasi "
                "(yakinsayan dongu). Sadece blocker/major maddeler bloklar; minor → oneri "
                "(yorum, RED sebebi degil). Reviewer JSON uretemezse eski davranisa (ham "
                "review_text → _amend_plan → tum plan dosyalarini yeniden yaz) doner.",
    },
    {
        "key": "CREW_ANALYZE_WI_MEDIA",
        "label": "WI Görsel/Link Analizi",
        "type": "bool",
        "default": True,
        "desc": "Work item description'daki gorseller ve linkler analiz edilsin mi?",
    },
    {
        "key": "CREW_TASK_GUARDRAILS",
        "label": "Task Guardrails",
        "type": "bool",
        "default": False,
        "desc": "Architect (JSON) ve Developer (kod) ciktilarini CrewAI guardrail ile dogrula; basarisizsa agent otomatik retry eder.",
    },
    {
        "key": "CREW_KNOWLEDGE_RAG",
        "label": "Knowledge RAG",
        "type": "bool",
        "default": False,
        "desc": "Domain knowledge'i backstory'ye tikistirmak yerine CrewAI Knowledge ile RAG olarak ver (token tasarrufu, kucuk modelde odak).",
    },
    {
        "key": "CREW_REPO_HISTORY_SUGGEST",
        "label": "Geçmiş-İş Repo Önerisi",
        "type": "bool",
        "default": False,
        "desc": "Başarılı geçmiş işleri (içerik+dosya yolu→repo) vector indekse al; yeni işte repo kararına advisory öneri olarak kickoff/discover/technical-design'a besle. Architect son kararı verir.",
    },
    {
        "key": "CREW_REPO_HISTORY_MIN_SCORE",
        "label": "Repo Önerisi Min. Skor",
        "type": "float",
        "default": 0.1,
        "min": 0.0,
        "desc": "Geçmiş-iş repo önerisinin kabul edileceği minimum benzerlik skoru. Altındaki öneriler yok sayılır.",
    },
    {
        "key": "CREW_AZ_BACKFILL_WORKERS",
        "label": "Azure Backfill Worker Sayısı",
        "type": "int",
        "default": 8,
        "min": 1,
        "desc": "Azure DevOps backfill sırasında PR/WI çekmek için eşzamanlı thread sayısı.",
    },
    {
        "key": "CREW_AZ_BACKFILL_LIMIT",
        "label": "Azure Backfill Maks. WI",
        "type": "int",
        "default": 0,
        "min": 0,
        "desc": "Taranacak maksimum done work item sayısı (güncelden geriye). 0 = limitsiz (tümü).",
    },
    {
        "key": "CREW_AZ_DONE_STATES",
        "label": "Azure 'Done' Durumları",
        "type": "str",
        "default": "Done",
        "desc": "Backfill'de 'tamamlanmış' sayılan WI durumları (virgülle ayrılmış). Sadece Done okunur; ek durum gerekirse 'Done,Closed' gibi genişlet.",
    },

    # ── Maliyet kontrolu ──
    {
        "key": "CREW_MAX_JOB_COST",
        "label": "Maks. Iş Maliyeti (USD)",
        "type": "float",
        "default": 5.0,
        "min": 0.5,
        "desc": "Kumulatif LLM maliyeti bu degeri asarsa pipeline durur, WI'ya yorum atilir.",
    },
    {
        "key": "CREW_PRICE_INPUT_USD_PER_M",
        "label": "Input Token Fiyatı (USD / 1M)",
        "type": "float",
        "default": 3.0,
        "desc": "Maliyet hesabi icin. Default: Sonnet.",
    },
    {
        "key": "CREW_PRICE_OUTPUT_USD_PER_M",
        "label": "Output Token Fiyatı (USD / 1M)",
        "type": "float",
        "default": 15.0,
        "desc": "Maliyet hesabi icin. Default: Sonnet.",
    },

    # ── Iterasyon ve retry limitleri ──
    {
        "key": "CREW_ARCHITECT_MAX_ITER",
        "label": "Architect Max Iter",
        "type": "int",
        "default": 10,
        "min": 1,
        "desc": "Mimar ajanin maksimum iterasyon sayisi.",
    },
    {
        "key": "CREW_REVIEW_MAX_RETRIES",
        "label": "Review Max Retry",
        "type": "int",
        "default": 1,
        "min": 0,
        "desc": "Code review reddedince kac kez yeniden gelistirme dongusu calisir. Her retry pahali (developer+reviewer yeniden calisir); 1 onerilir.",
    },
    {
        "key": "CREW_TECH_DESIGN_MAX_ATTEMPTS",
        "label": "Teknik Tasarim Max Deneme",
        "type": "int",
        "default": 2,
        "min": 1,
        "desc": "Architect tool'suz plan uretimi (Faz B) parse tutmazsa kac kez yeniden dener. 2 onerilir: 1. deneme format hatasiysa 2. genelde duzeltir; 3+ nadiren yardim eder, sadece Opus cagrisi yakar (refuzal zaten erken kesilir).",
    },

    # ── Context bütçeleri ──
    {
        "key": "CREW_SUMMARIZE_FORWARD",
        "label": "Ozet-Ileri Besleme",
        "type": "bool",
        "default": False,
        "desc": "Acikken: cok adima tasinan buyuk metinler (requirements) her prompt'ta HAM yerine bir kez Haiku ile ozetlenip tekrar kullanilir → tekrarlanan input token azalir. KAPALI (default): sadece truncate. Bilgi kaybi riski var → bir WI ile dogrula.",
    },
    {
        "key": "CREW_DEV_CONTEXT_BUDGET",
        "label": "Developer Context Bütçesi",
        "type": "int",
        "default": 12000,
        "min": 1000,
        "desc": "Developer ajana verilen toplam mevcut kod context limiti (karakter).",
    },
    {
        "key": "CREW_DEV_CONTEXT_PER_FILE",
        "label": "Developer Per-File Context",
        "type": "int",
        "default": 2000,
        "min": 200,
        "desc": "Developer ajana verilen tek dosya basina context limiti (karakter).",
    },
    {
        "key": "CREW_MIN_WI_CONTENT_CHARS",
        "label": "Min. WI İçerik Eşiği",
        "type": "int",
        "default": 100,
        "min": 0,
        "desc": "Plain text WI icerigi bu kadarin altindaysa pipeline baslamaz.",
    },

    # ── Claude CLI subprocess ──
    {
        "key": "CREW_CLAUDE_CLI_TIMEOUT",
        "label": "Claude CLI Timeout (sn) — toplam ömür",
        "type": "int",
        "default": 300,
        "min": 30,
        "desc": "Claude CLI subprocess TOPLAM-ömür hard timeout'u. Aşılınca tüm process-group SIGKILL. Karmasik kod uretiminde Opus 60-180s alabilir.",
    },
    {
        "key": "CREW_CLAUDE_CLI_IDLE_TIMEOUT",
        "label": "Claude CLI Idle Timeout (sn)",
        "type": "int",
        "default": 90,
        "min": 0,
        "desc": "Event-arası sessizlik hard timeout'u. claude -p ağında sessizce (HİÇ stream event üretmeden) takılırsa bu kadar saniye içinde SIGKILL (toplam-ömür beklemeden). job 169'da 283s tam sessiz stall yaşandı; bu onu ~idle_s içinde keser. 0 = kapalı (sadece toplam-ömür).",
    },

    # ── Resume kontrolu ──
    {
        "key": "CREW_ENABLE_RESUME",
        "label": "Cache'ten Resume",
        "type": "bool",
        "default": True,
        "desc": "Onceki job'dan tamamlanan adimlar cache'ten okunarak atlanir. Vendor/yeni context ile taze calistirmak icin kapat.",
    },

    # ── Repo deps (vendor/) ──
    {
        "key": "CREW_INSTALL_DEPS",
        "label": "Repo Deps Install",
        "type": "bool",
        "default": False,
        "desc": "Hedef repo'da composer install / go mod vendor / npm install calistir. vendor/node_modules klasoru olusur, agent'lar 3rd-party kodu da inceleyebilir. UYARI: ilk install yavas (5-15dk).",
    },
    {
        "key": "CREW_VENDOR_INDEX",
        "label": "Vendor Vector Index",
        "type": "bool",
        "default": False,
        "desc": "vendor/node_modules altindaki 3rd-party paketleri (composer.json/package.json'daki require listesinden) vector DB'ye index'le. Semantic search Butterfly/Laravel framework kodunda da arar. Per-paket max 300 chunk; CREW_VENDOR_INCLUDE env ile ek path eklenir.",
    },
    {
        "key": "CREW_CLI_REPO_TOOLS",
        "label": "Architect Repo Araclari (claude -p --add-dir)",
        "type": "bool",
        "default": False,
        "desc": "Teknik tasarimda architect'e klonlanmis hedef repoyu --add-dir ile, Read/Grep/Glob'u --allowedTools ile ver. Architect gercek kodu kesfeder (mevcut servisi bulup 'modify' yapar; yeni dosya halusine etmez). UYARI: agent ekstra tur/sure harcayabilir.",
    },
    {
        "key": "CREW_CLI_CALL_MAX_USD",
        "label": "Çağrı-başı Maks $ (repo-tool keşif cap)",
        "type": "float",
        "default": 0.0,
        "min": 0.0,
        "desc": "Repo araçlı (--add-dir) claude çağrılarına çağrı-başı dolar cap'i (claude --max-budget-usd). Architect/implement otonom derin keşfe dalıp tek çağrıda 27-tur/$1.6 şişebiliyor; bu onu sınırlar. 0 = limitsiz.",
    },
    {
        "key": "CREW_CLI_EFFORT",
        "label": "claude -p Efor Seviyesi (baseline)",
        "type": "str",
        "default": "low",
        "desc": "Pipeline claude -p çağrılarına --effort ile zorlanan BASELINE efor (low/medium/high/xhigh/max). Kullanıcının global ~/.claude/settings.json effortLevel (high/xhigh) ayarını DEVRALMASIN — otomatik pipeline'da düşük efor yeterli, yüksek efor her çağrıyı çok yavaşlatır. NOT: haiku efor desteklemez → yalnız efor-destekli modellerde eklenir. Boş = ekleme (global ayarı devral).",
    },
    {
        "key": "CREW_CLI_EFFORT_ARCHITECT",
        "label": "Mimar Efor Seviyesi",
        "type": "str",
        "default": "high",
        "desc": "Yazılım mimarı (software_architect) çağrıları için efor — planı o ürettiği için en kritik agent, baseline'dan YÜKSEK olur (low/medium/high/xhigh/max). Diğer agent'lar CREW_CLI_EFFORT (baseline) kullanır. Hard timeout (CREW_CLAUDE_CLI_TIMEOUT) yüksek efordaki takılmaları yine keser. Boş = baseline kullan.",
    },
    {
        "key": "CREW_CLI_DISABLE_ADVISOR",
        "label": "claude -p Advisor Kapat",
        "type": "bool",
        "default": True,
        "desc": "Pipeline claude -p çağrılarında global advisorModel'i (--settings ile) boşaltıp advisor'ı kapat. Otomatik çağrıların her birinde ekstra danışman modeline (fable vb.) gitmesi = fazladan gecikme/maliyet.",
    },
    {
        "key": "CREW_PLAN_GATE",
        "label": "Plan Completeness Gate",
        "type": "bool",
        "default": False,
        "desc": "Teknik tasarım sonrası, her FR/AC'nin plandaki bir değişikliğe karşılık geldiğini ucuz (haiku) denetçiyle doğrula; boşluk varsa architect'i geri bildirimle bir kez yeniden çalıştırıp planı genişlet. Eksik-kapsam planların implement'e ulaşmasını engeller.",
    },
    {
        "key": "CREW_ISSUE_GATE",
        "label": "İtiraz Kapısı (Katman 0)",
        "type": "bool",
        "default": False,
        "desc": "Reviewer itirazlarını LLM'siz deterministik kurala göre bloklayıcı/düşürülen olarak ayır. Bir itiraz ancak blocker/major VE (geçerli requirement_ids VEYA doğrulanmış repo emsali) ise bloklar; kanıtı (evidence file/line/quote) doğrulanamayan itiraz düşer. Düşürülenler kaybolmaz — PR yorumu olur ama job'ı öldürmez. Reviewer'ın verdiği veri olarak ele alınır, hüküm olarak değil: job #179'un iki yanlış blokörü (uydurma convention, ürün kararı) bu filtreden geçemezdi.",
    },
    {
        "key": "CREW_PLAN_PATH_GATE",
        "label": "Plan Yol/Entegrasyon Gate",
        "type": "bool",
        "default": False,
        "desc": "Teknik tasarım sonrası plandaki dosya yollarını GERÇEK repo klonuyla karşılaştır (LLM çağrısı yok): (a) yeni dosyanın üst dizini repoda yoksa uydurma yol, (b) plan hiçbir mevcut kaynak dosyasını değiştirmiyorsa entegrasyon/çağrı noktası yok. Sorun varsa architect'i somut geri bildirimle bir kez yeniden çalıştırır. Uydurma yollu ve çağrılmayan-kod planlarının implement'e ulaşıp review'da kalıcı RED almasını engeller.",
    },
    {
        "key": "CREW_REVIEW_RETRY_REPLAN",
        "label": "Review Retry Yapısal Re-plan",
        "type": "bool",
        "default": False,
        "desc": "Review retry'da maddeyi sadece anchor'landığı dosyaya göre değil, required_fix metninde geçen dosyalara ve planın yapısal geçerliliğine göre de sınıflandır. Başka bir dosyaya dokunmayı gerektiren maddeler (ör. 'çağrı noktası ekle') architect re-plan'ına gider ve re-plan'ın eklediği dosyalar implement listesine alınır. Aksi halde bu maddeler tek-dosya düzenlemesiyle asla kapanmaz ve retry döngüsü yakınsamaz.",
    },
    {
        "key": "CREW_PR_BUILD_GATE",
        "label": "PR Build/Test Gate",
        "type": "bool",
        "default": False,
        "desc": "PR olusunca Azure DevOps'un PR-test pipeline'ini (refs/pull/{id}/merge) poll et; testler patlarsa developer fix dongusune gir, build yesil olana kadar gate yap. Repoda PR-test pipeline'i yoksa atlanir. UYARI: build suresi kadar bekler.",
    },
    {
        "key": "CREW_REQUIRE_TESTS",
        "label": "Test Yazma Zorunlulugu",
        "type": "bool",
        "default": False,
        "desc": "Repoda test altyapisi varsa (PR-test pipeline veya phpunit.xml/*Test/tests/ dizini), developer degisen davranis icin test ekler/gunceller; reviewer test eksikse CHANGES_REQUIRED verir.",
    },
    {
        "key": "CREW_PR_BUILD_MAX_RETRIES",
        "label": "PR Build Fix Max Retry",
        "type": "int",
        "default": 2,
        "min": 0,
        "desc": "PR build (test) basarisiz olunca kac kez otomatik duzeltme+yeniden-build dongusu calisir.",
    },
    {
        "key": "CREW_PR_BUILD_POLL_TIMEOUT",
        "label": "PR Build Poll Timeout (sn)",
        "type": "int",
        "default": 1200,
        "min": 60,
        "desc": "PR build'inin tamamlanmasi icin beklenecek azami sure (saniye). Asilirsa gate 'belirsiz' sayilip gecilir.",
    },
    {
        "key": "CREW_PR_BUILD_POLL_INTERVAL",
        "label": "PR Build Poll Interval (sn)",
        "type": "int",
        "default": 30,
        "min": 5,
        "desc": "PR build durumunun kac saniyede bir sorgulanacagi.",
    },
]

_SCHEMA_BY_KEY: dict[str, dict] = {f["key"]: f for f in SCHEMA}


@lru_cache(maxsize=1)
def load_config() -> dict:
    if not _CONFIG_FILE.exists():
        return {}
    try:
        return yaml.safe_load(_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning(f"  pipeline_config.yaml okuma hatasi: {e}")
        return {}


def reset_cache() -> None:
    load_config.cache_clear()


def _coerce(value: Any, kind: str) -> Any:
    if value is None or value == "":
        return None
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return value


def get(key: str) -> Any:
    """Knob degerini cozumlenmis (typed) olarak dondur. yaml > env > default."""
    field = _SCHEMA_BY_KEY.get(key)
    if not field:
        raise KeyError(f"Bilinmeyen pipeline knob: {key}")

    cfg = load_config()
    if key in cfg:
        coerced = _coerce(cfg[key], field["type"])
        if coerced is not None:
            return coerced

    env_val = os.environ.get(key)
    if env_val is not None and env_val != "":
        coerced = _coerce(env_val, field["type"])
        if coerced is not None:
            return coerced

    return field["default"]


def get_source(key: str) -> str:
    """Bir knob'un degeri nereden geliyor? 'dashboard' | 'env' | 'default'."""
    field = _SCHEMA_BY_KEY.get(key)
    if not field:
        return "unknown"
    cfg = load_config()
    if key in cfg and _coerce(cfg[key], field["type"]) is not None:
        return "dashboard"
    if os.environ.get(key) not in (None, ""):
        return "env"
    return "default"


def all_values() -> list[dict]:
    """Schema + degerleri + kaynak — UI icin."""
    out = []
    for f in SCHEMA:
        out.append({
            **f,
            "value": get(f["key"]),
            "source": get_source(f["key"]),
            "env_present": os.environ.get(f["key"]) not in (None, ""),
        })
    return out


def save(values: dict) -> dict:
    """Dashboard'dan gelen degerleri yaml'a yaz.

    Bilinmeyen key'ler reddedilir; bos/None degerler yaml'dan silinir
    (resolver bir sonraki katmana duser).
    """
    if not isinstance(values, dict):
        raise ValueError("values bir dict olmali")
    unknown = set(values.keys()) - set(_SCHEMA_BY_KEY.keys())
    if unknown:
        raise ValueError(f"Bilinmeyen knob'lar: {sorted(unknown)}")

    doc = dict(load_config() or {})
    for k, v in values.items():
        field = _SCHEMA_BY_KEY[k]
        if v is None or v == "":
            doc.pop(k, None)
            continue
        coerced = _coerce(v, field["type"])
        if coerced is None:
            raise ValueError(f"Gecersiz deger: {k}={v!r} (tip: {field['type']})")
        # Min check
        if "min" in field and isinstance(coerced, (int, float)) and coerced < field["min"]:
            raise ValueError(f"{k} en az {field['min']} olmali (alindi: {coerced})")
        doc[k] = coerced

    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as fh:
        fh.write(
            "# Dashboard tarafindan yonetilir — pipeline davranis toggle'lari.\n"
            "# Cozumleme onceligi: BU DOSYA > env (CREW_*) > schema default\n\n"
        )
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=True, default_flow_style=False)
    reset_cache()
    return doc
