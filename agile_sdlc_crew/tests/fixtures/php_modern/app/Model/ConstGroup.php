<?php
namespace App\Model;

class OrderStatus // @expect enum
{
    const PAID = 'paid';
    const REFUND = 'refund';
    const CANCELED = 'canceled';
}

class MixedKinds
{
    const A = 'a';
    const B = 2;
    const C = 'c';
}

class WithMethods
{
    const X = 'x';
    const Y = 'y';
    const Z = 'z';

    public function run(): string { return self::X; }
}

class ShipmentType // @expect enum
{
    const STANDARD = 1;
    const EXPRESS = 2;
    const PICKUP = 3;

    public static function label(int $t): string { return (string) $t; }
}
