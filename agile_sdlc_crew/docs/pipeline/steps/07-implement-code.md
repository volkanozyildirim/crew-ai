# 07 — implement_change_task (Kod Yazma & Push)

## Kimlik
- **step_key:** `implement_change_task`
- **Flow metodu:** `step6_implement_code` (`flow.py:2419`)
- **Ajan:** `senior_developer`
- **Görünen ad:** Kod Yazma & Push
- **Tetikleyici:** `@listen(step5_create_branch)`
- **Sonraki:** `step7_create_pr`

## Ne yapar
Plan'daki her `change` için dosyayı uygular ve branch'e push eder. Mümkün olduğunca
**LLM çağırmadan** Python'da düzenleme yapar (direct-edit); olmazsa LLM'den blok
ister; en son çare append. Her push öncesi **güvenlik kontrolleri** uygulanır.

## Girdi
- `state.plan` (`changes[]`), `state.repo_name`, `state.branch_name`, `state.dry_run`
- env/knob: `CREW_DEV_CONTEXT_BUDGET` (12000), `CREW_DEV_CONTEXT_PER_FILE` (2000)

## İşleyiş (her dosya için)
1. **Skip kontrolü** (KN-24): branch'te TAM aynı içerik zaten push edilmişse atla
   (yalnızca tam eşleşme; prefix eşleşmesi yeni branch'te yanlış pozitif verir).
2. Mevcut dosya içeriği okunur (local öncelik, yoksa repo'da benzer dosya aranır,
   bulunamazsa `change_type=add`).
3. **Uygulama stratejisi seçimi** (KN-25):
   - `add` + new_code → append veya yeni dosya
   - full+new+current → **direct-edit önce** (`_try_direct_edit`, 4 katman fuzzy);
     başarısızsa LLM'den "SADECE YENİ BLOK" (MODE B) → Python fuzzy-replace;
     o da olmazsa append
   - current_code ≫ new_code (büyük kayıp riski) → append'e yönlendir (KN-26)
   - new_code yok → LLM'e tam dosya yazdır
4. **Kod doğrulama** (`_validate_code`); başarısızsa LLM ile bir kez düzeltme denenir.
5. **Güvenlik kontrolleri (push öncesi)** (KN-26, `flow.py:2661`):
   - add modunda dosya kısaldıysa → iptal
   - edit'te orijinal >500 char ve yeni <%50 → iptal (truncate şüphesi)
   - <50 char veya <3 satır → iptal
6. `push_file` ile push (dry-run'da local commit).

## Çıktı
- `state.all_pushes` (push edilen dosyalar listesi)
- DB + vector: `implement_change_task` ("N dosya push edildi")

## Karar noktaları
- **KN-24** — Skip: aynı içerik zaten push edilmiş. Bkz. [decision-points.md#kn-24](../decision-points.md#kn-24)
- **KN-25** — Uygulama stratejisi (direct-edit / LLM blok / append). Bkz. [decision-points.md#kn-25](../decision-points.md#kn-25)
- **KN-26** — Push öncesi güvenlik kontrolleri (kod kaybı koruması). Bkz. [decision-points.md#kn-26](../decision-points.md#kn-26)
- **KN-22** — Budget guard. Bkz. [decision-points.md#kn-22](../decision-points.md#kn-22)

## Resume / dry-run
- Resume yok (her dosya state'e göre yeniden değerlendirilir).
- Dry-run: push yerine local commit.

## Kaynak
- `flow.py:2419-2704` (`step6_implement_code`)
- `tasks.yaml:515-630` (`implement_change_task`)
- `agents.yaml:92-197` (`senior_developer` — MODE A/B kuralları)
- `main._try_direct_edit`, `main._validate_code`, `flow._extract_dev_output`
