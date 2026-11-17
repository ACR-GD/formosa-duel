"""Decompressor and encoders matching Formosa Duel's $002B22 ($D222-family) routines."""

import struct
from typing import Optional, Tuple

MAX_DECOMP_SIZE = 0x9480  # WRAM $FC0800..$FC9C80 scratch buffer (38,016 bytes)

# Extracted directly from Formosa Duel (Taiwan).bin (unswapped BE):
DEFAULT_HUFF_OFFSET = 0x009A24  # 164 bytes ending at $003040 (a4 walks backward)
DEFAULT_DIST_OFFSET = 0x0098B2  # 256 bytes ending at $002F2A

# $002D1A table: sentinel bit positions -> reads (16 - b) stream bits after prefix byte
_SENTINEL_BITS = (15, 15, 14, 14, 14, 13, 13, 13, 13, 12, 12, 12, 11, 11, 11, 10)


def d2b22_header(buf: bytes, offset: int) -> Tuple[int, int]:
    """Emulate header read at $002B3A: return (mode, size)."""
    if offset + 4 > len(buf):
        raise ValueError(f"Offset 0x{offset:06X} out of range")
    mode = buf[offset]
    flag = buf[offset + 1]
    if mode < 0xF0 or flag != 0x00:
        raise ValueError(
            f"Invalid $002B22 header at 0x{offset:06X}: mode=0x{mode:02X} flag=0x{flag:02X}"
        )
    size = struct.unpack_from(">H", buf, offset + 2)[0]
    return mode, size


def decompress_d222(
    rom: bytes,
    offset: int,
    huff_offset: int = DEFAULT_HUFF_OFFSET,
    dist_offset: int = DEFAULT_DIST_OFFSET,
) -> Tuple[bytes, int]:
    """Exact Python implementation of Formosa Duel $002B22 (modes 0xF0 raw, 0xF1 RLE, 0xF3/0xF4 LZ+Huffman).

    Returns (decompressed_bytes, end_offset_in_rom).
    """
    mode, out_len = d2b22_header(rom, offset)
    pos = offset + 4
    if out_len == 0:
        return b"", pos

    if mode not in (0xF1, 0xF3, 0xF4):
        end = pos + out_len
        return bytes(rom[pos:end]), end

    if mode == 0xF1:
        out = bytearray()
        rem = out_len
        p = pos
        while rem > 0:
            ctrl = rom[p]
            p += 1
            if ctrl <= 0xBF:
                out.append(ctrl)
                rem -= 1
            else:
                cnt = (ctrl & 0x3F) or 1
                val = rom[p]
                p += 1
                out.extend((val,) * cnt)
                rem -= cnt
                if rem < 0:
                    raise ValueError(f"0xF1 RLE overshoot at 0x{offset:06X}: rem={rem}")
        return bytes(out), p

    if mode == 0xF3:
        huff_raw = rom[pos : pos + 164]
        p = pos + 164
    else:
        huff_raw = rom[huff_offset : huff_offset + 164]
        p = pos

    dist_tbl = rom[dist_offset : dist_offset + 256]

    buckets = [-1] * 16
    chain = [0] * 328
    word_idx = 327
    for b in reversed(huff_raw):
        lo = b & 0x0F
        hi = (b >> 4) & 0x0F
        chain[word_idx] = buckets[lo]
        buckets[lo] = word_idx
        word_idx -= 1
        chain[word_idx] = buckets[hi]
        buckets[hi] = word_idx
        word_idx -= 1

    tree = []
    next_node_word = 1
    rem_slots = 2
    for bitlen_idx in range(16):
        cur = buckets[bitlen_idx]
        while cur >= 0:
            tree.append(cur - 0x200)
            rem_slots -= 1
            cur = chain[cur]
        for _ in range(rem_slots):
            tree.append(next_node_word)
            next_node_word += 1
        rem_slots <<= 1
        if len(tree) >= 0x51C // 2:
            break

    out = bytearray()
    bit_buf = 0
    bit_cnt = 0

    def read_bit() -> int:
        nonlocal p, bit_buf, bit_cnt
        if bit_cnt == 0:
            bit_buf = rom[p]
            p += 1
            bit_cnt = 8
        b = (bit_buf >> 7) & 1
        bit_buf = (bit_buf << 1) & 0xFF
        bit_cnt -= 1
        return b

    while True:
        node = 0
        while True:
            val = tree[node * 2 + read_bit()]
            if val < 0:
                sym = val + 0x200
                break
            node = val

        if sym < 256:
            out.append(sym)
        elif sym < 266:
            sub = sym - 256
            if sub == 0:
                break
            elif sub == 1:
                out.append(out[-1])
            elif sub == 2:
                out.append(out[-2])
            elif sub == 3:
                out.append(out[-4])
            elif sub == 4:
                out.append(out[-29])
            elif sub == 5:
                out.append(out[-2])
                out.append(out[-2])
            elif sub == 6:
                for _ in range(4):
                    out.append(out[-4])
            elif sub == 7:
                for _ in range(8):
                    out.append(out[-8])
            elif sub == 8:
                out.append(out[-2])
                out.append((out[-2] - 1) & 0xFF)
            elif sub == 9:
                out.append(out[-2])
                out.append((out[-2] + 1) & 0xFF)
        else:
            match_len = sym - 266 + 3
            prefix = rom[p]
            p += 1
            hi_nib = prefix >> 4
            n_extra = 16 - _SENTINEL_BITS[hi_nib]
            val16 = prefix
            for _ in range(n_extra):
                val16 = (val16 << 1) | read_bit()
            dist = ((dist_tbl[prefix] << 6) | (val16 & 0x3F)) + 1
            for _ in range(match_len):
                out.append(out[-dist])

        if len(out) > out_len:
            raise ValueError(
                f"Decompressed stream exceeded out_len ({len(out)} > {out_len}) without EOS symbol 256"
            )

    if len(out) != out_len:
        raise ValueError(
            f"EOS symbol 256 reached at {len(out)} bytes, expected {out_len}"
        )
    return bytes(out), p


def encode_d222_raw(data: bytes) -> bytes:
    """Encode uncompressed block with 0xF0 header matching $002B22 raw fallback."""
    if len(data) == 0 or len(data) > MAX_DECOMP_SIZE or (len(data) & 1):
        raise ValueError(f"Invalid $002B22 payload length {len(data)}")
    return bytes([0xF0, 0x00]) + struct.pack(">H", len(data)) + data


def encode_d222_rle(data: bytes) -> bytes:
    """Encode using $002B22 mode 0xF1 RLE."""
    n = len(data)
    if n == 0 or n > MAX_DECOMP_SIZE or (n & 1):
        raise ValueError(f"Invalid $002B22 payload length {n}")
    out = bytearray([0xF1, 0x00])
    out.extend(struct.pack(">H", n))
    i = 0
    while i < n:
        v = data[i]
        run = 1
        while i + run < n and run < 63 and data[i + run] == v:
            run += 1
        if run >= 3 or (run >= 2 and v >= 0xC0):
            out.append(0xC0 | run)
            out.append(v)
            i += run
        elif v >= 0xC0:
            out.append(0xC1)
            out.append(v)
            i += 1
        else:
            out.append(v)
            i += 1
    return bytes(out)


def _length_limited_huff_nibbles(freqs: list[int], max_bits: int = 15) -> list[int]:
    """Compute canonical code-length nibbles (`bitlen - 1` in 0..max_bits-1) for 328 symbols."""
    n_syms = len(freqs)
    if n_syms != 328:
        raise ValueError(f"Expected 328 symbols, got {n_syms}")

    # Guarantee every symbol gets a positive weight so the full 328-leaf Kraft tree sums to 1.0
    weights = [max(1, int(f)) for f in freqs]

    # Package-Merge algorithm guarantees exact Kraft=1 tree with max depth <= max_bits
    items = [(w, (s,)) for s, w in sorted(((i, weights[i]) for i in range(n_syms)), key=lambda x: x[1])]
    coins = items
    for _ in range(max_bits - 1):
        packaged = []
        for i in range(0, len(coins) - 1, 2):
            w1, s1 = coins[i]
            w2, s2 = coins[i + 1]
            packaged.append((w1 + w2, s1 + s2))
        merged = sorted(items + packaged, key=lambda x: x[0])
        coins = merged

    bitlens = [0] * n_syms
    for _, syms in coins[: (2 * n_syms - 2)]:
        for s in syms:
            bitlens[s] += 1

    if any(b < 1 or b > max_bits for b in bitlens):
        raise ValueError(f"Invalid bit lengths produced: min={min(bitlens)} max={max(bitlens)}")
    return [b - 1 for b in bitlens]


def _extract_codes_from_nibbles(nibbles: list[int]) -> dict[int, tuple[int, int]]:
    """Build exact $0095B6 decoding tree from 328 nibbles and map sym -> (code, bitlen)."""
    huff_raw = bytes((nibbles[2 * i] << 4) | nibbles[2 * i + 1] for i in range(164))
    buckets = [-1] * 16
    chain = [0] * 328
    word_idx = 327
    for b in reversed(huff_raw):
        lo = b & 0x0F
        hi = (b >> 4) & 0x0F
        chain[word_idx] = buckets[lo]
        buckets[lo] = word_idx
        word_idx -= 1
        chain[word_idx] = buckets[hi]
        buckets[hi] = word_idx
        word_idx -= 1

    tree = []
    next_node_word = 1
    rem_slots = 2
    for bitlen_idx in range(16):
        cur = buckets[bitlen_idx]
        while cur >= 0:
            tree.append(cur - 0x200)
            rem_slots -= 1
            cur = chain[cur]
        for _ in range(rem_slots):
            tree.append(next_node_word)
            next_node_word += 1
        rem_slots <<= 1
        if len(tree) >= 0x51C // 2:
            break

    codes: dict[int, tuple[int, int]] = {}

    def walk(node: int, prefix: int, depth: int) -> None:
        for bit in (0, 1):
            val = tree[node * 2 + bit]
            ncode = (prefix << 1) | bit
            if val < 0:
                codes[val + 0x200] = (ncode, depth + 1)
            else:
                walk(val, ncode, depth + 1)

    walk(0, 0, 0)
    return codes


def encode_d222_lzhuff(data: bytes, dist_tbl: bytes) -> bytes:
    """Encode using $0095B6 mode 0xF3 (LZ77 + per-block canonical Huffman table)."""
    n = len(data)
    if n == 0 or n > MAX_DECOMP_SIZE or (n & 1):
        raise ValueError(f"Invalid $0095B6 payload length {n}")
    if len(dist_tbl) != 256:
        raise ValueError(f"Expected 256-byte distance table, got {len(dist_tbl)}")

    # Precompute dist - 1 (0..4095) -> (prefix, extra_val, n_extra)
    dist_enc: list[tuple[int, int, int]] = [(0, 0, 0)] * 4096
    filled = [False] * 4096
    for prefix in range(256):
        n_extra = 16 - _SENTINEL_BITS[prefix >> 4]
        hi = dist_tbl[prefix] << 6
        for extra in range(1 << n_extra):
            val16 = (prefix << n_extra) | extra
            d_m1 = hi | (val16 & 0x3F)
            if not filled[d_m1]:
                dist_enc[d_m1] = (prefix, extra, n_extra)
                filled[d_m1] = True

    # Greedy LZ77 + special short-repeat tokenization using hash chains of 3-byte keys
    tokens: list[tuple] = []
    head: dict[bytes, list[int]] = {}
    i = 0
    while i < n:
        # Check shortcut opcodes (257..265) and general 3..64-byte LZ matches
        best_len = 0
        best_tok: Optional[tuple] = None

        if i >= 8 and i + 8 <= n and data[i : i + 8] == data[i - 8 : i]:
            best_len = 8
            best_tok = ("sym", 263)
        if best_len < 4 and i >= 4 and i + 4 <= n and data[i : i + 4] == data[i - 4 : i]:
            best_len = 4
            best_tok = ("sym", 262)

        if i + 3 <= n:
            key = data[i : i + 3]
            cand_list = head.get(key)
            if cand_list:
                # Inspect up to 64 recent positions within 4096-byte window
                for pos in reversed(cand_list[-64:]):
                    d = i - pos
                    if d > 4096:
                        break
                    ml = 3
                    limit = min(64, n - i)
                    while ml < limit and data[pos + ml] == data[i + ml]:
                        ml += 1
                    if ml > best_len:
                        best_len = ml
                        best_tok = ("lz", 266 + (ml - 3), d - 1)
                        if best_len == 64:
                            break

        if best_len >= 3 and best_tok is not None:
            tokens.append(best_tok)
            for k in range(best_len):
                p = i + k
                if p + 3 <= n:
                    head.setdefault(data[p : p + 3], []).append(p)
            i += best_len
            continue

        # Check 1-2 byte shortcut opcodes before literal fallback (note: second append sees updated out[-2] == orig out[-1])
        if i >= 2 and i + 2 <= n and data[i] == data[i - 2] and data[i + 1] == data[i - 1]:
            tokens.append(("sym", 261))
            tok_len = 2
        elif i >= 2 and i + 2 <= n and data[i] == data[i - 2] and data[i + 1] == ((data[i - 1] - 1) & 0xFF):
            tokens.append(("sym", 264))
            tok_len = 2
        elif i >= 2 and i + 2 <= n and data[i] == data[i - 2] and data[i + 1] == ((data[i - 1] + 1) & 0xFF):
            tokens.append(("sym", 265))
            tok_len = 2
        elif i >= 1 and data[i] == data[i - 1]:
            tokens.append(("sym", 257))
            tok_len = 1
        elif i >= 2 and data[i] == data[i - 2]:
            tokens.append(("sym", 258))
            tok_len = 1
        elif i >= 4 and data[i] == data[i - 4]:
            tokens.append(("sym", 259))
            tok_len = 1
        elif i >= 29 and data[i] == data[i - 29]:
            tokens.append(("sym", 260))
            tok_len = 1
        else:
            tokens.append(("sym", data[i]))
            tok_len = 1

        for k in range(tok_len):
            p = i + k
            if p + 3 <= n:
                head.setdefault(data[p : p + 3], []).append(p)
        i += tok_len

    # Mandatory EOS marker required by the 68000 $0095B6 decompressor
    tokens.append(("sym", 256))

    # Count symbol frequencies and boost frequent symbols for optimal canonical code lengths
    freqs = [0] * 328
    for tok in tokens:
        freqs[tok[1]] += 16

    nibbles = _length_limited_huff_nibbles(freqs, max_bits=15)
    codes = _extract_codes_from_nibbles(nibbles)

    huff_raw = bytes((nibbles[2 * j] << 4) | nibbles[2 * j + 1] for j in range(164))
    out = bytearray([0xF3, 0x00])
    out.extend(struct.pack(">H", n))
    out.extend(huff_raw)

    # Synchronized stream emission: bit-bytes are reserved in `out` at their exact
    # first-bit read position so synchronous `prefix = rom[p]` bytes sit at the right `p`.
    slot_idx = -1
    bits_left = 0

    def push_bit(b: int) -> None:
        nonlocal slot_idx, bits_left
        if bits_left == 0:
            slot_idx = len(out)
            out.append(0)
            bits_left = 8
        bits_left -= 1
        out[slot_idx] |= (b & 1) << bits_left

    def push_bits(val: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            push_bit((val >> shift) & 1)

    for tok in tokens:
        code, blen = codes[tok[1]]
        push_bits(code, blen)
        if tok[0] == "lz":
            prefix, extra, n_extra = dist_enc[tok[2]]
            out.append(prefix)
            push_bits(extra, n_extra)

    return bytes(out)


def encode_d222_best(data: bytes, verify_rom: Optional[bytes] = None) -> bytes:
    """Return shortest of LZ+Huff (0xF3), RLE (0xF1), and Raw (0xF0), self-verified by decode."""
    cands = [encode_d222_rle(data), encode_d222_raw(data)]
    if verify_rom is not None:
        dist_tbl = verify_rom[DEFAULT_DIST_OFFSET : DEFAULT_DIST_OFFSET + 256]
        cands.append(encode_d222_lzhuff(data, dist_tbl))
    best = min(cands, key=len)
    dec, end = decompress_d222(best, 0, dist_offset=DEFAULT_DIST_OFFSET) if verify_rom is None else (None, 0)
    if verify_rom is not None:
        # Verify 0xF3 with ROM's distance table by concatenating virtual stream or passing sliced verify_rom
        vrom = bytearray(max(len(verify_rom), DEFAULT_DIST_OFFSET + 256))
        vrom[DEFAULT_DIST_OFFSET : DEFAULT_DIST_OFFSET + 256] = verify_rom[DEFAULT_DIST_OFFSET : DEFAULT_DIST_OFFSET + 256]
        tmp_off = len(vrom)
        vrom.extend(best)
        dec, end = decompress_d222(bytes(vrom), tmp_off, dist_offset=DEFAULT_DIST_OFFSET)
        end -= tmp_off
    if dec != data or end != len(best):
        raise AssertionError("Self-check failed in encode_d222_best")
    return best
