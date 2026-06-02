"""
VIP member-card image generator — Pillow port of the Java VipMemberCardGenerator,
styled to the Curry Bliss premium mockup.

FRONT : logo, restaurant name, tagline, member name, "VIP MEMBER",
        gold band (ID · MEMBERSHIP DETAILS · BENEFITS OVERLEAF ►)
BACK  : EXCLUSIVE BENEFITS (3 lines), VALIDITY mm/yyyy–mm/yyyy, centered QR,
        "Scan to verify or redeem points…", website, MEMBER ID, ISSUED BY footer.

Look & feel (colors, title, tagline, 3 benefits, logo, website) is merchant-
configurable in Studio admin mode. PNG is written to static/vip_cards/ and
served by the existing /static mount (no Cloudinary).
"""

import os
from PIL import Image, ImageDraw, ImageFont
from app.config import get_settings

settings = get_settings()

W = 900
HF = 500          # front height
HB = 600          # back height
GAP = 20
_FONT_DIR = "/usr/share/fonts/truetype/dejavu"
_STATIC_DIR = os.path.expanduser("~/work/recsys/static")
_CARD_DIR   = os.path.join(_STATIC_DIR, "vip_cards")

_DEFAULT_BENEFITS = [
    "Priority reservations & seating",
    "Curated member events & tastings",
    "Bespoke concierge service",
]


# ── color + font helpers ──────────────────────────────────────────────────────

def _hex(c, default=(197, 160, 60)):
    try:
        c = (c or "").strip().lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return default


def _lighten(rgb, amt=0.35):
    return tuple(min(255, int(v + (255 - v) * amt)) for v in rgb)


def _darken(rgb, amt=0.6):
    return tuple(max(0, int(v * (1 - amt))) for v in rgb)


def _font(name, size):
    try:
        return ImageFont.truetype(os.path.join(_FONT_DIR, name), size)
    except Exception:
        return ImageFont.load_default()


def _vgrad(size, top, bottom):
    w, h = size
    base = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        base.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return base.resize((w, h))


def _center(draw, y, text, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, y), text, font=font, fill=fill)
    return bbox[2] - bbox[0]


def _border(draw, box, radius, color, width):
    draw.rounded_rectangle(box, radius=radius, outline=color, width=width)


def _make_qr(data, px):
    try:
        import qrcode
        qr = qrcode.QRCode(border=1, box_size=10,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data or "")
        qr.make(fit=True)
        return qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((px, px))
    except Exception:
        return None


# ── faces ─────────────────────────────────────────────────────────────────────

def _front(cfg):
    accent  = _hex(cfg["accent"])
    accent_l = _lighten(accent, 0.45)
    bg      = _hex(cfg["bg"], (18, 18, 18))
    bg2     = _lighten(bg, 0.10) if sum(bg) < 200 else _darken(bg, 0.15)

    img = _vgrad((W, HF), bg, bg2).convert("RGB")
    d = ImageDraw.Draw(img)
    _border(d, (10, 10, W - 10, HF - 10), 30, accent, 4)
    _border(d, (18, 18, W - 18, HF - 18), 24, accent_l, 1)

    y_name = 130
    logo_path = cfg.get("logo_path")
    if logo_path and os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert("RGBA")
            logo.thumbnail((110, 110))
            img.paste(logo, ((W - logo.width) // 2, 36), logo)
            y_name = 160
        except Exception:
            pass

    _center(d, y_name, cfg["title"].upper(), _font("DejaVuSerif-Bold.ttf", 62), accent_l)
    _center(d, y_name + 70, cfg["subtitle"], _font("DejaVuSerif-Italic.ttf", 26), (210, 205, 190))

    dy = y_name + 116
    d.line((W // 2 - 170, dy, W // 2 + 170, dy), fill=accent, width=1)
    _center(d, dy + 26, cfg["member_name"].upper(), _font("DejaVuSans-Bold.ttf", 36), (236, 236, 240))
    _center(d, dy + 76, "V I P   M E M B E R", _font("DejaVuSans.ttf", 15), accent_l)

    # Gold band footer: ID · MEMBERSHIP DETAILS · BENEFITS OVERLEAF
    band = _vgrad((W, 56), _darken(accent, 0.45), accent)
    img.paste(band, (0, HF - 56))
    d = ImageDraw.Draw(img)
    f = _font("DejaVuSansMono-Bold.ttf", 13)
    d.text((34, HF - 38), f"ID: {cfg['member_id'].upper()}", font=f, fill=_darken(bg, 0.2) if sum(bg) > 200 else (20, 18, 12))
    _center(d, HF - 40, "MEMBERSHIP DETAILS", _font("DejaVuSans-Bold.ttf", 17), (28, 22, 8))
    bo = "BENEFITS OVERLEAF ►"
    bb = d.textbbox((0, 0), bo, font=f)
    d.text((W - (bb[2] - bb[0]) - 28, HF - 38), bo, font=f, fill=(28, 22, 8))
    return img


def _back(cfg):
    accent  = _hex(cfg["accent"])
    accent_l = _lighten(accent, 0.45)
    bg      = _hex(cfg["bg"], (18, 18, 18))
    bg2     = _lighten(bg, 0.10) if sum(bg) < 200 else _darken(bg, 0.15)

    img = _vgrad((W, HB), bg, bg2).convert("RGB")
    d = ImageDraw.Draw(img)
    _border(d, (10, 10, W - 10, HB - 10), 30, accent, 4)
    _border(d, (18, 18, W - 18, HB - 18), 24, accent_l, 1)

    hw = _center(d, 40, "EXCLUSIVE BENEFITS", _font("DejaVuSans-Bold.ttf", 22), (236, 236, 240))
    d.line(((W - hw) // 2, 72, (W + hw) // 2, 72), fill=accent, width=2)

    numf, txtf = _font("DejaVuSerif-Bold.ttf", 24), _font("DejaVuSans.ttf", 20)
    by = 100
    for i, b in enumerate(cfg["benefits"][:3], 1):
        d.text((78, by), str(i), font=numf, fill=accent_l)
        d.text((116, by + 2), b, font=txtf, fill=(224, 221, 211))
        by += 46

    # Validity
    vy = by + 14
    _center(d, vy, f"VALIDITY: {cfg['validity_from']} to {cfg['validity_to']}",
            _font("DejaVuSans-Bold.ttf", 18), accent_l)

    # QR centered
    qpx = 150
    qr = _make_qr(cfg.get("qr_data"), qpx)
    qy = vy + 36
    if qr:
        pad = Image.new("RGB", (qpx + 20, qpx + 20), (255, 255, 255))
        pad.paste(qr, (10, 10))
        img.paste(pad, ((W - pad.width) // 2, qy))
        qy += pad.height
    else:
        qy += 20

    d = ImageDraw.Draw(img)
    _center(d, qy + 8, "Scan to verify or redeem points. Terms and conditions apply.",
            _font("DejaVuSans.ttf", 13), (180, 176, 166))
    line_y = qy + 28
    if cfg.get("website"):
        _center(d, line_y, cfg["website"], _font("DejaVuSans.ttf", 13), accent_l)
        line_y += 22
    _center(d, line_y, f"MEMBER ID: {cfg['member_id'].upper()}",
            _font("DejaVuSansMono-Bold.ttf", 14), (210, 205, 190))

    # Issued-by gold footer
    band = _vgrad((W, 46), _darken(accent, 0.45), accent)
    img.paste(band, (0, HB - 46))
    d = ImageDraw.Draw(img)
    _center(d, HB - 32, f"ISSUED BY {cfg['issuer'].upper()}",
            _font("DejaVuSans-Bold.ttf", 14), (28, 22, 8))
    return img


# ── public API ────────────────────────────────────────────────────────────────

def render(program, member_name="MEMBER NAME", member_id="PREVIEW",
           phone=None, key=None, validity_from="[MM/YYYY]", validity_to="[MM/YYYY]") -> str:
    """Render a card PNG for a VipProgram and return its public URL."""
    os.makedirs(_CARD_DIR, exist_ok=True)

    benefits = [b.strip() for b in (program.recurring_benefit, program.benefit2, program.benefit3)
                if b and b.strip()]
    for d in _DEFAULT_BENEFITS:
        if len(benefits) >= 3:
            break
        if d not in benefits:
            benefits.append(d)

    website = (program.card_website or "").strip()
    issuer  = (program.card_title or program.program_name or "Curry Bliss").strip()
    # QR → the verify page hosted on our VM (staff/member scans to confirm the
    # active membership, benefits, and paid period).
    qr_data = f"{settings.public_base_url}/vip/verify/{member_id}"

    cfg = {
        "title":         (program.card_title or program.program_name or "VIP").strip(),
        "subtitle":      (program.card_subtitle or "Something new for every visit").strip(),
        "accent":        program.accent_color or "#C5A03C",
        "bg":            program.bg_color or "#121212",
        "member_name":   member_name,
        "member_id":     str(member_id or "PREVIEW"),
        "benefits":      benefits[:3],
        "validity_from": validity_from,
        "validity_to":   validity_to,
        "website":       website,
        "issuer":        issuer,
        "qr_data":       qr_data,
        "logo_path":     _logo_path(program.logo_url),
    }

    composite = Image.new("RGB", (W, HF + GAP + HB), (10, 10, 10))
    composite.paste(_front(cfg), (0, 0))
    composite.paste(_back(cfg), (0, HF + GAP))

    fname = f"{key or f'r{program.restaurant_id}-preview'}.png"
    composite.save(os.path.join(_CARD_DIR, fname), "PNG")
    return f"{settings.public_base_url}/static/vip_cards/{fname}"


def _logo_path(logo_url):
    if not logo_url:
        return None
    marker = "/static/"
    if marker in logo_url:
        p = os.path.join(_STATIC_DIR, logo_url.split(marker, 1)[1])
        return p if os.path.exists(p) else None
    return None
