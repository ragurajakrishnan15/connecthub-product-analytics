"""Command-line entry points: exit codes and output."""
import json

import pytest

from analytics import cohort_engine, health_scoring
from experimentation import assignment, evaluate
from pipeline.__main__ import main as pipeline_main


def run(main, argv, capsys):
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def usage_error(main, argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


class TestCohortCli:
    def test_parquet_summary(self, sample_data, capsys):
        code, out, _ = run(cohort_engine.main, ['--date', '2025-12-31', '--source', 'parquet',
                                                '--data-dir', str(sample_data)], capsys)
        assert code == 0
        assert out.startswith('Retention as of 2025-12-31:') and 'week 4' in out

    def test_json_and_csv_output(self, sample_data, tmp_path, capsys):
        csv = tmp_path / 'cells.csv'
        code, out, _ = run(cohort_engine.main, ['--date', '2025-06-30', '--source', 'parquet',
                                                '--data-dir', str(sample_data), '--json',
                                                '--output', str(csv)], capsys)
        summary = json.loads(out)
        assert code == 0 and summary['as_of'] == '2025-06-30' and csv.exists()
        assert all(0 <= v <= 100 for v in summary['pooled_retention_pct'].values())

    def test_no_data_before_first_cohort_exits_1(self, sample_data, capsys):
        code, _, err = run(cohort_engine.main, ['--date', '2024-01-01', '--source', 'parquet',
                                                '--data-dir', str(sample_data)], capsys)
        assert code == 1 and 'no complete retention cohorts' in err

    def test_missing_files_exit_1(self, tmp_path, capsys):
        code, _, err = run(cohort_engine.main, ['--date', '2025-12-31', '--source', 'parquet',
                                                '--data-dir', str(tmp_path)], capsys)
        assert code == 1 and err.startswith('error:')

    def test_usage_errors_exit_2(self):
        assert usage_error(cohort_engine.main, []) == 2
        assert usage_error(cohort_engine.main, ['--date', 'yesterday']) == 2


class TestEvaluateCli:
    def test_parquet_evaluation(self, sample_data, capsys):
        code, out, _ = run(evaluate.main, ['--experiment-id', 'exp_onboarding_v2',
                                           '--source', 'parquet', '--data-dir', str(sample_data)],
                           capsys)
        assert code == 0 and out.startswith('exp_onboarding_v2: ')

    def test_json_output(self, sample_data, capsys):
        code, out, _ = run(evaluate.main, ['--experiment-id', 'exp_onboarding_v2', '--json',
                                           '--source', 'parquet', '--data-dir', str(sample_data)],
                           capsys)
        assert code == 0
        assert json.loads(out)['experiment_id'] == 'exp_onboarding_v2'

    def test_unknown_experiment_exits_1(self, capsys):
        code, _, err = run(evaluate.main, ['--experiment-id', 'nope', '--source', 'parquet'],
                           capsys)
        assert code == 1 and 'Unknown experiment' in err

    def test_experiment_without_assignments_exits_1(self, sample_data, capsys):
        # conftest writes assignments for exp_onboarding_v2 only
        code, _, err = run(evaluate.main, ['--experiment-id', 'exp_ai_summary_v1',
                                           '--source', 'parquet', '--data-dir', str(sample_data)],
                           capsys)
        assert code == 1 and 'no evaluable data' in err

    def test_needs_an_experiment(self):
        assert usage_error(evaluate.main, ['--source', 'parquet']) == 2
        assert usage_error(evaluate.main, ['--all', '--experiment-id', 'x']) == 2


class TestHealthCli:
    def test_parquet_scores(self, sample_data, tmp_path, capsys):
        csv = tmp_path / 'scores.csv'
        code, out, _ = run(health_scoring.main, ['--source', 'parquet', '--data-dir',
                                                 str(sample_data), '--json', '--output', str(csv)],
                           capsys)
        summary = json.loads(out)
        assert code == 0 and csv.exists()
        assert sum(summary['tiers'].values()) == summary['workspaces']

    def test_as_of_needs_parquet(self):
        assert usage_error(health_scoring.main, ['--as-of', '2025-06-30']) == 2


class TestAssignmentCli:
    def test_assigns_all_experiments(self, sample_data, tmp_path, capsys):
        for name in ('users', 'events'):
            (tmp_path / f'{name}.parquet').write_bytes(
                (sample_data / f'{name}.parquet').read_bytes())
        code, out, _ = run(assignment.main, ['--data-dir', str(tmp_path)], capsys)
        assert code == 0
        assert 'exp_onboarding_v2:' in out and 'exp_ai_summary_v1:' in out
        assert (tmp_path / 'experiment_assignments.parquet').exists()

    def test_missing_data_exits_1(self, tmp_path, capsys):
        code, _, err = run(assignment.main, ['--data-dir', str(tmp_path)], capsys)
        assert code == 1 and err.startswith('error:')


class TestPipelineCli:
    def test_rejects_unknown_step(self):
        assert usage_error(pipeline_main, ['step', 'nope']) == 2

    def test_rejects_conflicting_load_modes(self):
        assert usage_error(pipeline_main, ['run', '--through', '2025-12-30',
                                           '--start-date', '2025-12-31']) == 2
