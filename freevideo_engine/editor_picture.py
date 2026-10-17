"""Pure CPU picture placement, shared geometrically with web/editor_geometry.js.

Coordinates are pixel *edges*. ``matrix`` maps original source edges to output
edges (CSS a,b,c,d,e,f order). Plans contain JSON values only, are safe to mutate,
and reuse a bounded cache keyed only by normalized picture fields and dimensions.
No project size, decoder, filesystem, browser, or ComfyUI state is consulted.
"""
from copy import deepcopy
from functools import lru_cache
import math

def _number(value, default, low, high):
    if type(value) not in (int, float) or (type(value) is float and math.isnan(value)):
        return default
    return min(high, max(low, value))


def _dimension(value):
    if (type(value) not in (int, float) or not 1 <= value <= 2**53 - 1
            or not math.isfinite(value) or int(value) != value):
        raise ValueError('Picture dimensions must be positive integers')
    return int(value)


def _fields(clip):
    clip = clip if isinstance(clip, dict) else {}
    edges = [_number(clip.get('crop_' + edge), 0, 0, .9)
             for edge in ('left', 'top', 'right', 'bottom')]
    for a, b in ((0, 2), (1, 3)):
        total = edges[a] + edges[b]
        if total > .9:
            edges[a], edges[b] = edges[a] * .9 / total, edges[b] * .9 / total
    return (('contain' if clip.get('fit') == 'contain' else 'cover'), *edges,
            clip.get('flip_h') is True, clip.get('flip_v') is True,
            _number(clip.get('rotate'), 0, -180, 180),
            _number(clip.get('scale'), 1, .1, 10),
            _number(clip.get('offset_x'), 0, -2, 2),
            _number(clip.get('offset_y'), 0, -2, 2))


def _apply(matrix, point):
    a, b, c, d, e, f = matrix
    x, y = point
    return [a * x + c * y + e, b * x + d * y + f]


def _inverse(matrix):
    return _inverse_cached(tuple(matrix))


@lru_cache(maxsize=512)
def _inverse_cached(matrix):
    a, b, c, d, e, f = matrix
    det = a * d - b * c
    return (d / det, -b / det, -c / det, a / det,
            (c * f - d * e) / det, (b * e - a * f) / det)


def _clip_polygon(polygon, width, height):
    # Sutherland-Hodgman; keep order identical to the JS implementation.
    for axis, bound, sign in ((0, 0, 1), (0, width, -1), (1, 0, 1), (1, height, -1)):
        result = []
        if not polygon:
            break
        previous = polygon[-1]
        before = sign * (previous[axis] - bound)
        for point in polygon:
            after = sign * (point[axis] - bound)
            if (before >= 0) != (after >= 0):
                t = before / (before - after)
                result.append([previous[0] + t * (point[0] - previous[0]),
                               previous[1] + t * (point[1] - previous[1])])
            if after >= 0:
                result.append(point)
            previous, before = point, after
        polygon = result
    unique = []
    for point in polygon:
        if not unique or max(abs(point[i] - unique[-1][i]) for i in (0, 1)) > 1e-9:
            unique.append(point)
    if len(unique) > 1 and max(abs(unique[0][i] - unique[-1][i]) for i in (0, 1)) <= 1e-9:
        unique.pop()
    return unique


def _area(polygon):
    if not polygon:
        return 0
    return abs(sum(polygon[i - 1][0] * p[1] - polygon[i - 1][1] * p[0]
                   for i, p in enumerate(polygon))) / 2


@lru_cache(maxsize=256)
def _build_plan(fields, sw, sh, width, height):
    fit, left, top, right, bottom, flip_h, flip_v, angle, scale, ox, oy = fields
    x, y = min(sw - 1, round(left * sw)), min(sh - 1, round(top * sh))
    cw = max(1, sw - round(right * sw) - x)
    ch = max(1, sh - round(bottom * sh) - y)
    if angle % 90 == 0:
        cosine, sine = ((1, 0), (0, 1), (-1, 0), (0, -1))[int(angle / 90) % 4]
    else:
        radians = angle * math.pi / 180
        cosine, sine = math.cos(radians), math.sin(radians)
    rw, rh = abs(cosine) * cw + abs(sine) * ch, abs(sine) * cw + abs(cosine) * ch
    ratio = (max if fit == 'cover' else min)(width / rw, height / rh)
    # Preserve legacy rounding only for the unadjusted cover. Adjusted pictures
    # keep subpixel edges so the same clip scales proportionally across exports.
    legacy = fields == _fields({})
    if legacy:
        dw, dh = max(1, round(rw * ratio)), max(1, round(rh * ratio))
        dx, dy = -((dw - width) // 2), -((dh - height) // 2)
    else:
        dw, dh = rw * ratio * scale, rh * ratio * scale
        dx, dy = (width - dw) / 2 + ox * width, (height - dh) / 2 + oy * height
    cx, cy = dx + dw / 2, dy + dh / 2
    sx, sy = dw / rw, dh / rh
    fx, fy = -1 if flip_h else 1, -1 if flip_v else 1
    a, b, c, d = sx * cosine * fx, sy * sine * fx, -sx * sine * fy, sy * cosine * fy
    matrix = [a, b, c, d, cx - a * (x + cw / 2) - c * (y + ch / 2),
              cy - b * (x + cw / 2) - d * (y + ch / 2)]
    if angle % 90 == 0:
        raster_matrix = [sx, 0, 0, sy, dx, dy]
    else:
        ra, rb, rc, rd = sx * cosine, sy * sine, -sx * sine, sy * cosine
        raster_matrix = [ra, rb, rc, rd, cx - ra * cw / 2 - rc * ch / 2, cy - rb * cw / 2 - rd * ch / 2]
    corners = [[x, y], [x + cw, y], [x + cw, y + ch], [x, y + ch]]
    polygon = [_apply(matrix, p) for p in corners]
    inverse = _inverse(matrix)
    visible = [_apply(inverse, p) for p in _clip_polygon(polygon, width, height)]
    # Suppress insignificant numerical excursions outside the crop rectangle.
    visible = [[min(x + cw, max(x, p[0])), min(y + ch, max(y, p[1]))] for p in visible]
    output_corners = [[0, 0], [width, 0], [width, height], [0, height]]
    covers = all(x - 1e-7 <= p[0] <= x + cw + 1e-7 and y - 1e-7 <= p[1] <= y + ch + 1e-7
                 for p in (_apply(inverse, q) for q in output_corners))
    cropped_fraction = min(1, max(0, 1 - _area(visible) / (sw * sh)))
    # A one-pixel tolerance for explicit source-edge crops and output overflow.
    loss = max(x, y, sw - x - cw, sh - y - ch,
               -min(p[0] for p in polygon), max(p[0] for p in polygon) - width,
               -min(p[1] for p in polygon), max(p[1] for p in polygon) - height)
    if visible:
        vx, vy = min(p[0] for p in visible), min(p[1] for p in visible)
        vw, vh = max(p[0] for p in visible) - vx, max(p[1] for p in visible) - vy
    else:
        vx, vy, vw, vh = x, y, 0, 0
    return dict(version=1, source=dict(width=sw, height=sh), output=dict(width=width, height=height),
                fit=fit, crop=dict(x=x, y=y, width=cw, height=ch), flip_h=flip_h, flip_v=flip_v,
                rotate=angle, rotated=dict(width=rw, height=rh),
                center=dict(x=cx, y=cy), size=dict(width=dw, height=dh), matrix=matrix,
                raster_matrix=raster_matrix, legacy=legacy,
                covers=covers, cropped=loss > 1 + 1e-7, cropped_fraction=cropped_fraction,
                visible=dict(x=vx, y=vy, width=vw, height=vh, polygon=visible))


def plan(clip, source_width, source_height, width, height):
    """Return an independently mutable JSON plan; at most 256 geometries cached.

    Crop pairs over .9 are reduced proportionally; invalid types/NaN use field
    defaults, infinities and out-of-range numbers clamp. Dimensions must be
    positive integers. Unrelated clip fields never invalidate the geometry cache.
    """
    dimensions = tuple(_dimension(v) for v in (source_width, source_height, width, height))
    return deepcopy(_build_plan(_fields(clip), *dimensions))


def is_cropped(clip, source_width, source_height, width, height):
    """Whether a source crop or output-edge overflow exceeds one pixel in depth."""
    return plan(clip, source_width, source_height, width, height)['cropped']


def _resize(pixels, width, height):
    import av
    import numpy as np
    # Do not change the interpolation flag: this is editor_export._VideoReader's
    # PyAV/libswscale default, including its rounding and RGB conversion behavior.
    return av.VideoFrame.from_ndarray(np.ascontiguousarray(pixels), format='rgb24').reformat(
        width=width, height=height, format='rgb24').to_ndarray()


def _oriented(pixels, p):
    import numpy as np
    crop = p['crop']
    image = pixels[crop['y']:crop['y'] + crop['height'], crop['x']:crop['x'] + crop['width']]
    if p['flip_h']:
        image = image[:, ::-1]
    if p['flip_v']:
        image = image[::-1]
    if p['rotate'] % 90 == 0:
        image = np.rot90(image, -int(p['rotate'] / 90))
    return image


def _warp(image, width, height, matrix):
    import numpy as np
    from PIL import Image
    inverse = _inverse(matrix)
    a, b, c, d, e, f = inverse
    source = Image.fromarray(np.ascontiguousarray(image))
    return source.transform((width, height), Image.Transform.AFFINE, (a, c, e, b, d, f),
                            resample=Image.Resampling.BILINEAR)


@lru_cache(maxsize=16)
def _coverage_edges(matrix, iw, ih, width, height):
    """Exact convex-polygon/pixel-square area; only edge pixels need clipping.

    Pillow's affine alpha is a point sample, causing resolution-dependent hard
    edges. Interior pixels are cheap scanlines; work at edges is O(perimeter).
    """
    polygon = _clip_polygon([_apply(matrix, p) for p in ((0, 0), (iw, 0), (iw, ih), (0, ih))], width, height)
    if len(polygon) < 3:
        return (), ()
    edges = list(zip(polygon, polygon[1:] + polygon[:1]))
    spans, border = [], []
    # Convex polygon scanline fill at pixel centers, with edge areas below.
    for y in range(max(0, math.floor(min(p[1] for p in polygon))), min(height, math.ceil(max(p[1] for p in polygon)))):
        cuts = [p[0] + (y + .5 - p[1]) * (q[0] - p[0]) / (q[1] - p[1])
                for p, q in edges if min(p[1], q[1]) <= y + .5 < max(p[1], q[1])]
        if len(cuts) >= 2:
            start, end = max(0, math.ceil(min(cuts) - .5)), min(width, math.ceil(max(cuts) - .5))
            spans.append((y, start, end))
    cells = set()
    for p, q in edges:
        # Walk columns for shallow edges, rows for steep edges, never an AABB.
        axis = 0 if abs(q[0] - p[0]) >= abs(q[1] - p[1]) else 1
        other = 1 - axis
        if p[axis] > q[axis]:
            p, q = q, p
        span = q[axis] - p[axis]
        if span < 1e-12:
            continue
        for k in range(math.floor(p[axis]), math.floor(q[axis]) + 1):
            v0 = p[other] + (max(k, p[axis]) - p[axis]) / span * (q[other] - p[other])
            v1 = p[other] + (min(k + 1, q[axis]) - p[axis]) / span * (q[other] - p[other])
            for j in range(math.floor(min(v0, v1)), math.floor(max(v0, v1)) + 1):
                x, y = (k, j) if axis == 0 else (j, k)
                if 0 <= x < width and 0 <= y < height:
                    cells.add((x, y))
    for x, y in cells:
        local = [[p[0] - x, p[1] - y] for p in polygon]
        border.append((x, y, min(1, _area(_clip_polygon(local, 1, 1)))))
    return tuple(spans), tuple(border)


@lru_cache(maxsize=256)
def _coverage_axes(a, d, e, f, iw, ih, width, height):
    import numpy as np
    xs, ys = np.arange(width), np.arange(height)
    xcover = np.clip(np.minimum(xs + 1, e + a * iw) - np.maximum(xs, e), 0, 1).astype(np.float32)
    ycover = np.clip(np.minimum(ys + 1, f + d * ih) - np.maximum(ys, f), 0, 1).astype(np.float32)
    xcover.flags.writeable = ycover.flags.writeable = False
    return xcover, ycover


def _coverage(matrix, iw, ih, width, height):
    import numpy as np
    a, b, c, d, e, f = matrix
    if b == 0 and c == 0:
        xcover, ycover = _coverage_axes(a, d, e, f, iw, ih, width, height)
        return ycover[:, None] * xcover[None, :]
    spans, border = _coverage_edges(tuple(matrix), iw, ih, width, height)
    mask = np.zeros((height, width), dtype=np.float32)
    for y, start, end in spans:
        mask[y, start:end] = 1
    for x, y, area in border:
        mask[y, x] = area
    return mask


def _edge_samples(image, matrix, xs, ys):
    """Bilinear source colors clamped at edges for partially covered pixels."""
    import numpy as np
    a, b, c, d, e, f = _inverse(matrix)
    u = np.clip(a * (xs + .5) + c * (ys + .5) + e - .5, 0, image.shape[1] - 1)
    v = np.clip(b * (xs + .5) + d * (ys + .5) + f - .5, 0, image.shape[0] - 1)
    x0, y0 = u.astype(np.intp), v.astype(np.intp)
    x1, y1 = np.minimum(x0 + 1, image.shape[1] - 1), np.minimum(y0 + 1, image.shape[0] - 1)
    fx, fy = (u - x0)[:, None], (v - y0)[:, None]
    return ((image[y0, x0] * (1 - fx) + image[y0, x1] * fx) * (1 - fy)
            + (image[y1, x0] * (1 - fx) + image[y1, x1] * fx) * fy)


def _background(pixels, p):
    """A centered, covering copy of steps 1-3; no user scale/offset on the bed.

    Work at quarter width AND height (1080p full-size blur measured >30 ms).
    For arbitrary rotations, cover the actual opaque rectangle, not its empty
    bounding-box corners. No artificial black wedges enter the blur.
    """
    import numpy as np
    from PIL import ImageFilter
    width, height = p['output']['width'], p['output']['height']
    bw, bh = max(1, math.ceil(width / 4)), max(1, math.ceil(height / 4))
    image = _oriented(pixels, p)
    ih, iw = image.shape[:2]
    if p['rotate'] % 90 == 0:
        ratio = max(bw / iw, bh / ih)
        # Viewport sampling keeps memory bounded even for very thin sources.
        matrix = [ratio, 0, 0, ratio, (bw - iw * ratio) / 2, (bh - ih * ratio) / 2]
    else:
        angle = p['rotate'] * math.pi / 180
        c, s = math.cos(angle), math.sin(angle)
        ratio = max((abs(c) * bw + abs(s) * bh) / iw,
                    (abs(s) * bw + abs(c) * bh) / ih)
        a, b, cc, d = ratio * c, ratio * s, -ratio * s, ratio * c
        matrix = [a, b, cc, d, bw / 2 - a * iw / 2 - cc * ih / 2,
                  bh / 2 - b * iw / 2 - d * ih / 2]
    bed = _warp(image, bw, bh, matrix)
    radius = min(width, height) / 20 * min(bw / width, bh / height)
    bed = bed.filter(ImageFilter.GaussianBlur(radius))
    # LUT darkens exactly once; a black source remains black by design.
    # Darken the small image before upsampling to avoid another full-size pass.
    return _resize(np.asarray(bed.point([round(i * .7) for i in range(256)] * 3)), width, height)


def place(pixels, clip, width, height):
    """Place H×W×3 uint8 RGB pixels; return height×width×3 uint8, without mutation.

    Quarter turns transpose losslessly before resizing. Other angles compose
    the ordered geometry into one bilinear affine sample to avoid two resamples.
    A covered output never computes a background. RGB defaults match the old
    export's reformat/center-crop byte for byte (native YUV decode is upstream).
    """
    import numpy as np
    from PIL import Image
    if (not isinstance(pixels, np.ndarray) or pixels.ndim != 3 or pixels.shape[2] != 3
            or not pixels.shape[0] or not pixels.shape[1] or pixels.dtype != np.uint8):
        raise ValueError('pixels must be a nonempty H×W×3 uint8 RGB array')
    p = plan(clip, pixels.shape[1], pixels.shape[0], width, height)
    width, height = p['output']['width'], p['output']['height']
    dw, dh = p['size']['width'], p['size']['height']
    dx, dy = p['center']['x'] - dw / 2, p['center']['y'] - dh / 2
    image = _oriented(pixels, p)
    quarter = p['rotate'] % 90 == 0
    # Large user zooms sample just the viewport, avoiding a scale^2 allocation.
    # The legacy/default path always keeps the original PyAV resize semantics.
    legacy = p['legacy']
    integral = all(abs(v - round(v)) < 1e-9 for v in (dw, dh, dx, dy)) and dw >= 1 and dh >= 1
    if quarter and integral and (dw * dh <= 4 * width * height or legacy):
        dw, dh, dx, dy = (round(v) for v in (dw, dh, dx, dy))
        foreground = _resize(image, dw, dh)
        if p['covers']:
            return foreground[-dy:height - dy, -dx:width - dx].copy()
        result = _background(pixels, p)
        x0, y0, x1, y1 = max(0, dx), max(0, dy), min(width, dx + dw), min(height, dy + dh)
        if x1 > x0 and y1 > y0:
            result[y0:y1, x0:x1] = foreground[y0 - dy:y1 - dy, x0 - dx:x1 - dx]
        return result
    matrix = p['raster_matrix']
    if p['covers']:
        return np.asarray(_warp(image, width, height, matrix)).copy()
    foreground = np.asarray(_warp(image, width, height, matrix)).copy()
    mask = _coverage(matrix, image.shape[1], image.shape[0], width, height)
    ys, xs = np.nonzero((mask > 0) & (mask < 1))
    foreground[ys, xs] = np.rint(_edge_samples(image, matrix, xs, ys)).astype(np.uint8)
    background = _background(pixels, p)
    # Pillow blends in C without several full-size float RGB temporaries. The
    # alpha quantization is at most half a coverage level (<= .5 RGB level).
    alpha = Image.fromarray(np.rint(mask * 255).astype(np.uint8))
    return np.asarray(Image.composite(Image.fromarray(foreground), Image.fromarray(background), alpha)).copy()


# Record validation.
def picture_fit(value):
    if value not in ('cover', 'contain'):
        raise ValueError('Invalid picture fit')
    return value
