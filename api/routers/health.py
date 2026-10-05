"""GET /api/health (liveness) and GET /api/health/ready (readiness)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.params import allow_query_params
from api.schemas.common import problem_responses
from api.schemas.health import Liveness, Readiness
from api.services import health as service

router = APIRouter(prefix='/api/health', tags=['health'],
                   dependencies=[Depends(allow_query_params())])
NO_STORE = {'Cache-Control': 'no-store'}


@router.get('', response_model=Liveness, responses=problem_responses(400),
            summary='Liveness: the process is up')
def health(request: Request):
    """No database access. Used by the container healthcheck."""
    body = service.liveness(request.app.state.started)
    return JSONResponse(body.model_dump(mode='json'), headers=NO_STORE)


@router.get('/ready', response_model=Readiness,
            responses={503: {'model': Readiness, 'description': 'Not ready'},
                       **problem_responses(400)},
            summary='Readiness: the API can serve correct data')
def ready(request: Request):
    """Checks that the database answers, every relation the API reads exists and
    is readable by the API role, and a fully validated pipeline run exists.
    200 when ready, 503 otherwise; never cached."""
    body = service.readiness(request.app.state.engine, request.app.state.settings)
    status = 200 if body.status == 'ready' else 503
    return JSONResponse(body.model_dump(mode='json'), status_code=status, headers=NO_STORE)
