# Teknik Borç — Aşama 0 + 1a Uygulama Planı

**Spec:** `docs/superpowers/specs/2026-10-19-techdebt-design.md` · **Dal:** `feature/techdebt`
**Yürütme:** Native (bu oturum), her task TDD; testler `tests/test_katman0_gates.py` (`check()`), tek test:
`.venv/bin/python -c "import sys; sys.path.insert(0,'tests'); import test_katman0_gates as t; t.<TEST>(); sys.exit(1 if t.FAIL else 0)"`

## Global kısıtlar
- Kullanıcıya görünen metin Türkçe. Yeni ayarlar varsayılan kapalı. Yeni bağımlılık yok.
- Azure tek kayıt kaynağı; Tempo DB'de backlog yok.
- Kod değişikliğinden sonra sunucu `running:0` kontrolüyle yeniden başlatılır.

## Task 1 — Hat (lane) ve story genelleştirmesi (aşama 0)
- `quality/lanes.py`: `LANES`, `lane_tags(lane, source, needs_human)`, `story_title(lane, source, sprint)`, `story_description(lane, source)`, `story_tags(lane, source)`.
- `render.SOURCE_LABEL.get` fallback; `task_tags(g, lane="quality")`.
- `intake.resolve_story(..., lane="quality")`: techdebt hattında `tempo-techdebt` etiketiyle arar/açar.
- Test `test_techdebt_lane`: techdebt story başlığı/etiketi, quality davranışı değişmedi, techdebt Task etiketleri.

## Task 2 — Task açma yardımcısı + DB kaynak kolonu (aşama 0)
- `intake._open_tasks(client, groups, story, cfg, deps, source, lane, queue=True)` → satırlar + eklenen SP; `take` bunu kullanır.
- `db.SCHEMA` `source VARCHAR(32)`; `init_db` eski ENUM'u `ALTER … MODIFY` ile çevirir.
- Test `test_techdebt_open_tasks_db`: `_open_tasks(queue=False)` iş kuyruğa almaz; mevcut `test_quality_intake` geçer; şema VARCHAR + migration kodu var.

## Task 3 — Elle giriş (aşama 1a)
- `quality/techdebt.py`: `validate_manual(form, repo_ok, repo_root) -> (Group, errors)`, `manual_fingerprint`, `create_manual(client, form, team, deps)`.
- `render`: `manual` kaynağı için gövde (`_manual_body`), beklenen davranış = kullanıcının metni, AC.
- Test `test_techdebt_manual`: zorunlu alanlar, repo yerelde değilse hata, `..`/mutlak dosya yolu reddi, SP>3 → needs-human, parmak izi kararlı, board'da aynı fp açıksa reddedilir, Task techdebt story altında açılır ve kuyruğa alınmaz.

## Task 4 — Liste + kuyruk (aşama 1a)
- `techdebt.list_items(client, team, deps)` (WIQL: takım alanı + `tech-debt` + açık durum), `techdebt.queue(client, ids, team, deps)`.
- Test `test_techdebt_list_queue`: satır alanları (repo, source, needs_human, queueable), needs-human reddi, liste dışı id reddi, tavan, `queue_selected` açık işi atlar.

## Task 5 — Endpoint'ler + ayarlar + readiness muafiyeti
- Knob'lar `CREW_TECHDEBT` (false), `CREW_TECHDEBT_MAX_QUEUE` (5).
- `GET /api/techdebt`, `POST /api/techdebt/manual`, `POST /api/techdebt/queue`; kapalı→403, geçersiz→400, Busy→409, Azure→502.
- `flow._quality_skips_readiness`: `tempo-src-manual` muaf değil.
- Test `test_techdebt_endpoints`.

## Task 6 — Arayüz
- Üst barda **Teknik borç** sekmesi, `paneTechdebt`; Kalite kartı Genel bakış'tan buraya taşınır; liste, filtre, çoklu seçim, elle giriş formu; `loadTechdebt()`.
- Test `test_techdebt_ui` (html grep) + mevcut `test_quality_endpoints` geçer.

## Task 7 — Doküman, sunucu, canlı doğrulama
- CLAUDE.md notu; tüm test dosyası; sunucu restart; `CREW_TECHDEBT` aç; canlı **liste** (yazmasız). Canlı elle giriş/kuyruk yazma işlemleri kullanıcı onayıyla.
