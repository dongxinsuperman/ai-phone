"""Display-only boxes and labels. Candidate bounds and click centers never change."""

import io
from PIL import Image, ImageDraw, ImageFont


def annotate_points(frame):
    ns = frame.candidates
    im = Image.open(io.BytesIO(frame.image)).convert("RGB")
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=19)

    def overlap(a, b):
        return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])

    boxes = {}
    points = {}
    scales = {}
    for n in ns:
        x, y, r, s = n["bounds"]
        cx, cy = (x + r) // 2, (y + s) // 2
        points[n["id"]] = (cx, cy)
        scale = 0.45 if any(m is not n and overlap(n["bounds"], m["bounds"]) for m in ns) else 1.0
        scales[n["id"]] = scale
        hw, hh = (r - x) * scale / 2, (s - y) * scale / 2
        boxes[n["id"]] = (round(cx - hw), round(cy - hh), round(cx + hw), round(cy + hh))
    labels = {}
    occupied = []
    for n in sorted(ns, key=lambda n: (n["bounds"][2] - n["bounds"][0]) * (n["bounds"][3] - n["bounds"][1])):
        key = n["id"]
        cx, cy = points[key]
        bb = d.textbbox((0, 0), key, font=font)
        w, h = bb[2] - bb[0] + 4, bb[3] - bb[1] + 4
        x, y, r, s = boxes[key]
        options = []
        # Place near the click point; crossing another frame is preferable to a long leader.
        for ring in range(1, 100):
            radius = 8 + (ring - 1) * 8
            for dx, dy in [(1, -1), (-1, -1), (1, 1), (-1, 1), (0, -1), (0, 1), (1, 0), (-1, 0)]:
                lx = cx + dx * radius - (w if dx < 0 else w / 2 if dx == 0 else 0)
                ly = cy + dy * radius - (h if dy < 0 else h / 2 if dy == 0 else 0)
                lx = max(0, min(round(lx), im.width - w))
                ly = max(0, min(round(ly), im.height - h))
                rect = (lx, ly, lx + w, ly + h)
                if (boxes[key][2] - boxes[key][0]) * (
                    boxes[key][3] - boxes[key][1]
                ) < im.width * im.height * 0.15 and overlap(rect, boxes[key]):
                    continue
                if any(overlap(rect, (a - 3, b - 3, c + 3, t + 3)) for a, b, c, t in occupied):
                    continue
                if any(
                    lx - 4 <= px <= lx + w + 4 and ly - 4 <= py <= ly + h + 4 for px, py in points.values()
                ):
                    continue
                options.append(rect)
            if options:
                break
        if not options:
            raise ValueError("No collision-free point label position")
        rect = min(options, key=lambda v: ((v[0] + v[2]) / 2 - cx) ** 2 + ((v[1] + v[3]) / 2 - cy) ** 2)
        labels[key] = rect
        occupied.append(rect)
    for key, rect in boxes.items():
        d.rectangle(rect, outline="#dc2020", width=2)
    for key, (x, y, r, s) in labels.items():
        cx, cy = points[key]
        d.line((cx, cy, max(x, min(cx, r)), max(y, min(cy, s))), fill="#dc2020", width=1)
    for key, (cx, cy) in points.items():
        d.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill="#dc2020")
    for key, (x, y, r, s) in labels.items():
        d.rectangle((x, y, r, s), fill="white")
        bb = d.textbbox((0, 0), key, font=font)
        d.text((x + 2 - bb[0], y + 2 - bb[1]), key, font=font, fill="#b00000")
    out = io.BytesIO()
    im.resize((max(1, round(im.width * 0.8)), max(1, round(im.height * 0.8))), Image.Resampling.LANCZOS).save(
        out, format="JPEG", quality=85
    )
    return out.getvalue()
