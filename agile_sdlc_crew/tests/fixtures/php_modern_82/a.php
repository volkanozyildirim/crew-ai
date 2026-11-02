<?php
class Box {
    const SIZE = 10;
    const string NAME = 'x';
    const LIST = [1, 2];
    private $a;
    public function __construct(private int $id) {}
    public function set() { $this->a = 1; $this->dyn = 2; $this->arr[] = 3; $this->id = 4; }
    public function login(string $user, string $password, #[\SensitiveParameter] string $token) {
        $x = (new Foo())->bar();
        $y = (new Foo)->baz;
        $z = (new class {})->q();
        $c = get_class();
        $d = get_class($this);
        usort($l, 'self::cmp');
        error_reporting(E_ALL & ~E_STRICT);
        $r = lcg_value();
        trigger_error('x', E_USER_ERROR);
        $h = mb_convert_encoding($s, 'HTML-ENTITIES', 'UTF-8');
        assert_options(ASSERT_ACTIVE, 1);
        mt_srand(1, MT_RAND_PHP);
    }
}
final class Money { public function __construct(public readonly int $amount, public readonly string $cur) {} }
class Child extends Box { public function f() { $this->other = 1; } }
#[\AllowDynamicProperties]
class Bag { public function f() { $this->free = 1; } }
