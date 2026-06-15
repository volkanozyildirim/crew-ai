# 04 — dependency_analysis_task (Bağımlılık Analizi)

## Kimlik
- **step_key:** `dependency_analysis_task`
- **Flow metodu:** ayrı metod yok — `crew_step4_technical_design` başında "atlandı"
  olarak işaretlenir (`flow.py:1698`); HAL yolunda `flow.py:1101`'de atlanır
- **Ajan:** `software_architect` (nominal)
- **Görünen ad:** Bağımlılık Analizi

## Ne yapar
**Bu adım fiilen ÇALIŞMAZ — atlanır.** Tarihsel olarak repo yapısını/bağımlılıkları
ajan tool'larıyla inceleyen ayrı bir adımdı. Artık gereksiz: repo bilgisi
local'den (REPO_SUMMARY.md + dosya pre-fetch) alınıyor, bu yüzden
`technical_design_task` başında doğrudan "Atlandı — repo bilgisi local'den
alınıyor" mesajıyla `_step_done` çağrılır.

tasks.yaml'da tanımı hâlâ durur (`browse_repo` ile dizin/dependency analizi) ama
flow bu task'ı bir crew'a kickoff ETMEZ.

## Neden duruyor?
- STEP_DEFINITIONS ve dashboard'da görünür kalsın diye step_key korunuyor.
- Gelecekte tekrar etkinleştirilebilir (env-toggle ile) — şu an YAGNI.

## Karar noktaları
Yok (koşulsuz atlanır).

## Kaynak
- `flow.py:1698` (CrewAI yolu — done işaretleme)
- `flow.py:1101-1105` (HAL yolu — skip)
- `tasks.yaml:289-299` (`dependency_analysis_task` — kullanılmayan tanım)
