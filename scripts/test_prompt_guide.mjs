import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

// Evaluate the actual shared guide used in both views without ComfyUI or a GPU.
const source = await readFile(new URL('../web/studio.js', import.meta.url), 'utf8');
const guide = source.slice(source.indexOf('export function promptGuide()'), source.indexOf('export function loraPanel('));
for (const language of ['en', 'zh']) {
    test(`prompt guide recommends external AI in ${language}`, () => {
        const el = (tag, text, cls) => ({tag, text, cls, children: [], append(...items) { this.children.push(...items); }});
        const document = {createTextNode: text => ({text})};
        const t = (en, zh) => language === 'zh' ? zh : en;
        const render = new Function('el', 'document', 't', guide.replace('export ', '') + '\nreturn promptGuide();');
        const result = render(el, document, t);
        const links = result.children.filter(child => child.tag === 'a');
        assert.equal(links.length, 2);
        assert.ok(links[0].href.endsWith('/skills/h3-prompt-writing/SKILL.md'));
        assert.equal(links[1].href, 'https://www.flashml.ai/');
        for (const link of links) {
            assert.equal(link.target, '_blank');
            assert.equal(link.rel, 'noopener noreferrer');
        }
        const text = result.children.map(child => child.text).join('');
        assert.ok(text.includes(language === 'zh' ? '让 AI 参考 MiniMax H3 Prompt Skill ↗ 重写提示词' : 'ask AI to rewrite'));
        assert.ok(text.includes('FreeToken'));
        assert.equal(result.children.some(child => child.tag === 'button' || child.tag === 'input'), false);
    });
}
