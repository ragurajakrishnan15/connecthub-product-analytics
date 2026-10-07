"""The dashboard in a real browser (Playwright + Chromium) against the real API and warehouse.

Every displayed value is compared with what the API returns for the same request, read
independently of the page and formatted here without the page's code. The page runs under its
real Content-Security-Policy; any uncaught error, CSP violation or request to an unexpected host
fails the test (tests/browser/browserlib.py). Skipped when PostgreSQL, Playwright or Chromium is
not available: see requirements/browser.txt.
"""
import json
from datetime import date, timedelta

import pytest

pytest.importorskip('playwright.sync_api')

from browserlib import INDEX, approx_list, fmt_int, fmt_pct, round_half_up, scaled  # noqa: E402
from playwright.sync_api import expect  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.browser]
expect.set_options(timeout=20_000)

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
OVERVIEW = ['panel-kpis', 'panel-dau', 'panel-adoption', 'panel-revenue', 'panel-agent', 'voc-nps',
            'voc-tickets', 'voc-ai_resolution', 'voc-csat', 'panel-nps-trend', 'panel-nps-plan']
VERDICT_CLASS = {'SHIP': 'ship', 'CONTINUE': 'continue', 'HOLD': 'hold', 'REVERT': 'revert'}


def day_label(iso):
    d = date.fromisoformat(iso[:10])
    return f'{MONTHS[d.month - 1]} {d.day}, {d.year}'


def ready(d, *ids):
    for panel in ids:
        expect(d.page.locator(f'#{panel}')).to_have_attribute('data-state', 'ready')


def text(d, selector):
    return d.page.locator(selector).first.text_content().strip()


def year_window(meta):
    """The 12-month window the page asks /api/support and /api/nps for (365 days ending at the data end)."""
    end = date.fromisoformat(meta['data_end'])
    start = max(date.fromisoformat(meta['data_start']), end - timedelta(days=364))
    return f'start={start}&end={end}&granularity=month'


def load_overview(d):
    d.open()
    ready(d, *OVERVIEW)
    return d


# ================================================================ loading and resources
def test_every_overview_panel_loads_with_no_errors(dash):
    load_overview(dash)
    states = dash.page.eval_on_selector_all(
        '[data-state]', 'els => els.map(e => [e.id, e.getAttribute("data-state")])')
    assert sorted(i for i, s in states if s == 'ready') == sorted(OVERVIEW)
    assert not [i for i, s in states if s not in ('ready',)]          # nothing else started, nothing failed


def test_chart_js_is_the_vendored_4_4_1_served_from_this_origin(dash):
    response = dash.open()
    ready(dash, 'panel-kpis')
    assert dash.page.evaluate('Chart.version') == '4.4.1'
    scripts = dash.page.eval_on_selector_all('script[src]', 'els => els.map(e => [e.getAttribute("src"), e.integrity])')
    assert [s for s, _ in scripts] == ['vendor/chart.umd.js'] and scripts[0][1].startswith('sha384-')
    asset = dash.page.request.get(dash.app.origin + '/vendor/chart.umd.js')
    assert asset.status == 200 and asset.headers['content-type'].startswith('text/javascript')
    assert asset.headers['x-content-type-options'] == 'nosniff'
    csp = response.headers['content-security-policy']
    assert "script-src 'sha256-" in csp and "'self'" in csp
    for banned in ('unsafe-inline', 'unsafe-eval', 'unsafe-hashes', 'cdnjs', 'cloudflare'):
        assert banned not in csp


def test_the_page_asks_only_its_own_origin_and_google_fonts(dash):
    load_overview(dash)
    urls = [u for _, u, _ in dash.watch.requests]
    assert all(u.startswith((dash.app.origin, 'https://fonts.googleapis.com/', 'data:')) for u in urls), urls
    assert not [u for u in urls if any(h in u for h in ('cdnjs', 'jsdelivr', 'unpkg', 'cloudflare'))]
    assert not [u for u in urls if u.split('?')[0].endswith('.map')]
    assert f'{dash.app.origin}/vendor/chart.umd.js' in urls


def test_the_overview_loads_at_start_and_other_tabs_only_when_opened(dash):
    load_overview(dash)
    paths = set(dash.watch.api_paths())
    assert {'api/meta', 'api/overview', 'api/engagement', 'api/feature-adoption', 'api/revenue',
            'api/support', 'api/nps'} <= paths
    assert not paths & {'api/cohorts', 'api/activation', 'api/experiments', 'api/customer-health',
                        'api/customer-health/workspaces'}
    dash.tab('retention')
    ready(dash, 'panel-cohorts')
    assert dash.watch.api_paths().count('api/cohorts') == 1


def test_the_top_bar_shows_the_data_window_and_dataset_not_the_clock(dash):
    load_overview(dash)
    meta, _ = dash.app.get('/api/meta')
    assert text(dash, '#topbar-pill').startswith('Data as of ')
    assert text(dash, '#topbar-date') == (f"{day_label(meta['data_start'])} — {day_label(meta['data_end'])}"
                                          f" · {meta['dataset']['label']} data")
    assert 'Live' not in text(dash, '.topbar-right')


# ================================================================ overview
KPI_FORMAT = {
    'mrr_usd': lambda v: '$' + fmt_int(v), 'dau': fmt_int, 'week4_retention_rate': fmt_pct,
    'ai_feature_adoption_rate': fmt_pct, 'activation_rate_14d': fmt_pct, 'nps': lambda v: f'{v:.1f}',
}


@pytest.mark.parametrize('key', list(KPI_FORMAT))
def test_kpi_value_equals_the_api(dash, key):
    load_overview(dash)
    kpis = dash.app.get('/api/overview')[0]['kpis']
    expected = '—' if kpis[key]['value'] is None else KPI_FORMAT[key](kpis[key]['value'])
    assert text(dash, f'#panel-kpis [data-kpi="{key}"] .kpi-value') == expected


def test_kpi_tooltip_carries_the_api_definition(dash):
    load_overview(dash)
    kpis = dash.app.get('/api/overview')[0]['kpis']
    tip = dash.page.locator('#panel-kpis [data-kpi="mrr_usd"]').get_attribute('title')
    assert kpis['mrr_usd']['definition'] in tip and 'Current:' in tip and 'Previous:' in tip


def test_dau_chart_equals_the_api(dash):
    load_overview(dash)
    points = dash.app.get('/api/engagement?granularity=month')[0]['points']
    chart = dash.chart('chart-dau')
    assert chart['datasets'][0]['data'] == approx_list([p['avg_dau'] for p in points])
    assert len(chart['labels']) == len(points)


def test_adoption_chart_plots_observed_rate_for_every_curve(dash):
    load_overview(dash)
    curves = dash.app.get('/api/feature-adoption')[0]['curves']
    chart = dash.chart('chart-adoption')
    assert len(chart['datasets']) == len(curves) > 1
    for dataset, curve in zip(chart['datasets'], curves):
        assert dataset['data'] == approx_list([scaled(p['observed_rate']) for p in curve['points']])


def test_revenue_chart_stacks_to_the_api_mrr_with_the_top_plan_first(dash):
    load_overview(dash)
    meta = dash.app.get('/api/meta')[0]
    series = dash.app.get('/api/revenue?group_by=plan_tier')[0]['series']
    chart = dash.chart('chart-revenue')
    assert [d['label'] for d in chart['datasets']] == [p['name'] for p in meta['plan_tiers']][::-1]
    assert sum(d['data'][-1] for d in chart['datasets']) * 1000 == pytest.approx(series[-1]['mrr_usd'], abs=0.5)
    assert len(chart['labels']) == len(series)


def test_ai_resolution_chart_equals_the_api(dash):
    load_overview(dash)
    meta = dash.app.get('/api/meta')[0]
    series = dash.app.get('/api/support?' + year_window(meta))[0]['series']
    assert dash.chart('chart-agent')['datasets'][0]['data'] == approx_list(
        [scaled(s['ai_resolution_rate']) for s in series])


def test_voice_of_customer_cards_equal_the_api(dash):
    load_overview(dash)
    meta = dash.app.get('/api/meta')[0]
    support = dash.app.get('/api/support?' + year_window(meta))[0]
    nps = dash.app.get('/api/nps?' + year_window(meta))[0]
    assert text(dash, '#voc-tickets .kpi-value') == fmt_int(support['tickets']['created'])
    assert text(dash, '#voc-ai_resolution .kpi-value') == fmt_pct(support['ai_agent']['ai_resolution_rate'])
    assert text(dash, '#voc-csat .kpi-value') == f"{support['ai_agent']['avg_csat']:.2f}"
    assert text(dash, '#voc-nps .kpi-value') == f"{nps['summary']['nps']:.1f}"


def test_nps_by_plan_chart_equals_the_api(dash):
    load_overview(dash)
    nps = dash.app.get('/api/nps?' + year_window(dash.app.get('/api/meta')[0]))[0]
    chart = dash.chart('chart-nps-plan')
    assert chart['labels'] == [p['plan_tier'] for p in nps['by_plan']]
    assert chart['datasets'][0]['data'] == approx_list([p['nps'] for p in nps['by_plan']])


def test_the_api_caveats_are_shown_under_the_panel(dash):
    load_overview(dash)
    caveats = dash.app.get('/api/feature-adoption')[1]['caveats']
    assert caveats and text(dash, '#panel-adoption .panel-note') == ' '.join(caveats)


# ================================================================ retention and activation
def open_retention(d):
    d.open()
    d.tab('retention')
    ready(d, 'panel-cohorts')
    return d.app.get('/api/cohorts')[0]


def test_heatmap_has_a_row_per_cohort_and_a_dash_per_unobserved_week(dash):
    cohorts = open_retention(dash)
    assert dash.page.locator('#heatmap-container tbody tr').count() == len(cohorts['cohorts'])
    gaps = sum(1 for c in cohorts['cohorts'] for i, _ in enumerate(cohorts['weeks'])
               if c['cells'][i] is None or c['cells'][i]['retention_rate'] is None)
    assert dash.page.locator('#heatmap-container td.cell-muted').count() == gaps > 0
    assert dash.page.locator('#heatmap-container td.cell-muted').first.text_content() == '—'


def test_heatmap_cell_values_and_colors(dash):
    cohorts = open_retention(dash)
    row = next(i for i, c in enumerate(cohorts['cohorts']) if c['cells'][1] and c['cells'][1]['retention_rate'] is not None)
    cell = dash.page.locator('#heatmap-container tbody tr').nth(row).locator('td').nth(2)    # label, W0, W1
    assert cell.text_content() == f"{round_half_up(cohorts['cohorts'][row]['cells'][1]['retention_rate'] * 100)}%"
    assert cell.evaluate('e => e.style.background') != ''                    # colored through the DOM, not markup
    label = dash.page.locator('#heatmap-container tbody tr').nth(row).locator('td').first
    assert 'users in this cohort' in label.get_attribute('title')


def test_the_unbuilt_week_one_features_panel_is_gone(dash):
    open_retention(dash)
    assert dash.page.locator('#chart-feat-retention').count() == 0
    assert 'Retention by Features Adopted' not in dash.page.content()


def test_funnel_counts_and_bar_widths_equal_the_api(dash):
    dash.open()
    dash.tab('funnel')
    ready(dash, 'panel-funnel', 'panel-milestones')
    act = dash.app.get('/api/activation')[0]
    stages = dash.page.locator('#funnel-container .funnel-stage')
    assert stages.count() == len(act['funnel'])
    for i, stage in enumerate(act['funnel']):
        assert stages.nth(i).locator('.funnel-value').text_content() == fmt_int(stage['users'])
    width = stages.nth(1).locator('.funnel-bar').evaluate('e => parseFloat(e.style.width)')
    assert width == pytest.approx(act['funnel'][1]['users'] / act['funnel'][0]['users'] * 100, abs=0.01)
    assert dash.chart('chart-time-activation')['datasets'][0]['data'] == [
        r['median_days'] for r in act['time_to_milestone']]


# ================================================================ experiments
def open_experiments(d):
    d.open()
    d.tab('experiments')
    ready(d, 'panel-exp-list', 'panel-exp', 'panel-exp-curve')
    return d.app.get('/api/experiments')[0]['experiments']


def test_selector_lists_the_real_experiments_and_defaults_to_the_first_ab_test(dash):
    experiments = open_experiments(dash)
    ids = [e['experiment']['experiment_id'] for e in experiments]
    options = dash.page.eval_on_selector_all('#exp-select option', 'els => els.map(e => [e.value, e.textContent])')
    assert [v for v, _ in options] == ids
    assert all(label.endswith('(A/A check)') == (e['experiment']['kind'] == 'aa')
               for (_, label), e in zip(options, experiments))
    assert dash.page.input_value('#exp-select') == next(
        e['experiment']['experiment_id'] for e in experiments if e['experiment']['kind'] == 'ab')


def test_verdict_cards_guardrails_and_curve_equal_the_api(dash):
    open_experiments(dash)
    exp_id = dash.page.input_value('#exp-select')
    detail = dash.app.get(f'/api/experiments/{exp_id}')[0]
    ev = detail['evaluation']
    verdict = dash.page.locator('#exp-body .exp-verdict')
    assert verdict.text_content() == ev['decision_text']
    assert VERDICT_CLASS[ev['decision_code']] in verdict.get_attribute('class').split()
    body = text(dash, '#exp-body')
    assert fmt_pct(ev['primary']['control_rate']) in body and fmt_pct(ev['primary']['treatment_rate']) in body
    assert dash.page.locator('#exp-body .exp-card', has_text='Guardrail:').count() == len(ev['guardrails'])
    assert dash.page.locator('#exp-body .exp-banner').count() == 0                  # not an A/A check
    curve = dash.chart('chart-exp-time')
    assert [d['label'] for d in curve['datasets']] == ['Control', 'Treatment']
    assert curve['datasets'][1]['data'] == approx_list(
        [scaled(p['activation_rate']) for p in detail['activation_curve'][1]['points']])


def aa_and_ab(experiments):
    ids = {e['experiment']['kind']: e['experiment']['experiment_id'] for e in experiments}
    return ids['aa'], ids['ab']


def test_the_a_a_check_is_labelled_and_switching_experiments_works(dash):
    aa, ab = aa_and_ab(open_experiments(dash))
    dash.page.select_option('#exp-select', aa)
    expect(dash.page.locator('#exp-title')).to_contain_text(aa)
    expect(dash.page.locator('#exp-body .exp-banner')).to_contain_text('false positive')
    dash.page.select_option('#exp-select', ab)
    expect(dash.page.locator('#exp-title')).to_contain_text(ab)
    assert dash.page.locator('#exp-body .exp-banner').count() == 0


def test_a_late_response_for_a_superseded_experiment_is_dropped(dash):
    held, cancelled = [], []
    dash.page.route('**/api/experiments/exp_onboarding_v2', lambda route: held.append(route))   # never answers on its own
    dash.page.on('requestfailed', lambda r: cancelled.append((r.url, r.failure)))
    dash.open()
    dash.tab('experiments')
    expect(dash.page.locator('#panel-exp-list')).to_have_attribute('data-state', 'ready')
    expect(dash.page.locator('#panel-exp')).to_have_attribute('data-state', 'loading')          # the default one is pending
    experiments = dash.app.get('/api/experiments')[0]['experiments']
    aa, _ = aa_and_ab(experiments)
    dash.page.select_option('#exp-select', aa)
    expect(dash.page.locator('#exp-title')).to_contain_text(aa)
    assert held, 'the first request was never made'
    # the page cancelled the superseded request itself (the data layer's abort signal) ...
    expect(dash.page.locator('#exp-title')).to_contain_text(aa)
    assert [f for u, f in cancelled if u.endswith('/exp_onboarding_v2') and 'ABORTED' in (f or '')]
    # ... and even if its answer still reached the server and came back, the page ignores it
    held[0].continue_()                                       # the stale answer arrives now
    dash.page.wait_for_timeout(800)
    assert aa in text(dash, '#exp-title') and dash.page.get_attribute('#panel-exp', 'data-state') == 'ready'
    assert dash.page.locator('#exp-body .exp-banner').count() == 1


# ================================================================ customer health
def test_tier_cards_histogram_and_table_equal_the_api(dash):
    dash.open()
    dash.tab('health')
    ready(dash, 'panel-tiers', 'panel-health-dist', 'panel-health-table')
    summary = dash.app.get('/api/customer-health')[0]
    for tier in summary['tiers']:
        assert text(dash, f'#panel-tiers [data-tier="{tier["tier"]}"] .kpi-value') == fmt_int(tier['workspaces'])
    dataset = dash.chart('chart-health-dist')['datasets'][0]
    assert dataset['data'] == [b['workspaces'] for b in summary['histogram']]
    colors = dataset['backgroundColor']
    assert isinstance(colors, list) and len(colors) == len(summary['histogram'])       # one color per bar
    by_tier = {}
    for bin_, color in zip(summary['histogram'], colors):
        assert by_tier.setdefault(bin_['tier'], color) == color                        # a tier has one color
    assert len(set(by_tier.values())) == len(by_tier) > 1                              # tiers differ

    page = dash.app.get('/api/customer-health/workspaces?sort=health_score&order=asc&limit=10')[0]
    rows = dash.page.locator('#health-table-body tr')
    assert rows.count() == len(page['items'])
    scores = []
    for i, item in enumerate(page['items']):
        cells = rows.nth(i).locator('td')
        assert cells.nth(0).text_content() == item['workspace_name']
        assert rows.nth(i).locator('.health-badge').text_content() == item['risk_tier']
        scores.append(float(cells.nth(2).text_content()))
    assert scores == sorted(scores)


# ================================================================ the AI Analyst is a placeholder
def test_the_ai_tab_is_a_placeholder_that_asks_nothing(dash):
    load_overview(dash)
    before = len(dash.watch.api_paths())
    dash.tab('ai')
    expect(dash.page.locator('#tab-ai')).to_contain_text('coming in Phase 6')
    assert dash.page.locator('#tab-ai input, #tab-ai button, #tab-ai textarea, #tab-ai select').count() == 0
    dash.page.wait_for_timeout(300)
    assert len(dash.watch.api_paths()) == before


# ================================================================ loading, empty and error states
COHORTS = '**/api/cohorts*'
PROBLEM = {'type': 'urn:connecthub:problem:data-not-ready', 'title': 'Warehouse data not ready', 'status': 503,
           'detail': 'a required warehouse table does not exist', 'instance': '/api/cohorts', 'request_id': 'rid-42'}


def stub(page, pattern, status=200, body=None, content_type='application/json', headers=None):
    page.route(pattern, lambda route: route.fulfill(status=status, body=json.dumps(body), headers=headers or {},
                                                    content_type=content_type))


def test_a_panel_shows_loading_while_its_request_is_pending(dash):
    held = []
    dash.page.route(COHORTS, lambda route: held.append(route))
    dash.open()
    dash.tab('retention')
    expect(dash.page.locator('#panel-cohorts')).to_have_attribute('data-state', 'loading')
    assert text(dash, '#panel-cohorts .panel-state') == 'Loading…'
    assert dash.page.locator('#heatmap-container').is_hidden()
    held[0].continue_()
    ready(dash, 'panel-cohorts')


def test_an_error_shows_the_title_request_id_and_retry_and_retry_recovers(dash):
    stub(dash.page, COHORTS, 503, PROBLEM, 'application/problem+json', {'Retry-After': '5'})
    dash.open()
    dash.tab('retention')
    panel = dash.page.locator('#panel-cohorts')
    expect(panel).to_have_attribute('data-state', 'error')
    expect(panel.locator('.state-title')).to_have_text('Warehouse not ready')
    assert 'request rid-42' in text(dash, '#panel-cohorts .panel-state')
    assert dash.page.locator('#heatmap-container').is_hidden()
    dash.page.unroute(COHORTS)
    panel.get_by_role('button', name='Retry').click()
    ready(dash, 'panel-cohorts')
    assert dash.page.locator('#heatmap-container tbody tr').count() == len(dash.app.get('/api/cohorts')[0]['cohorts'])


def test_one_failing_panel_does_not_blank_the_others(dash):
    stub(dash.page, '**/api/revenue*', 503, {**PROBLEM, 'instance': '/api/revenue'}, 'application/problem+json')
    dash.open()
    expect(dash.page.locator('#panel-revenue')).to_have_attribute('data-state', 'error')
    ready(dash, *[p for p in OVERVIEW if p != 'panel-revenue'])


def test_a_successful_but_empty_response_shows_the_empty_state(dash):
    stub(dash.page, COHORTS, 200, {'data': {'cohorts': [], 'weeks': [], 'as_of': '2025-12-31'}, 'meta': {'caveats': []}})
    dash.open()
    dash.tab('retention')
    expect(dash.page.locator('#panel-cohorts')).to_have_attribute('data-state', 'empty')
    assert text(dash, '#panel-cohorts .panel-state') == 'No data for this period.'


def test_an_unreachable_api_and_a_malformed_response_are_explained(dash):
    dash.page.route(COHORTS, lambda route: route.abort())
    stub(dash.page, '**/api/activation*', 200, {'unexpected': True})
    dash.open()
    dash.tab('retention')
    expect(dash.page.locator('#panel-cohorts .state-title')).to_have_text('Cannot reach the API')
    dash.tab('funnel')
    expect(dash.page.locator('#panel-funnel .state-title')).to_have_text('Unexpected response')


def test_markup_in_api_strings_is_shown_as_text_and_never_run(dash):
    evil = '<img src=x onerror="window.__pwned=1"><b>x</b>'
    item = {'workspace_name': evil, 'plan_tier': evil, 'health_score': 5, 'seat_count': 3, 'dau_over_seats_ratio': 0.1,
            'used_ai_feature_30d': False, 'pct_ai_calls_automated': None, 'risk_tier': 'Critical'}
    stub(dash.page, '**/api/customer-health/workspaces*', 200,
         {'data': {'snapshot_date': '2025-12-31', 'total': 1, 'limit': 10, 'items': [item]}, 'meta': {'caveats': [evil]}})
    dash.open()
    dash.tab('health')
    ready(dash, 'panel-health-table')
    assert text(dash, '#health-table-body tr td') == evil
    assert dash.page.locator('#health-table-body img, #panel-health-table .panel-note b').count() == 0
    assert text(dash, '#panel-health-table .panel-note') == evil
    dash.page.wait_for_timeout(300)
    assert dash.page.evaluate('window.__pwned') is None
    assert not [u for _, u, _ in dash.watch.requests if u.endswith('/x')]            # the <img> never loaded


# ================================================================ opened as a file
def test_opened_as_a_file_every_panel_explains_it_must_be_served_by_the_api(dash):
    dash.page.goto(INDEX.as_uri())
    expect(dash.page.locator('#topbar-pill')).to_have_text('API unavailable')
    expect(dash.page.locator('#panel-dau .state-title')).to_have_text('Cannot load data')
    assert 'must be opened through the API' in text(dash, '#panel-dau .panel-state')
    states = dash.page.eval_on_selector_all('[data-state]', 'els => els.map(e => e.getAttribute("data-state"))')
    assert states == ['error'] * len(OVERVIEW)
    assert not dash.watch.api_paths()                                  # it asked nobody


# ================================================================ the browser enforces the policy
def test_the_content_security_policy_is_enforced_in_the_browser(dash):
    dash.watch.allow_csp_reports = True                                # these probes are meant to be refused
    load_overview(dash)
    refused = dash.page.evaluate("""() => new Promise(resolve => {
        const s = document.createElement('script'); s.textContent = 'window.__injected = 1';
        document.head.appendChild(s);
        const b = document.createElement('button'); b.setAttribute('onclick', 'window.__clicked = 1');
        document.body.appendChild(b); b.click();
        const p = document.createElement('p'); p.setAttribute('style', 'display:none'); document.body.appendChild(p);
        setTimeout(() => resolve({ inlineScript: window.__injected === undefined, inlineHandler: window.__clicked === undefined,
          styleAttribute: getComputedStyle(p).display !== 'none' }), 100);
    })""")
    # (eval is not probed here: Playwright runs page.evaluate through DevTools, which Chromium exempts from the
    # eval restriction. 'unsafe-eval' being absent is asserted on the header in the vendored-script test above.)
    assert refused == {'inlineScript': True, 'inlineHandler': True, 'styleAttribute': True}
    assert len(dash.watch.csp_reports()) >= 3


# ================================================================ the API-key dialog (API_AUTH_MODE=api_key)
def key_dialog(d):
    return d.page.locator('dialog.key-dialog')


def enter_key(d, key):
    dialog = key_dialog(d)
    expect(dialog).to_be_visible()
    dialog.locator('input').fill(key)
    dialog.get_by_role('button', name='Use key').click()


def test_the_page_loads_without_a_key_and_asks_for_one_once(dash_with_key):
    d = dash_with_key
    assert d.open().status == 200                                      # the page itself is public
    expect(key_dialog(d)).to_have_count(1)                             # eleven failing panels, one dialog
    expect(key_dialog(d)).to_be_visible()
    assert 'needs a key' in key_dialog(d).locator('p').first.text_content()
    assert key_dialog(d).locator('input').get_attribute('type') == 'password'


def test_a_wrong_key_is_refused_forgotten_and_not_retried_in_a_loop(dash_with_key):
    d = dash_with_key
    d.open()
    enter_key(d, 'wrong-key-' + 'x' * 20)
    expect(d.page.locator('#panel-kpis .state-title')).to_have_text('API key required')
    d.page.wait_for_timeout(600)
    expect(key_dialog(d)).to_have_count(0)                             # no second prompt by itself
    assert d.page.evaluate("sessionStorage.getItem('connecthub.apiKey')") is None
    assert [u for _, u, k in d.watch.requests if '/api/' in u and k == 'wrong-key-' + 'x' * 20]


def test_the_right_key_loads_the_data_and_stays_in_session_storage_only(dash_with_key):
    d = dash_with_key
    key = d.app.key
    d.open()
    enter_key(d, 'wrong-key-' + 'x' * 20)
    expect(d.page.locator('#panel-kpis .state-title')).to_have_text('API key required')
    d.page.locator('#panel-kpis').get_by_role('button', name='Retry').click()
    enter_key(d, key)
    ready(d, 'panel-kpis')
    assert text(d, '#panel-kpis [data-kpi="dau"] .kpi-value') != '—'
    assert d.page.evaluate("sessionStorage.getItem('connecthub.apiKey')") == key
    assert d.page.evaluate("localStorage.getItem('connecthub.apiKey')") is None
    seen = len(d.watch.requests)
    d.page.reload()                                                    # the tab remembers it: no dialog
    ready(d, *OVERVIEW)
    expect(key_dialog(d)).to_have_count(0)
    assert key not in d.page.content()
    assert not [u for _, u, _ in d.watch.requests if key in u]         # never in a URL
    after = [(u, k) for _, u, k in d.watch.requests[seen:] if '/api/' in u]
    assert after and all(k == key for _, k in after)                   # every API call sent it, as a header
    assert {u.split('/api/')[1].split('?')[0] for u, _ in after} >= {
        'meta', 'overview', 'engagement', 'feature-adoption', 'revenue', 'support', 'nps'}


def test_a_stored_key_that_is_rejected_is_reported_as_such(dash_with_key):
    d = dash_with_key
    d.page.goto(d.app.origin + '/api/health')                          # same origin, so sessionStorage is shared
    d.page.evaluate("sessionStorage.setItem('connecthub.apiKey', 'stale-key-' + 'y'.repeat(20))")
    d.open()
    expect(key_dialog(d)).to_be_visible()
    assert 'rejected' in key_dialog(d).locator('p').first.text_content()


def test_cancel_and_escape_close_the_dialog_and_leave_a_clear_error(dash_with_key):
    d = dash_with_key
    d.open()
    key_dialog(d).get_by_role('button', name='Cancel').click()
    expect(key_dialog(d)).to_have_count(0)
    expect(d.page.locator('#panel-dau .state-title')).to_have_text('API key required')
    d.page.reload()
    expect(key_dialog(d)).to_be_visible()
    d.page.keyboard.press('Escape')
    expect(key_dialog(d)).to_have_count(0)
