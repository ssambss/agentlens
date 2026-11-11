"""Images taken off the clipboard for a session's next message: shrunk and encoded for Claude."""

import io
import os
import subprocess

import pytest
from PIL import Image, ImageGrab

from agentlens import clipboard
from agentlens.clipboard import MAX_BYTES, MAX_EDGE, NoImage, encode
from agentlens.clipboard import paste_image as real_paste_image  # conftest stands in for it in the app


def test_a_big_screenshot_is_shrunk_to_the_long_edge_claude_takes_in_many_image_requests():
    image = encode(Image.new("RGB", (5120, 2880), (30, 30, 40)))
    assert (image.width, image.height) == (MAX_EDGE, 1125) and image.media_type == "image/png"
    assert Image.open(io.BytesIO(image.data)).size == (MAX_EDGE, 1125)
    small = encode(Image.new("RGBA", (640, 480)))
    assert small.size == "640×480" and Image.open(io.BytesIO(small.data)).mode == "RGBA"  # never enlarged


def test_a_photo_too_big_as_png_goes_as_jpeg():
    noise = Image.frombytes("RGB", (MAX_EDGE, MAX_EDGE), os.urandom(MAX_EDGE * MAX_EDGE * 3))
    image = encode(noise)
    assert image.media_type == "image/jpeg" and len(image.data) <= MAX_BYTES
    assert Image.open(io.BytesIO(image.data)).format == "JPEG"


def test_palette_and_cmyk_images_are_converted():
    assert Image.open(io.BytesIO(encode(Image.new("P", (10, 10))).data)).mode == "RGB"
    assert Image.open(io.BytesIO(encode(Image.new("CMYK", (10, 10))).data)).mode == "RGB"


def test_paste_reads_an_image_or_the_first_image_file_copied(monkeypatch, tmp_path):
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: Image.new("RGB", (300, 200)))
    assert real_paste_image().size == "300×200"

    notes, shot = tmp_path / "notes.txt", tmp_path / "shot.png"
    notes.write_text("not an image")
    Image.new("RGB", (64, 32)).save(shot)
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: [str(notes), str(shot)])  # copied in Explorer
    assert real_paste_image().size == "64×32"


@pytest.mark.parametrize("grabbed, says", [
    (lambda: None, "no image on the clipboard"),
    (lambda: ["C:/notes.txt"], "no image on the clipboard"),
])
def test_paste_says_why_there_is_nothing(monkeypatch, grabbed, says):
    monkeypatch.setattr(ImageGrab, "grabclipboard", grabbed)
    with pytest.raises(NoImage, match=says):
        real_paste_image()


def test_in_linux_with_neither_wl_paste_nor_xclip_agentlens_reads_x_itself(monkeypatch):
    def missing():
        raise NotImplementedError("wl-paste or xclip is required")

    monkeypatch.setattr(ImageGrab, "grabclipboard", missing)
    monkeypatch.setattr(clipboard.x11, "xlib", lambda: object())
    shot = io.BytesIO()
    Image.new("RGB", (64, 32)).save(shot, "PNG")
    asked = []

    def run(cmd, **kwargs):
        asked.append(cmd[-3:])
        return subprocess.CompletedProcess(cmd, 0 if shot.getvalue() else 1, shot.getvalue(), b"")

    monkeypatch.setattr(clipboard.subprocess, "run", run)
    monkeypatch.setenv("DISPLAY", ":0")
    assert real_paste_image().size == "64×32" and asked == [["agentlens.x11", "paste", "--image"]]
    shot = io.BytesIO()  # nothing copied, or not an image
    with pytest.raises(NoImage, match="no image on the clipboard"):
        real_paste_image()
    monkeypatch.delenv("DISPLAY")  # no X to reach: say what it takes
    with pytest.raises(NoImage, match="wl-paste or xclip"):
        real_paste_image()


def test_too_big_even_as_jpeg_is_refused(monkeypatch):
    monkeypatch.setattr(clipboard, "MAX_BYTES", 100)
    noise = Image.frombytes("RGB", (200, 200), os.urandom(200 * 200 * 3))
    with pytest.raises(NoImage, match="too big"):
        encode(noise)
