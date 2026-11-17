"""Build the English-translated Formosa Duel (Super A'Can) ROM and IPS patch."""

import hashlib
import json
import sys
from pathlib import Path
from typing import Dict

TOOLS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TOOLS_DIR.parent
NOTES_DIR = ROOT_DIR / "notes"
BUILD_DIR = ROOT_DIR / "build"

sys.path.insert(0, str(TOOLS_DIR))

from fd_rom import (
    ORIG_ROM_PATH,
    ORIG_ROM_SIZE,
    SpaceAllocator,
    apply_ips,
    load_rom,
    save_rom,
    write_ips,
)
from patchers import (
    patch_balloon_bgs,
    patch_chain_popups,
    patch_char_select_names,
    patch_continue_prompt,
    patch_gameplay_bgs,
    patch_main_menu_options,
    patch_options_eggs,
    patch_stage_banners_and_rematch,
    patch_title_balloons_and_start,
    patch_title_duel,
    patch_win_badge,
)
from vwf import load_fonts

OUT_ROM_PATH = BUILD_DIR / "Formosa Duel (EN).bin"
OUT_IPS_PATH = BUILD_DIR / "Formosa Duel (EN).ips"
REPORT_PATH = BUILD_DIR / "report.txt"
TRANS_JSON_PATH = NOTES_DIR / "translations_en.json"
INVENTORY_MD_PATH = NOTES_DIR / "text_inventory.md"


def sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build() -> None:
    rom, orig_swapped = load_rom(ORIG_ROM_PATH, verify_hash=True)
    fonts = load_fonts(NOTES_DIR)
    alloc = SpaceAllocator()

    translations: Dict[str, str] = {}
    stages = [
        patch_title_balloons_and_start,
        patch_title_duel,
        patch_main_menu_options,
        patch_options_eggs,
        patch_char_select_names,
        patch_chain_popups,
        patch_continue_prompt,
        patch_win_badge,
        patch_stage_banners_and_rematch,
        patch_balloon_bgs,
        patch_gameplay_bgs,
    ]
    for fn in stages:
        res = fn(rom, alloc, fonts)
        translations.update(res)

    alloc.verify_non_overlapping()
    if len(rom) != ORIG_ROM_SIZE:
        raise AssertionError(f"Expected ROM size {ORIG_ROM_SIZE:#x}, got {len(rom):#x}")

    patched_swapped = save_rom(rom, OUT_ROM_PATH)
    write_ips(orig_swapped, patched_swapped, OUT_IPS_PATH)

    reapplied = apply_ips(orig_swapped, OUT_IPS_PATH)
    if reapplied != patched_swapped:
        raise AssertionError("IPS round-trip verification failed!")

    used_free = sum(end - start for start, end, _ in alloc.allocations)
    rem_free = alloc.remaining_bytes()

    report_lines = [
        "Formosa Duel (Taiwan) -> English Translation Build Report",
        "=" * 58,
        f"Source ROM:   {ORIG_ROM_PATH.name} ({len(orig_swapped)} bytes)",
        f"Source SHA1:  {sha1_hex(orig_swapped)}",
        f"Source SHA256:{sha256_hex(orig_swapped)}",
        f"Patched ROM:  {OUT_ROM_PATH.name} ({len(patched_swapped)} bytes)",
        f"Patched SHA1: {sha1_hex(patched_swapped)}",
        f"Patched SHA256:{sha256_hex(patched_swapped)}",
        f"IPS Patch:    {OUT_IPS_PATH.name} ({OUT_IPS_PATH.stat().st_size} bytes, round-trip VERIFIED)",
        "",
        f"Relocated compressed blocks in 0x0EF8A8..0x100000: {used_free} bytes used, {rem_free} bytes remaining",
        "-" * 58,
    ]
    for s, e, label in alloc.allocations:
        report_lines.append(f"  0x{s:06X}..0x{e:06X} ({e - s:5d} bytes)  {label}")
    report_lines.append("")
    report_lines.append("Fixed in-place patches:")
    report_lines.append("-" * 58)
    for s, e, label in alloc.fixed_patches:
        report_lines.append(f"  0x{s:06X}..0x{e:06X} ({e - s:5d} bytes)  {label}")
    report_lines.append("")
    report_lines.append(f"Translated entries ({len(translations)}):")
    report_lines.append("-" * 58)
    for k, v in translations.items():
        report_lines.append(f"  {k} -> {v}")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    TRANS_JSON_PATH.write_text(
        json.dumps(translations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    inv_lines = [
        "# Formosa Duel (Super A'Can) Text & Graphic Translation Inventory",
        "",
        "| Original (ZH-TW) | English | Asset / Screen |",
        "|---|---|---|",
        "| 沙 / 福 / 摩 / 爾 (Title Balloons) | `SA` / `FO` / `MO` / `R` | Title0 F0..F3 (`0x009C9C`) |",
        "| 請按START鍵 | `PRESS START` | Title0 F5 (`0x009C9C`) |",
        "| 大對決 | `DUEL` | Title2 F41..F43 (`0x009CC4`) |",
        "| 設定選擇 | `OPTIONS` | Menu1 F4 (`0x009D80`) |",
        "| 難度 / 按鍵 / 回合 / 顏色 | `LVL SET` / `KEY SET` / `RND SET` / `CLR SET` | Menu0 F4..F7 (`0x009D6C`) |",
        "| 易 / 難 | `E` / `H` | Menu0 DiffBar (`0x009D6C`) |",
        "| 若望保祿 / 呂不敗 | `John Paul` / `Lady Lu` | Select0 F2, F4, F8, F10 (`0x009D56`) |",
        "| 連鎖 | `CHAIN` | Game2 `s1` (`0x009D16`, m_idx=8..24) |",
        "| 繼續嗎 | `CONTINUE` | DMA0 `s1` (`0x009D2C`, uncompressed) |",
        "| 勝 (Badge) | `WIN` | DMA1 F16 (`0x009D40`, uncompressed) |",
        "| 第1回..第9回 | `STAGE 1`..`STAGE 9` | Uncompressed ROM (`0x017388..0x0173D0`) |",
        "| 再戰 | `REMATCH` | Uncompressed ROM (`0x02C412..0x02C454`) |",
        "| Wallpaper Balloons (福爾摩沙) | `FO` / `R` / `MO` / `SA` | BG 8A, 8B, 18 |",
        "| 下一組 | `NEXT` | BG 6A / 6B |",
        "| 攻擊 ● 數 | `ATK ● CNT` | BG 6A |",
        "| 勝 (HUD) | `WIN` | BG 6A |",
        "| 等級 | `LEVEL` | BG 6B |",
        "| 得分 | `SCORE` | BG 6B |",
        "| 最大連鎖數 | `MAX CHAIN` | BG 6B |",
        "| 最高得分數 | `TOP SCORE` | BG 6B |",
    ]
    INVENTORY_MD_PATH.write_text("\n".join(inv_lines) + "\n", encoding="utf-8")

    print(f"Build OK! Used {used_free} bytes, {rem_free} bytes remaining.")
    print(f"ROM: {OUT_ROM_PATH}")
    print(f"IPS: {OUT_IPS_PATH}")
    print(f"Report: {REPORT_PATH}")


if __name__ == "__main__":
    build()
