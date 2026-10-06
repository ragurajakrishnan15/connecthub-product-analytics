"""GET /api/meta."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.engine import Connection

from api.db import get_conn
from api.params import allow_query_params
from api.schemas.common import Envelope, cacheable_responses
from api.schemas.meta import MetaData
from api.security import require_api_key
from api.services import meta as service

router = APIRouter(prefix='/api', tags=['meta'],
                   dependencies=[Depends(require_api_key)])


@router.get('/meta', response_model=Envelope[MetaData],
            dependencies=[Depends(allow_query_params())],
            responses=cacheable_responses(400, 401, 503, 504),
            summary='Data window, freshness and dimension metadata')
def meta(request: Request, conn: Connection = Depends(get_conn)):
    """Everything a client needs to configure itself without literals: the data
    window, the latest health snapshot, the last fully validated pipeline run,
    plan tiers with seat prices, features, registered experiments and health
    tiers. 503 `data-not-ready` until the pipeline has built the warehouse."""
    return service.build(conn, request.app.state.settings)
