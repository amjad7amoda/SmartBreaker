"""Shared branded email builder for the Smart Breaker platform.

Produces Arabic (RTL) HTML emails with a clean, technical visual identity (emerald +
white) and a matching plain-text fallback, then sends them via
``EmailMultiAlternatives``.

All user-facing emails across the project (accounts + organizations) go through
``send_branded_email`` so the branding stays consistent in one place.
"""
from datetime import datetime

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

# --- Brand identity -----------------------------------------------------------
BRAND_NAME_AR = 'Fluxa'
TAGLINE_AR = 'فريق الدعم الفني'

# --- Palette (emerald + white/neutral only) -----------------------------------
EMERALD = '#059669'
EMERALD_DEEP = '#047857'
EMERALD_SOFT = '#ECFDF5'
INK = '#0F172A'
MUTED = '#64748B'
BORDER = '#E2E8F0'
PAGE_BG = '#EEF2F6'
DANGER = '#DC2626'
DANGER_SOFT = '#FEF2F2'

# Status badge styling: (background, text color, arabic label)
_STATUS_STYLES = {
    'approved': (EMERALD_SOFT, EMERALD, 'تم الاعتماد'),
    'active': (EMERALD_SOFT, EMERALD, 'نشط'),
    'denied': (DANGER_SOFT, DANGER, 'مرفوض'),
}


def _paragraphs_html(paragraphs):
    out = []
    for text in paragraphs:
        out.append(
            f'<p dir="rtl" style="margin:0 0 14px 0;font-size:15px;'
            f'line-height:1.9;color:{INK};text-align:right;">{text}</p>'
        )
    return '\n'.join(out)


def _highlight_html(highlight):
    return f"""
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:8px 0 20px 0;">
        <tr>
          <td align="center" style="background:{EMERALD_SOFT};border:1px solid {EMERALD};border-radius:12px;padding:20px 16px;">
            <div dir="rtl" style="font-size:12px;color:{MUTED};margin-bottom:10px;">{highlight['caption_ar']}</div>
            <div dir="ltr" style="font-family:'Courier New',monospace;font-size:32px;font-weight:700;letter-spacing:8px;color:{EMERALD_DEEP};">{highlight['value']}</div>
          </td>
        </tr>
      </table>
    """


def _status_badge_html(status):
    bg, color, label_ar = _STATUS_STYLES[status]
    return f"""
      <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 auto 18px auto;">
        <tr>
          <td style="background:{bg};border-radius:999px;padding:8px 18px;color:{color};font-size:13px;font-weight:700;">
            &#9679; {label_ar}
          </td>
        </tr>
      </table>
    """


def _render_html(*, preheader, heading_ar, paragraphs_ar, highlight, status):
    status_html = _status_badge_html(status) if status else ''
    highlight_html = _highlight_html(highlight) if highlight else ''
    year = datetime.now().year

    return f"""\
<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background:{PAGE_BG};">
  <span style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</span>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE_BG};padding:28px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#FFFFFF;border-radius:16px;overflow:hidden;border:1px solid {BORDER};">
          <!-- gradient accent bar -->
          <tr><td style="height:5px;background:{EMERALD};font-size:0;line-height:0;">&nbsp;</td></tr>
          <!-- header / logo -->
          <tr>
            <td style="padding:26px 32px 8px 32px;" dir="rtl">
              <table role="presentation" cellpadding="0" cellspacing="0" align="right">
                <tr>
                  <td style="font-size:19px;font-weight:800;color:{INK};padding-left:10px;">القاطع الذكي</td>
                  <td style="font-size:26px;">&#9889;</td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- body -->
          <tr>
            <td style="padding:14px 32px 8px 32px;">
              {status_html}
              <h1 dir="rtl" style="margin:0 0 18px 0;font-size:20px;color:{INK};text-align:right;">{heading_ar}</h1>
              {_paragraphs_html(paragraphs_ar)}
              {highlight_html}
            </td>
          </tr>
          <!-- divider -->
          <tr><td style="padding:8px 32px;"><div style="height:1px;background:{BORDER};"></div></td></tr>
          <!-- footer -->
          <tr>
            <td style="padding:10px 32px 26px 32px;">
              <p dir="rtl" style="margin:0 0 12px 0;font-size:12px;line-height:1.7;color:{MUTED};text-align:right;">{TAGLINE_AR}</p>
              <p style="margin:0;font-size:11px;color:{MUTED};text-align:right;" dir="rtl">&copy; {year} {BRAND_NAME_AR}</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def _render_text(*, heading_ar, paragraphs_ar, highlight, status):
    lines = [BRAND_NAME_AR, '=' * 40, '']
    if status:
        _, _, label_ar = _STATUS_STYLES[status]
        lines.append(f'[{label_ar}]')
        lines.append('')
    lines.append(heading_ar)
    lines.append('')
    lines.extend(paragraphs_ar)
    if highlight:
        lines.append('')
        lines.append(f"{highlight['caption_ar']}: {highlight['value']}")
    lines.append('')
    lines.append('-' * 40)
    lines.append(TAGLINE_AR)
    return '\n'.join(lines)


def send_branded_email(*, subject, recipient, preheader, heading_ar,
                       paragraphs_ar, highlight=None, status=None):
    """Render and send one Arabic branded email.

    ``highlight`` (optional) is a dict with keys: ``value`` and ``caption_ar`` (shown
    above the code box in the HTML, and as the label in the plain-text body).
    ``status`` (optional) is one of ``approved`` / ``active`` / ``denied`` and renders a
    colored badge.
    """
    text_body = _render_text(
        heading_ar=heading_ar, paragraphs_ar=paragraphs_ar,
        highlight=highlight, status=status,
    )
    html_body = _render_html(
        preheader=preheader, heading_ar=heading_ar, paragraphs_ar=paragraphs_ar,
        highlight=highlight, status=status,
    )
    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
    )
    message.attach_alternative(html_body, 'text/html')
    message.send()
