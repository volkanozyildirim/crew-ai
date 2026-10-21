<?php
declare(strict_types=1);

namespace App\Model;

enum Status: string
{
    case Paid = 'paid';
    case Refund = 'refund';
}

#[Attribute]
final readonly class Modern
{
    public function __construct(private HttpClient $client, public int $count = 0)
    {
    }

    public function find(?string $sku = null): ?string
    {
        $city = $this->client->addr['city'] ?? null;
        $name = $this->client->getCustomer()?->getName();
        if (str_contains($sku ?? '', '/api/') || str_starts_with($sku ?? '', 'FLO-')) {
            return $city . $name;
        }
        [$a, $b] = explode(':', $sku ?? ':', 2);
        return match ($a) {
            'x' => $b,
            default => null,
        };
    }

    public function label(Status $s): string
    {
        $rows = array_map($this->normalize(...), [$s->value]);
        return $rows[0];
    }

    private function normalize(string $r): string { return $r; }
}
