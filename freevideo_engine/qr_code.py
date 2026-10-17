"""Small QR Code encoder for the LAN access address (byte mode, versions 1-10, error correction level M).

The launcher and `freevideo status` draw the result themselves, so there is no image library involved.
"""

MAX_VERSION = 10
# Per version (index 1..10): error correction codewords per block, and number of blocks, at level M.
_ECC_PER_BLOCK = (0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
_BLOCKS = (0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
_FORMAT_M = 0  # the two format bits for level M are 00


def _alignment_positions(version):
    if version == 1:
        return []
    count = version // 7 + 2
    step = 26 if version == 32 else (version * 4 + count * 2 + 1) // (count * 2 - 2) * 2
    last = version * 4 + 10
    return [6] + [last - step * k for k in range(count - 2, -1, -1)]


def _raw_modules(version):
    result = (16 * version + 128) * version + 64
    if version >= 2:
        count = version // 7 + 2
        result -= (25 * count - 10) * count - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version):
    return _raw_modules(version) // 8 - _ECC_PER_BLOCK[version] * _BLOCKS[version]


def _gf_multiply(x, y):
    result = 0
    for bit in range(7, -1, -1):
        result = (result << 1) ^ ((result >> 7) * 0x11D)
        result ^= ((y >> bit) & 1) * x
    return result


def _generator(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for index in range(degree):
            result[index] = _gf_multiply(result[index], root)
            if index + 1 < degree:
                result[index] ^= result[index + 1]
        root = _gf_multiply(root, 2)
    return result


def _remainder(data, generator):
    result = [0] * len(generator)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for index, coefficient in enumerate(generator):
            result[index] ^= _gf_multiply(coefficient, factor)
    return result


def _codewords(data, version):
    blocks = _BLOCKS[version]
    ecc_length = _ECC_PER_BLOCK[version]
    raw = _raw_modules(version) // 8
    short_blocks = blocks - raw % blocks
    short_length = raw // blocks
    generator = _generator(ecc_length)
    split, position = [], 0
    for index in range(blocks):
        length = short_length - ecc_length + (0 if index < short_blocks else 1)
        block = data[position:position + length]
        position += length
        split.append(block + _remainder(block, generator))
        if index < short_blocks:
            split[-1].insert(length, None)  # keeps columns aligned with the longer blocks
    result = []
    for column in range(len(split[-1])):
        for index, block in enumerate(split):
            if column != short_length - ecc_length or index >= short_blocks:
                result.append(block[column])
    return result


class _Matrix:
    def __init__(self, version):
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set(self, x, y, dark, function=True):
        self.modules[y][x] = bool(dark)
        if function:
            self.function[y][x] = True


def _draw_function_patterns(matrix, version):
    size = matrix.size
    for index in range(size):
        matrix.set(6, index, index % 2 == 0)
        matrix.set(index, 6, index % 2 == 0)

    def finder(cx, cy):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    distance = max(abs(dx), abs(dy))
                    matrix.set(x, y, distance not in (2, 4))

    finder(3, 3); finder(size - 4, 3); finder(3, size - 4)
    positions = _alignment_positions(version)
    for i, cx in enumerate(positions):
        for j, cy in enumerate(positions):
            if (i == 0 and j == 0) or (i == 0 and j == len(positions) - 1) or (i == len(positions) - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    matrix.set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
    _draw_format(matrix, 0)  # reserves the format areas; the real bits are written after masking
    if version >= 7:
        remainder = version
        for _ in range(12):
            remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
        bits = version << 12 | remainder
        for index in range(18):
            dark = (bits >> index) & 1
            a, b = size - 11 + index % 3, index // 3
            matrix.set(a, b, dark); matrix.set(b, a, dark)


def _draw_format(matrix, mask):
    size = matrix.size
    data = _FORMAT_M << 3 | mask
    remainder = data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
    bits = (data << 10 | remainder) ^ 0x5412

    def bit(index):
        return (bits >> index) & 1

    for index in range(0, 6):
        matrix.set(8, index, bit(index))
    matrix.set(8, 7, bit(6)); matrix.set(8, 8, bit(7)); matrix.set(7, 8, bit(8))
    for index in range(9, 15):
        matrix.set(14 - index, 8, bit(index))
    for index in range(0, 8):
        matrix.set(size - 1 - index, 8, bit(index))
    for index in range(8, 15):
        matrix.set(8, size - 15 + index, bit(index))
    matrix.set(8, size - 8, True)


def _draw_codewords(matrix, codewords):
    size = matrix.size
    bits = [(byte >> shift) & 1 for byte in codewords for shift in range(7, -1, -1)]
    index = 0
    for right in range(size - 1, 0, -2):
        if right <= 6:
            right -= 1
        for vertical in range(size):
            for offset in range(2):
                x = right - offset
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vertical if upward else vertical
                if not matrix.function[y][x] and index < len(bits):
                    matrix.modules[y][x] = bool(bits[index]); index += 1


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _apply_mask(matrix, mask):
    for y in range(matrix.size):
        for x in range(matrix.size):
            if not matrix.function[y][x] and _MASKS[mask](x, y):
                matrix.modules[y][x] = not matrix.modules[y][x]


def _penalty(modules):
    size = len(modules)
    score = 0
    for lines in (modules, [list(column) for column in zip(*modules)]):
        for line in lines:
            run = 1
            for index in range(1, size):
                if line[index] == line[index - 1]:
                    run += 1
                    continue
                score += run - 2 if run >= 5 else 0
                run = 1
            score += run - 2 if run >= 5 else 0
            text = ''.join('1' if value else '0' for value in line)
            for pattern in ('10111010000', '00001011101'):
                start = text.find(pattern)
                while start != -1:
                    score += 40
                    start = text.find(pattern, start + 1)
    for y in range(size - 1):
        for x in range(size - 1):
            if modules[y][x] == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                score += 3
    dark = sum(row.count(True) for row in modules)
    total = size * size
    score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
    return score


def encode(text):
    """Return the QR code for `text` as rows of booleans (True is a dark module), without the quiet zone."""
    payload = text.encode('utf-8')
    version = next((v for v in range(1, MAX_VERSION + 1) if len(payload) <= _data_codewords(v) - (2 if v < 10 else 3)), None)
    if version is None:
        raise ValueError('The text is too long for a QR code.')
    count_bits = 8 if version < 10 else 16
    bits = '0100' + format(len(payload), '0%db' % count_bits) + ''.join(format(byte, '08b') for byte in payload)
    capacity = _data_codewords(version) * 8
    bits += '0' * min(4, capacity - len(bits))
    bits += '0' * (-len(bits) % 8)
    data = [int(bits[index:index + 8], 2) for index in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < capacity // 8:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    codewords = _codewords(data, version)
    best = None
    for mask in range(8):
        matrix = _Matrix(version)
        _draw_function_patterns(matrix, version)
        _draw_codewords(matrix, codewords)
        _apply_mask(matrix, mask)
        _draw_format(matrix, mask)
        score = _penalty(matrix.modules)
        if best is None or score < best[0]:
            best = (score, matrix.modules)
    return [list(row) for row in best[1]]


def terminal_lines(text, quiet=2):
    """The code as lines of half-block characters, two module rows per line, dark modules drawn dark on light."""
    modules = encode(text)
    size = len(modules) + quiet * 2
    grid = [[False] * size for _ in range(size)]
    for y, row in enumerate(modules):
        for x, value in enumerate(row):
            grid[y + quiet][x + quiet] = value
    if size % 2:
        grid.append([False] * size)
    glyph = {(False, False): '█', (True, True): ' ', (False, True): '▀', (True, False): '▄'}
    return [''.join(glyph[(grid[y][x], grid[y + 1][x])] for x in range(size)) for y in range(0, len(grid), 2)]
