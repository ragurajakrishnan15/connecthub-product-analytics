"""Experiment portfolio and readouts (docs/metric-definitions.md#experiments).

Results are the persisted evaluations in analytics.experiment_results, computed
by experimentation/evaluate.py in the pipeline's analytics step and validated
by Great Expectations; nothing is re-evaluated per request. The experiment
list comes from the registry (experimentation/experiments.py).
"""
from itertools import groupby

from api.errors import APIError
from api.repositories import experiments as repo
from api.schemas.experiments import (ArmCurve, Bayesian, CurvePoint, Evaluation, ExperimentDetail,
                                     ExperimentInfo, ExperimentList, ExperimentSummary, Guardrail,
                                     Primary, PrimaryHeadline, Srm, SrmHeadline)
from api.services import context
from experimentation.decision import decision_code, guardrail_failed
from experimentation.experiments import EXPERIMENTS

MULTIPLE_TESTING = ('The decision rule applies no multiple-testing or peeking correction: about '
                    '1 in 20 experiments without a real effect will show a significant lift.')
AA_NOTE = ('{id} is an A/A check (identical arms): a SHIP decision here is a false positive '
           'by construction, expected in about 5% of runs.')
GUARDRAILS = (('avg_session_minutes_14d', 'guardrail_session_duration'),
              ('revenue_60d', 'guardrail_revenue'))


def _info(e):
    return ExperimentInfo(experiment_id=e.experiment_id, kind=e.kind, description=e.description,
                          hypothesis=e.hypothesis, eligibility=e.eligibility, start=e.start,
                          end=e.end, traffic_share=e.traffic_pct)


def _status(e, persisted, ctx):
    if persisted is None:
        return 'not_evaluated'
    return 'completed' if e.end_ts.date() <= ctx.data_end else 'running'


def _code(persisted):
    return persisted['result'].get('decision_code') or decision_code(persisted['decision'])


def _caveats(e, persisted, status):
    notes = [MULTIPLE_TESTING]
    if e.kind == 'aa' and persisted and _code(persisted) == 'SHIP':
        notes.append(AA_NOTE.format(id=e.experiment_id))
    if status == 'running':
        notes.append(f'{e.experiment_id} runs until {e.end}; results are interim.')
    return notes


def list_experiments(conn, decision, status):
    ctx = context.load(conn)
    persisted = repo.results(conn)
    out, caveats = [], [MULTIPLE_TESTING]
    for e in sorted(EXPERIMENTS.values(), key=lambda e: e.experiment_id):
        p = persisted.get(e.experiment_id)
        st = _status(e, p, ctx)
        summary = ExperimentSummary(experiment=_info(e), status=st, decision_code=None,
                                    decision_text=None, primary=None, srm=None, sample=None)
        if p:
            r = p['result']
            primary, srm = r['primary_metric'], r['srm_check']
            summary.decision_code, summary.decision_text = _code(p), p['decision']
            summary.primary = PrimaryHeadline(**{k: primary[k] for k in (
                'control_rate', 'treatment_rate', 'relative_lift', 'p_value', 'significant')})
            summary.srm = SrmHeadline(p_value=srm['p_value'], detected=srm['srm_detected'])
            summary.sample = {'control': r['sample_sizes']['control'],
                              'treatment': r['sample_sizes']['treatment']}
            caveats += [c for c in _caveats(e, p, st)[1:] if c not in caveats]
        if (decision and summary.decision_code != decision) or (status and st != status):
            continue
        out.append(summary)
    return context.envelope(ExperimentList, ExperimentList(experiments=out), ctx, repo.SOURCES[:1],
                            filters={'decision': decision, 'status': status}, caveats=caveats,
                            anchor='experiments')


def _evaluation(p):
    r = p['result']
    srm, primary, bayes = r['srm_check'], r['primary_metric'], r['bayesian_analysis']
    return Evaluation(
        srm=Srm(control_count=srm['control_count'], treatment_count=srm['treatment_count'],
                actual_ratio=srm['actual_ratio'], p_value=srm['p_value'],
                detected=srm['srm_detected']),
        primary=Primary(**{k: primary[k] for k in (
            'control_rate', 'treatment_rate', 'absolute_diff', 'relative_lift', 'ci_lower',
            'ci_upper', 'z_statistic', 'p_value', 'significant')}),
        bayesian=Bayesian(**{k: bayes[k] for k in (
            'prob_treatment_better', 'expected_lift', 'lift_ci_95', 'expected_loss')}),
        guardrails=[Guardrail(metric=metric, failed=guardrail_failed(r[key]), **{
            k: r[key][k] for k in ('control_mean', 'treatment_mean', 'absolute_diff',
                                   'relative_lift', 'p_value', 'significant')})
            for metric, key in GUARDRAILS],
        sample_sizes=r['sample_sizes'], decision_code=_code(p), decision_text=p['decision'])


def detail(conn, experiment_id):
    e = EXPERIMENTS.get(experiment_id)
    if e is None:
        raise APIError('experiment-not-found', f"unknown experiment {experiment_id!r}; known: "
                                               f"{', '.join(sorted(EXPERIMENTS))}")
    ctx = context.load(conn)
    p = repo.result(conn, experiment_id)
    st = _status(e, p, ctx)
    curves = [ArmCurve(variant=variant, points=[
        CurvePoint(day=r['day_since_signup'], activation_rate=r['rate'],
                   activated=r['activated_cumulative'], users_in_window=r['users_in_window'])
        for r in rows]) for variant, rows in groupby(repo.curve(conn, experiment_id),
                                                     key=lambda r: r['variant'])]
    caveats = _caveats(e, p, st)
    if p is None:
        caveats.append(f'{experiment_id} has no persisted evaluation yet; run the pipeline '
                       f'analytics step.')
    data = ExperimentDetail(experiment=_info(e), status=st,
                            evaluation=_evaluation(p) if p else None, activation_curve=curves)
    return context.envelope(ExperimentDetail, data, ctx, repo.SOURCES,
                            filters={'experiment_id': experiment_id}, caveats=caveats,
                            anchor='experiments')
