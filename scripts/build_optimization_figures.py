"""Build submission figures from saved measurements, without running benchmarks.

SVG output uses only the standard library. --png also needs Pillow, but does not
change the application dependencies. --verify-sources checks local source hashes.
"""

import argparse
import hashlib
import html
import json
import math
import statistics
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports" / "figures"
COLORS = ("#94a3b8", "#00838d")
TEXT = "#17243a"
MUTED = "#56657a"


class Canvas:
    def __init__(self, width, height, png=False):
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" role="img">'
        ]
        self.image = self.draw = None
        if png:
            from PIL import Image, ImageDraw

            self.image = Image.new("RGB", (width, height), "white")
            self.draw = ImageDraw.Draw(self.image)
        self.rect(0, 0, width, height, "white")

    def rect(self, x, y, width, height, color):
        self.parts.append(
            f'<rect x="{x:g}" y="{y:g}" width="{width:g}" '
            f'height="{height:g}" fill="{color}"/>'
        )
        if self.draw and width:
            self.draw.rectangle((x, y, x + width, y + height), fill=color)

    def line(self, x1, y1, x2, y2, color):
        self.parts.append(
            f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" '
            f'y2="{y2:g}" stroke="{color}"/>'
        )
        if self.draw:
            self.draw.line((x1, y1, x2, y2), fill=color)

    def text(self, x, y, value, size=15, color=TEXT, bold=False):
        self.parts.append(
            f'<text x="{x:g}" y="{y:g}" font-family="Arial, sans-serif" '
            f'font-size="{size}" fill="{color}" '
            f'font-weight="{700 if bold else 400}">{html.escape(str(value))}</text>'
        )
        if self.draw:
            from PIL import ImageFont

            names = (
                "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
                if bold
                else "/System/Library/Fonts/Supplemental/Arial.ttf",
            )
            for name in names:
                try:
                    font = ImageFont.truetype(name, size)
                    break
                except OSError:
                    continue
            else:
                font = ImageFont.load_default(size=size)
            self.draw.text((x, y), str(value), fill=color, font=font, anchor="ls")

    def save(self, name):
        (OUTPUT / f"{name}.svg").write_text("\n".join(self.parts + ["</svg>"]) + "\n")
        if self.image:
            self.image.save(OUTPUT / f"{name}.png")


def number(value):
    return f"{value:,.3f}".rstrip("0").rstrip(".")


def axis_limit(value):
    magnitude = 10 ** math.floor(math.log10(max(value, 0.001)))
    return next(
        step * magnitude
        for step in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 20)
        if step * magnitude >= value
    )


def render(figure, png):
    panels = figure["panels"]
    columns = min(2, len(panels))
    width = 1280
    panel_width = (width - 60) / columns
    group_height = 30 + 24 * max(len(panel["legend"]) for panel in panels)
    panel_height = 160 + group_height * max(len(panel["rows"]) for panel in panels)
    height = 118 + math.ceil(len(panels) / columns) * panel_height
    canvas = Canvas(width, height, png)
    canvas.parts.append(f'<title>{html.escape(figure["title"])}</title>')
    canvas.parts.append(f'<desc>{html.escape(figure["scope"])}</desc>')
    canvas.text(30, 38, figure["title"], 24, bold=True)
    for index, line in enumerate(textwrap.wrap(figure["scope"], 150)):
        canvas.text(30, 65 + index * 19, line, 14, MUTED)
    for index, panel in enumerate(panels):
        left = 30 + (index % columns) * panel_width
        top = 113 + (index // columns) * panel_height
        canvas.text(left, top, panel["title"], 18, bold=True)
        canvas.text(left, top + 22, panel["unit"], 13, MUTED)
        colors = (
            COLORS if len(panel["legend"]) == 2 else (COLORS[0], "#4e69a6", COLORS[1])
        )
        for item, label in enumerate(panel["legend"]):
            canvas.rect(
                left + item * panel_width / len(colors), top + 36, 10, 10, colors[item]
            )
            canvas.text(
                left + 16 + item * panel_width / len(colors), top + 46, label, 12
            )
        maximum = axis_limit(
            max(max(row["values"]) for row in panel["rows"])
            * 1.05
        )
        start = left + (270 if columns == 1 else 180)
        plot_width = panel_width - (350 if columns == 1 else 250)
        bottom = top + 65 + len(panel["rows"]) * group_height
        for tick in range(5):
            x = start + tick * plot_width / 4
            canvas.line(x, top + 65, x, bottom, "#e1e7ef")
            canvas.text(x - 6, bottom + 20, number(maximum * tick / 4), 12, MUTED)
        for item, row in enumerate(panel["rows"]):
            y = top + 76 + item * group_height
            for line, label in enumerate(
                textwrap.wrap(row["label"], 26 if columns == 1 else 21)
            ):
                canvas.text(left, y + line * 17, label, 14)
            for series, value in enumerate(row["values"]):
                bar_y = y - 12 + series * 24
                bar_width = value / maximum * plot_width
                color = colors[series]
                if series == len(colors) - 1 and value > row["values"][0] * 1.01:
                    color = "#bb5b34"
                canvas.rect(start, bar_y, bar_width, 17, color)
                label_x = start + bar_width + 7
                canvas.text(label_x, bar_y + 14, number(value), 13)
        for line, note in enumerate(
            textwrap.wrap(panel["note"], 80 if columns == 2 else 155)
        ):
            canvas.text(left, bottom + 47 + line * 18, note, 12, MUTED)
    canvas.text(
        30,
        height - 12,
        "Brown bars mark increases in lower-is-better metrics.",
        12,
        MUTED,
    )
    canvas.save(figure["id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--png", action="store_true")
    parser.add_argument("--verify-sources", action="store_true")
    args = parser.parse_args()
    data = json.loads((OUTPUT / "data.json").read_text())
    if args.verify_sources:
        for source in data["sources"]:
            content = (ROOT / source["path"]).read_bytes()
            assert len(content) == source["size_bytes"], source["path"]
            assert hashlib.sha256(content).hexdigest() == source["sha256"], source[
                "path"
            ]
    for figure in data["figures"]:
        for panel in figure["panels"]:
            for row in panel["rows"]:
                assert len(row["values"]) == len(row["samples"]) == len(panel["legend"])
                for value, samples in zip(row["values"], row["samples"], strict=True):
                    assert math.isfinite(value) and value >= 0
                    if samples:
                        assert math.isclose(
                            value, statistics.median(samples), rel_tol=1e-9
                        )
        render(figure, args.png)
    print(
        f"Built {len(data['figures'])} figures from saved reports; no benchmarks run."
    )


if __name__ == "__main__":
    main()
