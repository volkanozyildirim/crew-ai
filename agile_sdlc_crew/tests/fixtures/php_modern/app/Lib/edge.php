<?php
namespace App\Lib;

class Edge
{
    private $log; // @expect typed_properties
    private $h; // @expect typed_properties
    private $items; // @expect typed_properties

    public function __construct(Logger $logger, callable $h, ...$items) // ad uyuşmuyor / callable / variadic → promotion yok
    {
        $this->log = $logger;
        $this->h = $h;
        $this->items = $items;
    }

    public function byRef($a, &$error = null, #[A] string $x = null) // @expect implicit_nullable
    {
        return [$a, $error, $x];
    }

    public function &getRef()
    {
        return $this->items;
    }

    public function strings(string $s, string $h, string $n, $o, $f, $a, array $x)
    {
        if (strpos($h, $n, 5) !== false) { $s = ''; }              // offset: str_contains yok
        if (strpos($s, 'x') == 0) { $s = ''; }                    // gevşek karşılaştırma: tuzak, önerilmez
        if (substr($s, 0, 3) === 'FLO-') { $s = ''; }             // uzunluk ≠ literal: önek testi değil
        if (substr($s, 0, -1) === 'abc') { $s = ''; }
        $t = $o->c() ? $o->c()->total() * 100 : null;              // then dalı zincir + ifade: önerilmez
        $u = !$a->b ? $a->b->c : null;                             // önünde ! : önerilmez
        $v = $f && $a->b !== null ? $a->b->c : null;               // önünde && : önerilmez
        $w = $f && isset($x['k']) ? $x['k'] : null;                // önünde && : önerilmez
        $z = $a->b !== null ? $a->b->c : null; // @expect nullsafe
        $p = isset($x['pw']) ? $x['pw'] : 'password=Secret123!'; // @expect null_coalesce
        return [$t, $u, $v, $w, $z, $p];
    }

    public function fall(string $k): int
    {
        switch ($k) {                                              // düşme (fall-through): match önerilmez
            case 'a': $l = 1;
            case 'b': $l = 2; break;
            default: $l = 0;
        }
        return $l;
    }

    public function noDefault(string $k): int
    {
        switch ($k) {                                              // default yok: eşleşmeyen değer sessiz geçer, match fırlatır
            case 'a': return 1;
            case 'b': return 2;
        }
        return 0;
    }

    public function intLabels(int $k): string
    {
        switch ($k) {                                              // int etiket: gevşek/sıkı farkı, otonom önerilmez
            case 1: return 'a';
            case 2: return 'b';
            default: return 'c';
        }
    }

    public function callbacks(array $rows, $cb, $m)
    {
        $a = array_map(fn($c) => $rows[$c], ['id', 'name']);
        $b = call_user_func_array([$this, $m], ['foo', 'bar']);
        $c = call_user_func($cb, 'hello');
        array_walk($rows, $cb, 'prefix');
        $d = array_reduce($rows, $cb, 'init');
        $e = usort($rows, 'strcmp'); // @expect first_class_callable
        return [$a, $b, $c, $d, $e];
    }
}
