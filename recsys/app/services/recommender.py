import anthropic, time, re
from app.config import get_settings

settings = get_settings()


def clean_for_speech(text: str) -> str:
    """Strip markdown and special characters that TTS reads literally."""
    # Remove bold/italic markers
    text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text)
    # Remove headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bullet points and dashes used as list markers
    text = re.sub(r'^\s*[-•*]\s+', '', text, flags=re.MULTILINE)
    # Remove pipe characters (table separators)
    text = text.replace('|', ',')
    # Remove backticks
    text = text.replace('`', '')
    # Remove underscores used for emphasis
    text = re.sub(r'_(.*?)_', r'\1', text)
    # Collapse multiple spaces/newlines
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'  +', ' ', text)
    return text.strip()


def get_recommendation(menu_text: str, query: str) -> dict:
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    # Strip brand names so Claude presents items as Rosewood offerings
    clean_menu = menu_text.replace("Starbucks", "Rosewood").replace("starbucks", "Rosewood")

    system = (
        "You are a friendly luxury hotel concierge at Rosewood Hotels, handling room service orders. "
        "The items below are available through Rosewood's in-room dining and cafe service. "
        "Based on the menu below, give a short enthusiastic 1-2 sentence recommendation. "
        "Be specific — mention actual item names and prices. "
        "Keep it concise, under 40 words. "
        "IMPORTANT: Plain text only. No asterisks, no markdown, no bullet points, "
        "no special characters. Write exactly as you would speak it.\n\n"
        "ROOM SERVICE MENU:\n" + clean_menu[:6000]
    )
    t0 = time.time()
    msg = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": query}],
        system=system
    )
    latency_ms = round((time.time() - t0) * 1000, 1)
    text = clean_for_speech(msg.content[0].text.strip())

    noise = len(text) < 20 or "I don't know" in text

    return {
        "recommendation": text,
        "claude_latency_ms": latency_ms,
        "noise_flag": noise
    }
