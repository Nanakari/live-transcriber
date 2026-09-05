"""Generate an original neutral PNG cover with only the Python standard library."""
import struct
import zlib
from pathlib import Path


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def main():
    width, height = 640, 360
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            wave = 125 + ((x // 18 * 47) % 95)
            bar = 205 < x < 440 and x % 18 < 8 and abs(y - height // 2) < wave // 3
            pixels.extend((62, 151, 140) if bar else (24 + y // 24, 38 + y // 22, 48 + y // 20))
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(pixels))) + chunk(b"IEND", b"")
    target = Path(__file__).resolve().parents[1] / "app" / "assets" / "neutral_cover.png"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(data)


if __name__ == "__main__":
    main()
