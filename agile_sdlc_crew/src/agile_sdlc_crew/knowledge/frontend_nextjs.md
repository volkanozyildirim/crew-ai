# Frontend Development Guide — Next.js / React / TypeScript

## Proje Yapisi (Next.js App Router)

```
app/                — App Router (pages, layouts, route handlers)
  [route]/page.tsx
  [route]/layout.tsx
  api/[route]/route.ts
components/         — paylasimli UI components
  ui/               — shadcn/button, input, etc
  features/         — feature-specific
lib/                — utils, helpers, clients
hooks/              — custom React hooks
types/              — TypeScript tipleri
```

## Component Yazimi

### Typed component
```tsx
interface OrderCardProps {
  order: Order;
  onCancel?: (id: string) => void;
}

export function OrderCard({ order, onCancel }: OrderCardProps) {
  return (
    <div className="rounded-lg border p-4">
      <h3 className="font-semibold">{order.number}</h3>
      {onCancel && (
        <button onClick={() => onCancel(order.id)}>
          Iptal Et
        </button>
      )}
    </div>
  );
}
```

### Server vs Client Components
- Default: **Server Component** — async/await kullanilabilir, `useState`/`useEffect` kullanilamaz
- `"use client"` directive: interaktivite gerekli olanlarda (form, button handler)
- Mumkun olan maksimum kismi Server Component olarak tut

### Data Fetching
- Server Component'te: `async function` icinde `await fetch()` veya DB call
- Client Component'te: `SWR` / `React Query` (`useSWR`, `useQuery`)
- Cache stratejisi: `fetch(url, { next: { revalidate: 60 } })`

### State Management
- **Local state**: `useState` (component icinde)
- **URL state**: `useSearchParams`, `useRouter` (shareable, persisted)
- **Server state**: React Query / SWR (DB/API data)
- **Global client state**: Zustand veya Context (minimal kullan)

## Kritik Kurallar

1. **Type everything** — `any` kullanma, `unknown` varsa discriminated union
2. **Key prop**: `.map()` icinde unique `key` — index kullanma
3. **useEffect dependency**: ESLint exhaustive-deps uy
4. **Side effect Server Component'te YOK** — sadece render
5. **Event handler inline degil** — `const handleClick = useCallback(...)` re-render onle
6. **Image**: `<img>` yerine `next/image`
7. **Link**: `<a>` yerine `next/link`
8. **Loading/Error state**: her async component'te handle et

## Styling

- **Tailwind CSS** — utility-first
- **shadcn/ui** — kopyalanabilir typed components (button, input, dialog)
- Condition: `cn('base-class', isActive && 'active-class')` — tailwind-merge
- CSS-in-JS AZ kullan — Tailwind tercih edilir

## Form Handling

```tsx
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';

const schema = z.object({
  email: z.string().email(),
  name: z.string().min(2),
});

type FormData = z.infer<typeof schema>;

function OrderForm() {
  const { register, handleSubmit, formState: { errors } } = useForm<FormData>({
    resolver: zodResolver(schema),
  });

  const onSubmit = async (data: FormData) => {
    await fetch('/api/orders', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  };

  return (
    <form onSubmit={handleSubmit(onSubmit)}>
      <input {...register('email')} />
      {errors.email && <span>{errors.email.message}</span>}
    </form>
  );
}
```

## Performance

- **Code splitting**: `dynamic(() => import('./Heavy'))` — lazy load
- **Memoization**: `React.memo`, `useMemo`, `useCallback` gercekten gerektiginde
- **Suspense**: `<Suspense fallback={<Loading/>}>` — streaming
- **Bundle size**: `next-bundle-analyzer` ile kontrol

## Code Review Checklist

- [ ] TypeScript error yok (`tsc --noEmit`)
- [ ] ESLint warning yok
- [ ] Component split: 300+ satir ise parcala
- [ ] Accessibility: aria-label, semantic HTML, keyboard nav
- [ ] SEO: `<head>` metadata, title
- [ ] Loading/error state var mi?
- [ ] Mobile responsive mi?
- [ ] i18n: hardcoded string yerine translation key
