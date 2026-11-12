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
assert.equal(payload.defaultCase, 'inspect_8x8x6');
assert.equal(payload.cases[0], payload.defaultCase);
const groupCase = 'inspect_set3_groups';
assert(payload.cases.includes(groupCase));
assert.deepEqual(payload.order.filter(v => payload.sets[v].waves[groupCase]), ['s3_all']);
const smallGroup = payload.sets.s3_all.waves[groupCase];
assert.equal(smallGroup.trunc, false);
assert.equal(smallGroup.params.BBUF_DEPTH, 4);
assert.deepEqual([smallGroup.matrices.a.length, smallGroup.matrices.b.length, smallGroup.matrices.c.length], [16, 32, 128]);
assert.deepEqual(['a_reads', 'b_reads'].map(k => +payload.metrics['s3_all|' + groupCase][k]), [32, 32]);
for (const [variant, spec] of Object.entries(payload.sets)) {
  const demo = spec.waves[payload.defaultCase], m = demo.matrices;
  assert.equal(demo.trunc, false, 'Small demo must include the complete computation');
  assert.deepEqual([m.a.length, m.b.length, m.c.length], [48, 48, 64]);
  assert(m.a.concat(m.b).every(x => x >= -3 && x <= 3));
  for (let row = 0; row < 4; row++) assert.equal(m.a[row * 6 + 2], 0);
  assert(m.a.slice(24).some((x, i) => i % 6 === 2 && x !== 0));
  for (let row = 0; row < 8; row++) for (let col = 0; col < 8; col++) {
    let sum = 0;
    for (let k = 0; k < 6; k++) sum += m.a[row * 6 + k] * m.b[k * 8 + col];
    assert.equal(m.c[row * 8 + col], sum, 'Small matrix output must equal exact integer GEMM');
  }
  const metrics = payload.metrics[variant + '|' + payload.defaultCase];
  if (variant === 'baseline') assert.deepEqual([+metrics.a_reads, +metrics.b_reads], [96, 96]);
  if (variant === 's2_all' || variant === 's3_all') {
    assert.deepEqual([+metrics.a_reads, +metrics.b_reads], [48, 48]);
    for (const marker of ['A 재사용', 'B 재사용', 'B zero skip', 'B 미저장분 재읽기']) {
      assert(Number.isInteger(demo.markers[marker]), marker);
    }
  }
}
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
        n: wave.n,
        m: payload.metrics[variant + '|' + caseName],
        buffers: wave.buffers,
        row: dense(wave, 'tb_gemm.dut.control.row'),
        gs: dense(wave, 'tb_gemm.dut.control.grp_start'),
        gw: dense(wave, 'tb_gemm.dut.control.gw'),
        packets: wave.packets,
        full: dense(wave, 'tb_gemm.dut.loader.full'),
        alloc: dense(wave, 'tb_gemm.dut.loader.alloc'),
        prep: dense(wave, 'tb_gemm.dut.loader.prep'),
        rd: dense(wave, 'tb_gemm.dut.loader.rd'),
        wr: dense(wave, 'tb_gemm.dut.loader.wr'),
        ip: dense(wave, 'tb_gemm.dut.loader.ip'),
        lst: dense(wave, 'tb_gemm.dut.loader.state'),
        memV: dense(wave, 'tb_gemm.dut.mem_req_valid'),
        memR: dense(wave, 'tb_gemm.dut.mem_req_ready'),
        memRsp: dense(wave, 'tb_gemm.dut.mem_rsp_valid'),
        isB: dense(wave, 'tb_gemm.dut.mem_req_is_b'),
        opV: dense(wave, 'tb_gemm.dut.loader.op_valid'),
        opR: dense(wave, 'tb_gemm.dut.loader.op_ready'),
      },
      cacheView: {upto: -1, a: viewState(), b: viewState()},
    };
    if (variant === 's3_all' && [groupCase, 'bench_64x128x128_l23'].includes(caseName)) {
      const small = caseName === groupCase;
      const groups = context.groupRanges(D.ev);
      const ranges = small ? [[0, 8], [8, 16]] : [[0, 32], [32, 64], [64, 96], [96, 128]];
      assert.equal(JSON.stringify(groups.map(g => [g.start, g.end])), JSON.stringify(ranges));
      const B = D.cacheView.b;
      const depth = small ? 4 : 1024;
      assert.equal(wave.buffers.b.depth, depth);
      assert.equal(wave.buffers.b.writes.filter(e => e[4] !== null).length, depth * ranges.length);
      for (const [i, g] of groups.entries()) {
        assert(g.cycle < g.fill && g.fill < g.read);
        context.bufferDraw(D, g.cycle);
        assert.match(B.nodes['.buffer-meta'].textContent, /사용 가능 0 \/ 물리 용량/);
        if (i > 0) {
          assert.equal(B.map.size, depth, 'Physical values survive the group switch');
          assert.equal(B.map.get(0)[2], groups[i - 1].start, 'Entry 0 still holds the previous group');
          assert.match(B.nodes.tbody.innerHTML, /현재 재사용 불가/);
        }
        context.bufferDraw(D, g.fill);
        assert.equal(B.map.get(0)[2], g.start, 'The same physical entry is overwritten by the new group');
        assert.match(B.nodes.tbody.innerHTML, /◀채움/);
        context.bufferDraw(D, g.read);
        assert.match(B.nodes['.buffer-meta'].textContent, new RegExp(`사용 가능 ${depth} /`));
        assert.match(B.nodes.tbody.innerHTML, /◀조회/);
      }
    }
    const packets = wave.packets;
    assert(packets, 'Every optimized waveform must contain packet lane events');
    D.packetView = {
      upto: -1, next: 0, states: Array(packets.count).fill(null),
      changed: Array.from({length: packets.count}, () => ({a: [], b: []})),
      cards: Array.from({length: packets.count}, () => ({})),
    };
    const packetSnapshots = new Map();
    for (let c = 0; c < wave.n; c++) {
      context.packetReplay(D, c);
      let pending = 0, active = 0;
      for (let i = 0; i < packets.count; i++) {
        const p = D.packetView.states[i];
        const status = context.packetStatus(D, i, c);
        assert.notEqual(status.short, 'loading');
        if (status.kind === 'active') active++;
        if (variant !== 's1_all') {
          assert.equal(!!p, !!((D.ev.alloc[c] >> i) & 1), `${variant}/${caseName} cycle ${c} allocation`);
          if (p) assert.equal(!!p.prepared, !!((D.ev.prep[c] >> i) & 1));
        }
        if (!p) { assert.equal(status.short, 'empty'); continue; }
        if (p.pending) pending++;
        const ready = p.prepared && p.a.concat(p.b).every(x => x !== null);
        if (variant === 's1_all' && ((D.ev.full[c] >> i) & 1)) assert(ready);
        if (i === D.ev.rd[c] && D.ev.opV[c]) assert(ready, 'MAC must never see an incomplete operand');
        if (variant !== 's1_all' && i === D.ev.rd[c]) assert.equal(!!ready, !!D.ev.opV[c]);
        for (const k of ['a', 'b']) p[k].forEach((value, lane) => {
          assert.equal(value === null, p[k + 's'][lane] === null);
          if (value !== null) assert(value >= -128 && value <= 127);
          if (p[k + 's'][lane] === 'p' || p[k + 's'][lane] === 'z') assert.equal(value, 0);
        });
      }
      assert(pending <= 1, 'Only one outstanding memory read');
      assert(active <= 1, 'Never show several external readers at once');
      if (c % Math.max(1, Math.floor(wave.n / 30)) === 0 || c === wave.n - 1) {
        context.packetDraw(D, c);
        for (const card of D.packetView.cards) {
          assert(!card.innerHTML.includes('undefined'));
          assert(!card.innerHTML.includes('null'));
        }
        packetSnapshots.set(c, JSON.stringify(D.packetView.states));
      }
    }
    for (const [c, expected] of [...packetSnapshots].reverse()) {
      context.packetDraw(D, c);
      assert.equal(JSON.stringify(D.packetView.states), expected, 'Backward scrubbing must restore per-slot lanes');
    }
    for (const e of packets.events.filter(e => e[2] === 4).slice(0, 16)) {
      const [at, slot, , isB, lane, value] = e, k = isB ? 'b' : 'a';
      context.packetReplay(D, at - 1);
      assert.equal(D.packetView.states[slot][k][lane], null, 'An arriving byte must remain pending before its write edge');
      context.packetReplay(D, at);
      assert.equal(D.packetView.states[slot][k][lane], value);
      assert.equal(D.packetView.states[slot][k + 's'][lane], 'm');
    }
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
assert.equal(tested, (payload.cases.length - 1) * (payload.order.length - 1) + 1);
console.log(`PASS: ${tested} waveform cases, group switches and overwrites, packet lanes on every cycle, one external reader, RAM/response timing, signed data, backward scrub, and entry pagination`);
