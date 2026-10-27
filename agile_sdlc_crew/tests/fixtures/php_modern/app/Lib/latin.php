<?php
namespace App\Lib;
class Latin
{
    public function enc($raw)
    {
        return utf8_encode($raw); // Ýþÿ @expect utf8_encode
    }
}
