"""Obtaining the long-lived Google master token.

Password and app-password logins to Keep no longer work reliably; the
supported route is to sign in through Google's own embedded-setup page, take
the one-shot OAuth token it hands back, and exchange it for a master token.

The master token is equivalent to a logged-in device: it survives indefinitely
but is revoked when the account password changes or the device is removed from
the account, which is the failure this bridge alerts on.
"""

from __future__ import annotations

import uuid

import gpsoauth

EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"

INSTRUCTIONS = f"""
Obtaining a Google master token
===============================

1. Open a private/incognito browser window and go to:

     {EMBEDDED_SETUP_URL}

2. Sign in as the account whose Keep shopping list you want to mirror.
   Stop when you reach the "Google Terms of Service" page -- do not accept it.

3. Open your browser's developer tools -> Application/Storage -> Cookies for
   accounts.google.com, and copy the value of the "oauth_token" cookie.
   It starts with "oauth2_4/".

4. Paste it below. It is single-use, so if this fails, start again at step 1.
"""


def generate_android_id() -> str:
    """A stable per-install device id. Google ties the token to this value."""
    return uuid.uuid4().hex[:16]


def exchange(email: str, oauth_token: str, android_id: str) -> str:
    """Trade the one-shot OAuth token for a master token."""
    response = gpsoauth.exchange_token(email, oauth_token, android_id)
    token = response.get("Token")
    if not token:
        raise RuntimeError(
            "Google did not return a master token. Response: "
            f"{response.get('Error') or response}\n"
            "The oauth_token is single-use and expires quickly -- fetch a fresh one."
        )
    return token
