"""Geometry-only badge placement; never change candidate IDs or click bounds."""


def intersects(a, b, gap=3):
    return (a[0] < b[2] + gap and a[2] + gap > b[0]
            and a[1] < b[3] + gap and a[3] + gap > b[1])


def place_labels(candidates, sizes, image_size):
    width, height = image_size
    occupied = []
    placements = {}
    # Small target regions retain their preferred positions before containers.
    ordered = sorted(candidates, key=lambda n: (
        (n['bounds'][2] - n['bounds'][0]) * (n['bounds'][3] - n['bounds'][1]), n['id'],
    ))
    for node in ordered:
        x0, y0, x1, y1 = node['bounds']
        bw, bh = sizes[node['id']]
        bw, bh = min(bw, width), min(bh, height)
        choices = [(x0, y0), (x0, y0 - bh - 4), (x1 - bw, y0),
                   (x0, y1 - bh), (x1 - bw, y1 - bh)]
        for distance in range(1, 9):
            for dx, dy in [(0, -1), (1, 0), (0, 1), (-1, 0), (1, -1), (-1, -1)]:
                choices.append((x0 + dx * distance * (bw + 4), y0 + dy * distance * (bh + 4)))
        selected = None
        for x, y in choices:
            x, y = max(0, min(x, width - bw)), max(0, min(y, height - bh))
            rect = (x, y, x + bw, y + bh)
            if not any(intersects(rect, old) for old in occupied):
                selected = rect
                break
        if selected is None:
            # Bounded exhaustive grid fallback rather than painting over a badge.
            for y in range(0, max(1, height - bh + 1), bh + 4):
                for x in range(0, max(1, width - bw + 1), bw + 4):
                    rect = (x, y, x + bw, y + bh)
                    if not any(intersects(rect, old) for old in occupied):
                        selected = rect
                        break
                if selected is not None:
                    break
        if selected is None:
            raise ValueError('Too many candidate labels to display without overlap')
        occupied.append(selected)
        placements[node['id']] = selected
    return placements
