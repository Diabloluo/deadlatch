# Disclaimer

**Read this in full before using Deadlatch.** A short summary is provided
in the README; this document is the authoritative statement.

## Not investment advice

Deadlatch is a technical risk-evaluation tool. It does not provide
investment, financial, legal, or tax advice. Nothing in this repository, its
documentation, examples, or outputs constitutes a recommendation to buy, sell,
or hold any security. No rule threshold, policy example, or default limit in
this repository is advice about how you should trade.

## No guarantee against loss

Deadlatch evaluates orders against the policy you configure. It cannot
predict market moves, and it cannot prevent losses — including losses caused by
orders it evaluated as allowed (PASS or WARN), by orders placed without calling
it, by orders that ignored its BLOCK, or by incorrect inputs or configurations.
**The tool is a gate, not an insurance policy.** Do not trade capital you are
not prepared to lose.

## You are responsible for your inputs and rules

The guard evaluates exactly what you give it:

- **Inputs:** order, portfolio snapshot, and policy are provided by you. A
  stale, fabricated, or malformed snapshot can produce a wrong verdict.
  Validate your data sources yourself.
- **Rules:** the 12 rules and their thresholds come from the policy you write.
  You are responsible for configuring limits that match your actual account,
  instruments, and risk tolerance.
- **Fail-closed is not correctness:** BLOCK on missing data is safe-by-default,
  but a policy that is too loose, or inputs that are wrong in ways the schema
  cannot detect, are outside this tool's control.

## The guard never places orders

Deadlatch has no broker connectivity, no order routing, and no execution
capability. It only evaluates. Any order submission is performed exclusively by
you or by the system you integrate it into.

## Advisory-only and bypass

Deadlatch is advisory. It cannot force an agent that never calls it to
call it, and it cannot stop an agent that ignores a BLOCK from submitting
orders elsewhere. Whether the agent calls the guard and honors the result is
the integrator's decision. Do not assume the guard can stop a fully bypassing
agent.

## Examples are fictional

All tickers, orders, portfolios, policies, and numbers in examples, quick
starts, tests, and generated artifacts are fictional. They exist to demonstrate
mechanics, not to suggest real instruments, positions, or strategies. Any
resemblance to real securities or accounts is coincidental.

## Test before live trading

The project is pre-1.0 software (`0.1.0.dev1`). Before using it with real
capital, simulate, backtest, and verify behavior on your own data and against
your own broker semantics. Do not deploy it for live trading based solely on
this documentation.

## No SLA

This project is provided as-is without any support or service-level commitment.
See the MIT [LICENSE](LICENSE) for the full warranty disclaimer.
