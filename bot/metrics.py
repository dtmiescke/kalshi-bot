"""Shared accounting metrics for offline contract simulations.

Keep net P&L and ROI capital definitions in one place so model simulations
and historical replay cannot silently report different returns.
"""


def binary_contract_roi(trades):
    """Return (net_pnl, committed_capital, roi) for binary contract trades.

    Each trade has won (bool), cost (premium per contract), and fee (fee per
    contract), all in dollars. A win pays $1 per contract. ROI denominator
    includes both premium and fees.
    """
    net_pnl = 0.0
    committed = 0.0
    for trade in trades:
        cost = float(trade["cost"])
        fee = float(trade["fee"])
        net_pnl += (1.0 - cost if trade["won"] else -cost) - fee
        committed += cost + fee
    return net_pnl, committed, (net_pnl / committed if committed > 0 else None)
