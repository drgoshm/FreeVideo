"""Shared display-time rule for retained previews and original-media exports."""
from fractions import Fraction


def display_frame(frame):
    """Bake the container's display rotation into pixels (PyAV does not autorotate)."""
    rotation = frame.rotation % 360
    if not rotation:
        return frame
    import av
    import numpy as np
    if rotation % 90 == 0:
        pixels = np.rot90(frame.to_ndarray(format='rgb24'), rotation // 90)
        return av.VideoFrame.from_ndarray(np.ascontiguousarray(pixels), format='rgb24')
    return av.VideoFrame.from_image(frame.to_image().rotate(rotation, expand=True))


def frame_not_after(timestamp, number, fps=24):
    """Whether a decoded source frame may be displayed at timeline frame number.

    Consume every frame satisfying this predicate, including duplicate PTS;
    keep the last consumed frame until the next one is due.
    """
    return timestamp <= Fraction(number, fps)


def last_frame_end(timestamp, frame, rate):
    """Use the decoded last-frame duration; average FPS can be wrong for VFR."""
    duration = getattr(frame, 'duration', 0)
    return timestamp + (duration * frame.time_base if duration and frame.time_base
                        else Fraction(1, 1) / rate)
