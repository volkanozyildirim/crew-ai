"""PR Fix — PR yorumlarindaki geri bildirimlere gore kodu duzelt.

Tam pipeline calistirmaz. Mevcut branch'teki kodu okur, PR yorumlarini
analiz eder, sadece eksikleri/hatalari duzeltir ve push eder.

Akis:
1. PR detaylarini oku (branch, dosyalar)
2. PR yorumlarini oku (reviewer feedback)
3. Etkilenen dosyalari branch'ten oku
4. Architect: yorumlara gore duzeltme plani olustur
5. Developer: dosyalari duzelt
6. Push: branch'e push et
"""

import logging
import re

from agile_sdlc_crew.tools.azure_devops_base import AzureDevOpsClient
from agile_sdlc_crew.tools.local_repo import LocalRepoManager
from agile_sdlc_crew.pipeline import push_file

log = logging.getLogger("pipeline")


def run_pr_fix(repo_name: str, pr_id: int, work_item_id: str = "") -> dict:
    """PR yorumlarini okuyup kodu duzeltir.

    Returns: {"fixed_files": int, "pushed": int, "comments_analyzed": int}
    """
    client = AzureDevOpsClient()
    repo_mgr = LocalRepoManager()

    # 1. PR detaylari
    log.info(f"\n-- PR FIX: PR #{pr_id} ({repo_name}) --")
    pr = client.get_pull_request(repo_name, pr_id)
    branch = pr.get("sourceRefName", "").replace("refs/heads/", "")
    if not branch:
        raise RuntimeError(f"PR #{pr_id} branch bilgisi alinamadi")
    log.info(f"  Branch: {branch}")

    # WI ID — PR'dan veya parametreden
    if not work_item_id:
        title = pr.get("title", "")
        wi_match = re.search(r'#(\d+)', title)
        if wi_match:
            work_item_id = wi_match.group(1)
    log.info(f"  WI: #{work_item_id or 'bilinmiyor'}")

    # 2. PR yorumlarini oku
    comments = client.get_pr_comments_text(repo_name, pr_id)
    if not comments:
        log.info("  PR'da yorum yok, duzeltme yapilmiyor")
        return {"fixed_files": 0, "pushed": 0, "comments_analyzed": 0}

    # 2b. Thread bilgilerini al — resolve icin thread ID lazim
    threads = client.get_pr_threads(repo_name, pr_id)
    # Thread → dosya eslesmesi: {file_path: [(thread_id, comment_content)]}
    file_threads: dict[str, list[tuple[int, str]]] = {}
    general_threads: list[tuple[int, str]] = []
    for thread in threads:
        # Sistem/bot thread'lerini atla
        if thread.get("properties", {}).get("CodeReviewThreadType"):
            continue
        # Zaten resolved/fixed olanlari atla
        status = thread.get("status", "")
        if status in ("fixed", "closed", "wontFix", "byDesign"):
            continue
        thread_id = thread.get("id")
        if not thread_id:
            continue
        # Insan yorumu var mi?
        human_content = ""
        for comment in thread.get("comments", []):
            if comment.get("commentType") == "system":
                continue
            content = comment.get("content", "").strip()
            author = comment.get("author", {}).get("displayName", "")
            if content and "Agile SDLC Crew" not in content:
                human_content = content
                break
        if not human_content:
            continue
        # Dosya baglami
        ctx_thread = thread.get("threadContext")
        if ctx_thread and ctx_thread.get("filePath"):
            fp = ctx_thread["filePath"]
            file_threads.setdefault(fp, []).append((thread_id, human_content))
        else:
            general_threads.append((thread_id, human_content))

    all_active_threads = sum(len(v) for v in file_threads.values()) + len(general_threads)
    log.info(f"  {all_active_threads} aktif insan yorumu (dosya bazli: {len(file_threads)}, genel: {len(general_threads)})")

    if all_active_threads == 0:
        log.info("  Resolve edilecek yorum yok, duzeltme yapilmiyor")
        return {"fixed_files": 0, "pushed": 0, "comments_analyzed": 0}

    # Yorum ozetini olustur (developer context icin)
    feedback_parts = []
    mentioned_files = set()
    for fp, thread_list in file_threads.items():
        mentioned_files.add(fp)
        for _, content in thread_list:
            feedback_parts.append(f"[{fp}] {content[:300]}")
    for _, content in general_threads:
        feedback_parts.append(f"[genel] {content[:300]}")
    feedback_text = "\n\n".join(feedback_parts)

    # Eski format uyumu: human_comments listesi
    human_comments = []
    for fp, thread_list in file_threads.items():
        for _, content in thread_list:
            human_comments.append({"file_path": fp, "content": content})
    for _, content in general_threads:
        human_comments.append({"content": content})
    log.info(f"  Bahsedilen dosyalar: {mentioned_files or 'genel yorum'}")

    # 3. PR'daki degisen dosyalari bul
    pr_changes = client.get_pull_request_changes(repo_name, pr_id)
    changed_files = []
    for entry in pr_changes:
        item = entry.get("item", {})
        path = item.get("path", "")
        if path and not path.endswith("/"):
            changed_files.append(path)
    log.info(f"  PR'da {len(changed_files)} degisen dosya")

    # Hangi dosyalari duzeltecegiz?
    # Yorumda spesifik dosya varsa onlari, yoksa tum degisen dosyalari
    if mentioned_files:
        target_files = [f for f in changed_files if f in mentioned_files]
        if not target_files:
            # Yorumdaki dosya adi kisa olabilir (SettingsPage.tsx vs /frontend/src/pages/SettingsPage.tsx)
            for mf in mentioned_files:
                mf_name = mf.rsplit("/", 1)[-1].lower()
                for cf in changed_files:
                    if cf.lower().endswith(mf_name):
                        target_files.append(cf)
        if not target_files:
            target_files = changed_files  # fallback
    else:
        target_files = changed_files

    log.info(f"  Duzeltilecek dosyalar: {target_files}")

    # 4. Local repo'yu guncelle ve branch'e gec
    try:
        repo_dir = repo_mgr.base_dir / repo_name
        if repo_dir.exists():
            repo_mgr._git(["fetch", "origin", branch], cwd=repo_dir)
            repo_mgr._git(["checkout", branch], cwd=repo_dir)
            repo_mgr._git(["reset", "--hard", f"origin/{branch}"], cwd=repo_dir)
            log.info(f"  Local repo {branch} branch'ine guncellendi")
    except Exception as e:
        log.warning(f"  Local repo guncelleme hatasi: {e}")

    # 5. Dosyalari oku ve duzelt
    from agile_sdlc_crew.crew import AgileSDLCCrew
    crew_instance = AgileSDLCCrew()
    crew_instance.local_repo_mgr = repo_mgr

    pushed = 0
    for file_path in target_files:
        log.info(f"\n  PR-fix [{file_path}]")

        # Mevcut kodu branch'ten oku
        existing_content = ""
        try:
            existing_content = repo_mgr.get_file_content(repo_name, file_path, branch)
            log.info(f"    Mevcut kod: {len(existing_content)} karakter")
        except Exception:
            try:
                existing_content = client.get_file_content(repo_name, file_path, branch)
                log.info(f"    Mevcut kod (API): {len(existing_content)} karakter")
            except Exception as e:
                log.warning(f"    Dosya okunamadi: {e}, atlaniyor")
                continue

        if not existing_content.strip():
            continue

        # Bu dosyaya ozel yorumlar
        file_feedback = [c for c in human_comments if c.get("file_path") == file_path]
        general_feedback = [c for c in human_comments if not c.get("file_path")]
        specific_feedback = "\n".join(
            f"- {c['content'][:200]}" for c in (file_feedback or general_feedback)
        )

        # Developer'a duzeltme yaptir
        code_crew = crew_instance.create_code_crew()
        code_result = code_crew.kickoff(inputs={
            "work_item_id": work_item_id,
            "target_repo": repo_name,
            "target_file": file_path,
            "change_description": (
                f"PR REVIEW DUZELTMESI\n\n"
                f"Reviewer asagidaki geri bildirimi verdi:\n{specific_feedback}\n\n"
                f"Mevcut kodu bu geri bildirime gore duzelt. "
                f"Sadece belirtilen sorunu coz, baska degisiklik YAPMA."
            ),
            "current_code": existing_content[:6000],
            "new_code": existing_content[:6000],  # mevcut kodu ver, duzeltsin
            "previous_context": (
                f"# PR REVIEW GERI BILDIRIMI\n{feedback_text[:3000]}\n\n"
                f"# DUZELTILECEK DOSYA: {file_path}\n"
                f"Amac: reviewer'in belirttigi eksikleri/hatalari duzelt.\n"
                f"Tum dosyayi dondur — sadece gereken degisiklikleri yap."
            ),
        })

        from agile_sdlc_crew.flow import _extract_dev_output
        fixed_content = _extract_dev_output(code_result)
        if not fixed_content or len(fixed_content.strip()) < 30:
            log.warning(f"    Developer bos/kisa cikti, atlaniyor")
            continue

        # Boyut kontrolu
        orig_lines = len(existing_content.splitlines())
        new_lines = len(fixed_content.splitlines())
        if new_lines < orig_lines * 0.3:
            log.warning(f"    GUVENLIK: yeni kod ({new_lines} satir) << mevcut ({orig_lines}), atlaniyor")
            continue

        log.info(f"    Duzeltilmis kod: {len(fixed_content)} karakter ({new_lines} satir)")

        # Push
        commit_msg = f"fix(pr-review): {file_path.rsplit('/', 1)[-1]} — reviewer feedback (WI #{work_item_id})"
        result = push_file(repo_name, branch, file_path, fixed_content, commit_msg)
        if result.get("success"):
            pushed += 1
            log.info(f"    Push OK")

            # Bu dosyaya ait thread'lere yanit yaz + resolve et
            threads_for_file = file_threads.get(file_path, [])
            for thread_id, thread_content in threads_for_file:
                try:
                    # Degisikligi ozetle (ilk 200 char)
                    import difflib
                    diff_lines = list(difflib.unified_diff(
                        existing_content.splitlines()[:30],
                        fixed_content.splitlines()[:30],
                        lineterm="",
                    ))
                    diff_preview = "\n".join(diff_lines[:15]) if diff_lines else "(degisiklik yapildi)"

                    client.reply_to_pr_thread(
                        repo_name, pr_id, thread_id,
                        f"**Otomatik duzeltme yapildi.**\n\n"
                        f"Commit: `{commit_msg}`\n\n"
                        f"```diff\n{diff_preview}\n```\n\n"
                        f"---\n*Agile SDLC Crew - PR Fix*"
                    )
                    client.resolve_pr_thread(repo_name, pr_id, thread_id)
                    log.info(f"    Thread #{thread_id} yanitlandi ve resolve edildi")
                except Exception as e:
                    log.warning(f"    Thread #{thread_id} resolve hatasi: {e}")
        else:
            log.warning(f"    Push HATA: {result.get('error')}")

    # 6. Genel thread'leri de yanıtla (dosya bazlı olmayanlar)
    if pushed > 0 and general_threads:
        for thread_id, thread_content in general_threads:
            try:
                client.reply_to_pr_thread(
                    repo_name, pr_id, thread_id,
                    f"**{pushed} dosyada otomatik duzeltme yapildi.**\n\n"
                    f"---\n*Agile SDLC Crew - PR Fix*"
                )
                client.resolve_pr_thread(repo_name, pr_id, thread_id)
                log.info(f"  Genel thread #{thread_id} resolve edildi")
            except Exception as e:
                log.warning(f"  Genel thread #{thread_id} resolve hatasi: {e}")

    # 7. Ozet yorum
    if pushed > 0:
        resolved_count = sum(len(v) for v in file_threads.values()) + len(general_threads)
        try:
            client.add_pr_comment(
                repo_name, pr_id,
                f"## Otomatik Duzeltme Ozeti\n\n"
                f"**{pushed}** dosya duzeltildi, **{resolved_count}** yorum resolve edildi.\n\n"
                f"Lutfen degisiklikleri tekrar inceleyin.\n\n"
                f"---\n*Agile SDLC Crew - PR Fix*"
            )
        except Exception as e:
            log.warning(f"  PR ozet yorum hatasi: {e}")

    log.info(f"\n  PR FIX TAMAMLANDI: {pushed}/{len(target_files)} dosya duzeltildi")
    return {
        "fixed_files": len(target_files),
        "pushed": pushed,
        "comments_analyzed": len(human_comments),
    }
