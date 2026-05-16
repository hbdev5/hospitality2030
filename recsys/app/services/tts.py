"""
ElevenLabs TTS helper.
Generates MP3 files, saves to static/audio/, returns a public URL for Plivo <Play>.
Caches by content hash so repeated phrases reuse the same file.
"""
import io, hashlib, httpx, os

AUDIO_DIR = os.path.expanduser("~/work/recsys/static/audio")
BASE_URL  = "https://support.hostbuddy.io/recsys/static/audio"


def text_to_speech(text: str) -> str:
    """Returns public MP3 URL, or '' on failure."""
    # Import settings here to avoid circular import at module load time
    from app.config import get_settings
    s = get_settings()
    api_key  = s.elevenlabs_api_key
    voice_id = s.elevenlabs_voice_id

    if not api_key:
        print("[tts] ELEVENLABS_API_KEY not set")
        return ""

    cache_key = hashlib.md5(f"{voice_id}:{text}".encode()).hexdigest()
    filename  = f"{cache_key}.mp3"
    filepath  = os.path.join(AUDIO_DIR, filename)

    if os.path.exists(filepath):
        return f"{BASE_URL}/{filename}"

    try:
        resp = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": "eleven_turbo_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        os.makedirs(AUDIO_DIR, exist_ok=True)
        with open(filepath, "wb") as f:
            f.write(resp.content)
        print(f"[tts] Generated {filename} ({len(resp.content)} bytes)")
        return f"{BASE_URL}/{filename}"
    except Exception as e:
        print(f"[tts] ElevenLabs error: {e}")
        return ""
