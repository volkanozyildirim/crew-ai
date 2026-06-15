# 03 — discover_repos_task (Repo Keşfetme)

## Kimlik
- **step_key:** `discover_repos_task`
- **Flow metodu:** `_run_discover_repos` (`flow.py:399`), `crew_step4_technical_design` içinden çağrılır
- **Ajan:** `software_architect` (doğrudan LLM `.call()`, crew değil)
- **Görünen ad:** Repo Keşfetme
- **Tetikleyici:** `crew_step4_technical_design` başında çalışır

## Ne yapar
WI bu repolardan **hangisinde** yapılmalı sorusuna **öneri** üretir. LLM'e en
alakalı aday repoların özetlerini (framework, README, Domain, DB Tabloları,
Migration listesi) + birebir kod kanıtını verir, tek repo seçtirir. Sonuç
DB'ye yazılır ve technical_design context'ine hint olarak eklenir.

> **Kritik:** Bu adım `state.repo_name`'i SET ETMEZ — sadece öneridir. Son
> kararı architect (technical_design) kendi JSON'unda `repo_name` yazarak verir.

## Girdi
- `candidate_repos` (technical_design'da hazırlanan aday listesi, ≤25)
- `evidence` (symbol-grep birebir kod kanıtı, `_grep_symbol_evidence` çıktısı)
- `state.requirements_text`, aday repoların `get_repo_summary` özetleri (her biri ≤10K char)

## İşleyiş
1. Aday yoksa veya özetler boşsa atlanır.
2. Her aday için kısa özet (üst dizin listesinden öncesi) toplanır.
3. **Birebir kod kanıtı bloğu** eklenir: bir sembol YALNIZCA tek repoda geçiyorsa
   o repo gerçek sahiptir — repo adı benzerliğini EZER (KN-15).
4. Architect LLM çağrılır; öncelik sırası: (1) exclusive symbol kanıtı,
   (2) tablo/model/dosya sahipliği, (3) repo adı benzerliği (en zayıf).
5. JSON parse edilir (`target_repo`, `reason`, `alternatives`).
6. **Doğrulama** (KN-14): `target` known_repos'ta yoksa görmezden gelinir.

## Çıktı
- DB + vector: `discover_repos_task` (JSON öneri veya "seçim yapamadı")
- `state.repo_name` DEĞİŞMEZ (sadece öneri)

## Karar noktaları
- **KN-14** — Öneri doğrulama (known_repos kontrolü). Bkz. [decision-points.md#kn-14](../decision-points.md#kn-14)
- **KN-15** — Repo seçimi öncelik sırası (exclusive symbol > sahiplik > isim). Bkz. [decision-points.md#kn-15](../decision-points.md#kn-15)

## Resume / dry-run
- Resume yok (technical_design içinde her seferinde çalışır).
- Dry-run: etkilemez.

## Kaynak
- `flow.py:399-532` (`_run_discover_repos`), `flow.py:534-620` (`_grep_symbol_evidence`)
- `tasks.yaml:277-287` (`discover_repos_task`)
