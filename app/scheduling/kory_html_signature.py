"""HTML email signature for mail sent from Kory's own mailbox.

Kory-channel mail went out as plain text with a bare "Let's Win, / Kory" — no
podcast line, no logo, no contact block — while Lexi-channel mail carried a full
branded signature. This is the same treatment for his own account, matching the
block he signs with by hand.

The logo, its CID/hosted resolution, and the plain-to-HTML conversion are shared
with the Lexi signature: same asset, same delivery constraints (Gmail only
renders an inline image reliably when it is a CID attachment on a draft).
"""

from __future__ import annotations

import html
import os
import re
from typing import Any

from app.scheduling.lexi_html_signature import (
    _plain_to_html_paragraphs,
    build_lexi_inline_logo_attachment,
    lexi_signature_uses_hosted_logo,
    resolve_lexi_signature_logo_src,
)

_IFG_WEBSITE = "https://www.iconicfounders.com/"
# The podcast has its own site — the company site is a different destination and
# sending listeners there is a dead end for this line.
_PODCAST_WEBSITE = "https://www.theturnpodcast.com/"
_KORY_NAME = "Kory Mitchell"
_KORY_TITLE = "CEO"
_KORY_COMPANY = "Iconic Founders Group"
_KORY_LOCATION = "Denver, Colorado"
_KORY_MOBILE = "720-561-0611"
_PODCAST_LINE_PREFIX = (
    "See amazing founders who sold their businesses on my podcast The Turn "
    "- available at "
)
# The line ends on the link — no trailing "and all podcast channels."
_PODCAST_LABEL = "The Turn Podcast"


def kory_html_signature_enabled() -> bool:
    return os.getenv("LEXI_KORY_HTML_SIGNATURE_ENABLED", "true").lower() in {"1", "true", "yes"}


def _strip_kory_plain_signoff(text: str) -> str:
    """Remove a trailing "Let's Win, / Kory" so the HTML block doesn't double it.

    finalize_outbound_email_body appends the plain sign-off before this runs, and
    the composer often writes one too, so both spellings have to go — with or
    without the comma/exclamation, and with an optional surname.
    """
    normalized = (text or "").strip()
    return re.sub(
        r"\n*\s*(?:Let'?s\s+Win|Best|Thanks|Thank you|Warmly|Regards|Cheers)[,!.]?\s*\n+"
        r"\s*Kory(?:\s+Mitchell)?\s*$",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).rstrip()


def build_kory_html_signature_block(*, use_cid: bool = True) -> str:
    """Sign-off, podcast line, then logo-left / contact-right block."""
    podcast_link = (
        f'<a href="{html.escape(_PODCAST_WEBSITE, quote=True)}" '
        f'style="color:#0563c1;text-decoration:underline;">{html.escape(_PODCAST_LABEL)}</a>'
    )
    signoff = (
        '<p style="margin:16px 0 0 0;">Let\'s Win!</p>'
        '<p style="margin:16px 0 0 0;">Kory</p>'
        '<p style="margin:16px 0 0 0;"><em>'
        f"{html.escape(_PODCAST_LINE_PREFIX)}{podcast_link}"
        "</em></p>"
    )

    company = (
        f'<a href="{html.escape(_IFG_WEBSITE, quote=True)}" '
        f'style="color:#0563c1;text-decoration:underline;">{html.escape(_KORY_COMPANY)}</a>'
    )
    contact = (
        f'<div style="margin:0 0 4px 0;"><strong>{html.escape(_KORY_NAME)} - '
        f'{html.escape(_KORY_TITLE)}</strong></div>'
        f'<div style="margin:0 0 4px 0;">{company}</div>'
        f'<div style="margin:0 0 4px 0;">{html.escape(_KORY_LOCATION)}</div>'
        f'<div style="margin:0;">M: {html.escape(_KORY_MOBILE)}</div>'
    )

    logo_src = resolve_lexi_signature_logo_src(prefer_cid=use_cid)
    if not logo_src:
        return (
            f"{signoff}"
            '<table cellpadding="0" cellspacing="0" border="0" role="presentation" '
            'style="margin-top:16px;font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            'color:#333333;line-height:1.4;">'
            f'<tr><td style="vertical-align:top;">{contact}</td></tr></table>'
        )

    # Width-only sizing — the asset is 1024x678 and a forced square distorts it.
    logo_cell = (
        f'<img src="{html.escape(logo_src, quote=True)}" '
        f'alt="{html.escape(_KORY_COMPANY)}" width="150" '
        'style="display:block;width:150px;height:auto;border:0;" />'
    )
    return f"""{signoff}
<table cellpadding="0" cellspacing="0" border="0" role="presentation" style="margin-top:16px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#333333;line-height:1.4;">
  <tr>
    <td style="padding:0 16px 0 0;vertical-align:top;">{logo_cell}</td>
    <td style="vertical-align:top;">
      {contact}
    </td>
  </tr>
</table>"""


def build_kory_html_email(plain_body: str, *, use_cid: bool = True) -> str:
    """Full HTML body for a Kory-channel send."""
    main = _strip_kory_plain_signoff(plain_body)
    body_html = _plain_to_html_paragraphs(main)
    sig = build_kory_html_signature_block(use_cid=use_cid)
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#333333;">'
        f"{body_html}\n{sig}"
        "</div>"
    )


def kory_html_email_package(plain_body: str) -> tuple[str, list[dict[str, Any]], bool]:
    """HTML body, optional inline attachment, and whether draft+attach send is required.

    Gmail drops `data:` image URIs, so when the logo is not hosted the send has to
    go the draft→attach→send route with a CID reference. Same constraint the Lexi
    signature hit.
    """
    if lexi_signature_uses_hosted_logo():
        return build_kory_html_email(plain_body, use_cid=False), [], False
    attachment = build_lexi_inline_logo_attachment()
    if not attachment:
        return build_kory_html_email(plain_body, use_cid=False), [], False
    return build_kory_html_email(plain_body, use_cid=True), [attachment], True
