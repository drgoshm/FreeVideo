// Small, shared presentation glyphs. Accessible names belong to their controls.
const paths = {
    adjust: 'M3 1v14M8 1v14M13 1v14M1 5h4M6 11h4M11 5h4',
    fit: 'M1 6V1h5M10 1h5v5M15 10v5h-5M6 15H1v-5',
    check: 'm3 8 3 3 7-7',
};
export function editorIcon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 16 16'); svg.setAttribute('aria-hidden', 'true');
    svg.classList.add('fv-ed-glyph');
    const path = document.createElementNS(svg.namespaceURI, 'path');
    path.setAttribute('d', paths[name]); svg.append(path); return svg;
}
