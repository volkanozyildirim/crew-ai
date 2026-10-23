"""Sprint raporu — FLO şablonu dışındaki alternatif tasarımlar.

Klasik tasarım (`sprint_report.build_pptx`) FLO şablonunu klonlayıp metin
kutularını doldurur. Buradaki tasarımlar sunumu **sıfırdan** çizer: yumuşak
köşeli kartlar, renkli/degrade zeminler, büyük sayılar ve halkalar; proje başına
madde listesi yerine **tek cümlelik manşet**. Veri aynıdır (GroupSummary +
Azure burndown/velocity), yalnız sunuş değişir.

Her tasarım bir `Design` (renk/yazı tipi tokenları) — çizim kodu tektir. Yeni
tasarım = `DESIGNS`'a bir kayıt. Renk çiftleri (tamamlanan/devam eden) dataviz
doğrulayıcısından geçti; turuncu/pembe tonların zemin kontrastı düşük olduğundan
değerler her grafikte etiketle yazılır.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from agile_sdlc_crew import sprint_report as sr

log = logging.getLogger("agile_sdlc_crew.sprint_report")

W_IN, H_IN = 13.333, 7.5

RGB = tuple[int, int, int]


def _hex(h: str) -> RGB:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


@dataclass(frozen=True)
class Design:
    key: str
    label: str            # dropdown'da görünen Türkçe ad
    description: str      # dropdown altındaki tek satır
    bg: tuple[str, ...]   # 1 renk = düz zemin, 2 renk = degrade
    bg_angle: int
    cover_bg: tuple[str, ...]
    card: str
    card_line: str        # "" = kenarsız
    text: str
    muted: str
    accent: str           # başlık vurgusu / kicker
    done: str             # tamamlanan
    open: str             # devam eden
    track: str            # halka/çubuk izi
    neutral: str          # ikincil seri (velocity 'Planlanan') — zeminde görünür nötr
    blobs: tuple[tuple[str, int], ...]   # dekoratif yumuşak daireler: (renk, opaklık %)
    tiles: tuple[str, ...]  # alan kartı zeminleri (domain sırasına sabit)
    icon: str             # ikon çizgi rengi
    cover_text: str = ""  # kapak metin rengi ("" = text)
    font: str = "Aptos"
    font_head: str = "Aptos Display"
    shadow: int = 10      # kart gölgesi opaklığı (%), 0 = yok
    logo_pill: bool = False  # koyu zeminde logo beyaz kapsül üstünde

    def swatch(self) -> list[str]:
        return [self.bg[0], self.accent, self.done, self.open]


DESIGNS: dict[str, Design] = {
    d.key: d
    for d in (
        Design(
            key="pastel", label="Yumuşak Pastel",
            description="Krem zemin, pastel kartlar, yuvarlak hatlar — sakin ve ferah",
            bg=("#FBF7F2",), bg_angle=0, cover_bg=("#FFE9DC", "#E8E1FF"),
            card="#FFFFFF", card_line="", text="#2B2A33", muted="#77758A",
            accent="#E8784A", done="#3E9C7E", open="#F2994A", track="#EFE9E2",
            neutral="#CFC8BF",
            blobs=(("#FFD8C2", 55), ("#CDEFE3", 60), ("#E3DBFF", 55)),
            tiles=("#FFE7D9", "#DDF3EA", "#E8E2FF", "#FFF1C9", "#DCEBFF",
                   "#FFE0EC", "#E2F4D8", "#F0E6DA", "#ECECF1"),
            icon="#2B2A33", shadow=9,
        ),
        Design(
            key="gece", label="Gece Mavisi",
            description="Koyu lacivert degrade, canlı camgöbeği ve pembe vurgular",
            bg=("#0F172A", "#1E1B4B"), bg_angle=135, cover_bg=("#111827", "#312E81"),
            card="#1A2238", card_line="#2C3654", text="#F1F5F9", muted="#94A3B8",
            accent="#F9A8D4", done="#0EA5C6", open="#E8508F", track="#2C3654",
            neutral="#64748B",
            blobs=(("#7C3AED", 28), ("#06B6D4", 22), ("#EC4899", 20)),
            tiles=("#243052", "#22364A", "#2E2A55", "#33304A", "#1F3A4A",
                   "#3A2645", "#24402F", "#3A3226", "#2A2F3F"),
            icon="#E2E8F0", shadow=0, logo_pill=True,
        ),
        Design(
            key="canli", label="Canlı Degrade",
            description="Turuncu→mor degrade kapak, beyaz kartlar, enerjik renkler",
            bg=("#F7F6FB",), bg_angle=0, cover_bg=("#FF7A45", "#7C5CFF"),
            card="#FFFFFF", card_line="", text="#1F2937", muted="#6B7280",
            accent="#7C5CFF", done="#7C5CFF", open="#FF8A3D", track="#ECEAF5",
            neutral="#CFCBE0",
            blobs=(("#FF8A3D", 22), ("#7C5CFF", 20), ("#FF5C8A", 18)),
            tiles=("#FFE8DA", "#ECE6FF", "#FFE3EC", "#E0F2FE", "#FEF3C7",
                   "#DCFCE7", "#F3E8FF", "#FFEDD5", "#F1F5F9"),
            icon="#1F2937", cover_text="#FFFFFF", shadow=12, logo_pill=True,
        ),
    )
}

CLASSIC = {
    "key": "klasik", "label": "Klasik (FLO şablonu)",
    "description": "Kurumsal FLO şablonu — başlık + madde listeleri",
    "swatch": ["#FFFFFF", "#E97132", "#2F6FD0", "#0E2841"],
}


def design_options() -> list[dict]:
    """Dashboard dropdown'u için tasarım listesi (klasik ilk sırada)."""
    return [CLASSIC] + [
        {"key": d.key, "label": d.label, "description": d.description, "swatch": d.swatch()}
        for d in DESIGNS.values()
    ]


def resolve(key: str) -> str:
    """Bilinmeyen/boş tasarım -> env varsayılanı -> klasik."""
    k = (key or "").strip().lower()
    if k == "klasik" or k in DESIGNS:
        return k
    env = os.environ.get("CREW_SPRINT_REPORT_DESIGN", "").strip().lower()
    return env if env in DESIGNS else "klasik"


# ------------------------------------------------------------------ primitives


def _caps(s: str) -> str:
    """Kicker/etiket büyük harfi. Türkçe 'i'->'İ' dönüşümü YAPILMAZ: takım adları çoğu
    zaman İngilizce ('Logistic' -> 'LOGİSTİC' olurdu)."""
    return (s or "").replace("ı", "I").upper()


def _rgb(h: str):
    from pptx.dml.color import RGBColor

    return RGBColor(*_hex(h))


def _alpha(shape, pct: int):
    """Düz dolgulu şeklin opaklığını ayarla (python-pptx'te API yok -> XML)."""
    from pptx.oxml.ns import qn

    sf = shape._element.spPr.find(qn("a:solidFill"))
    clr = sf.find(qn("a:srgbClr")) if sf is not None else None
    if clr is None:
        return
    for old in clr.findall(qn("a:alpha")):
        clr.remove(old)
    a = clr.makeelement(qn("a:alpha"), {"val": str(int(pct * 1000))})
    clr.append(a)


def _shadow(shape, pct: int):
    """Yumuşak, geniş yayılımlı alt gölge (kart derinliği)."""
    from lxml import etree
    from pptx.oxml.ns import qn

    if pct <= 0:
        return
    sp = shape._element.spPr
    for old in sp.findall(qn("a:effectLst")):
        sp.remove(old)
    xml = (
        '<a:effectLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:outerShdw blurRad="228600" dist="38100" dir="5400000" algn="t" rotWithShape="0">'
        f'<a:srgbClr val="1E1B3A"><a:alpha val="{pct * 1000}"/></a:srgbClr>'
        "</a:outerShdw></a:effectLst>"
    )
    sp.append(etree.fromstring(xml))


def _no_shadow(shape):
    try:
        shape.shadow.inherit = False
    except Exception:
        pass


def _shape(slide, kind, x, y, w, h, fill: str, *, line: str = "", radius: float | None = None,
           alpha: int | None = None, shadow: int = 0):
    from pptx.util import Pt

    sh = slide.shapes.add_shape(kind, x, y, w, h)
    if radius is not None:
        try:
            sh.adjustments[0] = radius
        except Exception:
            pass
    sh.fill.solid()
    sh.fill.fore_color.rgb = _rgb(fill)
    if line:
        sh.line.color.rgb = _rgb(line)
        sh.line.width = Pt(1)
    else:
        sh.line.fill.background()
    _no_shadow(sh)
    if alpha is not None:
        _alpha(sh, alpha)
    if shadow:
        _shadow(sh, shadow)
    return sh


def _round(slide, d: Design, x, y, w, h, fill: str | None = None, radius=0.12, shadow=True):
    from pptx.enum.shapes import MSO_SHAPE

    return _shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h, fill or d.card,
                  line=d.card_line if fill is None else "", radius=radius,
                  shadow=d.shadow if shadow else 0)


def _oval(slide, x, y, w, h, fill: str, alpha: int | None = None):
    from pptx.enum.shapes import MSO_SHAPE

    return _shape(slide, MSO_SHAPE.OVAL, x, y, w, h, fill, alpha=alpha)


def _text(slide, d: Design, x, y, w, h, text: str, size: float, *, color: str | None = None,
          bold=False, head=False, align=None, anchor=None, spacing: int = 0, wrap=True):
    from pptx.util import Pt

    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = wrap
    for side in ("margin_left", "margin_right", "margin_top", "margin_bottom"):
        setattr(tf, side, 0)
    if anchor is not None:
        tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    if align is not None:
        p.alignment = align
    r = p.add_run()
    r.text = text
    f = r.font
    f.size = Pt(size)
    f.bold = bold
    f.name = d.font_head if head else d.font
    f.color.rgb = _rgb(color or d.text)
    if spacing:
        f._rPr.set("spc", str(spacing))
    return tb


def _icon(domain: str, color: str) -> str | None:
    """Domain ikonunu tasarımın çizgi rengine boya (alfa korunur), önbellekle."""
    base = sr._domain_icon_png(domain)
    if not base:
        return None
    out = os.path.join(sr.icon_dir(), f"{domain}_{color.lstrip('#').lower()}.png")
    if os.path.exists(out):
        return out
    try:
        from PIL import Image

        im = Image.open(base).convert("RGBA")
        solid = Image.new("RGBA", im.size, _hex(color) + (255,))
        solid.putalpha(im.getchannel("A"))
        solid.save(out)
        return out
    except Exception as e:  # pragma: no cover - PIL yoksa orijinal ikon
        log.warning(f"  İkon boyanamadı ({domain}): {e}")
        return base


def _logo_png() -> str | None:
    """FLO logosunu klasik şablondan bir kez çıkar (marka sürekliliği)."""
    out = os.path.join(sr.icon_dir(), "flo_logo.png")
    if os.path.exists(out):
        return out
    tpl = sr.template_path()
    if not os.path.exists(tpl):
        return None
    try:
        from pptx import Presentation

        for slide in Presentation(tpl).slides:
            for sh in slide.shapes:
                if sh.shape_type == 13 and "logo" in (sh.name or "").lower():
                    with open(out, "wb") as fh:
                        fh.write(sh.image.blob)
                    return out
    except Exception as e:
        log.warning(f"  Logo şablondan alınamadı: {e}")
    return None


def _tile_color(d: Design, domain: str) -> str:
    # renk varlığı izler, sırayı değil: domain'in DOMAIN_META'daki sabit sırası
    keys = list(sr.DOMAIN_META)
    i = keys.index(domain) if domain in keys else len(keys) - 1
    return d.tiles[i % len(d.tiles)]


# ------------------------------------------------------------------ slide chrome


class _Deck:
    def __init__(self, d: Design, kicker: str):
        from pptx import Presentation
        from pptx.util import Inches

        self.d = d
        self.kicker = kicker
        self.prs = Presentation()
        self.prs.slide_width = Inches(W_IN)
        self.prs.slide_height = Inches(H_IN)
        self.layout = self.prs.slide_layouts[6]  # boş
        self.logo = _logo_png()
        self.page = 0

    def _background(self, slide, colors: tuple[str, ...], angle: int):
        fill = slide.background.fill
        if len(colors) >= 2:
            fill.gradient()
            fill.gradient_angle = angle
            stops = fill.gradient_stops
            stops[0].color.rgb = _rgb(colors[0])
            stops[0].position = 0.0
            stops[1].color.rgb = _rgb(colors[-1])
            stops[1].position = 1.0
        else:
            fill.solid()
            fill.fore_color.rgb = _rgb(colors[0])

    def _blobs(self, slide, spots):
        from pptx.util import Inches

        for (color, alpha), (x, y, size) in zip(self.d.blobs, spots):
            _oval(slide, Inches(x), Inches(y), Inches(size), Inches(size), color, alpha)

    def _logo(self, slide, x, y, w):
        from pptx.util import Inches

        if not self.logo:
            return
        if self.d.logo_pill:  # koyu zeminde logo okunmaz: beyaz kapsül
            _round(slide, self.d, Inches(x - 0.14), Inches(y - 0.09), Inches(w + 0.28),
                   Inches(w / 2.98 + 0.18), fill="#FFFFFF", radius=0.5, shadow=False)
        slide.shapes.add_picture(self.logo, Inches(x), Inches(y), width=Inches(w))

    def content(self, title: str):
        """Standart içerik slaytı: zemin + köşe lekeleri + kicker + başlık + logo + sayfa no."""
        from pptx.enum.text import PP_ALIGN
        from pptx.util import Inches

        d = self.d
        s = self.prs.slides.add_slide(self.layout)
        self.page += 1
        self._background(s, d.bg, d.bg_angle)
        self._blobs(s, [(11.2, -1.6, 3.6), (-1.4, 5.6, 3.2), (12.4, 6.4, 1.8)])
        _text(s, d, Inches(0.7), Inches(0.42), Inches(9), Inches(0.3), self.kicker, 11,
              color=d.accent, bold=True, spacing=200)
        _text(s, d, Inches(0.7), Inches(0.72), Inches(10), Inches(0.6), title, 28,
              bold=True, head=True)
        self._logo(s, 11.45, 0.5, 1.15)
        _text(s, d, Inches(12.2), Inches(6.95), Inches(0.6), Inches(0.3), f"{self.page:02d}",
              11, color=d.muted, align=PP_ALIGN.RIGHT)
        return s

    def cover(self, title: str, subtitle: str, date_range: str):
        from pptx.util import Inches

        d = self.d
        s = self.prs.slides.add_slide(self.layout)
        self.page += 1
        self._background(s, d.cover_bg, 120)
        fg = d.cover_text or d.text
        sub = d.cover_text or d.accent
        self._blobs(s, [(8.6, -2.2, 6.4), (-2.0, 4.4, 5.0), (10.8, 5.0, 3.0)])
        _text(s, d, Inches(0.95), Inches(2.0), Inches(9), Inches(0.35), "SPRINT RAPORU", 14,
              color=sub, bold=True, spacing=300)
        _text(s, d, Inches(0.95), Inches(2.45), Inches(10.5), Inches(1.6), title, 44,
              color=fg, bold=True, head=True)
        _text(s, d, Inches(0.95), Inches(4.1), Inches(9), Inches(0.7), subtitle, 30,
              color=sub, bold=True, head=True)
        if date_range:
            _text(s, d, Inches(0.95), Inches(4.85), Inches(9), Inches(0.4), date_range, 16,
                  color=d.cover_text or d.muted)
        self._logo(s, 0.95, 0.75, 1.6)
        return s

    def closing(self):
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        from pptx.util import Inches

        d = self.d
        s = self.prs.slides.add_slide(self.layout)
        self.page += 1
        self._background(s, d.cover_bg, 120)
        self._blobs(s, [(-1.8, -1.8, 5.2), (9.6, 3.8, 5.6), (5.6, 6.2, 2.0)])
        _text(s, d, Inches(0), Inches(2.9), Inches(W_IN), Inches(1.0), "Teşekkürler", 54,
              color=d.cover_text or d.text, bold=True, head=True, align=PP_ALIGN.CENTER,
              anchor=MSO_ANCHOR.MIDDLE)
        self._logo(s, (W_IN - 1.6) / 2, 4.2, 1.6)
        return s


# ------------------------------------------------------------------ infographic pieces


def _ring(slide, d: Design, x, y, size, done: int, total: int, *, pct_size: float = 24):
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.oxml.ns import qn

    cd = CategoryChartData()
    cd.categories = ["Tamamlandı", "Devam Ediyor"]
    cd.add_series("İş", (done, max(total - done, 0)))
    ch = slide.shapes.add_chart(XL_CHART_TYPE.DOUGHNUT, x, y, size, size, cd).chart
    ch.has_title = False
    ch.has_legend = False
    _clear_chart_bg(ch)
    plot = ch.plots[0]
    hole = plot._element.find(qn("c:holeSize"))
    if hole is not None:
        hole.set("val", "74")
    for idx, color in ((0, d.done), (1, d.track)):
        pt = plot.series[0].points[idx]
        pt.format.fill.solid()
        pt.format.fill.fore_color.rgb = _rgb(color)
        pt.format.line.fill.background()
    pad = int(size * 0.2)
    _text(slide, d, x + pad, y + pad, size - 2 * pad, size - 2 * pad,
          f"%{sr._pct(done, total)}", pct_size, bold=True, head=True,
          align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)


def _pill(slide, d: Design, x, y, text: str, done: bool, size: float = 10):
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches

    color = d.done if done else d.open
    w = Inches(0.13 * len(text) * size / 10 + 0.36)
    h = Inches(0.3 * size / 10)
    pill = _round(slide, d, x, y, w, h, fill=color, radius=0.5, shadow=False)
    _alpha(pill, 18)
    _text(slide, d, x, y, w, h, text, size, color=color, bold=True,
          align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
    return w


def _bar(slide, d: Design, x, y, w, h, frac: float):
    _round(slide, d, x, y, w, h, fill=d.track, radius=0.5, shadow=False)
    frac = max(0.0, min(1.0, frac))
    if frac > 0:
        _round(slide, d, x, y, max(int(w * frac), h), h, fill=d.done, radius=0.5, shadow=False)


def _icon_disc(slide, d: Design, x, y, size, domain: str):
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN

    _oval(slide, x, y, size, size, _tile_color(d, domain))
    icon = _icon(domain, d.icon)
    inner = int(size * 0.58)
    if icon:
        slide.shapes.add_picture(icon, x + (size - inner) // 2, y + (size - inner) // 2,
                                 height=inner)
    else:
        _text(slide, d, x, y, size, size, sr.DOMAIN_META.get(domain, sr.DOMAIN_META["diger"])[1],
              18, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)


def _clear_chart_bg(ch):
    """Grafik ve çizim alanını şeffaf/kenarsız yap: kartın zemini görünsün."""
    from lxml import etree
    from pptx.oxml.ns import qn

    xml = ('<c:spPr xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
           'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
           '<a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>')
    cs = ch._chartSpace
    for parent, after in ((cs, cs.find(qn("c:chart"))),
                          (cs.find(qn("c:chart")).find(qn("c:plotArea")), None)):
        for old in parent.findall(qn("c:spPr")):
            parent.remove(old)
        el = etree.fromstring(xml)
        if after is not None:
            after.addnext(el)
        else:
            ext = parent.find(qn("c:extLst"))
            if ext is not None:
                ext.addprevious(el)
            else:
                parent.append(el)


def _style_chart_text(ch, d: Design, size: float = 11):
    from pptx.util import Pt

    _clear_chart_bg(ch)
    ch.font.size = Pt(size)
    ch.font.name = d.font
    ch.font.color.rgb = _rgb(d.muted)


def _status_chart(slide, d: Design, x, y, w, h, summaries, limit: int = 7):
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.util import Pt

    rows = [(sr._clip(s.title, 30), s.done, s.total - s.done) for s in summaries if s.total]
    if len(rows) > limit:
        head, tail = rows[: limit - 1], rows[limit - 1:]
        rows = head + [(f"Diğer ({len(tail)} proje)",
                        sum(r[1] for r in tail), sum(r[2] for r in tail))]
    if not rows:
        return
    cd = CategoryChartData()
    cd.categories = [r[0] for r in rows]
    cd.add_series("Tamamlandı", [r[1] for r in rows])
    cd.add_series("Devam Ediyor", [r[2] for r in rows])
    ch = slide.shapes.add_chart(XL_CHART_TYPE.BAR_STACKED, x, y, w, h, cd).chart
    _style_chart_text(ch, d, 10)
    ch.has_title = False
    ch.has_legend = True
    ch.legend.position = XL_LEGEND_POSITION.BOTTOM
    ch.legend.include_in_layout = False
    plot = ch.plots[0]
    plot.gap_width = 45
    plot.overlap = 100
    plot.has_data_labels = True
    dl = plot.data_labels
    dl.number_format = "0;;;"
    dl.number_format_is_linked = False
    dl.font.size = Pt(10)
    dl.font.bold = True
    dl.font.color.rgb = _rgb("#FFFFFF")
    for ser, color in zip(plot.series, (d.done, d.open)):
        ser.format.fill.solid()
        ser.format.fill.fore_color.rgb = _rgb(color)
        ser.format.line.color.rgb = _rgb(d.card)
        ser.format.line.width = Pt(2)
    cat = ch.category_axis
    cat.reverse_order = True
    cat.format.line.fill.background()
    cat.has_major_gridlines = False
    cat.tick_labels.font.color.rgb = _rgb(d.text)
    val = ch.value_axis
    val.visible = False
    val.has_major_gridlines = False


def _value_axis_soft(ch, d: Design):
    val = ch.value_axis
    val.has_major_gridlines = True
    val.major_gridlines.format.line.color.rgb = _rgb(d.track)
    val.format.line.fill.background()
    ch.category_axis.format.line.color.rgb = _rgb(d.track)


def _burndown(slide, d: Design, x, y, w, h, data: dict):
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.util import Pt

    cd = CategoryChartData()
    cd.categories = data["labels"]
    cd.add_series("Kalan", data["remaining"])
    cd.add_series("Toplam Kapsam", data["total"])
    cd.add_series("İdeal", data["ideal"])
    ch = slide.shapes.add_chart(XL_CHART_TYPE.AREA, x, y, w, h, cd).chart
    _style_chart_text(ch, d)
    ch.has_title = False
    ch.has_legend = True
    ch.legend.position = XL_LEGEND_POSITION.BOTTOM
    ch.legend.include_in_layout = False
    s0, s1, s2 = ch.series
    s0.format.fill.solid()
    s0.format.fill.fore_color.rgb = _rgb(d.done)
    s0.format.line.color.rgb = _rgb(d.done)
    s1.format.fill.background()
    s1.format.line.color.rgb = _rgb(d.open)
    s1.format.line.width = Pt(2.5)
    s2.format.fill.background()
    s2.format.line.color.rgb = _rgb(d.muted)
    s2.format.line.width = Pt(1.5)
    _value_axis_soft(ch, d)


def _velocity(slide, d: Design, x, y, w, h, data: dict):
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION

    cd = CategoryChartData()
    cd.categories = data["sprints"]
    cd.add_series("Planlanan", data["planned"])
    cd.add_series("Toplam", data["total"])
    cd.add_series("Tamamlanan", data["done"])
    ch = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, x, y, w, h, cd).chart
    _style_chart_text(ch, d, 10)
    ch.has_title = False
    ch.has_legend = True
    ch.legend.position = XL_LEGEND_POSITION.BOTTOM
    ch.legend.include_in_layout = False
    plot = ch.plots[0]
    plot.gap_width = 80
    plot.overlap = -10
    for ser, color in zip(plot.series, (d.neutral, d.open, d.done)):
        ser.format.fill.solid()
        ser.format.fill.fore_color.rgb = _rgb(color)
        ser.format.line.fill.background()
    _value_axis_soft(ch, d)


# ------------------------------------------------------------------ slides


def _overview(deck: _Deck, summaries):
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches

    d = deck.d
    s = deck.content("Bir Bakışta")
    total = sum(x.total for x in summaries)
    done = sum(x.done for x in summaries)
    projects = sum(1 for x in summaries if x.parent_id is not None)
    tiles = [(str(total), "Toplam İş", d.text), (str(done), f"Tamamlanan · %{sr._pct(done, total)}", d.done),
             (str(total - done), "Devam Eden", d.open), (str(projects), "Proje", d.accent)]
    x0, tw, gap = 0.7, 2.83, 0.2
    for i, (val, label, color) in enumerate(tiles):
        x = Inches(x0 + i * (tw + gap))
        _round(s, d, x, Inches(1.6), Inches(tw), Inches(1.45))
        _text(s, d, x, Inches(1.72), Inches(tw), Inches(0.8), val, 40, color=color, bold=True,
              head=True, align=PP_ALIGN.CENTER)
        _text(s, d, x, Inches(2.57), Inches(tw), Inches(0.3), label, 12, color=d.muted,
              align=PP_ALIGN.CENTER)

    # sol: projelere göre durum
    _round(s, d, Inches(0.7), Inches(3.3), Inches(7.3), Inches(3.55))
    _text(s, d, Inches(1.0), Inches(3.48), Inches(6), Inches(0.3), "Projelere göre iş durumu",
          13, bold=True)
    _status_chart(s, d, Inches(0.85), Inches(3.8), Inches(7.0), Inches(2.95), summaries)

    # sağ: iş alanları
    _round(s, d, Inches(8.2), Inches(3.3), Inches(4.43), Inches(3.55))
    _text(s, d, Inches(8.5), Inches(3.48), Inches(4), Inches(0.3), "İş alanları", 13, bold=True)
    counts: dict[str, int] = {}
    for x in summaries:
        counts[x.domain] = counts.get(x.domain, 0) + x.total
    doms = [k for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if n][:6]
    for i, dom in enumerate(doms):
        cx = Inches(8.45 + (i % 2) * 2.1)
        cy = Inches(3.95 + (i // 2) * 0.95)
        _icon_disc(s, d, cx, cy, Inches(0.7), dom)
        _text(s, d, cx + Inches(0.82), cy + Inches(0.02), Inches(1.25), Inches(0.4),
              str(counts[dom]), 20, bold=True, head=True)
        _text(s, d, cx + Inches(0.82), cy + Inches(0.42), Inches(1.25), Inches(0.3),
              sr.DOMAIN_META.get(dom, sr.DOMAIN_META["diger"])[0], 8, color=d.muted, bold=True,
              spacing=50)


def _project_cards(deck: _Deck, chunk, start_no: int):
    """Proje kartları (slayt başına ≤3): ikon, başlık, durum, halka, tek cümle manşet."""
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches

    d = deck.d
    s = deck.content("Neler Yaptık")
    cw, gap = 3.84, 0.31
    total_w = len(chunk) * cw + (len(chunk) - 1) * gap
    x0 = (W_IN - total_w) / 2
    top, ch = 1.6, 5.3
    for i, g in enumerate(chunk):
        x = Inches(x0 + i * (cw + gap))
        _round(s, d, x, Inches(top), Inches(cw), Inches(ch))
        # büyük, silik sıra numarası
        _text(s, d, x + Inches(cw - 1.25), Inches(top + 0.15), Inches(1.05), Inches(0.8),
              f"{start_no + i:02d}", 36, color=d.track, bold=True, head=True,
              align=PP_ALIGN.RIGHT)
        _icon_disc(s, d, x + Inches(0.35), Inches(top + 0.35), Inches(0.85), g.domain)
        _text(s, d, x + Inches(0.35), Inches(top + 1.35), Inches(cw - 0.7), Inches(0.25),
              _caps(sr.DOMAIN_META.get(g.domain, sr.DOMAIN_META["diger"])[0]), 9,
              color=d.muted, bold=True, spacing=120)
        _text(s, d, x + Inches(0.35), Inches(top + 1.62), Inches(cw - 0.7), Inches(0.75),
              sr._clip(g.title, 58), 17, bold=True, head=True)
        _pill(s, d, x + Inches(0.35), Inches(top + 2.45), g.status, g.status == "Tamamlandı")
        ring = 1.55
        _ring(s, d, x + Inches(0.3), Inches(top + 2.95), Inches(ring), g.done, g.total,
              pct_size=20)
        _text(s, d, x + Inches(0.3 + ring + 0.15), Inches(top + 3.35), Inches(cw - ring - 0.8),
              Inches(0.4), f"{g.done} / {g.total}", 20, bold=True, head=True)
        _text(s, d, x + Inches(0.3 + ring + 0.15), Inches(top + 3.78), Inches(cw - ring - 0.8),
              Inches(0.3), "iş tamamlandı", 11, color=d.muted)
        line = g.headline or (g.bullets[0] if g.bullets else "")
        if line:
            _text(s, d, x + Inches(0.35), Inches(top + 4.6), Inches(cw - 0.7), Inches(0.6),
                  sr._clip(line, 110), 11, color=d.muted)


def _other_tiles(deck: _Deck, chunk):
    """Küçük projeler: 3x2 kutu — ikon, başlık, durum, mini çubuk. Cümle yok."""
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches

    d = deck.d
    s = deck.content("Diğer Çalışmalar")
    tw, th, gx, gy = 3.84, 2.45, 0.31, 0.3
    for i, g in enumerate(chunk):
        x = Inches(0.7 + (i % 3) * (tw + gx))
        y = Inches(1.6 + (i // 3) * (th + gy))
        _round(s, d, x, y, Inches(tw), Inches(th))
        _icon_disc(s, d, x + Inches(0.3), y + Inches(0.3), Inches(0.7), g.domain)
        _text(s, d, x + Inches(1.15), y + Inches(0.3), Inches(tw - 1.4), Inches(0.75),
              sr._clip(g.title, 48), 14, bold=True, head=True)
        _pill(s, d, x + Inches(0.3), y + Inches(1.3), g.status, g.status == "Tamamlandı", 9)
        _text(s, d, x + Inches(0.3), y + Inches(1.75), Inches(tw - 0.6), Inches(0.25),
              f"{g.done} / {g.total} iş", 10, color=d.muted, bold=True, align=PP_ALIGN.RIGHT)
        _bar(s, d, x + Inches(0.3), y + Inches(2.03), Inches(tw - 0.6), Inches(0.12),
             g.done / g.total if g.total else 0)


def _chart_slide(deck: _Deck, title: str, draw, data: dict):
    from pptx.util import Inches

    s = deck.content(title)
    _round(s, deck.d, Inches(0.7), Inches(1.6), Inches(11.93), Inches(5.25))
    draw(s, deck.d, Inches(0.95), Inches(1.8), Inches(11.4), Inches(4.85), data)


def _team_slide(deck: _Deck, image_path: str):
    from pptx.util import Inches

    if not image_path or not os.path.exists(image_path):
        return
    s = deck.content("Ekip")
    _round(s, deck.d, Inches(0.7), Inches(1.6), Inches(11.93), Inches(5.25), fill="#FFFFFF")
    try:
        from PIL import Image

        iw, ih = Image.open(image_path).size
    except Exception:
        iw, ih = 4, 3
    box_w, box_h = 11.4, 4.95
    scale = min(box_w / iw, box_h / ih)
    w, h = iw * scale, ih * scale
    s.shapes.add_picture(image_path, Inches(0.7 + (11.93 - w) / 2), Inches(1.6 + (5.25 - h) / 2),
                         width=Inches(w), height=Inches(h))


def build_designed_pptx(
    design_key: str,
    title_line: str,
    sprint_name: str,
    date_range: str,
    summaries: list,
    out_path: str,
    burndown: dict | None = None,
    velocity: dict | None = None,
) -> str:
    """Alternatif tasarımla .pptx: kapak · bir bakışta · burndown · proje kartları ·
    diğer çalışmalar · velocity · ekip · teşekkürler."""
    d = DESIGNS[design_key]
    deck = _Deck(d, f"{_caps(sprint_name)}  ·  {_caps(title_line)}")
    deck.cover(title_line, sprint_name, date_range)
    if summaries:
        _overview(deck, summaries)
    if burndown:
        _chart_slide(deck, "Burndown", _burndown, burndown)
    min_items = sr._min_items()
    top = [g for g in summaries if g.total >= min_items]
    rest = [g for g in summaries if g.total < min_items]
    for i in range(0, len(top), 3):
        _project_cards(deck, top[i: i + 3], i + 1)
    for i in range(0, len(rest), 6):
        _other_tiles(deck, rest[i: i + 6])
    if velocity:
        _chart_slide(deck, "Son Sprintler", _velocity, velocity)
    _team_slide(deck, sr.team_image_path())
    deck.closing()
    deck.prs.save(out_path)
    return out_path
