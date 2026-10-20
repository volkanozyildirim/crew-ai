<?php
namespace App\Model;

class Legacy
{
    private $client; // @expect typed_properties
    private $logger; // @expect typed_properties

    public function __construct(HttpClient $client, Logger $logger) // @expect ctor_promotion
    {
        $this->client = $client;
        $this->logger = $logger;
    }

    public function find(string $sku = null, array $opts = null) // @expect implicit_nullable
    {
        return $sku;
    }

    public function ok(?string $sku = null, string|null $x = null, mixed $m = null): void
    {
    }

    public function encode($raw)
    {
        $a = utf8_encode($raw); // @expect utf8_encode
        $b = "Sipariş ${raw} hazır"; // @expect string_interp_brace
        $c = filter_var($raw, FILTER_SANITIZE_STRING); // @expect filter_sanitize_string
        while (list($k, $v) = each($raw)) { // @expect removed_functions @expect array_destructuring
            $b .= $k;
        }
        return [$a, $b, $c];
    }

    public function strings(string $uri, string $sku, array $addr, $order)
    {
        if (strpos($uri, '/api/') !== false) { // @expect str_contains
            $uri = '';
        }
        if (false === stripos($uri, 'x')) { // @expect str_contains
            $uri = '';
        }
        if (substr($sku, 0, 4) === 'FLO-') { // @expect str_starts_with
            $sku = '';
        }
        if (strpos($sku, 'A') === 0) { // @expect str_starts_with
            $sku = '';
        }
        $city = isset($addr['city']) ? $addr['city'] : null; // @expect null_coalesce
        $other = isset($addr['x']) ? $addr['y'] : null;
        $name = $order->getCustomer() ? $order->getCustomer()->getName() : null; // @expect nullsafe
        $mail = $order->getCustomer() !== null ? $order->getCustomer()->getMail() : null; // @expect nullsafe
        $tel = !is_null($order->getCustomer()) ? $order->getCustomer()->getTel() : null; // @expect nullsafe
        $flag = $order ? $order->flag : null;
        return [$city, $other, $name, $mail, $tel, $flag];
    }

    public function label(string $status): string
    {
        switch ($status) { // @expect match
            case 'paid':
                return 'Ödendi';
            case 'refund':
            case 'returned':
                return 'İade';
            default:
                return 'Bilinmiyor';
        }
    }

    public function label2(string $status): string
    {
        switch ($status) { // @expect match
            case 'paid': $label = 'Ödendi'; break;
            case 'refund': $label = 'İade'; break;
            default: $label = 'Bilinmiyor';
        }
        return $label;
    }

    public function notMatch(string $status): string
    {
        switch ($status) {
            case 'paid':
                $this->logger->info('x');
                return 'a';
            default:
                return 'b';
        }
    }

    public function callables(array $rows): array
    {
        $a = array_map([$this, 'normalize'], $rows); // @expect first_class_callable
        $b = array_map('strtolower', $rows); // @expect first_class_callable
        usort($rows, [self::class, 'cmp']); // @expect first_class_callable
        $c = array_filter($rows, fn($r) => $r !== null);
        return [$a, $b, $c];
    }

    private function normalize($r) { return $r; }
    private static function cmp($a, $b) { return $a <=> $b; }
}
