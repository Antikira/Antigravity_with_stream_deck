"""Icon generator for Stream Deck plugin.

Generates 144x144 PNG icons for the Antigravity Monitor plugin.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def create_icon(
    filepath: Path,
    bg_color: str,
    symbol: str,
    label: str = "",
    text_color: str = "#FFFFFF",
) -> None:
    """Create a 144x144 icon PNG."""
    size = (144, 144)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Draw rounded rectangle background
    draw.rounded_rectangle([(4, 4), (140, 140)], radius=28, fill=bg_color)
    draw.rounded_rectangle([(4, 4), (140, 140)], radius=28, outline=(255, 255, 255, 40), width=2)

    # Text / Symbol
    try:
        font_large = ImageFont.truetype("arial.ttf", 36)
        font_small = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # Draw symbol
    bbox = draw.textbbox((0, 0), symbol, font=font_large)
    w = bbox[2] - bbox[0]
    y_sym = 45 if label else 55
    draw.text(((144 - w) / 2, y_sym), symbol, fill=text_color, font=font_large)

    if label:
        bbox_l = draw.textbbox((0, 0), label, font=font_small)
        w_l = bbox_l[2] - bbox_l[0]
        draw.text(((144 - w_l) / 2, 95), label, fill=text_color, font=font_small)

    filepath.parent.mkdir(parents=True, exist_ok=True)
    img.save(filepath, "PNG")
    print(f"Generated icon: {filepath}")


def generate_all_icons(target_dir: Path) -> None:
    """Generate all icons for Stream Deck plugin."""
    target_dir.mkdir(parents=True, exist_ok=True)

    create_icon(target_dir / "pluginIcon.png", "#1565C0", "AGY", "Antigravity")
    create_icon(target_dir / "sessionIcon.png", "#2E7D32", "S", "Session")
    create_icon(target_dir / "prevIcon.png", "#37474F", "<", "Prev")
    create_icon(target_dir / "nextIcon.png", "#37474F", ">", "Next")
    create_icon(target_dir / "quotaIcon.png", "#E65100", "%", "Quota")
    create_icon(target_dir / "hubIcon.png", "#1E88E5", "HUB", "Antigravity")
    create_icon(target_dir / "backIcon.png", "#455A64", "TOP", "Parent")


if __name__ == "__main__":
    base = Path(__file__).resolve().parent.parent.parent
    img_dir = base / "sdplugin" / "com.user.antigravity.sdPlugin" / "Images"
    generate_all_icons(img_dir)
