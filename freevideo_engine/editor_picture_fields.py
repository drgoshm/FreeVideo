"""Sparse project fields using the renderer's authoritative normalization."""
from .editor_picture import _fields

PICTURE_KEYS = ('fit', 'crop_left', 'crop_top', 'crop_right', 'crop_bottom',
                'flip_h', 'flip_v', 'rotate', 'scale', 'offset_x', 'offset_y')


def picture_fields(value):
    return {key: item for key, item, default in zip(PICTURE_KEYS, _fields(value), _fields({}))
            if item != default}
