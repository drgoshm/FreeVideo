"""First frames with placed people: a scene, some character pictures and where each one stands.

With a scene picture the people are cut out of their plain backdrops and pasted at the asked places;
the result is a rough layout, and the image model renders it as one clean photograph (the layout is
passed as a reference, so the places and sizes carry over). With only a scene description the same
places are written into the prompt. Either way the answer is a normal generate request.
"""
from pathlib import Path
import uuid

from PIL import Image

MAX_PEOPLE = 3  # the model takes 4 pictures: the people (or none) plus the layout or the scene
TOLERANCE = 9


def cut_out(picture, step=TOLERANCE):
    """The person on their plain backdrop as an RGBA picture.

    The backdrop is what the edge reaches by changing less than `step` between neighbouring pixels,
    so a vignette or gradient backdrop goes as well; it is found on a small copy and scaled back.
    """
    from collections import deque
    rgb = picture.convert('RGB')
    scale = min(1., 384 / max(rgb.size))
    small = rgb.resize((max(2, round(rgb.width * scale)), max(2, round(rgb.height * scale))), Image.BILINEAR)
    width, height = small.size
    pixels = list(small.getdata())
    gone = bytearray(width * height)
    queue = deque()
    for x in range(width):
        queue.extend((x, width * (height - 1) + x))
    for y in range(height):
        queue.extend((width * y, width * y + width - 1))
    for index in queue:
        gone[index] = 1
    while queue:
        index = queue.popleft()
        r, g, b = pixels[index]
        x = index % width
        for other in (index - 1 if x else -1, index + 1 if x < width - 1 else -1, index - width, index + width):
            if 0 <= other < len(pixels) and not gone[other]:
                r2, g2, b2 = pixels[other]
                if abs(r - r2) + abs(g - g2) + abs(b - b2) < step:
                    gone[other] = 1
                    queue.append(other)
    mask = Image.new('L', small.size)
    mask.putdata([0 if flag else 255 for flag in gone])
    mask = mask.resize(rgb.size, Image.BILINEAR).point(lambda v: 255 if v > 127 else 0)
    result = rgb.convert('RGBA')
    result.putalpha(mask)
    box = mask.getbbox()
    return result.crop(box) if box else result


def collage(scene, people, width, height):
    """The scene scaled to cover the canvas with each person pasted: people = [(picture, x, height, feet)] in 0-1."""
    canvas = scene.convert('RGB')
    scale = max(width / canvas.width, height / canvas.height)
    canvas = canvas.resize((max(width, round(canvas.width * scale)), max(height, round(canvas.height * scale))))
    left, top = (canvas.width - width) // 2, (canvas.height - height) // 2
    canvas = canvas.crop((left, top, left + width, top + height))
    for picture, x, tall, feet in people:
        person = cut_out(picture)
        size = max(1, round(tall * height))
        person = person.resize((max(1, round(person.width * size / person.height)), size))
        at = (round(x * width - person.width / 2), round(feet * height - person.height))
        canvas.paste(person, at, person)
    return canvas


def _check(people):
    if not isinstance(people, list) or not 1 <= len(people) <= MAX_PEOPLE:
        raise ValueError('invalid_request')
    rows = []
    for person in people:
        if not isinstance(person, dict) or not isinstance(person.get('image'), str):
            raise ValueError('invalid_media')
        x = person.get('x', .5)
        tall = person.get('height', .62)
        feet = person.get('feet', .9)
        if any(type(v) not in (int, float) or not 0 <= v <= 1 for v in (x, tall, feet)) or tall < .05:
            raise ValueError('invalid_request')
        rows.append(dict(image=person['image'], x=float(x), height=float(tall), feet=float(feet),
                         name=str(person.get('name') or '')[:40]))
    return rows


def _where(x):
    return 'on the left' if x < .38 else 'on the right' if x > .62 else 'in the middle'


def _positions(people):
    """Left-to-right sentence; ordering words place the people better than percentages do."""
    order = sorted(range(len(people)), key=lambda i: people[i]['x'])
    if len(people) == 1:
        return 'The person shown in image 1 stands %s of the frame, full body, feet on the floor.' % _where(people[0]['x'])
    words = ['on the left', 'in the middle', 'on the right'] if len(people) == 3 else ['on the left', 'on the right']
    rows = ['%s stands the person shown in image %d' % (word, i + 1) for word, i in zip(words, order)]
    rows[0] = rows[0][0].upper() + rows[0][1:]
    return '; '.join(rows) + '. Show everyone head to toe, standing apart with empty floor between them, with natural shadows.'


def build(value, input_directory):
    """A generate request for a first frame. value: width, height, people=[{image, x, height, feet, name}],
    and the scene as scene_image (a file name) or scene (text). Pictures are file names in the input directory
    (or freevideo-image/<name>); the layout picture, if any, is written to freevideo-image/."""
    if not isinstance(value, dict):
        raise ValueError('invalid_request')
    people = _check(value.get('people'))
    width, height = value.get('width'), value.get('height')
    if any(type(v) is not int or not 256 <= v <= 2048 for v in (width, height)):
        raise ValueError('invalid_dimensions')
    width, height = width // 32 * 32, height // 32 * 32
    text = value.get('scene') if isinstance(value.get('scene'), str) else ''
    directory = Path(input_directory).resolve()

    def load(name):
        folder, _, leaf = name.rpartition('/')
        if not leaf or folder not in ('', 'freevideo-image') or any(c in name for c in ('\\', ':', '\0')):
            raise ValueError('invalid_media')
        path = (directory / folder / leaf) if folder else directory / leaf
        parent = directory / folder if folder else directory
        if (parent.is_symlink() or path.is_symlink() or not path.is_file()
                or path.resolve().parent != parent):
            raise ValueError('invalid_media')
        try:
            with Image.open(path) as image:
                return image.convert('RGBA')
        except OSError:
            raise ValueError('invalid_media') from None

    names = [person['image'] for person in people]
    scene_name = value.get('scene_image')
    intro = ' '.join('Image %d is the character %s.' % (i + 1, p['name'] or str(i + 1)) for i, p in enumerate(people))
    if isinstance(scene_name, str) and scene_name:
        layout = collage(load(scene_name), [(load(p['image']), p['x'], p['height'], p['feet']) for p in people],
                         width, height)
        folder = directory / 'freevideo-image'
        if folder.is_symlink() or folder.resolve().parent != directory:
            raise ValueError('invalid_media')
        leaf = 'layout-' + uuid.uuid4().hex + '.png'
        try:
            folder.mkdir(exist_ok=True)
            layout.save(folder / leaf)
        except OSError as error:
            # A runtime layout failure must reach the service's diagnostic-report
            # path, rather than being returned as a malformed request.
            raise ValueError('generation_failed') from error
        names.append('freevideo-image/' + leaf)
        n = len(people)
        prompt = ('%s Image %d is a rough layout with the characters pasted on the scene. Render image %d as one clean '
                  'photograph of the same place with the same people. Keep the scene, keep everyone at exactly the same '
                  'position and size, keep each face, hairstyle and clothes as in their own image. Remove every cut-out '
                  'edge and white outline. Relight everyone to the lighting of the room, ground them with soft contact '
                  'shadows on the floor and slight floor reflections so they stand in the space instead of floating.'
                  % (intro, n + 1, n + 1))
    else:
        if not text.strip():
            raise ValueError('invalid_prompt')
        prompt = '%s %s Keep each face and outfit exactly as in their own image. %s' % (
            intro, _positions(people), 'The scene: ' + text.strip().rstrip('.') + '.')
    if len(prompt) > 2000:
        raise ValueError('invalid_prompt')
    return dict(prompt=prompt, width=width, height=height, seed=value.get('seed'), images=names)
