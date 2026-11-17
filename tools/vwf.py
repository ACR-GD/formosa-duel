"""Variable-width bitmap font loader, text styling effects, and 8x8 tile deduplicator for Formosa Duel."""

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from PIL import Image

from fd_rom import TileGrid, TileKey, flip_tile_grid, tilemap_word

Grid2D = List[List[int]]


class BitmapFont:
    """Grid-packed monochrome bitmap font with per-glyph tight horizontal bounds."""

    def __init__(self, png_path: Path, json_path: Path) -> None:
        meta = json.loads(json_path.read_text(encoding="utf-8"))
        self.cell_w: int = int(meta["cell_w"])
        self.cell_h: int = int(meta["cell_h"])
        self.pad: int = int(meta.get("pad", 1))
        self.ascii_start: int = int(meta.get("ascii_start", 0))
        self.advances: Dict[str, int] = dict(meta.get("advances", {}))

        im = Image.open(png_path).convert("RGB")
        cols = (im.width - self.pad) // (self.cell_w + self.pad)
        px = im.load()

        self.glyphs: Dict[str, Tuple[int, Grid2D]] = {}
        for code in range(self.ascii_start, 128):
            ch = chr(code)
            idx = code - self.ascii_start
            row_i, col_i = divmod(idx, cols)
            x0 = self.pad + col_i * (self.cell_w + self.pad)
            y0 = self.pad + row_i * (self.cell_h + self.pad)
            if y0 + self.cell_h > im.height or x0 + self.cell_w > im.width:
                continue

            raw: Grid2D = []
            xs: List[int] = []
            for y in range(self.cell_h):
                r: List[int] = []
                for x in range(self.cell_w):
                    rgb = px[x0 + x, y0 + y]
                    on = 1 if (rgb[0] + rgb[1] + rgb[2]) < 380 else 0
                    r.append(on)
                    if on:
                        xs.append(x)
                raw.append(r)

            if xs:
                xmin, xmax = min(xs), max(xs)
                trimmed = [r[xmin : xmax + 1] for r in raw]
                self.glyphs[ch] = (xmax - xmin + 1, trimmed)

        # Normalize space glyph (default width if not explicitly given)
        sp_w = int(self.advances.get(" ", max(3, self.cell_w // 3)))
        self.glyphs[" "] = (sp_w, [[0] * sp_w for _ in range(self.cell_h)])

    def get_glyph(self, ch: str, scale_x: float = 1.0, space_w: Optional[int] = None) -> Tuple[int, Grid2D]:
        """Return (width, 2D mask) for character `ch`, optionally stretched horizontally."""
        if ch == " ":
            w = space_w if space_w is not None else self.glyphs[" "][0]
            if scale_x != 1.0 and space_w is None:
                w = max(1, int(round(w * scale_x)))
            return w, [[0] * w for _ in range(self.cell_h)]

        if ch not in self.glyphs:
            raise KeyError(f"Character {ch!r} (0x{ord(ch):02X}) missing from font")
        w, raw = self.glyphs[ch]
        if scale_x == 1.0:
            return w, [list(r) for r in raw]

        nw = max(1, int(round(w * scale_x)))
        scaled: Grid2D = []
        for y in range(self.cell_h):
            r = raw[y]
            scaled.append([r[min(w - 1, int(x / scale_x))] for x in range(nw)])
        return nw, scaled


def load_fonts(notes_dir: Path) -> Dict[str, BitmapFont]:
    """Load `banner`, `display`, and `body` VWF bitmap fonts from `notes_dir`."""
    return {
        name: BitmapFont(notes_dir / f"font_vwf_{name}.png", notes_dir / f"font_vwf_{name}.json")
        for name in ("banner", "display", "body")
    }


def render_text_mask(
    font: BitmapFont,
    text: str,
    tracking: int = 1,
    space_w: Optional[int] = None,
    scale_x: float = 1.0,
) -> Grid2D:
    """Render `text` into a tightly bounded 2D binary mask (`0/1`) of height `font.cell_h`."""
    pieces = [font.get_glyph(ch, scale_x=scale_x, space_w=space_w) for ch in text]
    if not pieces:
        return [[0] for _ in range(font.cell_h)]

    total_w = sum(w for w, _ in pieces) + tracking * (len(pieces) - 1)
    mask: Grid2D = [[0] * max(1, total_w) for _ in range(font.cell_h)]
    cx = 0
    for idx, (gw, g) in enumerate(pieces):
        for y in range(font.cell_h):
            row_dst = mask[y]
            row_src = g[y]
            for x in range(gw):
                if row_src[x]:
                    row_dst[cx + x] = 1
        cx += gw + (tracking if idx + 1 < len(pieces) else 0)
    return mask


def _ink_y_bounds(mask: Sequence[Sequence[int]]) -> Tuple[int, int]:
    ys = [y for y, row in enumerate(mask) if any(row)]
    if not ys:
        return 0, max(0, len(mask) - 1)
    return min(ys), max(ys)


def _place_mask(
    mask: Sequence[Sequence[int]],
    width: int,
    height: int,
    x_off: Optional[int],
    y_off: Optional[int],
) -> Grid2D:
    """Place binary mask onto a `width x height` canvas (center if x_off/y_off is None)."""
    mw = len(mask[0]) if mask else 0
    mh = len(mask)
    if x_off is None:
        x_off = (width - mw) // 2
    if y_off is None:
        ymin, ymax = _ink_y_bounds(mask)
        ink_h = ymax - ymin + 1
        y_off = ((height - ink_h) // 2) - ymin

    placed: Grid2D = [[0] * width for _ in range(height)]
    for y in range(mh):
        dy = y_off + y
        if not (0 <= dy < height):
            continue
        dst_row = placed[dy]
        src_row = mask[y]
        for x in range(mw):
            if src_row[x]:
                dx = x_off + x
                if 0 <= dx < width:
                    dst_row[dx] = 1
    return placed


def style_solid(
    mask: Sequence[Sequence[int]],
    width: int,
    height: int,
    fg: int,
    bg: int = 0,
    x_off: Optional[int] = None,
    y_off: Optional[int] = None,
) -> Grid2D:
    """Solid fill (`fg` on `bg`)."""
    m = _place_mask(mask, width, height, x_off, y_off)
    return [[fg if m[y][x] else bg for x in range(width)] for y in range(height)]


def style_drop_shadow(
    mask: Sequence[Sequence[int]],
    width: int,
    height: int,
    fg: int,
    shadow: int,
    bg: int = 0,
    shadow_deltas: Sequence[Tuple[int, int]] = ((0, 1), (0, 2)),
    x_off: Optional[int] = None,
    y_off: Optional[int] = None,
) -> Grid2D:
    """Solid fill with crisp directional drop-shadow (e.g. Title subtitle)."""
    m = _place_mask(mask, width, height, x_off, y_off)
    canvas: Grid2D = [[bg] * width for _ in range(height)]
    for dx, dy in shadow_deltas:
        for y in range(height):
            sy = y - dy
            if not (0 <= sy < height):
                continue
            for x in range(width):
                sx = x - dx
                if 0 <= sx < width and m[sy][sx]:
                    canvas[y][x] = shadow
    for y in range(height):
        for x in range(width):
            if m[y][x]:
                canvas[y][x] = fg
    return canvas


def style_outlined(
    mask: Sequence[Sequence[int]],
    width: int,
    height: int,
    fg: int,
    outline: int,
    bg: int = 0,
    neighbors: int = 4,
    x_off: Optional[int] = None,
    y_off: Optional[int] = None,
) -> Grid2D:
    """Solid foreground ringed by 4- or 8-connected outline."""
    m = _place_mask(mask, width, height, x_off, y_off)
    deltas = (
        ((-1, 0), (1, 0), (0, -1), (0, 1))
        if neighbors == 4
        else tuple((dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dx or dy)
    )
    canvas: Grid2D = [[bg] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            if m[y][x]:
                canvas[y][x] = fg
                continue
            for dx, dy in deltas:
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height and m[ny][nx]:
                    canvas[y][x] = outline
                    break
    return canvas


def style_carved_banner(
    mask: Sequence[Sequence[int]],
    width: int,
    height: int,
    fill_col: int,
    outline_col: int,
    top_col: int,
    bg_col: int = 0,
    x_off: Optional[int] = None,
    y_off: Optional[int] = None,
) -> Grid2D:
    """Arcade gold/amber header: 8-neighbor outline + dy=2 shadow, main fill + top-left glint."""
    m = _place_mask(mask, width, height, x_off, y_off)
    canvas: Grid2D = [[bg_col] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            if m[y][x]:
                continue
            hit = False
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if (dx or dy) and 0 <= y + dy < height and 0 <= x + dx < width and m[y + dy][x + dx]:
                        hit = True
                        break
                if hit:
                    break
            if not hit and y >= 2 and (m[y - 2][x] or (x >= 1 and m[y - 2][x - 1])):
                hit = True
            if hit:
                canvas[y][x] = outline_col

    for y in range(height):
        for x in range(width):
            if m[y][x]:
                above_empty = (y == 0) or (m[y - 1][x] == 0)
                left_empty = (x == 0) or (m[y][x - 1] == 0)
                canvas[y][x] = top_col if (above_empty and left_empty) else fill_col
    return canvas


def style_bevel_banner(
    mask: Sequence[Sequence[int]],
    width: int,
    height: int,
    outline_col: int,
    top_fill: int,
    bot_fill: int,
    bg_col: int = 0,
    x_off: Optional[int] = None,
    y_off: Optional[int] = None,
) -> Grid2D:
    """Two-tone beveled sprite banner (used by HUD PAUSED! and HURRY UP!)."""
    m = _place_mask(mask, width, height, x_off, y_off)
    ymin, ymax = _ink_y_bounds(m)
    split_y = ymin + (ymax - ymin + 1) // 2

    canvas: Grid2D = [[bg_col] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            if m[y][x]:
                continue
            hit = False
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if (dx or dy) and 0 <= y + dy < height and 0 <= x + dx < width and m[y + dy][x + dx]:
                        hit = True
                        break
                if hit:
                    break
            if not hit and y >= 2 and m[y - 2][x]:
                hit = True
            if hit:
                canvas[y][x] = outline_col

    for y in range(height):
        for x in range(width):
            if m[y][x]:
                canvas[y][x] = top_fill if y < split_y else bot_fill
    return canvas


class TileBankAllocator:
    """Fixed-size 4bpp tile bank deduplicator preserving non-target tiles and bank `nt`."""

    def __init__(self, tiles: List[TileGrid], free_indices: Iterable[int]) -> None:
        self.tiles = tiles
        free_list = sorted(set(free_indices))
        self.free_set = set(free_list)
        if 0 in self.free_set:
            raise ValueError("Tile 0 must always remain preserved as blank/transparent")
        self.free_queue: List[int] = list(free_list)
        self.allocated: List[int] = []
        self.lookup: Dict[TileKey, Tuple[int, bool, bool]] = {}

        # Index preserved tiles first so deduplication reuses e.g. blank tile 0 or solid fills
        for idx, grid in enumerate(self.tiles):
            if idx not in self.free_set:
                self._index_tile(idx, grid)

    def _index_tile(self, idx: int, grid: Sequence[Sequence[int]]) -> None:
        for fy in (False, True):
            for fx in (False, True):
                k = flip_tile_grid(grid, hflip=fx, vflip=fy)
                if k not in self.lookup:
                    self.lookup[k] = (idx, fx, fy)

    def get_or_alloc_word(
        self,
        cell: Sequence[Sequence[int]],
        pal: int,
        allow_flip: bool = True,
    ) -> int:
        """Return tilemap word for `cell`, reusing an existing/flipped tile or allocating from free pool."""
        key = tuple(tuple(v & 0xF for v in row) for row in cell)
        if allow_flip and key in self.lookup:
            tidx, fx, fy = self.lookup[key]
            return tilemap_word(tidx, pal, hflip=fx, vflip=fy)

        if not allow_flip:
            # Check exact unflipped match only
            for idx, grid in enumerate(self.tiles):
                if idx in self.free_set and idx not in self.allocated:
                    continue
                if tuple(tuple(r) for r in grid) == key:
                    return tilemap_word(idx, pal)

        if not self.free_queue:
            raise RuntimeError(
                f"Tile bank exhausted! All {len(self.allocated)} free tiles used."
            )
        tidx = self.free_queue.pop(0)
        self.allocated.append(tidx)
        self.tiles[tidx] = [list(r) for r in key]
        self._index_tile(tidx, key)
        return tilemap_word(tidx, pal, hflip=False, vflip=False)


def slice_canvas_to_words(
    canvas: Sequence[Sequence[int]],
    pal: int,
    allocator: TileBankAllocator,
    allow_flip: bool = True,
    zero_word_if_all: Optional[int] = None,
) -> List[int]:
    """Slice a `(H*8) x (W*8)` canvas row-major into PPU tilemap words."""
    h = len(canvas)
    w = len(canvas[0]) if h else 0
    if (h % 8) or (w % 8):
        raise ValueError(f"Canvas {w}x{h} must be multiple of 8x8 tiles")
    rows, cols = h // 8, w // 8
    words: List[int] = []
    for r in range(rows):
        for c in range(cols):
            cell = [canvas[r * 8 + py][c * 8 : c * 8 + 8] for py in range(8)]
            if zero_word_if_all is not None and all(
                v == zero_word_if_all for row in cell for v in row
            ):
                # Map pure background tiles cleanly to index 0 when requested
                words.append(0 if zero_word_if_all == 0 else tilemap_word(0, pal))
            else:
                words.append(allocator.get_or_alloc_word(cell, pal, allow_flip=allow_flip))
    return words


def scale_mask(mask: Grid2D, sx: float = 1.0, sy: float = 1.0) -> Grid2D:
    """Scale a 2D binary glyph mask horizontally and vertically via nearest-neighbor."""
    sh = len(mask)
    sw = len(mask[0]) if sh else 0
    if sh == 0 or sw == 0:
        return [[0]]
    nw = max(1, int(round(sw * sx)))
    nh = max(1, int(round(sh * sy)))
    return [
        [mask[min(sh - 1, int(y / sy))][min(sw - 1, int(x / sx))] for x in range(nw)]
        for y in range(nh)
    ]


def stamp_styled_text(
    canvas: Grid2D,
    cx: int,
    cy: int,
    mask: Grid2D,
    fill_top: int,
    fill_bot: int,
    outline_col: int,
    shadow_col: Optional[int] = None,
    gradient_split_frac: float = 0.55,
    thick_shadow: bool = False,
) -> None:
    """Center a 2D binary mask at (cx, cy) on `canvas` with outline/shadow and vertical gradient."""
    ch = len(canvas)
    cw = len(canvas[0]) if ch else 0
    mh = len(mask)
    mw = len(mask[0]) if mh else 0
    ox = cx - (mw // 2)
    oy = cy - (mh // 2)
    split_y = max(1, int(round(mh * gradient_split_frac)))

    if shadow_col is not None:
        offsets = ((1, 1), (2, 2)) if thick_shadow else ((1, 1),)
        for sdx, sdy in offsets:
            for my in range(mh):
                for mx in range(mw):
                    if not mask[my][mx]:
                        continue
                    px = ox + mx + sdx
                    py = oy + my + sdy
                    if 0 <= px < cw and 0 <= py < ch:
                        canvas[py][px] = shadow_col

    # 8-neighbor outline
    for my in range(mh):
        for mx in range(mw):
            if not mask[my][mx]:
                continue
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    px = ox + mx + dx
                    py = oy + my + dy
                    if 0 <= px < cw and 0 <= py < ch:
                        canvas[py][px] = outline_col

    # Fill
    for my in range(mh):
        col = fill_top if my < split_y else fill_bot
        for mx in range(mw):
            if mask[my][mx]:
                px = ox + mx
                py = oy + my
                if 0 <= px < cw and 0 <= py < ch:
                    canvas[py][px] = col


def render_canvas_to_words(
    canvas: Sequence[Sequence[int]],
    pal: int,
    tiles: List[TileGrid],
    reusable_indices: Iterable[int],
    allow_grow: bool = True,
    zero_if_all: Optional[int] = 0,
    max_tiles: int = 1024,
) -> List[int]:
    """Slice a canvas into 8x8 tiles reusing `reusable_indices` first and optionally appending to `tiles`."""
    h = len(canvas)
    w = len(canvas[0]) if h else 0
    if (h % 8) or (w % 8):
        raise ValueError(f"Canvas {w}x{h} must be multiple of 8x8 tiles")

    free_q = [i for i in sorted(set(reusable_indices)) if i > 0]
    free_set = set(free_q)
    lookup: Dict[TileKey, Tuple[int, bool, bool]] = {}

    def index_tile(tidx: int, grid: Sequence[Sequence[int]]) -> None:
        for fy in (False, True):
            for fx in (False, True):
                k = flip_tile_grid(grid, hflip=fx, vflip=fy)
                if k not in lookup:
                    lookup[k] = (tidx, fx, fy)

    for idx, grid in enumerate(tiles):
        if idx not in free_set:
            index_tile(idx, grid)

    words: List[int] = []
    rows, cols = h // 8, w // 8
    for r in range(rows):
        for c in range(cols):
            cell = [list(canvas[r * 8 + py][c * 8 : c * 8 + 8]) for py in range(8)]
            if zero_if_all is not None and all(v == zero_if_all for row in cell for v in row):
                words.append(0 if zero_if_all == 0 else tilemap_word(0, pal))
                continue
            key = tuple(tuple(v & 0xF for v in row) for row in cell)
            if key in lookup:
                tidx, fx, fy = lookup[key]
                words.append(tilemap_word(tidx, pal, hflip=fx, vflip=fy))
                continue
            if free_q:
                tidx = free_q.pop(0)
                tiles[tidx] = cell
                index_tile(tidx, cell)
                words.append(tilemap_word(tidx, pal))
            elif allow_grow and len(tiles) < max_tiles:
                tidx = len(tiles)
                tiles.append(cell)
                index_tile(tidx, cell)
                words.append(tilemap_word(tidx, pal))
            else:
                raise RuntimeError(
                    f"Tile bank full (len={len(tiles)}, max={max_tiles}, reusable exhausted)"
                )
    return words
