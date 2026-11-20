<?php
$a = "each($x) strlen(null) strftime('x') `ls` (real) $x";
$b = <<<SQL
select each(x), get_magic_quotes_gpc() from t
SQL;
// each($arr); (real) $x; function __autoload($c) {}
$c = $a->{'x'};
function f(?T $a = null, $b) {}
function g(...$rest) {}
function h($a = 1, ...$rest) {}
$d = 0 == $x;
$e = $x == 5;
class Modern {
    function __construct() {}
    function Modern() {}
}
$f = (bool) $x;
$g = (int) $x;
$h = (float) $x;
$i = (string) $x;
$j = $o->strftime('x');
$k = Foo::strlen(null);
$l = strlen($x);
class Ser2 implements Serializable { function __serialize(): array { return []; } function __unserialize(array $d): void {} }
switch ($x) {
    case 1:
        break;
    case $x ? 2 : 3:
        break;
}
$o = foo($a ? 1 : 2);
$p = $arr[0];
