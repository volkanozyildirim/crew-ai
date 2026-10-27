<?php
namespace App\Model;

class Product
{
    private string $sku; // @expect property_hooks
    private int $count; // @expect asymmetric_visibility
    protected $legacy; // @expect typed_properties
    public static $cache = []; // @expect typed_properties
    private ?Money $price = null; // @expect asymmetric_visibility (reprice() sınıf içinden yazar; private(set) buna izin verir)

    public function getSku(): string
    {
        return $this->sku;
    }

    public function setSku(string $v): void
    {
        $this->sku = strtoupper($v);
    }

    public function getCount(): int
    {
        return $this->count;
    }

    public function getPrice(): ?Money
    {
        return $this->price;
    }

    public function reprice(Money $m): void
    {
        $this->price = $m;
    }

    public function toRow(): array
    {
        return [ // @expect dto_from_array
            'sku' => $this->sku,
            'count' => $this->count,
            'price' => $this->price,
            'legacy' => $this->legacy,
        ];
    }

    public function small(): array
    {
        return ['a' => 1, 'b' => 2];
    }

    /**
     * @dataProvider skuProvider
     */
    public function testNormalize(string $in): void // @expect attributes
    {
    }
}
