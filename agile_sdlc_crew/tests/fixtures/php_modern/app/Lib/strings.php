<?php
namespace App\Lib;

class Strings
{
    // strpos($uri, '/api/') !== false yorumda
    /* isset($a) ? $a : null blok yorumda; list($a) = x; */
    public function run(string $x): string
    {
        $sql = "SELECT 1 WHERE strpos(col, 'x') !== false AND utf8_encode(y) = each(z)";
        $doc = <<<SQL
            isset($a) ? $a : null
            switch (x) { case 1: return 2; default: return 3; }
            array_map([$this, 'normalize'], $rows); list($k) = $v;
            SQL;
        $now = 'substr($x, 0, 4) === "FLO-"';
        return $sql . $doc . $now . $x;
    }
}
