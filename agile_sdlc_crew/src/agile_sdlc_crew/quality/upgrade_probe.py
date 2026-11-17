"""Python yükseltme sürücüsü: dosyaları `compile()` eder, depo kodunu ÇALIŞTIRMAZ.

`<yorumlayıcı> -I <bu dosya> <kök> <göreli dosyalar…>` ile, cwd depo DIŞINDA çağrılır. Eski ve yeni
yorumlayıcıda aynı çalışmalı: yalnız standart kütüphane, sürüme özgü sözdizimi yok. Her bulgu
için stdout'a bir JSON satırı yazar.
"""
import json
import os
import sys
import warnings


def _emit(rule, rel, line, text, message):
    sys.stdout.write(json.dumps({"rule": rule, "file": rel, "line": int(line or 0),
                                 "text": (text or "")[:200], "message": (message or "")[:300]}) + "\n")


def _line(src_lines, n):
    try:
        return src_lines[n - 1].strip() if n and 0 < n <= len(src_lines) else ""
    except Exception:
        return ""


def main(argv):
    if len(argv) < 2:
        return 2
    root = os.path.realpath(argv[0])
    for rel in argv[1:]:
        path = os.path.realpath(os.path.join(root, rel))
        if not path.startswith(root + os.sep):  # kök dışına çıkan (../, sembolik bağ) dosya okunmaz
            continue
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except (IOError, OSError):
            continue
        lines = raw.decode("utf-8", "replace").splitlines()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                compile(raw, path, "exec")
            except SyntaxError as e:
                _emit("probe:error", rel, e.lineno, _line(lines, e.lineno or 0), "SyntaxError: %s" % (e.msg,))
            except (ValueError, MemoryError, RecursionError) as e:
                _emit("probe:error", rel, 0, "", "%s: %s" % (type(e).__name__, e))
        for w in caught:
            if issubclass(w.category, (SyntaxWarning, DeprecationWarning)):
                n = w.lineno or 0
                _emit("probe:deprecation", rel, n, _line(lines, n), "%s: %s" % (w.category.__name__, w.message))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
