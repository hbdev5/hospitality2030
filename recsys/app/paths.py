"""
Deployment root resolution.

Every deployment (recsys, aie26, …) runs the same code from its OWN directory.
APP_HOME is that directory — the parent of the `app/` package — so templates,
static, uploads and audio always resolve inside the running deployment instead
of a hardcoded `~/work/recsys`. Override explicitly with the APP_HOME env var.
"""
import os

APP_HOME = (os.environ.get("APP_HOME")
            or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def home(*parts) -> str:
    """Path inside this deployment's root, e.g. home('templates')."""
    return os.path.join(APP_HOME, *parts)
