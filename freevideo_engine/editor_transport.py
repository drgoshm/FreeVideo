"""Optional marker metadata; absent fields retain the existing wire format."""


def marker_fields(record):
    if 'markers' not in record:
        return {}
    markers = record['markers']
    if not isinstance(markers, list) or len(markers) > 10000:
        raise ValueError('Invalid markers')
    rows = {}
    for marker in markers:
        if not isinstance(marker, dict):
            raise ValueError('Invalid marker')
        frame, name = marker.get('frame'), marker.get('name')
        if (type(frame) is not int or not 0 <= frame <= 10**7 or not isinstance(name, str)
                or len(name) > 8000 or any(0xd800 <= ord(c) <= 0xdfff for c in name)):
            raise ValueError('Invalid marker')
        rows[frame] = dict(frame=frame, name=name)
    return dict(markers=[rows[frame] for frame in sorted(rows)])
