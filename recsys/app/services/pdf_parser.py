"""
PDF menu parser.

Fast path : pdfplumber text extraction  (text-based PDFs)
Fallback  : pdf2image → Claude vision   (image/scanned PDFs)
"""

import io, os, base64, re, json, time
import pdfplumber
import httpx

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ── helpers ────────────────────────────────────────────────────────────────────

def _extract_text_pdfplumber(pdf_bytes: bytes) -> str:
    """Return all text from the PDF, or '' if none found."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            parts = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    parts.append(t)
            return "\n".join(parts).strip()
    except Exception as e:
        print(f"[pdf_parser] pdfplumber error: {e}")
        return ""


def _pdf_pages_to_base64(pdf_bytes: bytes, dpi: int = 150) -> list[str]:
    """Convert each PDF page to a base64-encoded JPEG string."""
    from pdf2image import convert_from_bytes
    images = convert_from_bytes(pdf_bytes, dpi=dpi, fmt="jpeg")
    result = []
    for img in images:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        result.append(base64.standard_b64encode(buf.getvalue()).decode())
    return result


def _claude_vision_extract(pdf_bytes: bytes) -> str:
    """
    Send PDF pages as images to Claude and ask it to return the full menu text.
    Returns raw text suitable for the recommender.
    """
    if not ANTHROPIC_API_KEY:
        return ""

    try:
        b64_pages = _pdf_pages_to_base64(pdf_bytes)
    except Exception as e:
        print(f"[pdf_parser] pdf2image error: {e}")
        return ""

    # Build content blocks — one image per page (cap at 6 pages to save tokens)
    content = []
    for b64 in b64_pages[:6]:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
        })
    content.append({
        "type": "text",
        "text": (
            "These are pages from a restaurant menu. "
            "Extract ALL menu items with their names, descriptions, and prices. "
            "Format each item as: ITEM NAME | description | $price\n"
            "If no price, just write the name and description. "
            "Be thorough — include every drink, food item, and modifier you can see."
        )
    })

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"]
        print(f"[pdf_parser] Claude vision extracted {len(text)} chars")
        return text
    except Exception as e:
        print(f"[pdf_parser] Claude vision error: {e}")
        return ""


def _parse_items(text: str) -> list[dict]:
    """
    Extract structured items from text.
    Looks for lines with prices ($X.XX) or pipe-separated format from Claude vision.
    """
    items = []
    price_re = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")

    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) < 3:
            continue
        price_match = price_re.search(line)
        price = float(price_match.group(1)) if price_match else None
        # Strip the price from the name
        name = price_re.sub("", line).strip(" |.-")
        if name:
            items.append({"name": name, "price": price})

    return items


# ── public API ─────────────────────────────────────────────────────────────────

def parse_menu_pdf(pdf_bytes: bytes) -> dict:
    """
    Parse a menu PDF. Returns:
      {
        "raw_text": str,        # full text for Claude recommender
        "items": list[dict],    # [{name, price}, ...]
        "method": str           # "text" | "vision"
      }
    """
    # 1. Try fast text extraction
    raw_text = _extract_text_pdfplumber(pdf_bytes)
    method = "text"

    # 2. If no usable text, fall back to Claude vision
    if len(raw_text.strip()) < 50:
        print("[pdf_parser] No text found — falling back to Claude vision OCR")
        raw_text = _claude_vision_extract(pdf_bytes)
        method = "vision"

    items = _parse_items(raw_text)
    print(f"[pdf_parser] method={method}  chars={len(raw_text)}  items={len(items)}")
    return {"raw_text": raw_text, "items": items, "method": method}
