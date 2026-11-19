"""Генератор QR-кодов (байтовый режим, уровни L и M, версии 1-15) без зависимостей. Вывод в SVG."""

from __future__ import annotations

# (число блоков, всего слов, слов данных[, ...ещё пары для второго размера блока])
RS = {
    1: {"L": [1, 26, 19], "M": [1, 26, 16]}, 2: {"L": [1, 44, 34], "M": [1, 44, 28]},
    3: {"L": [1, 70, 55], "M": [1, 70, 44]}, 4: {"L": [1, 100, 80], "M": [2, 50, 32]},
    5: {"L": [1, 134, 108], "M": [2, 67, 43]}, 6: {"L": [2, 86, 68], "M": [4, 43, 27]},
    7: {"L": [2, 98, 78], "M": [4, 49, 31]}, 8: {"L": [2, 121, 97], "M": [2, 60, 38, 2, 61, 39]},
    9: {"L": [2, 146, 116], "M": [3, 58, 36, 2, 59, 37]}, 10: {"L": [2, 86, 68, 2, 87, 69], "M": [4, 69, 43, 1, 70, 44]},
    11: {"L": [4, 101, 81], "M": [1, 80, 50, 4, 81, 51]}, 12: {"L": [2, 116, 92, 2, 117, 93], "M": [6, 58, 36, 2, 59, 37]},
    13: {"L": [4, 133, 107], "M": [8, 59, 37, 1, 60, 38]}, 14: {"L": [3, 145, 115, 1, 146, 116], "M": [4, 64, 40, 5, 65, 41]},
    15: {"L": [5, 109, 87, 1, 110, 88], "M": [5, 65, 41, 5, 66, 42]},
}
ALIGN = {
    2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50], 11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62], 14: [6, 26, 46, 66], 15: [6, 26, 48, 70],
}
FORMAT_BITS = {"L": 1, "M": 0}

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(n: int) -> list[int]:
    g = [1]
    for i in range(n):
        nxt = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            nxt[j] ^= c
            nxt[j + 1] ^= _gf_mul(c, _EXP[i])
        g = nxt
    return g


def _rs_remainder(data: list[int], n: int) -> list[int]:
    gen = _rs_generator(n)
    rem = list(data) + [0] * n
    for i in range(len(data)):
        coef = rem[i]
        if coef:
            for j in range(1, len(gen)):
                rem[i + j] ^= _gf_mul(gen[j], coef)
    return rem[len(data):]


def _blocks(version: int, ecc: str) -> list[tuple[int, int]]:
    row = RS[version][ecc]
    out = []
    for i in range(0, len(row), 3):
        out += [(row[i + 1], row[i + 2])] * row[i]
    return out  # (всего слов, слов данных)


def _capacity(version: int, ecc: str) -> int:
    return sum(d for _, d in _blocks(version, ecc))


def _encode_data(data: bytes, version: int, ecc: str) -> list[int]:
    cap_bits = _capacity(version, ecc) * 8
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        for i in range(n - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, cap_bits - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    while len(words) < cap_bits // 8:
        words.append(pad[(len(words) - len(bits) // 8) % 2])
    return words


def _interleave(words: list[int], version: int, ecc: str) -> list[int]:
    blocks = _blocks(version, ecc)
    data_blocks, ecc_blocks, pos = [], [], 0
    for total, dcount in blocks:
        chunk = words[pos:pos + dcount]
        pos += dcount
        data_blocks.append(chunk)
        ecc_blocks.append(_rs_remainder(chunk, total - dcount))
    out = []
    for i in range(max(len(b) for b in data_blocks)):
        for b in data_blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(len(ecc_blocks[0])):
        for b in ecc_blocks:
            out.append(b[i])
    return out


def _bch(value: int, poly: int, shift: int, top: int) -> int:
    v = value << shift
    while v.bit_length() > top:
        v ^= poly << (v.bit_length() - poly.bit_length())
    return v


def _format_bits(ecc: str, mask: int) -> int:
    data = (FORMAT_BITS[ecc] << 3) | mask
    return ((data << 10) | _bch(data, 0x537, 10, 10)) ^ 0x5412


def _version_bits(version: int) -> int:
    return (version << 12) | _bch(version, 0x1F25, 12, 12)


MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _base_matrix(version: int):
    n = 17 + 4 * version
    m = [[None] * n for _ in range(n)]
    func = [[False] * n for _ in range(n)]

    def setf(r, c, v):
        m[r][c] = v
        func[r][c] = True

    def finder(r0, c0):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < n and 0 <= c < n:
                    on = (0 <= dr <= 6 and dc in (0, 6)) or (0 <= dc <= 6 and dr in (0, 6)) or (2 <= dr <= 4 and 2 <= dc <= 4)
                    setf(r, c, on)

    finder(0, 0)
    finder(0, n - 7)
    finder(n - 7, 0)
    for i in range(8, n - 8):
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    pos = ALIGN.get(version, [])
    for r in pos:
        for c in pos:
            if (r, c) in ((6, 6), (6, pos[-1]), (pos[-1], 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    setf(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
    setf(n - 8, 8, True)  # тёмный модуль
    for i in range(9):
        if not func[8][i]:
            setf(8, i, False)
        if not func[i][8]:
            setf(i, 8, False)
    for i in range(8):
        if not func[8][n - 1 - i]:
            setf(8, n - 1 - i, False)
        if not func[n - 1 - i][8]:
            setf(n - 1 - i, 8, False)
    if version >= 7:
        vb = _version_bits(version)
        for i in range(18):
            bit = bool((vb >> i) & 1)
            setf(i // 3, n - 11 + i % 3, bit)
            setf(n - 11 + i % 3, i // 3, bit)
    return m, func


def _place(m, func, codewords: list[int]):
    n = len(m)
    bits = [(w >> i) & 1 for w in codewords for i in range(7, -1, -1)]
    idx, up = 0, True
    c = n - 1
    while c > 0:
        if c == 6:
            c -= 1
        rows = range(n - 1, -1, -1) if up else range(n)
        for r in rows:
            for cc in (c, c - 1):
                if not func[r][cc]:
                    m[r][cc] = bool(bits[idx]) if idx < len(bits) else False
                    idx += 1
        up = not up
        c -= 2


def _penalty(m) -> int:
    n = len(m)
    score = 0
    for grid in (m, [list(col) for col in zip(*m)]):
        for row in grid:
            run, prev = 1, row[0]
            for v in row[1:]:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, v
            if run >= 5:
                score += run - 2
            s = "".join("1" if v else "0" for v in row)
            score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(v for row in m for v in row)
    score += 10 * (abs(dark * 100 // (n * n) - 50) // 5)
    return score


def _apply(m, func, ecc: str, mask: int):
    n = len(m)
    out = [[(m[r][c] ^ MASKS[mask](r, c)) if not func[r][c] else m[r][c] for c in range(n)] for r in range(n)]
    fb = _format_bits(ecc, mask)
    for i in range(15):
        bit = bool((fb >> i) & 1)
        if i < 6:
            out[i][8] = bit
        elif i < 8:
            out[i + 1][8] = bit
        else:
            out[n - 15 + i][8] = bit
        if i < 8:
            out[8][n - 1 - i] = bit
        elif i < 9:
            out[8][15 - i] = bit
        else:
            out[8][14 - i] = bit
    return out


def make(data: str | bytes, ecc: str = "M", version: int | None = None, mask: int | None = None) -> list[list[bool]]:
    raw = data.encode() if isinstance(data, str) else data
    if ecc not in RS[1]:
        raise ValueError("уровень L или M")
    versions = [version] if version else range(1, 16)
    for v in versions:
        if len(raw) + 3 + (1 if v >= 10 else 0) <= _capacity(v, ecc):
            break
    else:
        raise ValueError("слишком много данных для QR")
    words = _interleave(_encode_data(raw, v, ecc), v, ecc)
    base, func = _base_matrix(v)
    _place(base, func, words)
    if mask is not None:
        return _apply(base, func, ecc, mask)
    best = min((_apply(base, func, ecc, k) for k in range(8)), key=_penalty)
    return best


def svg(data: str, ecc: str = "M", size: int = 220, border: int = 3) -> str:
    m = make(data, ecc)
    n = len(m)
    total = n + 2 * border
    path = []
    for r, row in enumerate(m):
        c = 0
        while c < n:
            if row[c]:
                start = c
                while c < n and row[c]:
                    c += 1
                path.append(f"M{start + border},{r + border}h{c - start}v1h-{c - start}z")
            else:
                c += 1
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {total} {total}" role="img" aria-label="QR-код" shape-rendering="crispEdges">'
        f'<rect width="{total}" height="{total}" fill="#fff"/><path d="{"".join(path)}" fill="#000"/></svg>'
    )
