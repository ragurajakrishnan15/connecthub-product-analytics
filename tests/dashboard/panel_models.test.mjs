// Tests for the dashboard's panel models (data -> display) and for the page itself
// (PHASE_5_PLAN.md step 3). The models are extracted from index.html between the PANEL MODELS
// markers, so these tests run what ships. Fixtures are trimmed real API responses.
// Run through pytest (tests/test_dashboard_data_layer.py) or: node --test tests/dashboard
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const html = readFileSync(resolve(root, 'index.html'), 'utf8');
const fx = name => JSON.parse(readFileSync(resolve(root, 'tests', 'dashboard', 'fixtures', name + '.json'), 'utf8'));

const between = (a, b) => html.slice(html.indexOf(a), html.indexOf(b));
const layerSource = between('// ===== DATA LAYER: BEGIN =====', '// ===== DATA LAYER: END =====');
const modelSource = between('// ===== PANEL MODELS: BEGIN =====', '// ===== PANEL MODELS: END =====');
assert.ok(modelSource.length > 2000, 'panel models markers not found in index.html');

const M = new Function(`${modelSource}
  return { formatValue, formatChange, kpiTooltip, formatDay, formatMonth, shiftDays, trailingWindow, topbarModel,
           engagementModel, adoptionModel, revenueModel, resolutionModel, supportCards, npsModel, heatColor,
           cohortModel, funnelModel, milestoneModel, experimentOptions, experimentModel, healthModel,
           workspaceRows, titleCase, formatP, FEATURE_LABELS, TIER_COLORS };`)();
const { ENDPOINTS } = new Function(`${layerSource}\nreturn { ENDPOINTS };`)();

// ---------------------------------------------------------------- formatting
describe('formatValue', () => {
  it('formats by the API unit', () => {
    assert.equal(M.formatValue(61870, 'usd'), '$61,870');
    assert.equal(M.formatValue(2400000, 'usd'), '$2.40M');
    assert.equal(M.formatValue(671, 'users'), '671');
    assert.equal(M.formatValue(42891, 'users'), '42,891');
    assert.equal(M.formatValue(0.39434, 'fraction'), '39.4%');
    assert.equal(M.formatValue(8.43, 'nps_points'), '8.4');
    assert.equal(M.formatValue(0, 'fraction'), '0.0%');
  });
  it('a missing value is a dash, never zero', () => {
    for (const v of [null, undefined, NaN, Infinity, 'x']) assert.equal(M.formatValue(v, 'usd'), '—');
  });
  it('rates are fractions: 1.0 is 100%, never 3840%-style double scaling', () => {
    assert.equal(M.formatValue(1, 'fraction'), '100.0%');
  });
});

describe('formatChange', () => {
  it('uses percentage points for rates, % for money and counts, points for NPS', () => {
    assert.deepEqual(M.formatChange({ unit: 'fraction', change_abs: -0.0157 }), { text: '↓ 1.6pp', dir: 'down' });
    assert.deepEqual(M.formatChange({ unit: 'usd', change_rel: 0.0881, change_abs: 5010 }), { text: '↑ 8.8%', dir: 'up' });
    assert.deepEqual(M.formatChange({ unit: 'users', change_rel: -0.0118, change_abs: -8 }), { text: '↓ 1.2%', dir: 'down' });
    assert.deepEqual(M.formatChange({ unit: 'nps_points', change_abs: 7.1, change_rel: 5.46 }), { text: '↑ 7.1', dir: 'up' });
  });
  it('a change that rounds to nothing is flat, not up or down', () => {
    assert.equal(M.formatChange({ unit: 'fraction', change_abs: 0.00001 }).dir, 'flat');
    assert.equal(M.formatChange({ unit: 'usd', change_rel: 0 }).dir, 'flat');
  });
  it('no previous period means no change shown', () => {
    assert.deepEqual(M.formatChange({ unit: 'usd', change_rel: null }), { text: '—', dir: 'none' });
    assert.deepEqual(M.formatChange(undefined), { text: '—', dir: 'none' });
  });
});

describe('KPI tooltips', () => {
  it('carry the API definition, both periods and any note', () => {
    const k = fx('overview').data.kpis.mrr_usd;
    const tip = M.kpiTooltip(k);
    assert.ok(tip.includes(k.definition));
    assert.ok(tip.includes('Current: Dec 1, 2025 – Dec 31, 2025'));
    assert.ok(tip.includes('Previous: Nov 1, 2025 – Nov 30, 2025'));
    assert.equal(M.kpiTooltip({ ...k, period: { start: '2025-12-31', end: '2025-12-31' } }).includes('Current: Dec 31, 2025\n'), true);
  });
});

describe('dates', () => {
  it('formats ISO dates without time-zone drift', () => {
    assert.equal(M.formatDay('2025-01-02'), 'Jan 2, 2025');
    assert.equal(M.formatDay('2026-10-05T23:00:40.189050Z'), 'Oct 5, 2026');
    assert.equal(M.formatMonth('2025-12-01'), 'Dec 2025');
    assert.equal(M.formatDay(null), '—');
    assert.equal(M.formatDay('garbage'), '—');
  });
  it('shifts across month, year and leap boundaries', () => {
    assert.equal(M.shiftDays('2025-03-01', -1), '2025-02-28');
    assert.equal(M.shiftDays('2024-03-01', -1), '2024-02-29');
    assert.equal(M.shiftDays('2025-01-01', -1), '2024-12-31');
    assert.equal(M.shiftDays('2025-12-31', -364), '2025-01-01');
  });
  it('a trailing window ends at the data end and never starts before the data', () => {
    assert.deepEqual(M.trailingWindow('2025-12-31', 365, '2025-01-02'), { start: '2025-01-02', end: '2025-12-31' });
    assert.deepEqual(M.trailingWindow('2025-12-31', 30, '2025-01-02'), { start: '2025-12-02', end: '2025-12-31' });
    assert.deepEqual(M.trailingWindow('2025-12-31', 365, undefined), { start: '2025-01-01', end: '2025-12-31' });
  });
});

describe('top bar', () => {
  it('shows the data window, the dataset label and the last validated run (not the clock)', () => {
    const m = M.topbarModel(fx('meta').data);
    assert.equal(m.pill, 'Data as of Oct 5, 2026');
    assert.equal(m.range, 'Jan 2, 2025 — Dec 31, 2025 · synthetic data');
  });
  it('says so when no pipeline run was ever validated', () => {
    const meta = { ...fx('meta').data, last_successful_run: null };
    assert.equal(M.topbarModel(meta).pill, 'No validated pipeline run');
  });
});

// ---------------------------------------------------------------- overview charts
describe('overview models', () => {
  it('engagement: one point per month, partial months starred', () => {
    const m = M.engagementModel(fx('engagement_month').data);
    assert.equal(m.labels.length, 12);
    assert.equal(m.labels[0], 'Jan 2025*');
    assert.equal(m.labels[11], 'Dec 2025');
    assert.deepEqual(m.values, fx('engagement_month').data.points.map(p => p.avg_dau));
    assert.match(m.subtitle, /partial month/);
  });
  it('adoption plots observed_rate (not the understated cumulative_rate) as percent', () => {
    const data = fx('feature_adoption').data;
    const m = M.adoptionModel(data);
    assert.equal(m.datasets.length, data.curves.length);
    assert.deepEqual(m.labels, ['Day 0', 'Day 1', 'Day 2', 'Day 3', 'Day 4', 'Day 5']);
    assert.equal(m.datasets[0].label, 'AI Assist');
    assert.equal(m.datasets[0].data[5], data.curves[0].points[5].observed_rate * 100);
    assert.notEqual(data.curves[0].points[5].observed_rate, data.curves[0].points[5].cumulative_rate);
  });
  it('adoption: unknown features get a readable label and gaps stay gaps', () => {
    const data = { curves: [{ feature: 'new_thing', is_ai: false, points: [{ day: 0, observed_rate: null }, { day: 1, observed_rate: 0.5 }] }] };
    const m = M.adoptionModel(data);
    assert.equal(m.datasets[0].label, 'New Thing');
    assert.deepEqual(m.datasets[0].data, [null, 50]);
  });
  it('revenue: highest plan first, in $K, zero for a plan with no revenue', () => {
    const data = fx('revenue').data;
    const m = M.revenueModel(data, ['Free', 'Essentials', 'Professional', 'Enterprise']);
    assert.deepEqual(m.datasets.map(d => d.label), ['Enterprise', 'Professional', 'Essentials', 'Free']);
    const last = data.series[data.series.length - 1];
    const stacked = m.datasets.reduce((sum, d) => sum + d.data[d.data.length - 1], 0) * 1000;
    assert.ok(Math.abs(stacked - last.mrr_usd) < 0.01);
    assert.ok(m.datasets[3].data.every(v => v === 0));
    assert.equal(m.labels[2], 'Dec 2025');
  });
  it('revenue: plan order falls back to what the data contains', () => {
    const m = M.revenueModel({ series: [{ month: '2025-01-01', by_plan: { A: { mrr_usd: 1000 }, B: { mrr_usd: 2000 } } }] }, null);
    assert.deepEqual(m.datasets.map(d => d.label), ['B', 'A']);
  });
  it('AI resolution rate: percent, with gaps for months without calls', () => {
    const m = M.resolutionModel({ series: [{ period_start: '2025-01-01', ai_resolution_rate: 0.5 }, { period_start: '2025-02-01', ai_resolution_rate: null }] });
    assert.deepEqual(m.values, [50, null]);
  });
});

describe('voice of customer', () => {
  it('support cards come straight from the response', () => {
    const d = fx('support_month').data;
    const c = M.supportCards(d);
    assert.equal(c.tickets.value, d.tickets.created.toLocaleString('en-US'));
    assert.match(c.tickets.sub, new RegExp(`^${d.tickets.resolved.toLocaleString('en-US')} resolved`));
    assert.equal(c.ai_resolution.value, (d.ai_agent.ai_resolution_rate * 100).toFixed(1) + '%');
    assert.equal(c.csat.value, d.ai_agent.avg_csat.toFixed(2));
  });
  it('NPS card, trend and per-plan bars', () => {
    const d = fx('nps').data;
    const m = M.npsModel(d);
    assert.equal(m.card.value, d.summary.nps.toFixed(1));
    assert.match(m.card.sub, /^±/);
    assert.equal(m.trend.values.length, d.series.length);
    assert.deepEqual(m.byPlan.labels, d.by_plan.map(p => p.plan_tier));
  });
  it('a suppressed NPS is a dash with the reason, never a number', () => {
    const m = M.npsModel({ summary: { nps: null, responses: 12, suppressed_reason: 'fewer than 30 responses' }, series: [{ period_start: '2025-01-01', nps: null, is_complete: true }], by_plan: [{ plan_tier: 'Free', nps: null, responses: 3 }] });
    assert.equal(m.card.value, '—');
    assert.match(m.card.sub, /fewer than 30 responses/);
    assert.deepEqual(m.trend.values, [null]);
    assert.deepEqual(m.byPlan.values, [null]);
  });
});

// ---------------------------------------------------------------- retention / activation
describe('cohort matrix', () => {
  it('labels cohorts by week start and keeps unobserved weeks as gaps', () => {
    const d = fx('cohorts').data;
    const m = M.cohortModel(d);
    assert.equal(m.weeks.length, d.weeks.length);
    assert.equal(m.weeks[0], 'W0');
    assert.equal(m.rows[0].label, 'Jun 30');
    assert.equal(m.rows[0].size, d.cohorts[0].cohort_size);
    assert.equal(m.rows[0].cells[0], 100);
    assert.equal(m.rows[0].cells[1], d.cohorts[0].cells[1].retention_rate * 100);
    assert.deepEqual(m.rows[3].cells.slice(1), new Array(d.weeks.length - 1).fill(null));
  });
  it('adds the year to the label only when the cohorts span years', () => {
    const d = { as_of: '2026-01-10', weeks: [0], cohorts: [{ cohort_week: '2025-12-29', cohort_size: 5, cells: [{ retention_rate: 1 }] }, { cohort_week: '2026-01-05', cohort_size: 4, cells: [{ retention_rate: 1 }] }] };
    assert.deepEqual(M.cohortModel(d).rows.map(r => r.label), ["Dec 29, '25", "Jan 5, '26"]);
  });
  it('heat colors keep the established thresholds', () => {
    assert.match(M.heatColor(80), /^rgba\(99,102,241,0\.8\)$/);
    assert.match(M.heatColor(40), /^rgba\(96,165,250,0\.4\)$/);
    assert.match(M.heatColor(10), /^rgba\(251,191,36,0\.1\)$/);
    assert.match(M.heatColor(1), /,0\.08\)$/);
  });
});

describe('activation models', () => {
  it('funnel: counts from the API, bar widths relative to signups', () => {
    const d = fx('activation').data;
    const m = M.funnelModel(d);
    assert.deepEqual(m.stages.map(s => s.label), ['Signed Up', 'Placed First Call', 'Call + AI Feature', 'Activated (14d)']);
    assert.equal(m.stages[0].width, 100);
    assert.ok(Math.abs(m.stages[1].width - d.funnel[1].users / d.funnel[0].users * 100) < 1e-9);
    assert.equal(m.stages[1].users, d.funnel[1].users.toLocaleString('en-US'));
    assert.match(m.subtitle, /through Dec 17, 2025/);
  });
  it('funnel: no signups does not divide by zero', () => {
    const m = M.funnelModel({ signups: 0, complete_windows_through: '2025-01-01', funnel: [{ stage: 'signed_up', users: 0, rate_of_signups: null }] });
    assert.equal(m.stages[0].width, 0);
    assert.equal(m.stages[0].ofSignups, '—');
  });
  it('milestones: medians with the 75th percentile for the tooltip', () => {
    const d = fx('activation').data;
    const m = M.milestoneModel(d);
    assert.deepEqual(m.values, d.time_to_milestone.map(r => r.median_days));
    assert.deepEqual(m.p75, d.time_to_milestone.map(r => r.p75_days));
    assert.equal(m.labels[0], 'First Call');
  });
});

// ---------------------------------------------------------------- experiments
describe('experiment selector', () => {
  it('lists every registered experiment and prefers the first real A/B test', () => {
    const m = M.experimentOptions(fx('experiments').data);
    assert.deepEqual(m.options.map(o => o.id), ['exp_ai_summary_v1', 'exp_onboarding_v2']);
    assert.equal(m.options[0].label, 'exp_ai_summary_v1 (A/A check)');
    assert.equal(m.defaultId, 'exp_onboarding_v2');
  });
  it('falls back to the first experiment, and handles none', () => {
    const only = { experiments: [{ experiment: { experiment_id: 'only_aa', kind: 'aa' } }] };
    assert.equal(M.experimentOptions(only).defaultId, 'only_aa');
    assert.deepEqual(M.experimentOptions({ experiments: [] }), { options: [], defaultId: null });
  });
});

describe('experiment model', () => {
  const ab = M.experimentModel(fx('experiment_ab').data);
  const detail = fx('experiment_ab').data;
  it('control and treatment rates with the sample behind them', () => {
    const [control, treatment] = ab.cards;
    assert.equal(control.value, (detail.evaluation.primary.control_rate * 100).toFixed(1) + '%');
    assert.equal(treatment.value, (detail.evaluation.primary.treatment_rate * 100).toFixed(1) + '%');
    assert.match(control.sub, new RegExp('n = ' + detail.evaluation.sample_sizes.control_14d_window.toLocaleString('en-US')));
    assert.equal(treatment.tone, 'good');
    assert.equal(control.tone, '');
  });
  it('lift, p-value and the confidence interval', () => {
    const lift = ab.cards[2];
    assert.equal(lift.value, '+' + (detail.evaluation.primary.relative_lift * 100).toFixed(1) + '%');
    assert.match(lift.sub, /p-value: <0\.001 · 95% CI 5\.1 to 8\.4 pp/);
  });
  it('Bayesian, SRM and one card per guardrail', () => {
    assert.equal(ab.cards[3].label, 'Bayesian P(Treatment Better)');
    assert.equal(ab.cards[4].value, '✓ Passed');
    const guardrails = ab.cards.filter(c => c.label.startsWith('Guardrail:'));
    assert.equal(guardrails.length, detail.evaluation.guardrails.length);
    assert.match(guardrails[0].sub, /min → .* min/);
    assert.match(guardrails[1].sub, /^\$\d+\.\d{2} → \$\d+\.\d{2}/);
    assert.ok(guardrails.every(c => c.tone === 'good'));
  });
  it('a failed guardrail or a detected SRM is flagged bad', () => {
    const bad = JSON.parse(JSON.stringify(detail));
    bad.evaluation.guardrails[0].failed = true;
    bad.evaluation.srm.detected = true;
    const m = M.experimentModel(bad);
    assert.equal(m.cards[4].value, '✗ Detected');
    assert.equal(m.cards[4].tone, 'bad');
    assert.equal(m.cards.find(c => c.label.startsWith('Guardrail:')).value, '✗ Failed');
  });
  it('the verdict is the API decision, styled by its code', () => {
    assert.equal(ab.verdict.text, detail.evaluation.decision_text);
    assert.equal(ab.verdict.cls, 'ship');
    for (const [code, cls] of [['SHIP', 'ship'], ['CONTINUE', 'continue'], ['HOLD', 'hold'], ['REVERT', 'revert'], ['SOMETHING_NEW', 'unknown']]) {
      const d = JSON.parse(JSON.stringify(detail));
      d.evaluation.decision_code = code;
      assert.equal(M.experimentModel(d).verdict.cls, cls, code);
    }
  });
  it('the curve has both arms, control dashed, as percent', () => {
    assert.equal(ab.curve.datasets.length, 2);
    assert.equal(ab.curve.datasets[0].label, 'Control');
    assert.equal(ab.curve.datasets[0].dashed, true);
    assert.equal(ab.curve.datasets[1].label, 'Treatment');
    assert.equal(ab.curve.labels[14], 'Day 14');
    assert.equal(ab.curve.datasets[1].data[14], detail.activation_curve[1].points[14].activation_rate * 100);
  });
  it('an A/A check is marked, not presented as a win', () => {
    const aa = M.experimentModel(fx('experiment_aa').data);
    assert.equal(aa.isAA, true);
    assert.equal(ab.isAA, false);
  });
  it('an experiment without an evaluation still has a model and no verdict', () => {
    const m = M.experimentModel({ ...detail, evaluation: null });
    assert.equal(m.evaluation, null);
    assert.deepEqual(m.cards, []);
    assert.equal(m.verdict, null);
    assert.equal(m.curve.datasets.length, 2);
  });
});

// ---------------------------------------------------------------- customer health
describe('health models', () => {
  it('tier cards in display order with the API counts and shares', () => {
    const d = fx('customer_health').data;
    const m = M.healthModel(d);
    assert.deepEqual(m.cards.map(c => c.tier), ['Champion', 'Healthy', 'At Risk', 'Critical']);
    const critical = d.tiers.find(t => t.tier === 'Critical');
    assert.equal(m.cards[3].count, critical.workspaces.toLocaleString('en-US'));
    assert.equal(m.cards[3].share, (critical.share * 100).toFixed(1) + '%');
  });
  it('a bar color per histogram bin, taken from the bin tier (the old single-color bug)', () => {
    const d = fx('customer_health').data;
    const m = M.healthModel(d);
    assert.equal(m.hist.values.length, d.histogram.length);
    assert.equal(m.hist.colors.length, d.histogram.length);
    assert.equal(m.hist.labels[0], '0–5');
    d.histogram.forEach((b, i) => assert.equal(m.hist.colors[i], M.TIER_COLORS[b.tier]));
    assert.equal(new Set(m.hist.colors).size, 4);
  });
  it('a tier the API does not return is a dash', () => {
    const m = M.healthModel({ tiers: [{ tier: 'Critical', workspaces: 1, share: 1 }], histogram: [] });
    assert.equal(m.cards[0].count, '—');
  });
  it('workspace rows keep the API order and wording', () => {
    const page = fx('workspaces').data;
    const rows = M.workspaceRows(page);
    assert.deepEqual(rows.map(r => r.name), page.items.map(w => w.workspace_name));
    assert.equal(rows[0].score, page.items[0].health_score.toFixed(1));
    assert.equal(rows[0].status, 'Critical');
    assert.equal(rows[0].statusClass, 'critical');
    assert.equal(rows[0].ai, 'None');
  });
  it('AI usage shows the automation share when there is one; scores are clamped for the bar', () => {
    const rows = M.workspaceRows({ items: [
      { workspace_name: 'a', plan_tier: 'Free', health_score: 140, seat_count: 1, dau_over_seats_ratio: 0.5, used_ai_feature_30d: true, pct_ai_calls_automated: 0.25, risk_tier: 'Champion' },
      { workspace_name: 'b', plan_tier: 'Free', health_score: -3, seat_count: 1, dau_over_seats_ratio: null, used_ai_feature_30d: true, pct_ai_calls_automated: null, risk_tier: 'Mystery' },
    ] });
    assert.equal(rows[0].ai, 'Active · 25% automated');
    assert.equal(rows[0].scorePct, 100);
    assert.equal(rows[1].ai, 'Active');
    assert.equal(rows[1].scorePct, 0);
    assert.equal(rows[1].dauSeats, '—');
    assert.equal(rows[1].statusClass, 'unknown');
  });
});

// ---------------------------------------------------------------- helpers
describe('small helpers', () => {
  it('titleCase and p-values', () => {
    assert.equal(M.titleCase('ai_voice_agent'), 'Ai Voice Agent');
    assert.equal(M.formatP(0.0194), '0.019');
    assert.equal(M.formatP(0.00001), '<0.001');
    assert.equal(M.formatP(null), '—');
  });
});

// ---------------------------------------------------------------- the page itself
describe('the page', () => {
  const script = html.slice(html.indexOf('<script>\n') + 9, html.lastIndexOf('</script>'));
  const markup = html.slice(html.indexOf('<body>'), html.indexOf('<script>\n'));
  const code = script.replace(/\/\/.*$/gm, '');

  it('has no mock analytics left: no known literals, no data arrays, no random values', () => {
    for (const leftover of ['42,891', '$2.4M', '2.4M', 'Acme Corp', 'TechFlow', 'Onboarding V2', '500000', '22,000+', '50M+',
                            'PROJECT_CONTEXT', 'getFallbackResponse', 'claude.use', 'Key Insight', 'yourusername', 'Math.random']) {
      assert.ok(!html.includes(leftover), 'leftover: ' + leftover);
    }
    // a literal array of 4+ numbers is a hard-coded series
    assert.ok(!/\[\s*-?\d+(\.\d+)?(\s*,\s*-?\d+(\.\d+)?){3,}\s*\]/.test(code.replace(/\[\s*(\d+\s*,\s*){1,2}\d+\s*\]/g, '')), 'numeric data array in the script');
  });

  it('loads Chart.js from its own origin: one relative script whose integrity is the vendored file hash, no CDN', async () => {
    const { createHash } = await import('node:crypto');
    const tags = [...html.matchAll(/<script\b([^>]*)\bsrc="([^"]+)"([^>]*)>/g)];
    assert.deepEqual(tags.map(t => t[2]), ['vendor/chart.umd.js']);
    const vendored = readFileSync(resolve(root, 'vendor', 'chart.umd.js'));
    const sri = 'sha384-' + createHash('sha384').update(vendored).digest('base64');
    assert.ok((tags[0][1] + tags[0][3]).includes(`integrity="${sri}"`), 'integrity attribute does not match vendor/chart.umd.js');
    assert.ok(!/cdnjs|cloudflare|jsdelivr|unpkg/i.test(html), 'the page names a CDN');
    assert.ok(!/<script[^>]*\bsrc="(https?:)?\/\//.test(html), 'a remote script');
    assert.match(vendored.subarray(0, 120).toString(), /Chart\.js v4\.4\.1/);
  });

  it('puts no API-sourced text through innerHTML or any HTML-parsing sink', () => {
    for (const sink of [/innerHTML/, /outerHTML/, /insertAdjacentHTML/, /document\.write/, /\beval\s*\(/, /new Function/, /setAttribute\(\s*['"]style/, /srcdoc/]) {
      assert.ok(!sink.test(code), String(sink));
    }
  });

  it('has no style attributes or inline event handlers in the markup (the CSP allows neither)', () => {
    assert.ok(!/\sstyle\s*=/.test(markup));
    assert.ok(!/\son[a-z]+\s*=/.test(markup));
  });

  it('has a retention panel without the unbuilt week-1-features chart', () => {
    assert.ok(!html.includes('chart-feat-retention'));
    assert.ok(!html.includes('Retention by Features Adopted'));
  });

  it('the Analyst tab is a real chat: a form, a text box, a send button and a conversation log', () => {
    const tab = markup.slice(markup.indexOf('id="tab-ai"'), markup.indexOf('</div><!-- /main -->'));
    assert.ok(!/coming in Phase 6/.test(tab));
    assert.match(tab, /<form[^>]*id="ai-form"/);
    assert.match(tab, /<textarea[^>]*id="ai-input"/);
    assert.match(tab, /<button[^>]*type="submit"[^>]*id="ai-send"/);
    assert.match(tab, /role="log"/);
    assert.match(tab, /grounded|checked against/i);
    assert.match(tab, /Each answer lists its sources/);
    assert.match(tab, /may be unavailable/);
    assert.ok(!/\sstyle\s*=|\son[a-z]+\s*=/.test(tab));
  });

  it('every element id the script uses exists in the markup', () => {
    const ids = new Set([...markup.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
    const used = new Set();
    for (const m of code.matchAll(/byId\('([^']+)'\)/g)) used.add(m[1]);
    for (const m of code.matchAll(/drawChart\('([^']+)'/g)) used.add(m[1]);
    for (const m of code.matchAll(/containers:\s*\[([^\]]+)\]/g)) for (const s of m[1].matchAll(/'([^']+)'/g)) used.add(s[1]);
    for (const m of code.matchAll(/'(voc-)'\s*\+\s*key/g)) used.add('voc-tickets'), used.add('voc-ai_resolution'), used.add('voc-csat');
    for (const id of used) assert.ok(ids.has(id), 'script uses #' + id + ' but the page has no such element');
    assert.ok(used.size > 30);
  });

  it('only calls endpoints the data layer defines, and only through the client', () => {
    const called = [...code.matchAll(/needApi\(\)\.(\w+)\(/g)].map(m => m[1]);
    assert.ok(called.length >= 12);
    for (const name of called) assert.ok(ENDPOINTS[name] || name === 'chat', 'unknown endpoint ' + name);   // chat: the analyst's POST
    assert.ok(!/(^|[^.\w])fetch\(/.test(code.replace(/window\.fetch\.bind/, '')), 'panels must not call fetch directly');
  });

  it('every KPI and tier card the markup declares exists in the API fixtures', () => {
    const kpis = fx('overview').data.kpis;
    for (const m of markup.matchAll(/data-kpi="([^"]+)"/g)) assert.ok(kpis[m[1]], 'no KPI ' + m[1]);
    const tiers = new Set(fx('customer_health').data.tiers.map(t => t.tier));
    for (const m of markup.matchAll(/data-tier="([^"]+)"/g)) assert.ok(tiers.has(m[1]), 'no tier ' + m[1]);
  });

  it('loads the overview at start and every other tab only when opened', () => {
    assert.match(code, /startTab\('overview'\);/);
    assert.match(code, /tab\.addEventListener\('click'[\s\S]*startTab\(tab\.dataset\.tab\)/);
    const groups = code.match(/const TAB_PANELS = \{([\s\S]*?)\};/)[1];
    for (const tab of ['overview', 'retention', 'funnel', 'experiments', 'health', 'ai']) assert.ok(groups.includes(tab + ':'), tab);
  });

  it('the real DAU, revenue and NPS windows come from /api/meta, not literals', () => {
    assert.match(code, /trailingWindow\(meta\.data_end, YEAR_DAYS, meta\.data_start\)/);
    assert.ok(!/\d{4}-\d{2}-\d{2}/.test(code), 'a hard-coded date in the script');
  });
});
