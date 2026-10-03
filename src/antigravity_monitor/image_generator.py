"""Dynamic key image generator for Stream Deck using SVG.

Generates SVG data URIs that can be directly passed to Stream Deck's setImage API,
without requiring external image processing libraries like Pillow.
"""

from __future__ import annotations

import html
import urllib.parse


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


def svg_to_data_uri(svg: str) -> str:
    """Convert SVG string to a data URI for Stream Deck setImage."""
    encoded = urllib.parse.quote(svg)
    return f"data:image/svg+xml;charset=utf8,{encoded}"
