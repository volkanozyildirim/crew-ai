# TypeScript / Next.js — güncel kullanım

İkincil stack (ana yoğunluk PHP + Go). Aşağıdakiler öneridir; blocker olması
için SKILL.md'deki üç kapıdan birine ya da bir Sonar ihlaline girmesi gerekir.

## Tip disiplini

- **`any` yasak.** Bilinmiyorsa `unknown` + daraltma. `any` Sonar'da doğrudan
  code smell'dir ve tip güvenliğini tüm çağrı zincirinde iptal eder.
- `@ts-ignore` yerine `@ts-expect-error` + gerekçe; en iyisi tipi düzeltmek.
- `satisfies` — geniş tipe uyumu doğrula ama dar tipi koru:
  ```ts
  const routes = { home: '/', order: '/order/:id' } satisfies Record<string, string>;
  ```
- Dış sınırdan (API, form, env) gelen veri **çalışma zamanında** doğrulansın
  (zod vb.). `as Foo` ile cast doğrulama değildir.
- `interface` public sözleşmede, `type` birleşim/yardımcı tipte.

## Next.js App Router

- **Server Component varsayılan**; `'use client'` yalnızca gerçekten
  gerekliyse (state, event, browser API). Gereksiz `'use client'` bundle'ı
  şişirir.
- Sunucu tarafı sırları client component'e prop olarak geçme.
- Veri çekme server tarafında; `fetch` cache/revalidate davranışını açıkça
  belirt.
- Server Action'larda girdi doğrulaması ve yetki kontrolü **zorunlu** —
  bunlar public endpoint'tir.
- `next/image`, `next/font` kullan; ham `<img>` ve elle font yükleme Core Web
  Vitals'ı düşürür.

## React

- `useEffect` bağımlılık dizisi eksiksiz olsun; eksik bağımlılık klasik bug.
- Effect'te temizlik döndür (abort, unsubscribe, timer).
- Liste `key`'i kararlı olsun — index değil.
- Türetilebilen değeri state'te tutma.
- `dangerouslySetInnerHTML` → XSS, blocker (sanitize edilmediyse).

## Sık yapılan hatalar (incelemede ara)

- `any`, `@ts-ignore`, `==` (yerine `===`).
- **Floating promise** — `await`siz async çağrı, yakalanmayan hata.
- `console.log` prod kodunda.
- `catch {}` boş.
- Gömülü sır / `NEXT_PUBLIC_` ile sır sızdırma (bu önek client'a gider).
- Döngü içinde `await` — `Promise.all` uygunsa.
- Büyük component (SRP) — 300+ satır, çok sorumluluk.
