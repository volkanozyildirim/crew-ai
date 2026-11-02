<?php
namespace App\Model;

class OrderStatus // @expect enum
{
    const PAID = 'paid'; // @expect typed_class_constants
    const REFUND = 'refund'; // @expect typed_class_constants
    const CANCELED = 'canceled'; // @expect typed_class_constants
}

class MixedKinds
{
    const A = 'a'; // @expect typed_class_constants
    const B = 2; // @expect typed_class_constants
    const C = 'c'; // @expect typed_class_constants
}

class WithMethods
{
    const X = 'x'; // @expect typed_class_constants
    const Y = 'y'; // @expect typed_class_constants
    const Z = 'z'; // @expect typed_class_constants

    public function run(): string { return self::X; }
}

class ShipmentType // @expect enum
{
    const STANDARD = 1; // @expect typed_class_constants
    const EXPRESS = 2; // @expect typed_class_constants
    const PICKUP = 3; // @expect typed_class_constants

    public static function label(int $t): string { return (string) $t; }
}
