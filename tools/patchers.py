"""Asset patchers for Formosa Duel (Super A'Can) English localization & VWF."""

import struct
from collections import Counter, deque
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from d222 import decompress_d222
from fd_rom import (
    SpaceAllocator,
    TileGrid,
    decode_tile_4bpp,
    decode_tiles_bytes,
    encode_tile_4bpp,
    relocate_bg_gfx,
    relocate_sprite_bank_gfx,
    tilemap_word,
)
from vwf import (
    BitmapFont,
    Grid2D,
    load_fonts,
    render_canvas_to_words,
    render_text_mask,
    scale_mask,
    stamp_styled_text,
)

# PPU sprite ysize table from emulator/src/video/ppu.cpp
YSIZES = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 16, 20, 22, 24, 26)


def _erase_outlined_glyphs(
    canvas: Grid2D,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    fill_cols: Set[int],
    outline_cols: Set[int],
    bg_col: int,
    preserve_neighbors: Set[int] = frozenset({3, 0}),
) -> None:
    """Erase glyphs in [x0, x1) x [y0, y1) to `bg_col` without touching container borders."""
    h = len(canvas)
    w = len(canvas[0]) if h else 0
    xa, xb = max(0, x0), min(w, x1)
    ya, yb = max(0, y0), min(h, y1)

    fill_pts: List[Tuple[int, int]] = []
    for y in range(ya, yb):
        for x in range(xa, xb):
            if canvas[y][x] in fill_cols:
                fill_pts.append((x, y))

    # Mark outline pixels near glyph fill (unless bordering preserve_neighbors like sky/transparent)
    outline_pts: Set[Tuple[int, int]] = set()
    for fx, fy in fill_pts:
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                nx, ny = fx + dx, fy + dy
                if xa <= nx < xb and ya <= ny < yb and canvas[ny][nx] in outline_cols:
                    adj_border = False
                    for bdy in (-1, 0, 1):
                        for bdx in (-1, 0, 1):
                            bx, by = nx + bdx, ny + bdy
                            if 0 <= bx < w and 0 <= by < h and canvas[by][bx] in preserve_neighbors:
                                adj_border = True
                                break
                        if adj_border:
                            break
                    if not adj_border:
                        outline_pts.add((nx, ny))

    for x, y in fill_pts:
        canvas[y][x] = bg_col
    for x, y in outline_pts:
        canvas[y][x] = bg_col


def _render_sub_canvas(
    tiles: Sequence[TileGrid],
    words: Sequence[int],
    m_idx: int,
    wt: int,
    ht: int,
    vt: int,
) -> Tuple[Grid2D, List[List[int]]]:
    """Render a sprite subregion `(wt*8) x (ht*8)` and return `(canvas, per_cell_pals)`."""
    canvas: Grid2D = [[0] * (wt * 8) for _ in range(ht * 8)]
    cell_pals: List[List[int]] = [[0] * wt for _ in range(ht)]
    for r in range(ht):
        for c in range(wt):
            w = words[m_idx + r * wt + c]
            cell_pals[r][c] = (w >> 12) & 0xF
            tidx = (w & 0x3FF) - vt
            vf = (w >> 10) & 1
            hf = (w >> 11) & 1
            if 0 <= tidx < len(tiles):
                t = tiles[tidx]
                for py in range(8):
                    sy = (7 - py) if vf else py
                    for px in range(8):
                        sx = (7 - px) if hf else px
                        canvas[r * 8 + py][c * 8 + px] = t[sy][sx]
    return canvas, cell_pals


def _write_sub_canvas(
    canvas: Grid2D,
    cell_pals: Sequence[Sequence[int]],
    default_pal: int,
    words: List[int],
    m_idx: int,
    wt: int,
    ht: int,
    vt: int,
    tiles: List[TileGrid],
    free_q: List[int],
    lookup: Dict[Tuple[Tuple[int, ...], ...], Tuple[int, bool, bool]],
    allow_grow: bool = True,
    max_local_tiles: int = 1024,
) -> None:
    """Slice modified canvas into tiles and write PPU words back into `words`."""
    from fd_rom import flip_tile_grid

    def index_tile(tidx: int, grid: Sequence[Sequence[int]]) -> None:
        for fy in (False, True):
            for fx in (False, True):
                k = flip_tile_grid(grid, hflip=fx, vflip=fy)
                if k not in lookup:
                    lookup[k] = (tidx, fx, fy)

    for r in range(ht):
        for c in range(wt):
            orig_w = words[m_idx + r * wt + c]
            pal = cell_pals[r][c] if orig_w != 0 else default_pal
            cell = [list(canvas[r * 8 + py][c * 8 : c * 8 + 8]) for py in range(8)]
            if orig_w == 0 and all(v == 0 for row in cell for v in row):
                words[m_idx + r * wt + c] = 0
                continue
            key = tuple(tuple(v & 0xF for v in row) for row in cell)
            if key in lookup:
                tidx, fx, fy = lookup[key]
                words[m_idx + r * wt + c] = tilemap_word(vt + tidx, pal, hflip=fx, vflip=fy)
                continue
            if free_q:
                tidx = free_q.pop(0)
                tiles[tidx] = cell
                index_tile(tidx, cell)
                words[m_idx + r * wt + c] = tilemap_word(vt + tidx, pal)
            elif allow_grow and (vt + len(tiles)) < max_local_tiles:
                tidx = len(tiles)
                tiles.append(cell)
                index_tile(tidx, cell)
                words[m_idx + r * wt + c] = tilemap_word(vt + tidx, pal)
            else:
                raise RuntimeError(
                    f"Sprite tile bank exhausted at vt={vt:#x}, nt={len(tiles)}"
                )


def _load_sprite_bank(rom: bytearray, bank_off: int, compressed: bool = True):
    vt, vm, gp, np_, mp, nwp = struct.unpack_from(">HHIIII", rom, bank_off)
    nt = struct.unpack_from(">H", rom, np_)[0]
    nw = struct.unpack_from(">H", rom, nwp)[0]
    if compressed:
        dec, _ = decompress_d222(rom, gp)
        assert len(dec) == nt * 32
    else:
        dec = bytes(rom[gp : gp + nt * 32])
    tiles = decode_tiles_bytes(dec)
    words = list(struct.unpack_from(f">{nw}H", rom, mp))
    return vt, vm, gp, np_, mp, nw, tiles, words


def _init_tile_lookup_and_free(
    tiles: Sequence[TileGrid],
    words: Sequence[int],
    vt: int,
    modified_word_ranges: Sequence[Tuple[int, int]],
    extra_free: Iterable[int] = (),
):
    from fd_rom import flip_tile_grid

    mod_indices: Set[int] = set()
    for s, e in modified_word_ranges:
        mod_indices.update(range(s, e))

    used_outside: Set[int] = set()
    used_inside: Set[int] = set()
    for idx, w in enumerate(words):
        tidx = (w & 0x3FF) - vt
        if 0 <= tidx < len(tiles):
            if idx in mod_indices:
                used_inside.add(tidx)
            else:
                used_outside.add(tidx)

    # Preserve tile 0 so 0 words remain clean
    free_set = ((used_inside | set(extra_free)) - used_outside) - {0}
    free_q = sorted(free_set)
    lookup: Dict[Tuple[Tuple[int, ...], ...], Tuple[int, bool, bool]] = {}
    for tidx, grid in enumerate(tiles):
        if tidx not in free_set:
            for fy in (False, True):
                for fx in (False, True):
                    k = flip_tile_grid(grid, hflip=fx, vflip=fy)
                    if k not in lookup:
                        lookup[k] = (tidx, fx, fy)
    return free_q, lookup


def _load_bg(rom: bytearray, gfx_off: int, map_off: int):
    dec, _ = decompress_d222(rom, gfx_off)
    tiles = decode_tiles_bytes(dec)
    words = list(struct.unpack_from(">1200H", rom, map_off))
    return tiles, words


def _render_bg_canvas(tiles: Sequence[TileGrid], words: Sequence[int]) -> Tuple[Grid2D, List[List[int]]]:
    canvas: Grid2D = [[0] * 320 for _ in range(240)]
    pals: List[List[int]] = [[0] * 40 for _ in range(30)]
    for r in range(30):
        for c in range(40):
            w = words[r * 40 + c]
            pals[r][c] = (w >> 12) & 0xF
            tidx = w & 0x3FF
            vf = (w >> 10) & 1
            hf = (w >> 11) & 1
            t = tiles[tidx] if tidx < len(tiles) else [[0] * 8] * 8
            for py in range(8):
                sy = (7 - py) if vf else py
                for px in range(8):
                    sx = (7 - px) if hf else px
                    canvas[r * 8 + py][c * 8 + px] = t[sy][sx]
    return canvas, pals


def _save_bg_canvas(
    rom: bytearray,
    alloc: SpaceAllocator,
    canvas: Grid2D,
    pals: Sequence[Sequence[int]],
    ptr_site: int,
    nt_site: int,
    map_off: int,
    label: str,
) -> None:
    tiles: List[TileGrid] = [[[0] * 8 for _ in range(8)]]
    free_q, lookup = _init_tile_lookup_and_free(tiles, [], 0, [])
    new_words = [0] * 1200
    for r in range(30):
        for c in range(40):
            cell = [list(canvas[r * 8 + py][c * 8 : c * 8 + 8]) for py in range(8)]
            from fd_rom import flip_tile_grid

            key = tuple(tuple(v & 0xF for v in row) for row in cell)
            pal = pals[r][c]
            if key in lookup:
                tidx, fx, fy = lookup[key]
                new_words[r * 40 + c] = tilemap_word(tidx, pal, hflip=fx, vflip=fy)
            else:
                if len(tiles) >= 1024:
                    raise RuntimeError(f"BG {label} exceeded 1024 tile limit")
                tidx = len(tiles)
                tiles.append(cell)
                for fy in (False, True):
                    for fx in (False, True):
                        k = flip_tile_grid(cell, hflip=fx, vflip=fy)
                        if k not in lookup:
                            lookup[k] = (tidx, fx, fy)
                new_words[r * 40 + c] = tilemap_word(tidx, pal)

    struct.pack_into(">1200H", rom, map_off, *new_words)
    alloc.record_patch(map_off, map_off + 2400, f"{label}_map")
    relocate_bg_gfx(rom, alloc, ptr_site, nt_site, tiles, f"{label}_gfx")


# ---------------------------------------------------------------------------
# Individual asset patchers
# ---------------------------------------------------------------------------


def patch_title_balloons_and_start(
    rom: bytearray, alloc: SpaceAllocator, fonts: Dict[str, BitmapFont]
) -> Dict[str, str]:
    """Title0 (0x009C9C): Balloons 福爾摩沙 -> FO/R/MO/SA & Cloud 請按START鍵 -> PRESS START."""
    bank_off = 0x009C9C
    vt, vm, gp, np_, mp, nw, tiles, words = _load_sprite_bank(rom, bank_off, compressed=True)
    ranges = [(0, 96), (96, 168), (168, 232), (232, 304), (464, 576)]
    free_q, lookup = _init_tile_lookup_and_free(tiles, words, vt, ranges)

    # Render ALL sub-canvases before mutating `tiles` in place (tiles are shared across balloons)
    c0, p0 = _render_sub_canvas(tiles, words, 0, 8, 12, vt)
    c1, p1 = _render_sub_canvas(tiles, words, 96, 8, 9, vt)
    c2, p2 = _render_sub_canvas(tiles, words, 168, 8, 8, vt)
    c3, p3 = _render_sub_canvas(tiles, words, 232, 8, 9, vt)
    c5, p5 = _render_sub_canvas(tiles, words, 464, 16, 7, vt)

    # 1. Green balloon F0 (沙 -> SA)
    _erase_outlined_glyphs(c0, 12, 8, 59, 46, {0xC}, {0x6, 0x2}, 0xB, {0x0, 0x7})
    m_sa = scale_mask(render_text_mask(fonts["display"], "SA", tracking=1), 2.0, 2.0)
    stamp_styled_text(c0, 35, 26, m_sa, 0xC, 0xC, 0x6)

    # 2. Red balloon F1 (福 -> FO)
    _erase_outlined_glyphs(c1, 5, 8, 44, 46, {0xC}, {0x6}, 0x4, {0x0, 0x7})
    m_fo = scale_mask(render_text_mask(fonts["display"], "FO", tracking=1), 2.0, 2.0)
    stamp_styled_text(c1, 24, 24, m_fo, 0xC, 0xC, 0x6)

    # 3. Orange balloon F2 (摩 -> MO)
    _erase_outlined_glyphs(c2, 5, 7, 44, 45, {0xC}, {0x6}, 0x8, {0x0, 0x7})
    m_mo = scale_mask(render_text_mask(fonts["display"], "MO", tracking=1), 1.85, 2.0)
    stamp_styled_text(c2, 24, 23, m_mo, 0xC, 0xC, 0x6)

    # 4. Blue balloon F3 (爾 -> R)
    _erase_outlined_glyphs(c3, 6, 8, 44, 46, {0xC, 0xE}, {0x6}, 0x3, {0x0, 0x7})
    m_r = scale_mask(render_text_mask(fonts["display"], "R", tracking=1), 2.2, 2.2)
    stamp_styled_text(c3, 24, 26, m_r, 0xC, 0xC, 0x6)

    # 5. Cloud banner F5 (請按START鍵 -> PRESS START): clear interior text + shadows cleanly
    for yy in range(28, 46):
        for xx in range(4, 126):
            if c5[yy][xx] in (0x7, 0x8, 0xD):
                c5[yy][xx] = 0x5
    _erase_outlined_glyphs(c5, 2, 26, 127, 48, {0xD, 0x8}, {0x7}, 0x5, {0x0, 0x3, 0xE})
    m_ps = scale_mask(render_text_mask(fonts["display"], "PRESS START", tracking=1, space_w=5), 1.0, 1.4)
    stamp_styled_text(c5, 65, 37, m_ps, 0x8, 0xD, 0x7)

    _write_sub_canvas(c0, p0, 4, words, 0, 8, 12, vt, tiles, free_q, lookup)
    _write_sub_canvas(c1, p1, 4, words, 96, 8, 9, vt, tiles, free_q, lookup)
    _write_sub_canvas(c2, p2, 4, words, 168, 8, 8, vt, tiles, free_q, lookup)
    _write_sub_canvas(c3, p3, 4, words, 232, 8, 9, vt, tiles, free_q, lookup)
    _write_sub_canvas(c5, p5, 4, words, 464, 16, 7, vt, tiles, free_q, lookup)

    struct.pack_into(f">{nw}H", rom, mp, *words)
    alloc.record_patch(mp, mp + nw * 2, "Title0_map")
    relocate_sprite_bank_gfx(rom, alloc, bank_off, tiles, "Title0_gfx")
    return {
        "沙 / 福 / 摩 / 爾": "SA / FO / MO / R",
        "請按START鍵": "PRESS START",
    }


def patch_title_duel(
    rom: bytearray, alloc: SpaceAllocator, fonts: Dict[str, BitmapFont]
) -> Dict[str, str]:
    """Title2 (0x009CC4): F41..F43 大對決 -> DUEL."""
    bank_off = 0x009CC4
    vt, vm, gp, np_, mp, nw, tiles, words = _load_sprite_bank(rom, bank_off, compressed=True)
    ranges = [(120, 152), (152, 184), (184, 216)]
    free_q, lookup = _init_tile_lookup_and_free(tiles, words, vt, ranges)

    # Adjust y-offsets in sub-descriptors (0x0E6A7E, 0x0E6A88, 0x0E6A92) to align evenly at y=0
    for sub_off in (0x0E6A7E, 0x0E6A88, 0x0E6A92):
        w0 = struct.unpack_from(">H", rom, sub_off)[0]
        struct.pack_into(">H", rom, sub_off, w0 & ~0x1FF)
    alloc.record_patch(0x0E6A7E, 0x0E6A9C, "Title2_duel_sub_align")

    # Live OAM places F41, F42, F43 at x=163, 212, 262 (each 48px visible across cols 0..5 = 147px total)
    duel_canvas: Grid2D = [[0] * 147 for _ in range(32)]
    m_duel = scale_mask(render_text_mask(fonts["display"], "DUEL", tracking=5), 2.7, 2.3)
    stamp_styled_text(duel_canvas, 73, 16, m_duel, 0xD, 0xD, 0x7, shadow_col=0x4, thick_shadow=True)

    slices_x = (0, 49, 99)
    for m_off, x_src in zip((120, 152, 184), slices_x):
        sub_c: Grid2D = [[0] * 64 for _ in range(32)]
        for y in range(32):
            sub_c[y][:48] = duel_canvas[y][x_src : x_src + 48]
        pals = [[4, 4, 4, 4, 4, 4, 0, 0] for _ in range(4)]
        _write_sub_canvas(sub_c, pals, 4, words, m_off, 8, 4, vt, tiles, free_q, lookup)

    struct.pack_into(f">{nw}H", rom, mp, *words)
    alloc.record_patch(mp, mp + nw * 2, "Title2_map")
    relocate_sprite_bank_gfx(rom, alloc, bank_off, tiles, "Title2_gfx")
    return {"大對決": "DUEL"}


def patch_main_menu_options(
    rom: bytearray, alloc: SpaceAllocator, fonts: Dict[str, BitmapFont]
) -> Dict[str, str]:
    """Menu1 (0x009D80): F4 設定選擇 -> OPTIONS."""
    bank_off = 0x009D80
    vt, vm, gp, np_, mp, nw, tiles, words = _load_sprite_bank(rom, bank_off, compressed=True)
    ranges = [(62, 126)]
    free_q, lookup = _init_tile_lookup_and_free(tiles, words, vt, ranges)

    c4: Grid2D = [[0] * 128 for _ in range(32)]
    p4 = [[5] * 16 for _ in range(4)]
    m_opt = scale_mask(render_text_mask(fonts["display"], "OPTIONS", tracking=2), 1.75, 2.1)
    stamp_styled_text(c4, 64, 16, m_opt, 0xC, 0x7, 0xD, shadow_col=0xB, thick_shadow=True)
    _write_sub_canvas(c4, p4, 5, words, 62, 16, 4, vt, tiles, free_q, lookup)

    struct.pack_into(f">{nw}H", rom, mp, *words)
    alloc.record_patch(mp, mp + nw * 2, "Menu1_map")
    relocate_sprite_bank_gfx(rom, alloc, bank_off, tiles, "Menu1_gfx")
    return {"設定選擇": "OPTIONS"}


def patch_options_eggs(
    rom: bytearray, alloc: SpaceAllocator, fonts: Dict[str, BitmapFont]
) -> Dict[str, str]:
    """Menu0 (0x009D6C): Options egg labels & Difficulty E/H indicators."""
    bank_off = 0x009D6C
    vt, vm, gp, np_, mp, nw, tiles, words = _load_sprite_bank(rom, bank_off, compressed=True)
    ranges = [
        (64, 68),
        (68, 72),
        (72, 76),
        (76, 80),
        (144, 148),
        (148, 152),
        (152, 156),
        (156, 160),
        (240, 288),
    ]
    free_q, lookup = _init_tile_lookup_and_free(tiles, words, vt, ranges)

    # Render DiffBar BEFORE egg_labels mutates shared tiles in `free_q`
    c_diff, p_diff = _render_sub_canvas(tiles, words, 240, 16, 3, vt)

    egg_labels = [
        (64, "LVL", 0xC),
        (68, "SET", 0xC),
        (72, "KEY", 0xC),
        (76, "SET", 0xC),
        (144, "RND", 0xB),
        (148, "SET", 0xB),
        (152, "CLR", 0xB),
        (156, "SET", 0xB),
    ]
    for m_off, txt, fill_col in egg_labels:
        c: Grid2D = [[0] * 16 for _ in range(16)]
        pals = [[8, 8], [8, 8]]
        mask = render_text_mask(fonts["body"], txt, tracking=0)
        # Fit within 14px width so 1px outline/shadow stays unclipped inside 16x16
        sx = min(1.0, 13.5 / max(1, len(mask[0])))
        mask = scale_mask(mask, sx, 1.2)
        stamp_styled_text(c, 8, 8, mask, fill_col, fill_col, 0xF, shadow_col=0xD)
        _write_sub_canvas(c, pals, 8, words, m_off, 2, 2, vt, tiles, free_q, lookup)

    # Difficulty bar: replace 易 and 難 with E and H while preserving middle bar pals
    _erase_outlined_glyphs(c_diff, 3, 4, 17, 18, {0xD}, set(), 0x3, set())
    _erase_outlined_glyphs(c_diff, 72, 4, 87, 18, {0xD}, set(), 0x3, set())
    m_e = scale_mask(render_text_mask(fonts["display"], "E"), 1.0, 1.0)
    m_h = scale_mask(render_text_mask(fonts["display"], "H"), 1.0, 1.0)
    stamp_styled_text(c_diff, 9, 11, m_e, 0xD, 0xD, 0x3)
    stamp_styled_text(c_diff, 79, 11, m_h, 0xD, 0xD, 0x3)
    _write_sub_canvas(c_diff, p_diff, 6, words, 240, 16, 3, vt, tiles, free_q, lookup)

    struct.pack_into(f">{nw}H", rom, mp, *words)
    alloc.record_patch(mp, mp + nw * 2, "Menu0_map")
    relocate_sprite_bank_gfx(rom, alloc, bank_off, tiles, "Menu0_gfx")
    return {
        "難度": "LVL SET",
        "按鍵": "KEY SET",
        "回合": "RND SET",
        "顏色": "CLR SET",
        "易 / 難": "E / H",
    }


def patch_char_select_names(
    rom: bytearray, alloc: SpaceAllocator, fonts: Dict[str, BitmapFont]
) -> Dict[str, str]:
    """Select0 (0x009D56): 若望保祿 -> John Paul, 呂不敗 -> Lady Lu."""
    bank_off = 0x009D56
    vt, vm, gp, np_, mp, nw, tiles, words = _load_sprite_bank(rom, bank_off, compressed=True)
    ranges = [(288, 304), (624, 656)]
    free_q, lookup = _init_tile_lookup_and_free(tiles, words, vt, ranges)

    # F2/F8: John Paul (8x2 = 64x16)
    c_jp: Grid2D = [[0] * 64 for _ in range(16)]
    p_jp = [[4] * 8 for _ in range(2)]
    m_jp = scale_mask(
        render_text_mask(fonts["body"], "John Paul", tracking=1, space_w=3), 1.0, 1.2
    )
    stamp_styled_text(c_jp, 32, 8, m_jp, 0xB, 0xB, 0xC)
    _write_sub_canvas(c_jp, p_jp, 4, words, 288, 8, 2, vt, tiles, free_q, lookup)

    # F4/F10: Lady Lu centered at x=25 to match original plaque center (x=14 + 25 = 39px)
    c_lu: Grid2D = [[0] * 128 for _ in range(16)]
    p_lu = [[4] * 8 + [0] * 8 for _ in range(2)]
    m_lu = scale_mask(
        render_text_mask(fonts["body"], "Lady Lu", tracking=1, space_w=4), 1.0, 1.2
    )
    stamp_styled_text(c_lu, 25, 8, m_lu, 0xB, 0xB, 0xC)
    _write_sub_canvas(c_lu, p_lu, 4, words, 624, 16, 2, vt, tiles, free_q, lookup)

    struct.pack_into(f">{nw}H", rom, mp, *words)
    alloc.record_patch(mp, mp + nw * 2, "Select0_map")
    relocate_sprite_bank_gfx(rom, alloc, bank_off, tiles, "Select0_gfx")
    return {"若望保祿": "John Paul", "呂不敗": "Lady Lu"}


def patch_chain_popups(
    rom: bytearray, alloc: SpaceAllocator, fonts: Dict[str, BitmapFont]
) -> Dict[str, str]:
    """Game2 (0x009D16): 連鎖 -> CHAIN across all 24 chain popups via shared s1 tiles."""
    bank_off = 0x009D16
    vt, vm, gp, np_, mp, nw, tiles, words = _load_sprite_bank(rom, bank_off, compressed=True)
    # 連鎖 s1 is m_idx=8..24 (4x4, words: 515A 515B 515C 0140 / 5164 5165 5166 0140 / 5167 5168 5169 0140 / 5171 5172 5173 0140)
    # Let's include m_idx=8..24 AND check if all 24 frames share that single s1 (they all reference m_idx=8..24? No, let's check all occurrences of s1 in map!)
    # Earlier dump showed F0,F1,F2,F3 s1 ALL point to m=57352..57368 (m_idx=8..24)!
    ranges = [(8, 24)]
    free_q, lookup = _init_tile_lookup_and_free(tiles, words, vt, ranges)

    c_ch: Grid2D = [[0] * 32 for _ in range(32)]
    p_ch = [[5, 5, 5, 5] for _ in range(4)]
    m_ch = render_text_mask(fonts["body"], "CHAIN", tracking=1)
    m_ch = scale_mask(m_ch, 1.0, 1.5)
    stamp_styled_text(c_ch, 16, 10, m_ch, 0x1, 0x1, 0xC)
    _write_sub_canvas(c_ch, p_ch, 5, words, 8, 4, 4, vt, tiles, free_q, lookup)

    struct.pack_into(f">{nw}H", rom, mp, *words)
    alloc.record_patch(mp, mp + nw * 2, "Game2_map")
    relocate_sprite_bank_gfx(rom, alloc, bank_off, tiles, "Game2_gfx")
    return {"連鎖": "CHAIN"}


def patch_continue_prompt(
    rom: bytearray, alloc: SpaceAllocator, fonts: Dict[str, BitmapFont]
) -> Dict[str, str]:
    """DMA0 (0x009D2C): Uncompressed in-place patch of 繼續嗎 -> CONTINUE."""
    bank_off = 0x009D2C
    vt, vm, gp, np_, mp, nw, tiles, words = _load_sprite_bank(rom, bank_off, compressed=False)
    # s1 at m_idx=192..216 (8x3 = 64x24) uses 16 dedicated tiles 0x71..0x80 and 8 zero words
    c_cont: Grid2D = [[0] * 64 for _ in range(16)]
    m_cont = render_text_mask(fonts["display"], "CONTINUE", tracking=1)
    stamp_styled_text(c_cont, 32, 8, m_cont, 0x9, 0x9, 0xC)

    # Write 16 tiles directly into tiles[0x71..0x80]
    for r in range(2):
        for c in range(8):
            cell = [list(c_cont[r * 8 + py][c * 8 : c * 8 + 8]) for py in range(8)]
            tidx = 0x71 + r * 8 + c
            tiles[tidx] = cell
            rom[gp + tidx * 32 : gp + (tidx + 1) * 32] = encode_tile_4bpp(cell)

    # Update m_idx=192..216: top row 0000, bottom 2 rows linear 5071..5080
    new_s1 = [0x0000] * 8 + [tilemap_word(0x71 + i, 5) for i in range(16)]
    for i, w in enumerate(new_s1):
        words[192 + i] = w

    struct.pack_into(f">{nw}H", rom, mp, *words)
    alloc.record_patch(gp + 0x71 * 32, gp + 0x81 * 32, "DMA0_gfx_continue")
    alloc.record_patch(mp + 192 * 2, mp + 216 * 2, "DMA0_map_continue")
    return {"繼續嗎": "CONTINUE"}


def patch_win_badge(
    rom: bytearray, alloc: SpaceAllocator, fonts: Dict[str, BitmapFont]
) -> Dict[str, str]:
    """DMA1 (0x009D40): Uncompressed in-place patch of F16 勝 badge -> WIN."""
    bank_off = 0x009D40
    vt, vm, gp, np_, mp, nw, tiles, words = _load_sprite_bank(rom, bank_off, compressed=False)
    # Use unused 敗 tiles (86..121) + unshared tiles from m_idx=72..144, keep nt=122 fixed!
    free_q, lookup = _init_tile_lookup_and_free(
        tiles, words, vt, [(72, 144)], extra_free=range(86, len(tiles))
    )

    c_win, p_win = _render_sub_canvas(tiles, words, 72, 8, 9, vt)
    _erase_outlined_glyphs(c_win, 5, 16, 46, 51, {0xB}, {0x8}, 0xA, {0x0, 0x7, 0xC, 0xF})
    m_w = scale_mask(render_text_mask(fonts["display"], "WIN", tracking=1), 1.4, 2.0)
    stamp_styled_text(c_win, 25, 33, m_w, 0xB, 0xB, 0x8)

    _write_sub_canvas(
        c_win,
        p_win,
        5,
        words,
        72,
        8,
        9,
        vt,
        tiles,
        free_q,
        lookup,
        allow_grow=False,
    )

    # Write modified tiles (only inside 0..121) & map back in-place
    for tidx, t in enumerate(tiles):
        rom[gp + tidx * 32 : gp + (tidx + 1) * 32] = encode_tile_4bpp(t)
    struct.pack_into(f">{nw}H", rom, mp, *words)
    alloc.record_patch(gp, gp + len(tiles) * 32, "DMA1_gfx_win")
    alloc.record_patch(mp, mp + nw * 2, "DMA1_map_win")
    return {"勝 (badge)": "WIN"}


def patch_stage_banners_and_rematch(
    rom: bytearray, alloc: SpaceAllocator, fonts: Dict[str, BitmapFont]
) -> Dict[str, str]:
    """State 18 uncompressed banners: 第一回..第九回 -> STAGE 1..STAGE 9, 再戰 -> REMATCH."""
    res: Dict[str, str] = {}
    for i in range(9):
        map_ptr = struct.unpack_from(">I", rom, 0x017388 + i * 4)[0]
        nt_ptr = struct.unpack_from(">I", rom, 0x0173AC + i * 4)[0]
        gfx_ptr = struct.unpack_from(">I", rom, 0x0173D0 + i * 4)[0]
        max_nt = struct.unpack_from(">H", rom, nt_ptr)[0]

        canvas: Grid2D = [[0] * 112 for _ in range(32)]
        label = f"STAGE {i + 1}"
        mask = scale_mask(render_text_mask(fonts["display"], label, tracking=1, space_w=4), 1.35, 2.2)
        stamp_styled_text(canvas, 56, 16, mask, 0x8, 0x7, 0x5, shadow_col=0x6, thick_shadow=True)

        # Deduplicate into <= max_nt tiles
        tiles: List[TileGrid] = [[[0] * 8 for _ in range(8)]]
        free_q, lookup = _init_tile_lookup_and_free(tiles, [], 0, [])
        pals = [[4] * 14 for _ in range(4)]
        words = [0] * 56
        _write_sub_canvas(
            canvas, pals, 4, words, 0, 14, 4, 0, tiles, free_q, lookup, allow_grow=True, max_local_tiles=max_nt
        )
        assert len(tiles) <= max_nt, f"Stage {i+1} tiles {len(tiles)} > {max_nt}"
        for tidx, t in enumerate(tiles):
            rom[gfx_ptr + tidx * 32 : gfx_ptr + (tidx + 1) * 32] = encode_tile_4bpp(t)
        struct.pack_into(">56H", rom, map_ptr, *words)
        struct.pack_into(">H", rom, nt_ptr, len(tiles))
        alloc.record_patch(map_ptr, map_ptr + 112, f"Stage{i+1}_map")
        alloc.record_patch(gfx_ptr, gfx_ptr + len(tiles) * 32, f"Stage{i+1}_gfx")
        alloc.record_patch(nt_ptr, nt_ptr + 2, f"Stage{i+1}_nt")
        res[f"第{i+1}回"] = label

    # REMATCH banner: 0x02C412 (map, 32w), 0x02C452 (nt=33), 0x02C454 (gfx)
    rem_max_nt = struct.unpack_from(">H", rom, 0x02C452)[0]
    c_rem: Grid2D = [[0] * 64 for _ in range(32)]
    m_rem = scale_mask(render_text_mask(fonts["body"], "REMATCH", tracking=1), 1.3, 2.1)
    stamp_styled_text(c_rem, 32, 16, m_rem, 0x4, 0x3, 0x1, shadow_col=0x2)
    tiles_r: List[TileGrid] = [[[0] * 8 for _ in range(8)]]
    _, lookup_r = _init_tile_lookup_and_free(tiles_r, [], 0, [])
    words_r = [0] * 32
    _write_sub_canvas(
        c_rem,
        [[3] * 8 for _ in range(4)],
        3,
        words_r,
        0,
        8,
        4,
        0,
        tiles_r,
        [],
        lookup_r,
        allow_grow=True,
        max_local_tiles=rem_max_nt,
    )
    assert len(tiles_r) <= rem_max_nt, f"REMATCH tiles {len(tiles_r)} > {rem_max_nt}"
    for tidx, t in enumerate(tiles_r):
        rom[0x02C454 + tidx * 32 : 0x02C454 + (tidx + 1) * 32] = encode_tile_4bpp(t)
    struct.pack_into(">32H", rom, 0x02C412, *words_r)
    struct.pack_into(">H", rom, 0x02C452, len(tiles_r))
    alloc.record_patch(0x02C412, 0x02C454 + len(tiles_r) * 32, "Rematch_banner")
    res["再戰"] = "REMATCH"
    return res


def patch_balloon_bgs(
    rom: bytearray, alloc: SpaceAllocator, fonts: Dict[str, BitmapFont]
) -> Dict[str, str]:
    """Wallpaper BGs for State 8A (2P Select), State 8B (1P Select), State 18 (VS): 福爾摩沙 -> FO/R/MO/SA."""
    targets = [
        ("BG_8A", 0x01DD72, 0x01D410, 0x00537C, 0x0053A0),
        ("BG_8B", 0x01BEC0, 0x01B55E, 0x00547E, 0x0054A2),
        ("BG_18", 0x029E16, 0x02B6E8, 0x00388C, 0x0038B0),
    ]
    col_to_text = {
        0xB: "FO",
        0x5: "R",
        0x4: "MO",
        0x9: "SA",
        0xD: "SA",
    }

    for name, gfx_off, map_off, ptr_site, nt_site in targets:
        tiles, words = _load_bg(rom, gfx_off, map_off)
        canvas, pals = _render_bg_canvas(tiles, words)

        # Find connected components of white text/outline pixels (1, 0xE) bridging up to 3px gaps
        vis = [[False] * 320 for _ in range(240)]
        comps: List[Tuple[int, int, int, int, List[Tuple[int, int]], int]] = []
        for y in range(240):
            for x in range(320):
                if not vis[y][x] and canvas[y][x] in (1, 0xE):
                    q = deque([(x, y)])
                    vis[y][x] = True
                    pts: List[Tuple[int, int]] = []
                    while q:
                        cx, cy = q.popleft()
                        pts.append((cx, cy))
                        for dy in range(-3, 4):
                            for dx in range(-3, 4):
                                nx, ny = cx + dx, cy + dy
                                if 0 <= nx < 320 and 0 <= ny < 240 and not vis[ny][nx] and canvas[ny][nx] in (1, 0xE):
                                    vis[ny][nx] = True
                                    q.append((nx, ny))
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    x0, x1 = min(xs), max(xs)
                    y0, y1 = min(ys), max(ys)
                    bw, bh = x1 - x0 + 1, y1 - y0 + 1
                    on_edge = x0 == 0 or y0 == 0 or x1 == 319 or y1 == 239
                    if len(pts) >= (30 if on_edge else 55) and (bh >= 10 or on_edge):
                        bcols = Counter()
                        for px, py in pts:
                            for ddx, ddy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                                nx, ny = px + ddx, py + ddy
                                if 0 <= nx < 320 and 0 <= ny < 240:
                                    v = canvas[ny][nx]
                                    if v in col_to_text:
                                        bcols[v] += 1
                        dom = bcols.most_common(1)[0][0] if bcols else 0
                        if dom in col_to_text:
                            comps.append((x0, y0, bw, bh, pts, dom))

        for x0, y0, bw, bh, pts, dom in comps:
            fill_bg = 0x9 if dom == 0xD else dom
            for px, py in pts:
                canvas[py][px] = fill_bg
            lbl = col_to_text[dom]
            # Stamp text if enough balloon belly is visible on screen
            if bw >= 22 and bh >= 18:
                sx = 1.6 if lbl == "MO" else 1.8
                sy = 1.8
                mask = scale_mask(render_text_mask(fonts["display"], lbl, tracking=1), sx, sy)
                stamp_styled_text(canvas, x0 + bw // 2, y0 + bh // 2, mask, 0x1, 0x1, 0xE)

        _save_bg_canvas(rom, alloc, canvas, pals, ptr_site, nt_site, map_off, name)

    return {"Wallpaper Balloons (8A/8B/18)": "FO / R / MO / SA"}


def patch_gameplay_bgs(
    rom: bytearray, alloc: SpaceAllocator, fonts: Dict[str, BitmapFont]
) -> Dict[str, str]:
    """2P Gameplay BG (State 6A) & 1P Endless BG (State 6B)."""
    # 1. State 6A (2P VS)
    t6a, w6a = _load_bg(rom, 0x01670E, 0x015DAC)
    c6a, p6a = _render_bg_canvas(t6a, w6a)

    # Top cloud: 下一組 -> NEXT (clear interior 0x2/0x5/0x9 completely)
    for yy in range(10, 34):
        for xx in range(126, 195):
            if c6a[yy][xx] in (0x2, 0x5, 0x9):
                c6a[yy][xx] = 0x6
    _erase_outlined_glyphs(c6a, 125, 10, 195, 34, {0x2, 0x5}, {0x9}, 0x6, {0x0, 0x3, 0x7})
    m_next = scale_mask(render_text_mask(fonts["display"], "NEXT", tracking=1), 1.15, 1.35)
    stamp_styled_text(c6a, 160, 22, m_next, 0x2, 0x5, 0x9)

    # Middle center panel: 攻擊 ● 數 -> ATK ● CNT
    _erase_outlined_glyphs(c6a, 124, 80, 160, 98, {0x6}, {0x1}, 0x9, set())
    _erase_outlined_glyphs(c6a, 168, 80, 195, 98, {0x6}, {0x1}, 0x9, set())
    m_atk = render_text_mask(fonts["body"], "ATK", tracking=1)
    m_cnt = render_text_mask(fonts["body"], "CNT", tracking=1)
    stamp_styled_text(c6a, 136, 89, m_atk, 0x6, 0x6, 0x1)
    stamp_styled_text(c6a, 180, 89, m_cnt, 0x6, 0x6, 0x1)

    # Lower center panel: 勝   勝 -> WIN   WIN
    _erase_outlined_glyphs(c6a, 130, 137, 158, 153, {0x6}, {0x1}, 0x9, set())
    _erase_outlined_glyphs(c6a, 162, 137, 190, 153, {0x6}, {0x1}, 0x9, set())
    m_win_sm = render_text_mask(fonts["body"], "WIN", tracking=1)
    stamp_styled_text(c6a, 144, 144, m_win_sm, 0x6, 0x6, 0x1)
    stamp_styled_text(c6a, 176, 144, m_win_sm, 0x6, 0x6, 0x1)

    _save_bg_canvas(rom, alloc, c6a, p6a, 0x008E84, 0x008EA8, 0x015DAC, "BG_6A")

    # 2. State 6B (1P Endless)
    t6b, w6b = _load_bg(rom, 0x0134B4, 0x012B52)
    c6b, p6b = _render_bg_canvas(t6b, w6b)

    # Top cloud: 下一組 -> NEXT
    for yy in range(10, 34):
        for xx in range(126, 195):
            if c6b[yy][xx] in (0x2, 0x5, 0x9):
                c6b[yy][xx] = 0x6
    _erase_outlined_glyphs(c6b, 125, 10, 195, 34, {0x2, 0x5}, {0x9}, 0x6, {0x0, 0x3, 0x7})
    stamp_styled_text(c6b, 160, 22, m_next, 0x2, 0x5, 0x9)

    # Cloud 1: 等級 -> LEVEL (x=240..298, y=32..54)
    _erase_outlined_glyphs(c6b, 240, 32, 297, 55, {0x2, 0xF}, {0x1}, 0xA, {0x3, 0x5})
    m_lvl = scale_mask(render_text_mask(fonts["display"], "LEVEL", tracking=1), 1.15, 1.35)
    stamp_styled_text(c6b, 268, 43, m_lvl, 0xF, 0x2, 0x1)

    # Cloud 2: 得分 -> SCORE (x=236..298, y=87..109)
    _erase_outlined_glyphs(c6b, 236, 87, 298, 110, {0x7, 0xD}, {0x1}, 0xA, {0x3, 0x5})
    m_scr = scale_mask(render_text_mask(fonts["display"], "SCORE", tracking=1), 1.15, 1.35)
    stamp_styled_text(c6b, 267, 98, m_scr, 0xD, 0x7, 0x1)

    # Cloud 3: 最大連鎖數 -> MAX CHAIN (x=216..301, y=141..157)
    _erase_outlined_glyphs(c6b, 215, 141, 302, 158, {0xD}, {0x1}, 0xA, {0x3, 0x5})
    m_mc = render_text_mask(fonts["display"], "MAX CHAIN", tracking=1, space_w=4)
    stamp_styled_text(c6b, 258, 149, m_mc, 0xD, 0xD, 0x1)

    # Cloud 4: 最高得分數 -> TOP SCORE (x=216..301, y=188..205)
    _erase_outlined_glyphs(c6b, 215, 188, 302, 206, {0x6}, {0x1}, 0xA, {0x3, 0x5})
    m_ts = render_text_mask(fonts["display"], "TOP SCORE", tracking=1, space_w=4)
    stamp_styled_text(c6b, 258, 197, m_ts, 0x6, 0x6, 0x1)

    _save_bg_canvas(rom, alloc, c6b, p6b, 0x009032, 0x009056, 0x012B52, "BG_6B")

    return {
        "下一組": "NEXT",
        "攻擊 ● 數": "ATK ● CNT",
        "勝 (HUD)": "WIN",
        "等級": "LEVEL",
        "得分": "SCORE",
        "最大連鎖數": "MAX CHAIN",
        "最高得分數": "TOP SCORE",
    }
