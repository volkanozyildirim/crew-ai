<?php
/**
 * PHP modernleştirme tarayıcısı — token_get_all ile, LLM yok.
 *
 * Kullanım: php php_modern_scan.php <repo kökü> [virgüllü hariç liste]
 * Çıktı (stdout, JSON): {"files": N, "hits": [{"rule","file","line","text"}], "adoption": {...}, "errors": [...]}
 *
 * String / heredoc / yorum token'ları tek token'dır; içlerindeki kalıplar
 * (ör. SQL içindeki `strpos(...) !== false`) asla bulgu üretmez — grep'in
 * `each(` → `foreach(` tuzağı burada yoktur. Kural anahtarları
 * config/php_modern_rules.yaml ile aynıdır.
 */

declare(strict_types=1);

$root = rtrim((string) ($argv[1] ?? ''), '/');
$exclude = array_values(array_filter(array_map('trim', explode(',', (string) ($argv[2] ?? '')))));
if ($root === '' || !is_dir($root)) {
    fwrite(STDERR, "kök dizin yok\n");
    exit(2);
}

final class Scanner
{
    /** @var array<int, array{id:int, text:string, line:int}> */
    private array $t = [];
    private int $n = 0;
    private string $file = '';
    /** @var string[] */
    private array $lines = [];
    public array $hits = [];
    public array $adoption = [
        'enum' => 0, 'readonly' => 0, 'match' => 0, 'nullsafe' => 0, 'attribute' => 0,
        'typed_props' => 0, 'untyped_props' => 0, 'strict_types_files' => 0,
    ];

    private const CALLBACK_FUNCS = ['array_map', 'array_filter', 'usort', 'uasort', 'uksort', 'array_walk',
        'call_user_func', 'call_user_func_array', 'array_reduce', 'iterator_apply'];
    private const REMOVED_FUNCS = ['each', 'create_function', 'money_format'];
    private const ENUMISH_NAME = '/(Status|Type|Kind|State|Enum|Constants?|Codes?)$/';
    private const ANNOTATIONS = '/@(Route|Assert|ORM|dataProvider|test\b|depends|covers|Serializer|Groups)\b/';

    public function scanFile(string $file, string $rel): void
    {
        $code = (string) file_get_contents($file);
        $this->file = $rel;
        $this->lines = preg_split('/\R/', $code) ?: [];
        $raw = @token_get_all($code);
        if (!is_array($raw)) {
            return;
        }
        $this->t = [];
        $line = 1;
        $hasStrict = false;
        foreach ($raw as $i => $tok) {
            if (is_array($tok)) {
                [$id, $text, $line] = $tok;
                if ($id === T_DOC_COMMENT && preg_match(self::ANNOTATIONS, $text)) {
                    $this->hit('attributes', $this->nextCodeLine($raw, $i, $line));
                }
                if ($id === T_WHITESPACE || $id === T_COMMENT || $id === T_DOC_COMMENT || $id === T_INLINE_HTML || $id === T_OPEN_TAG) {
                    continue;
                }
                if ($id === T_DECLARE) {
                    $hasStrict = $hasStrict || (bool) preg_match('/strict_types\s*=\s*1/', substr($code, 0, 400));
                }
                $this->t[] = ['id' => $id, 'text' => $text, 'line' => $line];
            } else {
                $this->t[] = ['id' => 0, 'text' => $tok, 'line' => $line];
            }
        }
        $this->n = count($this->t);
        if ($hasStrict) {
            $this->adoption['strict_types_files']++;
        }
        $this->walk();
    }

    /** Docblock'tan sonraki ilk kod token'ının satırı (anotasyon metot başlığına bağlanır). */
    private function nextCodeLine(array $raw, int $i, int $fallback): int
    {
        for ($j = $i + 1; $j < count($raw); $j++) {
            $tok = $raw[$j];
            if (is_array($tok)) {
                if (in_array($tok[0], [T_WHITESPACE, T_COMMENT, T_DOC_COMMENT, T_PUBLIC, T_PROTECTED, T_PRIVATE,
                    T_STATIC, T_ABSTRACT, T_FINAL, T_ATTRIBUTE], true)) {
                    continue;
                }
                return $tok[2];
            }
            return $fallback;
        }
        return $fallback;
    }

    private function hit(string $rule, int $line): void
    {
        $key = $rule . ':' . $line;
        if (isset($this->hits[$key])) {
            return;
        }
        $text = trim((string) ($this->lines[$line - 1] ?? ''));
        $this->hits[$key] = ['rule' => $rule, 'file' => $this->file, 'line' => $line, 'text' => mb_substr($text, 0, 200)];
    }

    // ── token yardımcıları ──
    private function id(int $i): int { return $this->t[$i]['id'] ?? -1; }
    private function tx(int $i): string { return $this->t[$i]['text'] ?? ''; }
    private function ln(int $i): int { return $this->t[$i]['line'] ?? 0; }
    private function is(int $i, string $ch): bool { return $this->id($i) === 0 && $this->tx($i) === $ch; }
    private function isWord(int $i, string $w): bool { return $this->id($i) === T_STRING && strcasecmp($this->tx($i), $w) === 0; }

    /** $i açılış ( [ { ise eşleşen kapanışın indeksi; yoksa -1. */
    private function closeOf(int $i): int
    {
        $open = $this->tx($i);
        $close = ['(' => ')', '[' => ']', '{' => '}'][$open] ?? null;
        if ($close === null || $this->id($i) !== 0) {
            return -1;
        }
        $d = 0;
        for ($j = $i; $j < $this->n; $j++) {
            if ($this->id($j) === 0 && $this->tx($j) === $open) $d++;
            elseif ($this->id($j) === 0 && $this->tx($j) === $close && --$d === 0) return $j;
            elseif ($this->id($j) === T_CURLY_OPEN || $this->id($j) === T_DOLLAR_OPEN_CURLY_BRACES) { if ($open === '{') $d++; }
        }
        return -1;
    }

    /** Parantez içi argümanları derinlik-0 virgüllerden böl: [[start,end], ...] (kapsayıcı). */
    private function args(int $open, int $close): array
    {
        $out = [];
        $d = 0;
        $s = $open + 1;
        for ($j = $open + 1; $j < $close; $j++) {
            if ($this->id($j) === 0) {
                $c = $this->tx($j);
                if ($c === '(' || $c === '[' || $c === '{') $d++;
                elseif ($c === ')' || $c === ']' || $c === '}') $d--;
                elseif ($c === ',' && $d === 0) { $out[] = [$s, $j - 1]; $s = $j + 1; }
            } elseif ($this->id($j) === T_CURLY_OPEN || $this->id($j) === T_DOLLAR_OPEN_CURLY_BRACES) {
                $d++;
            }
        }
        if ($s <= $close - 1) $out[] = [$s, $close - 1];
        return $out;
    }

    private function text(int $a, int $b): string
    {
        $s = '';
        for ($j = $a; $j <= $b; $j++) $s .= $this->tx($j) . ' ';
        return trim($s);
    }

    private function isCall(int $i): bool
    {
        if ($this->id($i) !== T_STRING || !$this->is($i + 1, '(')) return false;
        $p = $this->id($i - 1);
        if (in_array($p, [T_OBJECT_OPERATOR, T_NULLSAFE_OBJECT_OPERATOR, T_DOUBLE_COLON, T_FUNCTION, T_CONST, T_NEW], true)) return false;
        if ($p === T_NS_SEPARATOR && $this->id($i - 2) === T_STRING) return false; // Foo\bar()
        return true;
    }

    private function isNullTok(int $i): bool { return $this->isWord($i, 'null'); }

    /** $i'den başlayan değişken zinciri ($a->b()->c['x']) sonu (kapsayıcı); zincir değilse -1. */
    private function chainEnd(int $i): int
    {
        if ($this->id($i) !== T_VARIABLE) return -1;
        $j = $i;
        while (true) {
            $k = $j + 1;
            if (($this->id($k) === T_OBJECT_OPERATOR || $this->id($k) === T_NULLSAFE_OBJECT_OPERATOR) && ($this->id($k + 1) === T_STRING || $this->id($k + 1) === T_VARIABLE)) {
                $j = $k + 1;
                continue;
            }
            if ($this->id($k) === T_DOUBLE_COLON && $this->id($k + 1) === T_STRING) { $j = $k + 1; continue; }
            if ($this->is($k, '(') || $this->is($k, '[')) {
                $c = $this->closeOf($k);
                if ($c < 0) return $j;
                $j = $c;
                continue;
            }
            return $j;
        }
    }

    /** $a..$b aralığında derinlik-0'da verilen tek karakter token'ı bul. */
    private function findDepth0(int $a, int $b, string $ch): int
    {
        $d = 0;
        for ($j = $a; $j <= $b; $j++) {
            if ($this->id($j) === 0) {
                $c = $this->tx($j);
                if ($c === '(' || $c === '[' || $c === '{') $d++;
                elseif ($c === ')' || $c === ']' || $c === '}') $d--;
                elseif ($c === $ch && $d === 0) return $j;
            } elseif ($this->id($j) === T_CURLY_OPEN || $this->id($j) === T_DOLLAR_OPEN_CURLY_BRACES) $d++;
        }
        return -1;
    }

    /** İfade sonu: ; , ) ] derinlik-0 (başlangıçtan itibaren). */
    private function exprEnd(int $a): int
    {
        $d = 0;
        for ($j = $a; $j < $this->n; $j++) {
            if ($this->id($j) === 0) {
                $c = $this->tx($j);
                if ($c === '(' || $c === '[' || $c === '{') $d++;
                elseif ($c === ')' || $c === ']' || $c === '}') { if ($d === 0) return $j - 1; $d--; }
                elseif (($c === ';' || $c === ',') && $d === 0) return $j - 1;
            } elseif ($this->id($j) === T_CURLY_OPEN || $this->id($j) === T_DOLLAR_OPEN_CURLY_BRACES) $d++;
        }
        return $this->n - 1;
    }

    // ── ana döngü ──
    private function walk(): void
    {
        for ($i = 0; $i < $this->n; $i++) {
            $id = $this->id($i);
            switch ($id) {
                case T_ENUM: if ($this->id($i + 1) === T_STRING) $this->adoption['enum']++; break;
                case T_READONLY: $this->adoption['readonly']++; break;
                case T_MATCH: $this->adoption['match']++; break;
                case T_NULLSAFE_OBJECT_OPERATOR: $this->adoption['nullsafe']++; break;
                case T_ATTRIBUTE: $this->adoption['attribute']++; break;
                case T_DOLLAR_OPEN_CURLY_BRACES: $this->hit('string_interp_brace', $this->ln($i)); break;
                case T_FUNCTION: case T_FN: $this->checkFunction($i); break;
                case T_LIST:
                    if (!in_array($this->id($i - 1), [T_OBJECT_OPERATOR, T_NULLSAFE_OBJECT_OPERATOR, T_DOUBLE_COLON, T_FUNCTION], true)) $this->hit('array_destructuring', $this->ln($i));
                    break;
                case T_ISSET: $this->checkIssetTernary($i); break;
                case T_SWITCH: $this->checkSwitch($i); break;
                case T_CLASS: $this->checkClass($i); break;
                case T_VARIABLE: $this->checkNullsafe($i); break;
                case T_PUBLIC: case T_PROTECTED: case T_PRIVATE: case T_VAR: $this->checkPropertyDecl($i); break;
                case T_RETURN: $this->checkArrayReturn($i); break;
                case T_STRING: $this->checkString($i); break;
                case 0:
                    if ($this->tx($i) === '!' && $this->isWord($i + 1, 'is_null')) $this->checkIsNullTernary($i);
                    break;
            }
        }
    }

    private function checkString(int $i): void
    {
        $w = strtolower($this->tx($i));
        if ($w === 'filter_sanitize_string' && $this->id($i - 1) !== T_OBJECT_OPERATOR && $this->id($i - 1) !== T_DOUBLE_COLON) {
            $this->hit('filter_sanitize_string', $this->ln($i));
            return;
        }
        if (!$this->isCall($i)) {
            if ($w === 'is_null' && $this->is($i + 1, '(')) $this->checkIsNullTernary($i);
            return;
        }
        $close = $this->closeOf($i + 1);
        if ($close < 0) return;
        if ($w === 'utf8_encode' || $w === 'utf8_decode') { $this->hit('utf8_encode', $this->ln($i)); return; }
        if (in_array($w, self::REMOVED_FUNCS, true)) { $this->hit('removed_functions', $this->ln($i)); return; }
        if (in_array($w, ['strpos', 'stripos', 'mb_strpos', 'mb_stripos'], true)) {
            $this->checkStrpos($i, $close);
            return;
        }
        if ($w === 'substr' || $w === 'mb_substr') { $this->checkSubstr($i, $close); return; }
        if (in_array($w, self::CALLBACK_FUNCS, true)) { $this->checkCallbacks($i, $close); return; }
        if ($w === 'is_null') $this->checkIsNullTernary($i);
    }

    private function isCmp(int $i, bool $identicalOnly = false): bool
    {
        $id = $this->id($i);
        return $id === T_IS_IDENTICAL || $id === T_IS_NOT_IDENTICAL || (!$identicalOnly && ($id === T_IS_EQUAL || $id === T_IS_NOT_EQUAL));
    }

    private function checkStrpos(int $i, int $close): void
    {
        // strpos(...) !== false  |  strpos(...) === 0
        if ($this->isCmp($close + 1)) {
            if ($this->isWord($close + 2, 'false')) { $this->hit('str_contains', $this->ln($i)); return; }
            if ($this->id($close + 2) === T_LNUMBER && $this->tx($close + 2) === '0') { $this->hit('str_starts_with', $this->ln($i)); return; }
        }
        // false !== strpos(...)  |  0 === strpos(...)
        if ($this->isCmp($i - 1)) {
            if ($this->isWord($i - 2, 'false')) { $this->hit('str_contains', $this->ln($i)); return; }
            if ($this->id($i - 2) === T_LNUMBER && $this->tx($i - 2) === '0') { $this->hit('str_starts_with', $this->ln($i)); return; }
        }
    }

    private function checkSubstr(int $i, int $close): void
    {
        $args = $this->args($i + 1, $close);
        if (count($args) !== 3) return;
        [$a, $b] = $args[1];
        if (!($a === $b && $this->id($a) === T_LNUMBER && $this->tx($a) === '0')) return;
        if ($this->isCmp($close + 1) && $this->id($close + 2) === T_CONSTANT_ENCAPSED_STRING) { $this->hit('str_starts_with', $this->ln($i)); return; }
        if ($this->isCmp($i - 1) && $this->id($i - 2) === T_CONSTANT_ENCAPSED_STRING) $this->hit('str_starts_with', $this->ln($i));
    }

    private function checkCallbacks(int $i, int $close): void
    {
        foreach ($this->args($i + 1, $close) as [$a, $b]) {
            if ($a === $b && $this->id($a) === T_CONSTANT_ENCAPSED_STRING
                && preg_match('/^[\'"]\\\\?[A-Za-z_][\w\\\\]*(::[A-Za-z_]\w*)?[\'"]$/', $this->tx($a))) {
                $this->hit('first_class_callable', $this->ln($a));
                return;
            }
            if ($this->is($a, '[') && $this->closeOf($a) === $b) {
                $parts = $this->args($a, $b);
                if (count($parts) === 2 && $parts[1][0] === $parts[1][1] && $this->id($parts[1][0]) === T_CONSTANT_ENCAPSED_STRING
                    && preg_match('/^[\'"][A-Za-z_]\w*[\'"]$/', $this->tx($parts[1][0]))) {
                    $this->hit('first_class_callable', $this->ln($a));
                    return;
                }
            }
        }
    }

    private function checkIssetTernary(int $i): void
    {
        if (!$this->is($i + 1, '(')) return;
        $close = $this->closeOf($i + 1);
        if ($close < 0 || count($this->args($i + 1, $close)) !== 1 || !$this->is($close + 1, '?')) return;
        if ($this->id($i - 1) === 0 && $this->tx($i - 1) === '!') return;
        $x = $this->text($i + 2, $close - 1);
        $colon = $this->findDepth0($close + 2, $this->exprEnd($close + 2), ':');
        if ($colon < 0) return;
        if ($this->text($close + 2, $colon - 1) === $x) $this->hit('null_coalesce', $this->ln($i));
    }

    /** $x ? $x->y : null  |  $x !== null ? $x->y : null  (x nesne zinciri: içinde -> olmalı) */
    private function checkNullsafe(int $i): void
    {
        if ($this->id($i - 1) === T_OBJECT_OPERATOR || $this->id($i - 1) === T_NULLSAFE_OBJECT_OPERATOR || $this->id($i - 1) === T_DOUBLE_COLON) return;
        $e = $this->chainEnd($i);
        if ($e < 0) return;
        $x = $this->text($i, $e);
        if (strpos($x, '->') === false) return; // çıplak $var: 0 / '' olabilir, truthiness ≠ null kontrolü
        $q = -1;
        if ($this->is($e + 1, '?')) $q = $e + 1;
        elseif ($this->id($e + 1) === T_IS_NOT_IDENTICAL && $this->isNullTok($e + 2) && $this->is($e + 3, '?')) $q = $e + 3;
        if ($q < 0) return;
        $this->nullsafeTail($q, $x, $this->ln($i));
    }

    /** !is_null($x) ? $x->y : null */
    private function checkIsNullTernary(int $i): void
    {
        $neg = $this->is($i, '!');
        $f = $neg ? $i + 1 : $i;
        if (!$neg) return; // is_null($x) ? null : $x->y biçimi ayrıca ele alınmıyor (nadir)
        if (!$this->isWord($f, 'is_null') || !$this->is($f + 1, '(')) return;
        $close = $this->closeOf($f + 1);
        if ($close < 0 || !$this->is($close + 1, '?')) return;
        $x = $this->text($f + 2, $close - 1);
        $this->nullsafeTail($close + 1, $x, $this->ln($i));
    }

    private function nullsafeTail(int $q, string $x, int $line): void
    {
        $end = $this->exprEnd($q + 1);
        $colon = $this->findDepth0($q + 1, $end, ':');
        if ($colon < 0) return;
        $then = $this->text($q + 1, $colon - 1);
        if (strpos($then, $x . ' ->') !== 0 && strpos($then, $x . ' ?->') !== 0) return;
        if ($this->isNullTok($colon + 1) && $colon + 1 === $end) $this->hit('nullsafe', $line);
    }

    private function checkFunction(int $i): void
    {
        $p = $i + 1;
        if ($this->is($p, '&')) $p++;
        $isCtor = false;
        if ($this->id($p) === T_STRING) { $isCtor = strcasecmp($this->tx($p), '__construct') === 0; $p++; }
        if (!$this->is($p, '(')) return;
        $close = $this->closeOf($p);
        if ($close < 0) return;
        $params = [];
        foreach ($this->args($p, $close) as [$a, $b]) {
            $v = -1;
            for ($j = $a; $j <= $b; $j++) if ($this->id($j) === T_VARIABLE) { $v = $j; break; }
            if ($v < 0) continue;
            $promoted = false;
            $typeToks = [];
            for ($j = $a; $j < $v; $j++) {
                $id = $this->id($j);
                if (in_array($id, [T_PUBLIC, T_PROTECTED, T_PRIVATE, T_READONLY], true)) { $promoted = true; continue; }
                if ($this->is($j, '&') || $id === T_ELLIPSIS) continue;
                $typeToks[] = $j;
            }
            $params[] = ['var' => $this->tx($v), 'promoted' => $promoted, 'line' => $this->ln($v)];
            // implicit nullable: tip var, ? ile başlamıyor, null/mixed içermiyor, = null
            if ($typeToks && $this->is($v + 1, '=') && $this->isNullTok($v + 2) && $v + 2 === $b) {
                $first = $typeToks[0];
                $hasNull = false;
                foreach ($typeToks as $j) if ($this->isNullTok($j) || $this->isWord($j, 'mixed')) $hasNull = true;
                if (!$this->is($first, '?') && !$hasNull) $this->hit('implicit_nullable', $this->ln($v));
            }
        }
        if (!$isCtor) return;
        // ctor promotion: gövde yalnız $this->p = $p; atamaları
        $j = $close + 1;
        while ($j < $this->n && !$this->is($j, '{') && !$this->is($j, ';')) $j++;
        if (!$this->is($j, '{')) return;
        $bodyEnd = $this->closeOf($j);
        if ($bodyEnd < 0 || $bodyEnd === $j + 1) return;
        $names = [];
        foreach ($params as $pr) if (!$pr['promoted']) $names[$pr['var']] = true;
        $k = $j + 1;
        $assigns = 0;
        while ($k < $bodyEnd) {
            if ($this->id($k) === T_VARIABLE && $this->tx($k) === '$this' && $this->id($k + 1) === T_OBJECT_OPERATOR
                && $this->id($k + 2) === T_STRING && $this->is($k + 3, '=') && $this->id($k + 4) === T_VARIABLE
                && isset($names[$this->tx($k + 4)]) && $this->is($k + 5, ';')) {
                $assigns++;
                $k += 6;
                continue;
            }
            return; // başka bir ifade var
        }
        if ($assigns > 0) $this->hit('ctor_promotion', $this->ln($i));
    }

    private function checkSwitch(int $i): void
    {
        if (!$this->is($i + 1, '(')) return;
        $pc = $this->closeOf($i + 1);
        if ($pc < 0 || !$this->is($pc + 1, '{')) return;
        $end = $this->closeOf($pc + 1);
        if ($end < 0) return;
        $cases = 0;
        $kind = null;   // 'return' | 'assign'
        $var = null;
        $j = $pc + 2;
        while ($j < $end) {
            if ($this->id($j) !== T_CASE && $this->id($j) !== T_DEFAULT) return;
            $cases += $this->id($j) === T_CASE ? 1 : 0;
            // etiket sonu: derinlik-0 ':' ya da ';'
            $lbl = $j + 1;
            $d = 0;
            while ($lbl < $end) {
                if ($this->id($lbl) === 0) {
                    $c = $this->tx($lbl);
                    if ($c === '(' || $c === '[') $d++;
                    elseif ($c === ')' || $c === ']') $d--;
                    elseif (($c === ':' || $c === ';') && $d === 0) break;
                }
                $lbl++;
            }
            $s = $lbl + 1;
            if ($this->id($s) === T_CASE || $this->id($s) === T_DEFAULT || $s >= $end) { $j = $s; continue; } // boş (yığılmış) case
            // gövde: return expr ;   |   $v = expr ; break ;
            if ($this->id($s) === T_RETURN) {
                $semi = $this->findDepth0($s + 1, $end - 1, ';');
                if ($semi < 0 || ($kind !== null && $kind !== 'return')) return;
                for ($q = $s + 1; $q < $semi; $q++) if ($this->id($q) === T_SWITCH || $this->id($q) === T_MATCH) return;
                $kind = 'return';
                $j = $semi + 1;
                continue;
            }
            if ($this->id($s) === T_VARIABLE && $this->is($s + 1, '=')) {
                $semi = $this->findDepth0($s + 2, $end - 1, ';');
                if ($semi < 0 || ($kind !== null && $kind !== 'assign') || ($var !== null && $var !== $this->tx($s))) return;
                $kind = 'assign';
                $var = $this->tx($s);
                $j = $semi + 1;
                if ($this->id($j) === T_BREAK && $this->is($j + 1, ';')) $j += 2;
                elseif ($this->id($j) !== T_DEFAULT && $this->id($j) !== T_CASE && $j < $end) return;
                continue;
            }
            return;
        }
        if ($cases >= 2 && $kind !== null) $this->hit('match', $this->ln($i));
    }

    private function checkPropertyDecl(int $i): void
    {
        $j = $i + 1;
        while (in_array($this->id($j), [T_STATIC, T_READONLY, T_VAR, T_PUBLIC, T_PROTECTED, T_PRIVATE], true)) $j++;
        if ($this->id($j) === T_FUNCTION || $this->id($j) === T_CONST || $this->id($j) === T_CASE) return;
        if ($this->id($j) === T_VARIABLE) {
            // ctor promotion parametresi (tipsiz) de tipsiz property'dir
            $this->adoption['untyped_props']++;
            $this->hit('typed_properties', $this->ln($j));
            return;
        }
        // tip token(lar)ı sonra değişken
        $k = $j;
        while ($k < $this->n && $this->id($k) !== T_VARIABLE && !$this->is($k, ';') && !$this->is($k, '{') && !$this->is($k, '(')) $k++;
        if ($this->id($k) === T_VARIABLE && $k > $j) $this->adoption['typed_props']++;
    }

    private function checkArrayReturn(int $i): void
    {
        $open = $i + 1;
        if ($this->id($open) === T_ARRAY && $this->is($open + 1, '(')) $open++;
        if (!$this->is($open, '[') && !$this->is($open, '(')) return;
        $close = $this->closeOf($open);
        if ($close < 0) return;
        $keys = 0;
        foreach ($this->args($open, $close) as [$a, $b]) {
            if ($this->id($a) === T_CONSTANT_ENCAPSED_STRING && $this->id($a + 1) === T_DOUBLE_ARROW) $keys++;
        }
        if ($keys >= 4) $this->hit('dto_from_array', $this->ln($i));
    }

    private function checkClass(int $i): void
    {
        if ($this->id($i - 1) === T_DOUBLE_COLON || $this->id($i - 1) === T_NEW) return;
        if ($this->id($i + 1) !== T_STRING) return;
        $name = $this->tx($i + 1);
        $j = $i + 2;
        while ($j < $this->n && !$this->is($j, '{')) $j++;
        $end = $this->closeOf($j);
        if ($end < 0) return;
        $consts = [];
        $methods = [];
        $props = [];      // name => ['line'=>, 'vis'=>]
        $k = $j + 1;
        while ($k < $end) {
            $id = $this->id($k);
            if ($id === T_CONST) {
                $m = $k + 1;
                if ($this->id($m) !== T_STRING) { $k++; continue; }
                while ($m < $end && !$this->is($m, '=')) $m++;
                $v = $m + 1;
                $neg = $this->is($v, '-');
                if ($neg) $v++;
                $kind = $this->id($v) === T_CONSTANT_ENCAPSED_STRING && $this->is($v + 1, ';') ? 'string'
                    : ($this->id($v) === T_LNUMBER && $this->is($v + 1, ';') ? 'int' : 'other');
                $consts[] = $kind;
                $k = $v + 1;
                continue;
            }
            if ($id === T_FUNCTION && $this->id($k + 1) === T_STRING) {
                $fname = $this->tx($k + 1);
                $pc = $this->closeOf($k + 2);
                $body = -1; $bodyEnd = -1;
                $q = $pc + 1;
                while ($q < $end && !$this->is($q, '{') && !$this->is($q, ';')) $q++;
                if ($this->is($q, '{')) { $body = $q; $bodyEnd = $this->closeOf($q); }
                $methods[$fname] = ['params' => $pc > 0 ? count($this->args($k + 2, $pc)) : 0, 'body' => $body, 'end' => $bodyEnd];
                $k = $bodyEnd > 0 ? $bodyEnd + 1 : $q + 1;
                continue;
            }
            if (in_array($id, [T_PUBLIC, T_PROTECTED, T_PRIVATE, T_VAR], true)) {
                $m = $k + 1;
                while (in_array($this->id($m), [T_STATIC, T_READONLY], true)) $m++;
                if ($this->id($m) !== T_FUNCTION && $this->id($m) !== T_CONST) {
                    $v = $m;
                    while ($v < $end && $this->id($v) !== T_VARIABLE && !$this->is($v, ';')) $v++;
                    if ($this->id($v) === T_VARIABLE) $props[substr($this->tx($v), 1)] = ['line' => $this->ln($v), 'vis' => $id];
                }
            }
            $k++;
        }
        // enum adayı
        $n = count($consts);
        $uniform = $n >= 3 && count(array_unique($consts)) === 1 && $consts[0] !== 'other';
        if (($uniform && !$methods && !$props) || ($uniform && preg_match(self::ENUMISH_NAME, $name))) {
            $this->hit('enum', $this->ln($i));
        }
        // property hooks / asimetrik görünürlük
        foreach ($props as $pname => $p) {
            if ($p['vis'] === T_PUBLIC || $p['vis'] === T_VAR) continue;
            $uc = ucfirst($pname);
            $getter = null;
            foreach (['get' . $uc, 'is' . $uc, 'has' . $uc] as $g) if (isset($methods[$g]) && $methods[$g]['params'] === 0 && $this->isTrivialGetter($methods[$g], $pname)) { $getter = $g; break; }
            if ($getter === null) continue;
            $setter = $methods['set' . $uc] ?? null;
            if ($setter && $setter['params'] === 1 && $this->isSmallSetter($setter, $pname)) {
                $this->hit('property_hooks', $p['line']);
            } elseif (!$setter) {
                $this->hit('asymmetric_visibility', $p['line']);
            }
        }
    }

    private function isTrivialGetter(array $m, string $prop): bool
    {
        if ($m['body'] < 0) return false;
        $s = $m['body'] + 1;
        return $this->id($s) === T_RETURN && $this->tx($s + 1) === '$this' && $this->id($s + 2) === T_OBJECT_OPERATOR
            && $this->tx($s + 3) === $prop && $this->is($s + 4, ';') && $s + 5 === $m['end'];
    }

    private function isSmallSetter(array $m, string $prop): bool
    {
        if ($m['body'] < 0) return false;
        $stmts = 0;
        $assigns = false;
        for ($j = $m['body'] + 1; $j < $m['end']; $j++) {
            if ($this->is($j, ';')) $stmts++;
            if ($this->tx($j) === '$this' && $this->id($j + 1) === T_OBJECT_OPERATOR && $this->tx($j + 2) === $prop && $this->is($j + 3, '=')) $assigns = true;
        }
        return $assigns && $stmts <= 3;
    }
}

// ── dosya gezgini ──
$sc = new Scanner();
$files = 0;
$errors = [];
$it = new RecursiveIteratorIterator(new RecursiveCallbackFilterIterator(
    new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS),
    static function (SplFileInfo $f, string $key, RecursiveDirectoryIterator $iter) use ($root, $exclude): bool {
        $rel = ltrim(substr($f->getPathname(), strlen($root)), '/');
        if ($f->isDir()) {
            if ($f->getFilename() === '.git') return false;
            foreach ($exclude as $ex) {
                $ex = trim($ex, '/');
                if ($ex === '') continue;
                if ($rel === $ex || str_starts_with($rel, $ex . '/')) return false;
                if (strpos($ex, '/') === false && $f->getFilename() === $ex) return false;
            }
            return true;
        }
        return str_ends_with($f->getFilename(), '.php');
    }
));
foreach ($it as $f) {
    /** @var SplFileInfo $f */
    $rel = ltrim(substr($f->getPathname(), strlen($root)), '/');
    $files++;
    try {
        $sc->scanFile($f->getPathname(), $rel);
    } catch (Throwable $e) {
        $errors[] = $rel . ': ' . $e->getMessage();
    }
}
echo json_encode(['files' => $files, 'hits' => array_values($sc->hits), 'adoption' => $sc->adoption, 'errors' => $errors],
    JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
