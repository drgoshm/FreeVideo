const valid = size => [size?.width, size?.height].every(side => Number.isSafeInteger(side) && side >= 256 && side <= 4096 && side % 32 === 0);
// Keep editing/export dimensions independent of the creation panel's preset
// range (up to 2 MP). Capability estimates must use the same size as the request.
export function generationSize(project, sizes = []) {
    const {width, height} = project, area = width * height, ratio = width / height;
    if (valid(project) && area <= 2 * 1024 ** 2) return {width, height};
    let candidates = (sizes || []).filter(valid);
    if (!candidates.length) {
        for (let w = 256; w <= 4096; w += 32)
            for (let h = 256; h <= 4096 && w * h <= 2 * 1024 ** 2; h += 32) candidates.push({width: w, height: h});
    }
    candidates = [...candidates].sort((a, b) => Math.abs(Math.log(a.width / a.height / ratio)) - Math.abs(Math.log(b.width / b.height / ratio))
        || Math.abs(a.width * a.height - area) - Math.abs(b.width * b.height - area));
    return {width: candidates[0].width, height: candidates[0].height};
}
// A part that continues finished footage is made at that footage's shape, whatever the edit's own: the size all the
// footage a request continues has in common (an imported video off the 32-pixel grid gets the nearest size of its
// shape). null when a video in the request starts fresh or the sizes differ; the edit's size then stands.
export function continuedSize(before, sources) {
    const sizes = before.map(key => key && sources.get(key)).map(size =>
        valid(size) ? size : [size?.width, size?.height].every(side => Number.isSafeInteger(side) && side > 0) ? generationSize(size) : null);
    if (!sizes.length || !sizes.every(valid)) return null;
    const {width, height} = sizes[0];
    return sizes.every(size => size.width === width && size.height === height) ? {width, height} : null;
}
