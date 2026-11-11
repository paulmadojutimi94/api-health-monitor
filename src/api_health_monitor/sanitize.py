"""Make server-controlled text safe to print or post into chat.

The endpoints we monitor choose their own HTTP reason phrase ("500 Internal
Server Error" can say anything), error bodies and redirect targets. That text
ends up in our terminal and in Slack. Without scrubbing, a hostile or simply
broken server can:

  * page your whole team:      "500 <!channel> please log in at evil.example"
  * scramble your terminal:     "\\x1b[2J" clears the screen
  * hide text right-to-left:    "\\u202e" reverses what follows
  * flood the alert:            a 2 MB error message
"""

from __future__ import annotations

import re
import unicodedata

MAX_DETAIL_CHARS = 200

# C0/C1 control characters (includes ESC \x1b, which starts terminal escapes)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Unicode "format" characters: zero-width spaces, right-to-left overrides, etc.
_FORMAT_CATEGORIES = {"Cf"}


def clean_text(text: object, limit: int = MAX_DETAIL_CHARS) -> str:
    """Remove control/format characters, collapse whitespace, truncate."""
    s = str(text)
    s = _CONTROL.sub(" ", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) not in _FORMAT_CATEGORIES)
    s = " ".join(s.split())
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def escape_chat(text: str) -> str:
    """Escape the three characters Slack (and Discord-style markup) treat as commands.

    Slack turns <!channel>, <!here>, <@U123> and <http://x|label> into
    mentions and disguised links. Escaping < > & makes them plain text.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def discord_safe(text: str) -> str:
    """Discord pings on bare @everyone / @here; a zero-width space after @ defuses them.

    (Belt and braces: the webhook payload also sets allowed_mentions to none.)
    """
    return re.sub(r"@(everyone|here)", "@\u200b\\1", text)
