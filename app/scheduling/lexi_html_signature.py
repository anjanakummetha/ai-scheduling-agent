"""HTML email signature for Lexi outbound mail (IFG branding, Heidi-style layout)."""

from __future__ import annotations

import base64
import html
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR

_DEFAULT_LOGO = ROOT_DIR / "data" / "ifg_lexi_signature_logo.png"
_INLINE_LOGO_FILENAME = "ifg-logo.png"
_INLINE_LOGO_CID = _INLINE_LOGO_FILENAME
_IFG_WEBSITE = "https://www.iconicfounders.com/"


def lexi_signature_logo_url() -> str | None:
    url = os.getenv("LEXI_SIGNATURE_LOGO_URL", "").strip()
    return url or None


def lexi_signature_uses_hosted_logo() -> bool:
    return lexi_signature_logo_url() is not None


def lexi_html_signature_enabled() -> bool:
    return os.getenv("LEXI_HTML_SIGNATURE_ENABLED", "true").lower() in {"1", "true", "yes"}


def lexi_signature_logo_path() -> Path:
    return Path(os.getenv("LEXI_SIGNATURE_LOGO_PATH", str(_DEFAULT_LOGO)))


@lru_cache(maxsize=1)
def _embedded_logo_data_uri() -> str | None:
    path = lexi_signature_logo_path()
    if not path.is_file():
        return None
    raw = path.read_bytes()
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


def lexi_signature_logo_enabled() -> bool:
    """Whether the sign-off shows the IFG logo at all.

    On by default (embedded as an inline CID attachment). The earlier distortion
    was the <img> forcing the 1024x678 asset into a 132x132 square — fixed by
    scaling on width only. Set LEXI_SIGNATURE_EMBED_LOGO=false to disable, or a
    hosted LEXI_SIGNATURE_LOGO_URL to serve it without an attachment.
    """
    if lexi_signature_logo_url() is not None:
        return True
    return os.getenv("LEXI_SIGNATURE_EMBED_LOGO", "true").lower() in {"1", "true", "yes"}


def resolve_lexi_signature_logo_src(*, prefer_cid: bool = False) -> str | None:
    """Hosted HTTPS URL (universal) or inline CID for draft+attachment send.
    Returns None when the logo is disabled (the sign-off then has no image)."""
    if not lexi_signature_logo_enabled():
        return None
    url = lexi_signature_logo_url()
    if url and not prefer_cid:
        return url
    if prefer_cid or not url:
        return f"cid:{_INLINE_LOGO_CID}"
    if os.getenv("LEXI_SIGNATURE_EMBED_LOGO", "true").lower() in {"1", "true", "yes"}:
        return _embedded_logo_data_uri()
    return f"cid:{_INLINE_LOGO_CID}"


def build_lexi_inline_logo_attachment() -> dict[str, Any] | None:
    """Microsoft Graph inline attachment for the IFG logo (works in Gmail/Outlook)."""
    if not lexi_signature_logo_enabled():
        return None
    path = lexi_signature_logo_path()
    if not path.is_file():
        return None
    content_bytes = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": _INLINE_LOGO_FILENAME,
        "contentType": "image/png",
        "contentBytes": content_bytes,
        "isInline": True,
        "contentId": _INLINE_LOGO_CID,
    }


def _plain_to_html_paragraphs(text: str) -> str:
    """Convert plain-text body (no sign-off) to simple HTML paragraphs."""
    text = (text or "").strip().replace("\r\n", "\n")
    if not text:
        return ""
    blocks = re.split(r"\n\n+", text)
    parts: list[str] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if block.startswith("•") or block.startswith("- "):
            items = []
            for line in block.split("\n"):
                line = line.strip()
                if line.startswith("•"):
                    items.append(f"<li>{html.escape(line[1:].strip())}</li>")
                elif line.startswith("- "):
                    items.append(f"<li>{html.escape(line[2:].strip())}</li>")
                else:
                    items.append(f"<li>{html.escape(line)}</li>")
            parts.append("<ul style=\"margin:0 0 12px 18px;padding:0;\">" + "".join(items) + "</ul>")
        else:
            inner = "<br>".join(html.escape(ln) for ln in block.split("\n"))
            parts.append(f"<p style=\"margin:0 0 12px 0;\">{inner}</p>")
    return "\n".join(parts)


def _strip_lexi_plain_signoff(text: str) -> str:
    from app.scheduling.lexi_voice import LEXI_SIGNOFF_BLOCK

    normalized = (text or "").strip()
    if normalized.endswith(LEXI_SIGNOFF_BLOCK):
        return normalized[: -len(LEXI_SIGNOFF_BLOCK)].rstrip()
    return normalized


def build_lexi_html_signature_block(*, use_cid: bool = True) -> str:
    """Heidi-style two-column signature: logo left, contact right."""
    logo_src = resolve_lexi_signature_logo_src(prefer_cid=use_cid)
    logo_cell = ""
    if logo_src:
        # Width-only sizing: the asset is 1024x678, so a forced square height
        # distorts it. Email clients scale proportionally from width alone.
        logo_cell = (
            f'<img src="{html.escape(logo_src, quote=True)}" '
            'alt="Iconic Founders Group" width="132" '
            'style="display:block;width:132px;height:auto;border:0;" />'
        )
    company = (
        f'<a href="{html.escape(_IFG_WEBSITE, quote=True)}" '
        'style="color:#0563c1;text-decoration:underline;">Iconic Founders Group</a>'
    )
    email = (
        '<a href="mailto:lexi@iconicfounders.com" '
        'style="color:#0563c1;text-decoration:underline;">lexi@iconicfounders.com</a>'
    )
    contact = (
        '<div style="font-size:15px;font-weight:normal;color:#000000;margin:0 0 4px 0;">Lexi Knightly</div>'
        f'<div style="margin:0 0 4px 0;">{company}</div>'
        '<div style="margin:0 0 4px 0;color:#333333;">Executive Assistant</div>'
        f'<div style="margin:0;">{email}</div>'
    )
    if not logo_cell:
        # No logo — clean single-column block (no empty cell or divider).
        return (
            '<table cellpadding="0" cellspacing="0" border="0" role="presentation" '
            'style="margin-top:20px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#333333;line-height:1.4;">'
            f'<tr><td style="vertical-align:middle;">{contact}</td></tr></table>'
        )
    return f"""<table cellpadding="0" cellspacing="0" border="0" role="presentation" style="margin-top:20px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#333333;line-height:1.4;">
  <tr>
    <td style="padding:0 16px 0 0;vertical-align:middle;">{logo_cell}</td>
    <td style="vertical-align:middle;border-left:1px solid #cccccc;padding-left:16px;">
      {contact}
    </td>
  </tr>
</table>"""


def build_lexi_html_email(plain_body: str, *, use_cid: bool = True) -> str:
    """Full HTML body for Outlook send."""
    from app.scheduling.email_format import finalize_lexi_email_body

    normalized = finalize_lexi_email_body(plain_body, max_chars=None)
    main = _strip_lexi_plain_signoff(normalized)
    body_html = _plain_to_html_paragraphs(main)
    closing = '<p style="margin:16px 0 0 0;">Thank you,</p>' if main else ""
    sig = build_lexi_html_signature_block(use_cid=use_cid)
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#333333;">'
        f"{body_html}{closing}\n{sig}"
        "</div>"
    )


def lexi_html_email_package(plain_body: str) -> tuple[str, list[dict[str, Any]], bool]:
    """HTML body, optional inline attachment, and whether draft+attach send is required."""
    if lexi_signature_uses_hosted_logo():
        return build_lexi_html_email(plain_body, use_cid=False), [], False
    attachment = build_lexi_inline_logo_attachment()
    if not attachment:
        return build_lexi_html_email(plain_body, use_cid=False), [], False
    html_body = build_lexi_html_email(plain_body, use_cid=True)
    return html_body, [attachment], True


def plain_preview_from_html(html_body: str) -> str:
    """Best-effort plain text for logging (Teams cards stay plain)."""
    text = re.sub(r"<br\s*/?>", "\n", html_body, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()
