"""Coverage for differently coloured, oversized numeric lines and their boundaries."""

import pytest
from PIL import Image, ImageDraw, ImageFont

from ai_phone.agent.android_glyphs import _faces
from ai_phone.agent.android_numeric_text import read_large_numbers


def display(value, offset=0):
    faces = _faces()[1]
    if not faces:
        pytest.skip("no installed Latin face")
    path, index = faces[0]
    font = ImageFont.truetype(str(path), 125, index=index)
    frame = Image.new("RGB", (1060, 850), "#f2f2f5")
    draw = ImageDraw.Draw(frame)
    x, baseline = 62 + offset, 256
    for position, char in enumerate(value):
        colour = "#dadae0" if position < 2 else "#505665"
        draw.text((x, baseline), char, font=font, anchor="ls", fill=colour)
        x += font.getlength(char)
    return frame


@pytest.mark.parametrize("value", ["48:17.62", "53.87", "12:34:56"])
def test_reads_only_the_full_value_with_low_contrast_prefix(value):
    image = display(value, 57)
    rows = read_large_numbers(image, [])
    assert [text for text, _ in rows] == [value]
    assert read_large_numbers(image, [rows[0][1]]) == []


@pytest.mark.parametrize("value", ["GO:UP.AB", "AB:CD.EF", "781"])
def test_letters_and_unformatted_shapes_do_not_become_numbers(value):
    assert read_large_numbers(display(value), []) == []
