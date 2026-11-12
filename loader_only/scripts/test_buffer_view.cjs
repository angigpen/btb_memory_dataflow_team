// Test the exported viewer's actual replay/render functions without a browser.
// Usage: node loader_only/scripts/test_buffer_view.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../build/waves.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new Function(script); // Parse every viewer function, not only the test subset.
const jsonStart = script.indexOf('const P=') + 'const P='.length;
const jsonEnd = script.indexOf(';\nfor(const k in P.sets)', jsonStart);
const payload = JSON.parse(script.slice(jsonStart, jsonEnd));
const helpers = script.slice(script.indexOf('function lastBufferEvent('), script.indexOf('function rgb('));
const context = {};
vm.createContext(context);
vm.runInContext(helpers, context);

function dense(wave, name) {
  const changes = wave.data[name] || [0, 0];
  const out = new Int32Array(wave.n);
  let cycle = 0;
  for (let i = 0; i < changes.length; i += 2) {
    cycle += changes[i];
    const end = i + 2 < changes.length ? cycle + changes[i + 2] : wave.n;
    out.fill(changes[i + 1] || 0, cycle, end);
  }
  return out;
}
function viewState() {
  const nodes = {};
  return {
    map: new Map(), next: 0, page: 0, follow: {checked: true},
    panel: {querySelector: key => nodes[key] ||= {}},
    nodes,
  };
}
function snapshot(D) {
  return JSON.stringify(['a', 'b'].map(k => [...D.cacheView[k].map]));
}
let tested = 0;
for (const [variant, spec] of Object.entries(payload.sets)) {
  for (const [caseName, wave] of Object.entries(spec.waves)) {
    if (!wave.buffers) continue;
    const D = {
      set: variant,
      ev: {
        m: payload.metrics[variant + '|' + caseName],
        buffers: wave.buffers,
        row: dense(wave, 'tb_gemm.dut.control.row'),
        gs: dense(wave, 'tb_gemm.dut.control.grp_start'),
        gw: dense(wave, 'tb_gemm.dut.control.gw'),
      },
      cacheView: {upto: -1, a: viewState(), b: viewState()},
    };
    context.bufferDraw(D, 0);
    assert.equal(D.cacheView.a.map.size, 0);
    assert.equal(D.cacheView.b.map.size, 0);
    for (const kind of ['a', 'b']) {
      const trace = wave.buffers[kind];
      if (!trace.writes.length) continue;
      const first = trace.writes[0];
      context.bufferDraw(D, first[0] - 1);
      assert.equal(D.cacheView[kind].map.size, 0, 'A request/response must not become a premature RAM write');
      context.bufferDraw(D, first[0]);
      assert.deepEqual(D.cacheView[kind].map.get(first[1]), first);
      const before = snapshot(D);
      context.bufferDraw(D, wave.n - 1);
      context.bufferDraw(D, first[0]);
      assert.equal(snapshot(D), before, 'Backward scrubbing must restore exact prior RAM contents');
    }
    const c = wave.n - 1;
    context.bufferDraw(D, c);
    for (const kind of ['a', 'b']) {
      const trace = wave.buffers[kind];
      const expected = new Map();
      for (const e of trace.writes) if (e[0] <= c) expected.set(e[1], e);
      assert.equal(JSON.stringify([...D.cacheView[kind].map]), JSON.stringify([...expected]));
      const state = D.cacheView[kind];
      if (trace.depth) {
        state.follow.checked = false;
        state.page = Math.ceil(trace.depth / 6) - 1;
        context.bufferDraw(D, c);
        assert.equal(state.nodes['[data-next]'].disabled, true);
        assert(!state.nodes.tbody.innerHTML.includes('undefined'));
      }
    }
    tested++;
  }
}
assert.equal(tested, 12);
console.log(`PASS: ${tested} waveform cases, RAM write timing, signed data, backward scrub, and entry pagination`);
