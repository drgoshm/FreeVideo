// @-mentions of library characters in the prompt box.
// While editing, the text keeps the character's name ("@Su Wan"); the character's pictures are ordinary reference rows
// of the media list that carry `cast: {id, name, description}`. When a request is made the names become <Subject N>
// and the subject definitions go in front, the same wording the Agent's drama planner writes (agent/drama.py).
export const MAX_PEOPLE = 3;
export const MAX_PICTURES_EACH = 6;
export const MAX_REFERENCES = 32;
// Which of a card's pictures come first; the others follow in the card's own order.
export const PICTURE_ORDER = ['sheet', 'front', 'three_quarter', 'face'];

// subjects: [{name, description, pictures: [first, last] | null, audio: number | null}]. This is the one JS place for
// the wording; its Python twin is subject_definitions() and tests/fixtures/subject_definitions.json holds the cases
// both sides must reproduce character for character.
export function subjectDefinitions(subjects) {
    const parts = subjects.map((subject, index) => {
        let text = `<Subject ${index + 1}> is ${subject.name}`;
        const description = String(subject.description || '').trim().replace(/\.+$/, '');
        text += description ? `: ${description}.` : '.';
        const tags = [];
        if (subject.pictures) {
            // One [first, last] pair, or several when the person's pictures are not side by side.
            const ranges = typeof subject.pictures[0] === 'number' ? [subject.pictures] : subject.pictures;
            const pictures = ranges.map(([a, b]) => a === b ? `<Picture ${a}>` : `<Picture ${a}> to <Picture ${b}>`).join(', ');
            const single = ranges.length === 1 && ranges[0][0] === ranges[0][1];
            tags.push(`${pictures} show${single ? 's' : ''} its appearance`);
        }
        if (subject.audio) tags.push(`<Audio ${subject.audio}> is its voice`);
        if (tags.length) text += ' ' + tags.join('; ') + '.';
        return text;
    });
    return 'subject_definitions: ' + parts.join(' ');
}

const IMAGE = /\.(png|jpe?g|webp|bmp|tiff?)$/i;
const enabled = row => row && row.enabled !== false;
// The number H3 gives each reference picture: images in list order (connected inputs come after the list).
// A part that continues a video keeps the first CONTINUE_PICTURES of them (continuation.PICTURES_MAX).
export const CONTINUE_PICTURES = 3;
function pictureNumbers(rows, limit = Infinity) {
    const numbers = new Map();
    let count = 0;
    rows.forEach((row, index) => {
        if (enabled(row) && row.role === 'reference' && IMAGE.test(row.file || '') && count < limit) numbers.set(index, ++count);
    });
    return numbers;
}

// The characters present in the media list, in list order, with the rows that carry each one's pictures.
export function castInRows(rows) {
    const people = new Map();
    (Array.isArray(rows) ? rows : []).forEach((row, index) => {
        if (!enabled(row) || !row.cast?.id || row.role !== 'reference' || !IMAGE.test(row.file || '')) return;
        const name = row.cast.name || '';
        // `handle` is what the text says after the @: the name, or "name (2)" when another present character has the same name.
        const person = people.get(row.cast.id) || {id: row.cast.id, name, handle: row.cast.handle || name, description: row.cast.description || '', rows: []};
        person.rows.push(index); people.set(row.cast.id, person);
    });
    return [...people.values()];
}

// Ids of the characters present in the media list and written in the text, in list order.
export function mentionedIds(text, rows) {
    const people = castInRows(rows), handles = new Set(mentionRanges(String(text ?? ''), people).map(range => range.name));
    return people.filter(person => handles.has(person.handle)).map(person => person.id);
}

const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
// Where `@name` of a present character stands in the text; longer names win, an @ right after a Latin word or an address is
// not a mention (the same rule as the @ list's), but one right after another name or a Chinese character is.
export function mentionRanges(text, people) {
    const names = [...new Set(people.map(person => person.handle ?? person.name).filter(Boolean))].sort((a, b) => b.length - a.length);
    if (!names.length || !text.includes('@')) return [];
    // A name written in Latin letters does not end in the middle of a longer word ("@Su Wanda" is not "@Su Wan").
    const whole = name => escape(name) + (/[A-Za-z0-9_]$/.test(name) ? '(?![A-Za-z0-9_])' : '');
    const pattern = new RegExp('(?<![\\w.%+\\-])@(' + names.map(whole).join('|') + ')', 'gu');
    const ranges = [];
    for (const match of text.matchAll(pattern))
        ranges.push({start: match.index, end: match.index + 1 + match[1].length, name: match[1]});
    return ranges;
}

// The prompt a request carries. Characters mentioned in the text are numbered in order of first mention; one whose
// pictures are gone is written as a plain name. `active: false` (first/last frames) leaves every name plain too, since
// pictures of people cannot go with those. `continued` (the part continues a video) lets the characters stay: the part
// keeps the first three reference pictures and its frames come after them, so a character whose pictures are not among
// those three is written as a plain name.
export function expandCast(text, rows, {active = true, continued = false} = {}) {
    text = String(text ?? '');
    const list = Array.isArray(rows) ? rows : [];
    const people = castInRows(list);
    const ranges = mentionRanges(text, people);
    if (!ranges.length) return {text, subjects: []};
    if (!continued && list.some(row => enabled(row) && (row.role === 'first' || row.role === 'last'))) active = false;
    const numbers = pictureNumbers(list, continued ? CONTINUE_PICTURES : Infinity);
    const order = [];
    let result = '', at = 0;
    for (const range of ranges) {
        result += text.slice(at, range.start); at = range.end;
        const person = people.find(p => (p.handle ?? p.name) === range.name);
        if (!active || !person || !person.rows.some(index => numbers.has(index))) { result += person ? person.name : range.name; continue; }
        let number = order.indexOf(person) + 1;
        if (!number) {
            if (order.length >= MAX_PEOPLE) { result += person.name; continue; }
            order.push(person); number = order.length;
        }
        result += `<Subject ${number}>`;
    }
    result += text.slice(at);
    if (!order.length) return {text: result, subjects: []};
    const subjects = order.map(person => {
        // Consecutive picture numbers make one range; a person whose pictures are apart gets several.
        const ranges = [];
        for (const number of person.rows.map(index => numbers.get(index)).filter(Boolean).sort((a, b) => a - b)) {
            const last = ranges[ranges.length - 1];
            if (last && number === last[1] + 1) last[1] = number; else ranges.push([number, number]);
        }
        return {name: person.name, description: person.description, pictures: ranges, audio: null};
    });
    return {text: subjectDefinitions(subjects) + '\n\n' + result, subjects};
}

// The pictures of a card to put in the media list: the card's own order after the preferred labels, the first six.
export function cardPictures(card) {
    const rank = new Map(PICTURE_ORDER.map((label, index) => [label, index]));
    return (card?.images || []).map((image, index) => ({image, index}))
        .filter(({image}) => image?.media?.file && IMAGE.test(image.media.file))
        .sort((a, b) => (rank.get(a.image.label) ?? PICTURE_ORDER.length) - (rank.get(b.image.label) ?? PICTURE_ORDER.length) || a.index - b.index)
        .slice(0, MAX_PICTURES_EACH).map(({image}) => image.media.file);
}

// Whether a character can be mentioned now; `reason` is the one thing in the way.
// `count` is how many pictures the card would add when only its list row is known.
export function mentionState(rows, card, count = cardPictures(card).length, {continued = false} = {}) {
    const list = Array.isArray(rows) ? rows : [];
    const people = castInRows(list);
    if (people.some(person => person.id === card.id)) return {ok: true, present: true};
    if (!continued && list.some(row => enabled(row) && (row.role === 'first' || row.role === 'last'))) return {ok: false, reason: 'frames'};
    if (people.length >= MAX_PEOPLE) return {ok: false, reason: 'people'};
    if (continued && pictureNumbers(list).size >= CONTINUE_PICTURES) return {ok: false, reason: 'continued'};
    if (list.filter(enabled).length + count > MAX_REFERENCES) return {ok: false, reason: 'full'};
    return {ok: true, present: false};
}

// What the text says after the @ for this card: its name, unless another character in the list already goes by that
// name (the library allows two cards with one name); then "name (2)", "name (3)" … so each @ points at one card.
// `pending` maps ids of characters chosen a moment ago, whose rows have not landed yet, to the handle already typed.
export function mentionHandle(rows, card, pending = new Map()) {
    const people = castInRows(rows), present = people.find(person => person.id === card.id);
    if (present) return present.handle;
    if (pending.has(card.id)) return pending.get(card.id);
    const taken = new Set([...people.map(person => person.handle), ...pending.values()]);
    let handle = card.name, number = 1;
    while (taken.has(handle)) handle = `${card.name} (${++number})`;
    return handle;
}

// The media list with the character's pictures appended together, so one range of picture numbers covers them.
export function withCharacter(rows, card, typed = '') {
    const list = Array.isArray(rows) ? rows.map(row => ({...row})) : [];
    if (castInRows(list).some(person => person.id === card.id)) return list;
    const handle = typed || mentionHandle(list, card);
    const cast = {id: card.id, name: card.name, description: card.description || '', ...(handle === card.name ? {} : {handle})};
    return [...list, ...cardPictures(card).map(file => ({file, role: 'reference', enabled: true, cast}))];
}
