const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../app/web/static/workbench.js'), 'utf8');
function element(text = '') {
  return {text, children: [], attrs: {}, append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    setAttribute(key, value) { this.attrs[key] = value; },
    removeAttribute(key) { delete this.attrs[key]; }, addEventListener() {}};
}
const elements = new Map();
let item = {transcript: {path: 'outputs/example_transcript.json'}, label: 'Transcript'};
const state = {tab: 'summary', readerRequest: 0};
const context = vm.createContext({state, selected: () => item,
  $: id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
  node: (_, text) => element(text), safe: fn => fn, updateActions() {},
  readDocument: async path => { context.readPath = path; }});
vm.runInContext(source.slice(source.indexOf('function resultViews('), source.indexOf('async function readDocument('))
  + source.slice(source.indexOf('function renderExports('), source.indexOf('function updateActions(')), context);
(async () => {
  await vm.runInContext('renderResults()', context);
  assert.equal(context.readPath, 'outputs/example_transcript.md');
  const exports = elements.get('exportLinks').children.map(link => link.href);
  for (const extension of ['md', 'srt', 'json']) assert(exports.some(link => decodeURIComponent(link).endsWith(`example_transcript.${extension}`)));
  elements.get('reader').replaceChildren(element('Previous document'));
  const previousRequest = state.readerRequest;
  item = {label: 'Empty task'};
  await vm.runInContext('renderResults()', context);
  assert.equal(elements.get('reader').children[0].text, '此任务暂无可阅读的结果。');
  assert(state.readerRequest > previousRequest);
  assert.equal(elements.get('reader').attrs['aria-labelledby'], undefined);
  assert.equal(elements.get('exportLinks').children.length, 0);
  item = {run_id: 'example', label: 'Player', preview: {files: {
    'run_audio_subtitle_player.cmd': {path: 'outputs/player.cmd'},
    'live_preview.mp4': {path: 'outputs/video.mp4'},
  }, audio_stale: false, video_stale: true}};
  await vm.runInContext('renderResults()', context);
  const playerExports = elements.get('exportLinks').children.map(link => link.href);
  assert.deepEqual(playerExports, ['/api/artifacts/example/player-export']);
  item.preview.audio_stale = true;
  await vm.runInContext('renderResults()', context);
  assert.equal(elements.get('exportLinks').children.length, 0);
  vm.runInContext(source.slice(source.indexOf('function updateActions('), source.indexOf('async function reanalyze(')), context);
  await vm.runInContext('updateActions()', context);
  assert.equal(elements.get('playBtn').disabled, true);
  item.preview.audio_stale = false;
  state.running = false;
  await vm.runInContext('updateActions()', context);
  assert.equal(elements.get('playBtn').disabled, false);
  item.preview.audio_stale = true;
  item.preview.video_stale = false;
  await vm.runInContext('updateActions()', context);
  assert.equal(elements.get('playBtn').disabled, false);
  assert.equal(elements.get('playBtn').textContent, '播放带字幕视频');
  assert.equal(vm.runInContext('currentPlayback(selected()).file.path', context), 'outputs/video.mp4');
  state.running = true;
  await vm.runInContext('updateActions()', context);
  assert.equal(elements.get('playBtn').disabled, true);
  console.log('Workbench result regressions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
