"""Budget guardrail: a token cap per call, a spend ceiling and a kill switch.

Same rules as the browser lab "Fund it safely" (Unit 0, lesson 2).
"""


class BudgetExceeded(Exception):
    """Raised before a model call that would break the budget. The message is the reason code."""


def cost_usd(usage: dict, price_in_per_m: float, price_out_per_m: float) -> float:
    return (usage.get("input_tokens", 0) * price_in_per_m + usage.get("output_tokens", 0) * price_out_per_m) / 1_000_000


class Budget:
    def __init__(self, max_cost_usd: float, max_tokens_per_call: int, kill_switch: bool = False):
        self.max_cost_usd = max_cost_usd
        self.max_tokens_per_call = max_tokens_per_call
        self.kill_switch = kill_switch
        self.spent = 0.0

    def authorize(self, max_tokens: int) -> None:
        """Call before every model call.

        Raise BudgetExceeded("KILL_SWITCH") if the kill switch is on,
        BudgetExceeded("TOKEN_CAP") if max_tokens is above the per-call cap,
        BudgetExceeded("BUDGET_EXCEEDED") once spend has reached the ceiling.
        """
        if self.kill_switch:
            raise BudgetExceeded("KILL_SWITCH")
        if max_tokens > self.max_tokens_per_call:
            raise BudgetExceeded("TOKEN_CAP")
        if self.spent >= self.max_cost_usd:
            raise BudgetExceeded("BUDGET_EXCEEDED")

    def record(self, cost: float) -> None:
        """Add the cost of a finished call to the running spend."""
        self.spent += cost

    def available(self) -> bool:
        return not self.kill_switch and self.spent < self.max_cost_usd
