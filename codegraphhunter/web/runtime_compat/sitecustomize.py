"""Adapt the frozen runner's Responses calls only for a selected APR run.

The frozen implementation sends temperature=0 for every model. Reasoning GPT
models require removing sampling parameters when reasoning is enabled:
https://developers.openai.com/api/docs/guides/latest-model
This module is on PYTHONPATH only in an APR experiment child process.
"""

import functools
import os
import time
import uuid

from apr_response_options import response_parameters
from apr_call_audit import record, stage


def _install():
    model = os.environ.get("CGH_APR_MODEL", "")
    if not model:
        return
    try:
        from openai.resources.responses.responses import Responses
    except ImportError:
        return
    original = Responses.create

    @functools.wraps(original)
    def create(self, *args, **kwargs):
        if not os.environ.get("CGH_APR_LLM_AUDIT"):
            return original(self, *args, **response_parameters(model, kwargs))
        event = {"call_id": uuid.uuid4().hex, "stage": stage(), "requested_model": model}
        record(dict(event, status="started"))
        started = time.monotonic()
        try:
            response = original(self, *args, **response_parameters(model, kwargs))
        except Exception as error:
            record(dict(event, status="failed", error_type=type(error).__name__,
                        http_status=getattr(error, "status_code", None), request_id=getattr(error, "request_id", None),
                        elapsed_s=round(time.monotonic() - started, 3)))
            raise
        usage = getattr(response, "usage", None)
        record(dict(event, status=getattr(response, "status", None) or "unknown",
                    response_id=getattr(response, "id", None), request_id=getattr(response, "_request_id", None),
                    returned_model=getattr(response, "model", None),
                    input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None),
                    elapsed_s=round(time.monotonic() - started, 3)))
        return response

    Responses.create = create


_install()
