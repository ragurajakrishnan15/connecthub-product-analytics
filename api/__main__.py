"""
Run the API with uvicorn:

    python -m api

Host, port, workers and log level come from API_* settings. Invalid or missing
configuration exits with status 2 and a message per problem (no secret values).
"""
import sys

from pydantic import ValidationError

from api.settings import Settings, describe_validation_error


def main():
    try:
        settings = Settings()
    except ValidationError as exc:
        print(f'error: invalid API configuration: {describe_validation_error(exc)}',
              file=sys.stderr)
        return 2
    import uvicorn
    uvicorn.run('api.main:create_app', factory=True, host=settings.api_host,
                port=settings.api_port, workers=settings.api_workers,
                access_log=False, log_config=None, server_header=False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
