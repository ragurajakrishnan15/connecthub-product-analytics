"""
Request context shared by the data endpoints: the loaded data window and the
data version, and the response envelope built from them.

"Latest" and every default date window derive from data_end, the last event
date in the warehouse (PHASE_4_PLAN.md §1 principle 6).
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone

from api.errors import APIError
from api.repositories import meta as meta_repo
from api.repositories import system
from api.schemas.common import DateRange, Envelope, ResponseMeta

DEFINITIONS = 'docs/metric-definitions.md'


@dataclass(frozen=True)
class WarehouseContext:
    data_start: date
    data_end: date
    data_version: str | None


def load(conn):
    data_start, data_end = meta_repo.data_window(conn)
    if data_end is None:
        raise APIError('data-not-ready', 'the warehouse has no data loaded; '
                                         'run the pipeline (python -m pipeline run)')
    run = system.last_successful_run(conn)
    return WarehouseContext(data_start, data_end, run['run_id'] if run else None)


def envelope(model, data, ctx, sources, *, as_of=None, window=None, filters=None,
             caveats=(), anchor=None):
    """Wrap `data` in the standard envelope. `anchor` names the definitions section."""
    caveats = list(caveats)
    if window is not None and window.clamped:
        caveats.append(f'The requested range was clamped to the available data: '
                       f'{window.start}..{window.end}.')
    meta = ResponseMeta(
        as_of=as_of or ctx.data_end, data_start=ctx.data_start, data_end=ctx.data_end,
        effective_range=DateRange(start=window.start, end=window.end) if window else None,
        filters={k: (None if v is None else str(v)) for k, v in (filters or {}).items()},
        sources=list(sources), data_version=ctx.data_version,
        generated_at=datetime.now(timezone.utc),
        definitions=f'{DEFINITIONS}#{anchor}' if anchor else DEFINITIONS,
        caveats=caveats,
    )
    return Envelope[model](data=data, meta=meta)
