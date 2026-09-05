"""AI trade-analyst layer (advisory). Reviews already-screened + already-sized CSP candidates.

Guardrail: the AI can only flag or skip a trade — never widen risk, increase size, or approve
anything the deterministic screen/sizer rejected. Fail-open: any error means no verdict and the
trade proceeds under the rules.
"""
