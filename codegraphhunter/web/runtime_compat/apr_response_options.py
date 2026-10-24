"""Responses parameters shared by APR experiments and graph queries."""


def response_parameters(model, parameters):
    result = dict(parameters, model=model)
    if model.startswith(("gpt-5", "gpt-6")):
        for name in ("temperature", "top_p", "top_logprobs"):
            result.pop(name, None)
        if not model.endswith("-pro"):
            result.setdefault("reasoning", {"effort": "low"})
        # Reasoning and visible output share the output-token allowance.
        minimum = 16384 if model.endswith("-pro") else 8192
        budget = result.get("max_output_tokens")
        result["max_output_tokens"] = max(minimum, budget if isinstance(budget, int) else 0)
    return result
