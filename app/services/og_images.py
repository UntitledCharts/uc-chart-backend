"""Link preview (Open Graph) images drawn with Pillow, matching the old satori templates."""

from __future__ import annotations

import math
import re
import struct
import threading
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

__all__ = ["render_generic_og", "render_level_og", "render_user_og"]

_Color = tuple[int, int, int]

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "og"
_WIDTH = 1200
_HEIGHT = 630
_SUPERSAMPLE = 4
_GLYPH_STEPS = 4
_GLYPH_CACHE_LIMIT = 4096
_PNG_COMPRESS_LEVEL = 1
_PNG_RLE_STRATEGY = 3

_BACKGROUND: _Color = (2, 6, 23)
_WHITE: _Color = (255, 255, 255)
_BLACK: _Color = (0, 0, 0)
_SLATE_300: _Color = (203, 213, 225)
_SLATE_400: _Color = (148, 163, 184)
_SLATE_700: _Color = (51, 65, 85)
_SKY_400: _Color = (56, 189, 248)
_RED_300: _Color = (252, 165, 165)
_AMBER_400: _Color = (251, 191, 36)
_VIOLET_300: _Color = (196, 181, 253)
_VIOLET_400: _Color = (167, 139, 250)
_VIOLET_500: _Color = (139, 92, 246)
_SUBTITLE: _Color = (165, 184, 207)

_DEFAULT_SUBTITLE = "A community catalog of Sonolus rhythm-game charts"

_EMOJI_NAMES = frozenset(
    {
        "mikuslain",
        "yaminabe",
        "os_uweh",
        "silly_3d_printed_cat",
        "bugcat_swim",
        "ilovesharks",
    }
)

_EMOJI_TOKEN = re.compile(r":([a-zA-Z0-9_]+):")
_EMOJI_SPLIT = re.compile(r"(:[a-zA-Z0-9_]+:)")
_EMOJI_WORD = re.compile(r":([a-zA-Z0-9_]+):")
_JS_WHITESPACE = re.compile(
    "[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)

_FONT_FILES = {
    "inter-400": "Inter-Regular.ttf",
    "inter-700": "Inter-Bold.ttf",
    "unbounded-700": "Unbounded-Bold.ttf",
    "noto-400": "NotoSansJP-Regular.otf",
    "noto-700": "NotoSansJP-Bold.otf",
}

_LB_NS = 5
_LB_PR = 9
_LB_AL = 12
_LB_HL = 13
_LB_ID = 14
_LB_HY = 16
_LB_BA = 17
_LB_CM = 21
_LB_WJ = 22
_LB_H2 = 23
_LB_H3 = 24
_LB_JL = 25
_LB_JV = 26
_LB_JT = 27
_LB_RI = 28
_LB_ZWJ = 31
_LB_BK = 34
_LB_CR = 36
_LB_LF = 37
_LB_NL = 38
_LB_SP = 41

_LB_DIRECT = "0"
_LB_INDIRECT = "1"
_LB_COMBINING_INDIRECT = "2"
_LB_COMBINING_PROHIBITED = "3"

_LB_PAIRS = (
    "444444444444444444444344444444444",
    "044114444110000111004240000000010",
    "044114444111110111004240000000010",
    "444111444111111111114241111111111",
    "144111444111111111114241111111111",
    "044111444000000111004240000000010",
    "044111444000000111004240000000010",
    "044111444001010111004240000000010",
    "044111444001110111004240000000010",
    "144111444001111111004241111101110",
    "144111444001110111004240000000010",
    "144111444111110111004240000000010",
    "144111444111110111004240000000010",
    "144111444111110111004240000000010",
    "044111444010000111004240000000010",
    "044111444000000111004240000000010",
    "044101444001000111004240000000010",
    "044101444000000111004240000000010",
    "144111444111111111114241111111110",
    "044111444000000111044240000000010",
    "000000000000000000004000000000000",
    "144111444111110111004240000000010",
    "144111444111111111114241111111111",
    "044111444010000111004240001100010",
    "044111444010000111004240000100010",
    "044111444010000111004241111000010",
    "044111444010000111004240001100010",
    "044111444010000111004240000100010",
    "044111444000000111004240000010010",
    "044111444010000111004240000000110",
    "044111444010000111004240000000010",
    "144111444111110111004240000000010",
    "044110444000000000004240000000010",
)

_LB_BLOCKS = (
    (
        0x0000,
        "EEEEEEEEEAURRTEEEEEEEEEEEEEEEEEEY63<9:<302<98@87;;;;;;;;;;88<<<6<<<<<<<<<<<<<<<<<<<<<<<<"
        "<<<092<<<<<<<<<<<<<<<<<<<<<<<<<<<<<0A1<EEEEEEVEEEEEEEEEEEEEEEEEEEEEEEEEE40:999<<<<<3<A<<"
        ":9<<B<<<<<<3<<<0<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<",
    ),
    (
        0x2000,
        "AAAAAAA4AAADEOEEA4AAC<<<33033303<<<<???ARREEEEE4::::::::<33<55<<<<<<801555<<<<<<<<<<<<A<"
        "AAAA<AAAF<<<<<EEEEEEEEEE",
    ),
    (
        0x3000,
        "A11>>5>>0101010101>>010101015011>>>>>>>>>>EEEEEE>>>>>E>>>>>55>>><5>5>5>5>5>>>>>>>>>>>>>>"
        ">>>>>>>>>>>5>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>5>5>5>>>>>>5>>>>>>55<<EE5555>55>5>5>5>5>>>>>>"
        ">>>>>>>>>>>>>>>>>>>5>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>5>5>5>>>>>>5>>>>>>55>>>>5555>",
    ),
    (
        0xFE30,
        ">>>>>0101010101010101>>01>>>>>>>1>1<5566>010101>>>>>>>>>>9:><<<<",
    ),
    (
        0xFF00,
        "<6>>9:>>01>>1>1>>>>>>>>>>>55>>>6>>>>>>>>>>>>>>>>>>>>>>>>>>>0>1>>>>>>>>>>>>>>>>>>>>>>>>>>"
        ">>>0>1>0110115>5555555555>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>55>>>>>>>>>>>>>>>>"
        ">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>><<<:9>>>99<<<<<<<<<",
    ),
)

_LB_RANGES = (
    (0x05D0, 0x05F2, _LB_HL),
    (0x1100, 0x115F, _LB_JL),
    (0x1160, 0x11A7, _LB_JV),
    (0x11A8, 0x11FF, _LB_JT),
    (0x20A0, 0x20CF, _LB_PR),
    (0x2116, 0x2116, _LB_PR),
    (0x2E80, 0x2FFB, _LB_ID),
    (0x3100, 0x31EF, _LB_ID),
    (0x31F0, 0x31FF, _LB_NS),
    (0x3200, 0x4DBF, _LB_ID),
    (0x4E00, 0x9FFF, _LB_ID),
    (0xA000, 0xA4CF, _LB_ID),
    (0xF900, 0xFAFF, _LB_ID),
    (0x1F000, 0x1FAFF, _LB_ID),
    (0x20000, 0x3FFFD, _LB_ID),
)


@dataclass(frozen=True)
class _Face:
    """Metrics of one bundled font file, read the way satori's opentype.js reads them.

    Attributes:
        key: Name of the font in the bundled font table.
        path: Location of the font file.
        units_per_em: Design units per em.
        ascender: Ascender from the hhea table in design units.
        descender: Descender from the hhea table in design units.
        cmap: Glyph id of each supported code point.
        advances: Horizontal advance of each glyph id in design units.
    """

    key: str
    path: str
    units_per_em: int
    ascender: int
    descender: int
    cmap: dict[int, int]
    advances: tuple[int, ...]

    def has(self, code_point: int) -> bool:
        """Tell whether the font has a glyph for a code point.

        Args:
            code_point: Unicode code point.

        Returns:
            bool: True when the font maps the code point to a glyph.
        """
        return code_point in self.cmap

    def advance(self, code_point: int, size: float) -> float:
        """Measure the unhinted advance of a code point.

        Args:
            code_point: Unicode code point.
            size: Font size in pixels.

        Returns:
            float: Advance in pixels.
        """
        glyph = min(self.cmap.get(code_point, 0), len(self.advances) - 1)
        return self.advances[glyph] * size / self.units_per_em


@dataclass(frozen=True)
class _Style:
    """Text styling of one satori text node.

    Attributes:
        size: Font size in pixels.
        color: Fill color.
        bold: Whether the bold faces are used (CSS weights above 500).
        line_height: Line height multiplier, or None for the font's normal line height.
        display: Whether Unbounded is the primary family instead of Inter.
        letter_spacing: Extra advance after every character in pixels.
    """

    size: float
    color: _Color = _WHITE
    bold: bool = False
    line_height: float | None = None
    display: bool = False
    letter_spacing: float = 0.0


@dataclass(frozen=True)
class _Word:
    """A line-break segment measured the way satori measures it.

    Attributes:
        glyphs: Each character with the face that draws it and its advance.
        width: Sum of advances including trailing spaces.
        trailing: Advance of the trailing spaces.
        height: Rounded line height the segment asks for.
        baseline: Rounded baseline offset the segment asks for.
    """

    glyphs: tuple[tuple[str, _Face, float], ...]
    width: float
    trailing: float
    height: int
    baseline: int


@dataclass(frozen=True)
class _Line:
    """One line of a laid-out text node.

    Attributes:
        words: Segments placed on the line.
        height: Line box height.
        baseline: Baseline offset from the top of the line box.
    """

    words: tuple[_Word, ...]
    height: int
    baseline: int


@dataclass(frozen=True)
class _Text:
    """A text node laid out with satori's line breaking.

    Attributes:
        style: Text style.
        lines: Laid-out lines.
        width: Width of the text node.
        height: Height of the text node.
    """

    style: _Style
    lines: tuple[_Line, ...]
    width: int
    height: int


@dataclass(frozen=True)
class _Piece:
    """A positioned part of a paragraph, either a text node or an inline emoji.

    Attributes:
        x: Offset from the paragraph's left edge.
        y: Offset from the paragraph's top edge.
        text: Text node, or None for an emoji.
        emoji: Emoji name, or None for a text node.
        size: Edge length of the emoji box.
    """

    x: int
    y: int
    text: _Text | None = None
    emoji: str | None = None
    size: int = 0


@dataclass(frozen=True)
class _Paragraph:
    """Text laid out like satori lays out a plain or emoji-bearing text box.

    Attributes:
        pieces: Positioned text nodes and emojis.
        width: Width of the content.
        height: Height of the content.
    """

    pieces: tuple[_Piece, ...]
    width: int
    height: int


@dataclass(frozen=True)
class _Card:
    """Precomputed masks of a rounded, bordered, shadowed and possibly rotated image box.

    Attributes:
        side: Edge length of the box.
        border: Border width.
        frame: Edge length of the square frame the image and border masks are drawn in.
        inner: Coverage of the visible image area within the frame.
        border_mask: Border coverage with the border alpha applied, within the frame.
        shadow_frame: Edge length of the square frame the shadow mask is drawn in.
        shadow: Shadow coverage with the shadow alpha applied, within the shadow frame.
        angle: Clockwise rotation in degrees.
    """

    side: int
    border: int
    frame: int
    inner: Image.Image
    border_mask: Image.Image
    shadow_frame: int
    shadow: Image.Image
    angle: float


@dataclass(frozen=True)
class _Fan:
    """Placement of the fanned-out jacket cards.

    Attributes:
        left: Left edge of the fan container.
        top: Top edge of the fan container.
        cards: Left offset, top offset and clockwise rotation of each card slot.
        radius: Corner radius of a card.
        border_alpha: Opacity of the white card border.
    """

    left: int
    top: int
    cards: tuple[tuple[int, int, float], ...]
    radius: int
    border_alpha: float


_GENERIC_FAN = _Fan(710, 150, ((0, 70, -12.0), (170, 70, 12.0), (85, 0, 0.0)), 30, 0.16)
_USER_FAN = _Fan(736, 150, ((0, 56, -11.0), (150, 56, 11.0), (75, 0, 0.0)), 26, 0.18)

_thread_state = threading.local()


def _js_round(value: float) -> int:
    """Round half up like JavaScript's Math.round and yoga's pixel grid rounding.

    Args:
        value: Number to round.

    Returns:
        int: Rounded number.
    """
    return math.floor(value + 0.5)


def _js_number(value: float) -> str:
    """Format a number like JavaScript's default number to string conversion.

    Args:
        value: Number to format.

    Returns:
        str: Formatted number.
    """
    if isinstance(value, int) or float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _js_fixed(value: float, digits: int) -> str:
    """Format a number like JavaScript's Number.prototype.toFixed.

    Args:
        value: Number to format.
        digits: Digits after the decimal point.

    Returns:
        str: Formatted number.
    """
    return str(Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def _compact_number(value: int) -> str:
    """Abbreviate a count with k and m suffixes like the frontend does.

    Args:
        value: Count to abbreviate.

    Returns:
        str: Abbreviated count.
    """
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        return _js_fixed(value / 1000, 1 if value < 10_000 else 0).removesuffix(".0") + "k"
    return _js_fixed(value / 1_000_000, 1).removesuffix(".0") + "m"


def _utf16_length(text: str) -> int:
    """Count UTF-16 code units, which is what JavaScript string lengths count.

    Args:
        text: Text to measure.

    Returns:
        int: Number of UTF-16 code units.
    """
    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)


def _utf16_prefix(text: str, units: int) -> str:
    """Take the longest prefix that fits in a number of UTF-16 code units.

    Args:
        text: Text to cut.
        units: Maximum number of UTF-16 code units.

    Returns:
        str: The prefix.
    """
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > units:
            return text[:index]
    return text


def _og_text(value: str | None, limit: int) -> str:
    """Normalize and shorten text the way the frontend's ogText helper does.

    Args:
        value: Raw text.
        limit: Maximum length, counting a known emoji code as two characters.

    Returns:
        str: Text with whitespace collapsed, unknown emoji codes dropped and an ellipsis when cut.
    """
    cleaned = _EMOJI_TOKEN.sub(
        lambda match: f" {match.group(0)} " if match.group(1) in _EMOJI_NAMES else "",
        value or "",
    )
    cleaned = _JS_WHITESPACE.sub(" ", cleaned).strip(" ")
    out = ""
    used = 0
    for part in _EMOJI_SPLIT.split(cleaned):
        if not part:
            continue
        is_emoji = _EMOJI_WORD.fullmatch(part) is not None
        cost = 2 if is_emoji else _utf16_length(part)
        if used + cost > limit - 1:
            if not is_emoji:
                out += _utf16_prefix(part, max(0, limit - 1 - used))
            return out.rstrip() + "\u2026"
        out += part
        used += cost
    return out


def _line_break_class(code_point: int) -> int:
    """Look up the UAX #14 line breaking class the linebreak package would resolve.

    Args:
        code_point: Unicode code point.

    Returns:
        int: Line breaking class index.
    """
    for start, classes in _LB_BLOCKS:
        if start <= code_point < start + len(classes):
            return ord(classes[code_point - start]) - 48
    for low, high, line_class in _LB_RANGES:
        if low <= code_point <= high:
            return line_class
    if 0xAC00 <= code_point <= 0xD7A3:
        return _LB_H2 if (code_point - 0xAC00) % 28 == 0 else _LB_H3
    if unicodedata.category(chr(code_point)) in ("Mn", "Mc", "Me"):
        return _LB_CM
    return _LB_AL


def _first_line_break_class(line_class: int) -> int:
    """Map the class of a line's first character like the linebreak package does.

    Args:
        line_class: Line breaking class index.

    Returns:
        int: Class index used as the starting state.
    """
    if line_class in (_LB_LF, _LB_NL):
        return _LB_BK
    if line_class == _LB_SP:
        return _LB_WJ
    return line_class


def _break_segments(text: str) -> list[str]:
    """Split text at line break opportunities like satori's linebreak dependency does.

    Args:
        text: Text to split.

    Returns:
        list[str]: Segments, each keeping its trailing spaces.
    """
    if not text:
        return []
    classes = [_line_break_class(ord(char)) for char in text]
    segments: list[str] = []
    start = 0
    following = classes[0]
    current = _first_line_break_class(following)
    joiner = following == _LB_ZWJ
    after_hebrew = False
    regional = 0
    for position in range(1, len(text)):
        previous = following
        following = classes[position]
        if current == _LB_BK or (current == _LB_CR and following != _LB_LF):
            current = _first_line_break_class(following)
            segments.append(text[start:position])
            start = position
            continue
        should_break = False
        if following in (_LB_BK, _LB_LF, _LB_NL):
            current = _LB_BK
        elif following == _LB_CR:
            current = _LB_CR
        elif following != _LB_SP:
            rule = _LB_PAIRS[current][following]
            held = previous != _LB_SP and rule in (
                _LB_COMBINING_INDIRECT,
                _LB_COMBINING_PROHIBITED,
            )
            if not held:
                should_break = rule == _LB_DIRECT or (
                    previous == _LB_SP and rule in (_LB_INDIRECT, _LB_COMBINING_INDIRECT)
                )
                if joiner:
                    should_break = False
                if after_hebrew and current in (_LB_HY, _LB_BA):
                    should_break = False
                    after_hebrew = False
                else:
                    after_hebrew = current == _LB_HL
                if current == _LB_RI:
                    regional += 1
                    if regional == 2 and following == _LB_RI:
                        should_break = True
                        regional = 0
                else:
                    regional = 0
                current = following
        joiner = following == _LB_ZWJ
        if should_break:
            segments.append(text[start:position])
            start = position
    segments.append(text[start:])
    return segments


def _parse_cmap(data: bytes, table: int) -> dict[int, int]:
    """Read the best Unicode subtable of a font's cmap table.

    Args:
        data: Font file contents.
        table: Offset of the cmap table.

    Returns:
        dict[int, int]: Glyph id of each mapped code point.
    """
    count = struct.unpack_from(">H", data, table + 2)[0]
    subtables: dict[tuple[int, int], int] = {}
    for index in range(count):
        platform, encoding, offset = struct.unpack_from(">HHI", data, table + 4 + index * 8)
        subtables[(platform, encoding)] = table + offset
    mapping: dict[int, int] = {}
    for key in ((3, 10), (0, 6), (0, 4)):
        offset = subtables.get(key)
        if offset is not None and struct.unpack_from(">H", data, offset)[0] == 12:
            groups = struct.unpack_from(">I", data, offset + 12)[0]
            for group in range(groups):
                first, last, glyph = struct.unpack_from(">III", data, offset + 16 + group * 12)
                for code_point in range(first, last + 1):
                    mapping[code_point] = glyph + code_point - first
            return mapping
    for key in ((3, 1), (0, 3)):
        offset = subtables.get(key)
        if offset is None or struct.unpack_from(">H", data, offset)[0] != 4:
            continue
        segments = struct.unpack_from(">H", data, offset + 6)[0] // 2
        ends = offset + 14
        starts = ends + segments * 2 + 2
        deltas = starts + segments * 2
        range_offsets = deltas + segments * 2
        for segment in range(segments):
            last = struct.unpack_from(">H", data, ends + segment * 2)[0]
            first = struct.unpack_from(">H", data, starts + segment * 2)[0]
            delta = struct.unpack_from(">h", data, deltas + segment * 2)[0]
            range_offset = struct.unpack_from(">H", data, range_offsets + segment * 2)[0]
            for code_point in range(first, min(last, 0xFFFE) + 1):
                if range_offset == 0:
                    glyph = (code_point + delta) & 0xFFFF
                else:
                    address = range_offsets + segment * 2 + range_offset + (code_point - first) * 2
                    glyph = struct.unpack_from(">H", data, address)[0]
                    if glyph:
                        glyph = (glyph + delta) & 0xFFFF
                if glyph:
                    mapping[code_point] = glyph
        return mapping
    return mapping


@lru_cache(maxsize=None)
def _face(key: str) -> _Face:
    """Load the metrics of a bundled font once.

    Args:
        key: Name of the font in the bundled font table.

    Returns:
        _Face: Font metrics.
    """
    path = _ASSETS / "fonts" / _FONT_FILES[key]
    data = path.read_bytes()
    tables: dict[bytes, int] = {}
    for index in range(struct.unpack_from(">H", data, 4)[0]):
        tag, _, offset, _ = struct.unpack_from(">4sIII", data, 12 + index * 16)
        tables[tag] = offset
    units_per_em = struct.unpack_from(">H", data, tables[b"head"] + 18)[0]
    ascender, descender = struct.unpack_from(">hh", data, tables[b"hhea"] + 4)
    metric_count = struct.unpack_from(">H", data, tables[b"hhea"] + 34)[0]
    advances = struct.unpack_from(">" + "Hh" * metric_count, data, tables[b"hmtx"])[::2]
    cmap = _parse_cmap(data, tables[b"cmap"])
    cmap = {code_point: glyph for code_point, glyph in cmap.items() if glyph}
    return _Face(key, str(path), units_per_em, ascender, descender, cmap, advances)


@lru_cache(maxsize=None)
def _chain(display: bool, bold: bool) -> tuple[_Face, ...]:
    """Order the bundled fonts the way satori falls back between them.

    Args:
        display: Whether Unbounded is the primary family.
        bold: Whether the bold faces are used.

    Returns:
        tuple[_Face, ...]: Faces in fallback order.
    """
    weight = "700" if bold else "400"
    inter = _face(f"inter-{weight}")
    noto = _face(f"noto-{weight}")
    unbounded = _face("unbounded-700")
    return (unbounded, inter, noto) if display else (inter, noto, unbounded)


def _resolve(chain: tuple[_Face, ...], code_point: int) -> _Face:
    """Pick the first face in a fallback chain that has a glyph for a code point.

    Args:
        chain: Faces in fallback order.
        code_point: Unicode code point.

    Returns:
        _Face: Face that draws the code point.
    """
    for face in chain:
        if face.has(code_point):
            return face
    return chain[0]


def _pil_font(face: _Face, size: float) -> ImageFont.FreeTypeFont:
    """Get this thread's Pillow font object for a face and size.

    Args:
        face: Font metrics.
        size: Font size in pixels.

    Returns:
        ImageFont.FreeTypeFont: Font object owned by the calling thread.
    """
    fonts: dict[tuple[str, float], ImageFont.FreeTypeFont] | None = getattr(
        _thread_state, "fonts", None
    )
    if fonts is None:
        fonts = {}
        _thread_state.fonts = fonts
    font = fonts.get((face.key, size))
    if font is None:
        font = ImageFont.truetype(face.path, size, layout_engine=ImageFont.Layout.BASIC)
        fonts[(face.key, size)] = font
    return font


def _glyph(face: _Face, size: float, char: str, step: int) -> tuple[Image.Image, int, int] | None:
    """Rasterize one glyph at a sub-pixel offset, cached per thread.

    Args:
        face: Face that draws the glyph.
        size: Font size in pixels.
        char: Character to draw.
        step: Horizontal offset in steps of 1 / _GLYPH_STEPS pixels.

    Returns:
        tuple[Image.Image, int, int] | None: Coverage mask and its offset from the pen position
        on the baseline, or None for an empty glyph.
    """
    glyphs: dict[tuple[str, float, str, int], tuple[Image.Image, int, int] | None] | None = getattr(
        _thread_state, "glyphs", None
    )
    if glyphs is None or len(glyphs) >= _GLYPH_CACHE_LIMIT:
        glyphs = {}
        _thread_state.glyphs = glyphs
    key = (face.key, size, char, step)
    if key in glyphs:
        return glyphs[key]
    font = _pil_font(face, size)
    box = font.getbbox(char, anchor="ls")
    left, top = math.floor(box[0]), math.floor(box[1])
    right, bottom = math.ceil(box[2]), math.ceil(box[3])
    glyph: tuple[Image.Image, int, int] | None = None
    if right > left and bottom > top:
        mask = Image.new("L", (right - left + 2, bottom - top + 2), 0)
        ImageDraw.Draw(mask).text(
            (1 - left + step / _GLYPH_STEPS, 1 - top),
            char,
            fill=255,
            font=font,
            anchor="ls",
        )
        glyph = (mask, left - 1, top - 1)
    glyphs[key] = glyph
    return glyph


def _vertical_metrics(face: _Face, style: _Style) -> tuple[float, float]:
    """Compute satori's line height and baseline for text starting with a face.

    Args:
        face: Face of the first character.
        style: Text style.

    Returns:
        tuple[float, float]: Line height and baseline offset.
    """
    ascender = face.ascender / face.units_per_em * style.size
    content = (face.ascender - face.descender) / face.units_per_em * style.size
    height = content if style.line_height is None else style.size * style.line_height
    return height, ascender + (height - content) / 2


def _measure_word(text: str, style: _Style) -> _Word:
    """Measure one line-break segment.

    Args:
        text: Segment text.
        style: Text style.

    Returns:
        _Word: Measured segment.
    """
    chain = _chain(style.display, style.bold)
    glyphs: list[tuple[str, _Face, float]] = []
    for char in text:
        face = _resolve(chain, ord(char))
        glyphs.append((char, face, face.advance(ord(char), style.size) + style.letter_spacing))
    width = sum(glyph[2] for glyph in glyphs)
    content = sum(glyph[2] for glyph in glyphs[: len(text.rstrip())])
    height, baseline = _vertical_metrics(_resolve(chain, ord(text[0])), style)
    return _Word(tuple(glyphs), width, width - content, _js_round(height), _js_round(baseline))


def _flow(words: Sequence[_Word], width: float) -> tuple[list[_Line], float, int]:
    """Break measured segments into lines with satori's text flow algorithm.

    Args:
        words: Measured segments.
        width: Available width.

    Returns:
        tuple[list[_Line], float, int]: Lines, widest line and total height.
    """
    lines: list[_Line] = []
    current: list[_Word] = []
    current_width = 0.0
    current_height = 0
    current_baseline = 0
    widest = 0.0
    for index, word in enumerate(words):
        if index and current_width + word.width > width + word.trailing:
            lines.append(_Line(tuple(current), current_height, current_baseline))
            current = [word]
            current_width = word.width
            current_height = word.height if word.width else 0
            current_baseline = word.baseline if word.width else 0
            widest = max(widest, width)
        else:
            current.append(word)
            current_width += word.width
            if word.height > current_height:
                current_height = word.height
                current_baseline = word.baseline
        widest = max(widest, current_width)
    if current_width:
        lines.append(_Line(tuple(current), current_height, current_baseline))
    return lines, widest, sum(line.height for line in lines)


def _layout_text(text: str, style: _Style, width: float, balance: bool = False) -> _Text:
    """Lay out a text node like satori does, optionally with text-wrap: balance.

    Args:
        text: Text content.
        style: Text style.
        width: Available width.
        balance: Whether to balance line lengths.

    Returns:
        _Text: Laid-out text node.
    """
    words = [_measure_word(segment, style) for segment in _break_segments(text)]
    lines, widest, height = _flow(words, width)
    if balance:
        low, high = widest / 2, widest
        while low + 1 < high:
            middle = (low + high) / 2
            if _flow(words, middle)[2] > height:
                low = middle
            else:
                high = middle
        lines = _flow(words, high)[0]
        widest = high
    return _Text(style, tuple(lines), math.ceil(widest), height)


def _paragraph(text: str, style: _Style, width: float, balance: bool = False) -> _Paragraph:
    """Lay out a text box the way the templates' rich helper does, with inline emojis.

    Args:
        text: Text content that may contain known emoji codes.
        style: Text style.
        width: Available width.
        balance: Whether to balance line lengths of plain text.

    Returns:
        _Paragraph: Laid-out paragraph.
    """
    if not _EMOJI_TOKEN.search(text):
        node = _layout_text(text, style, width, balance)
        return _Paragraph((_Piece(0, 0, node),), node.width, node.height)
    gap = _js_round(style.size * 0.28)
    emoji_size = _js_round(style.size * 1.3)
    rows: list[list[_Piece]] = [[]]
    heights: list[list[int]] = [[]]
    used = 0
    for word in text.split(" "):
        match = _EMOJI_WORD.fullmatch(word)
        if match is not None and match.group(1) in _EMOJI_NAMES:
            piece = _Piece(0, 0, None, match.group(1), emoji_size)
            item_width = item_height = emoji_size
        elif match is None and word:
            node = _layout_text(word, style, width)
            if node.width + gap > width and (not rows[-1] or used + node.width + gap > width):
                node = _layout_text(word, style, max(0.0, width - gap))
                node = _Text(style, node.lines, max(0, _js_round(width - gap)), node.height)
            piece = _Piece(0, 0, node)
            item_width, item_height = node.width, node.height
        else:
            continue
        if rows[-1] and used + item_width + gap > width:
            rows.append([])
            heights.append([])
            used = 0
        rows[-1].append(_Piece(used, 0, piece.text, piece.emoji, piece.size))
        heights[-1].append(item_height)
        used += item_width + gap
    pieces: list[_Piece] = []
    top = 0
    widest = 0
    for row, row_heights in zip(rows, heights, strict=True):
        if not row:
            continue
        row_height = max(row_heights)
        for piece, item_height in zip(row, row_heights, strict=True):
            offset = top + _js_round((row_height - item_height) / 2)
            pieces.append(_Piece(piece.x, offset, piece.text, piece.emoji, piece.size))
        widest = max(widest, sum(_piece_width(piece) + gap for piece in row))
        top += row_height
    return _Paragraph(tuple(pieces), widest, top)


def _piece_width(piece: _Piece) -> int:
    """Get the width of a paragraph piece.

    Args:
        piece: Text node or emoji piece.

    Returns:
        int: Width of the piece's box.
    """
    return piece.text.width if piece.text is not None else piece.size


def _draw_text(canvas: Image.Image, node: _Text, x: int, y: int) -> None:
    """Draw a laid-out text node glyph by glyph at unhinted positions.

    Args:
        canvas: Image being drawn.
        node: Laid-out text node.
        x: Left edge of the text node.
        y: Top edge of the text node.
    """
    style = node.style
    snap = len(node.lines) > 1
    top = y
    for line in node.lines:
        baseline = top + line.baseline
        pen = 0.0
        shift: float | None = None
        for word in line.words:
            for char, face, advance in word.glyphs:
                if shift is None:
                    shift = _js_round(pen) - pen if snap else 0.0
                if char == " ":
                    shift = None
                elif not char.isspace():
                    position = x + pen + shift
                    column = math.floor(position)
                    step = _js_round((position - column) * _GLYPH_STEPS)
                    glyph = _glyph(face, style.size, char, step % _GLYPH_STEPS)
                    if glyph is not None:
                        mask, offset_x, offset_y = glyph
                        origin = (
                            column + step // _GLYPH_STEPS + offset_x,
                            baseline + offset_y,
                        )
                        canvas.paste(style.color, origin, mask)
                pen += advance
        top += line.height


def _draw_paragraph(canvas: Image.Image, paragraph: _Paragraph, x: int, y: int) -> None:
    """Draw a laid-out paragraph with its inline emojis.

    Args:
        canvas: Image being drawn.
        paragraph: Laid-out paragraph.
        x: Left edge of the paragraph.
        y: Top edge of the paragraph.
    """
    for piece in paragraph.pieces:
        if piece.text is not None:
            _draw_text(canvas, piece.text, x + piece.x, y + piece.y)
        elif piece.emoji is not None:
            image, left, top = _emoji(piece.emoji, piece.size)
            canvas.paste(image, (x + piece.x + left, y + piece.y + top), image)


def _contain(image: Image.Image, box: int) -> tuple[Image.Image, int, int]:
    """Scale an image to fit a square box like object-fit: contain.

    Args:
        image: Image to scale.
        box: Edge length of the box.

    Returns:
        tuple[Image.Image, int, int]: Scaled image and its offset inside the box.
    """
    scale = min(box / image.width, box / image.height)
    width = max(1, _js_round(image.width * scale))
    height = max(1, _js_round(image.height * scale))
    scaled = image.resize((width, height), Image.Resampling.LANCZOS)
    return (
        scaled,
        _js_round((box - image.width * scale) / 2),
        _js_round((box - image.height * scale) / 2),
    )


def _cover(
    image: Image.Image,
    width: int,
    height: int,
    resample: Image.Resampling = Image.Resampling.BILINEAR,
) -> Image.Image:
    """Scale and center-crop an image like object-fit: cover.

    Args:
        image: Image to scale.
        width: Target width.
        height: Target height.
        resample: Resampling filter.

    Returns:
        Image.Image: Image of exactly the target size.
    """
    scale = max(width / image.width, height / image.height)
    crop_width = width / scale
    crop_height = height / scale
    left = (image.width - crop_width) / 2
    top = (image.height - crop_height) / 2
    return image.resize(
        (width, height),
        resample,
        box=(left, top, left + crop_width, top + crop_height),
        reducing_gap=2.0,
    )


def _load_asset(name: str) -> Image.Image:
    """Read a bundled image fully into memory.

    Args:
        name: Path relative to the asset folder.

    Returns:
        Image.Image: Decoded RGBA image.
    """
    with Image.open(_ASSETS / name) as image:
        return image.convert("RGBA")


@lru_cache(maxsize=64)
def _emoji(name: str, box: int) -> tuple[Image.Image, int, int]:
    """Get an emoji scaled to fit its box.

    Args:
        name: Emoji name.
        box: Edge length of the emoji box.

    Returns:
        tuple[Image.Image, int, int]: Scaled RGBA image and its offset inside the box.
    """
    return _contain(_load_asset(f"emojis/{name}.png"), box)


@lru_cache(maxsize=8)
def _icon(name: str, box: int, opacity: float = 1.0) -> tuple[Image.Image, int, int]:
    """Get a bundled icon scaled to fit its box with an opacity applied.

    Args:
        name: File name in the asset folder.
        box: Edge length of the box.
        opacity: Opacity multiplier.

    Returns:
        tuple[Image.Image, int, int]: Scaled RGBA image and its offset inside the box.
    """
    image, left, top = _contain(_load_asset(name), box)
    if opacity < 1:
        image.putalpha(
            image.getchannel("A").point([_js_round(value * opacity) for value in range(256)])
        )
    return image, left, top


@lru_cache(maxsize=4)
def _default_picture(name: str) -> Image.Image:
    """Get a bundled fallback picture.

    Args:
        name: File name in the asset folder.

    Returns:
        Image.Image: Decoded image without an alpha channel when it is opaque.
    """
    return _flatten_opaque(_load_asset(name))


def _flatten_opaque(image: Image.Image) -> Image.Image:
    """Drop the alpha channel of an image that has no transparent pixels.

    Args:
        image: RGBA image.

    Returns:
        Image.Image: RGB image when fully opaque, else the RGBA image.
    """
    if image.getchannel("A").getextrema() == (255, 255):
        return image.convert("RGB")
    return image


def _decode(data: bytes | None) -> Image.Image | None:
    """Decode an untrusted image, taking the first frame of animations.

    Args:
        data: Encoded image, or None.

    Returns:
        Image.Image | None: RGB or RGBA image, or None when missing or unreadable.
    """
    if not data:
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format == "JPEG":
                image.draft("RGB", (_WIDTH, _WIDTH))
            transparent = image.mode in ("RGBA", "LA", "PA", "La", "RGBa") or (
                "transparency" in image.info
            )
            if not transparent:
                return image.convert("RGB")
            return _flatten_opaque(image.convert("RGBA"))
    except Exception:
        return None


def _scale_mask(mask: Image.Image, alpha: float) -> Image.Image:
    """Multiply a coverage mask by an opacity.

    Args:
        mask: Coverage mask.
        alpha: Opacity.

    Returns:
        Image.Image: Scaled mask.
    """
    return mask.point([_js_round(value * alpha) for value in range(256)])


@lru_cache(maxsize=64)
def _rounded_mask(width: int, height: int, radius: float, inset: int = 0) -> Image.Image:
    """Draw an antialiased rounded rectangle, optionally inset, by supersampling.

    Args:
        width: Width of the box.
        height: Height of the box.
        radius: Corner radius of the box before insetting.
        inset: Distance to shrink the shape by on every side.

    Returns:
        Image.Image: Coverage mask the size of the box.
    """
    radius = min(radius, width / 2, height / 2)
    scale = _SUPERSAMPLE
    mask = Image.new("L", (width * scale, height * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (
            inset * scale,
            inset * scale,
            (width - inset) * scale - 1,
            (height - inset) * scale - 1,
        ),
        radius=max(0.0, radius - inset) * scale,
        fill=255,
    )
    return mask.reduce(scale)


@lru_cache(maxsize=32)
def _badge_masks(
    width: int,
    height: int,
    radius: float,
    border: int,
    fill_alpha: float,
    border_alpha: float,
) -> tuple[Image.Image, Image.Image]:
    """Build the background and border masks of a rounded, bordered box.

    Args:
        width: Width of the box.
        height: Height of the box.
        radius: Corner radius.
        border: Border width.
        fill_alpha: Opacity of the background color, which also runs under the border.
        border_alpha: Opacity of the border color.

    Returns:
        tuple[Image.Image, Image.Image]: Background mask and border mask with the opacities applied.
    """
    outer = _rounded_mask(width, height, radius)
    band = ImageChops.subtract(outer, _rounded_mask(width, height, radius, border))
    return _scale_mask(outer, fill_alpha), _scale_mask(band, border_alpha)


@lru_cache(maxsize=4)
def _ring_masks(side: int, border: int, alpha: float) -> tuple[Image.Image, Image.Image]:
    """Build the masks of a circular picture frame.

    Args:
        side: Diameter including the border.
        border: Border width.
        alpha: Opacity of the border color.

    Returns:
        tuple[Image.Image, Image.Image]: Picture mask aligned to the content origin and border mask.
    """
    outer = _rounded_mask(side, side, side / 2)
    inner = _rounded_mask(side, side, side / 2, border)
    band = _scale_mask(ImageChops.subtract(outer, inner), alpha)
    return inner.crop((border, border, border + side, border + side)), band


def _draw_badge(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    radius: float,
    border: int,
    fill: tuple[_Color, float],
    stroke: tuple[_Color, float],
) -> None:
    """Draw a translucent rounded box with a translucent border.

    Args:
        canvas: Image being drawn.
        box: Left, top, width and height of the box.
        radius: Corner radius.
        border: Border width.
        fill: Background color and opacity.
        stroke: Border color and opacity.
    """
    left, top, width, height = box
    fill_mask, border_mask = _badge_masks(width, height, radius, border, fill[1], stroke[1])
    canvas.paste(fill[0], (left, top), fill_mask)
    canvas.paste(stroke[0], (left, top), border_mask)


def _rotation(angle: float, dx: float, dy: float) -> tuple[float, float]:
    """Rotate a vector clockwise on screen, the way CSS rotate() does.

    Args:
        angle: Clockwise rotation in degrees.
        dx: Horizontal component.
        dy: Vertical component.

    Returns:
        tuple[float, float]: Rotated vector.
    """
    radians = math.radians(angle)
    return (
        dx * math.cos(radians) - dy * math.sin(radians),
        dx * math.sin(radians) + dy * math.cos(radians),
    )


@lru_cache(maxsize=16)
def _card(
    side: int,
    radius: float,
    border: int,
    border_alpha: float,
    angle: float,
    shadow: tuple[float, float, float],
) -> _Card:
    """Precompute the masks of a rounded image box with a border and drop shadow.

    Args:
        side: Edge length of the box.
        radius: Corner radius.
        border: Border width.
        border_alpha: Opacity of the white border.
        angle: Clockwise rotation in degrees.
        shadow: Vertical offset, blur radius and opacity of the black box shadow.

    Returns:
        _Card: Masks ready to paste around the box center.
    """
    offset_y, blur, shadow_alpha = shadow
    radians = math.radians(angle)
    spread = abs(math.cos(radians)) + abs(math.sin(radians)) - 1
    margin = math.ceil(side * spread / 2) + 2 if angle else 0
    frame = side + margin * 2
    outer = Image.new("L", (frame, frame), 0)
    outer.paste(_rounded_mask(side, side, radius), (margin, margin))
    inner = Image.new("L", (frame, frame), 0)
    inner.paste(_rounded_mask(side, side, radius, border), (margin, margin))
    if angle:
        outer = outer.rotate(-angle, Image.Resampling.BICUBIC)
        inner = inner.rotate(-angle, Image.Resampling.BICUBIC)
    band = _scale_mask(ImageChops.subtract(outer, inner), border_alpha)
    pad = math.ceil(blur * 3 + offset_y)
    shadow_frame = frame + pad * 2
    shift_x, shift_y = _rotation(angle, 0, offset_y)
    cast = Image.new("L", (shadow_frame, shadow_frame), 0)
    cast.paste(outer, (pad + _js_round(shift_x), pad + _js_round(shift_y)))
    cast = cast.filter(ImageFilter.GaussianBlur(blur))
    hole = Image.new("L", (shadow_frame, shadow_frame), 0)
    hole.paste(outer, (pad, pad))
    cast = _scale_mask(ImageChops.multiply(cast, ImageChops.invert(hole)), shadow_alpha)
    return _Card(side, border, frame, inner, band, shadow_frame, cast, angle)


def _alpha_mask(mask: Image.Image, picture: Image.Image) -> Image.Image:
    """Combine a coverage mask with a picture's own transparency.

    Args:
        mask: Coverage mask.
        picture: RGB or RGBA picture the size of the mask.

    Returns:
        Image.Image: Mask to paste the picture through.
    """
    if picture.mode != "RGBA":
        return mask
    return ImageChops.multiply(mask, picture.getchannel("A"))


def _draw_card(
    canvas: Image.Image,
    card: _Card,
    center: tuple[int, int],
    picture: Image.Image | None,
    fill: _Color = _SLATE_700,
) -> tuple[int, int]:
    """Draw a box's shadow, border and clipped picture, the way satori layers them.

    Args:
        canvas: Image being drawn.
        card: Precomputed masks of the box.
        center: Center of the box on the canvas.
        picture: Picture drawn at the box's content origin, or None to fill with a color.
        fill: Background color used when there is no picture.

    Returns:
        tuple[int, int]: Top-left corner of the box's padding area on the canvas.
    """
    shadow_origin = (
        center[0] - card.shadow_frame // 2,
        center[1] - card.shadow_frame // 2,
    )
    canvas.paste(_BLACK, shadow_origin, card.shadow)
    origin = (center[0] - card.frame // 2, center[1] - card.frame // 2)
    canvas.paste(_WHITE, origin, card.border_mask)
    margin = (card.frame - card.side) // 2
    content = margin + card.border
    if picture is None:
        canvas.paste(fill, origin, card.inner)
        return origin[0] + content, origin[1] + content
    scaled = _cover(picture, card.side, card.side)
    local = Image.new(scaled.mode, (card.frame, card.frame))
    if card.angle:
        for dx, dy in ((-2, -2), (-2, 0), (0, -2)):
            local.paste(scaled, (content + dx, content + dy))
    local.paste(scaled, (content, content))
    if card.angle:
        local = local.rotate(-card.angle, Image.Resampling.BILINEAR)
    mask = _alpha_mask(card.inner, local)
    canvas.paste(local if local.mode == "RGB" else local.convert("RGB"), origin, mask)
    return origin[0] + content, origin[1] + content


def _draw_avatar(
    canvas: Image.Image,
    picture: Image.Image,
    left: int,
    top: int,
    side: int,
    border: int,
    alpha: float,
) -> None:
    """Draw a circular picture with a translucent ring, clipped like satori clips it.

    Args:
        canvas: Image being drawn.
        picture: Picture to draw.
        left: Left edge of the circle's box.
        top: Top edge of the circle's box.
        side: Diameter including the border.
        border: Border width.
        alpha: Opacity of the white border.
    """
    mask, border_mask = _ring_masks(side, border, alpha)
    canvas.paste(_WHITE, (left, top), border_mask)
    scaled = _cover(picture, side, side)
    flat = scaled if scaled.mode == "RGB" else scaled.convert("RGB")
    canvas.paste(flat, (left + border, top + border), _alpha_mask(mask, scaled))


def _draw_fan(canvas: Image.Image, fan: _Fan, pictures: Sequence[Image.Image]) -> None:
    """Draw up to three jackets fanned out, the first one on top in the middle.

    Args:
        canvas: Image being drawn.
        fan: Placement of the cards.
        pictures: Jackets in order of importance.
    """
    order = [pictures[index] if index < len(pictures) else None for index in (2, 1, 0)]
    for (left, top, angle), picture in zip(fan.cards, order, strict=True):
        if picture is None:
            continue
        card = _card(230, fan.radius, 3, fan.border_alpha, angle, (24.0, 25.0, 0.55))
        _draw_card(canvas, card, (fan.left + left + 115, fan.top + top + 115), picture)


def _interpolate(stops: Sequence[tuple[float, tuple[float, ...]]], t: float) -> tuple[float, ...]:
    """Interpolate gradient color stops at a position.

    Args:
        stops: Positions and channel values, sorted by position.
        t: Position between 0 and 1.

    Returns:
        tuple[float, ...]: Interpolated channel values.
    """
    if t <= stops[0][0]:
        return stops[0][1]
    for (start, low), (end, high) in zip(stops, stops[1:], strict=False):
        if t <= end:
            f = (t - start) / (end - start) if end > start else 1.0
            return tuple(a + (b - a) * f for a, b in zip(low, high, strict=True))
    return stops[-1][1]


def _linear_gradient(
    angle: float,
    stops: Sequence[tuple[float, tuple[float, ...]]],
    mode: str,
    size: tuple[int, int] = (_WIDTH, _HEIGHT),
) -> Image.Image:
    """Render a CSS linear-gradient by stretching a precise 1D ramp over the box.

    Args:
        angle: CSS gradient angle in degrees.
        stops: Positions and channel values of the color stops.
        mode: Pillow mode of the result, L or RGB.
        size: Width and height of the box.

    Returns:
        Image.Image: Gradient image.
    """
    steps = 1024
    values = [
        tuple(_js_round(channel) for channel in _interpolate(stops, index / (steps - 1)))
        for index in range(steps)
    ]
    ramp = Image.new(mode, (steps, 3))
    if mode == "L":
        ramp.putdata([value[0] for value in values] * 3)
    else:
        ramp.putdata(values * 3)
    width, height = size
    radians = math.radians(angle)
    dx, dy = math.sin(radians), -math.cos(radians)
    length = abs(width * dx) + abs(height * dy)
    start_x = width / 2 - dx * length / 2
    start_y = height / 2 - dy * length / 2
    k = (steps - 1) / length
    return ramp.transform(
        size,
        Image.Transform.AFFINE,
        (k * dx, k * dy, 0.5 - k * (dx * start_x + dy * start_y), 0, 0, 1.5),
        resample=Image.Resampling.BILINEAR,
    )


def _radial_glow(
    base: Image.Image,
    center: tuple[float, float],
    color: _Color,
    alpha: float,
    extent: float,
) -> Image.Image:
    """Blend a circular CSS radial-gradient that fades to transparent over a background.

    Args:
        base: Background to blend over.
        center: Center of the gradient.
        color: _Color at the center.
        alpha: Opacity at the center.
        extent: Fraction of the farthest-corner radius where the gradient becomes transparent.

    Returns:
        Image.Image: Blended image.
    """
    width, height = base.size
    radius = max(
        math.hypot(corner_x - center[0], corner_y - center[1])
        for corner_x in (0, width)
        for corner_y in (0, height)
    )
    k = 128 / (radius * extent)
    distance = Image.radial_gradient("L").transform(
        base.size,
        Image.Transform.AFFINE,
        (k, 0, 128.5 - k * center[0], 0, k, 128.5 - k * center[1]),
        resample=Image.Resampling.BILINEAR,
        fillcolor=255,
    )
    fade = [max(0.0, 1 - (value + 0.5) / (128 * math.sqrt(2))) for value in range(256)]
    mask = distance.point([_js_round(alpha * 255 * f) for f in fade])
    tint = Image.merge(
        "RGB",
        [distance.point([_js_round(channel * f) for f in fade]) for channel in color],
    )
    return Image.composite(tint, base, mask)


@lru_cache(maxsize=1)
def _generic_background() -> Image.Image:
    """Render the generic design's background gradients once.

    Returns:
        Image.Image: RGB background.
    """
    base = _linear_gradient(
        135, ((0.0, (6, 16, 29)), (0.55, (10, 23, 40)), (1.0, (10, 36, 48))), "RGB"
    )
    base = _radial_glow(base, (_WIDTH * 0.88, _HEIGHT * 0.85), (57, 197, 187), 0.28, 0.45)
    return _radial_glow(base, (_WIDTH * 0.12, _HEIGHT * 0.18), (59, 130, 246), 0.35, 0.45)


@lru_cache(maxsize=2)
def _backdrop_mask(design: str) -> Image.Image:
    """Combine a design's backdrop opacity and dark overlay gradient into one mask.

    Args:
        design: Either level or user.

    Returns:
        Image.Image: Mask of how much of the dark background covers the backdrop picture.
    """
    if design == "level":
        angle, opacity = 120.0, 0.4
        stops = ((0.0, 0.94), (0.45, 0.82), (1.0, 0.6))
    else:
        angle, opacity = 90.0, 0.55
        stops = ((0.0, 0.96), (0.52, 0.84), (1.0, 0.5))
    return _linear_gradient(
        angle,
        tuple((position, (255 * (1 - opacity * (1 - cover)),)) for position, cover in stops),
        "L",
    )


@lru_cache(maxsize=1)
def _solid_background() -> Image.Image:
    """Get a canvas-sized image of the dark page color.

    Returns:
        Image.Image: RGB image.
    """
    return Image.new("RGB", (_WIDTH, _HEIGHT), _BACKGROUND)


def _backdrop(design: str, picture: Image.Image | None) -> Image.Image:
    """Draw a translucent full-bleed picture under the design's dark overlay.

    Args:
        design: Either level or user.
        picture: Backdrop picture, or None for a plain background.

    Returns:
        Image.Image: New RGB canvas.
    """
    if picture is None:
        return _solid_background().copy()
    shrinking = max(_WIDTH / picture.width, _HEIGHT / picture.height) <= 1
    resample = Image.Resampling.BOX if shrinking else Image.Resampling.BILINEAR
    scaled = _cover(picture, _WIDTH, _HEIGHT, resample)
    if scaled.mode == "RGBA":
        flat = _solid_background().copy()
        flat.paste(scaled, (0, 0), scaled)
        scaled = flat
    scaled.paste(_BACKGROUND, (0, 0), _backdrop_mask(design))
    return scaled


@lru_cache(maxsize=1)
def _default_banner_backdrop() -> Image.Image:
    """Render the user design's backdrop for the bundled default banner once.

    Returns:
        Image.Image: RGB canvas background.
    """
    return _backdrop("user", _default_picture("def.png"))


@lru_cache(maxsize=1)
def _divider() -> tuple[Image.Image, Image.Image]:
    """Render the generic design's short gradient divider once.

    Returns:
        tuple[Image.Image, Image.Image]: Gradient colors and pill-shaped mask.
    """
    colors = _linear_gradient(90, ((0.0, (59, 130, 246)), (1.0, (57, 197, 187))), "RGB", (84, 6))
    return colors, _rounded_mask(84, 6, 3)


def _encode(canvas: Image.Image) -> bytes:
    """Encode the finished canvas as PNG.

    Args:
        canvas: Finished image.

    Returns:
        bytes: PNG file contents.
    """
    out = BytesIO()
    canvas.save(
        out,
        format="PNG",
        compress_level=_PNG_COMPRESS_LEVEL,
        compress_type=_PNG_RLE_STRATEGY,
    )
    return out.getvalue()


def _draw_brand(canvas: Image.Image, right: int) -> None:
    """Draw the bottom-right wordmark with the outline icon.

    Args:
        canvas: Image being drawn.
        right: Distance of the row from the right edge.
    """
    label = _layout_text("Untitled Charts", _Style(24, _SKY_400), _WIDTH - right)
    width = label.width + 14 + 46
    height = max(label.height, 46)
    left = _WIDTH - right - width
    top = _HEIGHT - 40 - height
    _draw_text(canvas, label, left, top + _js_round((height - label.height) / 2))
    icon, icon_left, icon_top = _icon("miku.png", 46, 0.9)
    icon_y = top + _js_round((height - 46) / 2) + icon_top
    canvas.paste(icon, (left + label.width + 14 + icon_left, icon_y), icon)


def _first_pictures(images: Sequence[bytes | None], count: int) -> list[Image.Image]:
    """Decode images in order until enough of them are readable.

    Args:
        images: Encoded images, possibly missing or unreadable.
        count: Number of pictures wanted.

    Returns:
        list[Image.Image]: Up to count decoded pictures.
    """
    pictures: list[Image.Image] = []
    for data in images:
        if len(pictures) == count:
            break
        picture = _decode(data)
        if picture is not None:
            pictures.append(picture)
    return pictures


def _stack(blocks: Sequence[tuple[int, int]], top: float, height: float) -> list[int]:
    """Place blocks in a column with justify-content: center and pixel-grid rounding.

    Args:
        blocks: Margin above and height of each block.
        top: Top edge of the column.
        height: Height of the column.

    Returns:
        list[int]: Top edge of each block on the canvas.
    """
    total = sum(margin + size for margin, size in blocks)
    cursor = (height - total) / 2
    tops: list[int] = []
    for margin, size in blocks:
        cursor += margin
        tops.append(_js_round(top + cursor))
        cursor += size
    return tops


def render_generic_og(
    title: str | None = None,
    subtitle: str | None = None,
    jackets: Sequence[bytes | None] = (),
) -> bytes:
    """Draw the site-wide link preview, or the home page one when there is no title.

    Args:
        title: Page title; empty or None draws the home design with the big wordmark.
        subtitle: Text under the divider; empty or None uses the site tagline.
        jackets: Encoded staff pick jackets in API order; the first three readable ones are shown.

    Returns:
        bytes: 1200x630 PNG.
    """
    is_home = not title
    heading = _og_text(title or "UntitledCharts", 34)
    tagline = _og_text(subtitle or _DEFAULT_SUBTITLE, 80)
    pictures = _first_pictures(jackets, 3)
    canvas = _generic_background().copy()
    column = 620 if pictures else 1040
    mark = 58 if is_home else 30
    wordmark = _layout_text(
        "UntitledCharts",
        _Style(mark, bold=True, display=True, letter_spacing=-1.0),
        column - mark - 18,
    )
    wordmark_height = max(mark, wordmark.height)
    headline = (
        None
        if is_home
        else _layout_text(heading, _Style(60, bold=True, display=True, line_height=1.1), column)
    )
    caption = _layout_text(
        tagline, _Style(30, _SUBTITLE, line_height=1.35), min(column, 560), balance=True
    )
    blocks = [(0, wordmark_height)]
    if headline is not None:
        blocks.append((26, headline.height))
    blocks += [(30, 6), (26, caption.height)]
    tops = _stack(blocks, 0, _HEIGHT)
    logo, logo_left, logo_top = _icon("logo.png", mark)
    canvas.paste(
        logo,
        (80 + logo_left, tops[0] + _js_round((wordmark_height - mark) / 2) + logo_top),
        logo,
    )
    _draw_text(
        canvas,
        wordmark,
        80 + mark + 18,
        tops[0] + _js_round((wordmark_height - wordmark.height) / 2),
    )
    if headline is not None:
        _draw_text(canvas, headline, 80, tops[1])
    colors, pill = _divider()
    canvas.paste(colors, (80, tops[-2]), pill)
    _draw_text(canvas, caption, 80, tops[-1])
    _draw_fan(canvas, _GENERIC_FAN, pictures)
    miku, miku_left, miku_top = _icon("miku.png", 56)
    canvas.paste(miku, (_WIDTH - 40 - 56 + miku_left, _HEIGHT - 34 - 56 + miku_top), miku)
    return _encode(canvas)


def render_level_og(
    *,
    title: str | None,
    artists: str | None,
    author: str | None,
    description: str | None,
    rating: float | None,
    like_count: int,
    comment_count: int,
    staff_pick: bool,
    jacket: bytes | None,
    background: bytes | None,
    avatar: bytes | None,
) -> bytes:
    """Draw a chart's link preview.

    Args:
        title: Chart title.
        artists: Chart artists.
        author: Charter name shown above the title.
        description: Chart description.
        rating: Chart difficulty rating; zero or None shows a question mark.
        like_count: Number of likes.
        comment_count: Number of comments.
        staff_pick: Whether the chart is a staff pick.
        jacket: Encoded jacket, or None for a placeholder.
        background: Encoded background; None falls back to the jacket.
        avatar: Encoded charter profile picture; None uses the default one.

    Returns:
        bytes: 1200x630 PNG.
    """
    title_text = _og_text(title, 30)
    artists_text = _og_text(artists, 40)
    author_text = _og_text(author, 26)
    description_text = _og_text(description, 140)
    cover = _decode(jacket)
    backdrop = _decode(background)
    if backdrop is None:
        backdrop = cover
    picture = _decode(avatar)
    if picture is None:
        picture = _default_picture("defpfp.png")
    canvas = _backdrop("level", backdrop)

    if staff_pick:
        label = _layout_text("\u2605 Staff Pick", _Style(20, _AMBER_400, bold=True), _WIDTH)
        width = label.width + 36
        height = label.height + 16
        left = _WIDTH - 60 - width
        _draw_badge(
            canvas,
            (left, 54, width, height),
            50,
            2,
            (_AMBER_400, 0.15),
            (_AMBER_400, 0.6),
        )
        _draw_text(canvas, label, left + 18, 62)

    byline = _paragraph(author_text, _Style(26, bold=True), 628)
    platform = _layout_text("on UntitledCharts", _Style(18, _SLATE_400), 628)
    byline_height = byline.height + 2 + platform.height
    header_height = max(56, byline_height)
    headline = _paragraph(title_text, _Style(56, bold=True, line_height=1.06), 700)
    credits = _paragraph(artists_text, _Style(26, _SLATE_300), 700)
    blurb = (
        _paragraph(description_text, _Style(23, _SLATE_400, line_height=1.35), 660)
        if description_text
        else None
    )
    likes = _layout_text(str(like_count or 0), _Style(24, _RED_300, bold=True), 700)
    comments = _layout_text(str(comment_count or 0), _Style(24, _SLATE_300, bold=True), 700)
    stats_height = max(26, likes.height, comments.height)
    blocks = [(0, header_height), (22, headline.height), (10, credits.height)]
    if blurb is not None:
        blocks.append((18, blurb.height))
    blocks.append((26, stats_height))
    tops = _stack(blocks, 70, 490)

    _draw_avatar(canvas, picture, 70, tops[0] + _js_round((header_height - 56) / 2), 56, 2, 0.25)
    byline_top = tops[0] + _js_round((header_height - byline_height) / 2)
    _draw_paragraph(canvas, byline, 142, byline_top)
    _draw_text(canvas, platform, 142, byline_top + byline.height + 2)
    _draw_paragraph(canvas, headline, 70, tops[1])
    _draw_paragraph(canvas, credits, 70, tops[2])
    if blurb is not None:
        _draw_paragraph(canvas, blurb, 70, tops[3])
    left = 70
    for icon_name, count in (("heart.png", likes), ("chat.png", comments)):
        group_height = max(26, count.height)
        group_top = tops[-1] + _js_round((stats_height - group_height) / 2)
        icon, _, _ = _icon(icon_name, 26)
        canvas.paste(icon, (left, group_top + _js_round((group_height - 26) / 2)), icon)
        _draw_text(
            canvas,
            count,
            left + 36,
            group_top + _js_round((group_height - count.height) / 2),
        )
        left += 36 + count.width + 32

    card = _card(300, 28, 1, 0.12, 0.0, (30.0, 30.0, 0.6))
    content_left, content_top = _draw_card(canvas, card, (980, 315), cover)
    if cover is None:
        note = _layout_text("\u266a", _Style(80), 300)
        _draw_text(
            canvas,
            note,
            content_left + _js_round((300 - note.width) / 2),
            content_top + _js_round((300 - note.height) / 2),
        )
    level = _layout_text(
        f"Lv. {_js_number(rating) if rating else '?'}",
        _Style(18, _SKY_400, bold=True),
        298,
    )
    badge_width = level.width + 26
    badge_left = content_left + 298 - 12 - badge_width
    _draw_badge(
        canvas,
        (badge_left, content_top + 12, badge_width, level.height + 10),
        8,
        1,
        (_BLACK, 0.7),
        (_WHITE, 0.2),
    )
    _draw_text(canvas, level, badge_left + 13, content_top + 17)
    _draw_brand(canvas, 70)
    return _encode(canvas)


def render_user_og(
    *,
    username: str | None,
    handle: int | str,
    role: str | None,
    bio: str | None,
    charts: int,
    likes: int,
    comments: int,
    avatar: bytes | None,
    banner: bytes | None,
    jackets: Sequence[bytes | None] = (),
) -> bytes:
    """Draw a user profile's link preview.

    Args:
        username: Display name.
        handle: Numeric handle shown after a hash sign.
        role: Badge text such as Admin or Moderator, or None for no badge.
        bio: Profile description.
        charts: Number of published charts.
        likes: Number of likes received.
        comments: Number of comments received.
        avatar: Encoded profile picture; None uses the default one.
        banner: Encoded banner; None uses the default one.
        jackets: Encoded jackets of the most liked charts, most liked first; up to three are shown.

    Returns:
        bytes: 1200x630 PNG.
    """
    name_text = _og_text(username, 22)
    bio_text = _og_text(bio, 110)
    picture = _decode(avatar)
    if picture is None:
        picture = _default_picture("defpfp.png")
    backdrop = _decode(banner)
    pictures = _first_pictures(jackets, 3)
    if backdrop is None:
        canvas = _default_banner_backdrop().copy()
    else:
        canvas = _backdrop("user", backdrop)

    name = _paragraph(name_text, _Style(62, bold=True, line_height=1.05), 660)
    tag = _layout_text(f"#{handle}", _Style(26, _SLATE_400), 660)
    badge = _layout_text(role, _Style(18, _VIOLET_300, bold=True), 660) if role else None
    badge_height = badge.height + 8 if badge is not None else 0
    tag_height = max(tag.height, badge_height)
    about = (
        _paragraph(bio_text, _Style(23, _SLATE_300, line_height=1.35), 600) if bio_text else None
    )
    stats: list[tuple[_Text, _Text]] = []
    for value, singular, plural in (
        (charts, "chart", "charts"),
        (likes, "like", "likes"),
        (comments, "comment", "comments"),
    ):
        number = _layout_text(_compact_number(value), _Style(42, bold=True, line_height=1.0), 660)
        caption = _layout_text(singular if value == 1 else plural, _Style(19, _SLATE_400), 660)
        stats.append((number, caption))
    stats_height = max(number.height + 8 + caption.height for number, caption in stats)
    blocks = [(0, 164), (26, name.height), (10, tag_height)]
    if about is not None:
        blocks.append((16, about.height))
    blocks.append((34, stats_height))
    tops = _stack(blocks, 0, _HEIGHT)

    _draw_avatar(canvas, picture, 72, tops[0], 164, 6, 0.14)
    _draw_paragraph(canvas, name, 72, tops[1])
    _draw_text(canvas, tag, 72, tops[2] + _js_round((tag_height - tag.height) / 2))
    if badge is not None:
        badge_left = 72 + tag.width + 14
        badge_top = tops[2] + _js_round((tag_height - badge_height) / 2)
        _draw_badge(
            canvas,
            (badge_left, badge_top, badge.width + 22, badge_height),
            8,
            1,
            (_VIOLET_500, 0.18),
            (_VIOLET_400, 0.45),
        )
        _draw_text(canvas, badge, badge_left + 11, badge_top + 4)
    if about is not None:
        _draw_paragraph(canvas, about, 72, tops[3])
    left = 72
    for number, caption in stats:
        _draw_text(canvas, number, left, tops[-1])
        _draw_text(canvas, caption, left, tops[-1] + number.height + 8)
        left += max(number.width, caption.width) + 48
    _draw_fan(canvas, _USER_FAN, pictures)
    _draw_brand(canvas, 72)
    return _encode(canvas)
