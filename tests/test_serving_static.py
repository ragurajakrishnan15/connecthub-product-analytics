"""Static checks on the dbt project (no database): serving models are tagged,
contracted and tested, and no model reads the wall clock."""
import os
import re

import yaml

from pipeline import config

MODELS = os.path.join(config.dbt_dir(), 'models')
SERVING_DIR = os.path.join(MODELS, 'gold', 'serving')
CLOCK = re.compile(r'\b(current_date|current_timestamp|now\s*\(|localtimestamp|clock_timestamp)',
                   re.IGNORECASE)


def _sql_files(root):
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.endswith('.sql'):
                yield os.path.join(dirpath, f)


def test_no_model_uses_the_wall_clock():
    """Data is a synthetic 2025; every 'latest' or 'complete' must come from the data."""
    offenders = [p for p in _sql_files(MODELS) if CLOCK.search(open(p).read())]
    assert offenders == []


def test_every_serving_model_has_an_enforced_contract_and_grain_test():
    with open(os.path.join(SERVING_DIR, 'serving.yml')) as f:
        spec = {m['name']: m for m in yaml.safe_load(f)['models']}
    sql = {os.path.splitext(f)[0] for f in os.listdir(SERVING_DIR) if f.endswith('.sql')}
    assert sql == set(spec), 'every serving model needs a serving.yml entry and vice versa'
    for name, model in spec.items():
        assert model['config']['contract']['enforced'] is True, name
        assert all('data_type' in c for c in model['columns']), name
        assert all(any(k.get('type') == 'not_null' for k in c.get('constraints', []))
                   for c in model['columns']), name
        assert any('unique_combination_of_columns' in t for t in model.get('tests', [])), name


def test_serving_models_are_tagged():
    with open(os.path.join(config.dbt_dir(), 'dbt_project.yml')) as f:
        project = yaml.safe_load(f)
    gold = project['models']['connecthub_analytics']['gold']
    assert gold['serving']['+tags'] == ['serving']
