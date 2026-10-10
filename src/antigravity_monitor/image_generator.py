"""Dynamic key image generator for Stream Deck using SVG.

Generates SVG data URIs that can be directly passed to Stream Deck's setImage API,
without requiring external image processing libraries like Pillow.
"""

from __future__ import annotations

import base64
import html


def generate_key_svg(
    title: str,
    subtitle: str = "",
    status_label: str = "",
    bg_color: str = "#222222",
    accent_color: str = "#4FC3F7",
    is_active: bool = False,
) -> str:
    # Escape status for XML/SVG safety
    safe_status = html.escape(status_label.strip())

    # Split title lines
    title_lines = [html.escape(line) for line in title.splitlines() if line.strip()]
    if not title_lines:
        title_lines = [""]

    title_svg_elements = []
    if len(title_lines) == 1:
        title_svg_elements.append(
            '<text x="72" y="72" text-anchor="middle" dominant-baseline="middle" '
            f'fill="#FFFFFF" font-family="Segoe UI, sans-serif" font-weight="bold" font-size="19">'
            f"{title_lines[0]}</text>"
        )
    elif len(title_lines) == 2:
        title_svg_elements.append(
            '<text x="72" y="55" text-anchor="middle" '
            f'fill="#FFFFFF" font-family="Segoe UI, sans-serif" font-weight="bold" font-size="18">'
            f"{title_lines[0]}</text>"
        )
        title_svg_elements.append(
            '<text x="72" y="85" text-anchor="middle" '
            f'fill="#EEEEEE" font-family="Segoe UI, sans-serif" font-size="16">'
            f"{title_lines[1]}</text>"
        )
    else:  # 3 or more lines (e.g. Quota key: 5時間枠 / 48% / 955K)
        title_svg_elements.append(
            '<text x="72" y="38" text-anchor="middle" '
            f'fill="#CCCCCC" font-family="Segoe UI, sans-serif" font-size="15">'
            f"{title_lines[0]}</text>"
        )
        title_svg_elements.append(
            '<text x="72" y="76" text-anchor="middle" '
            f'fill="#FFFFFF" font-family="Segoe UI, sans-serif" font-weight="bold" font-size="24">'
            f"{title_lines[1]}</text>"
        )
        title_svg_elements.append(
            '<text x="72" y="112" text-anchor="middle" '
            f'fill="#E0E0E0" font-family="Segoe UI, sans-serif" font-size="15">'
            f"{title_lines[2]}</text>"
        )

    # Optional status badge indicator at top-right
    badge_svg = ""
    if is_active:
        badge_svg = (
            f'<circle cx="124" cy="20" r="8" fill="{accent_color}" />'
            f'<circle cx="124" cy="20" r="12" fill="none" stroke="{accent_color}" '
            'stroke-width="2" opacity="0.6" />'
        )

    # Status ribbon at bottom if single or double line
    status_bar_svg = ""
    if safe_status and len(title_lines) <= 2:
        status_bar_svg = (
            '<rect x="12" y="108" width="120" height="24" rx="12" fill="#000000" opacity="0.4" />'
            '<text x="72" y="125" text-anchor="middle" '
            'fill="#FFFFFF" font-family="Segoe UI, sans-serif" font-weight="bold" font-size="13">'
            f"{safe_status}</text>"
        )

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">\n'
        "  <defs>\n"
        '    <linearGradient id="bgGrad" x1="0%" y1="0%" x2="0%" y2="100%">\n'
        f'      <stop offset="0%" stop-color="{bg_color}" stop-opacity="1" />\n'
        f'      <stop offset="100%" stop-color="{bg_color}" stop-opacity="0.85" />\n'
        "    </linearGradient>\n"
        "  </defs>\n"
        f'  <rect x="0" y="0" width="144" height="144" rx="20" fill="url(#bgGrad)" />\n'
        '  <rect x="2" y="2" width="140" height="140" rx="18" fill="none" stroke="#FFFFFF" '
        'stroke-width="1.5" opacity="0.25" />\n'
        f"  {badge_svg}\n"
        f"  {''.join(title_svg_elements)}\n"
        f"  {status_bar_svg}\n"
        "</svg>"
    )

    return svg


_SVG_HEADER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">\n'
)
_STATUS_BAR_RECT = (
    '  <rect x="14" y="110" width="116" height="24" rx="12" fill="#000000" opacity="0.45" />\n'
)


def generate_hub_rocket_svg(status: str = "empty") -> str:
    """Generate 144x144 SVG icon of Antigravity rocket mascot for Hub key.

    Supported status:
    - 'working': Flying diagonally with jet flame and speed lines. Label: WORKING.
    - 'done': Landed upright on moon with landing gear, stars, and flag. Label: DONE.
    - 'waiting': Crashed head-first into ground with warning exclamation, smoke, and sweat.
      Label: WAITING.
    - 'empty' / 'idle': Upright on launchpad tower resting with zZ. Label: READY.
    """
    normalized_status = status.lower().strip()

    if normalized_status == "working":
        return (
            f"{_SVG_HEADER}"
            "  <defs>\n"
            '    <linearGradient id="bgWorking" x1="0%" y1="0%" x2="0%" y2="100%">\n'
            '      <stop offset="0%" stop-color="#1565C0" />\n'
            '      <stop offset="100%" stop-color="#0D47A1" />\n'
            "    </linearGradient>\n"
            "  </defs>\n"
            '  <rect x="0" y="0" width="144" height="144" rx="20" fill="url(#bgWorking)" />\n'
            '  <rect x="2" y="2" width="140" height="140" rx="18" fill="none" stroke="#FFFFFF" '
            'stroke-width="1.5" opacity="0.2" />\n'
            '  <line x1="22" y1="92" x2="38" y2="76" stroke="#FFFFFF" '
            'stroke-width="2.5" stroke-linecap="round" opacity="0.4" />\n'
            '  <line x1="18" y1="64" x2="40" y2="42" stroke="#FFFFFF" '
            'stroke-width="2.5" stroke-linecap="round" opacity="0.5" />\n'
            '  <line x1="42" y1="104" x2="62" y2="84" stroke="#FFFFFF" '
            'stroke-width="2.5" stroke-linecap="round" opacity="0.3" />\n'
            '  <g transform="translate(68, 56) rotate(45)">\n'
            '    <path d="M-8 28 Q0 44 0 46 Q0 44 8 28 Z" fill="#FFFFFF" opacity="0.9" />\n'
            '    <circle cx="-5" cy="40" r="4.5" fill="#FFFFFF" opacity="0.75" />\n'
            '    <circle cx="5" cy="43" r="4" fill="#FFFFFF" opacity="0.65" />\n'
            '    <circle cx="1" cy="50" r="3" fill="#FFFFFF" opacity="0.5" />\n'
            '    <path d="M-15 14 L-23 26 L-13 26 Z" fill="#FFFFFF" stroke="#FFFFFF" '
            'stroke-width="2" stroke-linejoin="round" />\n'
            '    <path d="M15 14 L23 26 L13 26 Z" fill="#FFFFFF" stroke="#FFFFFF" '
            'stroke-width="2" stroke-linejoin="round" />\n'
            '    <path d="M-15 10 C-15 -18 0 -32 0 -32 C0 -32 15 -18 15 10 L15 26 L-15 26 Z" '
            'fill="#FFFFFF" />\n'
            '    <rect x="-8" y="26" width="16" height="4" rx="2" fill="#37474F" />\n'
            '    <circle cx="0" cy="-4" r="8.5" fill="#212121" />\n'
            '    <circle cx="-2.5" cy="-6.5" r="2.5" fill="#FFFFFF" opacity="0.85" />\n'
            "  </g>\n"
            f"{_STATUS_BAR_RECT}"
            '  <text x="72" y="127" text-anchor="middle" fill="#FFFFFF" '
            'font-family="Segoe UI, sans-serif" font-weight="bold" font-size="13" '
            'letter-spacing="1">WORKING</text>\n'
            "</svg>"
        )

    if normalized_status == "done":
        return (
            f"{_SVG_HEADER}"
            "  <defs>\n"
            '    <linearGradient id="bgDone" x1="0%" y1="0%" x2="0%" y2="100%">\n'
            '      <stop offset="0%" stop-color="#2E7D32" />\n'
            '      <stop offset="100%" stop-color="#1B5E20" />\n'
            "    </linearGradient>\n"
            "  </defs>\n"
            '  <rect x="0" y="0" width="144" height="144" rx="20" fill="url(#bgDone)" />\n'
            '  <rect x="2" y="2" width="140" height="140" rx="18" fill="none" stroke="#FFFFFF" '
            'stroke-width="1.5" opacity="0.2" />\n'
            '  <path d="M26 34 Q30 34 30 30 Q30 34 34 34 Q30 34 30 38 Q30 34 26 34 Z" '
            'fill="#FFFFFF" opacity="0.8" />\n'
            '  <path d="M112 38 Q115 38 115 35 Q115 38 118 38 Q115 38 115 41 Q115 38 112 38 Z" '
            'fill="#FFFFFF" opacity="0.7" />\n'
            '  <path d="M10 98 Q72 88 134 98" fill="none" stroke="#FFFFFF" '
            'stroke-width="2.5" opacity="0.4" stroke-linecap="round" />\n'
            '  <g transform="translate(64, 60)">\n'
            '    <line x1="-12" y1="20" x2="-22" y2="33" stroke="#FFFFFF" '
            'stroke-width="2.5" stroke-linecap="round" />\n'
            '    <circle cx="-22" cy="33" r="2.5" fill="#FFFFFF" />\n'
            '    <line x1="12" y1="20" x2="22" y2="33" stroke="#FFFFFF" '
            'stroke-width="2.5" stroke-linecap="round" />\n'
            '    <circle cx="22" cy="33" r="2.5" fill="#FFFFFF" />\n'
            '    <path d="M-15 4 C-15 -20 0 -34 0 -34 C0 -34 15 -20 15 4 L15 22 L-15 22 Z" '
            'fill="#FFFFFF" />\n'
            '    <rect x="-8" y="22" width="16" height="4" rx="2" fill="#37474F" />\n'
            '    <circle cx="0" cy="-6" r="8.5" fill="#212121" />\n'
            '    <circle cx="-2.5" cy="-8.5" r="2.5" fill="#FFFFFF" opacity="0.85" />\n'
            "  </g>\n"
            '  <line x1="102" y1="54" x2="102" y2="92" stroke="#FFFFFF" '
            'stroke-width="2" stroke-linecap="round" />\n'
            '  <path d="M102 56 L118 63 L102 70 Z" fill="#FFFFFF" />\n'
            f"{_STATUS_BAR_RECT}"
            '  <text x="72" y="127" text-anchor="middle" fill="#FFFFFF" '
            'font-family="Segoe UI, sans-serif" font-weight="bold" font-size="13" '
            'letter-spacing="1">DONE</text>\n'
            "</svg>"
        )

    if normalized_status == "waiting":
        return (
            f"{_SVG_HEADER}"
            "  <defs>\n"
            '    <linearGradient id="bgWaiting" x1="0%" y1="0%" x2="0%" y2="100%">\n'
            '      <stop offset="0%" stop-color="#EF6C00" />\n'
            '      <stop offset="100%" stop-color="#D84315" />\n'
            "    </linearGradient>\n"
            "  </defs>\n"
            '  <rect x="0" y="0" width="144" height="144" rx="20" fill="url(#bgWaiting)" />\n'
            '  <rect x="2" y="2" width="140" height="140" rx="18" fill="none" stroke="#FFFFFF" '
            'stroke-width="1.5" opacity="0.2" />\n'
            '  <g transform="translate(100, 22)">\n'
            '    <circle cx="10" cy="10" r="11" fill="#FFFFFF" />\n'
            '    <polygon points="4,18 2,25 9,20" fill="#FFFFFF" />\n'
            '    <text x="10" y="15" text-anchor="middle" fill="#D84315" '
            'font-family="Segoe UI, sans-serif" font-weight="900" font-size="15">!</text>\n'
            "  </g>\n"
            '  <path d="M42 38 Q36 30 42 22 Q48 14 42 6" fill="none" stroke="#FFFFFF" '
            'stroke-width="2.5" stroke-linecap="round" opacity="0.6" stroke-dasharray="3 3" />\n'
            '  <circle cx="48" cy="28" r="3" fill="#FFFFFF" opacity="0.5" />\n'
            '  <circle cx="56" cy="20" r="2" fill="#FFFFFF" opacity="0.4" />\n'
            '  <path d="M30 92 L62 90 L72 96 L82 89 L114 93" fill="none" stroke="#FFFFFF" '
            'stroke-width="2.5" opacity="0.4" stroke-linecap="round" />\n'
            '  <ellipse cx="72" cy="92" rx="18" ry="4" fill="#000000" opacity="0.4" />\n'
            '  <g transform="translate(72, 68) rotate(195)">\n'
            '    <path d="M-15 14 L-22 25 L-13 25 Z" fill="#FFFFFF" stroke="#FFFFFF" '
            'stroke-width="2" stroke-linejoin="round" />\n'
            '    <path d="M15 14 L22 25 L13 25 Z" fill="#FFFFFF" stroke="#FFFFFF" '
            'stroke-width="2" stroke-linejoin="round" />\n'
            '    <path d="M-14 8 C-14 -16 0 -28 0 -28 C0 -28 14 -16 14 8 L14 24 L-14 24 Z" '
            'fill="#FFFFFF" />\n'
            '    <rect x="-8" y="24" width="16" height="4" rx="2" fill="#37474F" />\n'
            '    <circle cx="0" cy="-6" r="8.5" fill="#212121" />\n'
            '    <circle cx="2.5" cy="-4" r="2.5" fill="#FFFFFF" opacity="0.85" />\n'
            "  </g>\n"
            '  <path d="M52 64 C50 68 54 70 56 68 C58 66 54 62 52 64 Z" '
            'fill="#FFFFFF" opacity="0.9" />\n'
            f"{_STATUS_BAR_RECT}"
            '  <text x="72" y="127" text-anchor="middle" fill="#FFFFFF" '
            'font-family="Segoe UI, sans-serif" font-weight="bold" font-size="13" '
            'letter-spacing="1">WAITING</text>\n'
            "</svg>"
        )

    # Default / Idle / Empty
    return (
        f"{_SVG_HEADER}"
        "  <defs>\n"
        '    <linearGradient id="bgIdle" x1="0%" y1="0%" x2="0%" y2="100%">\n'
        '      <stop offset="0%" stop-color="#37474F" />\n'
        '      <stop offset="100%" stop-color="#212121" />\n'
        "    </linearGradient>\n"
        "  </defs>\n"
        '  <rect x="0" y="0" width="144" height="144" rx="20" fill="url(#bgIdle)" />\n'
        '  <rect x="2" y="2" width="140" height="140" rx="18" fill="none" stroke="#FFFFFF" '
        'stroke-width="1.5" opacity="0.15" />\n'
        '  <text x="96" y="36" fill="#FFFFFF" font-family="Segoe UI, sans-serif" '
        'font-weight="bold" font-size="11" opacity="0.7">z</text>\n'
        '  <text x="104" y="26" fill="#FFFFFF" font-family="Segoe UI, sans-serif" '
        'font-weight="bold" font-size="13" opacity="0.85">Z</text>\n'
        '  <g opacity="0.4">\n'
        '    <line x1="36" y1="46" x2="36" y2="92" stroke="#FFFFFF" stroke-width="2.5" />\n'
        '    <line x1="36" y1="58" x2="48" y2="58" stroke="#FFFFFF" stroke-width="2" />\n'
        '    <line x1="36" y1="76" x2="48" y2="76" stroke="#FFFFFF" stroke-width="2" />\n'
        '    <rect x="30" y="90" width="84" height="6" rx="2" fill="#FFFFFF" />\n'
        "  </g>\n"
        '  <g transform="translate(72, 60)">\n'
        '    <path d="M-14 10 L-20 22 L-12 22 Z" fill="#FFFFFF" stroke="#FFFFFF" '
        'stroke-width="2" stroke-linejoin="round" />\n'
        '    <path d="M14 10 L20 22 L12 22 Z" fill="#FFFFFF" stroke="#FFFFFF" '
        'stroke-width="2" stroke-linejoin="round" />\n'
        '    <path d="M-14 4 C-14 -20 0 -34 0 -34 C0 -34 14 -20 14 4 L14 22 L-14 22 Z" '
        'fill="#FFFFFF" />\n'
        '    <rect x="-8" y="22" width="16" height="4" rx="2" fill="#263238" />\n'
        '    <circle cx="0" cy="-6" r="8.5" fill="#212121" />\n'
        '    <circle cx="-2.5" cy="-8.5" r="2.5" fill="#FFFFFF" opacity="0.85" />\n'
        "  </g>\n"
        f"{_STATUS_BAR_RECT}"
        '  <text x="72" y="127" text-anchor="middle" fill="#FFFFFF" '
        'font-family="Segoe UI, sans-serif" font-weight="bold" font-size="13" '
        'letter-spacing="1">READY</text>\n'
        "</svg>"
    )


def svg_to_data_uri(svg: str) -> str:
    """Convert an SVG string to a data URI."""
    b64 = base64.b64encode(svg.encode("utf-8")).decode("utf-8")
    return f"data:image/svg+xml;base64,{b64}"
